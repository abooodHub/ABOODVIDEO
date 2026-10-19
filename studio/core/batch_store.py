"""Persistent SQLite store for ABOODVIDEO Studio Season Batches & Episodes.

Uses SQLite with WAL mode, foreign keys, and atomic state transitions.
Schema: batches and episodes tables.
"""
from __future__ import annotations

import json
import inspect
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from studio.config import get_default_db_path

# Default persistent SQLite store in %LOCALAPPDATA%
DEFAULT_DB_PATH = get_default_db_path()
logger = logging.getLogger("studio.batch_store")


def _mutation_caller() -> str:
    frame = inspect.currentframe()
    try:
        frame = frame.f_back if frame else None
        while frame:
            module = frame.f_globals.get("__name__", "")
            if module != __name__:
                return f"{module}.{frame.f_code.co_name}"
            frame = frame.f_back
    finally:
        del frame
    return "unknown"


def _log_mutation(operation: str, caller: str, batch_id: str = "", episode_id: str = "", before: int = 0, after: int = 0) -> None:
    logger.info(
        "QUEUE_DB_MUTATION operation=%s caller=%s batch_id=%s episode_id=%s row_count_before=%d row_count_after=%d",
        operation, caller, batch_id, episode_id, before, after,
    )


def _as_dict(value: Any) -> Dict[str, Any]:
    """Decode optional JSON/object metadata without trusting legacy shapes."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        if not value.strip():
            return {}
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> List[Any]:
    """Decode optional JSON/list metadata, returning an empty list on legacy data."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        if not value.strip():
            return []
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
    return value if isinstance(value, list) else []


def _normalize_episode_history(data: Dict[str, Any]) -> None:
    settings_raw = data.get("settings_json")
    settings = _as_dict(settings_raw)
    if settings_raw not in (None, "", "{}") and not settings and settings_raw:
        logger.warning("Ignoring malformed optional settings metadata for episode %s", data.get("id", "unknown"))
    history_raw = settings.get("processing_history")
    history = _as_dict(history_raw)
    if history_raw is not None and not isinstance(history_raw, (dict, str)):
        logger.warning("Ignoring malformed optional processing history for episode %s", data.get("id", "unknown"))
    successful_raw = history.get("successful")
    successful = _as_dict(successful_raw)
    if successful_raw is not None and not isinstance(successful_raw, (dict, str)):
        logger.warning("Ignoring malformed optional success history for episode %s", data.get("id", "unknown"))
    data["settings"] = settings
    subtitle_metadata = _as_dict(settings.get("subtitle_metadata"))
    data["subtitle_metadata"] = subtitle_metadata
    for field in ("language_code", "language_name", "title", "forced", "default"):
        data[f"subtitle_{field}"] = subtitle_metadata.get(field)
    data["subtitle_display_label"] = subtitle_metadata.get("label") or data.get("subtitle_label")
    data["processing_history"] = history
    elapsed = successful.get("total_transcode_duration_seconds") or settings.get("encoding_elapsed_seconds")
    try:
        data["encoding_elapsed_seconds"] = float(elapsed) if elapsed is not None else None
    except (TypeError, ValueError):
        data["encoding_elapsed_seconds"] = None


class EpisodeState(str, Enum):
    """Episode processing states matching the ABOODVIDEO Studio audit model."""
    PENDING = "PENDING"
    PROBING = "PROBING"
    READY_TO_ENCODE = "READY_TO_ENCODE"
    ENCODING = "ENCODING"
    PACKAGING = "PACKAGING"
    VERIFYING_LOCAL = "VERIFYING_LOCAL"
    READY_TO_UPLOAD = "READY_TO_UPLOAD"
    UPLOADING = "UPLOADING"
    SERVER_VERIFYING = "SERVER_VERIFYING"
    READY = "READY"
    FAILED = "FAILED"
    PAUSED = "PAUSED"
    CANCELLED = "CANCELLED"
    SKIPPED = "SKIPPED"
    NEEDS_REVIEW = "NEEDS_REVIEW"


EPISODE_STATE_ARABIC: Dict[str, str] = {
    EpisodeState.PENDING.value: "في الانتظار",
    EpisodeState.PROBING.value: "جاري الفحص",
    EpisodeState.READY_TO_ENCODE.value: "جاهز للترميز",
    EpisodeState.ENCODING.value: "جاري الترميز",
    EpisodeState.PACKAGING.value: "جاري التجهيز",
    EpisodeState.VERIFYING_LOCAL.value: "التحقق المحلي",
    EpisodeState.READY_TO_UPLOAD.value: "جاهز للرفع",
    EpisodeState.UPLOADING.value: "جاري الرفع",
    EpisodeState.SERVER_VERIFYING.value: "تحقق الخادم",
    EpisodeState.READY.value: "جاهز (مكتمل)",
    EpisodeState.FAILED.value: "فشل",
    EpisodeState.PAUSED.value: "متوقف مؤقتاً",
    EpisodeState.CANCELLED.value: "ملغي",
    EpisodeState.SKIPPED.value: "تم التخطي",
    EpisodeState.NEEDS_REVIEW.value: "تحتاج مراجعة",
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def format_bytes(num_bytes: int) -> str:
    """Format bytes to human readable string (MB/GB)."""
    if num_bytes < 1024:
        return f"{num_bytes} B"
    elif num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} KB"
    elif num_bytes < 1024 * 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{num_bytes / (1024 * 1024 * 1024):.2f} GB"


class BatchStore:
    """SQLite-backed persistent store for season batches and episodes."""

    def __init__(self, db_path: Optional[Union[str, Path]] = None) -> None:
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        logger.info("DATABASE_OPEN path=%s", self.db_path.resolve())
        self.init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=30.0,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        # Enable WAL mode and foreign keys for durability and concurrent performance
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def init_db(self) -> None:
        """Initialize the database schema."""
        with self._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS batches (
                    id TEXT PRIMARY KEY,
                    season_folder_path TEXT NOT NULL,
                    destination_label TEXT NOT NULL DEFAULT '',
                    mode TEXT NOT NULL DEFAULT 'SEQUENTIAL',
                    status TEXT NOT NULL DEFAULT 'ACTIVE',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS episodes (
                    id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
                    order_index INTEGER NOT NULL,
                    source_path TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    display_label TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'PENDING',
                    current_stage TEXT NOT NULL DEFAULT '',
                    current_quality TEXT NOT NULL DEFAULT '',
                    progress REAL NOT NULL DEFAULT 0.0,
                    rendition_progress REAL NOT NULL DEFAULT 0.0,
                    fps REAL NOT NULL DEFAULT 0.0,
                    speed TEXT NOT NULL DEFAULT '0x',
                    eta TEXT NOT NULL DEFAULT '00:00:00',
                    elapsed_seconds REAL NOT NULL DEFAULT 0.0,
                    encoder_used TEXT NOT NULL DEFAULT '',
                    subtitle_path TEXT NOT NULL DEFAULT '',
                    subtitle_source TEXT NOT NULL DEFAULT 'none',
                    subtitle_format TEXT NOT NULL DEFAULT '',
                    subtitle_stream_index INTEGER,
                    subtitle_label TEXT NOT NULL DEFAULT '',
                    font_status TEXT NOT NULL DEFAULT '—',
                    missing_fonts TEXT NOT NULL DEFAULT '[]',
                    subtitle_candidates TEXT NOT NULL DEFAULT '[]',
                    settings_json TEXT NOT NULL DEFAULT '{}',
                    work_dir TEXT,
                    video_id TEXT,
                    session_id TEXT,
                    source_bytes INTEGER NOT NULL DEFAULT 0,
                    package_bytes INTEGER NOT NULL DEFAULT 0,
                    uploaded_bytes INTEGER NOT NULL DEFAULT 0,
                    local_package_cleaned INTEGER NOT NULL DEFAULT 0,
                    local_package_cleaned_at TEXT NOT NULL DEFAULT '',
                    freed_bytes INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    updated_at TEXT NOT NULL,
                    UNIQUE(batch_id, order_index),
                    UNIQUE(batch_id, source_path)
                );
                """
            )

            # Auto-migrate existing database tables if columns are missing
            existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(episodes);").fetchall()}
            for col_name, col_def in [
                ("current_stage", "TEXT NOT NULL DEFAULT ''"),
                ("current_quality", "TEXT NOT NULL DEFAULT ''"),
                ("progress", "REAL NOT NULL DEFAULT 0.0"),
                ("rendition_progress", "REAL NOT NULL DEFAULT 0.0"),
                ("fps", "REAL NOT NULL DEFAULT 0.0"),
                ("speed", "TEXT NOT NULL DEFAULT '0x'"),
                ("eta", "TEXT NOT NULL DEFAULT '00:00:00'"),
                ("elapsed_seconds", "REAL NOT NULL DEFAULT 0.0"),
                ("encoder_used", "TEXT NOT NULL DEFAULT ''"),
                ("subtitle_path", "TEXT NOT NULL DEFAULT ''"),
                ("subtitle_source", "TEXT NOT NULL DEFAULT 'none'"),
                ("subtitle_format", "TEXT NOT NULL DEFAULT ''"),
                ("subtitle_stream_index", "INTEGER DEFAULT NULL"),
                ("subtitle_label", "TEXT NOT NULL DEFAULT ''"),
                ("font_status", "TEXT NOT NULL DEFAULT '—'"),
                ("missing_fonts", "TEXT NOT NULL DEFAULT '[]'"),
                ("subtitle_candidates", "TEXT NOT NULL DEFAULT '[]'"),
                ("episode_number", "INTEGER DEFAULT NULL"),
                ("upload_session_id", "TEXT NOT NULL DEFAULT ''"),
                ("server_video_id", "TEXT NOT NULL DEFAULT ''"),
                ("upload_started_at", "TEXT NOT NULL DEFAULT ''"),
                ("server_status", "TEXT NOT NULL DEFAULT ''"),
                ("last_upload_error", "TEXT NOT NULL DEFAULT ''"),
                ("generation_id", "TEXT NOT NULL DEFAULT ''"),
                ("completed_at", "TEXT NOT NULL DEFAULT ''"),
                ("upload_speed", "TEXT NOT NULL DEFAULT '0 KB/s'"),
                ("upload_eta", "TEXT NOT NULL DEFAULT '00:00:00'"),
                ("local_package_cleaned", "INTEGER NOT NULL DEFAULT 0"),
                ("local_package_cleaned_at", "TEXT NOT NULL DEFAULT ''"),
                ("freed_bytes", "INTEGER NOT NULL DEFAULT 0"),
                ("duration_seconds", "REAL NOT NULL DEFAULT 0"),
                ("source_width", "INTEGER NOT NULL DEFAULT 0"),
                ("source_height", "INTEGER NOT NULL DEFAULT 0"),
                ("output_estimate_bytes", "INTEGER NOT NULL DEFAULT 0"),
                ("font_audit_json", "TEXT NOT NULL DEFAULT '{}'"),
            ]:
                if col_name not in existing_cols:
                    conn.execute(f"ALTER TABLE episodes ADD COLUMN {col_name} {col_def};")

            existing_batch_cols = {row["name"] for row in conn.execute("PRAGMA table_info(batches);").fetchall()}
            for col_name, col_def in [
                ("stop_after_current", "INTEGER NOT NULL DEFAULT 0"),
                ("pause_requested", "INTEGER NOT NULL DEFAULT 0"),
                ("target_folder_id", "TEXT NOT NULL DEFAULT ''"),
                ("target_series_id", "TEXT NOT NULL DEFAULT ''"),
                ("target_season_id", "TEXT NOT NULL DEFAULT ''"),
            ]:
                if col_name not in existing_batch_cols:
                    conn.execute(f"ALTER TABLE batches ADD COLUMN {col_name} {col_def};")

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_episodes_batch_order 
                ON episodes(batch_id, order_index);
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_batches_folder 
                ON batches(season_folder_path);
                """
            )
            conn.commit()

    def create_batch(
        self,
        season_folder_path: str,
        destination_label: str = "",
        mode: str = "SEQUENTIAL",
        batch_id: Optional[str] = None,
        target_folder_id: str = "",
        target_series_id: str = "",
        target_season_id: str = "",
    ) -> Dict[str, Any]:
        """Create a paused batch; importing media never authorizes processing."""
        bid = batch_id or str(uuid.uuid4())
        now = _utc_now_iso()
        norm_path = str(Path(season_folder_path).resolve())

        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO batches (id, season_folder_path, destination_label, mode, status, created_at, updated_at, target_folder_id, target_series_id, target_season_id)
                VALUES (?, ?, ?, ?, 'PAUSED', ?, ?, ?, ?, ?)
                """,
                (bid, norm_path, destination_label, mode, now, now, target_folder_id, target_series_id, target_season_id),
            )
            conn.commit()

        return self.get_batch(bid) or {}

    def get_batch(self, batch_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve batch by ID."""
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,))
            row = cursor.fetchone()
            if row:
                return dict(row)
        return None

    def find_batch_by_folder(self, season_folder_path: str) -> Optional[Dict[str, Any]]:
        """Find an existing active batch by folder path for duplicate prevention."""
        norm_path = str(Path(season_folder_path).resolve())
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM batches WHERE season_folder_path = ? ORDER BY created_at DESC LIMIT 1",
                (norm_path,),
            )
            row = cursor.fetchone()
            if row:
                return dict(row)
        return None

    def get_latest_active_batch(self) -> Optional[Dict[str, Any]]:
        """Get the most recently created active batch."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM batches WHERE status != 'DELETED' ORDER BY created_at DESC LIMIT 1"
            )
            row = cursor.fetchone()
            if row:
                return dict(row)
        return None

    def list_batches(self) -> List[Dict[str, Any]]:
        """List all batches."""
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT * FROM batches ORDER BY created_at DESC")
            return [dict(row) for row in cursor.fetchall()]

    def list_all_episodes(self) -> List[Dict[str, Any]]:
        """Return episode records across all seasons for durable ETA/history views."""
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT e.*, b.season_folder_path, b.destination_label, b.status AS batch_status "
                "FROM episodes e JOIN batches b ON b.id=e.batch_id "
                "ORDER BY e.updated_at DESC, e.order_index ASC"
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["settings"] = json.loads(item.get("settings_json") or "{}")
            except Exception:
                item["settings"] = {}
            item["font_audit"] = _as_dict(item.get("font_audit_json"))
            item["missing_fonts_list"] = _as_list(item.get("missing_fonts"))
            result.append(item)
        return result

    def update_batch(self, batch_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
        """Update batch attributes."""
        if not fields:
            return self.get_batch(batch_id)

        fields["updated_at"] = _utc_now_iso()
        set_clauses = [f"{col} = ?" for col in fields.keys()]
        values = list(fields.values())
        values.append(batch_id)

        sql = f"UPDATE batches SET {', '.join(set_clauses)} WHERE id = ?"
        with self._get_connection() as conn:
            conn.execute(sql, values)
            conn.commit()

        return self.get_batch(batch_id)

    def delete_batch(self, batch_id: str, expected_episode_count: Optional[int] = None) -> bool:
        """Delete one batch atomically, validating the expected cascade size."""
        caller = _mutation_caller()
        with self._get_connection() as conn:
            total_before = int(conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0])
            batch_before = int(conn.execute("SELECT COUNT(*) FROM episodes WHERE batch_id = ?", (batch_id,)).fetchone()[0])
            if expected_episode_count is not None and batch_before != expected_episode_count:
                raise RuntimeError(
                    f"Queue changed before batch deletion (expected {expected_episode_count} episodes, found {batch_before})."
                )
            res = conn.execute("DELETE FROM batches WHERE id = ?", (batch_id,))
            if res.rowcount != 1:
                if res.rowcount == 0:
                    return False
                raise RuntimeError(f"Batch deletion affected an unexpected number of rows: {res.rowcount}.")
            batch_after = int(conn.execute("SELECT COUNT(*) FROM episodes WHERE batch_id = ?", (batch_id,)).fetchone()[0])
            total_after = int(conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0])
            if batch_after != 0 or total_before - total_after != batch_before:
                raise RuntimeError("Batch deletion did not remove the expected episode rows; transaction rolled back.")
            if total_before >= 29 and total_after == 1:
                raise RuntimeError("Refusing a destructive operation that would unexpectedly reduce a 29+ episode queue to one row.")
            conn.commit()
        _log_mutation("DELETE_BATCH", caller, batch_id, "", total_before, total_after)
        return True

    def add_episodes(
        self,
        batch_id: str,
        episodes: List[Dict[str, Any]],
        expected_insert_count: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Atomically add records, optionally requiring an exact insertion count."""
        now = _utc_now_iso()
        added_ids = []
        caller = _mutation_caller()

        with self._get_connection() as conn:
            batch_count_before = int(conn.execute("SELECT COUNT(*) FROM episodes WHERE batch_id = ?", (batch_id,)).fetchone()[0])
            total_before = int(conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0])
            for ep in episodes:
                eid = ep.get("id") or str(uuid.uuid4())
                order_index = ep.get("order_index", 1)
                source_path = str(Path(ep["source_path"]).resolve())
                filename = ep.get("filename") or Path(source_path).name
                display_label = ep.get("display_label") or f"{order_index:02d}"
                state = ep.get("state", EpisodeState.PENDING.value)
                settings_payload = _as_dict(ep.get("settings_json") or {})
                if ep.get("subtitle_metadata"):
                    settings_payload["subtitle_metadata"] = _as_dict(ep.get("subtitle_metadata"))
                settings_json = json.dumps(settings_payload, ensure_ascii=False)
                work_dir = ep.get("work_dir")
                video_id = ep.get("video_id") or ep.get("server_video_id") or ""
                session_id = ep.get("session_id") or ep.get("upload_session_id") or ""
                upload_session_id = ep.get("upload_session_id") or session_id
                server_video_id = ep.get("server_video_id") or video_id
                episode_number = ep.get("episode_number")
                if episode_number is None:
                    episode_number = order_index
                source_bytes = ep.get("source_bytes", 0)
                duration_seconds = float(ep.get("duration_seconds", 0) or 0)
                source_width = int(ep.get("source_width", 0) or 0)
                source_height = int(ep.get("source_height", 0) or 0)
                output_estimate_bytes = int(ep.get("output_estimate_bytes", 0) or 0)
                font_audit = ep.get("font_audit", {}) or {}
                if not isinstance(font_audit, str):
                    font_audit = json.dumps(font_audit, ensure_ascii=False)
                package_bytes = ep.get("package_bytes", 0)
                uploaded_bytes = ep.get("uploaded_bytes", 0)
                error = ep.get("error")

                current_stage = ep.get("current_stage", "")
                current_quality = ep.get("current_quality", "")
                progress = ep.get("progress", 0.0)
                fps = ep.get("fps", 0.0)
                speed = ep.get("speed", "0x")
                eta = ep.get("eta", "00:00:00")
                rendition_progress = ep.get("rendition_progress", 0.0)
                elapsed_seconds = ep.get("elapsed_seconds", 0.0)
                encoder_used = ep.get("encoder_used", "")

                subtitle_path = ep.get("subtitle_path") or ""
                subtitle_source = ep.get("subtitle_source", "none")
                subtitle_format = ep.get("subtitle_format", "")
                subtitle_stream_index = ep.get("subtitle_stream_index")
                subtitle_label = ep.get("subtitle_label", "")
                font_status = ep.get("font_status", "—")

                missing_fonts = ep.get("missing_fonts", "[]")
                if isinstance(missing_fonts, (list, dict)):
                    missing_fonts = json.dumps(missing_fonts, ensure_ascii=False)

                subtitle_candidates = ep.get("subtitle_candidates", "[]")
                if isinstance(subtitle_candidates, (list, dict)):
                    subtitle_candidates = json.dumps(subtitle_candidates, ensure_ascii=False)

                local_package_cleaned = 1 if ep.get("local_package_cleaned") else 0
                local_package_cleaned_at = ep.get("local_package_cleaned_at", "")
                freed_bytes = ep.get("freed_bytes", 0)

                try:
                    conn.execute(
                        """
                        INSERT INTO episodes (
                            id, batch_id, order_index, source_path, filename, display_label,
                            state, current_stage, current_quality, progress, rendition_progress, fps, speed, eta,
                            elapsed_seconds, encoder_used,
                            subtitle_path, subtitle_source, subtitle_format, subtitle_stream_index,
                            subtitle_label, font_status, missing_fonts, subtitle_candidates,
                            settings_json, work_dir, video_id, session_id,
                            upload_session_id, server_video_id, episode_number,
                            source_bytes, package_bytes, uploaded_bytes,
                            duration_seconds, source_width, source_height, output_estimate_bytes, font_audit_json,
                            local_package_cleaned, local_package_cleaned_at, freed_bytes,
                            error, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            eid, batch_id, order_index, source_path, filename, display_label,
                            state, current_stage, current_quality, progress, rendition_progress, fps, speed, eta,
                            elapsed_seconds, encoder_used,
                            subtitle_path, subtitle_source, subtitle_format, subtitle_stream_index,
                            subtitle_label, font_status, missing_fonts, subtitle_candidates,
                            settings_json, work_dir, video_id, session_id,
                            upload_session_id, server_video_id, episode_number,
                            source_bytes, package_bytes, uploaded_bytes,
                            duration_seconds, source_width, source_height, output_estimate_bytes, font_audit,
                            local_package_cleaned, local_package_cleaned_at, freed_bytes,
                            error, now
                        ),
                    )
                    added_ids.append(eid)
                except sqlite3.IntegrityError:
                    # A repeat import may contain an already queued source, but
                    # every other constraint failure must abort the insertion.
                    duplicate = conn.execute(
                        "SELECT 1 FROM episodes WHERE batch_id = ? AND source_path = ?",
                        (batch_id, source_path),
                    ).fetchone()
                    if duplicate:
                        continue
                    raise

            if expected_insert_count is not None and len(added_ids) != expected_insert_count:
                raise RuntimeError(
                    f"Episode import inserted {len(added_ids)} rows; expected {expected_insert_count}. Transaction rolled back."
                )
            batch_count_after = int(conn.execute("SELECT COUNT(*) FROM episodes WHERE batch_id = ?", (batch_id,)).fetchone()[0])
            total_after = int(conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0])
            if batch_count_after - batch_count_before != len(added_ids) or total_after - total_before != len(added_ids):
                raise RuntimeError("Episode import row-count validation failed. Transaction rolled back.")
            conn.commit()

        # Update batch updated_at
        self.update_batch(batch_id)
        _log_mutation("INSERT_EPISODES", caller, batch_id, ",".join(added_ids), total_before, total_after)
        logger.info("DATABASE_COMMIT batch=%s inserted=%d", batch_id, len(added_ids))
        return self.get_episodes(batch_id)

    def restore_episode_snapshot(self, snapshot_db: Union[str, Path], expected_total: int = 29) -> Dict[str, int]:
        """Restore a verified queue snapshot without replacing the live database file."""
        snapshot_path = Path(snapshot_db).resolve()
        if not snapshot_path.is_file() or snapshot_path.resolve() == self.db_path.resolve():
            raise ValueError("A separate, existing queue snapshot is required for recovery.")
        source_uri = f"file:{snapshot_path.as_posix()}?mode=ro"
        source = sqlite3.connect(source_uri, uri=True)
        source.row_factory = sqlite3.Row
        try:
            source_batches = [dict(row) for row in source.execute("SELECT * FROM batches ORDER BY created_at, id")]
            source_episodes = [dict(row) for row in source.execute("SELECT * FROM episodes ORDER BY batch_id, order_index")]
        finally:
            source.close()
        if len(source_episodes) != expected_total:
            raise RuntimeError(f"Snapshot has {len(source_episodes)} episodes; expected {expected_total}.")
        if not source_batches:
            raise RuntimeError("Snapshot does not contain batches.")

        caller = _mutation_caller()
        with self._get_connection() as conn:
            total_before = int(conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0])
            for batch in source_batches:
                live = conn.execute("SELECT id FROM batches WHERE id = ?", (batch["id"],)).fetchone()
                if live:
                    conn.execute(
                        "UPDATE batches SET season_folder_path=?, destination_label=?, mode=?, status='PAUSED', "
                        "updated_at=?, stop_after_current=0, pause_requested=0, target_folder_id=?, target_series_id=?, target_season_id=? WHERE id=?",
                        (batch["season_folder_path"], batch.get("destination_label", ""), batch.get("mode", "SEQUENTIAL"),
                         _utc_now_iso(), batch.get("target_folder_id", ""), batch.get("target_series_id", ""),
                         batch.get("target_season_id", ""), batch["id"]),
                    )
                else:
                    columns = [key for key in batch if key in {row["name"] for row in conn.execute("PRAGMA table_info(batches)")}]
                    values = [batch[key] for key in columns]
                    if "status" in columns:
                        values[columns.index("status")] = "PAUSED"
                    conn.execute(f"INSERT INTO batches ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", values)

            for episode in source_episodes:
                batch_id = episode["batch_id"]
                if not conn.execute("SELECT 1 FROM batches WHERE id=?", (batch_id,)).fetchone():
                    raise RuntimeError(f"Snapshot episode references missing batch {batch_id}.")
                live = conn.execute("SELECT id FROM episodes WHERE id=?", (episode["id"],)).fetchone()
                source_norm = str(Path(episode["source_path"]).resolve()).casefold()
                duplicate = conn.execute("SELECT id FROM episodes WHERE lower(source_path)=? AND id<>?", (source_norm, episode["id"])).fetchone()
                if duplicate:
                    existing_duplicate = conn.execute("SELECT * FROM episodes WHERE id=?", (duplicate["id"],)).fetchone()
                    reference = next((row for row in source_episodes if str(Path(row["source_path"]).resolve()).casefold() == source_norm), None)
                    if not reference or str(existing_duplicate["batch_id"]) != batch_id:
                        raise RuntimeError("Snapshot source path conflicts with another live queue episode.")
                    conn.execute("DELETE FROM episodes WHERE id=?", (duplicate["id"],))
                order_conflict = conn.execute(
                    "SELECT id FROM episodes WHERE batch_id=? AND order_index=? AND id<>?",
                    (batch_id, episode["order_index"], episode["id"]),
                ).fetchone()
                if order_conflict:
                    # Move reinserted orphans out of the unique index while preserving the snapshot's order.
                    conn.execute("UPDATE episodes SET order_index=? WHERE id=?", (-1_000_000 - int(episode["order_index"]), order_conflict["id"]))
                settings = _as_dict(episode.get("settings_json"))
                settings.pop("processing_history", None)
                episode["settings_json"] = json.dumps(settings, ensure_ascii=False)
                # Interruption telemetry is volatile. Queue intent and source/subtitle/settings metadata are preserved.
                episode.update({
                    "state": EpisodeState.NEEDS_REVIEW.value if episode.get("state") == EpisodeState.NEEDS_REVIEW.value else EpisodeState.PENDING.value,
                    "current_stage": "RECOVERED_FROM_VERIFIED_SNAPSHOT", "current_quality": "", "progress": 0.0,
                    "rendition_progress": 0.0, "fps": 0.0, "speed": "0x", "eta": "00:00:00",
                    "elapsed_seconds": 0.0, "encoder_used": "", "work_dir": None, "video_id": "", "session_id": "",
                    "upload_session_id": "", "server_video_id": "", "upload_started_at": "", "server_status": "",
                    "last_upload_error": "", "generation_id": "", "completed_at": "", "upload_speed": "0 KB/s",
                    "upload_eta": "00:00:00", "package_bytes": 0, "uploaded_bytes": 0, "error": None,
                    "updated_at": _utc_now_iso(),
                })
                columns_live = {row["name"] for row in conn.execute("PRAGMA table_info(episodes)")}
                fields = [key for key in episode if key in columns_live]
                values = [episode[key] for key in fields]
                if live:
                    updates = [key for key in fields if key != "id"]
                    conn.execute(f"UPDATE episodes SET {','.join(f'{key}=?' for key in updates)} WHERE id=?", [episode[key] for key in updates] + [episode["id"]])
                else:
                    conn.execute(f"INSERT INTO episodes ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})", values)

            total_after = int(conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0])
            counts = {row["season_folder_path"]: int(row["count"]) for row in conn.execute(
                "SELECT b.season_folder_path, COUNT(e.id) AS count FROM batches b LEFT JOIN episodes e ON e.batch_id=b.id GROUP BY b.id"
            )}
            if total_after != expected_total or sum(counts.values()) != expected_total:
                raise RuntimeError(f"Queue recovery validation failed: expected {expected_total}, got {total_after}.")
            if any(batch["status"] != "PAUSED" for batch in conn.execute("SELECT status FROM batches")):
                raise RuntimeError("Queue recovery refused to leave a batch active.")
            conn.commit()
        _log_mutation("RESTORE_VERIFIED_SNAPSHOT", caller, "", "", total_before, total_after)
        return counts

    def get_episodes(self, batch_id: str) -> List[Dict[str, Any]]:
        """Get all episodes in a batch, strictly ordered by order_index ASC."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM episodes WHERE batch_id = ? ORDER BY order_index ASC",
                (batch_id,),
            )
            rows = cursor.fetchall()
            results = []
            for row in rows:
                d = dict(row)
                d["state_arabic"] = EPISODE_STATE_ARABIC.get(d["state"], d["state"])
                d["source_bytes_formatted"] = format_bytes(d.get("source_bytes", 0))
                d["package_bytes_formatted"] = format_bytes(d.get("package_bytes", 0))
                d["uploaded_bytes_formatted"] = format_bytes(d.get("uploaded_bytes", 0))
                if not d.get("upload_session_id") and d.get("session_id"):
                    d["upload_session_id"] = d["session_id"]
                if not d.get("server_video_id") and d.get("video_id"):
                    d["server_video_id"] = d["video_id"]
                if d.get("episode_number") is None:
                    d["episode_number"] = d.get("order_index", 1)
                d["local_package_cleaned"] = bool(d.get("local_package_cleaned", 0))
                d["local_package_cleaned_at"] = d.get("local_package_cleaned_at", "")
                d["freed_bytes"] = d.get("freed_bytes", 0)
                d["freed_bytes_formatted"] = format_bytes(d.get("freed_bytes", 0))
                d["missing_fonts_list"] = _as_list(d.get("missing_fonts"))
                d["font_audit"] = _as_dict(d.get("font_audit_json"))
                _normalize_episode_history(d)
                d["subtitle_candidates_list"] = _as_list(d.get("subtitle_candidates"))
                results.append(d)
            return results

    def get_episode(self, episode_id: str) -> Optional[Dict[str, Any]]:
        """Get an episode by ID."""
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,))
            row = cursor.fetchone()
            if row:
                d = dict(row)
                d["state_arabic"] = EPISODE_STATE_ARABIC.get(d["state"], d["state"])
                d["source_bytes_formatted"] = format_bytes(d.get("source_bytes", 0))
                d["package_bytes_formatted"] = format_bytes(d.get("package_bytes", 0))
                d["uploaded_bytes_formatted"] = format_bytes(d.get("uploaded_bytes", 0))
                if not d.get("upload_session_id") and d.get("session_id"):
                    d["upload_session_id"] = d["session_id"]
                if not d.get("server_video_id") and d.get("video_id"):
                    d["server_video_id"] = d["video_id"]
                if d.get("episode_number") is None:
                    d["episode_number"] = d.get("order_index", 1)
                d["local_package_cleaned"] = bool(d.get("local_package_cleaned", 0))
                d["local_package_cleaned_at"] = d.get("local_package_cleaned_at", "")
                d["freed_bytes"] = d.get("freed_bytes", 0)
                d["freed_bytes_formatted"] = format_bytes(d.get("freed_bytes", 0))
                d["missing_fonts_list"] = _as_list(d.get("missing_fonts"))
                d["font_audit"] = _as_dict(d.get("font_audit_json"))
                d["subtitle_candidates_list"] = _as_list(d.get("subtitle_candidates"))
                _normalize_episode_history(d)
                return d
        return None

    def update_episode(self, episode_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
        """Update episode attributes."""
        if not fields:
            return self.get_episode(episode_id)

        if "upload_session_id" in fields and "session_id" not in fields:
            fields["session_id"] = fields["upload_session_id"]
        elif "session_id" in fields and "upload_session_id" not in fields:
            fields["upload_session_id"] = fields["session_id"]

        if "server_video_id" in fields and "video_id" not in fields:
            fields["video_id"] = fields["server_video_id"]
        elif "video_id" in fields and "server_video_id" not in fields:
            fields["server_video_id"] = fields["video_id"]

        fields["updated_at"] = _utc_now_iso()
        if "settings_json" in fields and isinstance(fields["settings_json"], dict):
            fields["settings_json"] = json.dumps(fields["settings_json"], ensure_ascii=False)
        if "missing_fonts" in fields and isinstance(fields["missing_fonts"], (list, dict)):
            fields["missing_fonts"] = json.dumps(fields["missing_fonts"], ensure_ascii=False)
        if "subtitle_candidates" in fields and isinstance(fields["subtitle_candidates"], (list, dict)):
            fields["subtitle_candidates"] = json.dumps(fields["subtitle_candidates"], ensure_ascii=False)

        set_clauses = [f"{col} = ?" for col in fields.keys()]
        values = list(fields.values())
        values.append(episode_id)

        sql = f"UPDATE episodes SET {', '.join(set_clauses)} WHERE id = ?"
        with self._get_connection() as conn:
            conn.execute(sql, values)
            conn.commit()

        return self.get_episode(episode_id)

    def delete_episode(self, episode_id: str, expected_remaining_count: Optional[int] = None) -> bool:
        """Delete exactly one episode and preserve all surviving episode labels."""
        caller = _mutation_caller()
        with self._get_connection() as conn:
            ep = conn.execute("SELECT batch_id FROM episodes WHERE id = ?", (episode_id,)).fetchone()
            if not ep:
                return False
            batch_id = str(ep["batch_id"])
            total_before = int(conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0])
            res = conn.execute("DELETE FROM episodes WHERE id = ?", (episode_id,))
            total_after = int(conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0])
            if res.rowcount != 1 or total_after != total_before - 1:
                raise RuntimeError("Episode deletion affected an unexpected row count; transaction rolled back.")
            if expected_remaining_count is not None and total_after != expected_remaining_count:
                raise RuntimeError(
                    f"Episode deletion would leave {total_after} rows; expected {expected_remaining_count}. Transaction rolled back."
                )
            if total_before >= 29 and total_after == 1:
                raise RuntimeError("Refusing an unexpected reduction of a 29+ episode queue to one row.")
            remaining = conn.execute(
                "SELECT id FROM episodes WHERE batch_id = ? ORDER BY order_index ASC, id ASC",
                (batch_id,),
            ).fetchall()
            for offset, row in enumerate(remaining, start=1_000_000):
                conn.execute("UPDATE episodes SET order_index = ? WHERE id = ?", (offset, row["id"]))
            for order, row in enumerate(remaining, start=1):
                conn.execute("UPDATE episodes SET order_index = ? WHERE id = ?", (order, row["id"]))
            conn.commit()
        _log_mutation("DELETE_EPISODE", caller, batch_id, episode_id, total_before, total_after)
        self.update_batch(batch_id)
        return True

    def reorder_episodes(self, batch_id: str) -> None:
        """Ensure order_index is contiguous 1..N in order of existing order_index."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT id, display_label FROM episodes WHERE batch_id = ? ORDER BY order_index ASC, id ASC",
                (batch_id,),
            )
            rows = cursor.fetchall()

            # To avoid UNIQUE constraint conflict while updating, we first assign temporary offsets
            for i, row in enumerate(rows, start=1000000):
                conn.execute(
                    "UPDATE episodes SET order_index = ? WHERE id = ?",
                    (i, row["id"]),
                )
            for idx, row in enumerate(rows, start=1):
                conn.execute(
                    "UPDATE episodes SET order_index = ? WHERE id = ?",
                    (idx, row["id"]),
                )
            conn.commit()

    def clear_batch(self, batch_id: str, expected_episode_count: Optional[int] = None) -> bool:
        """Clear a batch's rows in one checked transaction; leave its batch record."""
        caller = _mutation_caller()
        with self._get_connection() as conn:
            total_before = int(conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0])
            batch_before = int(conn.execute("SELECT COUNT(*) FROM episodes WHERE batch_id = ?", (batch_id,)).fetchone()[0])
            if expected_episode_count is not None and batch_before != expected_episode_count:
                raise RuntimeError(
                    f"Queue changed before clearing batch (expected {expected_episode_count} episodes, found {batch_before})."
                )
            conn.execute("DELETE FROM episodes WHERE batch_id = ?", (batch_id,))
            total_after = int(conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0])
            if total_before - total_after != batch_before:
                raise RuntimeError("Batch clear affected an unexpected row count; transaction rolled back.")
            if total_before >= 29 and total_after == 1:
                raise RuntimeError("Refusing a batch clear that would unexpectedly reduce a 29+ episode queue to one row.")
            conn.commit()
        _log_mutation("CLEAR_BATCH_EPISODES", caller, batch_id, "", total_before, total_after)
        self.update_batch(batch_id)
        return True

    def delete_episodes(self, episode_ids: List[str], expected_deleted_count: int) -> int:
        """Delete a validated bulk set atomically and retain surviving numbering."""
        requested = [str(eid) for eid in episode_ids if eid]
        if len(set(requested)) != len(requested) or len(requested) != expected_deleted_count:
            raise RuntimeError("Bulk queue deletion request does not match its expected row count.")
        if not requested:
            return 0
        caller = _mutation_caller()
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            total_before = int(conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0])
            affected = []
            for start in range(0, len(requested), 500):
                chunk = requested[start:start + 500]
                placeholders = ",".join("?" for _ in chunk)
                affected.extend(conn.execute(
                    f"SELECT id, batch_id, state FROM episodes WHERE id IN ({placeholders})", chunk,
                ).fetchall())
            if len(affected) != expected_deleted_count:
                raise RuntimeError(
                    f"Bulk queue deletion expected {expected_deleted_count} rows but found {len(affected)}; transaction rolled back."
                )
            protected_states = {
                EpisodeState.PROBING.value, EpisodeState.READY_TO_ENCODE.value,
                EpisodeState.ENCODING.value, EpisodeState.PACKAGING.value,
                EpisodeState.VERIFYING_LOCAL.value, EpisodeState.UPLOADING.value,
                EpisodeState.SERVER_VERIFYING.value,
            }
            active_ids = [str(row["id"]) for row in affected if row["state"] in protected_states]
            if active_ids:
                raise RuntimeError("أزل تحديد الحلقات الجاري فحصها أو معالجتها أو رفعها أولاً.")
            batch_ids = sorted({str(row["batch_id"]) for row in affected})
            for start in range(0, len(requested), 500):
                chunk = requested[start:start + 500]
                placeholders = ",".join("?" for _ in chunk)
                conn.execute(f"DELETE FROM episodes WHERE id IN ({placeholders})", chunk)
            total_after = int(conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0])
            if total_before - total_after != expected_deleted_count:
                raise RuntimeError("Bulk queue deletion affected an unexpected row count; transaction rolled back.")
            if total_before >= 29 and total_after == 1:
                raise RuntimeError("Refusing a bulk operation that would unexpectedly reduce a 29+ episode queue to one row.")
            for batch_id in batch_ids:
                rows = conn.execute(
                    "SELECT id FROM episodes WHERE batch_id = ? ORDER BY order_index ASC, id ASC", (batch_id,),
                ).fetchall()
                for offset, row in enumerate(rows, start=1_000_000):
                    conn.execute("UPDATE episodes SET order_index = ? WHERE id = ?", (offset, row["id"]))
                for order, row in enumerate(rows, start=1):
                    conn.execute("UPDATE episodes SET order_index = ? WHERE id = ?", (order, row["id"]))
            conn.commit()
        _log_mutation("BULK_DELETE_EPISODES", caller, ",".join(batch_ids), ",".join(requested), total_before, total_after)
        for batch_id in batch_ids:
            self.update_batch(batch_id)
        return expected_deleted_count

    def get_batch_summary(self, batch_id: str) -> Dict[str, Any]:
        """Compute aggregated statistics for a batch."""
        batch = self.get_batch(batch_id)
        if not batch:
            return {}

        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT 
                    COUNT(*) as total_episodes,
                    COALESCE(SUM(source_bytes), 0) as total_source_bytes,
                    COALESCE(SUM(package_bytes), 0) as total_package_bytes,
                    COALESCE(SUM(uploaded_bytes), 0) as total_uploaded_bytes,
                    COALESCE(SUM(freed_bytes), 0) as total_freed_bytes
                FROM episodes
                WHERE batch_id = ?
                """,
                (batch_id,),
            )
            stats = dict(cursor.fetchone())

            cursor2 = conn.execute(
                """
                SELECT state, COUNT(*) as count 
                FROM episodes 
                WHERE batch_id = ? 
                GROUP BY state
                """,
                (batch_id,),
            )
            state_counts = {row["state"]: row["count"] for row in cursor2.fetchall()}

        return {
            "batch_id": batch_id,
            "season_folder_path": batch.get("season_folder_path", ""),
            "destination_label": batch.get("destination_label", ""),
            "mode": batch.get("mode", "SEQUENTIAL"),
            "status": batch.get("status", "ACTIVE"),
            "total_episodes": stats["total_episodes"],
            "total_source_bytes": stats["total_source_bytes"],
            "total_source_bytes_formatted": format_bytes(stats["total_source_bytes"]),
            "total_package_bytes": stats["total_package_bytes"],
            "total_uploaded_bytes": stats["total_uploaded_bytes"],
            "total_freed_bytes": stats.get("total_freed_bytes", 0),
            "total_freed_bytes_formatted": format_bytes(stats.get("total_freed_bytes", 0)),
            "state_counts": state_counts,
            "created_at": batch.get("created_at"),
            "updated_at": batch.get("updated_at"),
        }
