"""SQLite storage, FTS5 search, import, and pagination."""

from __future__ import annotations

import calendar
import json
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator

from analysis import LocalSentiment
from parser import (
    ParsedMessage,
    PhotoIndex,
    discover_message_files,
    normalize_text,
    parse_message_file,
)


WORD_RE = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", re.UNICODE)
VALID_SEARCH_MODES = {"contains", "phrase", "all", "any"}
ALLOWED_SENDERS = frozenset({"Mahrus", "🐧"})


class ClosingConnection(sqlite3.Connection):
    """A sqlite connection whose context manager also releases the file."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect(database_path: Path) -> sqlite3.Connection:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        database_path,
        timeout=30,
        factory=ClosingConnection,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def initialize_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY,
            sender TEXT NOT NULL,
            timestamp TEXT,
            timestamp_unix INTEGER,
            original_timestamp TEXT NOT NULL,
            message_text TEXT NOT NULL,
            normalized_text TEXT NOT NULL,
            message_type TEXT NOT NULL,
            source_filename TEXT NOT NULL,
            source_file_number INTEGER NOT NULL,
            source_position INTEGER NOT NULL,
            attachment_path TEXT,
            external_url TEXT,
            deduplication_key TEXT NOT NULL UNIQUE,
            conversation_position INTEGER,
            sentiment_label TEXT,
            sentiment_score REAL
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
            message_text,
            content='messages',
            content_rowid='id',
            tokenize='unicode61 remove_diacritics 0'
        );

        CREATE TABLE IF NOT EXISTS app_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS import_errors (
            source_filename TEXT PRIMARY KEY,
            error_type TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(sender);
        CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp_unix);
        CREATE INDEX IF NOT EXISTS idx_messages_timestamp_text ON messages(timestamp);
        CREATE INDEX IF NOT EXISTS idx_messages_type ON messages(message_type);
        CREATE INDEX IF NOT EXISTS idx_messages_source ON messages(source_filename);
        CREATE INDEX IF NOT EXISTS idx_messages_position ON messages(conversation_position);
        """
    )


def database_ready(database_path: Path) -> bool:
    if not database_path.is_file():
        return False
    try:
        with connect(database_path) as connection:
            row = connection.execute(
                "SELECT value FROM app_metadata WHERE key = 'import_summary'"
            ).fetchone()
            return row is not None
    except sqlite3.Error:
        return False


def _message_values(
    message: ParsedMessage, sentiment: LocalSentiment
) -> tuple[Any, ...]:
    label, score = sentiment.score(message.message_text, message.message_type)
    return (
        message.sender,
        message.timestamp,
        message.timestamp_unix,
        message.original_timestamp,
        message.message_text,
        message.normalized_text,
        message.message_type,
        message.source_filename,
        message.source_file_number,
        message.source_position,
        message.attachment_path,
        message.external_url,
        message.deduplication_key,
        label,
        score,
    )


INSERT_SQL = """
    INSERT OR IGNORE INTO messages (
        sender, timestamp, timestamp_unix, original_timestamp, message_text,
        normalized_text, message_type, source_filename, source_file_number,
        source_position, attachment_path, external_url, deduplication_key,
        sentiment_label, sentiment_score
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def import_messages(
    data_dir: Path,
    database_path: Path,
    rebuild: bool = False,
    progress_callback: Callable[[int, int, str, bool], None] | None = None,
    allowed_senders: set[str] | frozenset[str] | None = ALLOWED_SENDERS,
) -> dict[str, Any]:
    """Import every discovered HTML chunk. Never writes inside data_dir."""
    if rebuild:
        # These are the only SQLite-generated files removed by a rebuild.
        for generated_path in (
            database_path,
            Path(f"{database_path}-wal"),
            Path(f"{database_path}-shm"),
        ):
            if generated_path.is_file():
                generated_path.unlink()

    files = discover_message_files(data_dir)
    photo_index = PhotoIndex.build(data_dir / "photos")
    sentiment = LocalSentiment()
    stats: dict[str, Any] = {
        "files_found": len(files),
        "files_processed": 0,
        "files_failed": 0,
        "messages_imported": 0,
        "duplicates_skipped": 0,
        "messages_skipped_other_senders": 0,
        "oldest_timestamp": None,
        "newest_timestamp": None,
        "sentiment_method": sentiment.method,
        "photo_files_indexed": photo_index.file_count,
        "ambiguous_photo_filenames": photo_index.ambiguous_filename_count,
        "photo_references_matched": 0,
        "photo_references_unavailable": 0,
    }

    with connect(database_path) as connection:
        initialize_schema(connection)
        connection.execute("DELETE FROM messages")
        connection.execute("DELETE FROM messages_fts")
        connection.execute("DELETE FROM import_errors")
        connection.execute("DELETE FROM app_metadata WHERE key = 'import_summary'")
        connection.commit()

        for file_index, path in enumerate(files, start=1):
            eligible_count = 0
            inserted_before = connection.total_changes
            batch: list[tuple[Any, ...]] = []
            try:
                for message in parse_message_file(path, data_dir, photo_index):
                    if allowed_senders is not None and message.sender not in allowed_senders:
                        stats["messages_skipped_other_senders"] += 1
                        continue
                    eligible_count += 1
                    batch.append(_message_values(message, sentiment))
                    if len(batch) >= 1000:
                        connection.executemany(INSERT_SQL, batch)
                        batch.clear()
                if batch:
                    connection.executemany(INSERT_SQL, batch)
                inserted = connection.total_changes - inserted_before
                stats["messages_imported"] += inserted
                stats["duplicates_skipped"] += eligible_count - inserted
                stats["files_processed"] += 1
                connection.commit()
                if progress_callback:
                    progress_callback(file_index, len(files), path.name, True)
            except Exception as exc:
                connection.rollback()
                stats["files_failed"] += 1
                connection.execute(
                    "INSERT OR REPLACE INTO import_errors(source_filename, error_type) VALUES (?, ?)",
                    (path.name, type(exc).__name__),
                )
                connection.commit()
                if progress_callback:
                    progress_callback(file_index, len(files), path.name, False)

        connection.execute("DROP TABLE IF EXISTS temp.ordered_positions")
        connection.execute(
            """
            CREATE TEMP TABLE ordered_positions AS
            SELECT id,
                   ROW_NUMBER() OVER (
                       ORDER BY
                           CASE WHEN timestamp_unix IS NULL THEN 1 ELSE 0 END,
                           timestamp_unix ASC,
                           source_file_number DESC,
                           source_position DESC,
                           id ASC
                   ) AS position
            FROM messages
            """
        )
        connection.execute(
            "CREATE UNIQUE INDEX temp.idx_ordered_positions_id ON ordered_positions(id)"
        )
        connection.execute(
            """
            UPDATE messages
            SET conversation_position = (
                SELECT position
                FROM ordered_positions
                WHERE ordered_positions.id = messages.id
            )
            """
        )
        connection.execute("INSERT INTO messages_fts(messages_fts) VALUES ('rebuild')")

        date_row = connection.execute(
            """
            SELECT MIN(timestamp) AS oldest_timestamp,
                   MAX(timestamp) AS newest_timestamp
            FROM messages
            WHERE timestamp_unix IS NOT NULL
            """
        ).fetchone()
        stats["oldest_timestamp"] = date_row["oldest_timestamp"]
        stats["newest_timestamp"] = date_row["newest_timestamp"]
        photo_row = connection.execute(
            """
            SELECT
                SUM(CASE WHEN attachment_path IS NOT NULL THEN 1 ELSE 0 END)
                    AS matched,
                SUM(CASE WHEN attachment_path IS NULL THEN 1 ELSE 0 END)
                    AS unavailable
            FROM messages
            WHERE message_type = 'photo'
            """
        ).fetchone()
        stats["photo_references_matched"] = int(photo_row["matched"] or 0)
        stats["photo_references_unavailable"] = int(
            photo_row["unavailable"] or 0
        )
        connection.execute(
            "INSERT INTO app_metadata(key, value) VALUES ('import_summary', ?)",
            (json.dumps(stats, ensure_ascii=False),),
        )
        connection.execute("PRAGMA optimize")
        connection.commit()
    return stats


def get_import_summary(database_path: Path) -> dict[str, Any] | None:
    if not database_path.is_file():
        return None
    try:
        with connect(database_path) as connection:
            row = connection.execute(
                "SELECT value FROM app_metadata WHERE key = 'import_summary'"
            ).fetchone()
            return json.loads(row["value"]) if row else None
    except (sqlite3.Error, json.JSONDecodeError):
        return None


def get_senders(connection: sqlite3.Connection) -> list[str]:
    return [
        row["sender"]
        for row in connection.execute(
            "SELECT DISTINCT sender FROM messages ORDER BY sender COLLATE NOCASE"
        )
    ]


def date_to_unix(value: str, end: bool = False) -> int | None:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except (ValueError, TypeError):
        return None
    if end:
        parsed += timedelta(days=1)
    return calendar.timegm(parsed.timetuple())


def clean_search_filters(values: dict[str, Any]) -> dict[str, Any]:
    query = str(values.get("q", "")).strip()[:500]
    mode = str(values.get("mode", "contains"))
    if mode not in VALID_SEARCH_MODES:
        mode = "contains"
    direction = "desc" if str(values.get("direction", "asc")) == "desc" else "asc"
    try:
        per_page = int(values.get("per_page", 25))
    except (TypeError, ValueError):
        per_page = 25
    if per_page not in {25, 50, 100}:
        per_page = 25
    try:
        page = max(1, int(values.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    return {
        "q": query,
        "mode": mode,
        "sender": str(values.get("sender", "")).strip()[:200],
        "start_date": str(values.get("start_date", "")).strip(),
        "end_date": str(values.get("end_date", "")).strip(),
        "direction": direction,
        "per_page": per_page,
        "page": page,
    }


def _fts_terms(query: str) -> list[str]:
    return [term.replace('"', '""') for term in WORD_RE.findall(query)]


def _search_parts(filters: dict[str, Any]) -> tuple[str, list[str], list[Any]]:
    joins = ""
    where: list[str] = ["1=1"]
    params: list[Any] = []
    query = filters["q"]
    mode = filters["mode"]
    terms = _fts_terms(query)

    if query:
        if mode == "contains" or (mode in {"phrase", "all", "any"} and not terms):
            where.append("instr(m.normalized_text, ?) > 0")
            params.append(normalize_text(query))
        elif mode == "phrase":
            joins = "JOIN messages_fts f ON f.rowid = m.id"
            where.append("f.message_text MATCH ?")
            params.append('"' + " ".join(terms) + '"')
        elif mode in {"all", "any"}:
            joins = "JOIN messages_fts f ON f.rowid = m.id"
            operator = " AND " if mode == "all" else " OR "
            where.append("f.message_text MATCH ?")
            params.append(operator.join(f'"{term}"' for term in terms))

    if filters["sender"]:
        where.append("m.sender = ?")
        params.append(filters["sender"])
    start_unix = date_to_unix(filters["start_date"])
    end_unix = date_to_unix(filters["end_date"], end=True)
    if start_unix is not None:
        where.append("m.timestamp_unix >= ?")
        params.append(start_unix)
    if end_unix is not None:
        where.append("m.timestamp_unix < ?")
        params.append(end_unix)
    return joins, where, params


def search_messages(
    connection: sqlite3.Connection, filters: dict[str, Any]
) -> tuple[int, list[sqlite3.Row]]:
    joins, where, params = _search_parts(filters)
    where_sql = " AND ".join(where)
    total = connection.execute(
        f"SELECT COUNT(*) AS count FROM messages m {joins} WHERE {where_sql}",
        params,
    ).fetchone()["count"]
    direction = "DESC" if filters["direction"] == "desc" else "ASC"
    offset = (filters["page"] - 1) * filters["per_page"]
    rows = connection.execute(
        f"""
        SELECT m.*
        FROM messages m
        {joins}
        WHERE {where_sql}
        ORDER BY m.conversation_position {direction}
        LIMIT ? OFFSET ?
        """,
        [*params, filters["per_page"], offset],
    ).fetchall()
    return total, rows


def iter_search_messages(
    connection: sqlite3.Connection, filters: dict[str, Any]
) -> Iterator[sqlite3.Row]:
    joins, where, params = _search_parts(filters)
    direction = "DESC" if filters["direction"] == "desc" else "ASC"
    cursor = connection.execute(
        f"""
        SELECT m.*
        FROM messages m
        {joins}
        WHERE {' AND '.join(where)}
        ORDER BY m.conversation_position {direction}
        """,
        params,
    )
    yield from cursor


def conversation_page(
    connection: sqlite3.Connection,
    direction: str,
    sender: str,
    per_page: int,
    page: int,
) -> tuple[int, list[sqlite3.Row]]:
    where = "WHERE sender = ?" if sender else ""
    params: list[Any] = [sender] if sender else []
    total = connection.execute(
        f"SELECT COUNT(*) AS count FROM messages {where}", params
    ).fetchone()["count"]
    sql_direction = "DESC" if direction == "desc" else "ASC"
    offset = (page - 1) * per_page
    rows = connection.execute(
        f"""
        SELECT * FROM messages
        {where}
        ORDER BY conversation_position {sql_direction}
        LIMIT ? OFFSET ?
        """,
        [*params, per_page, offset],
    ).fetchall()
    return total, rows


def page_for_message(
    connection: sqlite3.Connection,
    message_id: int,
    direction: str,
    sender: str,
    per_page: int,
) -> int:
    row = connection.execute(
        "SELECT conversation_position, sender FROM messages WHERE id = ?", (message_id,)
    ).fetchone()
    if not row:
        return 1
    comparator = ">" if direction == "desc" else "<"
    where = [f"conversation_position {comparator} ?"]
    params: list[Any] = [row["conversation_position"]]
    if sender:
        where.append("sender = ?")
        params.append(sender)
    preceding = connection.execute(
        f"SELECT COUNT(*) AS count FROM messages WHERE {' AND '.join(where)}", params
    ).fetchone()["count"]
    return preceding // per_page + 1
