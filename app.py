"""Private localhost Flask interface for one Instagram conversation."""

from __future__ import annotations

import hashlib
import logging
import math
import re
import secrets
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    url_for,
)
from markupsafe import Markup, escape

from analysis import DEFAULT_STOP_WORDS, WORD_RE, analyze_messages, parse_stop_words
from database import (
    clean_search_filters,
    connect_readonly,
    conversation_page,
    database_ready,
    date_to_unix,
    get_database_summary,
    get_senders,
    page_for_message,
    search_messages,
)
from reports import generate_analysis_report, generate_search_report


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
PHOTO_DIR = DATA_DIR / "photos"
DATABASE_PATH = BASE_DIR / "instance" / "messages.db"
REPORTS_DIR = BASE_DIR / "reports"
DATABASE_UNAVAILABLE_MESSAGE = (
    "The local message database could not be found. Restore "
    "instance/messages.db from your backup before using the application."
)

app = Flask(__name__)
app.config.update(
    SECRET_KEY=secrets.token_hex(32),
    MAX_CONTENT_LENGTH=1_000_000,
)

# Access logs include query strings. Disable them so private search terms are
# never echoed to the terminal.
logging.getLogger("werkzeug").setLevel(logging.ERROR)


def _watched_files() -> list[Path]:
    files = [
        *BASE_DIR.glob("*.py"),
        *(BASE_DIR / "templates").glob("*.html"),
        *(BASE_DIR / "static").glob("*"),
    ]
    return sorted(path for path in files if path.is_file())


def _development_version() -> str:
    fingerprint = hashlib.sha256()
    for path in _watched_files():
        try:
            stat = path.stat()
        except OSError:
            continue
        fingerprint.update(path.relative_to(BASE_DIR).as_posix().encode("utf-8"))
        fingerprint.update(f":{stat.st_mtime_ns}:{stat.st_size}".encode("ascii"))
    return fingerprint.hexdigest()


def _ready_or_redirect():
    if not database_ready(DATABASE_PATH):
        return redirect(url_for("index"))
    return None


def _image_mimetype(path: Path) -> str | None:
    try:
        with path.open("rb") as photo:
            header = photo.read(16)
    except OSError:
        return None
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if (
        len(header) >= 12
        and header.startswith(b"RIFF")
        and header[8:12] == b"WEBP"
    ):
        return "image/webp"
    return None


def _safe_photo(
    filename: str | None,
) -> tuple[Path, str, str] | None:
    if not filename or "\\" in filename or "\x00" in filename:
        return None
    posix_path = PurePosixPath(filename)
    windows_path = PureWindowsPath(filename)
    if (
        posix_path.is_absolute()
        or windows_path.is_absolute()
        or bool(windows_path.drive)
        or any(part in {"", ".", ".."} for part in filename.split("/"))
    ):
        return None

    try:
        photo_root = PHOTO_DIR.resolve()
        candidate = (photo_root / Path(*posix_path.parts)).resolve()
        relative = candidate.relative_to(photo_root).as_posix()
    except (OSError, ValueError):
        return None
    if not candidate.is_file():
        return None
    mimetype = _image_mimetype(candidate)
    return (candidate, relative, mimetype) if mimetype else None


def _messages_with_photo_availability(rows) -> list[dict[str, Any]]:
    """Check photos only for the messages selected by the current page query."""
    messages: list[dict[str, Any]] = []
    for row in rows:
        message = dict(row)
        photo = (
            _safe_photo(message.get("attachment_path"))
            if message.get("message_type") == "photo"
            else None
        )
        message["attachment_path"] = photo[1] if photo else None
        message["photo_available"] = photo is not None
        messages.append(message)
    return messages


def _search_urls(filters: dict[str, Any], total_pages: int) -> dict[str, str | None]:
    base = dict(filters)
    current = filters["page"]
    urls: dict[str, str | None] = {
        "previous": url_for("search_report", **{**base, "page": current - 1})
        if current > 1
        else None,
        "next": url_for("search_report", **{**base, "page": current + 1})
        if current < total_pages
        else None,
        "oldest": url_for(
            "search_report", **{**base, "direction": "asc", "page": 1}
        ),
        "newest": url_for(
            "search_report", **{**base, "direction": "desc", "page": 1}
        ),
        "download": url_for("download_search_report", **base),
    }
    return urls


def _highlight(text: str, query: str, mode: str) -> Markup:
    if not query:
        return Markup(escape(text))
    terms = [query] if mode in {"contains", "phrase"} else WORD_RE.findall(query)
    terms = sorted({term for term in terms if term}, key=len, reverse=True)
    if not terms:
        return Markup(escape(text))
    pattern = re.compile("|".join(re.escape(term) for term in terms), re.IGNORECASE)
    pieces: list[Markup] = []
    position = 0
    for match in pattern.finditer(text):
        pieces.append(escape(text[position : match.start()]))
        pieces.append(Markup("<mark>") + escape(match.group(0)) + Markup("</mark>"))
        position = match.end()
    pieces.append(escape(text[position:]))
    return Markup("").join(pieces)


app.jinja_env.filters["highlight"] = _highlight


@app.route("/")
def index():
    return render_template(
        "index.html",
        summary=get_database_summary(DATABASE_PATH),
        database_unavailable_message=DATABASE_UNAVAILABLE_MESSAGE,
    )


@app.get("/__dev/version")
def development_version():
    response = jsonify(version=_development_version())
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


@app.route("/search")
def search():
    blocked = _ready_or_redirect()
    if blocked:
        return blocked
    with connect_readonly(DATABASE_PATH) as connection:
        senders = get_senders(connection)
    return render_template(
        "search.html",
        filters=clean_search_filters(request.args),
        senders=senders,
    )


@app.route("/search/report")
def search_report():
    blocked = _ready_or_redirect()
    if blocked:
        return blocked
    filters = clean_search_filters(request.args)
    with connect_readonly(DATABASE_PATH) as connection:
        senders = get_senders(connection)
        total, rows = search_messages(connection, filters)
    rows = _messages_with_photo_availability(rows)
    total_pages = max(1, math.ceil(total / filters["per_page"]))
    if filters["page"] > total_pages:
        filters["page"] = total_pages
        return redirect(url_for("search_report", **filters))
    return render_template(
        "search_report.html",
        filters=filters,
        senders=senders,
        results=rows,
        total=total,
        total_pages=total_pages,
        urls=_search_urls(filters, total_pages),
    )


@app.route("/search/report/download")
def download_search_report():
    blocked = _ready_or_redirect()
    if blocked:
        return blocked
    filters = clean_search_filters(request.args)
    with connect_readonly(DATABASE_PATH) as connection:
        total, _ = search_messages(connection, {**filters, "page": 1})
        output_path = generate_search_report(connection, REPORTS_DIR, filters, total)
    return send_file(
        output_path,
        as_attachment=True,
        download_name=output_path.name,
        mimetype="text/html",
    )


def _conversation_values() -> dict[str, Any]:
    direction = "desc" if request.args.get("direction") == "desc" else "asc"
    sender = request.args.get("sender", "").strip()[:200]
    try:
        per_page = int(request.args.get("per_page", 100))
    except ValueError:
        per_page = 100
    if per_page not in {50, 100, 250}:
        per_page = 100
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    try:
        target = int(request.args.get("target", 0))
    except ValueError:
        target = 0
    return {
        "direction": direction,
        "sender": sender,
        "per_page": per_page,
        "page": page,
        "target": target,
    }


@app.route("/conversation")
def conversation():
    blocked = _ready_or_redirect()
    if blocked:
        return blocked
    values = _conversation_values()
    with connect_readonly(DATABASE_PATH) as connection:
        senders = get_senders(connection)
        if values["target"] and "page" not in request.args:
            values["page"] = page_for_message(
                connection,
                values["target"],
                values["direction"],
                values["sender"],
                values["per_page"],
            )
        total, rows = conversation_page(
            connection,
            values["direction"],
            values["sender"],
            values["per_page"],
            values["page"],
        )
    rows = _messages_with_photo_availability(rows)
    total_pages = max(1, math.ceil(total / values["per_page"]))
    if values["page"] > total_pages:
        values["page"] = total_pages
        return redirect(url_for("conversation", **values))
    previous_url = (
        url_for("conversation", **{**values, "page": values["page"] - 1})
        if values["page"] > 1
        else None
    )
    next_url = (
        url_for("conversation", **{**values, "page": values["page"] + 1})
        if values["page"] < total_pages
        else None
    )
    return render_template(
        "conversation.html",
        values=values,
        senders=senders,
        messages=rows,
        total=total,
        total_pages=total_pages,
        previous_url=previous_url,
        next_url=next_url,
    )


def _analysis_inputs() -> tuple[str, str, int, str]:
    start_date = request.values.get("start_date", "").strip()
    end_date = request.values.get("end_date", "").strip()
    try:
        top_n = int(request.values.get("top_n", 20))
    except ValueError:
        top_n = 20
    if top_n not in {20, 50, 100}:
        top_n = 20
    stop_words = request.values.get("stop_words")
    if stop_words is None:
        stop_words = DEFAULT_STOP_WORDS
    return start_date, end_date, top_n, stop_words[:10000]


@app.route("/analysis", methods=["GET", "POST"])
def analysis_page():
    blocked = _ready_or_redirect()
    if blocked:
        return blocked
    start_date, end_date, top_n, stop_words = _analysis_inputs()
    with connect_readonly(DATABASE_PATH) as connection:
        result = analyze_messages(
            connection,
            date_to_unix(start_date),
            date_to_unix(end_date, end=True),
            parse_stop_words(stop_words),
            top_n,
        )
    if not result["total_messages"]:
        flash("No messages were available for this analysis.", "warning")
    return render_template(
        "analysis.html",
        result=result,
        start_date=start_date,
        end_date=end_date,
        top_n=top_n,
        stop_words=stop_words,
    )


@app.post("/analysis/download")
def download_analysis_report():
    blocked = _ready_or_redirect()
    if blocked:
        return blocked
    start_date, end_date, top_n, stop_words = _analysis_inputs()
    with connect_readonly(DATABASE_PATH) as connection:
        result = analyze_messages(
            connection,
            date_to_unix(start_date),
            date_to_unix(end_date, end=True),
            parse_stop_words(stop_words),
            top_n,
        )
    output_path = generate_analysis_report(
        REPORTS_DIR, result, start_date, end_date, top_n
    )
    return send_file(
        output_path,
        as_attachment=True,
        download_name=output_path.name,
        mimetype="text/html",
    )


@app.get("/photos/<path:filename>")
def photo(filename: str):
    safe_photo = _safe_photo(filename)
    if safe_photo is None:
        abort(404)
    _, relative, mimetype = safe_photo
    response = send_from_directory(
        PHOTO_DIR,
        relative,
        as_attachment=False,
        conditional=True,
        mimetype=mimetype,
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.cache_control.private = True
    return response


def main() -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    app.run(
        host="127.0.0.1",
        port=5000,
        debug=False,
        use_reloader=False,
    )


if __name__ == "__main__":
    main()
