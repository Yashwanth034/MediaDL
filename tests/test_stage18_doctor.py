from pathlib import Path

from mediadl.core.doctor import CheckStatus, Doctor
from mediadl.core.paths import AppPaths


def paths(tmp_path: Path) -> AppPaths:
    return AppPaths(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "log",
        default_output_dir=tmp_path / "downloads",
    )


def test_doctor_initializes_and_validates_isolated_runtime(tmp_path: Path) -> None:
    report = Doctor(
        paths=paths(tmp_path),
        dependency_finder=lambda command: f"/mock/{command}",
        version_reader=lambda path: "deno 2.4.5" if path.endswith("deno") else "v22.12.0",
        ejs_available=lambda: True,
    ).run()

    assert report.healthy
    by_name = {check.name: check for check in report.checks}
    assert by_name["yt-dlp"].status is CheckStatus.OK
    assert by_name["yt-dlp EJS"].status is CheckStatus.OK
    assert by_name["JS runtime"].status is CheckStatus.OK
    assert "deno" in by_name["JS runtime"].detail
    assert "2.4.5" in by_name["JS runtime"].detail
    assert by_name["FFmpeg"].status is CheckStatus.OK
    assert by_name["FFprobe"].status is CheckStatus.OK
    assert by_name["Database"].status is CheckStatus.OK
    assert "integrity ok" in by_name["Database"].detail
    assert by_name["Output"].status is CheckStatus.OK
    assert (tmp_path / "data" / "mediadl.sqlite3").is_file()
    assert (tmp_path / "downloads").is_dir()


def test_missing_runtime_and_ffmpeg_are_reported_with_required_ffmpeg_failure(
    tmp_path: Path,
) -> None:
    report = Doctor(
        paths=paths(tmp_path),
        dependency_finder=lambda _: None,
        ejs_available=lambda: True,
    ).run()

    assert not report.healthy
    assert report.warnings == 2
    by_name = {check.name: check for check in report.checks}
    assert by_name["JS runtime"].status is CheckStatus.WARN
    assert "requires a supported" in by_name["JS runtime"].detail
    assert by_name["FFmpeg"].status is CheckStatus.FAIL
    assert by_name["FFprobe"].status is CheckStatus.WARN
    assert "conversion/merge" in by_name["FFmpeg"].detail


def test_doctor_reports_installed_node_20_as_unsupported(tmp_path: Path) -> None:
    report = Doctor(
        paths=paths(tmp_path),
        dependency_finder={"node": "/usr/bin/node"}.get,
        version_reader=lambda _: "v20.20.2",
        ejs_available=lambda: True,
    ).run()

    by_name = {check.name: check for check in report.checks}
    assert by_name["JS runtime"].status is CheckStatus.WARN
    assert "node v20.20.2 unsupported" in by_name["JS runtime"].detail
    assert ">=22.0.0" in by_name["JS runtime"].detail


def test_missing_ejs_is_a_broken_youtube_install(tmp_path: Path) -> None:
    report = Doctor(
        paths=paths(tmp_path),
        dependency_finder=lambda _: None,
        ejs_available=lambda: False,
    ).run()

    assert not report.healthy
    by_name = {check.name: check for check in report.checks}
    assert by_name["yt-dlp EJS"].status is CheckStatus.FAIL
