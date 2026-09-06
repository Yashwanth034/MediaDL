"""Compact terminal transfer progress kept outside download engines."""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from typing import Any

from rich.cells import cell_len, set_cell_size
from rich.console import Console


class TerminalDownloadProgress:
    """Keep exactly one compact live transfer line in interactive terminals.

    Rich's multi-refresh ``Progress`` display is excellent in many terminals, but some
    terminal/shell combinations preserve its redraw frames instead of replacing them.
    MediaDL therefore owns one bounded carriage-return line directly. The line never
    exceeds the current terminal width and disappears before the final job summary.
    """

    def __init__(
        self,
        console: Console,
        *,
        refresh_interval: float = 0.20,
        stream_interval: float = 5.0,
    ) -> None:
        self._console = console
        self._refresh_interval = max(0.05, refresh_interval)
        self._stream_interval = max(0.0, stream_interval)
        self._last_render_at = 0.0
        self._current_position: int | None = None
        self._line_active = False
        self._last_line_cells = 0
        self._lock = threading.Lock()
        stream_is_tty = bool(getattr(console.file, "isatty", lambda: False)())
        self._ansi_clear = bool(
            console.is_terminal and not bool(getattr(console, "legacy_windows", False))
        )
        # Rich may report ``is_terminal=False`` for a real interactive TTY under
        # conservative TERM/environment detection. Do not make MediaDL silently
        # lose progress in that case; carriage-return rendering works without ANSI.
        self._dynamic = bool(stream_is_tty or self._ansi_clear)
        # Shell wrappers such as LYQO may capture child stdout through a pipe. In
        # that case carriage-return redraw is inappropriate, but completely hiding
        # progress is worse. Emit throttled newline snapshots instead.
        self._stream_mode = not self._dynamic

    def __enter__(self) -> TerminalDownloadProgress:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self._clear_line()

    def observe(
        self,
        label: str,
        position: int,
        total_items: int,
        status: Mapping[str, Any],
    ) -> None:
        """Render one throttled live line for transfer and post-processing work."""

        state = str(status.get("status") or "").casefold()
        if state not in {"downloading", "finished", "processing", "batch"}:
            return

        # Keep the live redraw line title-free. Long Unicode titles can occupy more
        # physical terminal cells than libraries report and force a wrap; once a line
        # wraps, carriage-return updates leave old rows behind. The item position plus
        # transfer/post-process state is enough for the live line.
        _ = label
        width = max(20, min(int(self._console.width or 80) - 2, 78))
        now = time.monotonic()
        interval = self._stream_interval if self._stream_mode else self._refresh_interval

        with self._lock:
            if state == "batch":
                completed_items = int(_nonnegative_number(status.get("completed_items")) or 0)
                active_items = int(_nonnegative_number(status.get("active_items")) or 0)
                preparing_items = int(_nonnegative_number(status.get("preparing_items")) or 0)
                downloading_items = int(
                    _nonnegative_number(status.get("downloading_items")) or 0
                )
                processing_items = int(_nonnegative_number(status.get("processing_items")) or 0)
                converting_items = int(_nonnegative_number(status.get("converting_items")) or 0)
                if completed_items < total_items and now - self._last_render_at < interval:
                    return
                self._write_line(
                    _format_batch_line(
                        completed_items=completed_items,
                        total_items=max(total_items, 1),
                        active_items=active_items,
                        preparing_items=preparing_items,
                        downloading_items=downloading_items,
                        processing_items=processing_items,
                        converting_items=converting_items,
                        width=width,
                    )
                )
                self._current_position = completed_items
                self._last_render_at = now
                return

            if state == "processing":
                if self._stream_mode and now - self._last_render_at < interval:
                    return
                phase = str(status.get("phase") or "Processing…")
                self._write_line(
                    _format_processing_line(
                        position=position,
                        total_items=max(total_items, position),
                        phase=phase,
                        width=width,
                    )
                )
                self._current_position = position
                self._last_render_at = now
                return

            if (
                state == "downloading"
                and self._current_position == position
                and now - self._last_render_at < interval
            ):
                return

            total = _positive_number(status.get("total_bytes")) or _positive_number(
                status.get("total_bytes_estimate")
            )
            completed = _nonnegative_number(status.get("downloaded_bytes")) or 0.0
            speed = _positive_number(status.get("speed"))
            eta = _nonnegative_number(status.get("eta"))

            self._write_line(
                _format_progress_line(
                    position=position,
                    total_items=max(total_items, position),
                    completed=completed,
                    total=total,
                    speed=speed,
                    eta=eta,
                    finished=state == "finished",
                    width=width,
                )
            )
            self._current_position = position
            self._last_render_at = now

    def _write_line(self, line: str) -> None:
        stream = self._console.file
        cells = cell_len(line)
        if self._stream_mode:
            stream.write(line)
            stream.write("\n")
            stream.flush()
            self._last_line_cells = 0
            self._line_active = False
            return
        if self._ansi_clear:
            stream.write("\r\x1b[2K")
            stream.write(line)
        else:
            # Plain carriage-return fallback for real TTYs that Rich does not mark
            # as fully terminal-capable. Pad over any longer previous line so stale
            # characters never remain visible.
            stream.write("\r")
            stream.write(line)
            if self._last_line_cells > cells:
                stream.write(" " * (self._last_line_cells - cells))
        stream.flush()
        self._last_line_cells = cells
        self._line_active = True

    def _clear_line(self) -> None:
        if not self._dynamic or not self._line_active:
            return
        stream = self._console.file
        if self._ansi_clear:
            stream.write("\r\x1b[2K")
        else:
            stream.write("\r")
            stream.write(" " * self._last_line_cells)
            stream.write("\r")
        stream.flush()
        self._last_line_cells = 0
        self._line_active = False


def _format_progress_line(
    *,
    position: int,
    total_items: int,
    completed: float,
    total: float | None,
    speed: float | None,
    eta: float | None,
    finished: bool,
    width: int,
) -> str:
    ratio = 1.0 if finished else (min(1.0, completed / total) if total else None)
    bar_width = 12 if width >= 62 else 8 if width >= 48 else 5
    parts = [f"{position}/{total_items}", _progress_bar(ratio, bar_width)]

    if total is not None:
        percent = 100.0 if finished else min(100.0, (completed / total) * 100.0)
        shown_completed = total if finished else min(completed, total)
        parts.append(f"{percent:.0f}%")
        parts.append(f"{_format_bytes(shown_completed)}/{_format_bytes(total)}")
    elif completed > 0:
        parts.append(_format_bytes(completed))

    if not finished and speed is not None:
        parts.append(f"{_format_bytes(speed)}/s")
    if not finished and eta is not None:
        parts.append(f"ETA {_format_eta(eta)}")

    line = "  ".join(parts)
    if cell_len(line) > width:
        line = set_cell_size(line, width).rstrip()
    return line


def _format_batch_line(
    *,
    completed_items: int,
    total_items: int,
    active_items: int,
    preparing_items: int,
    downloading_items: int,
    processing_items: int,
    converting_items: int,
    width: int,
) -> str:
    total = max(1, total_items)
    completed = max(0, min(total, completed_items))
    ratio = completed / total
    bar_width = 12 if width >= 62 else 8 if width >= 48 else 5
    parts = [
        f"{completed}/{total}",
        _progress_bar(ratio, bar_width),
        f"{ratio * 100.0:.0f}%",
    ]
    if active_items:
        parts.append(f"active {active_items}")
    if downloading_items:
        parts.append(f"downloading {downloading_items}")
    if converting_items:
        parts.append(f"converting {converting_items}")
    elif processing_items:
        parts.append(f"processing {processing_items}")
    elif preparing_items:
        parts.append(f"preparing {preparing_items}")
    line = "  ".join(parts)
    if cell_len(line) > width:
        line = set_cell_size(line, width).rstrip()
    return line


def _format_processing_line(
    *,
    position: int,
    total_items: int,
    phase: str,
    width: int,
) -> str:
    line = f"{position}/{total_items}  {_progress_bar(None, 12)}  {phase}"
    if cell_len(line) > width:
        line = set_cell_size(line, width).rstrip()
    return line


def _progress_bar(ratio: float | None, width: int) -> str:
    if ratio is None:
        pulse = min(3, width)
        return "[" + ("▓" * pulse) + ("░" * (width - pulse)) + "]"
    filled = max(0, min(width, int(round(ratio * width))))
    return "[" + ("█" * filled) + ("░" * (width - filled)) + "]"


def _format_bytes(value: float) -> str:
    size = max(0.0, value)
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    unit = units[0]
    for candidate in units:
        unit = candidate
        if size < 1024.0 or candidate == units[-1]:
            break
        size /= 1024.0
    if unit == "B":
        return f"{size:.0f} {unit}"
    if size >= 100:
        return f"{size:.0f} {unit}"
    if size >= 10:
        return f"{size:.1f} {unit}"
    return f"{size:.2f} {unit}"


def _format_eta(value: float) -> str:
    total = max(0, int(round(value)))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def _positive_number(value: object) -> float | None:
    parsed = _number(value)
    return parsed if parsed is not None and parsed > 0 else None


def _nonnegative_number(value: object) -> float | None:
    parsed = _number(value)
    return parsed if parsed is not None and parsed >= 0 else None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
