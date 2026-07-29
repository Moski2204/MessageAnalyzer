"""Background analysis jobs and a separate, disposable SQLite result cache."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from analysis import analyze_messages, parse_stop_words
from database import connect_readonly, date_to_unix


ANALYSIS_VERSION = "3"
DEFAULT_CHUNK_SIZE = 5_000
ACTIVE_STATUSES = {"queued", "running"}
TERMINAL_STATUSES = {"complete", "failed", "interrupted"}
JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
SAFE_FAILURE_MESSAGE = (
    "Analysis could not be completed. The message database was not changed. "
    "Please try again."
)
INTERRUPTED_MESSAGE = (
    "This analysis was interrupted when the application stopped. "
    "Start it again when you are ready."
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def valid_job_id(value: str) -> bool:
    return bool(JOB_ID_RE.fullmatch(value))


def source_database_quick_identity(database_path: Path) -> dict[str, int]:
    stat = database_path.stat()
    return {
        "file_size": stat.st_size,
        "modified_ns": stat.st_mtime_ns,
    }


@dataclass(frozen=True)
class AnalysisSettings:
    start_date: str
    end_date: str
    full_conversation: bool
    top_n: int
    stop_words: str

    @property
    def start_unix(self) -> int | None:
        return None if self.full_conversation else date_to_unix(self.start_date)

    @property
    def end_unix(self) -> int | None:
        return (
            None
            if self.full_conversation
            else date_to_unix(self.end_date, end=True)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_date": self.start_date,
            "end_date": self.end_date,
            "full_conversation": self.full_conversation,
            "top_n": self.top_n,
            "stop_words": self.stop_words,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AnalysisSettings":
        top_n = int(value.get("top_n", 20))
        if top_n not in {20, 50, 100}:
            top_n = 20
        return cls(
            start_date=str(value.get("start_date", "")),
            end_date=str(value.get("end_date", "")),
            full_conversation=bool(value.get("full_conversation", False)),
            top_n=top_n,
            stop_words=str(value.get("stop_words", ""))[:10_000],
        )


def source_database_fingerprint(database_path: Path) -> dict[str, Any]:
    """Fingerprint source metadata without hashing the complete message database."""
    quick_identity = source_database_quick_identity(database_path)
    with connect_readonly(database_path) as connection:
        message_count = connection.execute(
            "SELECT COUNT(*) AS count FROM messages"
        ).fetchone()["count"]
        highest_id = connection.execute(
            "SELECT MAX(id) AS highest_id FROM messages"
        ).fetchone()["highest_id"]
        oldest = connection.execute(
            """
            SELECT timestamp_unix
            FROM messages
            WHERE timestamp_unix IS NOT NULL
            ORDER BY timestamp_unix ASC
            LIMIT 1
            """
        ).fetchone()
        newest = connection.execute(
            """
            SELECT timestamp_unix
            FROM messages
            WHERE timestamp_unix IS NOT NULL
            ORDER BY timestamp_unix DESC
            LIMIT 1
            """
        ).fetchone()
    values = {
        **quick_identity,
        "message_count": int(message_count),
        "highest_message_id": int(highest_id or 0),
        "oldest_timestamp": oldest["timestamp_unix"] if oldest else None,
        "newest_timestamp": newest["timestamp_unix"] if newest else None,
    }
    encoded = json.dumps(
        values, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {**values, "digest": hashlib.sha256(encoded).hexdigest()}


def build_cache_key(
    settings: AnalysisSettings,
    source_fingerprint: dict[str, Any],
    analysis_version: str = ANALYSIS_VERSION,
) -> str:
    payload = {
        "analysis_version": analysis_version,
        "start_date": settings.start_date,
        "end_date": settings.end_date,
        "full_conversation": settings.full_conversation,
        "top_n": settings.top_n,
        "stop_words": sorted(parse_stop_words(settings.stop_words)),
        "source_fingerprint": source_fingerprint["digest"],
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def count_selected_messages(
    database_path: Path, settings: AnalysisSettings
) -> int:
    where: list[str] = []
    params: list[Any] = []
    if settings.start_unix is not None:
        where.append("timestamp_unix >= ?")
        params.append(settings.start_unix)
    if settings.end_unix is not None:
        where.append("timestamp_unix < ?")
        params.append(settings.end_unix)
    sql = "SELECT COUNT(*) AS count FROM messages"
    if where:
        sql += " WHERE " + " AND ".join(where)
    with connect_readonly(database_path) as connection:
        return int(connection.execute(sql, params).fetchone()["count"])


class ClosingCacheConnection(sqlite3.Connection):
    """Commit or roll back, then always release the cache file handle."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class AnalysisCache:
    """Small SQLite repository that never writes to the source message database."""

    def __init__(self, path: Path):
        self.path = path
        self._initialization_lock = threading.RLock()
        self.recovered_corrupt_cache = False
        self._initialize()

    def _raw_connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=30,
            factory=ClosingCacheConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @staticmethod
    def _recoverable_error(error: sqlite3.DatabaseError) -> bool:
        message = str(error).casefold()
        return any(
            marker in message
            for marker in (
                "file is not a database",
                "database disk image is malformed",
                "file is encrypted",
                "no such table: analysis_jobs",
            )
        )

    def _quarantine_and_recreate(self) -> None:
        self.recovered_corrupt_cache = True
        if self.path.exists():
            quarantine = self.path.with_name(
                f"{self.path.stem}.corrupt-{int(time.time())}-"
                f"{uuid.uuid4().hex[:8]}{self.path.suffix}"
            )
            self.path.replace(quarantine)
        self._initialize_schema()

    def _connect(self) -> sqlite3.Connection:
        with self._initialization_lock:
            if not self.path.is_file():
                self._initialize_schema()
            connection = self._raw_connect()
            try:
                connection.execute(
                    "SELECT 1 FROM analysis_jobs LIMIT 1"
                ).fetchone()
            except sqlite3.DatabaseError as error:
                connection.close()
                if not self._recoverable_error(error):
                    raise
                self._quarantine_and_recreate()
                connection = self._raw_connect()
            return connection

    def _initialize_schema(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._raw_connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS analysis_jobs (
                    job_id TEXT PRIMARY KEY,
                    cache_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    progress REAL,
                    processed_messages INTEGER NOT NULL DEFAULT 0,
                    total_messages INTEGER NOT NULL DEFAULT 0,
                    settings_json TEXT NOT NULL,
                    fingerprint_json TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    elapsed_seconds REAL NOT NULL DEFAULT 0,
                    analysis_version TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_analysis_jobs_cache
                ON analysis_jobs(cache_key, status, completed_at DESC);

                CREATE UNIQUE INDEX IF NOT EXISTS idx_analysis_one_active
                ON analysis_jobs((1))
                WHERE status IN ('queued', 'running');
                """
            )

    def _initialize(self) -> None:
        with self._initialization_lock:
            try:
                self._initialize_schema()
            except sqlite3.DatabaseError as error:
                if not self._recoverable_error(error):
                    raise
                self._quarantine_and_recreate()

    @staticmethod
    def _decode_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        decoded = dict(row)
        try:
            decoded["settings"] = json.loads(decoded.pop("settings_json"))
            decoded["fingerprint"] = json.loads(decoded.pop("fingerprint_json"))
            result_json = decoded.pop("result_json")
            decoded["result"] = json.loads(result_json) if result_json else None
        except (TypeError, json.JSONDecodeError):
            return None
        return decoded

    def mark_interrupted_jobs(self) -> int:
        now = _utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE analysis_jobs
                SET status = 'interrupted',
                    stage = 'Interrupted',
                    error = ?,
                    completed_at = ?
                WHERE status IN ('queued', 'running')
                """,
                (INTERRUPTED_MESSAGE, now),
            )
            return cursor.rowcount

    def create_job(
        self,
        job_id: str,
        cache_key: str,
        settings: AnalysisSettings,
        fingerprint: dict[str, Any],
        total_messages: int,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO analysis_jobs (
                    job_id, cache_key, status, stage, progress,
                    processed_messages, total_messages, settings_json,
                    fingerprint_json, created_at, analysis_version
                ) VALUES (?, ?, 'queued', 'Queued', 0, 0, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    cache_key,
                    total_messages,
                    json.dumps(settings.to_dict(), ensure_ascii=False),
                    json.dumps(fingerprint, sort_keys=True),
                    _utc_now(),
                    ANALYSIS_VERSION,
                ),
            )

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        if not valid_job_id(job_id):
            return None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM analysis_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return self._decode_row(row)

    def find_completed(self, cache_key: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM analysis_jobs
                WHERE cache_key = ?
                  AND status = 'complete'
                  AND result_json IS NOT NULL
                ORDER BY completed_at DESC
                """,
                (cache_key,),
            ).fetchall()
        for row in rows:
            decoded = self._decode_row(row)
            if decoded is not None and decoded["result"] is not None:
                return decoded
        return None

    def active_job(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM analysis_jobs
                WHERE status IN ('queued', 'running')
                ORDER BY created_at DESC
                LIMIT 1
                """
            ).fetchone()
        return self._decode_row(row)

    def recent_completed(self, limit: int = 5) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM analysis_jobs
                WHERE status = 'complete' AND result_json IS NOT NULL
                ORDER BY completed_at DESC
                LIMIT ?
                """,
                (max(1, min(limit, 20)),),
            ).fetchall()
        return [
            decoded
            for row in rows
            if (decoded := self._decode_row(row)) is not None
        ]

    def set_running(self, job_id: str) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE analysis_jobs
                SET status = 'running',
                    stage = 'Loading messages',
                    progress = 0,
                    started_at = ?
                WHERE job_id = ? AND status = 'queued'
                """,
                (_utc_now(), job_id),
            )
            if cursor.rowcount != 1:
                raise sqlite3.OperationalError(
                    "The queued analysis job is no longer available."
                )

    def update_progress(
        self,
        job_id: str,
        stage: str,
        progress: float,
        processed_messages: int,
        total_messages: int,
        elapsed_seconds: float,
    ) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE analysis_jobs
                SET stage = ?,
                    progress = ?,
                    processed_messages = ?,
                    total_messages = ?,
                    elapsed_seconds = ?
                WHERE job_id = ? AND status = 'running'
                """,
                (
                    stage[:100],
                    max(0.0, min(float(progress), 99.9)),
                    max(0, int(processed_messages)),
                    max(0, int(total_messages)),
                    max(0.0, float(elapsed_seconds)),
                    job_id,
                ),
            )
            if cursor.rowcount != 1:
                raise sqlite3.OperationalError(
                    "The running analysis job is no longer available."
                )

    def complete_job(
        self, job_id: str, result: dict[str, Any], elapsed_seconds: float
    ) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE analysis_jobs
                SET status = 'complete',
                    stage = 'Complete',
                    progress = 100,
                    processed_messages = total_messages,
                    result_json = ?,
                    error = NULL,
                    completed_at = ?,
                    elapsed_seconds = ?
                WHERE job_id = ?
                """,
                (
                    json.dumps(
                        result,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    _utc_now(),
                    max(0.0, float(elapsed_seconds)),
                    job_id,
                ),
            )
            if cursor.rowcount != 1:
                raise sqlite3.OperationalError(
                    "The completed analysis job could not be saved."
                )

    def fail_job(self, job_id: str, elapsed_seconds: float) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE analysis_jobs
                SET status = 'failed',
                    stage = 'Failed',
                    error = ?,
                    completed_at = ?,
                    elapsed_seconds = ?
                WHERE job_id = ?
                """,
                (
                    SAFE_FAILURE_MESSAGE,
                    _utc_now(),
                    max(0.0, float(elapsed_seconds)),
                    job_id,
                ),
            )

    def clear(self) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM analysis_jobs")
            connection.commit()
            connection.execute("VACUUM")

    def size_bytes(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0


class AnalysisJobManager:
    """Run no more than one local analysis thread at a time."""

    def __init__(
        self,
        database_path: Path,
        cache_path: Path,
        analyzer: Callable[..., dict[str, Any]] = analyze_messages,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
    ):
        self.database_path = database_path
        self.cache = AnalysisCache(cache_path)
        self.analyzer = analyzer
        self.chunk_size = chunk_size
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._active_job_id: str | None = None
        self.cache.mark_interrupted_jobs()

    def submit(
        self, settings: AnalysisSettings, force: bool = False
    ) -> dict[str, Any]:
        with self._lock:
            active = self.active_job()
            if active is not None:
                return {"outcome": "busy", "job": active}

            fingerprint = source_database_fingerprint(self.database_path)
            cache_key = build_cache_key(settings, fingerprint)
            if not force:
                cached = self.cache.find_completed(cache_key)
                if cached is not None:
                    return {"outcome": "cached", "job": cached}

            total_messages = (
                int(fingerprint["message_count"])
                if settings.full_conversation
                else count_selected_messages(self.database_path, settings)
            )
            if total_messages == 0:
                return {"outcome": "empty", "job": None}

            job_id = uuid.uuid4().hex
            self.cache.create_job(
                job_id,
                cache_key,
                settings,
                fingerprint,
                total_messages,
            )
            self._active_job_id = job_id
            self._thread = threading.Thread(
                target=self._run_job,
                args=(job_id, settings, total_messages),
                name=f"analysis-{job_id[:8]}",
                daemon=True,
            )
            self._thread.start()
            return {
                "outcome": "started",
                "job": self.cache.get_job(job_id),
            }

    def _run_job(
        self,
        job_id: str,
        settings: AnalysisSettings,
        total_messages: int,
    ) -> None:
        started = time.monotonic()

        def progress(
            stage: str,
            percentage: float,
            processed: int,
            total: int,
        ) -> None:
            self.cache.update_progress(
                job_id,
                stage,
                percentage,
                processed,
                total,
                time.monotonic() - started,
            )

        try:
            self.cache.set_running(job_id)
            with connect_readonly(self.database_path) as connection:
                result = self.analyzer(
                    connection,
                    settings.start_unix,
                    settings.end_unix,
                    parse_stop_words(settings.stop_words),
                    settings.top_n,
                    progress_callback=progress,
                    chunk_size=self.chunk_size,
                    total_messages=total_messages,
                )
            progress(
                "Saving results",
                99,
                total_messages,
                total_messages,
            )
            self.cache.complete_job(
                job_id, result, time.monotonic() - started
            )
        except Exception:
            logging.getLogger(__name__).error(
                "Analysis job %s failed without changing messages.db.",
                job_id,
            )
            try:
                self.cache.fail_job(job_id, time.monotonic() - started)
            except (OSError, sqlite3.Error):
                logging.getLogger(__name__).error(
                    "Analysis job %s could not record its failure state.",
                    job_id,
                )
        finally:
            with self._lock:
                if self._active_job_id == job_id:
                    self._active_job_id = None
                    self._thread = None

    def active_job(self) -> dict[str, Any] | None:
        if self._thread is not None and self._thread.is_alive():
            if self._active_job_id:
                return self.cache.get_job(self._active_job_id)
        return self.cache.active_job()

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        return self.cache.get_job(job_id)

    def job_is_current(self, job: dict[str, Any]) -> bool:
        if job.get("analysis_version") != ANALYSIS_VERSION:
            return False
        try:
            current = source_database_quick_identity(self.database_path)
        except OSError:
            return False
        stored = job.get("fingerprint") or {}
        return all(stored.get(key) == value for key, value in current.items())

    def recent_completed(self, limit: int = 5) -> list[dict[str, Any]]:
        candidates = self.cache.recent_completed(max(limit * 4, limit))
        return [
            job for job in candidates if self.job_is_current(job)
        ][:limit]

    def clear_cache(self) -> bool:
        with self._lock:
            if self.active_job() is not None:
                return False
            self.cache.clear()
            return True

    def wait_for_job(
        self, job_id: str, timeout: float = 10
    ) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.get_job(job_id)
            if job is None or job["status"] in TERMINAL_STATUSES:
                return job
            time.sleep(0.02)
        return self.get_job(job_id)
