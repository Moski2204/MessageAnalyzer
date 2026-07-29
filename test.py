from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

from bs4 import BeautifulSoup

from analysis import analyze_messages, parse_stop_words
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
        with connect_readonly(self.database) as connection:
            result = analyze_messages(
                connection,
                date_to_unix("2024-01-02"),
                date_to_unix("2024-01-02", end=True),
                parse_stop_words("the first"),
                20,
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
                client.post(
                    "/analysis",
                    data={
                        "start_date": "2024-01-02",
                        "end_date": "2024-01-02",
                        "top_n": "20",
                        "stop_words": "the and",
                    },
                ),
            ]
            self.assertTrue(all(response.status_code == 200 for response in checks))
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
            analysis_download = client.post(
                "/analysis/download",
                data={
                    "start_date": "2024-01-02",
                    "end_date": "2024-01-02",
                    "top_n": "20",
                    "stop_words": "the and",
                },
            )
            self.assertEqual(analysis_download.status_code, 200)
            analysis_download.close()

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

        self.assertEqual(home.status_code, 200)
        self.assertEqual(redirected_search.status_code, 200)
        for response in (home, redirected_search):
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

        self.assertEqual(home.status_code, 200)
        self.assertEqual(blocked_search.status_code, 302)
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


if __name__ == "__main__":
    unittest.main()
