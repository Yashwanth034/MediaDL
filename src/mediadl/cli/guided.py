"""Interactive guided command builder for users who run bare ``mdl``.

The guided flow compiles choices into the same public CLI arguments used by
power users. It does not implement a second download path, so guided and
explicit commands always share validation, planning, dedupe, and execution.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import typer
from rich.console import Console

from mediadl.core.errors import InputError
from mediadl.core.formats import OutputFormat
from mediadl.core.numbers import parse_count
from mediadl.downloads.format_policy import FormatPolicyBuilder
from mediadl.selection.filters import parse_duration_seconds, parse_filter_date
from mediadl.selection.generic import parse_generic_filter
from mediadl.sources.models import SourceDescriptor, SourceKind
from mediadl.sources.playlists import PlaylistSummary
from mediadl.sources.resolver import SourceResolver

Prompt = Callable[..., Any]
Confirm = Callable[..., bool]
SectionProbe = Callable[[SourceDescriptor], bool | None]
ItemCountProbe = Callable[[SourceDescriptor], int | None]
PlaylistDiscovery = Callable[[SourceDescriptor], tuple[PlaylistSummary, ...] | None]


@dataclass(frozen=True, slots=True)
class CollectionCountHint:
    label: str
    count: int | None


def build_guided_argv(
    console: Console,
    *,
    prompt: Prompt = typer.prompt,
    confirm: Confirm = typer.confirm,
    resolver: SourceResolver | None = None,
    section_probe: SectionProbe | None = None,
    item_count_probe: ItemCountProbe | None = None,
    playlist_discovery: PlaylistDiscovery | None = None,
) -> list[str]:
    """Interactively build one normal ``mdl download`` argument vector."""

    console.print("[bold]MediaDL[/bold] · guided download")
    source_resolver = resolver or SourceResolver()
    raw_source = _nonempty(prompt, "Paste YouTube URL or @channel")
    source = source_resolver.resolve(raw_source)
    console.print(f"Detected: [cyan]{_source_label(source.kind)}[/cyan]")

    if (
        source.kind.is_collection
        and source.kind not in {SourceKind.CHANNEL, SourceKind.CHANNEL_PLAYLISTS}
        and section_probe
    ):
        with console.status("[cyan]Checking[/cyan] collection availability…"):
            availability = section_probe(source)
        if availability is False:
            raise InputError(
                f"No usable items were found in this {_source_label(source.kind)}. "
                "Choose another source or channel section."
            )
        if availability is None:
            console.print(
                "[yellow]Could not fully verify collection availability; "
                "continuing without blocking you.[/yellow]"
            )

    if source.kind is SourceKind.CHANNEL:
        channel_urls, channel_args = _channel_section(
            console,
            prompt,
            confirm,
            source,
            raw_source,
            section_probe,
            playlist_discovery,
        )
        argv = [*channel_urls, *channel_args]
    elif source.kind is SourceKind.CHANNEL_PLAYLISTS:
        root_url = source.root_url or source.url.removesuffix("/playlists")
        root_source = SourceDescriptor(
            platform=source.platform,
            kind=SourceKind.CHANNEL,
            source_key=source.source_key,
            url=root_url,
            root_url=root_url,
            title=source.title,
        )
        selected = _channel_playlist_selection(
            console,
            prompt,
            confirm,
            root_source,
            playlist_discovery,
        )
        if selected is None:
            console.print("Cancelled.")
            raise typer.Exit(0)
        argv = [playlist.url for playlist in selected]
    else:
        argv = [raw_source]

    if source.kind.is_collection:
        selection_args, selection_implies_sort = _collection_selection(
            console,
            prompt,
            confirm,
            sources=_guided_scope_sources(source, argv, source_resolver),
            item_count_probe=item_count_probe,
        )
        argv.extend(selection_args)
        maximum_duration_args = _maximum_duration_shortcut(prompt)
        other_filter_args = _filters(console, prompt, confirm)
        if "--duration-max" in other_filter_args:
            maximum_duration_args = []
        argv.extend(maximum_duration_args)
        argv.extend(other_filter_args)
        if not selection_implies_sort:
            argv.extend(_optional_sort(console, prompt, confirm))
        argv.extend(_output(console, prompt))
        if not confirm("Start download?", default=True):
            console.print("Cancelled.")
            raise typer.Exit(0)
        argv.append("--yes")
        return argv

    argv.extend(_output(console, prompt))
    if not confirm("Start download?", default=True):
        console.print("Cancelled.")
        raise typer.Exit(0)
    return argv


def _source_label(kind: SourceKind) -> str:
    return {
        SourceKind.VIDEO: "single video",
        SourceKind.PLAYLIST: "playlist",
        SourceKind.CHANNEL: "channel",
        SourceKind.CHANNEL_VIDEOS: "channel videos",
        SourceKind.CHANNEL_SHORTS: "channel shorts",
        SourceKind.CHANNEL_STREAMS: "channel streams",
        SourceKind.CHANNEL_PLAYLISTS: "channel playlists",
    }[kind]


def _guided_scope_sources(
    original: SourceDescriptor,
    argv: list[str],
    resolver: SourceResolver,
) -> tuple[SourceDescriptor, ...]:
    if original.kind is SourceKind.CHANNEL:
        if "--videos" in argv:
            return (original.for_tab(SourceKind.CHANNEL_VIDEOS),)
        if "--shorts" in argv:
            return (original.for_tab(SourceKind.CHANNEL_SHORTS),)
        if "--streams" in argv:
            return (original.for_tab(SourceKind.CHANNEL_STREAMS),)
        if "--everything" in argv:
            return tuple(
                original.for_tab(kind)
                for kind in (
                    SourceKind.CHANNEL_VIDEOS,
                    SourceKind.CHANNEL_SHORTS,
                    SourceKind.CHANNEL_STREAMS,
                )
            )

    raw_sources = tuple(value for value in argv if not value.startswith("--"))
    if not raw_sources:
        return (original,)
    return tuple(resolver.resolve(value) for value in raw_sources)


def _scope_label(source: SourceDescriptor) -> str:
    return {
        SourceKind.CHANNEL_VIDEOS: "Videos",
        SourceKind.CHANNEL_SHORTS: "Shorts",
        SourceKind.CHANNEL_STREAMS: "Streams",
        SourceKind.PLAYLIST: source.title or "Playlist",
    }.get(source.kind, source.title or _source_label(source.kind).title())


def _collection_count_hints(
    console: Console,
    sources: tuple[SourceDescriptor, ...],
    item_count_probe: ItemCountProbe | None,
) -> tuple[CollectionCountHint, ...]:
    if not sources:
        return ()
    if item_count_probe is None:
        return tuple(CollectionCountHint(_scope_label(source), None) for source in sources)
    if len(sources) > 4:
        console.print(
            f"[dim]{len(sources)} collections selected; exact per-collection counts will "
            "be determined during planning.[/dim]"
        )
        return tuple(CollectionCountHint(_scope_label(source), None) for source in sources)

    hints: list[CollectionCountHint] = []
    for source in sources:
        label = _scope_label(source)
        with console.status(f"[cyan]Detecting[/cyan] available {label} count…"):
            count = item_count_probe(source)
        hints.append(CollectionCountHint(label, count))

    known = [hint for hint in hints if hint.count is not None]
    if len(hints) == 1 and known:
        console.print(f"Available: [green]{known[0].label} · {known[0].count:,} items[/green].")
    elif known:
        details = " · ".join(
            f"{hint.label} {hint.count:,}" if hint.count is not None else f"{hint.label} ?"
            for hint in hints
        )
        console.print(f"Available: [green]{details}[/green].")
    else:
        console.print("[yellow]Could not determine the exact item count right now.[/yellow]")
    return tuple(hints)


def _channel_section(
    console: Console,
    prompt: Prompt,
    confirm: Confirm,
    source: SourceDescriptor,
    raw_source: str,
    section_probe: SectionProbe | None,
    playlist_discovery: PlaylistDiscovery | None,
) -> tuple[list[str], list[str]]:
    options = (
        ("1", "Videos"),
        ("2", "Shorts"),
        ("3", "Streams / Live"),
        ("4", "Playlists"),
        ("5", "Everything (Videos + Shorts + Streams; playlists excluded to avoid repeats)"),
    )
    kinds = {
        "1": SourceKind.CHANNEL_VIDEOS,
        "2": SourceKind.CHANNEL_SHORTS,
        "3": SourceKind.CHANNEL_STREAMS,
    }
    flags = {
        "1": ["--videos"],
        "2": ["--shorts"],
        "3": ["--streams"],
        "5": ["--everything"],
    }
    labels = {
        SourceKind.CHANNEL_VIDEOS: "Videos",
        SourceKind.CHANNEL_SHORTS: "Shorts",
        SourceKind.CHANNEL_STREAMS: "Streams",
    }
    availability: dict[SourceKind, bool | None] = {}

    while True:
        choice = _menu(console, prompt, "Channel section", options, default="1")
        if choice == "4":
            selected = _channel_playlist_selection(
                console,
                prompt,
                confirm,
                source,
                playlist_discovery,
            )
            if selected is None:
                continue
            return [playlist.url for playlist in selected], []

        if section_probe is None:
            return [raw_source], flags[choice]

        chosen_kinds = tuple(kinds.values()) if choice == "5" else (kinds[choice],)
        for kind in chosen_kinds:
            if kind in availability:
                continue
            label = labels[kind]
            with console.status(f"[cyan]Checking[/cyan] {label} availability…"):
                availability[kind] = section_probe(source.for_tab(kind))

        empty = [labels[kind] for kind in chosen_kinds if availability[kind] is False]
        unknown = [labels[kind] for kind in chosen_kinds if availability[kind] is None]
        available = [labels[kind] for kind in chosen_kinds if availability[kind] is True]

        if choice != "5" and empty:
            console.print(
                f"[yellow]No usable {empty[0]} found for this channel.[/yellow] "
                "Choose another section."
            )
            continue

        if choice == "5":
            included_kinds = tuple(
                kind for kind in chosen_kinds if availability[kind] is not False
            )
            if not included_kinds:
                console.print(
                    "[yellow]No usable Videos, Shorts, or Streams were found "
                    "for this channel.[/yellow] Choose a different source."
                )
                continue
            if empty:
                console.print(
                    f"[yellow]Skipping empty sections: {', '.join(empty)}.[/yellow]"
                )
                included_labels = [labels[kind] for kind in included_kinds]
                console.print(
                    f"Using available sections: [green]{', '.join(included_labels)}[/green]."
                )
                if unknown:
                    console.print(
                        "[yellow]Some included sections could not be fully verified: "
                        f"{', '.join(unknown)}.[/yellow]"
                    )
                return [source.for_tab(kind).url for kind in included_kinds], []

        if unknown:
            console.print(
                "[yellow]Could not fully verify availability for "
                f"{', '.join(unknown)}; continuing without blocking you.[/yellow]"
            )
        elif available:
            console.print(f"Available: [green]{', '.join(available)}[/green].")
        return [raw_source], flags[choice]


def _channel_playlist_selection(
    console: Console,
    prompt: Prompt,
    confirm: Confirm,
    source: SourceDescriptor,
    playlist_discovery: PlaylistDiscovery | None,
) -> tuple[PlaylistSummary, ...] | None:
    if playlist_discovery is None:
        console.print(
            "[yellow]Playlist discovery is unavailable in this session.[/yellow] "
            "Choose another section."
        )
        return None

    with console.status("[cyan]Discovering[/cyan] channel playlists…"):
        playlists = playlist_discovery(source)
    if playlists is None:
        console.print(
            "[yellow]Could not verify the channel Playlists tab right now.[/yellow] "
            "Choose another section or try again."
        )
        return None
    if not playlists:
        console.print(
            "[yellow]No usable playlists found for this channel.[/yellow] "
            "Choose another section."
        )
        return None

    console.print(f"Found [green]{len(playlists)}[/green] playlist(s).")
    while True:
        action = _menu(
            console,
            prompt,
            "Playlist selection",
            (
                ("1", "All playlists"),
                ("2", "Choose playlist(s) from numbered list"),
                ("3", "Search playlist titles"),
                ("4", "Back to channel sections"),
            ),
            default="2",
        )
        if action == "4":
            return None
        if action == "1":
            if len(playlists) > 20:
                console.print(
                    f"[yellow]This channel has {len(playlists)} playlists.[/yellow] "
                    "MediaDL will create one collection plan per playlist."
                )
                if not confirm(f"Use all {len(playlists)} playlists?", default=False):
                    continue
            _playlist_scope_note(console, len(playlists))
            return playlists

        if action == "3":
            term = _nonempty(prompt, "Search playlist title").casefold()
            rows = tuple(
                (index, playlist)
                for index, playlist in enumerate(playlists, start=1)
                if term in playlist.title.casefold()
            )
            if not rows:
                console.print("[yellow]No playlist titles matched that search.[/yellow]")
                continue
        else:
            rows = tuple(enumerate(playlists, start=1))

        selected = _playlist_picker(console, prompt, rows)
        if selected is None:
            continue
        _render_playlist_selection(console, selected)
        if not confirm(f"Use these {len(selected)} playlist(s)?", default=True):
            continue
        _playlist_scope_note(console, len(selected))
        return selected


def _playlist_picker(
    console: Console,
    prompt: Prompt,
    rows: tuple[tuple[int, PlaylistSummary], ...],
) -> tuple[PlaylistSummary, ...] | None:
    page_size = 20
    page = 0
    total_pages = max(1, (len(rows) + page_size - 1) // page_size)
    allowed = {index: playlist for index, playlist in rows}

    while True:
        start = page * page_size
        chunk = rows[start : start + page_size]
        console.print(
            f"\n[bold]Playlists {start + 1}-{start + len(chunk)} of {len(rows)}[/bold] "
            f"· page {page + 1}/{total_pages}"
        )
        for index, playlist in chunk:
            console.print(f"  {index}. {_short_title(playlist.title)}")
        console.print(
            "[dim]Enter numbers/ranges (example: 1,3,5:8), "
            "'all' for all shown/search matches, n=next, p=previous, b=back.[/dim]"
        )
        raw = _nonempty(prompt, "Select playlist(s)").strip()
        lowered = raw.casefold()
        if lowered in {"b", "back"}:
            return None
        if lowered in {"n", "next"}:
            if page + 1 < total_pages:
                page += 1
            else:
                console.print("[yellow]Already on the last page.[/yellow]")
            continue
        if lowered in {"p", "prev", "previous"}:
            if page > 0:
                page -= 1
            else:
                console.print("[yellow]Already on the first page.[/yellow]")
            continue
        if lowered == "all":
            return tuple(playlist for _, playlist in rows)

        try:
            indexes = _parse_playlist_indexes(raw, allowed=set(allowed))
        except InputError as exc:
            console.print(f"[yellow]Invalid playlist selection: {exc}[/yellow]")
            continue
        return tuple(allowed[index] for index in indexes)


def _parse_playlist_indexes(value: str, *, allowed: set[int]) -> tuple[int, ...]:
    selected: list[int] = []
    seen: set[int] = set()
    for raw_piece in value.split(","):
        piece = raw_piece.strip()
        if not piece:
            continue
        separator = ":" if ":" in piece else "-" if "-" in piece else None
        if separator is None:
            indexes = (_parse_playlist_index(piece),)
        else:
            parts = piece.split(separator)
            if len(parts) != 2:
                raise InputError("ranges must look like 5:8")
            start = _parse_playlist_index(parts[0])
            end = _parse_playlist_index(parts[1])
            if end < start:
                start, end = end, start
            indexes = tuple(range(start, end + 1))
        for index in indexes:
            if index not in allowed:
                raise InputError(f"playlist number {index} is not in the current list")
            if index not in seen:
                seen.add(index)
                selected.append(index)
    if not selected:
        raise InputError("enter at least one playlist number")
    return tuple(selected)


def _parse_playlist_index(value: str) -> int:
    try:
        parsed = int(value.strip())
    except ValueError as exc:
        raise InputError("playlist numbers must be whole numbers") from exc
    if parsed < 1:
        raise InputError("playlist numbers start at 1")
    return parsed


def _render_playlist_selection(
    console: Console,
    playlists: tuple[PlaylistSummary, ...],
) -> None:
    console.print(f"\nSelected [green]{len(playlists)}[/green] playlist(s):")
    for playlist in playlists[:8]:
        count = f" · {playlist.item_count} items" if playlist.item_count is not None else ""
        console.print(f"  • {_short_title(playlist.title)}{count}")
    if len(playlists) > 8:
        console.print(f"  … and {len(playlists) - 8} more")


def _playlist_scope_note(console: Console, count: int) -> None:
    console.print(
        f"[dim]The next selection/filter/output choices apply to each of the {count} "
        "selected playlist(s). Videos repeated across playlists are handled by dedupe.[/dim]"
    )


def _short_title(title: str, *, limit: int = 88) -> str:
    collapsed = " ".join(title.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


def _collection_selection(
    console: Console,
    prompt: Prompt,
    confirm: Confirm,
    *,
    sources: tuple[SourceDescriptor, ...] = (),
    item_count_probe: ItemCountProbe | None = None,
) -> tuple[list[str], bool]:
    choice = _menu(
        console,
        prompt,
        "Select media",
        (
            ("1", "All matching items"),
            ("2", "First N items (example: 10 = first 10 items)"),
            ("3", "Last N items (example: 10 = last 10 items)"),
            ("4", "Latest N items (newest first)"),
            ("5", "Oldest N items (oldest first)"),
            ("6", "Range A:B (example: 5:20)"),
            ("7", "Popular N items (most viewed; example: 10 = choose 10 items)"),
            ("8", "Bottom N items by views"),
            ("9", "Top N items by likes (example: 10 = choose 10 items)"),
            ("10", "Bottom N items by likes"),
        ),
        default="1",
    )
    if choice == "1":
        return ["--all"], False

    hints = _collection_count_hints(console, sources, item_count_probe)
    known_counts = [hint.count for hint in hints if hint.count is not None]
    exact_max = (
        max(known_counts)
        if hints and len(known_counts) == len(hints)
        else None
    )

    if choice == "6":
        while True:
            label = (
                f"Range (available 1:{exact_max:,}; Enter = 1:{exact_max:,})"
                if exact_max is not None
                else "Range (for example 1:20)"
            )
            raw = str(
                prompt(
                    label,
                    default=f"1:{exact_max}" if exact_max is not None else "",
                    show_default=False,
                )
            ).strip()
            if not raw and exact_max is not None:
                raw = f"1:{exact_max}"
            if not raw:
                typer.echo("Enter a range such as 1:20.")
                continue
            try:
                _validate_range(raw)
            except InputError as exc:
                typer.echo(f"Invalid: {exc}")
                continue
            if exact_max is not None and int(raw.split(":", 1)[1]) > exact_max:
                console.print(
                    f"[yellow]Only {exact_max:,} item(s) are available. "
                    f"Use an end value up to {exact_max:,}.[/yellow]"
                )
                continue
            return ["--range", raw], False

    flag, implies_sort = {
        "2": ("--first", False),
        "3": ("--last", False),
        "4": ("--latest", True),
        "5": ("--oldest", True),
        "7": ("--most-viewed", True),
        "8": ("--least-viewed", True),
        "9": ("--most-liked", True),
        "10": ("--least-liked", True),
    }[choice]
    if choice in {"7", "8"}:
        console.print(
            "[dim]This is the number of items to select. "
            "For a view threshold such as 10L views, use Filters → Views.[/dim]"
        )
    elif choice in {"9", "10"}:
        console.print(
            "[dim]This is the number of items to select. "
            "For a like threshold such as 50K likes, use Filters → Likes.[/dim]"
        )

    while True:
        label = (
            f"How many items? (available: {exact_max:,}; Enter = {exact_max:,})"
            if exact_max is not None
            else "How many items? (example: 10)"
        )
        raw = str(
            prompt(
                label,
                default=str(exact_max) if exact_max is not None else "",
                show_default=False,
            )
        ).strip()
        if not raw and exact_max is not None:
            raw = str(exact_max)
        try:
            count = int(raw)
        except ValueError:
            typer.echo("Enter a positive whole number.")
            continue
        if count < 1:
            typer.echo("Enter a positive whole number.")
            continue
        if exact_max is not None and count > exact_max:
            console.print(
                f"[yellow]Only {exact_max:,} item(s) are available. "
                f"Enter 1-{exact_max:,}, or press Enter for all.[/yellow]"
            )
            continue
        if count <= 10_000:
            break
        console.print(
            f"[yellow]{count:,} means item count, not views/likes.[/yellow] "
            "For thresholds such as 10L views, use Filters."
        )
        if confirm(f"Really select {count:,} items?", default=False):
            break
    return [flag, str(count)], implies_sort


def _maximum_duration_shortcut(prompt: Prompt) -> list[str]:
    while True:
        raw = str(
            prompt(
                "Maximum duration (e.g. 7m, 30m, 1.5h; Enter for no limit)",
                default="",
                show_default=False,
            )
        ).strip()
        if not raw:
            return []
        try:
            parse_duration_seconds(raw)
        except InputError as exc:
            typer.echo(f"Invalid: {exc}")
            continue
        return ["--duration-max", raw]


def _filters(console: Console, prompt: Prompt, confirm: Confirm) -> list[str]:
    if not confirm("Add other filters?", default=False):
        return []

    argv: list[str] = []
    while True:
        choice = _menu(
            console,
            prompt,
            "Filter",
            (
                ("1", "Views threshold (example: 10L = 1,000,000 views)"),
                ("2", "Likes threshold (example: 50K = 50,000 likes)"),
                ("3", "Upload date (example: on/after 2025-01-01)"),
                ("4", "Duration range/min/max (e.g. skip <30s or >10m)"),
                ("5", "Title contains text"),
                ("6", "Custom --where expression"),
                ("7", "Done adding filters"),
            ),
            default="7" if argv else "1",
        )
        if choice == "7":
            return argv
        if choice in {"1", "2"}:
            argv.extend(_engagement_filter(console, prompt, "views" if choice == "1" else "likes"))
        elif choice == "3":
            argv.extend(_date_filter(console, prompt))
        elif choice == "4":
            argv.extend(_duration_filter(console, prompt))
        elif choice == "5":
            argv.extend(["--title", _nonempty(prompt, "Title contains")])
        else:
            expression = _validated(prompt, "Custom filter", parse_generic_filter)
            argv.extend(["--where", expression])


def _engagement_filter(console: Console, prompt: Prompt, field: str) -> list[str]:
    choice = _menu(
        console,
        prompt,
        field.title(),
        (("1", "Above"), ("2", "Below"), ("3", "Between")),
        default="1",
    )
    if choice == "3":
        low = _validated(prompt, "Minimum (e.g. 10L)", parse_count)
        high = _validated(prompt, "Maximum (e.g. 1Cr)", parse_count)
        if parse_count(low) > parse_count(high):
            low, high = high, low
        return [f"--{field}-between", f"{low}:{high}"]
    value = _validated(prompt, "Threshold (e.g. 10L, 1Cr, 500K)", parse_count)
    return [f"--{field}-{'above' if choice == '1' else 'below'}", value]


def _date_filter(console: Console, prompt: Prompt) -> list[str]:
    choice = _menu(
        console,
        prompt,
        "Upload date",
        (("1", "On/after"), ("2", "On/before"), ("3", "Between")),
        default="1",
    )
    if choice == "3":
        after = _validated(prompt, "Start date (YYYY-MM-DD)", parse_filter_date)
        before = _validated(prompt, "End date (YYYY-MM-DD)", parse_filter_date)
        if parse_filter_date(after) > parse_filter_date(before):
            after, before = before, after
        return ["--after", after, "--before", before]
    value = _validated(prompt, "Date (YYYY-MM-DD)", parse_filter_date)
    return ["--after" if choice == "1" else "--before", value]


def _duration_filter(console: Console, prompt: Prompt) -> list[str]:
    choice = _menu(
        console,
        prompt,
        "Duration",
        (("1", "At least"), ("2", "At most"), ("3", "Between")),
        default="2",
    )
    if choice == "3":
        minimum = _validated(prompt, "Minimum duration (e.g. 5m)", parse_duration_seconds)
        maximum = _validated(prompt, "Maximum duration (e.g. 20m)", parse_duration_seconds)
        if parse_duration_seconds(minimum) > parse_duration_seconds(maximum):
            minimum, maximum = maximum, minimum
        return ["--duration-min", minimum, "--duration-max", maximum]
    value = _validated(prompt, "Duration (e.g. 15m or 01:30)", parse_duration_seconds)
    return ["--duration-min" if choice == "1" else "--duration-max", value]


def _optional_sort(console: Console, prompt: Prompt, confirm: Confirm) -> list[str]:
    if not confirm("Change sort order?", default=False):
        return []
    choice = _menu(
        console,
        prompt,
        "Sort",
        (
            ("1", "Source order"),
            ("2", "Popular (most viewed)"),
            ("3", "Least viewed"),
            ("4", "Most liked"),
            ("5", "Least liked"),
            ("6", "Latest (newest first)"),
            ("7", "Oldest (oldest first)"),
            ("8", "Longest"),
            ("9", "Shortest"),
            ("10", "Title A-Z"),
            ("11", "Title Z-A"),
        ),
        default="1",
    )
    value = {
        "1": "source",
        "2": "most_viewed",
        "3": "least_viewed",
        "4": "most_liked",
        "5": "least_liked",
        "6": "latest",
        "7": "oldest",
        "8": "longest",
        "9": "shortest",
        "10": "title_az",
        "11": "title_za",
    }[choice]
    return ["--sort", value]


def _output(console: Console, prompt: Prompt) -> list[str]:
    kind = _menu(
        console,
        prompt,
        "Output",
        (("1", "Video"), ("2", "Audio")),
        default="1",
    )
    if kind == "1":
        format_choice = _menu(
            console,
            prompt,
            "Video format",
            (("1", "MP4"), ("2", "WebM"), ("3", "MKV"), ("4", "Original")),
            default="1",
        )
        output_format = {
            "1": OutputFormat.MP4,
            "2": OutputFormat.WEBM,
            "3": OutputFormat.MKV,
            "4": OutputFormat.ORIGINAL,
        }[format_choice]
        quality = _video_quality(console, prompt, output_format)
    else:
        format_choice = _menu(
            console,
            prompt,
            "Audio format",
            (
                ("1", "MP3 (most compatible · conversion required)"),
                ("2", "M4A (usually faster when source is compatible)"),
                ("3", "Opus (usually faster when source is Opus)"),
                ("4", "FLAC (lossless conversion)"),
                ("5", "WAV (lossless conversion)"),
            ),
            default="1",
        )
        output_format = {
            "1": OutputFormat.MP3,
            "2": OutputFormat.M4A,
            "3": OutputFormat.OPUS,
            "4": OutputFormat.FLAC,
            "5": OutputFormat.WAV,
        }[format_choice]
        quality = _audio_quality(console, prompt, output_format)

    if output_format is OutputFormat.MP4:
        flag = "--mp4"
    elif output_format is OutputFormat.MP3:
        flag = "--mp3"
    else:
        flag = "--format"
    argv = [flag]
    if flag == "--format":
        argv.append(output_format.value)
    # Guided choices are explicit user intent and must override saved defaults,
    # including an explicit choice of "Best".
    argv.extend(["--quality", quality])
    return argv


def _video_quality(console: Console, prompt: Prompt, output_format: OutputFormat) -> str:
    choice = _menu(
        console,
        prompt,
        "Video quality",
        (
            ("1", "Best"),
            ("2", "2160p"),
            ("3", "1440p"),
            ("4", "1080p"),
            ("5", "720p"),
            ("6", "480p"),
            ("7", "360p"),
            ("8", "Lowest"),
            ("9", "Custom"),
        ),
        default="1",
    )
    value = {
        "1": "best",
        "2": "2160",
        "3": "1440",
        "4": "1080",
        "5": "720",
        "6": "480",
        "7": "360",
        "8": "lowest",
    }.get(choice)
    if value is None:
        value = _nonempty(prompt, "Quality (e.g. 1080, exact:1080, lowest)")
    _validate_format_quality(output_format, value)
    return value


def _audio_quality(console: Console, prompt: Prompt, output_format: OutputFormat) -> str:
    if output_format in {OutputFormat.FLAC, OutputFormat.WAV}:
        return "best"
    best_label = (
        "Best quality (slowest MP3 conversion)"
        if output_format is OutputFormat.MP3
        else "Best"
    )
    choice = _menu(
        console,
        prompt,
        "Audio quality",
        (
            ("1", best_label),
            ("2", "320 kbps"),
            ("3", "256 kbps"),
            ("4", "192 kbps"),
            ("5", "128 kbps"),
            ("6", "Custom"),
        ),
        default="1",
    )
    value = {"1": "best", "2": "320", "3": "256", "4": "192", "5": "128"}.get(choice)
    if value is None:
        value = _nonempty(prompt, "Bitrate (32-512 kbps)")
    _validate_format_quality(output_format, value)
    return value


def _validate_format_quality(output_format: OutputFormat, quality: str) -> None:
    FormatPolicyBuilder.build(output_format, quality)


def _menu(
    console: Console,
    prompt: Prompt,
    title: str,
    options: tuple[tuple[str, str], ...],
    *,
    default: str,
) -> str:
    console.print(f"\n[bold]{title}[/bold]")
    valid = {key for key, _ in options}
    for key, label in options:
        console.print(f"  {key}. {label}")
    while True:
        choice = str(prompt("Choose", default=default)).strip()
        if choice in valid:
            return choice
        console.print(f"[yellow]Choose one of: {', '.join(key for key, _ in options)}[/yellow]")


def _nonempty(prompt: Prompt, label: str) -> str:
    while True:
        value = str(prompt(label)).strip()
        if value:
            return value


def _positive_int(prompt: Prompt, label: str) -> int:
    while True:
        raw = _nonempty(prompt, label)
        try:
            value = int(raw)
        except ValueError:
            typer.echo("Enter a positive whole number.")
            continue
        if value > 0:
            return value
        typer.echo("Enter a positive whole number.")


def _validated(prompt: Prompt, label: str, validator: Callable[[str], object]) -> str:
    while True:
        value = _nonempty(prompt, label)
        try:
            validator(value)
        except InputError as exc:
            typer.echo(f"Invalid: {exc}")
            continue
        return value


def _validate_range(value: str) -> None:
    pieces = value.strip().split(":")
    if len(pieces) != 2:
        raise InputError("Range must use A:B")
    try:
        start, end = (int(piece) for piece in pieces)
    except ValueError as exc:
        raise InputError("Range must use positive integers") from exc
    if start < 1 or end < start:
        raise InputError("Range must use positive A:B with B >= A")
