from __future__ import annotations

import io
import json
import re
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup

from analysis import analyze_messages, parse_stop_words
from analysis_jobs import (
    ANALYSIS_VERSION,
    AnalysisCache,
    AnalysisJobManager,
    AnalysisSettings,
    build_cache_key,
    source_database_fingerprint,
)
from database import (
    clean_search_filters,
    connect_readonly,
    date_to_unix,
    page_for_message,
    search_messages,
)
from parser import (
    ParsedMessage,
    PhotoIndex,
    discover_message_files,
    normalize_text,
    parse_message_file,
)
from reports import generate_analysis_report, generate_search_report


def message(sender: str, text: str, stamp: str, extra: str = "") -> str:
    return f"""
    <div class="pam _3-95 _2ph- _a6-g uiBoxWhite noborder">
      <h2>{sender}</h2>
      <div class="_3-95 _a6-p"><div><div>{text}</div></div>{extra}</div>
      <div class="_3-94 _a6-o">{stamp}</div>
    </div>
    """


def seed_database(
    database_path: Path,
    rows: list[dict[str, Any]],
    summary_overrides: dict[str, Any] | None = None,
) -> None:
    """Create a complete viewer fixture without using production write helpers."""
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database_path)) as connection:
        connection.executescript(
            """
            CREATE TABLE messages (
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

            CREATE VIRTUAL TABLE messages_fts USING fts5(
                message_text,
                content='messages',
                content_rowid='id',
                tokenize='unicode61 remove_diacritics 0'
            );

            CREATE TABLE app_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE INDEX idx_messages_sender ON messages(sender);
            CREATE INDEX idx_messages_timestamp ON messages(timestamp_unix);
            CREATE INDEX idx_messages_position
                ON messages(conversation_position);
            """
        )
        for position, row in enumerate(rows, start=1):
            message_text = str(row.get("message_text", ""))
            timestamp_unix = row.get("timestamp_unix")
            timestamp = row.get("timestamp")
            if timestamp is None and timestamp_unix is not None:
                timestamp = connection.execute(
                    "SELECT datetime(?, 'unixepoch')", (timestamp_unix,)
                ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO messages (
                    sender, timestamp, timestamp_unix, original_timestamp,
                    message_text, normalized_text, message_type,
                    source_filename, source_file_number, source_position,
                    attachment_path, external_url, deduplication_key,
                    conversation_position, sentiment_label, sentiment_score
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.get("sender", "A"),
                    timestamp,
                    timestamp_unix,
                    row.get("original_timestamp", timestamp or "Unknown time"),
                    message_text,
                    row.get("normalized_text", normalize_text(message_text)),
                    row.get("message_type", "text"),
                    row.get("source_filename", "message_1.html"),
                    row.get("source_file_number", 1),
                    row.get("source_position", position - 1),
                    row.get("attachment_path"),
                    row.get("external_url"),
                    row.get("deduplication_key", f"fixture-{position}"),
                    row.get("conversation_position", position),
                    row.get("sentiment_label", "neutral"),
                    row.get("sentiment_score", 0.0),
                ),
            )
        connection.execute(
            """
            INSERT INTO messages_fts(rowid, message_text)
            SELECT id, message_text FROM messages
            """
        )
        oldest_newest = connection.execute(
            """
            SELECT MIN(timestamp) AS oldest_timestamp,
                   MAX(timestamp) AS newest_timestamp
            FROM messages
            WHERE timestamp_unix IS NOT NULL
            """
        ).fetchone()
        summary = {
            "files_found": 1,
            "files_processed": 1,
            "files_failed": 0,
            "messages_imported": len(rows),
            "duplicates_skipped": 0,
            "messages_skipped_other_senders": 0,
            "oldest_timestamp": oldest_newest[0],
            "newest_timestamp": oldest_newest[1],
        }
        if summary_overrides:
            summary.update(summary_overrides)
        connection.execute(
            "INSERT INTO app_metadata(key, value) VALUES ('import_summary', ?)",
            (json.dumps(summary),),
        )
        connection.commit()


def parsed_row(parsed: ParsedMessage) -> dict[str, Any]:
    return {
        "sender": parsed.sender,
        "timestamp": parsed.timestamp,
        "timestamp_unix": parsed.timestamp_unix,
        "original_timestamp": parsed.original_timestamp,
        "message_text": parsed.message_text,
        "normalized_text": parsed.normalized_text,
        "message_type": parsed.message_type,
        "source_filename": parsed.source_filename,
        "source_file_number": parsed.source_file_number,
        "source_position": parsed.source_position,
        "attachment_path": parsed.attachment_path,
        "external_url": parsed.external_url,
        "deduplication_key": parsed.deduplication_key,
    }


def standard_rows() -> list[dict[str, Any]]:
    day_start = date_to_unix("2024-01-02")
    assert day_start is not None

    def row(
        minute: int,
        sender: str,
        text: str,
        source_position: int,
    ) -> dict[str, Any]:
        return {
            "sender": sender,
            "timestamp_unix": day_start + (9 * 60 + minute) * 60,
            "original_timestamp": f"Jan 02, 2024, 9:{minute:02d} am",
            "message_text": text,
            "source_position": source_position,
        }

    return [
        row(0, "A", "<script>alert(1)</script>", 8),
        row(1, "A", "First message", 7),
        row(5, "B", "Second message", 6),
        row(6, "B", "Hello there?", 5),
        row(10, "A", "Echo", 4),
        row(10, "A", "Echo", 3),
        row(20, "A", "Later & safe", 2),
        row(21, "A", "سلام 😊 café", 1),
    ]


class MessageAnalyzerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            prefix=".message-analyzer-test-",
            dir=Path(__file__).resolve().parent,
        )
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.database = self.root / "instance" / "messages.db"
        self.analysis_cache = self.root / "instance" / "analysis_cache.db"
        self.reports = self.root / "reports"

        # Export order is newest-first. One duplicate crosses the two chunks.
        html_2 = (
            "<html><body>"
            + message("Meta AI", "Excluded assistant message", "Jan 02, 2024, 9:22 am")
            + message("A", "سلام 😊 café", "Jan 02, 2024, 9:21 am")
            + message("A", "Later &amp; safe", "Jan 02, 2024, 9:20 am")
            + message("A", "Echo", "Jan 02, 2024, 9:10 am")
            + message("A", "Echo", "Jan 02, 2024, 9:10 am")
            + message("B", "Hello there?", "Jan 02, 2024, 9:06 am")
            + message("B", "Second message", "Jan 02, 2024, 9:05 am")
            + "</body></html>"
        )
        html_10 = (
            "<html><body>"
            + message("B", "Second message", "Jan 02, 2024, 9:05 am")
            + message(
                "A",
                "First message",
                "Jan 02, 2024, 9:01 am",
                '<ul><li><span>reaction-name</span></li></ul>',
            )
            + message("A", "&lt;script&gt;alert(1)&lt;/script&gt;", "Jan 02, 2024, 9:00 am")
            + "</body></html>"
        )
        (self.data / "message_10.html").write_text(html_10, encoding="utf-8")
        (self.data / "message_2.html").write_text(html_2, encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_discovery_parser_dedup_fts_filters_and_target(self):
        files = discover_message_files(self.data)
        self.assertEqual([path.name for path in files], ["message_2.html", "message_10.html"])

        first_chunk = list(parse_message_file(files[0], self.data))
        second_chunk = list(parse_message_file(files[1], self.data))
        self.assertEqual(first_chunk[0].sender, "Meta AI")
        self.assertEqual(first_chunk[1].sender, "A")
        self.assertEqual(first_chunk[1].message_text, "سلام 😊 café")
        self.assertEqual(first_chunk[2].message_text, "Later & safe")
        self.assertIsNotNone(first_chunk[0].timestamp_unix)

        echo_keys = [
            parsed.deduplication_key
            for parsed in first_chunk
            if parsed.message_text == "Echo"
        ]
        self.assertEqual(len(echo_keys), 2)
        self.assertEqual(len(set(echo_keys)), 2)
        first_second_message = next(
            parsed
            for parsed in first_chunk
            if parsed.message_text == "Second message"
        )
        overlapping_second_message = next(
            parsed
            for parsed in second_chunk
            if parsed.message_text == "Second message"
        )
        self.assertEqual(
            first_second_message.deduplication_key,
            overlapping_second_message.deduplication_key,
        )

        seed_database(
            self.database,
            standard_rows(),
            {
                "files_found": 2,
                "files_processed": 2,
                "duplicates_skipped": 1,
                "messages_skipped_other_senders": 1,
            },
        )
        with connect_readonly(self.database) as connection:
            filters = clean_search_filters(
                {
                    "q": "First message",
                    "mode": "phrase",
                    "sender": "A",
                    "start_date": "2024-01-02",
                    "end_date": "2024-01-02",
                    "per_page": 25,
                }
            )
            total, rows = search_messages(connection, filters)
            self.assertEqual(total, 1)
            self.assertEqual(rows[0]["sender"], "A")
            target_page = page_for_message(connection, rows[0]["id"], "asc", "", 50)
            self.assertEqual(target_page, 1)

            empty_text_filters = clean_search_filters(
                {"sender": "B", "start_date": "2024-01-02", "per_page": 25}
            )
            filtered_total, _ = search_messages(connection, empty_text_filters)
            self.assertEqual(filtered_total, 2)

            emoji_filters = clean_search_filters(
                {"q": "😊", "mode": "contains", "per_page": 25}
            )
            emoji_total, emoji_rows = search_messages(connection, emoji_filters)
            self.assertEqual(emoji_total, 1)
            self.assertIn("سلام", emoji_rows[0]["message_text"])

            all_words = clean_search_filters(
                {"q": "Hello there", "mode": "all", "per_page": 25}
            )
            any_words = clean_search_filters(
                {"q": "Hello nonexistentword", "mode": "any", "per_page": 25}
            )
            self.assertEqual(search_messages(connection, all_words)[0], 1)
            self.assertEqual(search_messages(connection, any_words)[0], 1)

    def test_analysis_runs_stop_words_and_safe_report(self):
        seed_database(self.database, standard_rows())
        progress_updates = []
        with connect_readonly(self.database) as connection:
            result = analyze_messages(
                connection,
                date_to_unix("2024-01-02"),
                date_to_unix("2024-01-02", end=True),
                parse_stop_words("the first"),
                20,
                progress_callback=lambda *values: progress_updates.append(values),
                chunk_size=2,
            )
            self.assertEqual(result["total_messages"], 8)
            response_counts = {
                row["sender"]: row["count"] for row in result["responses"]
            }
            self.assertEqual(response_counts["A"], 1)
            self.assertEqual(response_counts["B"], 1)
            self.assertNotIn(
                "first", [row["word"] for row in result["words"]["overall"]]
            )
            self.assertEqual(progress_updates[-1][0], "Building conversation patterns")
            self.assertEqual(progress_updates[-1][2:], (8, 8))
            self.assertTrue(
                {
                    "Loading messages",
                    "Calculating response times",
                    "Counting frequent words",
                    "Calculating sentiment",
                    "Building conversation patterns",
                }.issubset({update[0] for update in progress_updates})
            )

            filters = clean_search_filters(
                {"q": "script", "mode": "contains", "per_page": 25}
            )
            total, _ = search_messages(connection, filters)
            report = generate_search_report(connection, self.reports, filters, total)
            analysis_report = generate_analysis_report(
                self.reports, result, "2024-01-02", "2024-01-02", 20
            )
        content = report.read_text(encoding="utf-8")
        self.assertNotIn("<script>alert(1)</script>", content)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", content)
        self.assertIn("Conversation Patterns", analysis_report.read_text(encoding="utf-8"))

    def test_flask_pages_and_download_routes(self):
        import app as app_module

        seed_database(self.database, standard_rows())
        with connect_readonly(self.database) as connection:
            target = connection.execute(
                "SELECT id FROM messages ORDER BY conversation_position LIMIT 1"
            ).fetchone()["id"]

        with (
            patch.object(app_module, "DATA_DIR", self.data),
            patch.object(app_module, "DATABASE_PATH", self.database),
            patch.object(app_module, "ANALYSIS_CACHE_PATH", self.analysis_cache),
            patch.object(app_module, "REPORTS_DIR", self.reports),
        ):
            app_module.app.config["TESTING"] = True
            client = app_module.app.test_client()
            checks = [
                client.get("/"),
                client.get("/__dev/version"),
                client.get("/search"),
                client.get(
                    "/search/report?q=First+message&mode=phrase&per_page=25"
                ),
                client.get(f"/conversation?target={target}&direction=asc"),
                client.get("/analysis"),
            ]
            self.assertTrue(all(response.status_code == 200 for response in checks))
            analysis_html = checks[-1].get_data(as_text=True)
            self.assertIn('name="full_conversation"', analysis_html)
            self.assertIn(">Analyze<", analysis_html)
            self.assertIn("Opening this page never starts a calculation.", analysis_html)
            self.assertIsNone(app_module._analysis_manager().active_job())
            home_html = checks[0].get_data(as_text=True)
            self.assertNotIn("hot_reload.js", home_html)
            home = BeautifulSoup(home_html, "html.parser")
            home_actions = home.select("section.actions a.button")
            self.assertEqual(
                [action.get_text(" ", strip=True) for action in home_actions],
                [
                    "Search Conversation",
                    "View Full Conversation",
                    "View Analysis",
                ],
            )
            self.assertEqual(
                [action.get("href") for action in home_actions],
                ["/search", "/conversation", "/analysis"],
            )
            self.assertFalse(home.select("section.actions form"))
            self.assertFalse(home.select("section.actions button"))
            self.assertNotIn("Import Messages", home_html)
            self.assertNotIn("Rebuild Database", home_html)
            self.assertIn("version", checks[1].get_json())
            for removed_path in ("/import", "/rebuild"):
                with self.subTest(path=removed_path, method="GET"):
                    self.assertEqual(client.get(removed_path).status_code, 404)
                with self.subTest(path=removed_path, method="POST"):
                    self.assertEqual(client.post(removed_path).status_code, 404)
            search_download = client.get(
                "/search/report/download?q=not-a-real-match&mode=contains"
            )
            self.assertEqual(search_download.status_code, 200)
            self.assertIn("attachment", search_download.headers["Content-Disposition"])
            search_download.close()

            token = app_module.app.config["ANALYSIS_ACTION_TOKEN"]
            started = client.post(
                "/analysis/start",
                data={
                    "action_token": token,
                    "start_date": "2024-01-02",
                    "end_date": "2024-01-02",
                    "top_n": "20",
                    "stop_words": "the and",
                },
            )
            self.assertEqual(started.status_code, 303)
            job_id = started.headers["Location"].rstrip("/").split("/")[-1]
            manager = app_module._analysis_manager()
            completed = manager.wait_for_job(job_id)
            self.assertIsNotNone(completed)
            self.assertEqual(completed["status"], "complete")

            status_response = client.get(f"/analysis/jobs/{job_id}/status")
            self.assertEqual(status_response.status_code, 200)
            self.assertEqual(status_response.get_json()["status"], "complete")
            self.assertEqual(status_response.headers["Cache-Control"], "no-store, max-age=0")
            result_response = client.get(f"/analysis/results/{job_id}")
            self.assertEqual(result_response.status_code, 200)
            self.assertIn("Analysis Results", result_response.get_data(as_text=True))

            cached = client.post(
                "/analysis/start",
                data={
                    "action_token": token,
                    "start_date": "2024-01-02",
                    "end_date": "2024-01-02",
                    "top_n": "20",
                    "stop_words": "the and",
                },
            )
            self.assertEqual(cached.status_code, 303)
            self.assertIn(f"/analysis/results/{job_id}", cached.headers["Location"])
            self.assertIn("cached=1", cached.headers["Location"])

            recalculated = client.post(
                f"/analysis/results/{job_id}/recalculate",
                data={"action_token": token},
            )
            self.assertEqual(recalculated.status_code, 303)
            recalculated_id = (
                recalculated.headers["Location"].rstrip("/").split("/")[-1]
            )
            self.assertNotEqual(recalculated_id, job_id)
            self.assertEqual(
                manager.wait_for_job(recalculated_id)["status"], "complete"
            )

            def fail_if_recomputed(*_args, **_kwargs):
                raise AssertionError("cached download must not recompute")

            manager.analyzer = fail_if_recomputed
            analysis_download = client.post(
                f"/analysis/results/{recalculated_id}/download",
                data={"action_token": token},
            )
            self.assertEqual(analysis_download.status_code, 200)
            self.assertIn(
                "attachment", analysis_download.headers["Content-Disposition"]
            )
            analysis_download.close()
            message_bytes = self.database.read_bytes()
            message_stat = self.database.stat()
            self.assertEqual(
                client.post(
                    "/analysis/cache/clear",
                    data={"action_token": token},
                ).status_code,
                400,
            )
            cleared = client.post(
                "/analysis/cache/clear",
                data={"action_token": token, "confirm": "clear"},
            )
            self.assertEqual(cleared.status_code, 303)
            self.assertEqual(manager.recent_completed(), [])
            self.assertEqual(self.database.read_bytes(), message_bytes)
            unchanged_stat = self.database.stat()
            self.assertEqual(unchanged_stat.st_size, message_stat.st_size)
            self.assertEqual(
                unchanged_stat.st_mtime_ns, message_stat.st_mtime_ns
            )

    def test_analysis_requires_explicit_scope_and_get_stays_fast(self):
        import app as app_module

        seed_database(self.database, standard_rows())
        with (
            patch.object(app_module, "DATABASE_PATH", self.database),
            patch.object(app_module, "ANALYSIS_CACHE_PATH", self.analysis_cache),
        ):
            app_module.app.config["TESTING"] = True
            client = app_module.app.test_client()
            started = time.monotonic()
            landing = client.get("/analysis")
            elapsed = time.monotonic() - started
            self.assertEqual(landing.status_code, 200)
            self.assertLess(elapsed, 2)
            self.assertIn("may take several minutes", landing.get_data(as_text=True))

            token = app_module.app.config["ANALYSIS_ACTION_TOKEN"]
            blank = client.post(
                "/analysis/start",
                data={"action_token": token, "top_n": "20"},
            )
            reversed_range = client.post(
                "/analysis/start",
                data={
                    "action_token": token,
                    "start_date": "2024-01-03",
                    "end_date": "2024-01-02",
                },
            )
            empty_range = client.post(
                "/analysis/start",
                data={
                    "action_token": token,
                    "start_date": "2025-01-01",
                    "end_date": "2025-01-02",
                },
            )
            self.assertEqual(blank.status_code, 400)
            self.assertEqual(reversed_range.status_code, 400)
            self.assertEqual(empty_range.status_code, 400)
            self.assertIsNone(app_module._analysis_manager().active_job())

            full = client.post(
                "/analysis/start",
                data={
                    "action_token": token,
                    "full_conversation": "1",
                    "top_n": "20",
                    "stop_words": "the and",
                },
            )
            self.assertEqual(full.status_code, 303)
            job_id = full.headers["Location"].rstrip("/").split("/")[-1]
            self.assertEqual(
                app_module._analysis_manager().wait_for_job(job_id)["status"],
                "complete",
            )

    def test_single_background_worker_does_not_block_submission(self):
        seed_database(self.database, standard_rows())
        entered = threading.Event()
        release = threading.Event()

        def blocking_analyzer(connection, *args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test worker was not released")
            return analyze_messages(connection, *args, **kwargs)

        manager = AnalysisJobManager(
            self.database,
            self.analysis_cache,
            analyzer=blocking_analyzer,
            chunk_size=2,
        )
        settings = AnalysisSettings("", "", True, 20, "the and")
        started = time.monotonic()
        first = manager.submit(settings)
        submit_elapsed = time.monotonic() - started
        self.assertEqual(first["outcome"], "started")
        self.assertLess(submit_elapsed, 2)
        self.assertTrue(entered.wait(1))
        try:
            second = manager.submit(settings)
            self.assertEqual(second["outcome"], "busy")
            self.assertEqual(
                second["job"]["job_id"], first["job"]["job_id"]
            )
        finally:
            release.set()
        completed = manager.wait_for_job(first["job"]["job_id"])
        self.assertEqual(completed["status"], "complete")
        self.assertEqual(completed["processed_messages"], 8)
        self.assertEqual(completed["progress"], 100)

    def test_analysis_cache_keys_cover_all_invalidation_inputs(self):
        seed_database(self.database, standard_rows())
        fingerprint = source_database_fingerprint(self.database)
        base = AnalysisSettings(
            "2024-01-02", "2024-01-02", False, 20, "the and"
        )
        base_key = build_cache_key(base, fingerprint)
        equivalent_stop_words = AnalysisSettings(
            "2024-01-02", "2024-01-02", False, 20, "and   the"
        )
        self.assertEqual(
            base_key, build_cache_key(equivalent_stop_words, fingerprint)
        )
        variants = [
            AnalysisSettings(
                "2024-01-01", "2024-01-02", False, 20, "the and"
            ),
            AnalysisSettings("", "", True, 20, "the and"),
            AnalysisSettings(
                "2024-01-02", "2024-01-02", False, 50, "the and"
            ),
            AnalysisSettings(
                "2024-01-02", "2024-01-02", False, 20, "the and new"
            ),
        ]
        for settings in variants:
            with self.subTest(settings=settings):
                self.assertNotEqual(
                    base_key, build_cache_key(settings, fingerprint)
                )
        self.assertNotEqual(
            base_key,
            build_cache_key(base, fingerprint, ANALYSIS_VERSION + "-changed"),
        )

        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE messages SET message_text = message_text || 'x' WHERE id = 1"
            )
            connection.commit()
        changed_fingerprint = source_database_fingerprint(self.database)
        self.assertNotEqual(
            fingerprint["digest"], changed_fingerprint["digest"]
        )
        self.assertNotEqual(
            base_key, build_cache_key(base, changed_fingerprint)
        )

    def test_source_change_invalidates_cached_pages_and_downloads(self):
        import app as app_module

        seed_database(self.database, standard_rows())
        with (
            patch.object(app_module, "DATABASE_PATH", self.database),
            patch.object(app_module, "ANALYSIS_CACHE_PATH", self.analysis_cache),
            patch.object(app_module, "REPORTS_DIR", self.reports),
        ):
            app_module.app.config["TESTING"] = True
            client = app_module.app.test_client()
            token = app_module.app.config["ANALYSIS_ACTION_TOKEN"]
            submitted = client.post(
                "/analysis/start",
                data={
                    "action_token": token,
                    "start_date": "2024-01-02",
                    "end_date": "2024-01-02",
                    "top_n": "20",
                    "stop_words": "the and",
                },
            )
            job_id = submitted.headers["Location"].rstrip("/").split("/")[-1]
            manager = app_module._analysis_manager()
            self.assertEqual(
                manager.wait_for_job(job_id)["status"], "complete"
            )
            self.assertEqual(
                client.get(f"/analysis/results/{job_id}").status_code,
                200,
            )

            with closing(sqlite3.connect(self.database)) as connection:
                connection.execute(
                    """
                    UPDATE messages
                    SET message_text = message_text || 'x'
                    WHERE id = 1
                    """
                )
                connection.commit()

            landing_html = client.get("/analysis").get_data(as_text=True)
            self.assertNotIn(job_id, landing_html)
            stale_result = client.get(f"/analysis/results/{job_id}")
            self.assertEqual(stale_result.status_code, 409)
            self.assertIn(
                "no longer valid", stale_result.get_data(as_text=True)
            )
            stale_download = client.post(
                f"/analysis/results/{job_id}/download",
                data={"action_token": token},
            )
            self.assertEqual(stale_download.status_code, 303)

    def test_interrupted_corrupt_and_cleared_cache_never_change_messages(self):
        seed_database(self.database, standard_rows())
        source_before = self.database.read_bytes()
        data_before = {
            path.name: path.read_bytes() for path in self.data.glob("*.html")
        }
        settings = AnalysisSettings("", "", True, 20, "the and")
        fingerprint = source_database_fingerprint(self.database)
        cache = AnalysisCache(self.analysis_cache)
        stale_id = "a" * 32
        cache.create_job(
            stale_id,
            build_cache_key(settings, fingerprint),
            settings,
            fingerprint,
            8,
        )

        restarted = AnalysisJobManager(self.database, self.analysis_cache)
        stale = restarted.get_job(stale_id)
        self.assertEqual(stale["status"], "interrupted")
        self.assertIn("interrupted", stale["error"].lower())
        self.assertTrue(restarted.clear_cache())
        self.assertEqual(restarted.recent_completed(), [])
        self.assertEqual(self.database.read_bytes(), source_before)
        self.assertEqual(
            {path.name: path.read_bytes() for path in self.data.glob("*.html")},
            data_before,
        )

        self.analysis_cache.unlink()
        self.assertEqual(restarted.recent_completed(), [])
        self.assertTrue(self.analysis_cache.is_file())
        self.analysis_cache.write_bytes(b"runtime cache damage")
        self.assertEqual(restarted.recent_completed(), [])
        self.assertTrue(restarted.cache.recovered_corrupt_cache)

        corrupt_path = self.root / "instance" / "corrupt-analysis-cache.db"
        corrupt_path.write_bytes(b"not a SQLite database")
        recovered = AnalysisCache(corrupt_path)
        self.assertTrue(recovered.recovered_corrupt_cache)
        self.assertTrue(corrupt_path.is_file())
        self.assertTrue(
            list(corrupt_path.parent.glob("corrupt-analysis-cache.corrupt-*.db"))
        )
        self.assertEqual(self.database.read_bytes(), source_before)

    def test_background_failures_are_sanitized(self):
        seed_database(self.database, standard_rows())
        private_marker = "PRIVATE-MESSAGE-BODY-MUST-NOT-LEAK"

        def failing_analyzer(*_args, **_kwargs):
            raise RuntimeError(private_marker)

        manager = AnalysisJobManager(
            self.database,
            self.analysis_cache,
            analyzer=failing_analyzer,
        )
        submitted = manager.submit(
            AnalysisSettings("", "", True, 20, "the and")
        )
        failed = manager.wait_for_job(submitted["job"]["job_id"])
        self.assertEqual(failed["status"], "failed")
        self.assertNotIn(private_marker, failed["error"])
        self.assertIsNone(failed["result"])

        transition_cache = self.root / "instance" / "transition-cache.db"
        transition_manager = AnalysisJobManager(
            self.database, transition_cache
        )
        original_set_running = transition_manager.cache.set_running

        def fail_transition(_job_id):
            raise sqlite3.OperationalError("simulated cache transition failure")

        transition_manager.cache.set_running = fail_transition
        transition_submission = transition_manager.submit(
            AnalysisSettings("", "", True, 20, "the and")
        )
        transition_failed = transition_manager.wait_for_job(
            transition_submission["job"]["job_id"]
        )
        self.assertEqual(transition_failed["status"], "failed")
        self.assertIsNone(transition_manager.active_job())

        transition_manager.cache.set_running = original_set_running
        retried = transition_manager.submit(
            AnalysisSettings("", "", True, 20, "the and")
        )
        self.assertEqual(
            transition_manager.wait_for_job(retried["job"]["job_id"])[
                "status"
            ],
            "complete",
        )

    def test_missing_database_shows_one_restore_notice_without_creating_a_file(self):
        import app as app_module

        missing_database = self.root / "missing-instance" / "messages.db"
        expected_message = (
            "The local message database could not be found. Restore "
            "instance/messages.db from your backup before using the application."
        )

        with patch.object(app_module, "DATABASE_PATH", missing_database):
            app_module.app.config["TESTING"] = True
            client = app_module.app.test_client()

            home = client.get("/")
            redirected_search = client.get("/search", follow_redirects=True)
            redirected_analysis = client.get(
                "/analysis", follow_redirects=True
            )
            blocked_analysis_start = client.post(
                "/analysis/start",
                data={
                    "action_token": app_module.app.config[
                        "ANALYSIS_ACTION_TOKEN"
                    ],
                    "full_conversation": "1",
                },
            )

        self.assertEqual(home.status_code, 200)
        self.assertEqual(redirected_search.status_code, 200)
        self.assertEqual(redirected_analysis.status_code, 200)
        self.assertEqual(blocked_analysis_start.status_code, 302)
        for response in (home, redirected_search, redirected_analysis):
            html = response.get_data(as_text=True)
            self.assertEqual(html.count(expected_message), 1)
            self.assertEqual(html.count("Database unavailable."), 1)
            self.assertNotIn("Import Messages", html)
            self.assertNotIn("Rebuild Database", html)
        self.assertFalse(missing_database.exists())

    def test_corrupt_database_shows_restore_notice_and_redirects_viewer_routes(self):
        import app as app_module

        corrupt_database = self.root / "corrupt-instance" / "messages.db"
        corrupt_database.parent.mkdir(parents=True)
        corrupt_contents = b"harmless test bytes; not a SQLite database"
        corrupt_database.write_bytes(corrupt_contents)
        expected_message = (
            "The local message database could not be found. Restore "
            "instance/messages.db from your backup before using the application."
        )

        with patch.object(app_module, "DATABASE_PATH", corrupt_database):
            app_module.app.config["TESTING"] = True
            client = app_module.app.test_client()

            home = client.get("/")
            blocked_search = client.get("/search")
            redirected_search = client.get("/search", follow_redirects=True)
            blocked_analysis = client.get("/analysis")

        self.assertEqual(home.status_code, 200)
        self.assertEqual(blocked_search.status_code, 302)
        self.assertEqual(blocked_analysis.status_code, 302)
        self.assertTrue(blocked_search.headers["Location"].endswith("/"))
        self.assertEqual(redirected_search.status_code, 200)
        for response in (home, redirected_search):
            html = response.get_data(as_text=True)
            self.assertEqual(html.count(expected_message), 1)
            self.assertEqual(html.count("Database unavailable."), 1)
        self.assertEqual(corrupt_database.read_bytes(), corrupt_contents)

    def test_readonly_database_connection_allows_queries_and_rejects_writes(self):
        database = self.root / "readonly-instance" / "messages.db"
        database.parent.mkdir(parents=True)
        with closing(sqlite3.connect(database)) as writable_connection:
            writable_connection.execute(
                "CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
            )
            writable_connection.execute(
                "INSERT INTO sample (value) VALUES (?)",
                ("local test value",),
            )
            writable_connection.commit()

        with connect_readonly(database) as readonly_connection:
            row = readonly_connection.execute(
                "SELECT value FROM sample WHERE id = 1"
            ).fetchone()
            self.assertEqual(row["value"], "local test value")
            with self.assertRaises(sqlite3.OperationalError):
                readonly_connection.execute(
                    "INSERT INTO sample (value) VALUES (?)",
                    ("must not be written",),
                )

        with closing(sqlite3.connect(database)) as verification_connection:
            stored_count = verification_connection.execute(
                "SELECT COUNT(*) FROM sample"
            ).fetchone()[0]
        self.assertEqual(stored_count, 1)

    def test_direct_start_prints_url_and_disables_debug_and_reloader(self):
        import app as app_module

        output = io.StringIO()
        with (
            patch("sys.argv", ["app.py"]),
            patch.object(app_module, "REPORTS_DIR", self.reports),
            patch.object(app_module.app, "run") as run,
            redirect_stdout(output),
        ):
            app_module.main()

        self.assertIn("Message Analyzer is starting.", output.getvalue())
        self.assertEqual(output.getvalue().count(app_module.LOCAL_APP_URL), 1)
        self.assertIn("Press Ctrl+C to stop the server.", output.getvalue())
        run.assert_called_once_with(
            host="127.0.0.1",
            port=5000,
            debug=False,
            use_reloader=False,
        )

    def test_photo_index_rejects_ambiguous_and_unsafe_paths(self):
        photos = self.root / "duplicate-photos"
        (photos / "first").mkdir(parents=True)
        (photos / "second").mkdir()
        (photos / "first" / "same.jpg").write_bytes(b"\xff\xd8\xfffirst")
        (photos / "second" / "same.jpg").write_bytes(b"\xff\xd8\xffsecond")

        index = PhotoIndex.build(photos)

        self.assertEqual(index.file_count, 2)
        self.assertEqual(index.ambiguous_filename_count, 1)
        self.assertEqual(
            index.resolve("export/messages/chat/photos/first/same.jpg"),
            "first/same.jpg",
        )
        self.assertIsNone(index.resolve("export/messages/chat/photos/same.jpg"))
        self.assertIsNone(index.resolve("../photos/first/same.jpg"))
        self.assertIsNone(index.resolve("C:\\private\\photos\\same.jpg"))
        self.assertIsNone(index.resolve("https://example.com/photo.jpg"))

    def test_distinct_missing_photos_do_not_collapse_during_deduplication(self):
        data = self.root / "missing-photo-case"
        (data / "photos").mkdir(parents=True)
        stamp = "Jan 02, 2024, 9:01 am"
        html = (
            "<html><body>"
            + message(
                "A",
                "",
                stamp,
                '<img src="export/chat/photos/missing-one.jpg">',
            )
            + message(
                "A",
                "",
                stamp,
                '<img src="export/chat/photos/missing-two.jpg">',
            )
            + "</body></html>"
        )
        html_path = data / "message_1.html"
        html_path.write_text(html, encoding="utf-8")

        parsed = list(parse_message_file(html_path, data))

        self.assertEqual(len(parsed), 2)
        self.assertEqual(
            [item.message_type for item in parsed],
            ["photo", "photo"],
        )
        self.assertTrue(all(item.attachment_path is None for item in parsed))
        self.assertEqual(len({item.deduplication_key for item in parsed}), 2)

    def test_missing_local_gif_keeps_gif_type_without_storing_unsafe_path(self):
        data = self.root / "missing-gif-case"
        (data / "photos").mkdir(parents=True)
        html = (
            "<html><body>"
            + message(
                "A",
                "",
                "Jan 02, 2024, 9:01 am",
                '<img src="export/chat/photos/missing-animation.gif">',
            )
            + "</body></html>"
        )
        html_path = data / "message_1.html"
        html_path.write_text(html, encoding="utf-8")

        parsed = list(parse_message_file(html_path, data))

        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].message_type, "gif")
        self.assertIsNone(parsed[0].attachment_path)

    def test_distinct_audio_and_video_media_keep_separate_deduplication_keys(self):
        data = self.root / "media-dedup-case"
        (data / "photos").mkdir(parents=True)
        stamp = "Jan 02, 2024, 9:01 am"
        html = (
            "<html><body>"
            + message(
                "A",
                "",
                stamp,
                '<audio src="export/audio/first.m4a"></audio>',
            )
            + message(
                "A",
                "",
                stamp,
                '<audio src="export/audio/second.m4a"></audio>',
            )
            + message(
                "A",
                "",
                stamp,
                '<video src="export/video/first.mp4"></video>',
            )
            + message(
                "A",
                "",
                stamp,
                '<video src="export/video/second.mp4"></video>',
            )
            + "</body></html>"
        )
        html_path = data / "message_1.html"
        html_path.write_text(html, encoding="utf-8")

        parsed = list(parse_message_file(html_path, data))

        self.assertEqual(len(parsed), 4)
        self.assertEqual(
            [item.message_type for item in parsed],
            ["audio", "audio", "video", "video"],
        )
        self.assertTrue(all(item.attachment_path is None for item in parsed))
        self.assertEqual(len({item.deduplication_key for item in parsed}), 4)

    def test_reaction_media_is_ignored_consistently_by_both_parsers(self):
        import parser as parser_module

        data = self.root / "reaction-media-case"
        photos = data / "photos"
        photos.mkdir(parents=True)
        (photos / "reaction.jpg").write_bytes(b"\xff\xd8\xff\xe0reaction")
        html = (
            "<html><body>"
            + message(
                "A",
                "Normal text",
                "Jan 02, 2024, 9:01 am",
                (
                    '<ul><li><img src="export/chat/photos/reaction.jpg">'
                    '<a href="export/chat/photos/reaction.jpg">reaction</a>'
                    "</li></ul>"
                ),
            )
            + "</body></html>"
        )
        html_path = data / "message_1.html"
        html_path.write_text(html, encoding="utf-8")
        photo_index = PhotoIndex.build(photos)

        streamed = list(
            parser_module._parse_message_file_lxml(
                html_path, data, photo_index
            )
        )
        fallback = list(
            parser_module._parse_message_file_bs4(
                html_path, data, photo_index
            )
        )

        self.assertEqual(streamed[0].message_type, "text")
        self.assertIsNone(streamed[0].attachment_path)
        self.assertEqual(fallback[0].message_type, "text")
        self.assertIsNone(fallback[0].attachment_path)
        self.assertEqual(
            streamed[0].deduplication_key,
            fallback[0].deduplication_key,
        )

    def test_local_photo_mapping_display_and_route_security(self):
        import app as app_module

        data = self.root / "photo-case"
        photos = data / "photos"
        photos.mkdir(parents=True)
        valid_name = "valid-photo-without-extension"
        (photos / valid_name).write_bytes(b"\xff\xd8\xff\xe0local-photo")
        html = (
            "<html><body>"
            + message(
                "A",
                "",
                "Jan 02, 2024, 9:01 am",
                (
                    '<img src="your_instagram_activity/messages/chat/photos/'
                    f'{valid_name}">'
                ),
            )
            + message(
                "A",
                "",
                "Jan 02, 2024, 9:02 am",
                (
                    '<img src="your_instagram_activity/messages/chat/photos/'
                    'missing-photo.jpg">'
                ),
            )
            + "</body></html>"
        )
        html_path = data / "message_1.html"
        html_path.write_text(html, encoding="utf-8")
        database = self.root / "photo-instance" / "messages.db"

        parsed = list(parse_message_file(html_path, data))
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0].message_type, "photo")
        self.assertEqual(parsed[0].attachment_path, valid_name)
        self.assertEqual(parsed[1].message_type, "photo")
        self.assertIsNone(parsed[1].attachment_path)

        seed_database(database, [parsed_row(item) for item in parsed])
        with connect_readonly(database) as connection:
            rows = connection.execute(
                """
                SELECT message_type, attachment_path
                FROM messages
                ORDER BY timestamp_unix
                """
            ).fetchall()
        self.assertEqual(rows[0]["message_type"], "photo")
        self.assertEqual(rows[0]["attachment_path"], valid_name)
        self.assertIsNone(rows[1]["attachment_path"])
        self.assertFalse(Path(rows[0]["attachment_path"]).is_absolute())
        self.assertNotIn("..", Path(rows[0]["attachment_path"]).parts)

        with (
            patch.object(app_module, "DATA_DIR", data),
            patch.object(app_module, "PHOTO_DIR", photos),
            patch.object(app_module, "DATABASE_PATH", database),
            patch.object(app_module, "REPORTS_DIR", self.reports),
        ):
            app_module.app.config["TESTING"] = True
            client = app_module.app.test_client()

            photo_response = client.get(f"/photos/{valid_name}")
            self.assertEqual(photo_response.status_code, 200)
            self.assertEqual(photo_response.mimetype, "image/jpeg")
            self.assertEqual(
                photo_response.headers["X-Content-Type-Options"], "nosniff"
            )
            photo_response.close()

            conversation = client.get("/conversation?direction=asc&per_page=50")
            conversation_html = conversation.get_data(as_text=True)
            self.assertEqual(conversation.status_code, 200)
            self.assertIn('loading="lazy"', conversation_html)
            self.assertIn(f"/photos/{valid_name}", conversation_html)
            self.assertIn("Photo unavailable.", conversation_html)

            search = client.get("/search/report?mode=contains&per_page=25")
            search_html = search.get_data(as_text=True)
            self.assertEqual(search.status_code, 200)
            self.assertIn('loading="lazy"', search_html)
            self.assertIn("Photo unavailable.", search_html)

            blocked_paths = (
                "/photos/missing-photo.jpg",
                "/photos/%2e%2e%2fmessage_1.html",
                "/photos/C:%5CWindows%5Cwin.ini",
                "/photos/%5C%5Cserver%5Cshare%5Cphoto.jpg",
                "/media/message_1.html",
            )
            for path in blocked_paths:
                with self.subTest(path=path):
                    self.assertEqual(client.get(path).status_code, 404)

    def test_conversation_only_renders_photos_from_the_current_page(self):
        import app as app_module

        data = self.root / "pagination-photo-case"
        photos = data / "photos"
        photos.mkdir(parents=True)
        day_start = date_to_unix("2024-01-02")
        self.assertIsNotNone(day_start)
        fixture_rows = []
        for minute in range(51):
            filename = f"page-{minute:02d}.jpg"
            (photos / filename).write_bytes(b"\xff\xd8\xff\xe0page-photo")
            fixture_rows.append(
                {
                    "sender": "A",
                    "timestamp_unix": day_start + (9 * 60 + minute) * 60,
                    "original_timestamp": (
                        f"Jan 02, 2024, 9:{minute:02d} am"
                    ),
                    "message_text": "[Photo]",
                    "message_type": "photo",
                    "attachment_path": filename,
                }
            )
        database = self.root / "pagination-photo-instance" / "messages.db"
        seed_database(database, fixture_rows)

        with (
            patch.object(app_module, "DATA_DIR", data),
            patch.object(app_module, "PHOTO_DIR", photos),
            patch.object(app_module, "DATABASE_PATH", database),
        ):
            app_module.app.config["TESTING"] = True
            client = app_module.app.test_client()
            first_page = client.get(
                "/conversation?direction=asc&per_page=50&page=1"
            ).get_data(as_text=True)
            second_page = client.get(
                "/conversation?direction=asc&per_page=50&page=2"
            ).get_data(as_text=True)

        self.assertIn("/photos/page-00.jpg", first_page)
        self.assertNotIn("/photos/page-50.jpg", first_page)
        self.assertIn("/photos/page-50.jpg", second_page)

    def test_frontend_navigation_forms_senders_and_conversation_hooks(self):
        import app as app_module

        allowed_rows = []
        for row in standard_rows():
            allowed = dict(row)
            allowed["sender"] = "Mahrus" if row["sender"] == "A" else "🐧"
            allowed_rows.append(allowed)
        seed_database(self.database, allowed_rows)
        with connect_readonly(self.database) as connection:
            target = connection.execute(
                """
                SELECT id
                FROM messages
                WHERE sender = ? AND message_text = ?
                """,
                ("Mahrus", "First message"),
            ).fetchone()["id"]

        with (
            patch.object(app_module, "DATA_DIR", self.data),
            patch.object(app_module, "DATABASE_PATH", self.database),
            patch.object(app_module, "ANALYSIS_CACHE_PATH", self.analysis_cache),
            patch.object(app_module, "REPORTS_DIR", self.reports),
        ):
            app_module.app.config["TESTING"] = True
            client = app_module.app.test_client()
            responses = {
                "Home": client.get("/"),
                "Search": client.get("/search"),
                "Search report": client.get(
                    "/search/report?q=First+message&mode=phrase&per_page=25"
                ),
                "Conversation": client.get(
                    f"/conversation?target={target}&direction=asc&per_page=50"
                ),
                "Analysis": client.get("/analysis"),
            }
            self.assertTrue(
                all(response.status_code == 200 for response in responses.values())
            )
            soups = {
                name: BeautifulSoup(response.get_data(as_text=True), "html.parser")
                for name, response in responses.items()
            }

            expected_navigation = [
                ("Home", "/"),
                ("Search", "/search"),
                ("Conversation", "/conversation"),
                ("Analysis", "/analysis"),
            ]
            expected_active = {
                "Home": "Home",
                "Search": "Search",
                "Search report": "Search",
                "Conversation": "Conversation",
                "Analysis": "Analysis",
            }
            for page_name, soup in soups.items():
                with self.subTest(page=page_name, contract="navigation"):
                    navigation = soup.select_one("nav[aria-label='Main navigation']")
                    self.assertIsNotNone(navigation)
                    links = navigation.select("a.nav-link")
                    self.assertEqual(
                        [
                            (link.get_text(" ", strip=True), link.get("href"))
                            for link in links
                        ],
                        expected_navigation,
                    )
                    active = navigation.select("a[aria-current='page']")
                    self.assertEqual(len(active), 1)
                    self.assertEqual(
                        active[0].get_text(" ", strip=True),
                        expected_active[page_name],
                    )
                    self.assertIn("is-active", active[0].get("class", []))

            def assert_form_contract(
                soup,
                selector: str,
                action: str,
                method: str,
                field_names: set[str],
            ) -> None:
                form = soup.select_one(selector)
                self.assertIsNotNone(form)
                self.assertEqual(form.get("action"), action)
                self.assertEqual(form.get("method", "get").casefold(), method)
                actual_names = [
                    control.get("name")
                    for control in form.select("[name]")
                    if control.get("name")
                ]
                self.assertEqual(len(actual_names), len(field_names))
                self.assertEqual(
                    set(actual_names),
                    field_names,
                )

            assert_form_contract(
                soups["Search"],
                "form.search-workspace",
                "/search/report",
                "get",
                {
                    "q",
                    "mode",
                    "sender",
                    "start_date",
                    "end_date",
                    "direction",
                    "per_page",
                },
            )
            assert_form_contract(
                soups["Conversation"],
                "form.conversation-filters",
                "/conversation",
                "get",
                {"direction", "sender", "per_page"},
            )
            assert_form_contract(
                soups["Analysis"],
                "form[action='/analysis/start']",
                "/analysis/start",
                "post",
                {
                    "action_token",
                    "start_date",
                    "end_date",
                    "top_n",
                    "full_conversation",
                    "stop_words",
                },
            )
            assert_form_contract(
                soups["Analysis"],
                "form[action='/analysis/cache/clear']",
                "/analysis/cache/clear",
                "post",
                {"action_token", "confirm"},
            )

            for page_name, soup in soups.items():
                with self.subTest(page=page_name, contract="removed-context"):
                    self.assertFalse(soup.select("[name='context'], #context"))
                    self.assertNotIn(
                        "context",
                        {
                            label.get_text(" ", strip=True).casefold()
                            for label in soup.select("label")
                        },
                    )

            for page_name in ("Search", "Conversation"):
                with self.subTest(page=page_name, contract="senders"):
                    options = soups[page_name].select(
                        "select[name='sender'] > option"
                    )
                    self.assertEqual(
                        [option.get("value") for option in options],
                        ["", "Mahrus", "🐧"],
                    )
                    self.assertEqual(
                        [option.get_text(" ", strip=True) for option in options],
                        ["All senders", "Mahrus", "🐧"],
                    )

            search_report = soups["Search report"]
            self.assertIn(
                "Sort: Oldest first",
                search_report.select_one(".filter-summary").get_text(
                    " ", strip=True
                ),
            )
            current_sort = search_report.select(
                ".search-report-actions [aria-current='true']"
            )
            self.assertEqual(len(current_sort), 1)
            self.assertEqual(
                current_sort[0].get_text(" ", strip=True), "Oldest First"
            )

            conversation = soups["Conversation"]
            right_rows = conversation.select(".conversation-row-right")
            left_rows = conversation.select(".conversation-row-left")
            self.assertTrue(right_rows)
            self.assertTrue(left_rows)
            for row in right_rows:
                bubble = row.select_one("article.conversation-bubble-right")
                self.assertIsNotNone(bubble)
                self.assertEqual(
                    bubble.select_one("header strong").get_text(strip=True),
                    "Mahrus",
                )
            for row in left_rows:
                bubble = row.select_one("article.conversation-bubble-left")
                self.assertIsNotNone(bubble)
                self.assertEqual(
                    bubble.select_one("header strong").get_text(strip=True),
                    "🐧",
                )

            selected = conversation.select_one(f"article#message-{target}")
            self.assertIsNotNone(selected)
            self.assertIn("selected", selected.get("class", []))
            self.assertIsNotNone(selected.select_one(".selected-message-badge"))
            deep_link = soups["Search report"].select_one(
                ".message-source-footer a[href^='/conversation?target=']"
            )
            self.assertIsNotNone(deep_link)
            deep_link_url = urlsplit(deep_link.get("href"))
            self.assertEqual(deep_link_url.path, "/conversation")
            self.assertEqual(
                parse_qs(deep_link_url.query),
                {"target": [str(target)], "direction": ["asc"]},
            )
            self.assertEqual(deep_link_url.fragment, f"message-{target}")

    def test_frontend_local_assets_custom_404_and_desktop_css_contracts(self):
        import app as app_module

        seed_database(self.database, standard_rows())
        with (
            patch.object(app_module, "DATABASE_PATH", self.database),
            patch.object(app_module, "ANALYSIS_CACHE_PATH", self.analysis_cache),
        ):
            app_module.app.config["TESTING"] = True
            client = app_module.app.test_client()
            manager = app_module._analysis_manager()
            settings = AnalysisSettings("", "", True, 20, "the and")
            fingerprint = source_database_fingerprint(self.database)
            queued_id = "f" * 32
            manager.cache.create_job(
                queued_id,
                build_cache_key(settings, fingerprint),
                settings,
                fingerprint,
                len(standard_rows()),
            )

            responses = [
                client.get("/"),
                client.get("/search"),
                client.get("/conversation?direction=asc&per_page=50"),
                client.get("/analysis"),
                client.get(f"/analysis/jobs/{queued_id}"),
            ]
            self.assertTrue(all(response.status_code == 200 for response in responses))
            rendered_pages = [
                BeautifulSoup(response.get_data(as_text=True), "html.parser")
                for response in responses
            ]
            for soup in rendered_pages:
                with self.subTest(title=soup.title.get_text(" ", strip=True)):
                    stylesheets = [
                        link.get("href")
                        for link in soup.select("link[rel~='stylesheet']")
                    ]
                    self.assertEqual(stylesheets, ["/static/styles.css"])
                    for source in [
                        script.get("src")
                        for script in soup.select("script[src]")
                    ]:
                        parsed_source = urlsplit(source)
                        self.assertFalse(parsed_source.scheme)
                        self.assertFalse(parsed_source.netloc)
                        self.assertTrue(parsed_source.path.startswith("/static/"))
                    self.assertNotIn(
                        "/static/hot_reload.js",
                        [
                            script.get("src")
                            for script in soup.select("script[src]")
                        ],
                    )

            progress = rendered_pages[-1]
            progress_panel = progress.select_one("#analysis-progress")
            self.assertFalse(progress_panel.has_attr("aria-busy"))
            self.assertFalse(
                progress.select_one(".progress-details").has_attr("aria-live")
            )
            self.assertEqual(
                progress.select_one("#analysis-stage").get("aria-live"),
                "polite",
            )
            self.assertEqual(
                [
                    script.get("src")
                    for script in progress.select("script[src]")
                ],
                ["/static/analysis_status.js"],
            )
            active = progress.select(
                "nav[aria-label='Main navigation'] a[aria-current='page']"
            )
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0].get_text(" ", strip=True), "Analysis")

            missing = client.get("/this-route-does-not-exist")
            self.assertEqual(missing.status_code, 404)
            missing_page = BeautifulSoup(
                missing.get_data(as_text=True), "html.parser"
            )
            self.assertIn("error-page", missing_page.body.get("class", []))
            state = missing_page.select_one(
                "section.error-page-state[role='status']"
            )
            self.assertIsNotNone(state)
            self.assertEqual(
                state.select_one(".state-code").get_text(strip=True), "404"
            )
            self.assertTrue(state.select_one("h1").get_text(" ", strip=True))
            self.assertEqual(
                {link.get("href") for link in state.select(".actions a")},
                {"/", "/search"},
            )

        static_dir = Path(__file__).resolve().parent / "static"
        stylesheet = (static_dir / "styles.css").read_text(encoding="utf-8")
        rules: dict[str, dict[str, str]] = {}
        css_without_comments = re.sub(
            r"/\*.*?\*/", "", stylesheet, flags=re.DOTALL
        )
        for match in re.finditer(
            r"([^{}]+)\{([^{}]*)\}", css_without_comments
        ):
            declarations = {}
            for declaration in match.group(2).split(";"):
                if ":" not in declaration:
                    continue
                property_name, value = declaration.split(":", 1)
                declarations[property_name.strip()] = value.strip()
            for selector in match.group(1).split(","):
                rules.setdefault(selector.strip(), {}).update(declarations)
        self.assertEqual(rules[":root"]["--content-width"], "1450px")
        self.assertIn("var(--content-width)", rules[".shell"]["width"])
        self.assertEqual(rules[".table-wrap"]["overflow-x"], "auto")

        local_assets = "\n".join(
            path.read_text(encoding="utf-8")
            for path in static_dir.iterdir()
            if path.is_file() and path.suffix in {".css", ".js"}
        ).casefold()
        self.assertNotRegex(local_assets, r"@import\b")
        self.assertNotRegex(local_assets, r"https?://")
        self.assertNotRegex(local_assets, r"""url\s*\(\s*['"]?\s*//""")
        self.assertNotRegex(
            local_assets,
            r"""(?:fetch|importscripts)\s*\(\s*['"`]\s*//""",
        )


if __name__ == "__main__":
    unittest.main()
