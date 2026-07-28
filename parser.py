"""Parse Instagram's HTML message export without rendering imported HTML."""

from __future__ import annotations

import calendar
import hashlib
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup, SoupStrainer, Tag
from dateutil import parser as date_parser
from lxml import etree


MESSAGE_FILE_RE = re.compile(r"^message_(\d+)\.html$", re.IGNORECASE)
MESSAGE_CONTAINER_CLASS = "_a6-g"
CONTENT_CLASS = "_a6-p"
TIMESTAMP_CLASS = "_a6-o"
WHITESPACE_RE = re.compile(r"\s+")


@dataclass(slots=True)
class ParsedMessage:
    sender: str
    timestamp: str | None
    timestamp_unix: int | None
    original_timestamp: str
    message_text: str
    normalized_text: str
    message_type: str
    source_filename: str
    source_file_number: int
    source_position: int
    attachment_path: str | None
    external_url: str | None
    deduplication_key: str


def discover_message_files(data_dir: Path) -> list[Path]:
    """Return message_*.html files in numeric filename order."""
    files: list[tuple[int, Path]] = []
    if not data_dir.is_dir():
        return []
    for path in data_dir.iterdir():
        if not path.is_file():
            continue
        match = MESSAGE_FILE_RE.match(path.name)
        if match:
            files.append((int(match.group(1)), path))
    return [path for _, path in sorted(files, key=lambda item: item[0])]


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    return WHITESPACE_RE.sub(" ", value).strip().casefold()


def parse_timestamp(value: str) -> tuple[str | None, int | None]:
    """Parse the export's timezone-free display timestamp."""
    value = WHITESPACE_RE.sub(" ", value).strip()
    if not value:
        return None, None

    parsed: datetime | None = None
    for fmt in ("%b %d, %Y, %I:%M %p", "%b %d, %Y %I:%M %p"):
        try:
            parsed = datetime.strptime(value, fmt)
            break
        except ValueError:
            pass
    if parsed is None:
        try:
            parsed = date_parser.parse(value, fuzzy=False)
        except (ValueError, OverflowError, TypeError):
            return None, None

    if parsed.tzinfo is not None:
        unix_value = int(parsed.timestamp())
        iso_value = parsed.isoformat(timespec="seconds")
    else:
        # The export does not specify a timezone. Treat the clock fields as a
        # stable, timezone-neutral ordering key rather than applying host DST.
        unix_value = calendar.timegm(parsed.timetuple())
        iso_value = parsed.isoformat(sep=" ", timespec="seconds")
    return iso_value, unix_value


def _safe_local_path(raw_value: str | None, html_path: Path, data_dir: Path) -> str | None:
    if not raw_value:
        return None
    parsed = urlparse(raw_value)
    if parsed.scheme or parsed.netloc:
        return None
    raw_path = unquote(parsed.path).replace("\\", "/")
    if not raw_path:
        return None
    try:
        candidate = (html_path.parent / raw_path).resolve()
        data_root = data_dir.resolve()
        candidate.relative_to(data_root)
    except (OSError, ValueError):
        return None
    return candidate.relative_to(data_root).as_posix()


def _external_url(content: Tag) -> str | None:
    for anchor in content.find_all("a", href=True):
        href = str(anchor.get("href", "")).strip()
        parsed = urlparse(href)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return href
    return None


def _attachment_path(content: Tag, html_path: Path, data_dir: Path) -> str | None:
    candidates: list[str] = []
    for tag_name, attribute in (
        ("audio", "src"),
        ("source", "src"),
        ("video", "src"),
        ("video", "poster"),
        ("img", "src"),
        ("a", "href"),
    ):
        for element in content.find_all(tag_name):
            value = element.get(attribute)
            if value:
                candidates.append(str(value))
    for candidate in candidates:
        safe_path = _safe_local_path(candidate, html_path, data_dir)
        if safe_path:
            return safe_path
    return None


def _content_text(content: Tag) -> str:
    """Collect visible message text while excluding reaction lists."""
    parts: list[str] = []
    for node in content.find_all(string=True):
        if node.find_parent("ul") is not None:
            continue
        value = WHITESPACE_RE.sub(" ", str(node)).strip()
        if value:
            parts.append(value)
    return WHITESPACE_RE.sub(" ", " ".join(parts)).strip()


def _classify_message(
    text: str,
    external_url: str | None,
    attachment_path: str | None,
    has_audio: bool,
    has_video: bool,
    has_image: bool,
) -> tuple[str, str]:
    lower_text = text.casefold()
    parsed_url = urlparse(external_url or "")
    url_path = parsed_url.path.casefold()

    if any(
        marker in lower_text
        for marker in (
            "content isn't available",
            "content is not available",
            "message unavailable",
            "deleted a message",
            "unsent a message",
            "removed a message",
        )
    ):
        return "unavailable", text or "[Unavailable content]"
    if "/reel/" in url_path or "/reels/" in url_path:
        return "shared_reel", text or "[Shared reel]"
    if "/p/" in url_path:
        return "shared_post", text or "[Shared post]"
    if has_audio:
        return "audio", text or "[Audio message]"
    if has_video:
        return "video", text or "[Video]"
    if has_image:
        suffix = Path(attachment_path or parsed_url.path).suffix.casefold()
        if suffix == ".gif" or "giphy.com" in parsed_url.netloc.casefold():
            return "gif", text or "[GIF]"
        return "photo", text or "[Photo]"
    if attachment_path:
        suffix = Path(attachment_path).suffix.casefold()
        if suffix in {".mp3", ".m4a", ".aac", ".ogg", ".wav"}:
            return "audio", text or "[Audio message]"
        if suffix in {".mp4", ".mov", ".webm"}:
            return "video", text or "[Video]"
        if suffix == ".gif":
            return "gif", text or "[GIF]"
        if suffix in {".jpg", ".jpeg", ".png", ".webp", ".heic"}:
            return "photo", text or "[Photo]"
    if not text and external_url:
        return "link", external_url
    if not text:
        return "unavailable", "[Unavailable content]"
    return "text", text


def _deduplication_key(
    sender: str,
    timestamp: str | None,
    original_timestamp: str,
    normalized_message: str,
    message_type: str,
    attachment_path: str | None,
) -> str:
    fields = (
        sender,
        timestamp or original_timestamp,
        normalized_message,
        message_type,
        attachment_path or "",
    )
    return hashlib.sha256("\x1f".join(fields).encode("utf-8")).hexdigest()


def _build_message(
    *,
    sender: str,
    original_timestamp: str,
    raw_text: str,
    external_url: str | None,
    attachment_path: str | None,
    has_audio: bool,
    has_video: bool,
    has_image: bool,
    html_path: Path,
    source_file_number: int,
    source_position: int,
) -> ParsedMessage:
    timestamp, timestamp_unix = parse_timestamp(original_timestamp)
    message_type, message_text = _classify_message(
        raw_text,
        external_url,
        attachment_path,
        has_audio,
        has_video,
        has_image,
    )
    sender = sender or "[Unknown sender]"
    normalized_message = normalize_text(message_text)
    return ParsedMessage(
        sender=sender,
        timestamp=timestamp,
        timestamp_unix=timestamp_unix,
        original_timestamp=original_timestamp,
        message_text=message_text,
        normalized_text=normalized_message,
        message_type=message_type,
        source_filename=html_path.name,
        source_file_number=source_file_number,
        source_position=source_position,
        attachment_path=attachment_path,
        external_url=external_url,
        deduplication_key=_deduplication_key(
            sender,
            timestamp,
            original_timestamp,
            normalized_message,
            message_type,
            attachment_path,
        ),
    )


def _preserve_repeated_occurrence(
    message: ParsedMessage, occurrences: Counter[str]
) -> ParsedMessage:
    """Preserve repeated same-minute messages while deduplicating file overlap.

    Instagram timestamps only have minute precision. Adding the occurrence
    ordinal prevents two legitimate identical messages in the same file from
    collapsing, while equivalent ordinal occurrences in overlapping files
    still share a key.
    """
    base_key = message.deduplication_key
    ordinal = occurrences[base_key]
    occurrences[base_key] += 1
    message.deduplication_key = f"{base_key}:{ordinal}"
    return message


def _parse_message_file_bs4(html_path: Path, data_dir: Path) -> Iterator[ParsedMessage]:
    """BeautifulSoup fallback for HTML that the streaming parser rejects."""
    # A callable is required for BeautifulSoup's parse-time matching of this
    # export's multi-valued class attribute. Descendants of each accepted
    # message container are retained.
    only_messages = SoupStrainer(
        "div",
        class_=lambda value: bool(value)
        and (
            MESSAGE_CONTAINER_CLASS in value.split()
            if isinstance(value, str)
            else MESSAGE_CONTAINER_CLASS in value
        ),
    )
    with html_path.open("r", encoding="utf-8-sig", errors="replace") as source:
        soup = BeautifulSoup(source, "lxml", parse_only=only_messages)

    containers = soup.find_all("div", class_=MESSAGE_CONTAINER_CLASS)
    file_match = MESSAGE_FILE_RE.match(html_path.name)
    source_file_number = int(file_match.group(1)) if file_match else 0
    occurrences: Counter[str] = Counter()
    for source_position, container in enumerate(containers):
        sender_element = container.find("h2")
        content = container.find("div", class_=CONTENT_CLASS)
        timestamp_element = container.find("div", class_=TIMESTAMP_CLASS)

        sender = (
            WHITESPACE_RE.sub(" ", sender_element.get_text(" ", strip=True)).strip()
            if sender_element
            else ""
        )
        original_timestamp = (
            WHITESPACE_RE.sub(" ", timestamp_element.get_text(" ", strip=True)).strip()
            if timestamp_element
            else ""
        )
        if content is None:
            raw_text = ""
            attachment_path = None
            external_url = None
            has_audio = has_video = has_image = False
        else:
            raw_text = _content_text(content)
            external_url = _external_url(content)
            attachment_path = _attachment_path(content, html_path, data_dir)
            has_audio = content.find("audio") is not None
            has_video = content.find("video") is not None
            has_image = content.find("img") is not None

        yield _preserve_repeated_occurrence(
            _build_message(
                sender=sender,
                original_timestamp=original_timestamp,
                raw_text=raw_text,
                external_url=external_url,
                attachment_path=attachment_path,
                has_audio=has_audio,
                has_video=has_video,
                has_image=has_image,
                html_path=html_path,
                source_file_number=source_file_number,
                source_position=source_position,
            ),
            occurrences,
        )


def _joined_itertext(element) -> str:
    return WHITESPACE_RE.sub(" ", " ".join(element.itertext())).strip()


def _lxml_content_details(
    content, html_path: Path, data_dir: Path
) -> tuple[str, str | None, str | None, bool, bool, bool]:
    text_parts: list[str] = []
    external_url: str | None = None
    local_candidates: list[str] = []
    has_audio = has_video = has_image = False

    def visit(element, inside_reactions: bool = False) -> None:
        nonlocal external_url, has_audio, has_video, has_image
        tag = element.tag.casefold() if isinstance(element.tag, str) else ""
        is_reaction_list = inside_reactions or tag == "ul"
        if element.text and not is_reaction_list:
            text_parts.append(element.text)

        if not is_reaction_list:
            if tag == "a":
                href = element.get("href")
                if href:
                    local_candidates.append(href)
                    parsed = urlparse(href)
                    if (
                        external_url is None
                        and parsed.scheme in {"http", "https"}
                        and parsed.netloc
                    ):
                        external_url = href
            elif tag == "audio":
                has_audio = True
                if element.get("src"):
                    local_candidates.append(element.get("src"))
            elif tag == "source":
                if element.get("src"):
                    local_candidates.append(element.get("src"))
            elif tag == "video":
                has_video = True
                for attribute in ("src", "poster"):
                    if element.get(attribute):
                        local_candidates.append(element.get(attribute))
            elif tag == "img":
                has_image = True
                if element.get("src"):
                    local_candidates.append(element.get("src"))

        for child in element:
            visit(child, is_reaction_list)
            if child.tail and not is_reaction_list:
                text_parts.append(child.tail)

    visit(content)
    raw_text = WHITESPACE_RE.sub(" ", " ".join(text_parts)).strip()
    attachment_path = next(
        (
            safe
            for candidate in local_candidates
            if (safe := _safe_local_path(candidate, html_path, data_dir))
        ),
        None,
    )
    return (
        raw_text,
        external_url,
        attachment_path,
        has_audio,
        has_video,
        has_image,
    )


def _parse_message_file_lxml(html_path: Path, data_dir: Path) -> Iterator[ParsedMessage]:
    file_match = MESSAGE_FILE_RE.match(html_path.name)
    source_file_number = int(file_match.group(1)) if file_match else 0
    source_position = 0
    occurrences: Counter[str] = Counter()

    context = etree.iterparse(
        str(html_path),
        events=("end",),
        tag="div",
        html=True,
        recover=True,
        encoding="utf-8",
        huge_tree=True,
    )
    for _, container in context:
        if MESSAGE_CONTAINER_CLASS not in (container.get("class") or "").split():
            continue

        sender_element = None
        content = None
        timestamp_element = None
        for element in container.iter():
            tag = element.tag.casefold() if isinstance(element.tag, str) else ""
            if tag == "h2" and sender_element is None:
                sender_element = element
            elif tag == "div":
                classes = (element.get("class") or "").split()
                if content is None and CONTENT_CLASS in classes:
                    content = element
                elif timestamp_element is None and TIMESTAMP_CLASS in classes:
                    timestamp_element = element

        sender = _joined_itertext(sender_element) if sender_element is not None else ""
        original_timestamp = (
            _joined_itertext(timestamp_element)
            if timestamp_element is not None
            else ""
        )

        if content is None:
            raw_text = ""
            external_url = None
            attachment_path = None
            has_audio = has_video = has_image = False
        else:
            (
                raw_text,
                external_url,
                attachment_path,
                has_audio,
                has_video,
                has_image,
            ) = _lxml_content_details(content, html_path, data_dir)

        yield _preserve_repeated_occurrence(
            _build_message(
                sender=sender,
                original_timestamp=original_timestamp,
                raw_text=raw_text,
                external_url=external_url,
                attachment_path=attachment_path,
                has_audio=has_audio,
                has_video=has_video,
                has_image=has_image,
                html_path=html_path,
                source_file_number=source_file_number,
                source_position=source_position,
            ),
            occurrences,
        )
        source_position += 1

        # Release the completed message subtree while streaming the file.
        container.clear()
        parent = container.getparent()
        if parent is not None:
            while container.getprevious() is not None:
                del parent[0]


def parse_message_file(html_path: Path, data_dir: Path) -> Iterator[ParsedMessage]:
    """Yield messages with a streaming parser and a BeautifulSoup fallback."""
    try:
        yield from _parse_message_file_lxml(html_path, data_dir)
    except (etree.LxmlError, OSError, ValueError):
        # A malformed file is retried in full. Import-level deduplication makes
        # a rare partial streaming retry safe.
        yield from _parse_message_file_bs4(html_path, data_dir)
