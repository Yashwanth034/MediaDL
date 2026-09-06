#!/usr/bin/env python3
"""Refresh hard-coded bundled runtime versions from their official stable releases."""

from __future__ import annotations

import json
import os
import re
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = ROOT / "packaging" / "build_standalone.py"
DENO_LATEST_URL = "https://api.github.com/repos/denoland/deno/releases/latest"
_VERSION_RE = re.compile(r"^v?(\d+\.\d+\.\d+)$")
_DENO_ASSIGNMENT_RE = re.compile(r'^DENO_VERSION = "(\d+\.\d+\.\d+)"$', re.MULTILINE)


def latest_deno_version(*, timeout: float = 20.0) -> str:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "MediaDL-dependency-refresh",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(DENO_LATEST_URL, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        payload = json.loads(response.read(1024 * 1024).decode("utf-8"))
    tag = str(payload.get("tag_name", "")).strip()
    match = _VERSION_RE.fullmatch(tag)
    if match is None:
        raise RuntimeError(f"Unexpected latest Deno release tag: {tag!r}")
    return match.group(1)


def replace_deno_version(text: str, version: str) -> tuple[str, bool]:
    if _VERSION_RE.fullmatch(version) is None:
        raise ValueError(f"Invalid Deno version: {version}")
    match = _DENO_ASSIGNMENT_RE.search(text)
    if match is None:
        raise RuntimeError("DENO_VERSION assignment was not found")
    current = match.group(1)
    if current == version:
        return text, False
    updated = _DENO_ASSIGNMENT_RE.sub(f'DENO_VERSION = "{version}"', text, count=1)
    return updated, True


def main() -> int:
    latest = latest_deno_version()
    text = BUILD_SCRIPT.read_text(encoding="utf-8")
    updated, changed = replace_deno_version(text, latest)
    if changed:
        BUILD_SCRIPT.write_text(updated, encoding="utf-8")
        print(f"deno-updated={latest}")
    else:
        print(f"deno-current={latest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
