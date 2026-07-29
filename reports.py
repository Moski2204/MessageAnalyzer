"""Generate escaped, self-contained offline HTML reports."""

from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path
from typing import Any

from database import iter_search_messages


REPORT_CSS = """
:root {
  color-scheme: light;
  --canvas: #f5f3f8;
  --surface: #ffffff;
  --surface-soft: #faf9fc;
  --ink: #1d1a24;
  --muted: #706a7a;
  --line: #ded9e7;
  --accent: #6842d8;
  --accent-dark: #4f2caf;
  --accent-soft: #eee8ff;
  --positive: #147554;
  --neutral: #746d7f;
  --negative: #ad3948;
  --shadow: 0 14px 38px rgba(37, 27, 55, .08);
}
* { box-sizing: border-box; }
html { background: var(--canvas); }
body {
  min-height: 100vh;
  margin: 0;
  color: var(--ink);
  background: var(--canvas);
  font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size: 16px;
  line-height: 1.55;
}
.report-shell {
  width: min(1180px, calc(100% - 2rem));
  margin: 0 auto;
  padding: 2rem 0 3rem;
}
.report-header {
  position: relative;
  overflow: hidden;
  padding: 1.6rem 1.8rem;
  border: 1px solid #d8cff0;
  border-radius: 16px;
  background: #f7f4ff;
  box-shadow: var(--shadow);
}
.brand-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: .75rem;
}
.eyebrow {
  color: var(--accent-dark);
  font-size: .75rem;
  font-weight: 800;
  letter-spacing: .12em;
  text-transform: uppercase;
}
.offline-badge {
  display: inline-flex;
  align-items: center;
  padding: .35rem .65rem;
  border: 1px solid #cfc2f2;
  border-radius: 999px;
  color: var(--accent-dark);
  background: rgba(255, 255, 255, .72);
  font-size: .78rem;
  font-weight: 700;
}
h1, h2, h3 {
  margin-top: 0;
  line-height: 1.2;
  letter-spacing: -.025em;
}
h1 {
  max-width: 850px;
  margin: .7rem 0 0;
  font-size: clamp(1.9rem, 4vw, 2.6rem);
}
h2 { margin-bottom: 1rem; font-size: clamp(1.35rem, 3vw, 2rem); }
h3 { margin: 1.5rem 0 .7rem; font-size: 1.08rem; }
main { display: grid; gap: 1.25rem; margin-top: 1.25rem; }
.meta,
.notice,
.report-section {
  border: 1px solid var(--line);
  border-radius: 16px;
  background: var(--surface);
  box-shadow: 0 8px 26px rgba(37, 27, 55, .045);
}
.summary-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 1px;
  overflow: hidden;
  padding: 0;
  background: var(--line);
}
.meta-item {
  min-width: 0;
  padding: 1rem 1.1rem;
  background: var(--surface);
}
.meta-item span {
  display: block;
  margin-bottom: .28rem;
  color: var(--muted);
  font-size: .76rem;
  font-weight: 700;
  letter-spacing: .04em;
  text-transform: uppercase;
}
.meta-item strong {
  display: block;
  overflow-wrap: anywhere;
  font-size: .98rem;
  font-variant-numeric: tabular-nums;
}
.report-section { padding: clamp(1rem, 3vw, 1.55rem); }
.section-heading {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 1rem;
  margin-bottom: .9rem;
}
.section-heading h2 { margin-bottom: 0; }
.notice {
  padding: 1rem 1.15rem;
  border-left: 5px solid var(--accent);
}
.message-list { display: grid; gap: .75rem; }
.message {
  padding: 1rem 1.05rem;
  border: 1px solid var(--line);
  border-radius: 13px;
  background: var(--surface);
  box-shadow: 0 5px 18px rgba(37, 27, 55, .035);
}
.message.match { border-left: 5px solid var(--accent); }
.message-head {
  display: flex;
  flex-wrap: wrap;
  gap: .25rem .45rem;
  align-items: baseline;
  margin-bottom: .48rem;
  color: var(--muted);
  font-size: .8rem;
}
.message-head strong { color: var(--ink); font-size: .86rem; }
.body {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  word-break: break-word;
}
.table-wrap {
  width: 100%;
  margin: .8rem 0 1.2rem;
  overflow-x: auto;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: var(--surface);
  -webkit-overflow-scrolling: touch;
}
table {
  width: 100%;
  min-width: 680px;
  border-collapse: collapse;
  background: var(--surface);
  font-size: .88rem;
  font-variant-numeric: tabular-nums;
}
th,
td {
  padding: .68rem .75rem;
  border-bottom: 1px solid var(--line);
  text-align: left;
  vertical-align: top;
}
th {
  color: #4d4658;
  background: var(--surface-soft);
  font-size: .75rem;
  font-weight: 800;
  letter-spacing: .025em;
  text-transform: uppercase;
  white-space: nowrap;
}
tbody tr:nth-child(even) { background: #fbfafd; }
tbody tr:last-child td { border-bottom: 0; }
.positive { color: var(--positive); }
.neutral { color: var(--neutral); }
.negative { color: var(--negative); }
.report-footer {
  margin-top: 1.4rem;
  padding: .3rem .2rem;
  color: var(--muted);
  text-align: center;
}
.small { margin: 0; font-size: .82rem; }
@media (max-width: 720px) {
  .report-shell { width: min(100% - 1rem, 1180px); padding-top: .5rem; }
  .report-header { border-radius: 16px; }
  .summary-grid { grid-template-columns: 1fr; }
  .report-section { border-radius: 13px; }
  .message-head { display: block; }
  .message-head span { display: inline; }
  table { font-size: .82rem; }
}
@media print {
  @page { margin: 12mm; }
  :root {
    --canvas: #ffffff;
    --surface: #ffffff;
    --surface-soft: #f3f3f3;
    --ink: #000000;
    --muted: #444444;
    --line: #bcbcbc;
  }
  html,
  body { background: #ffffff; }
  body { font-size: 10pt; }
  .report-shell { width: 100%; margin: 0; padding: 0; }
  .report-header,
  .meta,
  .notice,
  .report-section,
  .message { box-shadow: none; }
  .report-header {
    padding: 0 0 8mm;
    border: 0;
    border-bottom: 2px solid #555555;
    border-radius: 0;
    background: #ffffff;
  }
  h1 { font-size: 24pt; }
  main { display: block; margin-top: 7mm; }
  .meta,
  .notice { margin: 0 0 6mm; break-inside: avoid; }
  .report-section { margin: 0 0 6mm; break-inside: auto; }
  h2,
  h3 { break-after: avoid; }
  .summary-grid { box-shadow: none; }
  .message { break-inside: avoid; }
  .table-wrap { overflow: visible; border-radius: 0; }
  table { min-width: 0; font-size: 7.5pt; }
  th { white-space: normal; }
  th,
  td { padding: 3pt 4pt; }
  thead { display: table-header-group; }
  tr { break-inside: avoid; }
  .report-footer { margin-top: 6mm; }
}
"""


def _safe(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _filename(prefix: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return f"{prefix}_{stamp}.html"


def _document_start(title: str) -> str:
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; "
        "style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'\">"
        "<meta name=\"referrer\" content=\"no-referrer\">"
        f"<title>{_safe(title)}</title><style>{REPORT_CSS}</style></head><body>"
        '<div class="report-shell"><header class="report-header">'
        '<div class="brand-row"><span class="eyebrow">Message Analyzer</span>'
        '<span class="offline-badge">Offline · Local-only</span></div>'
        f"<h1>{_safe(title)}</h1></header><main>"
    )


def _document_end(footer_text: str | None = None) -> str:
    footer = (
        f'<footer class="report-footer"><p class="small">{_safe(footer_text)}</p>'
        "</footer>"
        if footer_text
        else ""
    )
    return f"</main>{footer}</div></body></html>"


def _summary_item(label: str, value: Any) -> str:
    return (
        '<div class="meta-item">'
        f"<span>{_safe(label)}</span><strong>{_safe(value)}</strong></div>"
    )


def _message_html(row, css_class: str) -> str:
    display_time = row["original_timestamp"] or row["timestamp"] or "Unknown time"
    return (
        f'<article class="message {_safe(css_class)}">'
        f'<div class="message-head"><strong>{_safe(row["sender"])}</strong>'
        f'<span>· {_safe(display_time)}</span>'
        f'<span>· {_safe(row["message_type"])}</span>'
        f'<span>· {_safe(row["source_filename"])}</span></div>'
        f'<div class="body">{_safe(row["message_text"])}</div></article>'
    )


def generate_search_report(
    connection,
    reports_dir: Path,
    filters: dict[str, Any],
    total: int,
) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    output_path = reports_dir / _filename("search_report")
    generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with output_path.open("w", encoding="utf-8", newline="\n") as output:
        output.write(_document_start("Instagram Conversation Search Report"))
        output.write(
            '<section class="meta summary-grid" aria-label="Report summary">'
            + _summary_item("Generated", generated)
            + _summary_item("Query", filters["q"] or "(none)")
            + _summary_item("Mode", filters["mode"])
            + _summary_item("Sender", filters["sender"] or "All")
            + _summary_item("Start date", filters["start_date"] or "None")
            + _summary_item("End date", filters["end_date"] or "None")
            + _summary_item("Order", filters["direction"])
            + _summary_item("Total matches", total)
            + "</section>"
        )
        if total == 0:
            output.write('<p class="notice">No messages matched your search.</p>')
        else:
            output.write(
                '<section class="report-section" '
                'aria-labelledby="matching-messages-heading">'
                '<div class="section-heading">'
                '<h2 id="matching-messages-heading">Matching messages</h2>'
                '</div><div class="message-list">'
            )
            for row in iter_search_messages(connection, filters):
                output.write(_message_html(row, "match"))
            output.write("</div></section>")
        output.write(
            _document_end(
                "This report is local, self-contained, and contains only escaped "
                "text. It makes no network requests."
            )
        )
    return output_path


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    head = "".join(
        f'<th scope="col">{_safe(header)}</th>' for header in headers
    )
    body = "".join(
        "<tr>" + "".join(f"<td>{_safe(cell)}</td>" for cell in row) + "</tr>"
        for row in rows
    )
    return (
        '<div class="table-wrap"><table><thead><tr>'
        f"{head}</tr></thead><tbody>{body}</tbody></table></div>"
    )


def generate_analysis_report(
    reports_dir: Path,
    result: dict[str, Any],
    start_date: str,
    end_date: str,
    top_n: int,
    *,
    full_conversation: bool = False,
    calculation_seconds: float | None = None,
    completed_at: str | None = None,
    from_cache: bool = False,
) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    output_path = reports_dir / _filename("analysis_report")
    generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    date_range = (
        "Full Conversation"
        if full_conversation
        else f"{start_date} to {end_date}"
    )
    calculation_time = (
        f"{calculation_seconds:.1f} seconds"
        if calculation_seconds is not None
        else "Not recorded"
    )

    with output_path.open("w", encoding="utf-8", newline="\n") as output:
        output.write(_document_start("Instagram Conversation Analysis Report"))
        output.write(
            '<section class="meta summary-grid" aria-label="Report summary">'
            + _summary_item("Generated", generated)
            + _summary_item("Date range", date_range)
            + _summary_item("Total messages", result["total_messages"])
            + _summary_item("Calculation time", calculation_time)
            + _summary_item("Completed", completed_at or "Not recorded")
            + _summary_item(
                "Loaded from analysis cache", "Yes" if from_cache else "No"
            )
            + _summary_item("Sentiment method", result["sentiment"]["method"])
            + "</section>"
        )

        output.write(
            '<section class="report-section" '
            'aria-labelledby="response-times-heading">'
            '<div class="section-heading">'
            '<h2 id="response-times-heading">Approximate response times</h2>'
            "</div>"
        )
        output.write(
            _table(
                [
                    "Sender",
                    "Responses",
                    "Median",
                    "Average",
                    "Average ≤24h",
                    "Fastest",
                    "Slowest",
                    "<5m",
                    "<30m",
                    "<1h",
                    "<6h",
                    "<24h",
                    ">24h gaps",
                ],
                [
                    [
                        row["sender"],
                        row["count"],
                        row["median"],
                        row["average"],
                        row["average_under_24h"],
                        row["fastest"],
                        row["slowest"],
                        row["under_5m"],
                        row["under_30m"],
                        row["under_1h"],
                        row["under_6h"],
                        row["under_24h"],
                        row["over_24h"],
                    ]
                    for row in result["responses"]
                ],
            )
        )
        output.write("</section>")

        output.write(
            '<section class="report-section" '
            'aria-labelledby="frequent-words-heading">'
            '<div class="section-heading">'
            f'<h2 id="frequent-words-heading">Frequent words '
            f"(top {_safe(top_n)})</h2></div>"
        )
        output.write(
            _table(
                ["Word", "Count", "Percentage"],
                [
                    [row["word"], row["count"], f"{row['percentage']:.2f}%"]
                    for row in result["words"]["overall"]
                ],
            )
        )
        for group in result["words"]["by_sender"]:
            output.write(f"<h3>{_safe(group['sender'])}</h3>")
            output.write(
                _table(
                    ["Word", "Count", "Percentage"],
                    [
                        [row["word"], row["count"], f"{row['percentage']:.2f}%"]
                        for row in group["words"]
                    ],
                )
            )
        output.write("</section>")

        output.write(
            '<section class="report-section" '
            'aria-labelledby="sentiment-heading">'
            '<div class="section-heading">'
            '<h2 id="sentiment-heading">Approximate sentiment</h2>'
            "</div>"
        )
        output.write(
            f"<p>{_safe(result['sentiment']['analyzed'])} messages analysed; "
            f"{_safe(result['sentiment']['skipped'])} skipped.</p>"
        )
        output.write(
            _table(
                ["Group", "Positive", "Neutral", "Negative", "Average score"],
                [
                    [
                        "Overall",
                        *[
                            f"{row['count']} ({row['percentage']:.2f}%)"
                            for row in result["sentiment"]["overall"]
                        ],
                        f"{result['sentiment']['average_score']:.3f}"
                        if result["sentiment"]["average_score"] is not None
                        else "—",
                    ],
                    *[
                        [
                            group["sender"],
                            *[
                                f"{row['count']} ({row['percentage']:.2f}%)"
                                for row in group["rows"]
                            ],
                            f"{group['average_score']:.3f}"
                            if group["average_score"] is not None
                            else "—",
                        ]
                        for group in result["sentiment"]["by_sender"]
                    ],
                ],
            )
        )
        output.write("<h3>Sentiment by month</h3>")
        output.write(
            _table(
                ["Month", "Positive", "Neutral", "Negative"],
                [
                    [
                        month["month"],
                        *[
                            f"{row['count']} ({row['percentage']:.2f}%)"
                            for row in month["rows"]
                        ],
                    ]
                    for month in result["sentiment"]["by_month"]
                ],
            )
        )
        output.write("</section>")

        output.write(
            '<section class="report-section" '
            'aria-labelledby="conversation-patterns-heading">'
            '<div class="section-heading">'
            '<h2 id="conversation-patterns-heading">Conversation Patterns</h2>'
            "</div>"
        )
        output.write(
            _table(
                ["Sender", "Messages", "Message share", "Runs", "Run share"],
                [
                    [
                        row["sender"],
                        row["messages"],
                        f"{row['message_percentage']:.2f}%",
                        row["runs"],
                        f"{row['run_percentage']:.2f}%",
                    ]
                    for row in result["sender_activity"]
                ],
            )
        )
        output.write("<h3>Who sent the next message after long gaps</h3>")
        output.write(
            _table(
                ["Gap", *result["senders"]],
                [
                    [
                        row["threshold"],
                        *[sender["count"] for sender in row["senders"]],
                    ]
                    for row in result["patterns"]["gap_resumers"]
                ],
            )
        )
        output.write("<h3>Monthly changes</h3>")
        output.write(
            _table(
                ["Month", "Messages", "Positive", "Negative", "Median response"],
                [
                    [
                        row["month"],
                        row["messages"],
                        f"{row['positive_percentage']:.2f}%",
                        f"{row['negative_percentage']:.2f}%",
                        row["median_response"],
                    ]
                    for row in result["patterns"]["periods"]
                ],
            )
        )
        output.write("<h3>Highest-activity days</h3>")
        output.write(
            _table(
                ["Day", "Messages"],
                [
                    [row["day"], row["messages"]]
                    for row in result["patterns"]["high_activity_days"]
                ],
            )
        )
        output.write("<h3>Longest inactive periods</h3>")
        output.write(
            _table(
                ["Duration", "Resumed at", "Next sender"],
                [
                    [row["duration"], row["resumed_at"], row["sender"]]
                    for row in result["patterns"]["inactive_gaps"]
                ],
            )
        )
        output.write("<h3>Message length and questions</h3>")
        output.write(
            _table(
                ["Sender", "Avg length", "Median length", "Questions", "Question ratio"],
                [
                    [
                        row["sender"],
                        f"{row['average']:.1f}",
                        f"{row['median']:.1f}",
                        row["questions"],
                        f"{row['question_ratio']:.2f}%",
                    ]
                    for row in result["patterns"]["lengths"]
                ],
            )
        )
        output.write("</section>")
        output.write(
            '<section class="notice"><strong>Limitations.</strong> Sentiment is '
            "automated and approximate. It may misunderstand sarcasm, jokes, context, "
            "slang, emojis, Urdu, Arabic, Roman Urdu, and other non-English text. "
            "Positive or negative labels do not establish affection, sincerity, "
            "hostility, harm, feelings, motives, honesty, compatibility, or intent. "
            "Conversation-pattern measurements describe activity in this export only "
            "and do not establish how either person felt.</section>"
        )
        output.write(_document_end())
    return output_path
