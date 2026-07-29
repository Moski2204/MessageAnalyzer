"""Read-only SQLite access, FTS5 search, and pagination."""

from __future__ import annotations

import calendar
import json
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from parser import normalize_text


WORD_RE = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", re.UNICODE)
VALID_SEARCH_MODES = {"contains", "phrase", "all", "any"}


class ClosingConnection(sqlite3.Connection):
    """A sqlite connection whose context manager also releases the file."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect_readonly(database_path: Path) -> sqlite3.Connection:
    """Open an existing database without creating or modifying local files."""
    if not database_path.is_file():
        raise sqlite3.OperationalError("The local message database is missing.")
    database_uri = f"{database_path.resolve().as_uri()}?mode=ro&immutable=1"
    connection = sqlite3.connect(
        database_uri,
        uri=True,
        timeout=30,
        factory=ClosingConnection,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA query_only = ON")
    return connection


def database_ready(database_path: Path) -> bool:
    if not database_path.is_file():
        return False
    try:
        with connect_readonly(database_path) as connection:
            row = connection.execute(
                "SELECT value FROM app_metadata WHERE key = 'import_summary'"
            ).fetchone()
            return row is not None
    except sqlite3.Error:
        return False


def get_database_summary(database_path: Path) -> dict[str, Any] | None:
    if not database_path.is_file():
        return None
    try:
        with connect_readonly(database_path) as connection:
            row = connection.execute(
                "SELECT value FROM app_metadata WHERE key = 'import_summary'"
            ).fetchone()
        if not row:
            return None
        stored = json.loads(row["value"])
        if not isinstance(stored, dict):
            return None
        summary = {
            "message_count": stored.get("messages_imported", 0),
            "oldest_timestamp": stored.get("oldest_timestamp"),
            "newest_timestamp": stored.get("newest_timestamp"),
        }
        for optional_key in (
            "files_processed",
            "files_found",
            "duplicates_skipped",
            "messages_skipped_other_senders",
        ):
            if optional_key in stored:
                summary[optional_key] = stored[optional_key]
        return summary
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
