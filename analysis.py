"""Local-only descriptive analysis for imported messages."""

from __future__ import annotations

import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any


DEFAULT_STOP_WORDS = """a about after again all also am an and any are as at be because
been before being but by can could did do does doing down during each few for from
further had has have having he her here hers herself him himself his how i if in
into is it its itself just me more most my myself no nor not now of off on once only
or other our ours ourselves out over own same she should so some such than that the
their theirs them themselves then there these they this those through to too under
until up very was we were what when where which while who whom why will with would
you your yours yourself yourselves im i'm ive i've dont don't didnt didn't cant can't
wont won't thats that's theres there's"""

WORD_RE = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", re.UNICODE)
URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
PLACEHOLDER_RE = re.compile(r"^\[(?:Photo|Video|GIF|Audio message|Shared post|Shared reel|Unavailable content)\]$")

POSITIVE_FALLBACK = {
    "amazing", "awesome", "beautiful", "best", "enjoy", "excellent", "fun",
    "glad", "good", "great", "happy", "hope", "kind", "like", "love", "lovely",
    "nice", "perfect", "smile", "thanks", "thank", "wonderful", "yay", "yes",
}
NEGATIVE_FALLBACK = {
    "angry", "annoyed", "awful", "bad", "broken", "cry", "difficult", "dislike",
    "hate", "hurt", "mad", "no", "sad", "sorry", "terrible", "tired", "upset",
    "worse", "worst", "wrong",
}


class LocalSentiment:
    """Use VADER when installed, with a small offline fallback."""

    def __init__(self) -> None:
        try:
            from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

            self._analyzer = SentimentIntensityAnalyzer()
            self.method = "VADER (vaderSentiment, local)"
        except ImportError:
            self._analyzer = None
            self.method = "Compact local English word-list fallback"

    def score(self, text: str, message_type: str = "text") -> tuple[str | None, float | None]:
        if message_type in {"unavailable", "system"} or PLACEHOLDER_RE.fullmatch(text.strip()):
            return None, None
        cleaned = URL_RE.sub(" ", text).strip()
        words = [word.casefold() for word in WORD_RE.findall(cleaned)]
        if not words:
            return None, None
        if self._analyzer is not None:
            score = float(self._analyzer.polarity_scores(cleaned)["compound"])
        else:
            hits = sum(word in POSITIVE_FALLBACK for word in words) - sum(
                word in NEGATIVE_FALLBACK for word in words
            )
            score = max(-1.0, min(1.0, hits / max(1, len(words) ** 0.5)))
        if score >= 0.05:
            return "positive", score
        if score <= -0.05:
            return "negative", score
        return "neutral", score


def parse_stop_words(value: str) -> set[str]:
    return {word.casefold() for word in WORD_RE.findall(value)}


def format_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "—"
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"{seconds} sec"
    if seconds < 3600:
        return f"{seconds / 60:.1f} min"
    if seconds < 86400:
        return f"{seconds / 3600:.1f} hr"
    return f"{seconds / 86400:.1f} days"


def _duration_summary(values: list[int]) -> dict[str, Any]:
    under_24 = [value for value in values if value <= 86400]
    return {
        "count": len(values),
        "median": format_duration(statistics.median(values)) if values else "—",
        "average": format_duration(statistics.mean(values)) if values else "—",
        "average_under_24h": format_duration(statistics.mean(under_24)) if under_24 else "—",
        "fastest": format_duration(min(values)) if values else "—",
        "slowest": format_duration(max(values)) if values else "—",
        "under_5m": sum(value < 300 for value in values),
        "under_30m": sum(value < 1800 for value in values),
        "under_1h": sum(value < 3600 for value in values),
        "under_6h": sum(value < 21600 for value in values),
        "under_24h": sum(value < 86400 for value in values),
        "over_24h": sum(value > 86400 for value in values),
    }


def _word_rows(counter: Counter[str], total: int, top_n: int) -> list[dict[str, Any]]:
    return [
        {
            "word": word,
            "count": count,
            "percentage": (count / total * 100) if total else 0,
        }
        for word, count in counter.most_common(top_n)
    ]


def _sentiment_rows(counter: Counter[str]) -> list[dict[str, Any]]:
    total = sum(counter.values())
    return [
        {
            "label": label,
            "count": counter.get(label, 0),
            "percentage": counter.get(label, 0) / total * 100 if total else 0,
        }
        for label in ("positive", "neutral", "negative")
    ]


def _median(values: list[int]) -> float:
    return float(statistics.median(values)) if values else 0.0


def analyze_messages(
    connection,
    start_unix: int | None,
    end_unix: int | None,
    stop_words: set[str],
    top_n: int,
) -> dict[str, Any]:
    where = ["1=1"]
    params: list[Any] = []
    if start_unix is not None:
        where.append("timestamp_unix >= ?")
        params.append(start_unix)
    if end_unix is not None:
        where.append("timestamp_unix < ?")
        params.append(end_unix)

    sql = f"""
        SELECT sender, timestamp_unix, timestamp, message_text, message_type,
               sentiment_label, sentiment_score
        FROM messages
        WHERE {' AND '.join(where)}
        ORDER BY conversation_position ASC
    """

    total_messages = 0
    sender_counts: Counter[str] = Counter()
    run_counts: Counter[str] = Counter()
    response_times: dict[str, list[int]] = defaultdict(list)
    response_by_month: dict[str, list[int]] = defaultdict(list)
    gap_resumers = {
        "6 hours": Counter(),
        "24 hours": Counter(),
        "3 days": Counter(),
    }
    lengths: dict[str, list[int]] = defaultdict(list)
    questions: Counter[str] = Counter()
    overall_words: Counter[str] = Counter()
    sender_words: dict[str, Counter[str]] = defaultdict(Counter)
    word_totals: Counter[str] = Counter()
    sentiment_overall: Counter[str] = Counter()
    sentiment_by_sender: dict[str, Counter[str]] = defaultdict(Counter)
    sentiment_score_sums: Counter[str] = Counter()
    sentiment_score_counts: Counter[str] = Counter()
    sentiment_by_month: dict[str, Counter[str]] = defaultdict(Counter)
    monthly_messages: Counter[str] = Counter()
    daily_messages: Counter[str] = Counter()
    skipped_sentiment = 0
    inactive_gaps: list[tuple[int, int, str]] = []

    previous_sender: str | None = None
    previous_timestamp: int | None = None
    previous_run_last_timestamp: int | None = None

    for row in connection.execute(sql, params):
        total_messages += 1
        sender = row["sender"]
        timestamp_unix = row["timestamp_unix"]
        text = row["message_text"] or ""
        message_type = row["message_type"]
        sender_counts[sender] += 1

        if timestamp_unix is not None:
            stamp = datetime.fromtimestamp(timestamp_unix, timezone.utc)
            month = stamp.strftime("%Y-%m")
            day = stamp.strftime("%Y-%m-%d")
            monthly_messages[month] += 1
            daily_messages[day] += 1
            if previous_timestamp is not None:
                gap = timestamp_unix - previous_timestamp
                if gap >= 21600:
                    gap_resumers["6 hours"][sender] += 1
                if gap >= 86400:
                    gap_resumers["24 hours"][sender] += 1
                    inactive_gaps.append((gap, timestamp_unix, sender))
                if gap >= 259200:
                    gap_resumers["3 days"][sender] += 1
            previous_timestamp = timestamp_unix

        if previous_sender is None or sender != previous_sender:
            run_counts[sender] += 1
            if (
                previous_sender is not None
                and timestamp_unix is not None
                and previous_run_last_timestamp is not None
                and timestamp_unix >= previous_run_last_timestamp
            ):
                response = timestamp_unix - previous_run_last_timestamp
                response_times[sender].append(response)
                response_by_month[
                    datetime.fromtimestamp(timestamp_unix, timezone.utc).strftime("%Y-%m")
                ].append(response)
            previous_sender = sender
        if timestamp_unix is not None:
            previous_run_last_timestamp = timestamp_unix

        if message_type not in {"unavailable", "system"} and not PLACEHOLDER_RE.fullmatch(text):
            lengths[sender].append(len(text))
            if "?" in text:
                questions[sender] += 1
            clean_text = URL_RE.sub(" ", text)
            words = [
                word.casefold().replace("’", "'")
                for word in WORD_RE.findall(clean_text)
                if word.casefold() not in stop_words
            ]
            overall_words.update(words)
            sender_words[sender].update(words)
            word_totals["overall"] += len(words)
            word_totals[sender] += len(words)

        label = row["sentiment_label"]
        if label:
            sentiment_overall[label] += 1
            sentiment_by_sender[sender][label] += 1
            score = row["sentiment_score"]
            if score is not None:
                sentiment_score_sums["overall"] += float(score)
                sentiment_score_sums[sender] += float(score)
                sentiment_score_counts["overall"] += 1
                sentiment_score_counts[sender] += 1
            if timestamp_unix is not None:
                sentiment_by_month[
                    datetime.fromtimestamp(timestamp_unix, timezone.utc).strftime("%Y-%m")
                ][label] += 1
        else:
            skipped_sentiment += 1

    senders = sorted(sender_counts)
    total_runs = sum(run_counts.values())
    analyzed_sentiment = sum(sentiment_overall.values())

    sender_activity = [
        {
            "sender": sender,
            "messages": sender_counts[sender],
            "message_percentage": sender_counts[sender] / total_messages * 100
            if total_messages
            else 0,
            "runs": run_counts[sender],
            "run_percentage": run_counts[sender] / total_runs * 100 if total_runs else 0,
        }
        for sender in senders
    ]
    response_rows = [
        {"sender": sender, **_duration_summary(response_times[sender])}
        for sender in senders
    ]
    sender_word_groups = [
        {
            "sender": sender,
            "total": word_totals[sender],
            "words": _word_rows(sender_words[sender], word_totals[sender], top_n),
        }
        for sender in senders
    ]
    sender_sentiment = [
        {
            "sender": sender,
            "rows": _sentiment_rows(sentiment_by_sender[sender]),
            "average_score": sentiment_score_sums[sender] / sentiment_score_counts[sender]
            if sentiment_score_counts[sender]
            else None,
        }
        for sender in senders
    ]
    length_rows = [
        {
            "sender": sender,
            "average": statistics.mean(lengths[sender]) if lengths[sender] else 0,
            "median": _median(lengths[sender]),
            "questions": questions[sender],
            "question_ratio": questions[sender] / sender_counts[sender] * 100
            if sender_counts[sender]
            else 0,
        }
        for sender in senders
    ]
    gap_rows = [
        {
            "threshold": threshold,
            "senders": [
                {"sender": sender, "count": counts[sender]} for sender in senders
            ],
        }
        for threshold, counts in gap_resumers.items()
    ]
    period_rows = []
    all_months = sorted(set(monthly_messages) | set(sentiment_by_month))[-24:]
    for month in all_months:
        labels = sentiment_by_month[month]
        response_values = response_by_month[month]
        period_rows.append(
            {
                "month": month,
                "messages": monthly_messages[month],
                "positive_percentage": labels["positive"] / sum(labels.values()) * 100
                if labels
                else 0,
                "negative_percentage": labels["negative"] / sum(labels.values()) * 100
                if labels
                else 0,
                "median_response": format_duration(statistics.median(response_values))
                if response_values
                else "—",
            }
        )

    return {
        "total_messages": total_messages,
        "senders": senders,
        "sender_activity": sender_activity,
        "responses": response_rows,
        "words": {
            "total": word_totals["overall"],
            "overall": _word_rows(overall_words, word_totals["overall"], top_n),
            "by_sender": sender_word_groups,
        },
        "sentiment": {
            "method": LocalSentiment().method,
            "analyzed": analyzed_sentiment,
            "skipped": skipped_sentiment,
            "overall": _sentiment_rows(sentiment_overall),
            "average_score": sentiment_score_sums["overall"]
            / sentiment_score_counts["overall"]
            if sentiment_score_counts["overall"]
            else None,
            "by_sender": sender_sentiment,
            "by_month": [
                {"month": month, "rows": _sentiment_rows(sentiment_by_month[month])}
                for month in sorted(sentiment_by_month)
            ],
        },
        "patterns": {
            "lengths": length_rows,
            "gap_resumers": gap_rows,
            "periods": period_rows,
            "high_activity_days": [
                {"day": day, "messages": count}
                for day, count in daily_messages.most_common(10)
            ],
            "inactive_gaps": [
                {
                    "duration": format_duration(gap),
                    "resumed_at": datetime.fromtimestamp(
                        stamp, timezone.utc
                    ).strftime("%Y-%m-%d %H:%M"),
                    "sender": sender,
                }
                for gap, stamp, sender in sorted(inactive_gaps, reverse=True)[:10]
            ],
        },
    }
