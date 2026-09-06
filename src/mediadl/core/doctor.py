"""Local installation diagnostics for MediaDL."""

from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from mediadl.core.config import ConfigStore
from mediadl.core.paths import AppPaths, get_app_paths
from mediadl.engines.js_runtime import VersionReader, detect_js_runtime, probe_js_runtimes
from mediadl.storage.database import Database

DependencyFinder = Callable[[str], str | None]
EjsAvailabilityChecker = Callable[[], bool]


class CheckStatus(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    name: str
    status: CheckStatus
    detail: str
    required: bool = True


@dataclass(frozen=True, slots=True)
class DoctorReport:
    checks: tuple[DoctorCheck, ...]

    @property
    def healthy(self) -> bool:
        return not any(check.required and check.status is CheckStatus.FAIL for check in self.checks)

    @property
    def warnings(self) -> int:
        return sum(check.status is CheckStatus.WARN for check in self.checks)


class Doctor:
    def __init__(
        self,
        *,
        paths: AppPaths | None = None,
        dependency_finder: DependencyFinder = shutil.which,
        version_reader: VersionReader | None = None,
        ejs_available: EjsAvailabilityChecker | None = None,
    ) -> None:
        self.paths = paths or get_app_paths()
        self.dependency_finder = dependency_finder
        self.version_reader = version_reader
        self.ejs_available = ejs_available or _yt_dlp_ejs_available

    def run(self) -> DoctorReport:
        checks = [
            DoctorCheck(
                "Platform",
                CheckStatus.OK,
                f"{platform.system()} {platform.machine()} · Python {platform.python_version()}",
            ),
            DoctorCheck("Executable", CheckStatus.OK, str(Path(sys.executable))),
            self._yt_dlp_check(),
            self._ejs_check(),
            self._js_runtime_check(),
            self._dependency_check("FFmpeg", "ffmpeg", required=True),
            self._dependency_check("FFprobe", "ffprobe", required=False),
            self._runtime_dirs_check(),
            self._database_check(),
            self._output_check(),
        ]
        return DoctorReport(tuple(checks))

    def _yt_dlp_check(self) -> DoctorCheck:
        try:
            from yt_dlp.version import __version__ as yt_dlp_version
        except Exception as exc:  # pragma: no cover - installation breakage path
            return DoctorCheck("yt-dlp", CheckStatus.FAIL, f"unavailable: {exc}")
        return DoctorCheck("yt-dlp", CheckStatus.OK, str(yt_dlp_version))

    def _ejs_check(self) -> DoctorCheck:
        if self.ejs_available():
            return DoctorCheck("yt-dlp EJS", CheckStatus.OK, "challenge solver available")
        return DoctorCheck(
            "yt-dlp EJS",
            CheckStatus.FAIL,
            "missing; YouTube challenge solving is not reliable in this installation",
        )

    def _js_runtime_check(self) -> DoctorCheck:
        runtime = detect_js_runtime(self.dependency_finder, self.version_reader)
        if runtime is not None:
            version = f" · {runtime.version}" if runtime.version else ""
            location = "bundled with MediaDL" if runtime.bundled else runtime.executable
            return DoctorCheck(
                "JS runtime",
                CheckStatus.OK,
                f"{runtime.name}{version} · {location}",
                required=False,
            )
        probes = probe_js_runtimes(self.dependency_finder, self.version_reader)
        if probes:
            detail = "; ".join(
                f"{probe.name} {probe.version or 'unknown version'} unsupported "
                f"(requires {probe.requirement})"
                for probe in probes
            )
        else:
            detail = "not found; YouTube extraction requires a supported JavaScript runtime"
        return DoctorCheck(
            "JS runtime",
            CheckStatus.WARN,
            detail,
            required=False,
        )

    def _dependency_check(self, label: str, command: str, *, required: bool) -> DoctorCheck:
        path = self.dependency_finder(command)
        if path is not None:
            return DoctorCheck(label, CheckStatus.OK, path, required=required)
        detail = "not found; required for conversion/merge and smart fingerprinting"
        status = CheckStatus.FAIL if required else CheckStatus.WARN
        return DoctorCheck(label, status, detail, required=required)

    def _runtime_dirs_check(self) -> DoctorCheck:
        try:
            self.paths.ensure_runtime_dirs()
            for directory in (self.paths.config_dir, self.paths.data_dir, self.paths.log_dir):
                _verify_writable_directory(directory)
        except OSError as exc:
            return DoctorCheck("App data", CheckStatus.FAIL, str(exc))
        return DoctorCheck("App data", CheckStatus.OK, str(self.paths.data_dir))

    def _database_check(self) -> DoctorCheck:
        try:
            database = Database(self.paths.database_file)
            version = database.initialize()
            with database.connection() as connection:
                integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        except Exception as exc:
            return DoctorCheck("Database", CheckStatus.FAIL, str(exc))
        status = CheckStatus.OK if integrity.casefold() == "ok" else CheckStatus.FAIL
        return DoctorCheck("Database", status, f"schema {version} · integrity {integrity}")

    def _output_check(self) -> DoctorCheck:
        try:
            output = ConfigStore(self.paths).load().output_path
            output.mkdir(parents=True, exist_ok=True)
            _verify_writable_directory(output)
        except Exception as exc:
            return DoctorCheck("Output", CheckStatus.FAIL, str(exc))
        free = shutil.disk_usage(output).free
        return DoctorCheck("Output", CheckStatus.OK, f"{output} · {_human_bytes(free)} free")


def _yt_dlp_ejs_available() -> bool:
    return importlib.util.find_spec("yt_dlp_ejs") is not None


def _verify_writable_directory(directory: Path) -> None:
    probe = directory / f".mediadl-write-test-{os.getpid()}"
    try:
        probe.write_bytes(b"")
    finally:
        probe.unlink(missing_ok=True)


def _human_bytes(value: int) -> str:
    amount = float(max(value, 0))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            if unit == "B":
                return f"{int(amount)} {unit}"
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} TiB"
