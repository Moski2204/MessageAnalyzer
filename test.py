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
from parser import discover_message_files, parse_message_file
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
            self.assertIn(
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


if __name__ == "__main__":
    unittest.main()
