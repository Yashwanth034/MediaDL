"""Checksum-verified standalone MediaDL release updater."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mediadl.core.errors import InputError

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")
DEFAULT_UPDATE_MANIFEST_URL = (
    "https://github.com/Yashwanth034/MediaDL/releases/latest/download/manifest.json"
)
AUTO_UPDATE_INTERVAL_SECONDS = 6 * 60 * 60


@dataclass(frozen=True, slots=True)
class ReleaseAsset:
    url: str
    sha256: str


@dataclass(frozen=True, slots=True)
class ReleaseManifest:
    version: str
    assets: dict[str, ReleaseAsset]

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ReleaseManifest:
        version = str(payload.get("version", "")).strip()
        _version_tuple(version)
        raw_assets = payload.get("assets")
        if not isinstance(raw_assets, dict) or not raw_assets:
            raise InputError("Update manifest must contain a non-empty assets object")
        assets: dict[str, ReleaseAsset] = {}
        for key, raw in raw_assets.items():
            if not isinstance(raw, dict):
                raise InputError(f"Invalid update asset entry: {key}")
            url = str(raw.get("url", "")).strip()
            sha256 = str(raw.get("sha256", "")).strip().lower()
            if not url.startswith(("https://", "file://")):
                raise InputError(f"Update asset URL must use HTTPS or file://: {key}")
            if _SHA256_RE.fullmatch(sha256) is None:
                raise InputError(f"Update asset SHA-256 is invalid: {key}")
            assets[str(key)] = ReleaseAsset(url=url, sha256=sha256)
        return cls(version=version.lstrip("v"), assets=assets)


@dataclass(frozen=True, slots=True)
class UpdateCheck:
    current_version: str
    latest_version: str
    platform_key: str
    asset: ReleaseAsset

    @property
    def update_available(self) -> bool:
        return _version_tuple(self.latest_version) > _version_tuple(self.current_version)


@dataclass(frozen=True, slots=True)
class AutoUpdateResult:
    status: str
    latest_version: str | None = None


def platform_key(*, system: str | None = None, machine: str | None = None) -> str:
    system_name = (system or platform.system()).casefold()
    machine_name = (machine or platform.machine()).casefold()
    os_name = {
        "linux": "linux",
        "darwin": "macos",
        "windows": "windows",
    }.get(system_name)
    if os_name is None:
        raise InputError(f"Unsupported update platform: {system or platform.system()}")
    arch = {
        "x86_64": "x86_64",
        "amd64": "x86_64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get(machine_name)
    if arch is None:
        raise InputError(f"Unsupported update architecture: {machine or platform.machine()}")
    return f"{os_name}-{arch}"


def load_manifest(url: str, *, timeout: float = 20.0) -> ReleaseManifest:
    if not url.startswith(("https://", "file://")):
        raise InputError("Update manifest URL must use HTTPS or file://")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
            data = response.read(2 * 1024 * 1024 + 1)
    except OSError as exc:
        raise InputError(f"Could not fetch update manifest: {exc}") from exc
    if len(data) > 2 * 1024 * 1024:
        raise InputError("Update manifest is unexpectedly large")
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InputError(f"Update manifest is invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise InputError("Update manifest root must be an object")
    return ReleaseManifest.from_dict(payload)


def check_update(
    manifest: ReleaseManifest,
    *,
    current_version: str,
    target_platform: str | None = None,
) -> UpdateCheck:
    key = target_platform or platform_key()
    asset = manifest.assets.get(key)
    if asset is None:
        raise InputError(f"Release {manifest.version} has no asset for {key}")
    _version_tuple(current_version)
    return UpdateCheck(
        current_version=current_version.lstrip("v"),
        latest_version=manifest.version,
        platform_key=key,
        asset=asset,
    )


def auto_update_if_due(
    *,
    current_version: str,
    state_file: Path,
    manifest_url: str = DEFAULT_UPDATE_MANIFEST_URL,
    interval_seconds: float = AUTO_UPDATE_INTERVAL_SECONDS,
    now: float | None = None,
    executable: Path | None = None,
) -> AutoUpdateResult:
    """Best-effort standalone update check with a persistent bounded cadence."""

    checked_at = time.time() if now is None else now
    last_check = _read_last_update_check(state_file)
    if last_check is not None and checked_at - last_check < interval_seconds:
        return AutoUpdateResult("not_due")

    # Record the attempt before networking so an offline machine does not retry on
    # every MediaDL invocation. A later invocation retries after the bounded interval.
    _write_last_update_check(state_file, checked_at)
    manifest = load_manifest(manifest_url, timeout=5.0)
    check = check_update(manifest, current_version=current_version)
    if not check.update_available:
        return AutoUpdateResult("current", check.latest_version)

    staged = stage_update(check)
    status = install_verified_update(staged, executable=executable)
    return AutoUpdateResult(status, check.latest_version)


def download_verified_asset(
    asset: ReleaseAsset,
    *,
    destination: Path,
    timeout: float = 60.0,
) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    try:
        with (
            urllib.request.urlopen(asset.url, timeout=timeout) as response,  # noqa: S310
            destination.open("wb") as handle,
        ):
            while chunk := response.read(1024 * 1024):
                digest.update(chunk)
                handle.write(chunk)
    except OSError as exc:
        destination.unlink(missing_ok=True)
        raise InputError(f"Could not download update: {exc}") from exc
    actual = digest.hexdigest()
    if actual != asset.sha256:
        destination.unlink(missing_ok=True)
        raise InputError(f"Update checksum mismatch: expected {asset.sha256}, received {actual}")
    return destination


def install_verified_update(staged: Path, *, executable: Path | None = None) -> str:
    target = (executable or Path(sys.executable)).resolve()
    if not staged.is_file():
        raise InputError(f"Staged update does not exist: {staged}")
    if not target.is_file():
        raise InputError(f"Installed executable does not exist: {target}")
    if platform.system().casefold() == "windows":
        _schedule_windows_replace(staged.resolve(), target)
        return "scheduled"

    try:
        mode = stat.S_IMODE(target.stat().st_mode)
        os.chmod(staged, mode | stat.S_IXUSR)
        os.replace(staged, target)
    except OSError as exc:
        raise InputError(f"Could not replace installed MediaDL executable: {exc}") from exc
    return "installed"


def stage_update(
    check: UpdateCheck,
    *,
    directory: Path | None = None,
) -> Path:
    suffix = ".exe" if check.platform_key.startswith("windows-") else ""
    root = directory or Path(tempfile.mkdtemp(prefix="mediadl-update-"))
    target = root / f"mdl-{check.latest_version}{suffix}"
    return download_verified_asset(check.asset, destination=target)


def _schedule_windows_replace(staged: Path, target: Path) -> None:
    script = staged.with_suffix(staged.suffix + ".update.ps1")
    script.write_text(
        "param([int]$ProcessId,[string]$Source,[string]$Target)\n"
        "$ErrorActionPreference='Stop'\n"
        "try { Wait-Process -Id $ProcessId -ErrorAction SilentlyContinue } catch {}\n"
        "for ($i=0; $i -lt 50; $i++) {\n"
        "  try { Move-Item -LiteralPath $Source -Destination $Target -Force; break }\n"
        "  catch { Start-Sleep -Milliseconds 200 }\n"
        "}\n"
        "Remove-Item -LiteralPath $MyInvocation.MyCommand.Path -Force "
        "-ErrorAction SilentlyContinue\n",
        encoding="utf-8",
    )
    try:
        subprocess.Popen(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
                str(os.getpid()),
                str(staged),
                str(target),
            ],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            close_fds=True,
        )
    except OSError as exc:
        script.unlink(missing_ok=True)
        raise InputError(f"Could not schedule Windows update replacement: {exc}") from exc


def _read_last_update_check(state_file: Path) -> float | None:
    try:
        payload = json.loads(state_file.read_text(encoding="utf-8"))
        value = float(payload.get("checked_at"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError, AttributeError):
        return None
    return value if value >= 0 else None


def _write_last_update_check(state_file: Path, checked_at: float) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = state_file.with_suffix(state_file.suffix + ".tmp")
    temporary.write_text(
        json.dumps({"checked_at": checked_at}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, state_file)


def _version_tuple(value: str) -> tuple[int, int, int]:
    match = _VERSION_RE.fullmatch(value.strip())
    if match is None:
        raise InputError(f"Version must use semantic MAJOR.MINOR.PATCH form: {value}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]
