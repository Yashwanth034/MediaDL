"""Readable, cross-platform-safe filename and folder policy."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from mediadl.core.errors import InputError
from mediadl.core.formats import OutputFormat

_INVALID_COMPONENT_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE = re.compile(r"\s+")
_REPEATED_DASHES = re.compile(r"(?:\s*-\s*){2,}")
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


class MediaFolder(StrEnum):
    VIDEOS = "Videos"
    SHORTS = "Shorts"
    STREAMS = "Streams"
    AUDIO = "Audio"


def sanitize_component(
    value: str,
    *,
    fallback: str = "Untitled",
    max_length: int = 160,
) -> str:
    """Return one readable path component safe on common desktop filesystems."""

    if max_length < 16:
        raise InputError("Filename component limit must be at least 16 characters")

    normalized = unicodedata.normalize("NFC", str(value))
    normalized = _INVALID_COMPONENT_CHARS.sub(" - ", normalized)
    normalized = _WHITESPACE.sub(" ", normalized)
    normalized = _REPEATED_DASHES.sub(" - ", normalized)
    normalized = normalized.strip(" .-")

    if not normalized or normalized in {".", ".."}:
        normalized = fallback

    reserved_stem = normalized.split(".", 1)[0].upper()
    if reserved_stem in _WINDOWS_RESERVED:
        normalized = f"_{normalized}"

    if len(normalized) > max_length:
        normalized = normalized[:max_length].rstrip(" .-")

    return normalized or fallback


def safe_join(base: Path, *components: str) -> Path:
    """Join only sanitized components and prove the result remains below base."""

    root = base.expanduser().resolve(strict=False)
    result = root
    for component in components:
        result /= sanitize_component(component)

    resolved = result.resolve(strict=False)
    if resolved != root and root not in resolved.parents:
        raise InputError("Output path escaped the selected destination")
    return resolved


def build_media_filename(
    *,
    title: str,
    source_id: str,
    extension: str,
    upload_date: str | date | datetime | None = None,
    max_length: int = 220,
) -> str:
    """Build a readable collision-safe final filename from known metadata."""

    clean_id = sanitize_component(source_id, fallback="unknown-id", max_length=80)
    clean_extension = _clean_extension(extension)
    prefix = _date_prefix(upload_date)

    fixed = f"{prefix}[{clean_id}].{clean_extension}"
    minimum_length = len(fixed) + 3
    if max_length < minimum_length:
        raise InputError("Filename limit is too small to preserve the source ID")
    title_budget = max(16, max_length - len(fixed) - 1)
    clean_title = sanitize_component(title, max_length=title_budget)
    filename = f"{prefix}{clean_title} [{clean_id}].{clean_extension}"

    if len(filename) > max_length:
        overflow = len(filename) - max_length
        clean_title = clean_title[: max(1, len(clean_title) - overflow)].rstrip(" .-")
        filename = f"{prefix}{clean_title} [{clean_id}].{clean_extension}"
    return filename


def collection_directory(
    base: Path,
    *,
    collection_name: str,
    output_format: OutputFormat,
    media_folder: MediaFolder = MediaFolder.VIDEOS,
) -> Path:
    """Return the clean default folder for one collection download."""

    folder = MediaFolder.AUDIO if output_format.is_audio else media_folder
    return safe_join(base, collection_name, folder.value)


@dataclass(frozen=True, slots=True)
class FilenamePolicy:
    """Central yt-dlp naming options used before complete metadata is known."""

    max_filename_length: int = 220

    def ytdlp_options(self, output_dir: Path) -> dict[str, Any]:
        if self.max_filename_length < 80:
            raise InputError("Filename limit must be at least 80 characters")
        output_dir = output_dir.expanduser().resolve(strict=False)
        title_limit = min(160, self.max_filename_length - 60)
        template_name = f"%(title).{title_limit}s [%(id)s].%(ext)s"
        output_template = str(output_dir / template_name)
        return {
            "outtmpl": output_template,
            "windowsfilenames": True,
            "trim_file_name": self.max_filename_length,
        }


def _clean_extension(extension: str) -> str:
    cleaned = extension.strip().lower().lstrip(".")
    if not cleaned or not re.fullmatch(r"[a-z0-9]{1,12}", cleaned):
        raise InputError("Invalid output file extension")
    return cleaned


def _date_prefix(value: str | date | datetime | None) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        parsed = value.date()
    elif isinstance(value, date):
        parsed = value
    elif isinstance(value, str):
        compact = value.strip()
        try:
            if re.fullmatch(r"\d{8}", compact):
                parsed = datetime.strptime(compact, "%Y%m%d").date()
            else:
                parsed = date.fromisoformat(compact)
        except ValueError as exc:
            raise InputError(f"Invalid upload date: {value}") from exc
    else:
        raise InputError("Unsupported upload date value")
    return f"{parsed.isoformat()} - "
