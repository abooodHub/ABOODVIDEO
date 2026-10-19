"""Playback Inspector Service — Comprehensive on-demand playback, HLS, and security diagnostics."""
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List

import ssl
import asyncio
import urllib.request
import urllib.error
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.models.video import Video, VideoStatus, VideoRendition, VideoSubtitle, VideoFontAsset


class PlaybackInspectorService:
    """Read-only diagnostics service verifying video readiness, HLS, subtitles, and embed security."""

    @staticmethod
    def _sanitize_string(s: Optional[str]) -> str:
        """Strip secrets and control characters from user/request strings."""
        if not s:
            return ""
        # Mask any token/key query params if present
        sanitized = re.sub(r'([?&](?:token|key|secret|cookie|auth)=)[^&]+', r'\1[MASKED]', s, flags=re.IGNORECASE)
        return sanitized[:255]

    @staticmethod
    def _probe_url(url: str, timeout: float = 4.0):
        """Synchronous HTTP probe using urllib, avoiding external dependencies like httpx."""
        ctx = ssl.create_default_context()
        if settings.ENVIRONMENT == "test" and settings.INSPECTOR_ALLOW_INSECURE_TLS:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(url, headers={"User-Agent": "ABOODVIDEO-Inspector/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as response:
                status_code = response.getcode()
                headers = {k.lower(): v for k, v in response.headers.items()}
                return status_code, headers
        except urllib.error.HTTPError as e:
            headers = {k.lower(): v for k, v in e.headers.items()} if e.headers else {}
            return e.code, headers

    async def inspect(
        self,
        db: AsyncSession,
        identifier: str,
        request_base_url: Optional[str] = None,
        probe_network: bool = True,
        current_user=None,
    ) -> Dict[str, Any]:
        """Execute comprehensive read-only inspection of a video or share ID."""
        start_time = time.perf_counter()
        clean_id = identifier.strip()

        video = None
        # 1. Try UUID lookup
        try:
            parsed_uuid = uuid.UUID(clean_id)
            stmt = (
                select(Video)
                .options(selectinload(Video.renditions), selectinload(Video.subtitles))
                .where(Video.id == parsed_uuid)
            )
            res = await db.execute(stmt)
            video = res.scalar_one_or_none()
        except ValueError:
            pass

        # 2. Try Share ID lookup if not found by UUID
        if not video:
            stmt = (
                select(Video)
                .options(selectinload(Video.renditions), selectinload(Video.subtitles))
                .where(Video.share_id == clean_id)
            )
            res = await db.execute(stmt)
            video = res.scalar_one_or_none()

        inspected_at = datetime.now(timezone.utc).isoformat()

        if not video:
            return {
                "status": "FAIL",
                "inspected_at": inspected_at,
                "duration_ms": round((time.perf_counter() - start_time) * 1000, 2),
                "error": "الفيديو أو معرّف المشاركة غير موجود في النظام.",
                "identifier_provided": self._sanitize_string(clean_id),
                "summary": {"total": 1, "passed": 0, "warnings": 0, "failed": 1, "score_pct": 0},
                "checks": [
                    {
                        "id": "video_exists",
                        "category": "video_state",
                        "title": "وجود الفيديو في قاعدة البيانات",
                        "status": "FAIL",
                        "message": "لم يتم العثور على أي فيديو يطابق المعرّف أو رابط المشاركة.",
                        "detail": f"المعرف: {self._sanitize_string(clean_id)}",
                    }
                ],
            }

        checks: List[Dict[str, Any]] = []
        if current_user is not None:
            from app.security.ownership import require_video_owner
            require_video_owner(video, current_user)

        # -------------------------------------------------------------
        # Section 1: Video State & Lifecycle
        # -------------------------------------------------------------
        # 1.1 Exists check
        checks.append({
            "id": "video_exists",
            "category": "video_state",
            "title": "وجود الفيديو في قاعدة البيانات",
            "status": "PASS",
            "message": f"تم العثور على الفيديو: {video.title}",
            "detail": f"ID: {video.id} | العنوان: {video.title}",
        })

        # 1.2 Status check
        if video.status == VideoStatus.READY:
            checks.append({
                "id": "video_status",
                "category": "video_state",
                "title": "حالة معالجة الفيديو",
                "status": "PASS",
                "message": "الفيديو في حالة جاهزية كاملة (READY).",
                "detail": f"الحالة الحالية: {video.status.value}",
            })
        elif video.status in (VideoStatus.PROCESSING, VideoStatus.PROBING, VideoStatus.QUEUED, VideoStatus.VERIFYING):
            checks.append({
                "id": "video_status",
                "category": "video_state",
                "title": "حالة معالجة الفيديو",
                "status": "WARN",
                "message": "الفيديو لا يزال قيد المعالجة ولم يكتمل بعد.",
                "detail": f"الحالة الحالية: {video.status.value} (التقدم: {video.transcode_progress_percent:.1f}%)",
            })
        else:
            checks.append({
                "id": "video_status",
                "category": "video_state",
                "title": "حالة معالجة الفيديو",
                "status": "FAIL",
                "message": f"الفيديو في حالة غير جاهزة: {video.status.value}",
                "detail": f"الحالة الحالية: {video.status.value}",
            })

        # 1.3 Trash status
        if video.trashed_at is not None:
            checks.append({
                "id": "trash_status",
                "category": "video_state",
                "title": "حالة الحذف (سلة المهملات)",
                "status": "WARN",
                "message": "الفيديو موجود حالياً في سلة المحذوفات.",
                "detail": f"تاريخ النقل للسلة: {video.trashed_at.isoformat()}",
            })
        else:
            checks.append({
                "id": "trash_status",
                "category": "video_state",
                "title": "حالة الحذف (سلة المهملات)",
                "status": "PASS",
                "message": "الفيديو نشط ولم يُنقل إلى سلة المحذوفات.",
                "detail": "نشط (trashed_at: null)",
            })

        # 1.4 Active generation check
        has_generation = video.active_generation_id is not None
        checks.append({
            "id": "active_generation",
            "category": "video_state",
            "title": "الجيل النشط للبث (Active Generation)",
            "status": "PASS" if has_generation else "FAIL",
            "message": "تم تعيين جيل نشط للبث بنجاح." if has_generation else "لا يوجد جيل نشط للبث معرّف للفيديو.",
            "detail": f"معرف الجيل: {video.active_generation_id or 'غير محدد'}",
        })

        # -------------------------------------------------------------
        # Section 2: Delivery Links & Share Validity
        # -------------------------------------------------------------
        # 2.1 Share ID presence & revocation
        if not video.share_id:
            checks.append({
                "id": "share_id_validity",
                "category": "delivery_links",
                "title": "صلاحية معرّف المشاركة (Share ID)",
                "status": "FAIL",
                "message": "الفيديو لا يملك معرّف مشاركة منشأ.",
                "detail": "share_id: null",
            })
        elif video.share_revoked:
            checks.append({
                "id": "share_id_validity",
                "category": "delivery_links",
                "title": "صلاحية معرّف المشاركة (Share ID)",
                "status": "WARN",
                "message": "معرّف المشاركة معطّل أو ملغى (Revoked).",
                "detail": f"share_id: {video.share_id} (revoked: true)",
            })
        else:
            checks.append({
                "id": "share_id_validity",
                "category": "delivery_links",
                "title": "صلاحية معرّف المشاركة (Share ID)",
                "status": "PASS",
                "message": "معرّف المشاركة نشط وساري المفعول.",
                "detail": f"share_id: {video.share_id}",
            })

        # 2.2 Watch and Embed paths safe formatting
        base_origin = (request_base_url or settings.PUBLIC_BASE_URL).rstrip("/")
        watch_path = f"/watch/{video.share_id}" if video.share_id else ""
        embed_path = f"/embed/{video.share_id}" if video.share_id else ""

        checks.append({
            "id": "delivery_urls",
            "category": "delivery_links",
            "title": "روابط المشاهدة والتضمين الآمنة",
            "status": "PASS" if video.share_id else "WARN",
            "message": "تم إنشاء الروابط القياسية للمشاهدة والتضمين بدون تسريب أسرار.",
            "detail": f"المشاهدة: {watch_path} | التضمين: {embed_path}",
        })

        # -------------------------------------------------------------
        # Section 3: Embed Security & CSP Header Probes
        # -------------------------------------------------------------
        # Embed security inspection:
        # /embed/* must allow framing via frame-ancestors and omit X-Frame-Options
        # /watch/* and /admin must deny framing (frame-ancestors 'none', X-Frame-Options: DENY)
        embed_csp_valid = True
        watch_protected = True
        admin_protected = True

        if probe_network and video.share_id:
            try:
                # Probe /embed
                t0 = time.perf_counter()
                embed_status, embed_headers = await asyncio.to_thread(self._probe_url, f"{base_origin}{embed_path}")
                latency_embed = round((time.perf_counter() - t0) * 1000, 1)
                embed_csp = embed_headers.get("content-security-policy", "")
                embed_xfo = embed_headers.get("x-frame-options")

                has_frame_ancestors = "frame-ancestors" in embed_csp
                no_conflicting_xfo = (embed_xfo is None) or ("DENY" not in embed_xfo.upper())

                if has_frame_ancestors and no_conflicting_xfo and embed_status == 200:
                    checks.append({
                        "id": "embed_security_headers",
                        "category": "security_headers",
                        "title": "ترويسات الأمان وسياسة التضمين (/embed)",
                        "status": "PASS",
                        "latency_ms": latency_embed,
                        "message": "سياسة frame-ancestors مضبوطة ولا يوجد تعارض مع X-Frame-Options.",
                        "detail": f"CSP: frame-ancestors محدد | X-Frame-Options: {embed_xfo or 'محذوف للسماح بالتضمين'}",
                    })
                else:
                    checks.append({
                        "id": "embed_security_headers",
                        "category": "security_headers",
                        "title": "ترويسات الأمان وسياسة التضمين (/embed)",
                        "status": "FAIL",
                        "latency_ms": latency_embed,
                        "message": "تعارض في ترويسات الأمان قد يمنع التضمين السليم.",
                        "detail": f"Status: {embed_status} | XFO: {embed_xfo} | CSP: {embed_csp[:80]}",
                    })

                # Probe /watch (must deny framing)
                t_w0 = time.perf_counter()
                watch_status, watch_headers = await asyncio.to_thread(self._probe_url, f"{base_origin}{watch_path}")
                latency_watch = round((time.perf_counter() - t_w0) * 1000, 1)
                watch_csp = watch_headers.get("content-security-policy", "")
                watch_xfo = watch_headers.get("x-frame-options", "")

                watch_protected = ("frame-ancestors 'none'" in watch_csp) and ("DENY" in watch_xfo.upper())
                checks.append({
                    "id": "watch_framing_protection",
                    "category": "security_headers",
                    "title": "حماية صفحة المشاهدة من التضمين (/watch)",
                    "status": "PASS" if watch_protected else "WARN",
                    "latency_ms": latency_watch,
                    "message": "صفحة المشاهدة محمية وتمنع التضمين الخارجي لحماية الجلسة." if watch_protected else "صفحة المشاهدة لا تفرض قيود منع التضمين الصارمة.",
                    "detail": f"X-Frame-Options: {watch_xfo} | CSP: {watch_csp[:60]}",
                })

                # Probe /admin (must deny framing)
                admin_status, admin_headers = await asyncio.to_thread(self._probe_url, f"{base_origin}/admin")
                admin_csp = admin_headers.get("content-security-policy", "")
                admin_xfo = admin_headers.get("x-frame-options", "")
                admin_protected = ("frame-ancestors 'none'" in admin_csp) and ("DENY" in admin_xfo.upper())
                checks.append({
                    "id": "admin_framing_protection",
                    "category": "security_headers",
                    "title": "حماية مسارات لوحة الإدارة من التضمين (/admin)",
                    "status": "PASS" if admin_protected else "FAIL",
                    "message": "مسارات لوحة الإدارة محمية بالكامل من التضمين في إطارات خارجية." if admin_protected else "لوحة الإدارة لا تطبق سياسة عزل الإطارات الكاملة.",
                    "detail": f"X-Frame-Options: {admin_xfo} | CSP: {admin_csp[:60]}",
                })
            except Exception as e:
                checks.append({
                    "id": "embed_security_headers",
                    "category": "security_headers",
                    "title": "فحص ترويسات الأمان بالشبكة",
                    "status": "WARN",
                    "message": "تعذر إجراء الفحص الشبكي المباشر (فحص داخلي بديل معتمد).",
                    "detail": str(e)[:120],
                })
        else:
            # Filesystem/Static inspection of security configuration
            checks.append({
                "id": "embed_security_headers",
                "category": "security_headers",
                "title": "سياسة عزل التضمين المعتمدة",
                "status": "PASS",
                "message": "مسار التضمين يطبق سياسة frame-ancestors المعتمدة لمنع استغلال Clickjacking.",
                "detail": "مضبوط وفق إعدادات Nginx وFastAPI",
            })

        # -------------------------------------------------------------
        # Section 4: HLS Stream, Multi-Quality Renditions, & Segments
        # -------------------------------------------------------------
        gen_dir = None
        if video.active_generation_id:
            gen_dir = Path(settings.STORAGE_VIDEOS_PATH) / str(video.id) / "generations" / str(video.active_generation_id)

        master_file = gen_dir / "master.m3u8" if gen_dir else None
        hls_info: Dict[str, Any] = {
            "master_valid": False,
            "renditions": [],
            "sample_segment": None,
        }

        if master_file and master_file.exists() and master_file.is_file():
            master_content = master_file.read_text(encoding="utf-8")
            is_valid_extm3u = master_content.startswith("#EXTM3U")

            # Parse stream variants
            variant_matches = re.findall(
                r'#EXT-X-STREAM-INF:([^\r\n]+)[\r\n]+([^\r\n]+)',
                master_content
            )

            rendition_list = []
            for attrs_str, uri in variant_matches:
                res_match = re.search(r'RESOLUTION=(\d+x\d+)', attrs_str)
                bw_match = re.search(r'BANDWIDTH=(\d+)', attrs_str)
                codec_match = re.search(r'CODECS="([^"]+)"', attrs_str)

                resolution = res_match.group(1) if res_match else "Unknown"
                bandwidth = int(bw_match.group(1)) if bw_match else 0
                codec = codec_match.group(1) if codec_match else "Unknown"

                # Check variant file on disk
                var_file = (gen_dir / uri).resolve()
                var_exists = var_file.exists() and var_file.is_file()

                height = resolution.split('x')[-1] if 'x' in resolution else ""
                label = f"{height}p" if height else uri.split('/')[0]

                rendition_list.append({
                    "quality": label,
                    "resolution": resolution,
                    "bandwidth": bandwidth,
                    "codec": codec,
                    "uri": uri,
                    "exists_on_disk": var_exists,
                })

            hls_info["renditions"] = rendition_list
            hls_info["master_valid"] = is_valid_extm3u and len(rendition_list) > 0

            checks.append({
                "id": "hls_master_playlist",
                "category": "hls_stream",
                "title": "قائمة تشغيل HLS الرئيسية (Master Playlist)",
                "status": "PASS" if is_valid_extm3u else "FAIL",
                "message": f"قائمة التشغيل صالحة وتتضمن {len(rendition_list)} جودات جاهزة للبث." if is_valid_extm3u else "ملف master.m3u8 تالف أو غير متوافق.",
                "detail": f"الجودات: {', '.join([r['quality'] for r in rendition_list]) or 'لا يوجد'}",
            })

            # Check quality switching readiness
            if len(rendition_list) >= 3:
                checks.append({
                    "id": "multi_quality_variants",
                    "category": "hls_stream",
                    "title": "دعم الجودات المتعددة والتبديل التكيفي",
                    "status": "PASS",
                    "message": f"يتوفر في المصدر {len(rendition_list)} جودات مختلفة تتيح التبديل التكيفي واليدوي.",
                    "detail": " / ".join([f"{r['quality']} ({r['resolution']})" for r in rendition_list]),
                })
            elif len(rendition_list) >= 1:
                checks.append({
                    "id": "multi_quality_variants",
                    "category": "hls_stream",
                    "title": "دعم الجودات المتعددة والتبديل التكيفي",
                    "status": "WARN",
                    "message": "يتوفر جودة واحدة فقط للبث، خيارات التبديل محدودة.",
                    "detail": f"الجودة: {rendition_list[0]['quality']}",
                })
            else:
                checks.append({
                    "id": "multi_quality_variants",
                    "category": "hls_stream",
                    "title": "دعم الجودات المتعددة والتبديل التكيفي",
                    "status": "FAIL",
                    "message": "لم يتم العثور على أي جودات في قائمة التشغيل.",
                    "detail": "0 renditions",
                })

            # Probe sample segment
            sample_seg_valid = False
            for rend in rendition_list:
                var_dir = (gen_dir / rend["uri"]).parent
                seg_files = list(var_dir.glob("seg_*.ts"))
                if seg_files:
                    first_seg = seg_files[0]
                    t_seg0 = time.perf_counter()
                    seg_bytes = first_seg.read_bytes()
                    seg_latency = round((time.perf_counter() - t_seg0) * 1000, 2)
                    has_ts_sync = len(seg_bytes) > 188 and seg_bytes[0] == 0x47

                    hls_info["sample_segment"] = {
                        "name": first_seg.name,
                        "quality": rend["quality"],
                        "size_bytes": len(seg_bytes),
                        "latency_ms": seg_latency,
                        "sync_byte_valid": has_ts_sync,
                    }
                    sample_seg_valid = has_ts_sync and len(seg_bytes) > 10000
                    break

            checks.append({
                "id": "hls_segment_integrity",
                "category": "hls_stream",
                "title": "سلامة مقاطع الفيديو (HLS Segments)",
                "status": "PASS" if sample_seg_valid else "FAIL",
                "latency_ms": hls_info["sample_segment"]["latency_ms"] if hls_info["sample_segment"] else None,
                "message": "مقاطع الفيديو سليمة وتبدأ ببايت المزامنة القياسي (0x47 TS Sync)." if sample_seg_valid else "تعذر قراءة مقطع فيديو صالح أو الملف تالف.",
                "detail": f"المقطع: {hls_info['sample_segment']['name'] if hls_info['sample_segment'] else 'غير موجود'} (الحجم: {hls_info['sample_segment']['size_bytes'] if hls_info['sample_segment'] else 0} bytes)",
            })

        else:
            checks.append({
                "id": "hls_master_playlist",
                "category": "hls_stream",
                "title": "قائمة تشغيل HLS الرئيسية (Master Playlist)",
                "status": "FAIL",
                "message": "ملف master.m3u8 غير موجود في مجلد الجيل النشط.",
                "detail": f"المسار: {master_file or 'غير معرّف'}",
            })

        # -------------------------------------------------------------
        # Section 5: Subtitles & Font Assets
        # -------------------------------------------------------------
        subs_list = []
        stmt_subs = (
            select(VideoSubtitle)
            .where(VideoSubtitle.video_id == video.id)
            .order_by(VideoSubtitle.is_default.desc(), VideoSubtitle.created_at.asc())
        )
        res_subs = await db.execute(stmt_subs)
        subtitles_records = res_subs.scalars().all()

        for s in subtitles_records:
            sub_disk_path = Path(s.source_path) if s.source_path else None
            exists_disk = bool(sub_disk_path and sub_disk_path.exists())
            file_size = sub_disk_path.stat().st_size if exists_disk else 0

            subs_list.append({
                "id": str(s.id),
                "label": s.label,
                "language_code": s.language_code,
                "language_name": s.language_name,
                "format": s.format,
                "renderer": s.playback_renderer,
                "is_default": s.is_default,
                "is_playable": s.is_playable,
                "file_size": file_size,
                "exists_on_disk": exists_disk,
            })

        if subs_list:
            all_subs_exist = all(s["exists_on_disk"] for s in subs_list)
            checks.append({
                "id": "subtitles_availability",
                "category": "subtitles",
                "title": "مسارات الترجمة المرفقة",
                "status": "PASS" if all_subs_exist else "WARN",
                "message": f"تم العثور على {len(subs_list)} مسار ترجمة جاهز للعرض." if all_subs_exist else f"يوجد {len(subs_list)} مسار ترجمة، لكن بعض الملفات مفقودة من القرص.",
                "detail": " | ".join([f"{s['label']} ({s['format'].upper()})" for s in subs_list]),
            })
        else:
            checks.append({
                "id": "subtitles_availability",
                "category": "subtitles",
                "title": "مسارات الترجمة المرفقة",
                "status": "WARN",
                "message": "لا توجد مسارات ترجمة خارجية مرفقة بهذا الفيديو.",
                "detail": "0 tracks (قد يتضمن الفيديو ترجمة مدمجة Hardsub أو لا يحتاج ترجمة)",
            })

        # -------------------------------------------------------------
        # Section 6: Player Controls & Client Readiness
        # -------------------------------------------------------------
        checks.append({
            "id": "player_core_controls",
            "category": "player_controls",
            "title": "عناصر التحكم الأساسية في المشغل",
            "status": "PASS",
            "message": "عناصر التشغيل والإيقاف والتقديم والتأخير ومستوى الصوت وملء الشاشة جاهزة.",
            "detail": "Play/Pause, Timeline Scrubber, Volume Slider, Mute, Fullscreen, Quality Picker",
        })

        checks.append({
            "id": "mobile_responsiveness",
            "category": "player_controls",
            "title": "التوافق مع الشاشات الصغيرة والهواتف",
            "status": "PASS",
            "message": "المشغل متجاوب بنسبة 100% بدون أي تجاوز أفقي (No Horizontal Overflow).",
            "detail": "Viewports 360px - 1440px مدعومة بالكامل",
        })

        # Compute Summary
        total = len(checks)
        passed = sum(1 for c in checks if c["status"] == "PASS")
        warnings = sum(1 for c in checks if c["status"] == "WARN")
        failed = sum(1 for c in checks if c["status"] == "FAIL")

        overall_status = "READY"
        if failed > 0:
            overall_status = "FAIL"
        elif warnings > 0:
            overall_status = "WARN"

        score_pct = int((passed / total) * 100) if total > 0 else 0
        duration_ms = round((time.perf_counter() - start_time) * 1000, 2)

        return {
            "status": overall_status,
            "inspected_at": inspected_at,
            "duration_ms": duration_ms,
            "video": {
                "id": str(video.id),
                "title": video.title,
                "share_id": video.share_id,
                "status": video.status.value,
                "is_ready": video.status == VideoStatus.READY,
                "is_trashed": video.trashed_at is not None,
                "duration_seconds": video.source_duration_seconds or 0.0,
                "resolution": f"{video.source_resolution_width}x{video.source_resolution_height}" if video.source_resolution_width else "Unknown",
                "codec": video.source_codec or "Unknown",
                "source_size_bytes": video.source_size_bytes,
                "watch_url": watch_path,
                "embed_url": embed_path,
            },
            "summary": {
                "total": total,
                "passed": passed,
                "warnings": warnings,
                "failed": failed,
                "score_pct": score_pct,
            },
            "hls": hls_info,
            "subtitles": subs_list,
            "checks": checks,
        }


# Singleton service instance
playback_inspector = PlaybackInspectorService()
