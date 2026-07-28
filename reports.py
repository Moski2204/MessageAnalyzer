"""Generate escaped, self-contained offline HTML reports."""

from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path
from typing import Any

from database import iter_search_messages


REPORT_CSS = """
body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;line-height:1.5;
max-width:1050px;margin:0 auto;padding:2rem;color:#202124;background:#f7f7f8}
h1,h2,h3{line-height:1.2}.meta,.notice{background:#fff;border:1px solid #ddd;
border-radius:10px;padding:1rem;margin:1rem 0}.message{background:#fff;border:1px
solid #ddd;border-radius:10px;padding:.8rem 1rem;margin:.55rem 0}.match{
border-left:5px solid #6c4ce3}
.message-head{font-size:.85rem;color:#666;margin-bottom:.35rem}.body{
white-space:pre-wrap;overflow-wrap:anywhere}table{width:100%;border-collapse:
collapse;background:#fff;margin:1rem 0}th,td{text-align:left;padding:.55rem;
border:1px solid #ddd;vertical-align:top}th{background:#eee}.small{font-size:
.85rem;color:#666}.positive{color:#177245}.negative{color:#a12626}
"""


def _safe(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _filename(prefix: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return f"{prefix}_{stamp}.html"


def _document_start(title: str) -> str:
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>{_safe(title)}</title><style>{REPORT_CSS}</style></head><body>"
        f"<h1>{_safe(title)}</h1>"
    )


def _message_html(row, css_class: str) -> str:
    display_time = row["original_timestamp"] or row["timestamp"] or "Unknown time"
    return (
        f'<article class="message {_safe(css_class)}">'
        f'<div class="message-head"><strong>{_safe(row["sender"])}</strong> · '
        f'{_safe(display_time)} · {_safe(row["message_type"])} · '
        f'{_safe(row["source_filename"])}</div>'
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
            '<section class="meta">'
            f"<div><strong>Generated:</strong> {_safe(generated)}</div>"
            f"<div><strong>Query:</strong> {_safe(filters['q'] or '(none)')}</div>"
            f"<div><strong>Mode:</strong> {_safe(filters['mode'])}</div>"
            f"<div><strong>Sender:</strong> {_safe(filters['sender'] or 'All')}</div>"
            f"<div><strong>Start date:</strong> {_safe(filters['start_date'] or 'None')}</div>"
            f"<div><strong>End date:</strong> {_safe(filters['end_date'] or 'None')}</div>"
            f"<div><strong>Order:</strong> {_safe(filters['direction'])}</div>"
            f"<div><strong>Total matches:</strong> {total}</div></section>"
        )
        if total == 0:
            output.write('<p class="notice">No messages matched your search.</p>')
        for row in iter_search_messages(connection, filters):
            output.write("<section>")
            output.write(_message_html(row, "match"))
            output.write("</section>")
        output.write(
            '<p class="small">This report is local, self-contained, and contains '
            "only escaped text. It makes no network requests.</p></body></html>"
        )
    return output_path


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    head = "".join(f"<th>{_safe(header)}</th>" for header in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{_safe(cell)}</td>" for cell in row) + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def generate_analysis_report(
    reports_dir: Path,
    result: dict[str, Any],
    start_date: str,
    end_date: str,
    top_n: int,
) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    output_path = reports_dir / _filename("analysis_report")
    generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with output_path.open("w", encoding="utf-8", newline="\n") as output:
        output.write(_document_start("Instagram Conversation Analysis Report"))
        output.write(
            '<section class="meta">'
            f"<div><strong>Generated:</strong> {_safe(generated)}</div>"
            f"<div><strong>Date range:</strong> {_safe(start_date or 'Beginning')} "
            f"to {_safe(end_date or 'End')}</div>"
            f"<div><strong>Total messages:</strong> {result['total_messages']}</div>"
            f"<div><strong>Sentiment method:</strong> "
            f"{_safe(result['sentiment']['method'])}</div></section>"
        )

        output.write("<h2>Approximate response times</h2>")
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

        output.write(f"<h2>Frequent words (top {top_n})</h2>")
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

        output.write("<h2>Approximate sentiment</h2>")
        output.write(
            f"<p>{result['sentiment']['analyzed']} messages analysed; "
            f"{result['sentiment']['skipped']} skipped.</p>"
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

        output.write("<h2>Conversation Patterns</h2>")
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
        output.write(
            '<section class="notice"><strong>Limitations.</strong> Sentiment is '
            "automated and approximate. It may misunderstand sarcasm, jokes, context, "
            "slang, emojis, Urdu, Arabic, Roman Urdu, and other non-English text. "
            "Positive or negative labels do not establish affection, sincerity, "
            "hostility, harm, feelings, motives, honesty, compatibility, or intent. "
            "Conversation-pattern measurements describe activity in this export only "
            "and do not establish how either person felt.</section></body></html>"
        )
    return output_path
