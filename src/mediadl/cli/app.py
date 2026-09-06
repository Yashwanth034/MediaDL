"""MediaDL command-line entry point."""

from __future__ import annotations

import os
import sys
from contextlib import nullcontext
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from mediadl import __version__
from mediadl.auth.options import AuthConfig
from mediadl.cli.collection import CollectionQueryInput, compile_collection_query
from mediadl.cli.guided import build_guided_argv
from mediadl.cli.progress import TerminalDownloadProgress
from mediadl.cli.workflow import CollectionWorkflow, NoMatchingMediaError
from mediadl.core.config import AppConfig, ConfigStore
from mediadl.core.doctor import CheckStatus, Doctor
from mediadl.core.errors import InputError, MediaDLError, UserCancelledError
from mediadl.core.formats import OutputFormat
from mediadl.core.logging import configure_logging
from mediadl.core.paths import get_app_paths
from mediadl.core.policies import DedupeMode
from mediadl.core.updater import check_update, install_verified_update, load_manifest, stage_update
from mediadl.dedupe.basic import BasicDedupeService
from mediadl.dedupe.repository import SmartDedupeRepository
from mediadl.dedupe.service import SmartDedupeService
from mediadl.downloads.direct import DirectDownloadCoordinator, DirectDownloadStatus
from mediadl.downloads.disk_guard import DiskSpaceGuard
from mediadl.downloads.executor import CollectionJobExecutor, ExecutionSummary
from mediadl.downloads.jobs import JobBreakdown, JobRecord, JobRepository, JobStatus
from mediadl.downloads.plan import DownloadPlan
from mediadl.downloads.preview import PlanPreview, build_preview
from mediadl.downloads.single import SingleDownloadRequest, SingleDownloadService
from mediadl.engines.media_fingerprint import FFmpegFingerprintEngine
from mediadl.engines.network import NetworkPolicy
from mediadl.engines.ytdlp import PostprocessorHook, ProgressHook, YtDlpAdapter
from mediadl.index.enrichment import MetadataEnricher
from mediadl.index.repository import IndexRepository
from mediadl.selection.filters import SortMode
from mediadl.sources.models import ScanResult, SourceDescriptor, SourceKind
from mediadl.sources.playlists import ChannelPlaylistCatalog, PlaylistSummary
from mediadl.sources.resolver import SourceResolver
from mediadl.sources.scanner import CollectionScanner
from mediadl.storage.database import Database

app = typer.Typer(
    name="mdl",
    help="Download single videos, playlists, and channels cleanly.",
    add_completion=False,
    no_args_is_help=False,
    pretty_exceptions_enable=False,
)
console = Console()

# Bare `mdl` can enumerate a collection once to show the user its exact size.
# The same one-process scan is then reused by planning so guided UX does not
# pay for a second full YouTube enumeration.
_GUIDED_SCAN_CACHE: dict[tuple[str, str], ScanResult] = {}


def _guided_scan_key(source: SourceDescriptor) -> tuple[str, str]:
    return source.kind.value, source.url


def _single_format(
    config: AppConfig,
    *,
    mp4: bool,
    mp3: bool,
    format_value: OutputFormat | None,
) -> OutputFormat:
    explicit_count = int(mp4) + int(mp3) + int(format_value is not None)
    if explicit_count > 1:
        raise InputError("Choose only one of --mp4, --mp3, or --format")
    if mp4:
        return OutputFormat.MP4
    if mp3:
        return OutputFormat.MP3
    if format_value is not None:
        return format_value
    return OutputFormat(config.default_format)


def _dedupe_mode(config: AppConfig, value: DedupeMode | None) -> DedupeMode:
    return value or DedupeMode(config.dedupe_mode)


def _collection_worker_count(
    output_format: OutputFormat,
    requested: int | None,
) -> int:
    """Choose bounded parallelism without overloading FFmpeg-heavy formats."""

    if requested is not None:
        if requested < 1 or requested > 8:
            raise InputError("--jobs must be between 1 and 8")
        return requested
    cpu_count = max(1, os.cpu_count() or 1)
    if output_format in {OutputFormat.M4A, OutputFormat.OPUS}:
        return min(4, cpu_count)
    if output_format in {OutputFormat.MP3, OutputFormat.FLAC, OutputFormat.WAV}:
        return min(3, cpu_count)
    return min(3, cpu_count)


def _collection_conversion_worker_count(output_format: OutputFormat) -> int:
    """Keep CPU-heavy FFmpeg conversion bounded independently from downloads."""

    if output_format not in {OutputFormat.MP3, OutputFormat.FLAC, OutputFormat.WAV}:
        return 1
    return min(2, max(1, os.cpu_count() or 1))


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _guided_section_probe(source: SourceDescriptor) -> bool | None:
    """Cheap one-item probe used only to avoid dead-end guided channel choices."""

    try:
        result = CollectionScanner(
            YtDlpAdapter(network_policy=NetworkPolicy(retries=2, extractor_retries=1))
        ).scan(source, max_items=1)
    except MediaDLError:
        # A temporary/auth/network problem must not falsely label a real section empty.
        return None
    return result.item_count > 0 or bool(result.reported_count)


def _guided_item_count_probe(source: SourceDescriptor) -> int | None:
    """Enumerate one selected collection once and retain it for guided planning."""

    key = _guided_scan_key(source)
    cached = _GUIDED_SCAN_CACHE.get(key)
    if cached is not None:
        return cached.reported_count if cached.reported_count is not None else cached.item_count
    try:
        scan = CollectionScanner(
            YtDlpAdapter(network_policy=NetworkPolicy(retries=2, extractor_retries=1))
        ).scan(source)
    except MediaDLError:
        return None
    _GUIDED_SCAN_CACHE[key] = scan
    return scan.reported_count if scan.reported_count is not None else scan.item_count


def _guided_playlist_discovery(
    source: SourceDescriptor,
) -> tuple[PlaylistSummary, ...] | None:
    """Discover a channel's playlists without turning transient failures into 'empty'."""

    try:
        return ChannelPlaylistCatalog(
            YtDlpAdapter(network_policy=NetworkPolicy(retries=2, extractor_retries=1))
        ).discover(source)
    except MediaDLError:
        return None


def _database() -> Database:
    paths = get_app_paths()
    paths.ensure_runtime_dirs()
    database = Database(paths.database_file)
    database.initialize()
    return database


def _smart_dedupe(database: Database) -> SmartDedupeService | None:
    engine = FFmpegFingerprintEngine()
    if not engine.available():
        return None
    return SmartDedupeService(engine, SmartDedupeRepository(database))


def _adapter(
    *,
    logger: object,
    browser: str | None,
    browser_profile: str | None,
    cookie_file: Path | None,
    retries: int,
) -> YtDlpAdapter:
    return YtDlpAdapter(
        logger=logger,  # type: ignore[arg-type]
        auth=AuthConfig(
            browser=browser,
            browser_profile=browser_profile,
            cookie_file=cookie_file,
        ),
        network_policy=NetworkPolicy(retries=retries),
    )


@app.callback(invoke_without_command=True)
def root(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option("--version", "-V", help="Show MediaDL version and exit."),
    ] = False,
) -> None:
    """Show root help/version while real work lives in explicit subcommands."""

    if version:
        console.print(f"MediaDL {__version__}")
        raise typer.Exit(0)
    if ctx.invoked_subcommand is None:
        console.print("[bold]MediaDL[/bold]")
        console.print("Run [cyan]mdl --help[/cyan] for usage.")


@app.command(name="download")
def download(
    urls: Annotated[
        list[str],
        typer.Argument(help="One or more YouTube video, playlist, or channel URLs/handles."),
    ],
    verbose: Annotated[
        bool,
        typer.Option("--verbose", help="Enable verbose diagnostic logging."),
    ] = False,
    mp4: Annotated[
        bool,
        typer.Option("--mp4", help="Use MP4 output."),
    ] = False,
    mp3: Annotated[
        bool,
        typer.Option("--mp3", help="Use MP3 audio output."),
    ] = False,
    format_value: Annotated[
        OutputFormat | None,
        typer.Option("--format", help="Output format."),
    ] = None,
    quality: Annotated[
        str | None,
        typer.Option("--quality", "-q", help="Quality: best, 1080, exact:1080, 320, etc."),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Destination folder."),
    ] = None,
    dedupe: Annotated[
        DedupeMode | None,
        typer.Option("--dedupe", help="Duplicate policy: safe, audio, or off."),
    ] = None,
    preview: Annotated[
        bool,
        typer.Option("--preview", help="Show the frozen plan without downloading."),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Start without confirmation."),
    ] = False,
    all_items: Annotated[
        bool,
        typer.Option("--all", help="Select all matching collection items."),
    ] = False,
    first: Annotated[int | None, typer.Option("--first", help="First N items.")] = None,
    last: Annotated[int | None, typer.Option("--last", help="Last N items.")] = None,
    range_value: Annotated[
        str | None,
        typer.Option("--range", help="1-based inclusive range, e.g. 101:200."),
    ] = None,
    latest: Annotated[int | None, typer.Option("--latest", help="Latest N items.")] = None,
    oldest: Annotated[int | None, typer.Option("--oldest", help="Oldest N items.")] = None,
    most_viewed: Annotated[
        int | None,
        typer.Option("--most-viewed", help="Top N by views."),
    ] = None,
    least_viewed: Annotated[
        int | None,
        typer.Option("--least-viewed", help="Bottom N by views."),
    ] = None,
    most_liked: Annotated[
        int | None,
        typer.Option("--most-liked", help="Top N by likes."),
    ] = None,
    least_liked: Annotated[
        int | None,
        typer.Option("--least-liked", help="Bottom N by likes."),
    ] = None,
    sort_mode: Annotated[
        SortMode | None,
        typer.Option("--sort", help="Explicit collection sort."),
    ] = None,
    views_above: Annotated[
        str | None,
        typer.Option("--views-above", help="Views strictly above, e.g. 10L or 1Cr."),
    ] = None,
    views_below: Annotated[
        str | None,
        typer.Option("--views-below", help="Views strictly below, e.g. 1Cr."),
    ] = None,
    views_between: Annotated[
        str | None,
        typer.Option("--views-between", help="Inclusive views range, e.g. 10L:1Cr."),
    ] = None,
    likes_above: Annotated[
        str | None,
        typer.Option("--likes-above", help="Likes strictly above."),
    ] = None,
    likes_below: Annotated[
        str | None,
        typer.Option("--likes-below", help="Likes strictly below."),
    ] = None,
    likes_between: Annotated[
        str | None,
        typer.Option("--likes-between", help="Inclusive likes range, e.g. 1L:5L."),
    ] = None,
    after: Annotated[
        str | None,
        typer.Option("--after", help="Uploaded on/after YYYY-MM-DD."),
    ] = None,
    before: Annotated[
        str | None,
        typer.Option("--before", help="Uploaded on/before YYYY-MM-DD."),
    ] = None,
    title_contains: Annotated[
        str | None,
        typer.Option("--title", help="Title contains this text."),
    ] = None,
    duration_min: Annotated[
        str | None,
        typer.Option("--duration-min", help="Minimum duration, e.g. 5m or 01:30."),
    ] = None,
    duration_max: Annotated[
        str | None,
        typer.Option("--duration-max", help="Maximum duration."),
    ] = None,
    where: Annotated[
        list[str] | None,
        typer.Option(
            "--where",
            help=(
                "Repeatable custom filter, e.g. 'views>=10L', 'likes<2L', "
                "'duration>=5m', 'date>=2025-01-01', 'title~linux', or 'type=short'."
            ),
        ),
    ] = None,
    videos: Annotated[
        bool,
        typer.Option("--videos", help="For a channel root, use Videos."),
    ] = False,
    shorts: Annotated[
        bool,
        typer.Option("--shorts", help="For a channel root, use Shorts."),
    ] = False,
    streams: Annotated[
        bool,
        typer.Option("--streams", help="For a channel root, use Streams."),
    ] = False,
    everything: Annotated[
        bool,
        typer.Option(
            "--everything", help="For a channel root, process Videos, Shorts, and Streams."
        ),
    ] = False,
    browser: Annotated[
        str | None,
        typer.Option("--cookies-from-browser", help="Use authorized cookies from a browser."),
    ] = None,
    browser_profile: Annotated[
        str | None,
        typer.Option("--browser-profile", help="Browser profile for cookie access."),
    ] = None,
    cookie_file: Annotated[
        Path | None,
        typer.Option("--cookies", help="Netscape-format cookie file."),
    ] = None,
    parallel_jobs: Annotated[
        int | None,
        typer.Option(
            "--jobs",
            min=1,
            max=8,
            help="Parallel collection items. Default: automatic safe value for the format.",
        ),
    ] = None,
    retries: Annotated[
        int,
        typer.Option("--retries", min=0, help="yt-dlp request retry count."),
    ] = 5,
) -> None:
    """Download one video or plan/execute a playlist/channel collection."""

    config = ConfigStore().load()
    logger = configure_logging(verbose=verbose or config.verbose)

    selected_format = _single_format(
        config,
        mp4=mp4,
        mp3=mp3,
        format_value=format_value,
    )
    selected_quality = quality or config.default_quality
    selected_dedupe = _dedupe_mode(config, dedupe)
    destination = (output or config.output_path).expanduser()
    adapter = _adapter(
        logger=logger,
        browser=browser,
        browser_profile=browser_profile,
        cookie_file=cookie_file,
        retries=retries,
    )
    if not urls:
        raise InputError("Provide at least one YouTube video, playlist, channel URL, or @handle")
    resolved_sources = tuple(SourceResolver().resolve(value) for value in urls)
    database = _database()

    single_downloader = SingleDownloadService(adapter)
    direct_coordinator = DirectDownloadCoordinator(
        single_downloader,
        IndexRepository(database),
        BasicDedupeService(database),
    )
    batch_failures: list[tuple[str, str]] = []
    video_sources = tuple(source for source in resolved_sources if source.kind is SourceKind.VIDEO)
    for video_position, source in enumerate(video_sources, start=1):
        request = SingleDownloadRequest(
            url=source.url,
            output_dir=destination,
            output_format=selected_format,
            quality=selected_quality,
        )
        try:
            console.print(f"Downloading as [bold]{selected_format.value.upper()}[/bold]…")
            disk_guard = DiskSpaceGuard(destination)
            disk_guard.ensure_space()
            with TerminalDownloadProgress(console) as progress:
                progress.observe(
                    source.source_key,
                    video_position,
                    len(video_sources),
                    {"status": "processing", "phase": "Preparing…"},
                )
                result = direct_coordinator.download(
                    source,
                    request,
                    dedupe_mode=selected_dedupe,
                    progress_hook=_combined_progress_hook(
                        disk_guard,
                        progress,
                        source.source_key,
                        video_position,
                        len(video_sources),
                    ),
                    postprocessor_hook=_combined_postprocessor_hook(
                        progress,
                        source.source_key,
                        video_position,
                        len(video_sources),
                        selected_format,
                    ),
                )
            if result.status is DirectDownloadStatus.SKIPPED_DUPLICATE:
                console.print(
                    f"[cyan]Already have[/cyan]  {result.title}",
                    highlight=False,
                )
            else:
                console.print(f"[green]Done[/green]  {result.title}", highlight=False)
        except KeyboardInterrupt:
            raise UserCancelledError() from None
        except MediaDLError as exc:
            if len(resolved_sources) == 1:
                raise
            batch_failures.append((source.url, str(exc)))
            console.print(f"[red]Failed[/red]  {source.url} · {exc}", highlight=False)

    collection_inputs = tuple(
        source for source in resolved_sources if source.kind is not SourceKind.VIDEO
    )
    if not collection_inputs:
        _raise_batch_failures(batch_failures)
        return

    if all_items and any(
        value is not None
        for value in (
            first,
            last,
            range_value,
            latest,
            oldest,
            most_viewed,
            least_viewed,
            most_liked,
            least_liked,
        )
    ):
        raise InputError("--all cannot be combined with another collection selector")

    query = compile_collection_query(
        CollectionQueryInput(
            first=first,
            last=last,
            range_value=range_value,
            latest=latest,
            oldest=oldest,
            most_viewed=most_viewed,
            least_viewed=least_viewed,
            most_liked=most_liked,
            least_liked=least_liked,
            sort_mode=sort_mode,
            views_above=views_above,
            views_below=views_below,
            views_between=views_between,
            likes_above=likes_above,
            likes_below=likes_below,
            likes_between=likes_between,
            after=after,
            before=before,
            title_contains=title_contains,
            duration_min=duration_min,
            duration_max=duration_max,
            where=tuple(where or ()),
        )
    )
    sources = tuple(
        expanded
        for source in collection_inputs
        for expanded in _collection_sources(
            source,
            videos=videos,
            shorts=shorts,
            streams=streams,
            everything=everything,
        )
    )

    jobs = JobRepository(database)
    jobs.recover_interrupted_jobs()
    workflow = CollectionWorkflow(
        CollectionScanner(adapter),
        IndexRepository(database),
        MetadataEnricher(adapter),
    )
    collection_downloader = SingleDownloadService(adapter)
    collection_dedupe = BasicDedupeService(database)
    smart_dedupe = _smart_dedupe(database)
    batch_mode = len(resolved_sources) > 1 or len(sources) > 1
    matched_collection_sources = 0
    no_match_skips: list[tuple[str, str]] = []
    prepared_plans: list[tuple[SourceDescriptor, DownloadPlan]] = []

    # Plan every selected collection first. This removes the dead time that used to
    # appear between playlist #1 and playlist #2, and lets one confirmation approve
    # the whole frozen batch rather than prompting again after each source finishes.
    for collection_source in sources:
        try:
            planning = (
                console.status(f"[cyan]Planning[/cyan] {collection_source.source_key}…")
                if _interactive()
                else nullcontext()
            )
            with planning:
                plan = workflow.prepare(
                    collection_source,
                    query,
                    output_format=selected_format,
                    quality=selected_quality,
                    dedupe_mode=selected_dedupe,
                    output_dir=destination,
                    preloaded_scan=_GUIDED_SCAN_CACHE.get(_guided_scan_key(collection_source)),
                )
            matched_collection_sources += 1
            prepared_plans.append((collection_source, plan))
            _render_plan(build_preview(plan), plan.plan_id)
        except NoMatchingMediaError as exc:
            if not batch_mode:
                raise
            label = exc.source_title or collection_source.url
            no_match_skips.append((label, str(exc)))
            console.print(f"[yellow]Skipped[/yellow]  {label} · {exc}", highlight=False)
        except MediaDLError as exc:
            if not batch_mode:
                raise
            batch_failures.append((collection_source.url, str(exc)))
            console.print(f"[red]Failed[/red]  {collection_source.url} · {exc}", highlight=False)

    if matched_collection_sources == 0 and no_match_skips and not batch_failures:
        raise InputError(
            "No selected sources matched the current selection/filter rules. "
            "Adjust the filters or choose another source."
        )

    if preview:
        _raise_batch_failures(batch_failures)
        return

    if prepared_plans and not yes and _interactive():
        prompt_text = (
            "Start all selected downloads?"
            if len(prepared_plans) > 1
            else "Start download?"
        )
        if not typer.confirm(prompt_text, default=True):
            console.print("Cancelled.")
            return

    for collection_source, plan in prepared_plans:
        jobs.create_job(plan)
        with TerminalDownloadProgress(console) as progress:
            summary = CollectionJobExecutor(
                jobs,
                collection_downloader,
                collection_dedupe,
                smart_dedupe=smart_dedupe,
                progress_observer=progress.observe,
                max_workers=_collection_worker_count(selected_format, parallel_jobs),
                conversion_workers=_collection_conversion_worker_count(selected_format),
            ).run(plan.plan_id)
        _render_execution(summary)
        issue = _execution_issue(summary)
        if issue is not None:
            if not batch_mode:
                raise MediaDLError(issue, 1)
            batch_failures.append((collection_source.url, issue))

    _raise_batch_failures(batch_failures)


@app.command()
def resume(
    job_id: Annotated[
        str | None, typer.Argument(help="Job ID. Defaults to latest resumable job.")
    ] = None,
    browser: Annotated[
        str | None,
        typer.Option("--cookies-from-browser", help="Use authorized browser cookies."),
    ] = None,
    browser_profile: Annotated[str | None, typer.Option("--browser-profile")] = None,
    cookie_file: Annotated[Path | None, typer.Option("--cookies")] = None,
    parallel_jobs: Annotated[
        int | None,
        typer.Option("--jobs", min=1, max=8, help="Parallel collection items."),
    ] = None,
) -> None:
    """Resume a paused or interrupted collection job."""

    config = ConfigStore().load()
    logger = configure_logging(verbose=config.verbose)
    database = _database()
    jobs = JobRepository(database)
    recovered = jobs.recover_interrupted_jobs()
    selected = job_id or jobs.latest_resumable_job()
    if selected is None:
        raise InputError("No resumable MediaDL job was found")
    if selected in recovered:
        console.print("Recovered interrupted job state.")
    adapter = _adapter(
        logger=logger,
        browser=browser,
        browser_profile=browser_profile,
        cookie_file=cookie_file,
        retries=5,
    )
    selected_format = OutputFormat(jobs.load_plan(selected).output_format)
    with TerminalDownloadProgress(console) as progress:
        summary = CollectionJobExecutor(
            jobs,
            SingleDownloadService(adapter),
            BasicDedupeService(database),
            smart_dedupe=_smart_dedupe(database),
            progress_observer=progress.observe,
            max_workers=_collection_worker_count(selected_format, parallel_jobs),
            conversion_workers=_collection_conversion_worker_count(selected_format),
        ).run(selected)
    _render_execution(summary)
    issue = _execution_issue(summary)
    if issue is not None:
        raise MediaDLError(issue, 1)


@app.command()
def retry(
    job_id: Annotated[
        str | None, typer.Argument(help="Job ID. Defaults to latest retryable job.")
    ] = None,
    parallel_jobs: Annotated[
        int | None,
        typer.Option("--jobs", min=1, max=8, help="Parallel collection items."),
    ] = None,
) -> None:
    """Retry retryable failures for a collection job."""

    config = ConfigStore().load()
    logger = configure_logging(verbose=config.verbose)
    database = _database()
    jobs = JobRepository(database)
    jobs.recover_interrupted_jobs()
    selected = job_id or jobs.latest_retryable_job()
    if selected is None:
        raise InputError("No retryable MediaDL job was found")
    count = jobs.retry_failures(selected)
    if count == 0:
        raise InputError(f"Job {selected} has no retryable failures")
    adapter = _adapter(
        logger=logger,
        browser=None,
        browser_profile=None,
        cookie_file=None,
        retries=5,
    )
    selected_format = OutputFormat(jobs.load_plan(selected).output_format)
    with TerminalDownloadProgress(console) as progress:
        summary = CollectionJobExecutor(
            jobs,
            SingleDownloadService(adapter),
            BasicDedupeService(database),
            smart_dedupe=_smart_dedupe(database),
            progress_observer=progress.observe,
            max_workers=_collection_worker_count(selected_format, parallel_jobs),
            conversion_workers=_collection_conversion_worker_count(selected_format),
        ).run(selected)
    _render_execution(summary)
    issue = _execution_issue(summary)
    if issue is not None:
        raise MediaDLError(issue, 1)


@app.command(name="recover-unavailable")
def recover_unavailable(
    job_id: Annotated[
        str, typer.Argument(help="Completed job ID whose unavailable items should be retried.")
    ],
    browser: Annotated[
        str | None,
        typer.Option("--cookies-from-browser", help="Use authorized browser cookies."),
    ] = None,
    browser_profile: Annotated[str | None, typer.Option("--browser-profile")] = None,
    cookie_file: Annotated[Path | None, typer.Option("--cookies")] = None,
    parallel_jobs: Annotated[
        int | None,
        typer.Option("--jobs", min=1, max=8, help="Parallel collection items."),
    ] = None,
) -> None:
    """Retry only items previously recorded as unavailable for one collection job."""

    config = ConfigStore().load()
    logger = configure_logging(verbose=config.verbose)
    database = _database()
    jobs = JobRepository(database)
    jobs.recover_interrupted_jobs()
    count = jobs.recover_unavailable(job_id)
    if count == 0:
        raise InputError(f"Job {job_id} has no unavailable items to recover")
    console.print(f"Recovering {count} previously unavailable item(s)…")
    adapter = _adapter(
        logger=logger,
        browser=browser,
        browser_profile=browser_profile,
        cookie_file=cookie_file,
        retries=5,
    )
    selected_format = OutputFormat(jobs.load_plan(job_id).output_format)
    with TerminalDownloadProgress(console) as progress:
        summary = CollectionJobExecutor(
            jobs,
            SingleDownloadService(adapter),
            BasicDedupeService(database),
            smart_dedupe=_smart_dedupe(database),
            progress_observer=progress.observe,
            max_workers=_collection_worker_count(selected_format, parallel_jobs),
            conversion_workers=_collection_conversion_worker_count(selected_format),
        ).run(job_id)
    _render_execution(summary)
    issue = _execution_issue(summary)
    if issue is not None:
        raise MediaDLError(issue, 1)


@app.command()
def history(
    limit: Annotated[int, typer.Option("--limit", min=1, max=200)] = 20,
) -> None:
    """Show recent persistent collection jobs."""

    records = JobRepository(_database()).list_jobs(limit=limit)
    if not records:
        console.print("No MediaDL jobs yet.")
        return
    table = Table(title="MediaDL Jobs", show_lines=False)
    table.add_column("Job")
    table.add_column("Source")
    table.add_column("Status")
    table.add_column("Done", justify="right")
    table.add_column("Skipped", justify="right")
    table.add_column("Failed", justify="right")
    for record in records:
        _add_history_row(table, record)
    console.print(table)


@app.command(name="job")
def job_details(
    job_id: Annotated[str, typer.Argument(help="Collection job ID to inspect.")],
) -> None:
    """Show the exact persisted status breakdown for one collection job."""

    breakdown = JobRepository(_database()).job_breakdown(job_id)
    _render_job_breakdown(breakdown)


@app.command()
def update(
    manifest: Annotated[
        str | None,
        typer.Option(
            "--manifest",
            help="Release manifest URL. Can also be set with MEDIADL_UPDATE_MANIFEST_URL.",
        ),
    ] = None,
    check_only: Annotated[
        bool,
        typer.Option("--check", help="Only check whether a newer standalone release exists."),
    ] = False,
) -> None:
    """Check for and install a checksum-verified standalone update."""

    manifest_url = manifest or os.environ.get("MEDIADL_UPDATE_MANIFEST_URL")
    if not manifest_url:
        raise InputError(
            "No MediaDL release feed is configured yet. Pass --manifest or set "
            "MEDIADL_UPDATE_MANIFEST_URL."
        )
    release = load_manifest(manifest_url)
    check = check_update(release, current_version=__version__)
    if not check.update_available:
        console.print(f"[green]Up to date[/green]  MediaDL {__version__}")
        return
    console.print(
        f"Update available: [bold]{check.current_version}[/bold] → "
        f"[bold]{check.latest_version}[/bold] ({check.platform_key})"
    )
    if check_only:
        return
    if not getattr(sys, "frozen", False):
        raise InputError(
            "Self-update is only available from the standalone MediaDL binary. "
            "This source/development install can use --check."
        )
    staged = stage_update(check)
    result = install_verified_update(staged)
    if result == "scheduled":
        console.print("[green]Update verified[/green]  It will finish after this command exits.")
    else:
        console.print(f"[green]Updated[/green]  MediaDL {check.latest_version}")


@app.command()
def doctor() -> None:
    """Check this MediaDL installation and its local runtime dependencies."""

    report = Doctor().run()
    table = Table(title="MediaDL Doctor", show_lines=False)
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail")
    style = {
        CheckStatus.OK: "green",
        CheckStatus.WARN: "yellow",
        CheckStatus.FAIL: "red",
    }
    for check in report.checks:
        table.add_row(
            check.name,
            f"[{style[check.status]}]{check.status.value}[/{style[check.status]}]",
            check.detail,
        )
    console.print(table)
    if report.healthy:
        suffix = f" · {report.warnings} optional warning(s)" if report.warnings else ""
        console.print(f"[green]Ready[/green]{suffix}")
        return
    raise typer.Exit(1)


@app.command(name="config")
def config_command(
    output: Annotated[Path | None, typer.Option("--output")] = None,
    format_value: Annotated[OutputFormat | None, typer.Option("--format")] = None,
    quality: Annotated[str | None, typer.Option("--quality")] = None,
    dedupe: Annotated[DedupeMode | None, typer.Option("--dedupe")] = None,
) -> None:
    """Show or update MediaDL defaults."""

    store = ConfigStore()
    config = store.load()
    changed = False
    if output is not None:
        config.output_dir = str(output.expanduser())
        changed = True
    if format_value is not None:
        config.default_format = format_value.value
        changed = True
    if quality is not None:
        if not quality.strip():
            raise InputError("Default quality cannot be empty")
        config.default_quality = quality.strip()
        changed = True
    if dedupe is not None:
        config.dedupe_mode = dedupe.value
        changed = True
    if changed:
        store.save(config)
        console.print("[green]Saved[/green] MediaDL defaults.")
    _render_config(config)


def _collection_sources(
    source: SourceDescriptor,
    *,
    videos: bool,
    shorts: bool,
    streams: bool,
    everything: bool,
) -> tuple[SourceDescriptor, ...]:
    flags = int(videos) + int(shorts) + int(streams) + int(everything)
    if flags > 1:
        raise InputError("Choose only one of --videos, --shorts, --streams, or --everything")

    tab_kinds = {
        SourceKind.CHANNEL_VIDEOS,
        SourceKind.CHANNEL_SHORTS,
        SourceKind.CHANNEL_STREAMS,
    }
    if source.kind is SourceKind.PLAYLIST or source.kind in tab_kinds:
        return (source,)
    if source.kind is not SourceKind.CHANNEL:
        raise InputError(f"Unsupported collection source type: {source.kind.value}")

    if everything:
        return (
            source.for_tab(SourceKind.CHANNEL_VIDEOS),
            source.for_tab(SourceKind.CHANNEL_SHORTS),
            source.for_tab(SourceKind.CHANNEL_STREAMS),
        )
    if shorts:
        return (source.for_tab(SourceKind.CHANNEL_SHORTS),)
    if streams:
        return (source.for_tab(SourceKind.CHANNEL_STREAMS),)
    if videos:
        return (source.for_tab(SourceKind.CHANNEL_VIDEOS),)

    if _interactive():
        console.print("[bold]Channel section[/bold]")
        console.print("1  Videos\n2  Shorts\n3  Streams\n4  Everything")
        choice = typer.prompt("Choose", default="1").strip()
        if choice == "2":
            return (source.for_tab(SourceKind.CHANNEL_SHORTS),)
        if choice == "3":
            return (source.for_tab(SourceKind.CHANNEL_STREAMS),)
        if choice == "4":
            return (
                source.for_tab(SourceKind.CHANNEL_VIDEOS),
                source.for_tab(SourceKind.CHANNEL_SHORTS),
                source.for_tab(SourceKind.CHANNEL_STREAMS),
            )
        if choice != "1":
            raise InputError("Channel section choice must be 1, 2, 3, or 4")
    return (source.for_tab(SourceKind.CHANNEL_VIDEOS),)


def _combined_progress_hook(
    disk_guard: DiskSpaceGuard,
    progress: TerminalDownloadProgress,
    label: str,
    position: int,
    total_items: int,
) -> ProgressHook:
    def hook(status: dict[str, object]) -> None:
        disk_guard.progress_hook(status)
        progress.observe(label, position, total_items, status)

    return hook


def _combined_postprocessor_hook(
    progress: TerminalDownloadProgress,
    label: str,
    position: int,
    total_items: int,
    output_format: OutputFormat,
) -> PostprocessorHook:
    phase = (
        f"Converting {output_format.value.upper()}…"
        if output_format.is_audio
        else f"Finalizing {output_format.value.upper()}…"
    )

    def hook(status: dict[str, object]) -> None:
        state = str(status.get("status") or "").casefold()
        if state in {"started", "processing"}:
            progress.observe(
                label,
                position,
                total_items,
                {"status": "processing", "phase": phase},
            )

    return hook


def _execution_issue(summary: ExecutionSummary) -> str | None:
    if summary.status is JobStatus.COMPLETED:
        return None
    if summary.status is JobStatus.PAUSED:
        return f"Job {summary.job_id} is paused and must be resumed to finish"
    if summary.status is JobStatus.COMPLETED_WITH_FAILURES:
        return f"Job {summary.job_id} completed with {summary.failed} failure(s)"
    if summary.status is JobStatus.CANCELLED:
        return f"Job {summary.job_id} was cancelled"
    return f"Job {summary.job_id} did not reach a completed state ({summary.status.value})"


def _raise_batch_failures(failures: list[tuple[str, str]]) -> None:
    if not failures:
        return
    console.print(f"[yellow]Batch finished with {len(failures)} incomplete source(s):[/yellow]")
    for source, message in failures:
        console.print(f"  {source} · {message}", highlight=False)
    raise MediaDLError(
        f"{len(failures)} source(s) did not complete; all remaining sources were still processed.",
        1,
    )


def _render_plan(preview: PlanPreview, plan_id: str) -> None:
    table = Table(title=preview.source, show_header=False, box=None)
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Selected", str(preview.selected_count))
    table.add_row("Selection", preview.selection)
    table.add_row("Sort", preview.sort)
    table.add_row("Output", f"{preview.output_format} · {preview.quality}")
    table.add_row("Duplicates", preview.dedupe)
    table.add_row("Destination", preview.destination)
    table.add_row("Estimated size", preview.estimated_size)
    table.add_row("Free space", preview.free_space)
    table.add_row("Job", plan_id)
    console.print(table)
    for warning in preview.warnings:
        console.print(f"[yellow]Warning:[/yellow] {warning}")


def _render_execution(summary: ExecutionSummary) -> None:
    style = "green" if summary.status is JobStatus.COMPLETED else "yellow"
    console.print(
        f"[{style}]{summary.status.value}[/{style}]  "
        f"downloaded={summary.completed} "
        f"duplicate={summary.skipped_duplicate} "
        f"unavailable={summary.skipped_unavailable} "
        f"failed={summary.failed}"
    )
    if summary.status is JobStatus.PAUSED:
        console.print(f"Resume later with [cyan]mdl resume {summary.job_id}[/cyan].")


def _render_job_breakdown(breakdown: JobBreakdown) -> None:
    table = Table(title=f"MediaDL Job {breakdown.job_id[:12]}", show_header=False, box=None)
    table.add_column(style="bold")
    table.add_column(justify="right")
    table.add_row("Status", breakdown.status.value)
    table.add_row("Total", str(breakdown.total))
    table.add_row("Downloaded", str(breakdown.completed))
    table.add_row("Duplicate", str(breakdown.skipped_duplicate))
    table.add_row("Unavailable", str(breakdown.skipped_unavailable))
    table.add_row("Retryable failed", str(breakdown.retryable_failed))
    table.add_row("Final failed", str(breakdown.final_failed))
    table.add_row("Pending", str(breakdown.pending))
    table.add_row("Downloading", str(breakdown.downloading))
    table.add_row("Postprocessing", str(breakdown.postprocessing))
    table.add_row("Verifying", str(breakdown.verifying))
    table.add_row("Cancelled", str(breakdown.cancelled))
    console.print(table)
    if breakdown.unavailable_reasons:
        reasons = Table(title="Unavailable reasons", show_header=True, box=None)
        reasons.add_column("Count", justify="right")
        reasons.add_column("Reason")
        for reason, count in breakdown.unavailable_reasons:
            reasons.add_row(str(count), reason)
        console.print(reasons)


def _add_history_row(table: Table, record: JobRecord) -> None:
    source = record.source_title or record.source_key or "Unknown"
    table.add_row(
        record.job_id[:12],
        source,
        record.status.value,
        f"{record.completed}/{record.total}",
        str(record.skipped),
        str(record.retryable_failed + record.final_failed),
    )


def _render_config(config: AppConfig) -> None:
    table = Table(title="MediaDL Defaults", show_header=False, box=None)
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Output", config.output_dir)
    table.add_row("Format", config.default_format)
    table.add_row("Quality", config.default_quality)
    table.add_row("Duplicates", config.dedupe_mode)
    console.print(table)


_KNOWN_COMMANDS = {
    "download",
    "resume",
    "retry",
    "recover-unavailable",
    "history",
    "job",
    "update",
    "doctor",
    "config",
}
_ROOT_ONLY_FLAGS = {"--help", "-h", "--version", "-V"}


def _normalized_argv(argv: list[str]) -> list[str]:
    """Preserve `mdl URL ...` while keeping real subcommands unambiguous."""

    if not argv:
        return []
    first = argv[0]
    if first in _KNOWN_COMMANDS or first in _ROOT_ONLY_FLAGS:
        return list(argv)
    return ["download", *argv]


def main() -> None:
    """Console-script wrapper with guided bare-``mdl`` mode and stable errors."""

    try:
        argv = list(sys.argv[1:])
        _GUIDED_SCAN_CACHE.clear()
        if not argv and _interactive():
            argv = build_guided_argv(
                console,
                section_probe=_guided_section_probe,
                item_count_probe=_guided_item_count_probe,
                playlist_discovery=_guided_playlist_discovery,
            )
        app(args=_normalized_argv(argv))
    except typer.Exit as exc:
        raise SystemExit(exc.exit_code) from None
    except KeyboardInterrupt:
        console.print("[yellow]Cancelled by user.[/yellow]", highlight=False)
        raise SystemExit(130) from None
    except UserCancelledError as exc:
        console.print(f"[yellow]{exc}[/yellow]", highlight=False)
        raise SystemExit(exc.exit_code) from None
    except MediaDLError as exc:
        console.print(f"[red]Error:[/red] {exc}", highlight=False)
        raise SystemExit(exc.exit_code) from None


if __name__ == "__main__":
    main()
