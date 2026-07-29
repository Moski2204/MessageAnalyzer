"""Private localhost Flask interface for one Instagram conversation."""

from __future__ import annotations

import hashlib
import logging
import math
import re
import secrets
import sqlite3
import threading
from datetime import datetime
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

from analysis import DEFAULT_STOP_WORDS, WORD_RE
from analysis_jobs import (
    AnalysisJobManager,
    AnalysisSettings,
    valid_job_id,
)
from database import (
    clean_search_filters,
    connect_readonly,
    conversation_page,
    database_ready,
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
ANALYSIS_CACHE_PATH = BASE_DIR / "instance" / "analysis_cache.db"
REPORTS_DIR = BASE_DIR / "reports"
LOCAL_APP_URL = "http://127.0.0.1:5000"
DATABASE_UNAVAILABLE_MESSAGE = (
    "The local message database could not be found. Restore "
    "instance/messages.db from your backup before using the application."
)

app = Flask(__name__)
app.config.update(
    SECRET_KEY=secrets.token_hex(32),
    ANALYSIS_ACTION_TOKEN=secrets.token_urlsafe(32),
    MAX_CONTENT_LENGTH=1_000_000,
)

_analysis_managers: dict[tuple[str, str], AnalysisJobManager] = {}
_analysis_managers_lock = threading.Lock()

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


def _analysis_manager() -> AnalysisJobManager:
    key = (
        str(DATABASE_PATH.resolve()),
        str(ANALYSIS_CACHE_PATH.resolve()),
    )
    with _analysis_managers_lock:
        manager = _analysis_managers.get(key)
        if manager is None:
            manager = AnalysisJobManager(DATABASE_PATH, ANALYSIS_CACHE_PATH)
            _analysis_managers[key] = manager
        return manager


def _analysis_action_token() -> str:
    return str(app.config["ANALYSIS_ACTION_TOKEN"])


def _require_analysis_action_token() -> None:
    supplied = request.form.get("action_token", "")
    if not secrets.compare_digest(supplied, _analysis_action_token()):
        abort(400)


def _analysis_form_values() -> dict[str, Any]:
    try:
        top_n = int(request.form.get("top_n", 20))
    except (TypeError, ValueError):
        top_n = 20
    if top_n not in {20, 50, 100}:
        top_n = 20
    stop_words = request.form.get("stop_words")
    if stop_words is None:
        stop_words = DEFAULT_STOP_WORDS
    return {
        "start_date": request.form.get("start_date", "").strip()[:10],
        "end_date": request.form.get("end_date", "").strip()[:10],
        "full_conversation": request.form.get("full_conversation") == "1",
        "top_n": top_n,
        "stop_words": stop_words[:10_000],
    }


def _validated_analysis_settings(
    values: dict[str, Any],
) -> tuple[AnalysisSettings | None, str | None]:
    if values["full_conversation"]:
        return (
            AnalysisSettings(
                start_date="",
                end_date="",
                full_conversation=True,
                top_n=values["top_n"],
                stop_words=values["stop_words"],
            ),
            None,
        )
    if not values["start_date"] or not values["end_date"]:
        return (
            None,
            "Choose both a start date and an end date, or select Full Conversation.",
        )
    try:
        start = datetime.strptime(values["start_date"], "%Y-%m-%d")
        end = datetime.strptime(values["end_date"], "%Y-%m-%d")
    except ValueError:
        return None, "Enter a valid start date and end date."
    if start > end:
        return None, "The start date cannot be after the end date."
    return AnalysisSettings(**values), None


def _cache_size_label(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} bytes"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


def _render_analysis_landing(
    values: dict[str, Any] | None = None,
    status_code: int = 200,
):
    manager = _analysis_manager()
    if values is None:
        values = {
            "start_date": "",
            "end_date": "",
            "full_conversation": False,
            "top_n": 20,
            "stop_words": DEFAULT_STOP_WORDS,
        }
    response = render_template(
        "analysis.html",
        values=values,
        recent_jobs=manager.recent_completed(),
        active_job=manager.active_job(),
        cache_size=_cache_size_label(manager.cache.size_bytes()),
        cache_recovered=manager.cache.recovered_corrupt_cache,
        action_token=_analysis_action_token(),
    )
    return response, status_code


@app.get("/analysis")
def analysis_page():
    blocked = _ready_or_redirect()
    if blocked:
        return blocked
    return _render_analysis_landing()


@app.post("/analysis/start")
def start_analysis():
    blocked = _ready_or_redirect()
    if blocked:
        return blocked
    _require_analysis_action_token()
    values = _analysis_form_values()
    settings, validation_error = _validated_analysis_settings(values)
    if validation_error:
        flash(validation_error, "warning")
        return _render_analysis_landing(values, 400)
    assert settings is not None
    try:
        submission = _analysis_manager().submit(settings)
    except (OSError, sqlite3.Error):
        flash(
            "Analysis could not start because the local databases were unavailable.",
            "error",
        )
        return _render_analysis_landing(values, 503)
    job = submission["job"]
    if submission["outcome"] == "empty":
        flash("No messages exist in the selected date range.", "warning")
        return _render_analysis_landing(values, 400)
    if submission["outcome"] == "cached":
        return redirect(
            url_for("analysis_result", job_id=job["job_id"], cached=1),
            code=303,
        )
    if submission["outcome"] == "busy":
        flash("Another analysis is already running.", "warning")
    return redirect(
        url_for("analysis_job_page", job_id=job["job_id"]),
        code=303,
    )


@app.get("/analysis/jobs/<job_id>")
def analysis_job_page(job_id: str):
    blocked = _ready_or_redirect()
    if blocked:
        return blocked
    if not valid_job_id(job_id):
        abort(404)
    job = _analysis_manager().get_job(job_id)
    if job is None:
        abort(404)
    if job["status"] == "complete":
        return redirect(
            url_for("analysis_result", job_id=job_id, cached=0),
            code=303,
        )
    return render_template(
        "analysis_status.html",
        job=job,
        status_url=url_for("analysis_job_status", job_id=job_id),
    )


@app.get("/analysis/jobs/<job_id>/status")
def analysis_job_status(job_id: str):
    if not valid_job_id(job_id):
        abort(404)
    job = _analysis_manager().get_job(job_id)
    if job is None:
        abort(404)
    payload = {
        "status": job["status"],
        "stage": job["stage"],
        "percentage": job["progress"],
        "processed_messages": job["processed_messages"],
        "total_messages": job["total_messages"],
        "elapsed_seconds": job["elapsed_seconds"],
        "error": job["error"],
        "result_url": (
            url_for("analysis_result", job_id=job_id, cached=0)
            if job["status"] == "complete"
            else None
        ),
    }
    response = jsonify(payload)
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


@app.get("/analysis/results/<job_id>")
def analysis_result(job_id: str):
    blocked = _ready_or_redirect()
    if blocked:
        return blocked
    if not valid_job_id(job_id):
        abort(404)
    manager = _analysis_manager()
    job = manager.get_job(job_id)
    if job is None:
        abort(404)
    if job["status"] != "complete" or job["result"] is None:
        return redirect(
            url_for("analysis_job_page", job_id=job_id),
            code=303,
        )
    if not manager.job_is_current(job):
        flash(
            "This cached result is no longer valid because messages.db changed. "
            "Run the analysis again.",
            "warning",
        )
        return _render_analysis_landing(job["settings"], 409)
    return render_template(
        "analysis_result.html",
        job=job,
        result=job["result"],
        settings=AnalysisSettings.from_dict(job["settings"]),
        cache_hit=request.args.get("cached") == "1",
        action_token=_analysis_action_token(),
    )


@app.post("/analysis/results/<job_id>/download")
def download_analysis_report(job_id: str):
    blocked = _ready_or_redirect()
    if blocked:
        return blocked
    _require_analysis_action_token()
    if not valid_job_id(job_id):
        abort(404)
    manager = _analysis_manager()
    job = manager.get_job(job_id)
    if job is None or job["status"] != "complete" or job["result"] is None:
        abort(404)
    if not manager.job_is_current(job):
        flash(
            "The cached result cannot be downloaded because messages.db changed.",
            "warning",
        )
        return redirect(url_for("analysis_page"), code=303)
    settings = AnalysisSettings.from_dict(job["settings"])
    output_path = generate_analysis_report(
        REPORTS_DIR,
        job["result"],
        settings.start_date,
        settings.end_date,
        settings.top_n,
        full_conversation=settings.full_conversation,
        calculation_seconds=job["elapsed_seconds"],
        completed_at=job["completed_at"],
        from_cache=True,
    )
    return send_file(
        output_path,
        as_attachment=True,
        download_name=output_path.name,
        mimetype="text/html",
    )


@app.post("/analysis/results/<job_id>/recalculate")
def recalculate_analysis(job_id: str):
    blocked = _ready_or_redirect()
    if blocked:
        return blocked
    _require_analysis_action_token()
    if not valid_job_id(job_id):
        abort(404)
    manager = _analysis_manager()
    previous = manager.get_job(job_id)
    if previous is None or previous["status"] != "complete":
        abort(404)
    settings = AnalysisSettings.from_dict(previous["settings"])
    try:
        submission = manager.submit(settings, force=True)
    except (OSError, sqlite3.Error):
        flash(
            "Analysis could not restart because the local databases were unavailable.",
            "error",
        )
        return redirect(url_for("analysis_result", job_id=job_id), code=303)
    if submission["outcome"] == "empty":
        flash("No messages exist in the saved date range.", "warning")
        return redirect(url_for("analysis_page"), code=303)
    if submission["outcome"] == "busy":
        flash("Another analysis is already running.", "warning")
    return redirect(
        url_for(
            "analysis_job_page",
            job_id=submission["job"]["job_id"],
        ),
        code=303,
    )


@app.post("/analysis/cache/clear")
def clear_analysis_cache():
    _require_analysis_action_token()
    if request.form.get("confirm") != "clear":
        abort(400)
    manager = _analysis_manager()
    if not manager.clear_cache():
        flash("Stop or finish the current analysis before clearing the cache.", "warning")
    else:
        flash(
            "Analysis cache cleared. Messages, photos, exports, and reports were untouched.",
            "success",
        )
    return redirect(url_for("analysis_page"), code=303)


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
    print("Message Analyzer is starting.", flush=True)
    print(f"\nOpen the app here:\n{LOCAL_APP_URL}\n", flush=True)
    print("Press Ctrl+C to stop the server.\n", flush=True)
    app.run(
        host="127.0.0.1",
        port=5000,
        debug=False,
        use_reloader=False,
    )


if __name__ == "__main__":
    main()
