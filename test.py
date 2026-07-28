from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from analysis import analyze_messages, parse_stop_words
from database import (
    clean_search_filters,
    connect,
    date_to_unix,
    import_messages,
    page_for_message,
    search_messages,
)
from parser import PhotoIndex, discover_message_files, parse_message_file
from reports import generate_analysis_report, generate_search_report


def message(sender: str, text: str, stamp: str, extra: str = "") -> str:
    return f"""
    <div class="pam _3-95 _2ph- _a6-g uiBoxWhite noborder">
      <h2>{sender}</h2>
      <div class="_3-95 _a6-p"><div><div>{text}</div></div>{extra}</div>
      <div class="_3-94 _a6-o">{stamp}</div>
    </div>
    """


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

    def test_discovery_parser_import_dedup_fts_filters_and_target(self):
        files = discover_message_files(self.data)
        self.assertEqual([path.name for path in files], ["message_2.html", "message_10.html"])

        parsed = list(parse_message_file(files[0], self.data))
        self.assertEqual(parsed[0].sender, "Meta AI")
        self.assertEqual(parsed[1].sender, "A")
        self.assertEqual(parsed[1].message_text, "سلام 😊 café")
        self.assertEqual(parsed[2].message_text, "Later & safe")
        self.assertIsNotNone(parsed[0].timestamp_unix)

        stats = import_messages(self.data, self.database, allowed_senders={"A", "B"})
        self.assertEqual(stats["files_processed"], 2)
        self.assertEqual(stats["files_failed"], 0)
        self.assertEqual(stats["messages_imported"], 8)
        self.assertEqual(stats["duplicates_skipped"], 1)
        self.assertEqual(stats["messages_skipped_other_senders"], 1)
        repeated_stats = import_messages(
            self.data, self.database, allowed_senders={"A", "B"}
        )
        self.assertEqual(repeated_stats["messages_imported"], 8)
        self.assertEqual(repeated_stats["duplicates_skipped"], 1)
        self.assertEqual(repeated_stats["messages_skipped_other_senders"], 1)

        with connect(self.database) as connection:
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
        stats = import_messages(self.data, self.database, allowed_senders={"A", "B"})
        self.assertEqual(stats["messages_imported"], 8)
        with connect(self.database) as connection:
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

        import_messages(self.data, self.database, allowed_senders={"A", "B"})
        with connect(self.database) as connection:
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
            self.assertNotIn(
                "hot_reload.js",
                checks[0].get_data(as_text=True),
            )
            self.assertIn("version", checks[1].get_json())
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

    def test_normal_start_disables_debug_and_reloader(self):
        import app as app_module

        with (
            patch("sys.argv", ["app.py"]),
            patch.object(app_module, "REPORTS_DIR", self.reports),
            patch.object(app_module.app, "run") as run,
        ):
            app_module.main()

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
        (data / "message_1.html").write_text(html, encoding="utf-8")
        database = self.root / "missing-photo-instance" / "messages.db"

        stats = import_messages(data, database, allowed_senders={"A"})

        self.assertEqual(stats["messages_imported"], 2)
        self.assertEqual(stats["duplicates_skipped"], 0)
        self.assertEqual(stats["photo_references_matched"], 0)
        self.assertEqual(stats["photo_references_unavailable"], 2)
        with connect(database) as connection:
            stored = connection.execute(
                "SELECT attachment_path FROM messages"
            ).fetchall()
        self.assertEqual(len(stored), 2)
        self.assertTrue(all(row["attachment_path"] is None for row in stored))

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
        (data / "message_1.html").write_text(html, encoding="utf-8")
        database = self.root / "missing-gif-instance" / "messages.db"

        stats = import_messages(data, database, allowed_senders={"A"})

        self.assertEqual(stats["messages_imported"], 1)
        self.assertEqual(stats["photo_references_unavailable"], 0)
        with connect(database) as connection:
            row = connection.execute(
                "SELECT message_type, attachment_path FROM messages"
            ).fetchone()
        self.assertEqual(row["message_type"], "gif")
        self.assertIsNone(row["attachment_path"])

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
        (data / "message_1.html").write_text(html, encoding="utf-8")
        database = self.root / "media-dedup-instance" / "messages.db"

        stats = import_messages(data, database, allowed_senders={"A"})

        self.assertEqual(stats["messages_imported"], 4)
        self.assertEqual(stats["duplicates_skipped"], 0)
        with connect(database) as connection:
            rows = connection.execute(
                """
                SELECT message_type, attachment_path
                FROM messages
                ORDER BY id
                """
            ).fetchall()
        self.assertEqual(
            [row["message_type"] for row in rows],
            ["audio", "audio", "video", "video"],
        )
        self.assertTrue(all(row["attachment_path"] is None for row in rows))

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
        (data / "message_1.html").write_text(html, encoding="utf-8")
        database = self.root / "photo-instance" / "messages.db"

        stats = import_messages(data, database, allowed_senders={"A"})
        self.assertEqual(stats["files_found"], 1)
        self.assertEqual(stats["messages_imported"], 2)
        self.assertEqual(stats["photo_files_indexed"], 1)
        self.assertEqual(stats["photo_references_matched"], 1)
        self.assertEqual(stats["photo_references_unavailable"], 1)

        with connect(database) as connection:
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
        messages = []
        for minute in range(51):
            filename = f"page-{minute:02d}.jpg"
            (photos / filename).write_bytes(b"\xff\xd8\xff\xe0page-photo")
            messages.append(
                message(
                    "A",
                    "",
                    f"Jan 02, 2024, 9:{minute:02d} am",
                    f'<img src="export/chat/photos/{filename}">',
                )
            )
        (data / "message_1.html").write_text(
            "<html><body>" + "".join(messages) + "</body></html>",
            encoding="utf-8",
        )
        database = self.root / "pagination-photo-instance" / "messages.db"
        stats = import_messages(data, database, allowed_senders={"A"})
        self.assertEqual(stats["messages_imported"], 51)

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
