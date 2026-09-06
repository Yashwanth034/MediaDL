from __future__ import annotations

import io
import sys
from collections.abc import Iterator
from typing import Any

import pytest
from rich.console import Console

import mediadl.cli.app as cli_app
from mediadl.cli.guided import build_guided_argv
from mediadl.sources.models import SourceDescriptor, SourceKind
from mediadl.sources.playlists import PlaylistSummary


class ScriptedPrompts:
    def __init__(self, answers: list[str], confirms: list[bool]) -> None:
        self._answers: Iterator[str] = iter(answers)
        self._confirms: Iterator[bool] = iter(confirms)

    def prompt(self, _label: str, **_kwargs: Any) -> str:
        return next(self._answers)

    def confirm(self, _label: str, **_kwargs: Any) -> bool:
        return next(self._confirms)


def _console() -> Console:
    return Console(file=io.StringIO(), force_terminal=False)


def test_guided_single_video_compiles_mp4_720_command() -> None:
    url = "https://www.youtube.com/watch?v=abc123"
    scripted = ScriptedPrompts(
        answers=[url, "1", "1", "5"],
        confirms=[True],
    )

    argv = build_guided_argv(
        _console(),
        prompt=scripted.prompt,
        confirm=scripted.confirm,
    )

    assert argv == [url, "--mp4", "--quality", "720"]


def test_guided_explicit_best_quality_overrides_any_saved_default() -> None:
    url = "https://www.youtube.com/watch?v=abc123"
    scripted = ScriptedPrompts(
        answers=[url, "1", "1", "1"],
        confirms=[True],
    )

    argv = build_guided_argv(
        _console(),
        prompt=scripted.prompt,
        confirm=scripted.confirm,
    )

    assert argv == [url, "--mp4", "--quality", "best"]


def test_guided_channel_exposes_section_selection_views_likes_output_and_start() -> None:
    scripted = ScriptedPrompts(
        answers=[
            "@Example",
            "1",  # Videos
            "2",  # First N
            "20",
            "",  # No maximum duration
            "1",  # Views filter
            "1",  # Above
            "10L",
            "2",  # Likes filter
            "1",  # Above
            "50K",
            "7",  # Done filters
            "1",  # Video output
            "1",  # MP4
            "4",  # 1080p
        ],
        confirms=[
            True,   # Add other filters
            False,  # Keep source sort
            True,   # Start download
        ],
    )

    argv = build_guided_argv(
        _console(),
        prompt=scripted.prompt,
        confirm=scripted.confirm,
    )

    assert argv == [
        "@Example",
        "--videos",
        "--first",
        "20",
        "--views-above",
        "10L",
        "--likes-above",
        "50K",
        "--mp4",
        "--quality",
        "1080",
        "--yes",
    ]


def test_guided_channel_can_select_most_liked_without_conflicting_explicit_sort() -> None:
    scripted = ScriptedPrompts(
        answers=[
            "@Example",
            "2",  # Shorts
            "9",  # Most liked N
            "5",
            "",  # No maximum duration
            "2",  # Audio
            "1",  # MP3
            "2",  # 320 kbps
        ],
        confirms=[
            False,  # No other filters
            True,   # Start download
        ],
    )

    argv = build_guided_argv(
        _console(),
        prompt=scripted.prompt,
        confirm=scripted.confirm,
    )

    assert argv == [
        "@Example",
        "--shorts",
        "--most-liked",
        "5",
        "--mp3",
        "--quality",
        "320",
        "--yes",
    ]
    assert "--sort" not in argv


def test_guided_explicit_empty_collection_fails_before_selection() -> None:
    scripted = ScriptedPrompts(
        answers=["https://www.youtube.com/@Example/streams"],
        confirms=[],
    )

    with pytest.raises(cli_app.InputError, match="No usable items were found"):
        build_guided_argv(
            _console(),
            prompt=scripted.prompt,
            confirm=scripted.confirm,
            section_probe=lambda _source: False,
        )


def test_guided_empty_channel_section_reprompts_before_selection() -> None:
    scripted = ScriptedPrompts(
        answers=[
            "@Example",
            "3",  # Streams -> unavailable
            "1",  # Videos -> available
            "2",  # First N
            "2",
            "",  # No maximum duration
            "1",  # Video
            "1",  # MP4
            "5",  # 720p
        ],
        confirms=[
            False,  # No other filters
            False,  # Keep source order
            True,   # Start download
        ],
    )
    probed: list[SourceKind] = []

    def probe(source: SourceDescriptor) -> bool:
        probed.append(source.kind)
        return source.kind is not SourceKind.CHANNEL_STREAMS

    console = _console()
    argv = build_guided_argv(
        console,
        prompt=scripted.prompt,
        confirm=scripted.confirm,
        section_probe=probe,
    )

    assert probed == [SourceKind.CHANNEL_STREAMS, SourceKind.CHANNEL_VIDEOS]
    assert argv == [
        "@Example",
        "--videos",
        "--first",
        "2",
        "--mp4",
        "--quality",
        "720",
        "--yes",
    ]
    rendered = console.file.getvalue()
    assert "No usable Streams found" in rendered
    assert "Available: Videos" in rendered


def test_guided_everything_skips_empty_sections_and_keeps_available_tabs() -> None:
    scripted = ScriptedPrompts(
        answers=[
            "@Example",
            "5",  # Everything
            "2",  # First N
            "2",
            "",  # No maximum duration
            "1",  # Video
            "1",  # MP4
            "5",  # 720p
        ],
        confirms=[
            False,  # No other filters
            False,  # Keep source order
            True,   # Start download
        ],
    )

    def probe(source: SourceDescriptor) -> bool:
        return source.kind is not SourceKind.CHANNEL_STREAMS

    console = _console()
    argv = build_guided_argv(
        console,
        prompt=scripted.prompt,
        confirm=scripted.confirm,
        section_probe=probe,
    )

    assert argv[:2] == [
        "https://www.youtube.com/@Example/videos",
        "https://www.youtube.com/@Example/shorts",
    ]
    assert "--everything" not in argv
    assert "--streams" not in argv
    assert "Skipping empty sections: Streams" in console.file.getvalue()


def test_guided_channel_playlist_picker_can_choose_multiple_playlists() -> None:
    scripted = ScriptedPrompts(
        answers=[
            "@Example",
            "4",  # Playlists
            "2",  # Choose from numbered list
            "1,3",
            "1",  # All matching items in each selected playlist
            "",  # No maximum duration
            "1",  # Video
            "1",  # MP4
            "5",  # 720p
        ],
        confirms=[
            True,   # Use these two playlists
            False,  # No other filters
            False,  # Keep source order
            True,   # Start download
        ],
    )
    playlists = (
        PlaylistSummary("PL1", "One", "https://www.youtube.com/playlist?list=PL1"),
        PlaylistSummary("PL2", "Two", "https://www.youtube.com/playlist?list=PL2"),
        PlaylistSummary("PL3", "Three", "https://www.youtube.com/playlist?list=PL3"),
    )

    argv = build_guided_argv(
        _console(),
        prompt=scripted.prompt,
        confirm=scripted.confirm,
        playlist_discovery=lambda _source: playlists,
    )

    assert argv == [
        "https://www.youtube.com/playlist?list=PL1",
        "https://www.youtube.com/playlist?list=PL3",
        "--all",
        "--mp4",
        "--quality",
        "720",
        "--yes",
    ]


def test_guided_channel_playlist_search_can_select_all_matches() -> None:
    scripted = ScriptedPrompts(
        answers=[
            "@Example",
            "4",  # Playlists
            "3",  # Search titles
            "folk",
            "all",
            "2",  # First N items in each selected playlist
            "2",
            "",  # No maximum duration
            "2",  # Audio
            "1",  # MP3
            "2",  # 320 kbps
        ],
        confirms=[
            True,   # Use matched playlists
            False,  # No other filters
            False,  # Keep source order
            True,   # Start download
        ],
    )
    playlists = (
        PlaylistSummary("PL1", "Telangana Folk Songs", "https://www.youtube.com/playlist?list=PL1"),
        PlaylistSummary("PL2", "Devotional", "https://www.youtube.com/playlist?list=PL2"),
        PlaylistSummary("PL3", "New Folk Hits", "https://www.youtube.com/playlist?list=PL3"),
    )

    argv = build_guided_argv(
        _console(),
        prompt=scripted.prompt,
        confirm=scripted.confirm,
        playlist_discovery=lambda _source: playlists,
    )

    assert argv[:2] == [
        "https://www.youtube.com/playlist?list=PL1",
        "https://www.youtube.com/playlist?list=PL3",
    ]
    assert argv[2:] == ["--first", "2", "--mp3", "--quality", "320", "--yes"]


def test_guided_direct_channel_playlists_url_opens_playlist_picker() -> None:
    scripted = ScriptedPrompts(
        answers=[
            "https://www.youtube.com/@Example/playlists",
            "2",  # Choose from numbered list
            "2",
            "1",  # All matching media in selected playlist
            "",  # No maximum duration
            "1",  # Video
            "1",  # MP4
            "1",  # Best
        ],
        confirms=[
            True,   # Use selected playlist
            False,  # No filters
            False,  # Keep source order
            True,   # Start download
        ],
    )
    playlists = (
        PlaylistSummary("PL1", "One", "https://www.youtube.com/playlist?list=PL1"),
        PlaylistSummary("PL2", "Two", "https://www.youtube.com/playlist?list=PL2"),
    )

    argv = build_guided_argv(
        _console(),
        prompt=scripted.prompt,
        confirm=scripted.confirm,
        playlist_discovery=lambda _source: playlists,
    )

    assert argv == [
        "https://www.youtube.com/playlist?list=PL2",
        "--all",
        "--mp4",
        "--quality",
        "best",
        "--yes",
    ]


def test_guided_oldest_shows_detected_count_and_enter_uses_all_available() -> None:
    scripted = ScriptedPrompts(
        answers=[
            "@Example",
            "1",  # Videos
            "5",  # Oldest N
            "",   # Enter = detected 874
            "",   # No maximum duration
            "2",  # Audio
            "2",  # M4A
            "1",  # Best
        ],
        confirms=[
            False,  # No other filters
            True,   # Start download
        ],
    )
    probed: list[SourceKind] = []

    def count_probe(source: SourceDescriptor) -> int:
        probed.append(source.kind)
        return 874

    console = _console()
    argv = build_guided_argv(
        console,
        prompt=scripted.prompt,
        confirm=scripted.confirm,
        item_count_probe=count_probe,
    )

    assert probed == [SourceKind.CHANNEL_VIDEOS]
    assert "Available: Videos · 874 items" in console.file.getvalue()
    assert argv == [
        "@Example",
        "--videos",
        "--oldest",
        "874",
        "--format",
        "m4a",
        "--quality",
        "best",
        "--yes",
    ]


def test_guided_maximum_duration_shortcut_skips_long_uploads() -> None:
    scripted = ScriptedPrompts(
        answers=[
            "@Example",
            "1",  # Videos
            "2",  # First N
            "10",
            "7m",  # Arbitrary user-entered maximum duration
            "1",  # Video
            "1",  # MP4
            "1",  # Best
        ],
        confirms=[
            False,  # No other filters
            False,  # Keep source order
            True,   # Start download
        ],
    )

    argv = build_guided_argv(
        _console(),
        prompt=scripted.prompt,
        confirm=scripted.confirm,
    )

    assert argv == [
        "@Example",
        "--videos",
        "--first",
        "10",
        "--duration-max",
        "7m",
        "--mp4",
        "--quality",
        "best",
        "--yes",
    ]


def test_guided_large_top_n_warns_that_count_is_not_a_view_threshold() -> None:
    scripted = ScriptedPrompts(
        answers=[
            "@Example",
            "1",  # Videos
            "7",  # Top N by views
            "100000000",  # Mistaken view threshold
            "10",  # Correct item count after rejecting warning
            "",  # No maximum duration
            "1",  # Video
            "1",  # MP4
            "1",  # Best
        ],
        confirms=[
            False,  # Do not really select 100,000,000 items
            False,  # No other filters
            True,   # Start download (sort is implied)
        ],
    )
    console = _console()

    argv = build_guided_argv(
        console,
        prompt=scripted.prompt,
        confirm=scripted.confirm,
    )

    assert "--most-viewed" in argv
    assert argv[argv.index("--most-viewed") + 1] == "10"
    rendered = console.file.getvalue()
    assert "100,000,000 means item count, not views/likes" in rendered
    assert "Popular N items (most viewed" in rendered
    assert "use Filters" in rendered


def test_bare_interactive_main_uses_guided_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: list[list[str]] = []
    monkeypatch.setattr(sys, "argv", ["mdl"])
    monkeypatch.setattr(cli_app, "_interactive", lambda: True)
    monkeypatch.setattr(
        cli_app,
        "build_guided_argv",
        lambda _console, **_kwargs: [
            "https://www.youtube.com/watch?v=abc",
            "--mp4",
            "--quality",
            "720",
        ],
    )
    monkeypatch.setattr(cli_app, "app", lambda *, args: recorded.append(list(args)))

    cli_app.main()

    assert recorded == [[
        "download",
        "https://www.youtube.com/watch?v=abc",
        "--mp4",
        "--quality",
        "720",
    ]]


def test_noninteractive_bare_main_does_not_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: list[list[str]] = []
    monkeypatch.setattr(sys, "argv", ["mdl"])
    monkeypatch.setattr(cli_app, "_interactive", lambda: False)
    monkeypatch.setattr(
        cli_app,
        "build_guided_argv",
        lambda _console: pytest.fail("must not prompt"),
    )
    monkeypatch.setattr(cli_app, "app", lambda *, args: recorded.append(list(args)))

    cli_app.main()

    assert recorded == [[]]
