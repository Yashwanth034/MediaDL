#!/usr/bin/env python3
"""Build one self-contained MediaDL executable for the current OS/architecture."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from mediadl import __version__  # noqa: E402
from mediadl.core.updater import platform_key  # noqa: E402

DENO_VERSION = "2.9.5"
_DENO_ASSETS = {
    "linux-x86_64": "deno-x86_64-unknown-linux-gnu.zip",
    "linux-arm64": "deno-aarch64-unknown-linux-gnu.zip",
    "windows-x86_64": "deno-x86_64-pc-windows-msvc.zip",
    "windows-arm64": "deno-aarch64-pc-windows-msvc.zip",
    "macos-x86_64": "deno-x86_64-apple-darwin.zip",
    "macos-arm64": "deno-aarch64-apple-darwin.zip",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", type=Path, default=ROOT / "dist")
    parser.add_argument(
        "--base-url",
        default="",
        help="Optional HTTPS release base URL used in the generated manifest fragment.",
    )
    parser.add_argument(
        "--expected-platform",
        default="",
        help="Fail if the current native OS/architecture does not match this platform key.",
    )
    args = parser.parse_args()

    dist = args.dist.resolve()
    work = ROOT / "build" / "pyinstaller"
    spec = ROOT / "build" / "spec"
    dist.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    spec.mkdir(parents=True, exist_ok=True)

    key = platform_key()
    if args.expected_platform and args.expected_platform != key:
        raise SystemExit(
            f"Native build platform mismatch: expected {args.expected_platform}, detected {key}"
        )
    suffix = ".exe" if key.startswith("windows-") else ""
    plain = dist / f"mdl{suffix}"
    tagged = dist / f"mdl-{key}{suffix}"
    deno = prepare_bundled_deno(key)

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--clean",
        "--noconfirm",
        "--onefile",
        "--name",
        "mdl",
        "--paths",
        str(SRC),
        "--collect-all",
        "yt_dlp",
        "--collect-all",
        "yt_dlp_ejs",
        "--add-binary",
        f"{deno}{os.pathsep}mediadl_runtime",
        "--hidden-import",
        "mediadl.storage.migrations",
        "--collect-data",
        "mediadl.storage.migrations",
        "--distpath",
        str(dist),
        "--workpath",
        str(work),
        "--specpath",
        str(spec),
        str(ROOT / "src" / "mediadl" / "__main__.py"),
    ]
    subprocess.run(command, cwd=ROOT, check=True)
    if not plain.is_file():
        raise SystemExit(f"PyInstaller did not create expected executable: {plain}")

    shutil.copy2(plain, tagged)
    if not suffix:
        tagged.chmod(tagged.stat().st_mode | 0o111)
    digest = sha256(tagged)
    base_url = args.base_url.rstrip("/")
    asset_url = f"{base_url}/{tagged.name}" if base_url else tagged.as_uri()
    fragment = {
        "version": __version__,
        "assets": {
            key: {
                "url": asset_url,
                "sha256": digest,
            }
        },
    }
    manifest = dist / f"manifest-{key}.json"
    manifest.write_text(json.dumps(fragment, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(tagged)
    print(f"sha256={digest}")
    print(f"bundled-deno={DENO_VERSION}")
    print(manifest)
    return 0


def prepare_bundled_deno(key: str) -> Path:
    """Download, checksum-verify, and cache the recommended Deno runtime for this build."""

    asset = _DENO_ASSETS.get(key)
    if asset is None:
        raise SystemExit(f"No bundled Deno asset mapping for platform: {key}")
    runtime_dir = ROOT / "build" / "bundled-runtime" / key
    executable_name = "deno.exe" if key.startswith("windows-") else "deno"
    executable = runtime_dir / executable_name
    if _deno_version(executable) == DENO_VERSION:
        return executable

    runtime_dir.mkdir(parents=True, exist_ok=True)
    archive = runtime_dir / asset
    checksum_file = runtime_dir / f"{asset}.sha256sum"
    base = f"https://github.com/denoland/deno/releases/download/v{DENO_VERSION}"
    _download(f"{base}/{asset}", archive)
    _download(f"{base}/{asset}.sha256sum", checksum_file)
    expected = checksum_file.read_text(encoding="utf-8").strip().split()[0].casefold()
    actual = sha256(archive)
    if not expected or actual.casefold() != expected:
        archive.unlink(missing_ok=True)
        raise SystemExit(
            f"Bundled Deno checksum mismatch for {asset}: expected {expected}, got {actual}"
        )

    with zipfile.ZipFile(archive) as zipped:
        member = next(
            (
                name
                for name in zipped.namelist()
                if Path(name).name.casefold() == executable_name.casefold()
            ),
            None,
        )
        if member is None:
            raise SystemExit(f"Deno archive does not contain {executable_name}")
        with zipped.open(member) as source, executable.open("wb") as target:
            shutil.copyfileobj(source, target)
    if not key.startswith("windows-"):
        executable.chmod(executable.stat().st_mode | 0o111)
    detected = _deno_version(executable)
    if detected != DENO_VERSION:
        raise SystemExit(f"Bundled Deno validation failed: expected {DENO_VERSION}, got {detected}")
    return executable


def _deno_version(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        result = subprocess.run(
            [str(path), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    first = (result.stdout or result.stderr or "").splitlines()
    if not first:
        return None
    parts = first[0].strip().split()
    return parts[1] if len(parts) >= 2 and parts[0].casefold() == "deno" else None


def _download(url: str, destination: Path) -> None:
    temp = destination.with_suffix(destination.suffix + ".part")
    temp.unlink(missing_ok=True)
    try:
        with urllib.request.urlopen(url, timeout=60) as response, temp.open("wb") as output:
            shutil.copyfileobj(response, output)
        os.replace(temp, destination)
    except Exception:
        temp.unlink(missing_ok=True)
        raise


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
