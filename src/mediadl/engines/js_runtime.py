"""Supported JavaScript runtime discovery for modern yt-dlp YouTube extraction."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

DependencyFinder = Callable[[str], str | None]
VersionReader = Callable[[str], str | None]

# yt-dlp/EJS requirements as of the 2026.06+ releases. Prefer Deno, then Node,
# then QuickJS. Bun remains supported only in a narrow, deprecated version range.
_RUNTIME_CANDIDATES = (
    ("deno", "deno", (2, 3, 0), None),
    ("node", "node", (22, 0, 0), None),
    ("quickjs", "qjs", (2023, 12, 9), None),
    ("bun", "bun", (1, 2, 11), (1, 3, 14)),
)
_VERSION_RE = re.compile(r"(?<!\d)(\d+)(?:[.\-](\d+))?(?:[.\-](\d+))?")


@dataclass(frozen=True, slots=True)
class JavaScriptRuntime:
    name: str
    executable: str
    version: str | None = None
    bundled: bool = False

    def ytdlp_options(self) -> dict[str, object]:
        return {"js_runtimes": {self.name: {"path": self.executable}}}


@dataclass(frozen=True, slots=True)
class JavaScriptRuntimeProbe:
    name: str
    executable: str
    version: str | None
    supported: bool
    requirement: str
    bundled: bool = False


def probe_js_runtimes(
    dependency_finder: DependencyFinder = shutil.which,
    version_reader: VersionReader | None = None,
) -> tuple[JavaScriptRuntimeProbe, ...]:
    """Inspect bundled/system candidate runtimes and record yt-dlp compatibility."""

    reader = version_reader or _read_runtime_version
    probes: list[JavaScriptRuntimeProbe] = []
    seen: set[str] = set()
    bundled_deno = _bundled_deno_path()
    for runtime_name, command, minimum, maximum in _RUNTIME_CANDIDATES:
        candidates: list[tuple[str, bool]] = []
        if runtime_name == "deno" and bundled_deno is not None:
            candidates.append((bundled_deno, True))
        system_executable = dependency_finder(command)
        if system_executable:
            candidates.append((system_executable, False))
        for executable, bundled in candidates:
            normalized = str(Path(executable).resolve(strict=False))
            if normalized in seen:
                continue
            seen.add(normalized)
            version_text = reader(executable)
            parsed = _parse_version(version_text)
            supported = parsed is not None and parsed >= minimum
            if maximum is not None:
                supported = supported and parsed is not None and parsed <= maximum
            probes.append(
                JavaScriptRuntimeProbe(
                    name=runtime_name,
                    executable=executable,
                    version=version_text,
                    supported=supported,
                    requirement=_requirement_text(minimum, maximum),
                    bundled=bundled,
                )
            )
    return tuple(probes)


def detect_js_runtime(
    dependency_finder: DependencyFinder = shutil.which,
    version_reader: VersionReader | None = None,
) -> JavaScriptRuntime | None:
    """Return the first bundled/system runtime satisfying current yt-dlp/EJS requirements."""

    for probe in probe_js_runtimes(dependency_finder, version_reader):
        if probe.supported:
            return JavaScriptRuntime(
                probe.name,
                probe.executable,
                probe.version,
                bundled=probe.bundled,
            )
    return None


def _bundled_deno_path() -> str | None:
    """Locate Deno embedded by PyInstaller without depending on the user's PATH."""

    bundle_root = getattr(sys, "_MEIPASS", None)
    if not bundle_root:
        return None
    executable_name = "deno.exe" if os.name == "nt" else "deno"
    candidate = Path(bundle_root) / "mediadl_runtime" / executable_name
    if not candidate.is_file():
        return None
    if os.name != "nt":
        with suppress(OSError):
            candidate.chmod(candidate.stat().st_mode | 0o111)
    return str(candidate)


def _read_runtime_version(executable: str) -> str | None:
    try:
        completed = subprocess.run(
            [executable, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = (completed.stdout or completed.stderr or "").strip()
    if not output:
        return None
    return output.splitlines()[0].strip()


def _parse_version(value: str | None) -> tuple[int, int, int] | None:
    if not value:
        return None
    match = _VERSION_RE.search(value)
    if match is None:
        return None
    return tuple(int(part or 0) for part in match.groups())  # type: ignore[return-value]


def _requirement_text(
    minimum: tuple[int, int, int],
    maximum: tuple[int, int, int] | None,
) -> str:
    minimum_text = ".".join(str(part) for part in minimum)
    if maximum is None:
        return f">={minimum_text}"
    maximum_text = ".".join(str(part) for part in maximum)
    return f">={minimum_text}, <={maximum_text}"
