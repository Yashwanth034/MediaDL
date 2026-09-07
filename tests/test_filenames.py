from datetime import date
from pathlib import Path

import pytest

from mediadl.core.errors import InputError
from mediadl.core.formats import OutputFormat
from mediadl.storage.filenames import (
    FilenamePolicy,
    MediaFolder,
    build_media_filename,
    collection_directory,
    safe_join,
    sanitize_component,
)


def test_sanitize_component_keeps_unicode_and_removes_invalid_characters() -> None:
    result = sanitize_component('  తెలుగు Song: Live? / Test*  ')

    assert "తెలుగు" in result
    assert ":" not in result
    assert "?" not in result
    assert "/" not in result
    assert "*" not in result
    assert result == "తెలుగు Song - Live - Test"


def test_sanitize_component_blocks_traversal_and_windows_reserved_names() -> None:
    assert sanitize_component("../../etc/passwd") == "etc - passwd"
    assert sanitize_component("CON") == "_CON"
    assert sanitize_component("con.txt") == "_con.txt"
    assert sanitize_component("LPT9") == "_LPT9"


def test_safe_join_never_uses_raw_path_separators(tmp_path: Path) -> None:
    result = safe_join(tmp_path, "../Channel", "../../Videos")

    assert result.parent.parent == tmp_path.resolve()
    assert tmp_path.resolve() in result.parents
    assert ".." not in result.parts[-2:]


def test_build_media_filename_is_readable_collision_safe_and_dated() -> None:
    filename = build_media_filename(
        title="Why Linux? / Windows?",
        source_id="abc123XYZ",
        extension=".MP4",
        upload_date=date(2025, 8, 4),
    )

    assert filename == "2025-08-04 - Why Linux - Windows [abc123XYZ].mp4"
    assert filename.endswith("[abc123XYZ].mp4")


def test_same_title_different_source_ids_do_not_collide() -> None:
    first = build_media_filename(title="Same title", source_id="id-one", extension="mp4")
    second = build_media_filename(title="Same title", source_id="id-two", extension="mp4")

    assert first != second
    assert "[id-one]" in first
    assert "[id-two]" in second


def test_long_title_is_trimmed_without_losing_source_id() -> None:
    filename = build_media_filename(
        title="Very long title " * 50,
        source_id="immutable-source-id",
        extension="webm",
        max_length=90,
    )

    assert len(filename) <= 90
    assert filename.endswith("[immutable-source-id].webm")


def test_filename_limit_cannot_cut_source_identity() -> None:
    with pytest.raises(InputError, match="preserve the source ID"):
        build_media_filename(
            title="Example",
            source_id="x" * 80,
            extension="mp4",
            max_length=40,
        )


def test_invalid_extensions_are_rejected() -> None:
    for extension in ("../mp4", "mp4.exe!", "", "a" * 20):
        with pytest.raises(InputError):
            build_media_filename(
                title="Example",
                source_id="abc",
                extension=extension,
            )


def test_collection_layout_uses_audio_or_requested_media_folder(tmp_path: Path) -> None:
    video = collection_directory(
        tmp_path,
        collection_name="Example: Channel",
        output_format=OutputFormat.MP4,
        media_folder=MediaFolder.SHORTS,
    )
    audio = collection_directory(
        tmp_path,
        collection_name="Example: Channel",
        output_format=OutputFormat.MP3,
        media_folder=MediaFolder.STREAMS,
    )

    assert video == tmp_path.resolve() / "Example - Channel" / "Shorts"
    assert audio == tmp_path.resolve() / "Example - Channel" / "Audio"



def test_unicode_components_are_bounded_by_utf8_bytes() -> None:
    value = sanitize_component("తెలుగు" * 100, max_length=160)

    assert len(value.encode()) <= 160
    assert value


def test_ytdlp_template_keeps_multibyte_temp_filename_under_filesystem_limit(
    tmp_path: Path,
) -> None:
    from yt_dlp import YoutubeDL

    options = FilenamePolicy(max_filename_length=220).ytdlp_options(tmp_path)
    with YoutubeDL({**options, "quiet": True}) as ydl:
        filename = Path(
            ydl.prepare_filename(
                {
                    "id": "5DHvwHQuuCo",
                    "title": (
                        "గురువారం రోజు అన్ని రాశుల వారు ఈ పాటలు వింటే మీ కుటుంబం వ్యాపారం "
                        "అంతా బాగుంటుంది - SAI RAM SAI SHAYM"
                    ),
                    "ext": "m4a",
                }
            )
        )

    assert filename.name.endswith("[5DHvwHQuuCo].m4a")
    assert len(filename.name.encode()) <= 220
    assert len(f"{filename.name}.part".encode()) <= 255

def test_ytdlp_policy_trims_title_before_source_id(tmp_path: Path) -> None:
    options = FilenamePolicy(max_filename_length=220).ytdlp_options(tmp_path)

    assert "%(title).160B" in options["outtmpl"]
    assert "[%(id)s].%(ext)s" in options["outtmpl"]
    assert options["windowsfilenames"] is True
    assert options["trim_file_name"] == 220


def test_filename_policy_rejects_unreasonably_small_limit(tmp_path: Path) -> None:
    with pytest.raises(InputError, match="at least 80"):
        FilenamePolicy(max_filename_length=60).ytdlp_options(tmp_path)
