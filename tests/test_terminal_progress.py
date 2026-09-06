from io import StringIO

from rich.cells import cell_len
from rich.console import Console

from mediadl.cli.progress import (
    TerminalDownloadProgress,
    _format_batch_line,
    _format_progress_line,
)


class _TTYBuffer(StringIO):
    def isatty(self) -> bool:
        return True


def test_progress_line_is_short_and_bounded() -> None:
    line = _format_progress_line(
        position=2,
        total_items=5,
        completed=24 * 1024 * 1024,
        total=38 * 1024 * 1024,
        speed=6.1 * 1024 * 1024,
        eta=2,
        finished=False,
        width=68,
    )

    assert cell_len(line) <= 68
    assert line.startswith("2/5")
    assert "ETA 0:02" in line
    assert "MiB/s" in line


def test_batch_progress_line_shows_one_overall_collection_status() -> None:
    line = _format_batch_line(
        completed_items=188,
        total_items=1883,
        active_items=4,
        preparing_items=0,
        downloading_items=3,
        processing_items=1,
        converting_items=0,
        width=78,
    )

    assert cell_len(line) <= 78
    assert line.startswith("188/1883")
    assert "10%" in line
    assert "active 4" in line
    assert "downloading 3" in line
    assert "processing 1" in line


def test_interactive_progress_rewrites_one_line_without_newline_history() -> None:
    stream = _TTYBuffer()
    console = Console(file=stream, force_terminal=True, width=80)

    with TerminalDownloadProgress(console, refresh_interval=0.05) as progress:
        progress.observe(
            "First title",
            1,
            2,
            {
                "status": "downloading",
                "downloaded_bytes": 1024,
                "total_bytes": 4096,
                "speed": 2048,
                "eta": 2,
            },
        )
        progress.observe(
            "Second title",
            2,
            2,
            {
                "status": "downloading",
                "downloaded_bytes": 2048,
                "total_bytes": 4096,
                "speed": 2048,
                "eta": 1,
            },
        )
        progress.observe(
            "Second title",
            2,
            2,
            {
                "status": "finished",
                "downloaded_bytes": 4096,
                "total_bytes": 4096,
            },
        )

    rendered = stream.getvalue()
    assert "\n" not in rendered
    assert rendered.count("\r") >= 4  # 3 renders + final clear
    assert "1/2" in rendered
    assert "2/2" in rendered
    assert "First title" not in rendered
    assert "Second title" not in rendered


def test_processing_phase_replaces_finished_transfer_with_clean_status() -> None:
    stream = _TTYBuffer()
    console = Console(file=stream, force_terminal=True, width=80)

    with TerminalDownloadProgress(console, refresh_interval=0.05) as progress:
        progress.observe(
            "ignored title",
            1,
            4,
            {
                "status": "finished",
                "downloaded_bytes": 48 * 1024 * 1024,
                "total_bytes": 48 * 1024 * 1024,
            },
        )
        progress.observe(
            "ignored title",
            1,
            4,
            {"status": "processing", "phase": "Converting MP3…"},
        )

    rendered = stream.getvalue()
    assert "[▓▓▓░░░░░░░░░]" in rendered
    assert "Converting MP3…" in rendered
    assert "ignored title" not in rendered
    assert "\n" not in rendered


def test_real_tty_still_renders_when_rich_terminal_detection_is_disabled() -> None:
    stream = _TTYBuffer()
    console = Console(file=stream, force_terminal=False, width=80)

    with TerminalDownloadProgress(console, refresh_interval=0.05) as progress:
        progress.observe(
            "Collection",
            421,
            1883,
            {
                "status": "batch",
                "completed_items": 421,
                "active_items": 4,
                "preparing_items": 1,
                "downloading_items": 3,
                "processing_items": 0,
                "converting_items": 0,
            },
        )

    rendered = stream.getvalue()
    assert "421/1883" in rendered
    assert "downloading 3" in rendered
    assert "\x1b[2K" not in rendered
    assert "\r" in rendered


def test_noninteractive_progress_emits_throttled_newline_snapshots() -> None:
    stream = StringIO()
    console = Console(file=stream, force_terminal=False, width=80)

    with TerminalDownloadProgress(console, stream_interval=0.0) as progress:
        progress.observe(
            "Example",
            1,
            1,
            {
                "status": "downloading",
                "downloaded_bytes": 1,
                "total_bytes": 2,
            },
        )
        progress.observe(
            "Example",
            1,
            1,
            {"status": "finished", "downloaded_bytes": 2, "total_bytes": 2},
        )

    rendered = stream.getvalue()
    assert "1/1" in rendered
    assert "50%" in rendered
    assert "100%" in rendered
    assert "\x1b[2K" not in rendered
    assert "\r" not in rendered
    assert rendered.count("\n") == 2
