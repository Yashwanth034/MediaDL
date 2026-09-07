from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import mediadl.cli.app as cli_app
from mediadl.cli.collection import CollectionQuery
from mediadl.core.config import AppConfig
from mediadl.core.formats import OutputFormat
from mediadl.core.policies import DedupeMode
from mediadl.downloads.executor import ExecutionSummary
from mediadl.downloads.jobs import JobRecord, JobStatus
from mediadl.downloads.plan import DiskSpace, DownloadPlanBuilder
from mediadl.sources.models import MediaItemStub, SourceKind
from mediadl.sources.resolver import SourceResolver


def test_public_argv_alias_preserves_simple_url_syntax_and_subcommands() -> None:
    url = "https://www.youtube.com/watch?v=abc123"

    assert cli_app._normalized_argv([url, "--mp3"]) == ["download", url, "--mp3"]
    assert cli_app._normalized_argv(["--mp3", url]) == ["download", "--mp3", url]
    assert cli_app._normalized_argv(["history"]) == ["history"]
    assert cli_app._normalized_argv(["resume", "job-1"]) == ["resume", "job-1"]
    assert cli_app._normalized_argv(["recover-unavailable", "job-1"]) == [
        "recover-unavailable",
        "job-1",
    ]
    assert cli_app._normalized_argv(["recover-failed", "job-1"]) == [
        "recover-failed",
        "job-1",
    ]
    assert cli_app._normalized_argv(["--help"]) == ["--help"]
    assert cli_app._normalized_argv(["--version"]) == ["--version"]
    assert cli_app._normalized_argv([]) == []


def test_collection_worker_defaults_are_bounded_by_output_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_app.os, "cpu_count", lambda: 12)

    assert cli_app._collection_worker_count(OutputFormat.M4A, None) == 4
    assert cli_app._collection_worker_count(OutputFormat.OPUS, None) == 4
    assert cli_app._collection_worker_count(OutputFormat.MP3, None) == 3
    assert cli_app._collection_worker_count(OutputFormat.WAV, None) == 3
    assert cli_app._collection_worker_count(OutputFormat.MP4, None) == 3
    assert cli_app._collection_worker_count(OutputFormat.M4A, None, authenticated=True) == 2
    assert cli_app._collection_worker_count(OutputFormat.MP4, None, authenticated=True) == 2
    assert cli_app._collection_worker_count(OutputFormat.M4A, 6, authenticated=True) == 6
    assert cli_app._collection_worker_count(OutputFormat.M4A, 6) == 6
    assert cli_app._collection_conversion_worker_count(OutputFormat.MP3) == 2
    assert cli_app._collection_conversion_worker_count(OutputFormat.FLAC) == 2
    assert cli_app._collection_conversion_worker_count(OutputFormat.M4A) == 1


def test_channel_root_defaults_to_videos_when_noninteractive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = SourceResolver().resolve("@Example")
    monkeypatch.setattr(cli_app, "_interactive", lambda: False)

    resolved = cli_app._collection_sources(
        source,
        videos=False,
        shorts=False,
        streams=False,
        everything=False,
    )

    assert len(resolved) == 1
    assert resolved[0].kind is SourceKind.CHANNEL_VIDEOS


def test_channel_everything_expands_to_all_three_tabs() -> None:
    source = SourceResolver().resolve("@Example")

    resolved = cli_app._collection_sources(
        source,
        videos=False,
        shorts=False,
        streams=False,
        everything=True,
    )

    assert [item.kind for item in resolved] == [
        SourceKind.CHANNEL_VIDEOS,
        SourceKind.CHANNEL_SHORTS,
        SourceKind.CHANNEL_STREAMS,
    ]


def test_global_channel_tab_choice_does_not_override_explicit_tab_source() -> None:
    source = SourceResolver().resolve("https://www.youtube.com/@Example/shorts")

    resolved = cli_app._collection_sources(
        source,
        videos=True,
        shorts=False,
        streams=False,
        everything=False,
    )

    assert resolved == (source,)


def _plan_for(source: Any, query: CollectionQuery, output_dir: Path):
    return DownloadPlanBuilder(
        disk_probe=lambda _: DiskSpace(total=10_000, used=100, free=9_900),
        now_provider=lambda: datetime(2026, 9, 5, tzinfo=UTC),
        id_provider=lambda: "cli-stage16-plan",
    ).build(
        source=source,
        items=[
            MediaItemStub(
                media_key="abc",
                title="Example",
                url="https://www.youtube.com/watch?v=abc",
                view_count=5_000_000,
                like_count=200_000,
                upload_date="20250101",
                duration_seconds=600,
            )
        ],
        output_format=OutputFormat.MP4,
        quality="best",
        dedupe_mode=DedupeMode.SAFE,
        output_dir=output_dir,
        selection_label=query.selection_label,
        sort_mode=query.sort_mode.value,
    )


class FakeConfigStore:
    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def load(self) -> AppConfig:
        return AppConfig(output_dir="/tmp/mediadl-stage16")


class FakeJobs:
    created: list[str] = []

    def __init__(self, database: object) -> None:
        self.database = database

    def recover_interrupted_jobs(self) -> tuple[str, ...]:
        return ()

    def create_job(self, plan: Any) -> str:
        type(self).created.append(plan.plan_id)
        return plan.plan_id


class FakeExecutor:
    runs: list[str] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def run(self, job_id: str) -> ExecutionSummary:
        type(self).runs.append(job_id)
        return ExecutionSummary(
            job_id=job_id,
            status=JobStatus.COMPLETED,
            completed=1,
            skipped=0,
            failed=0,
        )


def test_recover_unavailable_cli_requeues_only_selected_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RecoveryJobs:
        def __init__(self, database: object) -> None:
            pass

        def recover_interrupted_jobs(self) -> tuple[str, ...]:
            return ()

        def recover_unavailable(self, job_id: str) -> int:
            assert job_id == "old-job"
            return 3

        def load_plan(self, job_id: str) -> object:
            assert job_id == "old-job"
            return type("Plan", (), {"output_format": OutputFormat.M4A.value})()

    FakeExecutor.runs = []
    monkeypatch.setattr(cli_app, "ConfigStore", FakeConfigStore)
    monkeypatch.setattr(cli_app, "configure_logging", lambda **_: object())
    monkeypatch.setattr(cli_app, "_database", lambda: object())
    monkeypatch.setattr(cli_app, "JobRepository", RecoveryJobs)
    monkeypatch.setattr(cli_app, "_adapter", lambda **_: object())
    monkeypatch.setattr(cli_app, "SingleDownloadService", lambda adapter: object())
    monkeypatch.setattr(cli_app, "BasicDedupeService", lambda database: object())
    monkeypatch.setattr(cli_app, "_smart_dedupe", lambda database: None)
    monkeypatch.setattr(cli_app, "CollectionJobExecutor", FakeExecutor)

    result = CliRunner().invoke(cli_app.app, ["recover-unavailable", "old-job"])

    assert result.exit_code == 0, result.output
    assert "Recovering 3 previously unavailable item(s)" in result.stdout
    assert "downloaded=1" in result.stdout
    assert FakeExecutor.runs == ["old-job"]


def test_recover_failed_cli_requeues_only_final_failures_with_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RecoveryJobs:
        def __init__(self, database: object) -> None:
            pass

        def recover_interrupted_jobs(self) -> tuple[str, ...]:
            return ()

        def recover_final_failures(self, job_id: str) -> int:
            assert job_id == "old-job"
            return 41

        def load_plan(self, job_id: str) -> object:
            assert job_id == "old-job"
            return type("Plan", (), {"output_format": OutputFormat.M4A.value})()

    FakeExecutor.runs = []
    monkeypatch.setattr(cli_app, "ConfigStore", FakeConfigStore)
    monkeypatch.setattr(cli_app, "configure_logging", lambda **_: object())
    monkeypatch.setattr(cli_app, "_database", lambda: object())
    monkeypatch.setattr(cli_app, "JobRepository", RecoveryJobs)
    monkeypatch.setattr(cli_app, "_adapter", lambda **_: object())
    monkeypatch.setattr(cli_app, "SingleDownloadService", lambda adapter: object())
    monkeypatch.setattr(cli_app, "BasicDedupeService", lambda database: object())
    monkeypatch.setattr(cli_app, "_smart_dedupe", lambda database: None)
    monkeypatch.setattr(cli_app, "CollectionJobExecutor", FakeExecutor)

    result = CliRunner().invoke(
        cli_app.app,
        ["recover-failed", "old-job", "--cookies-from-browser", "chrome"],
    )

    assert result.exit_code == 0, result.output
    assert "Recovering 41 final-failed item(s)" in result.stdout
    assert "downloaded=1" in result.stdout
    assert FakeExecutor.runs == ["old-job"]


def _patch_collection_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    captured_queries: list[CollectionQuery],
) -> None:
    class FakeWorkflow:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def prepare(
            self,
            source: Any,
            query: CollectionQuery,
            **kwargs: Any,
        ) -> Any:
            captured_queries.append(query)
            return _plan_for(source, query, tmp_path / "downloads")

    FakeJobs.created = []
    FakeExecutor.runs = []
    monkeypatch.setattr(cli_app, "ConfigStore", FakeConfigStore)
    monkeypatch.setattr(cli_app, "configure_logging", lambda **_: object())
    monkeypatch.setattr(cli_app, "_adapter", lambda **_: object())
    monkeypatch.setattr(cli_app, "_database", lambda: object())
    monkeypatch.setattr(cli_app, "JobRepository", FakeJobs)
    monkeypatch.setattr(cli_app, "CollectionWorkflow", FakeWorkflow)
    monkeypatch.setattr(cli_app, "CollectionScanner", lambda adapter: object())
    monkeypatch.setattr(cli_app, "IndexRepository", lambda database: object())
    monkeypatch.setattr(cli_app, "MetadataEnricher", lambda adapter: object())
    monkeypatch.setattr(cli_app, "CollectionJobExecutor", FakeExecutor)
    monkeypatch.setattr(cli_app, "SingleDownloadService", lambda adapter: object())
    monkeypatch.setattr(cli_app, "BasicDedupeService", lambda database: object())
    monkeypatch.setattr(cli_app, "_interactive", lambda: False)


def test_collection_cli_compiles_views_shorthand_and_preview_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: list[CollectionQuery] = []
    _patch_collection_runtime(monkeypatch, tmp_path, captured)

    result = CliRunner().invoke(
        cli_app.app,
        [
            "download",
            "https://www.youtube.com/@Example/videos",
            "--views-above",
            "10L",
            "--views-below",
            "1Cr",
            "--most-viewed",
            "5",
            "--preview",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Selected" in result.stdout
    assert captured[0].selection_label == "Most viewed 5"
    assert captured[0].filter_spec.views is not None
    assert captured[0].filter_spec.views.matches(5_000_000)
    assert not captured[0].filter_spec.views.matches(1_000_000)
    assert FakeJobs.created == []
    assert FakeExecutor.runs == []


def test_collection_cli_yes_creates_and_executes_frozen_job(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: list[CollectionQuery] = []
    _patch_collection_runtime(monkeypatch, tmp_path, captured)

    result = CliRunner().invoke(
        cli_app.app,
        [
            "download",
            "https://www.youtube.com/@Example",
            "--shorts",
            "--latest",
            "1",
            "--yes",
        ],
    )

    assert result.exit_code == 0, result.output
    assert FakeJobs.created == ["cli-stage16-plan"]
    assert FakeExecutor.runs == ["cli-stage16-plan"]
    assert "completed" in result.stdout
    assert captured[0].selection_label == "Latest 1"


def test_multiple_channels_can_be_planned_in_one_command_with_shared_custom_filters(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: list[CollectionQuery] = []
    _patch_collection_runtime(monkeypatch, tmp_path, captured)

    result = CliRunner().invoke(
        cli_app.app,
        [
            "download",
            "https://www.youtube.com/@ChannelOne",
            "https://www.youtube.com/@ChannelTwo",
            "--videos",
            "--where",
            "views>=12.5L",
            "--where",
            "duration<20m",
            "--preview",
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(captured) == 2
    assert all(query.generic_filters for query in captured)
    assert result.stdout.count("Selected") == 2
    assert FakeJobs.created == []
    assert FakeExecutor.runs == []


def test_multi_source_download_confirms_once_for_the_whole_prepared_batch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: list[CollectionQuery] = []
    _patch_collection_runtime(monkeypatch, tmp_path, captured)
    monkeypatch.setattr(cli_app, "_interactive", lambda: True)

    result = CliRunner().invoke(
        cli_app.app,
        [
            "download",
            "https://www.youtube.com/playlist?list=PLone",
            "https://www.youtube.com/playlist?list=PLtwo",
            "--first",
            "1",
        ],
        input="y\n",
    )

    assert result.exit_code == 0, result.output
    assert len(captured) == 2
    assert len(FakeJobs.created) == 2
    assert len(FakeExecutor.runs) == 2
    assert result.stdout.count("Start all selected downloads?") == 1
    assert result.stdout.count("completed") == 2


def test_multi_source_batch_continues_after_one_source_planning_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: list[CollectionQuery] = []
    _patch_collection_runtime(monkeypatch, tmp_path, captured)

    class FlakyWorkflow:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def prepare(
            self,
            source: Any,
            query: CollectionQuery,
            **kwargs: Any,
        ) -> Any:
            if source.source_key == "@BadChannel":
                raise cli_app.InputError("synthetic source failure")
            captured.append(query)
            return _plan_for(source, query, tmp_path / "downloads")

    monkeypatch.setattr(cli_app, "CollectionWorkflow", FlakyWorkflow)

    result = CliRunner().invoke(
        cli_app.app,
        [
            "download",
            "https://www.youtube.com/@BadChannel",
            "https://www.youtube.com/@GoodChannel",
            "--videos",
            "--preview",
        ],
    )

    assert result.exit_code == 1
    assert "Failed" in result.stdout
    assert "synthetic source failure" in result.stdout
    assert "Selected" in result.stdout
    assert "Batch finished with 1 incomplete source" in result.stdout
    assert len(captured) == 1


def test_multi_source_zero_filter_matches_is_clean_skip_not_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: list[CollectionQuery] = []
    _patch_collection_runtime(monkeypatch, tmp_path, captured)

    class MixedWorkflow:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def prepare(
            self,
            source: Any,
            query: CollectionQuery,
            **kwargs: Any,
        ) -> Any:
            if source.source_key == "PLempty":
                raise cli_app.NoMatchingMediaError(
                    "0 of 2 available item(s) matched: all are longer than the 30m maximum "
                    "(shortest is 43m 50s).",
                    source_title="Long playlist",
                    available_count=2,
                )
            captured.append(query)
            return _plan_for(source, query, tmp_path / "downloads")

    monkeypatch.setattr(cli_app, "CollectionWorkflow", MixedWorkflow)

    result = CliRunner().invoke(
        cli_app.app,
        [
            "download",
            "https://www.youtube.com/playlist?list=PLgood",
            "https://www.youtube.com/playlist?list=PLempty",
            "--duration-max",
            "30m",
            "--preview",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Selected" in result.stdout
    assert "Skipped" in result.stdout
    assert "Long playlist" in result.stdout
    assert "all are longer than" in result.stdout
    assert "the 30m maximum" in result.stdout
    assert "Failed" not in result.stdout
    assert "incomplete source" not in result.stdout


def test_playlist_is_not_broken_by_global_channel_tab_choice_in_multi_source_batch() -> None:
    playlist = SourceResolver().resolve("https://www.youtube.com/playlist?list=PL123")

    resolved = cli_app._collection_sources(
        playlist,
        videos=False,
        shorts=True,
        streams=False,
        everything=False,
    )

    assert resolved == (playlist,)


def test_history_subcommand_renders_persistent_job_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = JobRecord(
        job_id="1234567890abcdef",
        status=JobStatus.COMPLETED,
        source_title="Example",
        source_key="@Example",
        created_at="2026-09-05",
        updated_at="2026-09-05",
        completed_at="2026-09-05",
        total=10,
        completed=8,
        skipped=2,
        retryable_failed=0,
        final_failed=0,
    )

    class HistoryJobs:
        def __init__(self, database: object) -> None:
            pass

        def list_jobs(self, *, limit: int) -> tuple[JobRecord, ...]:
            assert limit == 20
            return (record,)

    monkeypatch.setattr(cli_app, "_database", lambda: object())
    monkeypatch.setattr(cli_app, "JobRepository", HistoryJobs)

    result = CliRunner().invoke(cli_app.app, ["history"])

    assert result.exit_code == 0, result.output
    assert "MediaDL Jobs" in result.stdout
    assert "Example" in result.stdout
    assert "8/10" in result.stdout
