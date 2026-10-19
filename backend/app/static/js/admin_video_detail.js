window.AVi18n ||= {t: value => value, tpl: (parts, ...values) => parts.reduce((out, part, i) => out + part + (i < values.length ? values[i] : ''), '')};
/* ABOODVIDEO Admin Video Details (Phase 5). */
document.addEventListener('DOMContentLoaded', () => {
    const params = new URLSearchParams(window.location.search);
    const videoId = params.get('v');
    const feedback = document.getElementById('video-feedback');

    const statusLabels = { READY: window.AVi18n.t('جاهز'), PROCESSING: window.AVi18n.t('قيد المعالجة'), FAILED: window.AVi18n.t('فشل'), CANCELLED: window.AVi18n.t('ملغى'), UPLOADING: window.AVi18n.t('جارٍ الرفع'), UPLOADED: window.AVi18n.t('تم الرفع'), QUEUED: window.AVi18n.t('بالانتظار'), PROBING: window.AVi18n.t('فحص المصدر'), VERIFYING: window.AVi18n.t('تحقق') };
    const visibilityLabels = { PRIVATE: window.AVi18n.t('خاص'), UNLISTED: window.AVi18n.t('غير مدرج'), PUBLIC: window.AVi18n.t('عام'), PASSWORD_PROTECTED: window.AVi18n.t('محمي بكلمة سر') };

    const fmtBytes = (bytes) => {
        if (!bytes) return '0 B';
        const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
        const i = Math.floor(Math.log(bytes) / Math.log(1024));
        return (bytes / Math.pow(1024, i)).toFixed(i >= 3 ? 1 : 2) + ' ' + sizes[i];
    };
    const fmtDate = (iso) => {
        if (!iso) return '—';
        try { return new Date(iso).toLocaleString((document.documentElement.lang === 'en' ? 'en-US' : 'ar'), { dateStyle: 'medium', timeStyle: 'short' }); }
        catch (_) { return iso; }
    };
    const fmtDuration = (sec) => {
        if (!sec || isNaN(sec)) return '00:00';
        const s = Math.round(sec);
        const h = Math.floor(s / 3600);
        const m = Math.floor((s % 3600) / 60);
        const remS = s % 60;
        const mm = String(m).padStart(2, '0');
        const ss = String(remS).padStart(2, '0');
        return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
    };
    const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    const el = (tag, text, cls = []) => { const e = document.createElement(tag); if (text !== '') e.textContent = String(text); e.classList.add(...cls); return e; };
    const dtPair = (grid, label, value) => {
        const tile = el('div', '', ['spec-tile']);
        tile.append(el('span', label, ['spec-tile-label']), el('span', value, ['spec-tile-val']));
        grid.appendChild(tile);
    };

    function showError(message) {
        document.getElementById('video-loading').classList.add('hidden');
        feedback.textContent = message;
        feedback.classList.remove('hidden');
        feedback.classList.add('is-error');
    }

    const tabNames = ['overview','media','subtitles','processing','sharing','storage','analytics'];
    function activateTab(name, updateUrl = true) {
        if (!tabNames.includes(name)) name = 'overview';
        document.querySelectorAll('.detail-tab').forEach(tab => {
            const active = tab.id === 'tab-btn-' + name;
            tab.classList.toggle('is-active', active);
            tab.setAttribute('aria-selected', String(active));
            tab.tabIndex = active ? 0 : -1;
        });
        document.querySelectorAll('.tab-panel').forEach(panel => {
            const active = panel.id === 'tab-' + name;
            panel.classList.toggle('hidden', !active);
            panel.hidden = !active;
        });
        if (updateUrl) {const url = new URL(location.href); url.hash = name; history.replaceState(null,'',url);}
    }
    function selectFromUrl() {
        const requested = location.hash.slice(1) || params.get('tab') || 'overview';
        activateTab(requested === 'inspection' ? 'media' : requested, false);
        if (requested === 'inspection') {
            const inspector = document.getElementById('video-inspector');
            if (inspector) inspector.open = true;
        }
    }
    document.querySelectorAll('.detail-tab').forEach(tab => {
        tab.addEventListener('click', () => activateTab(tab.id.replace('tab-btn-', '')));
        tab.addEventListener('keydown', event => {
            if (!['ArrowRight','ArrowLeft','Home','End'].includes(event.key)) return;
            event.preventDefault();
            const index = tabNames.indexOf(tab.id.replace('tab-btn-',''));
            const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabNames.length - 1 : (index + (event.key === 'ArrowLeft' ? 1 : -1) + tabNames.length) % tabNames.length;
            activateTab(tabNames[next]); document.getElementById('tab-btn-'+tabNames[next]).focus();
        });
    });
    window.addEventListener('hashchange', selectFromUrl);
    window.addEventListener('popstate', selectFromUrl);
    selectFromUrl();

    async function loadAll() {
        if (!videoId) { showError(window.AVi18n.t('لم يتم تحديد فيديو. ارجع للمكتبة واختر فيديو.')); return; }
        let video;
        try {
            const res = await fetch(`/api/v1/admin/videos/${encodeURIComponent(videoId)}`, { credentials: 'include', cache: 'no-store' });
            if (!res.ok) throw new Error();
            video = await res.json();
        } catch (_) {
            showError(window.AVi18n.t('تعذر تحميل بيانات الفيديو — قد يكون محذوفاً.'));
            return;
        }
        render(video);
        document.dispatchEvent(new CustomEvent('abv-video-loaded', {detail: {id: video.id}}));
        selectFromUrl();
        loadJobs(video.id);
    }

    function render(video) {
        document.getElementById('video-loading').classList.add('hidden');
        document.getElementById('video-detail').classList.remove('hidden');
        document.title = `${video.title} | ABOODVIDEO`;

        document.getElementById('detail-title').textContent = video.title;
        const parts = [video.category, video.series_name, video.season_number ? window.AVi18n.tpl`الموسم ${video.season_number}` : '', video.episode_number ? window.AVi18n.tpl`الحلقة ${video.episode_number}` : ''].filter(Boolean);
        document.getElementById('detail-subtitle').textContent = parts.join(' · ') || video.source_filename;

        const badges = document.getElementById('detail-badges');
        badges.replaceChildren(
            el('span', statusLabels[video.status] || video.status, ['badge', `status-${String(video.status).toLowerCase()}`]),
            el('span', visibilityLabels[video.visibility] || video.visibility, ['badge', 'badge-muted']),
        );
        const top = video.available_renditions && video.available_renditions.length
            ? video.available_renditions[video.available_renditions.length - 1].quality : null;
        if (top) badges.appendChild(el('span', top === '2160p' ? '4K' : top, ['badge', 'badge-quality']));
        if (video.subtitle_count) badges.appendChild(el('span', window.AVi18n.tpl`${video.subtitle_count} ترجمة`, ['badge', 'badge-muted']));

        const bcrumb = document.getElementById('breadcrumb-title');
        if (bcrumb) bcrumb.textContent = video.title;

        const thumb = document.getElementById('detail-thumb');
        if (video.status === 'READY') {
            const img = new Image();
            img.alt = '';
            img.onload = () => thumb.replaceChildren(img);
            img.onerror = () => {};
            img.src = `/api/v1/admin/videos/${video.id}/poster`;
        }

        const preview = document.getElementById('btn-preview');
        const heroPlay = document.getElementById('hero-play-action');
        if (video.status === 'READY') {
            const watchUrl = `/admin/videos/${encodeURIComponent(String(video.id))}/watch`;
            preview.href = watchUrl;
            preview.classList.remove('hidden');
            if (heroPlay) heroPlay.href = watchUrl;
        }

        const inspectBtn = document.getElementById('btn-inspect');
        if (inspectBtn) {
            inspectBtn.href = `/admin/videos/detail?v=${encodeURIComponent(String(video.id))}#inspection`;
            inspectBtn.addEventListener('click', event => {
                event.preventDefault();
                const url = new URL(location.href); url.hash = 'inspection'; history.pushState(null,'',url);
                selectFromUrl(); document.getElementById('video-inspector')?.scrollIntoView({behavior:'smooth',block:'start'});
            });
        }

        const quickEmbedBtn = document.getElementById('btn-quick-embed');
        if (quickEmbedBtn) {
            quickEmbedBtn.addEventListener('click', () => {
                activateTab('sharing');
                const embedSec = document.getElementById('embed-generator-section');
                if (embedSec) embedSec.scrollIntoView({ behavior: 'smooth' });
            });
        }

        document.getElementById('btn-copy-link').addEventListener('click', async () => {
            const link = video.share_id && !video.share_revoked
                ? `${window.location.origin}/watch/${video.share_id}`
                : `${window.location.origin}/admin/videos/${video.id}/watch`;
            try { await navigator.clipboard.writeText(link); feedbackMessage(window.AVi18n.t('تم نسخ رابط المشاهدة.')); }
            catch (_) { feedbackMessage(window.AVi18n.t('تعذر نسخ الرابط.'), true); }
        });

        // Populate Header & Human-readable Specs box
        const qualityText = top ? (top === '2160p' ? '4K UHD' : top) : (video.source_resolution_height ? `${video.source_resolution_height}p` : '—');
        const durationText = fmtDuration(video.source_duration_seconds);

        const heroQuality = document.getElementById('hero-quality-chip');
        if (heroQuality) heroQuality.textContent = qualityText;

        const heroDuration = document.getElementById('hero-duration-chip');
        if (heroDuration) heroDuration.textContent = durationText;

        const specFilename = document.getElementById('spec-source-filename');
        if (specFilename) specFilename.textContent = video.source_filename || '—';

        const specQuality = document.getElementById('spec-quality');
        if (specQuality) specQuality.textContent = qualityText;

        const specDuration = document.getElementById('spec-duration');
        if (specDuration) specDuration.textContent = durationText;

        const specSize = document.getElementById('spec-size');
        if (specSize) specSize.textContent = fmtBytes(video.source_size_bytes);

        const specSubs = document.getElementById('spec-subtitles');
        if (specSubs) specSubs.textContent = video.subtitle_count ? window.AVi18n.tpl`${video.subtitle_count} مسار متوفر` : window.AVi18n.t('لا يوجد');

        // Download Policy Controls
        const policySelect = document.getElementById('detail-policy-select');
        const effectiveBadge = document.getElementById('detail-effective-badge');
        const directDlBtn = document.getElementById('btn-direct-download');
        const savePolicyBtn = document.getElementById('btn-save-policy');
        const policyFeedback = document.getElementById('policy-feedback');

        function updatePolicyBadge(allowed) {
            if (!effectiveBadge) return;
            if (allowed) {
                effectiveBadge.textContent = window.AVi18n.t('مسموح');
                effectiveBadge.className = 'policy-live-badge allowed';
            } else {
                effectiveBadge.textContent = window.AVi18n.t('معطل');
                effectiveBadge.className = 'policy-live-badge denied';
            }
        }

        if (policySelect) {
            policySelect.value = video.allow_download_policy || 'INHERIT';
        }
        updatePolicyBadge(video.effective_download_allowed);

        if (directDlBtn) {
            directDlBtn.href = `/api/v1/playback/direct-download/${video.id}`;
        }

        async function savePolicy() {
            const val = policySelect ? policySelect.value : 'INHERIT';
            try {
                const res = await fetch(`/api/v1/admin/videos/${video.id}`, {
                    method: 'PATCH',
                    credentials: 'include',
                    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': getCsrf() },
                    body: JSON.stringify({ allow_download_policy: val }),
                });
                if (!res.ok) throw new Error();
                const updated = await res.json();
                video.allow_download_policy = updated.allow_download_policy;
                video.effective_download_allowed = updated.effective_download_allowed;
                updatePolicyBadge(updated.effective_download_allowed);
                if (policyFeedback) {
                    policyFeedback.textContent = window.AVi18n.t('✓ تم حفظ سياسة التنزيل بنجاح');
                    setTimeout(() => { policyFeedback.textContent = ''; }, 3500);
                }
                feedbackMessage(window.AVi18n.t('تم حفظ سياسة التنزيل بنجاح.'));
            } catch (_) {
                feedbackMessage(window.AVi18n.t('تعذر حفظ سياسة التنزيل.'), true);
            }
        }

        if (savePolicyBtn) savePolicyBtn.onclick = savePolicy;
        if (policySelect) policySelect.onchange = savePolicy;

        // Overview Grid
        const overview = document.getElementById('overview-grid');
        overview.replaceChildren();
        dtPair(overview, window.AVi18n.t('العنوان'), video.title);
        dtPair(overview, window.AVi18n.t('الوصف'), video.description || '—');
        dtPair(overview, window.AVi18n.t('المسلسل'), video.series_name || '—');
        dtPair(overview, window.AVi18n.t('الموسم'), video.season_number || '—');
        dtPair(overview, window.AVi18n.t('الحلقة'), video.episode_number || '—');
        dtPair(overview, window.AVi18n.t('المجلد'), video.category || '—');
        dtPair(overview, window.AVi18n.t('سياسة التنزيل المحددة'), video.allow_download_policy === 'ALLOW' ? window.AVi18n.t('سماح بالتنزيل دائماً (ALLOW)') : (video.allow_download_policy === 'DENY' ? window.AVi18n.t('تعطيل التنزيل دائماً (DENY)') : window.AVi18n.t('موروثة من الأصل (INHERIT)')));
        dtPair(overview, window.AVi18n.t('الوضع الفعلي للتنزيل'), video.effective_download_allowed ? window.AVi18n.t('مسموح') : window.AVi18n.t('معطل'));
        dtPair(overview, window.AVi18n.t('سياسة الظهور'), `${visibilityLabels[video.visibility] || video.visibility} (${video.visibility})`);
        dtPair(overview, window.AVi18n.t('الحالة'), statusLabels[video.status] || video.status);
        dtPair(overview, window.AVi18n.t('أضيف في'), fmtDate(video.created_at));

        // Media
        const media = document.getElementById('media-grid');
        media.replaceChildren();
        dtPair(media, window.AVi18n.t('ملف المصدر'), video.source_filename);
        dtPair(media, window.AVi18n.t('أبعاد المصدر'), video.source_resolution_width ? `${video.source_resolution_width}×${video.source_resolution_height}` : window.AVi18n.t('غير معروف'));
        dtPair(media, window.AVi18n.t('الكوديك'), video.source_codec || window.AVi18n.t('غير معروف'));
        dtPair(media, window.AVi18n.t('معدل البت'), video.source_bitrate ? `${Math.round(video.source_bitrate / 1000)} kbps` : window.AVi18n.t('غير معروف'));
        dtPair(media, window.AVi18n.t('المدة'), video.source_duration_seconds ? window.AVi18n.tpl`${Math.round(video.source_duration_seconds)} ثانية` : window.AVi18n.t('غير معروف'));
        dtPair(media, window.AVi18n.t('حجم المصدر'), fmtBytes(video.source_size_bytes));

        const rendTbody = document.getElementById('renditions-tbody');
        rendTbody.replaceChildren();
        if (video.available_renditions && video.available_renditions.length) {
            for (const r of [...video.available_renditions].reverse()) {
                const tr = document.createElement('tr');
                tr.append(
                    el('td', r.quality === '2160p' ? '4K' : r.quality),
                    el('td', `${r.width}×${r.height}`),
                    el('td', r.codec || '—'),
                    el('td', r.bitrate ? `${Math.round(r.bitrate / 1000)} kbps` : '—'),
                    el('td', fmtBytes(r.size_bytes)),
                );
                rendTbody.appendChild(tr);
            }
        } else {
            const tr = el('tr', '');
            const td = el('td', window.AVi18n.t('لم تُنشر جودات بعد — الفيديو بحاجة إلى معالجة.'), ['table-empty-cell']);
            td.colSpan = 5;
            tr.appendChild(td);
            rendTbody.appendChild(tr);
        }

        // Sharing
        const sharing = document.getElementById('sharing-grid');
        sharing.replaceChildren();
        dtPair(sharing, window.AVi18n.t('سياسة الظهور'), visibilityLabels[video.visibility] || video.visibility);
        dtPair(sharing, window.AVi18n.t('الرابط السري'), video.share_id && !video.share_revoked ? window.AVi18n.t('فعّال') : (video.share_revoked ? window.AVi18n.t('ملغى') : window.AVi18n.t('غير مولّد')));
        dtPair(sharing, window.AVi18n.t('رمز مرور المشاهدين'), video.has_viewer_password ? window.AVi18n.t('مفعّل') : window.AVi18n.t('غير مفعّل'));
        const shareUrl = video.share_id ? `${window.location.origin}/watch/${video.share_id}` : '';
        const baseUrl = video.share_id ? `${window.location.origin}/embed/${video.share_id}` : '';

        function updateEmbedCode() {
            if (!video.share_id) return;
            const params = new URLSearchParams();

            const col = (document.getElementById('embed-cfg-color')?.value || '').trim().replace('#', '');
            if (col && col !== '00d2be') params.set('color', col);

            const wm = (document.getElementById('embed-cfg-watermark')?.value || '').trim();
            if (wm && wm !== 'ABOODVIDEO') params.set('watermark', wm);

            const skipVal = parseInt(document.getElementById('embed-cfg-skip')?.value || '85', 10);
            if (!isNaN(skipVal) && skipVal !== 85) params.set('skip', String(skipVal));

            if (document.getElementById('embed-cfg-autoplay')?.checked) params.set('autoplay', '1');
            if (document.getElementById('embed-cfg-banner')?.checked) params.set('banner', '1');

            const qs = params.toString();
            const finalEmbedUrl = qs ? `${baseUrl}?${qs}` : baseUrl;
            const snippet = `<iframe src="${finalEmbedUrl}" width="100%" height="100%" frameborder="0" allowfullscreen allow="autoplay; fullscreen; picture-in-picture"></iframe>`;

            const iframeInput = document.getElementById('embed-iframe-code');
            if (iframeInput) iframeInput.value = snippet;
        }

        document.querySelectorAll('.btn-color-dot').forEach(btn => {
            btn.addEventListener('click', () => {
                const colorInput = document.getElementById('embed-cfg-color');
                if (colorInput) {
                    colorInput.value = btn.getAttribute('data-col');
                    updateEmbedCode();
                }
            });
        });

        ['embed-cfg-color', 'embed-cfg-watermark', 'embed-cfg-skip', 'embed-cfg-autoplay', 'embed-cfg-banner'].forEach(id => {
            const el = document.getElementById(id);
            if (el) {
                el.addEventListener('input', updateEmbedCode);
                el.addEventListener('change', updateEmbedCode);
            }
        });

        document.getElementById('share-link').value = shareUrl;
        updateEmbedCode();

        const copyBtn = document.getElementById('btn-copy-share');
        copyBtn.addEventListener('click', () => {
            const link = document.getElementById('share-link').value;
            if (link) navigator.clipboard.writeText(link).then(() => feedbackMessage(window.AVi18n.t('تم نسخ الرابط.')), () => feedbackMessage(window.AVi18n.t('تعذر النسخ.'), true));
        });

        const copyIframeBtn = document.getElementById('btn-copy-iframe-code');
        if (copyIframeBtn) {
            copyIframeBtn.addEventListener('click', () => {
                const code = document.getElementById('embed-iframe-code').value;
                const feedbackSpan = document.getElementById('embed-copy-feedback');
                if (code) {
                    navigator.clipboard.writeText(code).then(() => {
                        feedbackMessage(window.AVi18n.t('تم نسخ كود الـ iframe بنجاح!'));
                        if (feedbackSpan) {
                            feedbackSpan.textContent = window.AVi18n.t('✓ تم نسخ كود التضمين للحافظة!');
                            setTimeout(() => { feedbackSpan.textContent = ''; }, 3500);
                        }
                    }, () => feedbackMessage(window.AVi18n.t('تعذر نسخ الكود.'), true));
                }
            });
        }
        const rotateBtn = document.getElementById('btn-rotate-share');
        rotateBtn.addEventListener('click', async () => {
            const token = getCsrf();
            const res = await fetch(`/api/v1/admin/videos/${video.id}`, {
                method: 'PATCH', credentials: 'include',
                headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': token },
                body: JSON.stringify({ rotate_share_link: true })
            });
            if (res.ok) {
                const updated = await res.json();
                document.getElementById('share-link').value = `${window.location.origin}/watch/${updated.share_id}`;
                feedbackMessage(window.AVi18n.t('تم تجديد الرابط وإبطال القديم.'));
            } else {
                feedbackMessage(window.AVi18n.t('تعذر تجديد الرابط.'), true);
            }
        });

        // Storage
        const storage = document.getElementById('storage-grid');
        storage.replaceChildren();
        const renditionsSize = (video.available_renditions || []).reduce((sum, r) => sum + (r.size_bytes || 0), 0);
        dtPair(storage, window.AVi18n.t('المصدر'), fmtBytes(video.source_size_bytes));
        dtPair(storage, window.AVi18n.t('ملفات HLS'), fmtBytes(renditionsSize));
        dtPair(storage, window.AVi18n.t('الإجمالي التقريبي'), fmtBytes(video.source_size_bytes + renditionsSize));

        // Analytics
        const analytics = document.getElementById('analytics-grid');
        analytics.replaceChildren();
        dtPair(analytics, window.AVi18n.t('المشاهدات'), video.view_count);
        dtPair(analytics, window.AVi18n.t('البيانات المنقولة'), fmtBytes(video.bandwidth_bytes));
    }

    async function loadJobs(id) {
        const box = document.getElementById('detail-jobs-list');
        try {
            const res = await fetch(`/api/v1/admin/transcodes?video_id=${encodeURIComponent(id)}&limit=10`, { credentials: 'include', cache: 'no-store' });
            if (!res.ok) throw new Error();
            const data = await res.json();
            const jobs = data.items || [];
            if (!jobs.length) {
                box.replaceChildren(el('div', window.AVi18n.t('لا توجد وظائف معالجة مسجلة لهذا الفيديو.'), ['empty-hint-sm']));
                return;
            }
            box.replaceChildren(...jobs.map(j => {
                const item = el('div', '', ['job-item']);
                const header = el('div', '', ['job-item-header']);
                header.append(
                    el('span', statusLabels[j.status] || j.status, ['badge', `status-${String(j.status).toLowerCase()}`]),
                    el('span', window.AVi18n.tpl`المحاولة ${j.attempt}/${j.max_attempts}`, ['badge', 'badge-muted']),
                    el('span', j.id, ['job-item-id']),
                );
                const meta = el('div',
                    window.AVi18n.tpl`بدأت: ${fmtDate(j.started_at)} · اكتملت: ${fmtDate(j.completed_at)} · العامل: ${j.worker_id || '—'}` +
                    (j.current_rendition ? window.AVi18n.tpl` · الجودة: ${j.current_rendition}` : '') +
                    (j.ffmpeg_fps ? ` · ${Math.round(j.ffmpeg_fps)} FPS` : ''), ['job-item-meta']);
                item.append(header, meta);
                if (window.AVi18n.t(j.error_message)) item.appendChild(el('div', window.AVi18n.t(j.error_message), ['job-error']));
                return item;
            }));
        } catch (_) {
            box.replaceChildren(el('div', window.AVi18n.t('تعذر تحميل سجل المعالجة.'), ['empty-hint-sm']));
        }
    }

    function getCsrf() {
        for (const c of document.cookie.split(';')) {
            const [k, v] = c.trim().split('=');
            if (k === (document.body.dataset.csrfCookieName || 'aboodvideo_csrf')) return v;
        }
        return '';
    }

    function feedbackMessage(message, isError = false) {
        feedback.textContent = message;
        feedback.classList.remove('hidden');
        feedback.classList.toggle('is-error', !!isError);
        window.clearTimeout(feedbackMessage._t);
        feedbackMessage._t = window.setTimeout(() => feedback.classList.add('hidden'), 5000);
    }

    loadAll();
});
