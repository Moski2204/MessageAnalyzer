"""Parse Instagram's HTML message export without rendering exported HTML."""

from __future__ import annotations

import calendar
import hashlib
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
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
MEDIA_REFERENCE_SPECS = (
    ("audio", "src"),
    ("source", "src"),
    ("video", "src"),
    ("video", "poster"),
    ("img", "src"),
    ("a", "href"),
)


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


@dataclass(slots=True)
class PhotoIndex:
    """Resolve exported image references against the local photos directory."""

    photo_dir: Path
    by_relative_path: dict[str, tuple[str, ...]]
    by_filename: dict[str, tuple[str, ...]]
    file_count: int
    ambiguous_filename_count: int

    @classmethod
    def build(cls, photo_dir: Path) -> "PhotoIndex":
        root = photo_dir.resolve()
        relative_paths: defaultdict[str, list[str]] = defaultdict(list)
        filenames: defaultdict[str, list[str]] = defaultdict(list)
        file_count = 0

        if photo_dir.is_dir():
            for path in photo_dir.rglob("*"):
                if not path.is_file():
                    continue
                try:
                    resolved = path.resolve()
                    relative = resolved.relative_to(root).as_posix()
                except (OSError, ValueError):
                    # Ignore symlinks or filesystem entries that escape PHOTO_DIR.
                    continue
                relative_paths[relative.casefold()].append(relative)
                filenames[path.name.casefold()].append(relative)
                file_count += 1

        by_relative_path = {
            key: tuple(sorted(values)) for key, values in relative_paths.items()
        }
        by_filename = {
            key: tuple(sorted(values)) for key, values in filenames.items()
        }
        return cls(
            photo_dir=root,
            by_relative_path=by_relative_path,
            by_filename=by_filename,
            file_count=file_count,
            ambiguous_filename_count=sum(
                1 for values in by_filename.values() if len(values) > 1
            ),
        )

    def resolve(self, raw_value: str | None) -> str | None:
        """Return one verified POSIX path relative to PHOTO_DIR, or ``None``."""
        if not raw_value:
            return None
        parsed = urlparse(raw_value)
        if parsed.scheme or parsed.netloc:
            return None

        raw_path = unquote(parsed.path).replace("\\", "/")
        if not raw_path or "\x00" in raw_path:
            return None
        posix_path = PurePosixPath(raw_path)
        windows_path = PureWindowsPath(raw_path)
        if (
            posix_path.is_absolute()
            or windows_path.is_absolute()
            or bool(windows_path.drive)
            or any(part in {"", ".", ".."} for part in raw_path.split("/"))
        ):
            return None

        parts = posix_path.parts
        photo_positions = [
            index for index, part in enumerate(parts) if part.casefold() == "photos"
        ]
        suffix_parts = (
            parts[photo_positions[-1] + 1 :] if photo_positions else parts
        )
        if not suffix_parts:
            return None
        relative_reference = PurePosixPath(*suffix_parts).as_posix()

        exact_matches = self.by_relative_path.get(
            relative_reference.casefold(), ()
        )
        if len(exact_matches) == 1:
            return exact_matches[0]
        if len(exact_matches) > 1:
            return None

        filename_matches = self.by_filename.get(suffix_parts[-1].casefold(), ())
        return filename_matches[0] if len(filename_matches) == 1 else None


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


def _external_url(content: Tag) -> str | None:
    for anchor in content.find_all("a", href=True):
        if anchor.find_parent("ul") is not None:
            continue
        href = str(anchor.get("href", "")).strip()
        parsed = urlparse(href)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return href
    return None


def _normalized_local_reference(raw_value: str | None) -> str | None:
    if not raw_value:
        return None
    parsed = urlparse(raw_value)
    if parsed.scheme or parsed.netloc:
        return None
    normalized = unquote(raw_value).replace("\\", "/").strip()
    return normalized or None


def _attachment_reference_key(candidates: list[str]) -> str | None:
    normalized = [
        reference
        for candidate in candidates
        if (reference := _normalized_local_reference(candidate))
    ]
    if not normalized:
        return None
    return hashlib.sha256("\x1f".join(normalized).encode("utf-8")).hexdigest()


def _resolve_photo_candidates(
    candidates: list[str], photo_index: PhotoIndex
) -> str | None:
    for candidate in candidates:
        safe_path = photo_index.resolve(candidate)
        if safe_path:
            return safe_path
    return None


def _has_gif_reference(candidates: list[str]) -> bool:
    for candidate in candidates:
        parsed = urlparse(candidate)
        suffix = PurePosixPath(unquote(parsed.path).replace("\\", "/")).suffix
        if suffix.casefold() == ".gif" or "giphy.com" in parsed.netloc.casefold():
            return True
    return False


def _attachment_details(
    content: Tag, photo_index: PhotoIndex
) -> tuple[str | None, str | None, bool]:
    candidates: list[str] = []
    photo_candidates: list[str] = []
    for tag_name, attribute in MEDIA_REFERENCE_SPECS:
        for element in content.find_all(tag_name):
            if element.find_parent("ul") is not None:
                continue
            value = element.get(attribute)
            if value:
                value = str(value)
                candidates.append(value)
                if tag_name == "img" and attribute == "src":
                    photo_candidates.append(value)
    return (
        _resolve_photo_candidates(photo_candidates, photo_index),
        _attachment_reference_key(candidates),
        _has_gif_reference(photo_candidates),
    )


def _has_nonreaction_element(content: Tag, tag_name: str) -> bool:
    return any(
        element.find_parent("ul") is None
        for element in content.find_all(tag_name)
    )


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
    image_is_gif: bool,
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
        if (
            image_is_gif
            or suffix == ".gif"
            or "giphy.com" in parsed_url.netloc.casefold()
        ):
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
    attachment_reference_key: str | None,
) -> str:
    fields = (
        sender,
        timestamp or original_timestamp,
        normalized_message,
        message_type,
        attachment_reference_key or attachment_path or "",
    )
    return hashlib.sha256("\x1f".join(fields).encode("utf-8")).hexdigest()


def _build_message(
    *,
    sender: str,
    original_timestamp: str,
    raw_text: str,
    external_url: str | None,
    attachment_path: str | None,
    attachment_reference_key: str | None,
    has_audio: bool,
    has_video: bool,
    has_image: bool,
    image_is_gif: bool,
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
        image_is_gif,
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
            attachment_reference_key,
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


def _parse_message_file_bs4(
    html_path: Path, data_dir: Path, photo_index: PhotoIndex
) -> Iterator[ParsedMessage]:
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
            attachment_reference_key = None
            external_url = None
            has_audio = has_video = has_image = False
            image_is_gif = False
        else:
            raw_text = _content_text(content)
            external_url = _external_url(content)
            (
                attachment_path,
                attachment_reference_key,
                image_is_gif,
            ) = _attachment_details(content, photo_index)
            has_audio = _has_nonreaction_element(content, "audio")
            has_video = _has_nonreaction_element(content, "video")
            has_image = _has_nonreaction_element(content, "img")

        yield _preserve_repeated_occurrence(
            _build_message(
                sender=sender,
                original_timestamp=original_timestamp,
                raw_text=raw_text,
                external_url=external_url,
                attachment_path=attachment_path,
                attachment_reference_key=attachment_reference_key,
                has_audio=has_audio,
                has_video=has_video,
                has_image=has_image,
                image_is_gif=image_is_gif,
                html_path=html_path,
                source_file_number=source_file_number,
                source_position=source_position,
            ),
            occurrences,
        )


def _joined_itertext(element) -> str:
    return WHITESPACE_RE.sub(" ", " ".join(element.itertext())).strip()


def _lxml_content_details(
    content, photo_index: PhotoIndex
) -> tuple[
    str,
    str | None,
    str | None,
    str | None,
    bool,
    bool,
    bool,
    bool,
]:
    text_parts: list[str] = []
    external_url: str | None = None
    media_candidates: dict[tuple[str, str], list[str]] = {
        spec: [] for spec in MEDIA_REFERENCE_SPECS
    }
    has_audio = has_video = has_image = False

    def visit(element, inside_reactions: bool = False) -> None:
        nonlocal external_url, has_audio, has_video, has_image
        tag = element.tag.casefold() if isinstance(element.tag, str) else ""
        is_reaction_list = inside_reactions or tag == "ul"
        if element.text and not is_reaction_list:
            text_parts.append(element.text)

        if not is_reaction_list:
            for spec in MEDIA_REFERENCE_SPECS:
                spec_tag, attribute = spec
                if tag == spec_tag and element.get(attribute):
                    media_candidates[spec].append(element.get(attribute))
            if tag == "a":
                href = element.get("href")
                if href:
                    parsed = urlparse(href)
                    if (
                        external_url is None
                        and parsed.scheme in {"http", "https"}
                        and parsed.netloc
                    ):
                        external_url = href
            elif tag == "audio":
                has_audio = True
            elif tag == "video":
                has_video = True
            elif tag == "img":
                has_image = True

        for child in element:
            visit(child, is_reaction_list)
            if child.tail and not is_reaction_list:
                text_parts.append(child.tail)

    visit(content)
    raw_text = WHITESPACE_RE.sub(" ", " ".join(text_parts)).strip()
    ordered_candidates = [
        candidate
        for spec in MEDIA_REFERENCE_SPECS
        for candidate in media_candidates[spec]
    ]
    attachment_path = _resolve_photo_candidates(
        media_candidates[("img", "src")], photo_index
    )
    attachment_reference_key = _attachment_reference_key(ordered_candidates)
    image_is_gif = _has_gif_reference(media_candidates[("img", "src")])
    return (
        raw_text,
        external_url,
        attachment_path,
        attachment_reference_key,
        has_audio,
        has_video,
        has_image,
        image_is_gif,
    )


def _parse_message_file_lxml(
    html_path: Path, data_dir: Path, photo_index: PhotoIndex
) -> Iterator[ParsedMessage]:
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
            attachment_reference_key = None
            has_audio = has_video = has_image = False
            image_is_gif = False
        else:
            (
                raw_text,
                external_url,
                attachment_path,
                attachment_reference_key,
                has_audio,
                has_video,
                has_image,
                image_is_gif,
            ) = _lxml_content_details(content, photo_index)

        yield _preserve_repeated_occurrence(
            _build_message(
                sender=sender,
                original_timestamp=original_timestamp,
                raw_text=raw_text,
                external_url=external_url,
                attachment_path=attachment_path,
                attachment_reference_key=attachment_reference_key,
                has_audio=has_audio,
                has_video=has_video,
                has_image=has_image,
                image_is_gif=image_is_gif,
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


def parse_message_file(
    html_path: Path,
    data_dir: Path,
    photo_index: PhotoIndex | None = None,
) -> Iterator[ParsedMessage]:
    """Yield messages with a streaming parser and a BeautifulSoup fallback."""
    if photo_index is None:
        photo_index = PhotoIndex.build(data_dir / "photos")
    try:
        yield from _parse_message_file_lxml(html_path, data_dir, photo_index)
    except (etree.LxmlError, OSError, ValueError):
        # A malformed file is retried in full. Stable deduplication keys make a
        # rare partial streaming retry safe for callers.
        yield from _parse_message_file_bs4(html_path, data_dir, photo_index)
