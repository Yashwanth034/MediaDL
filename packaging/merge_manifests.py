#!/usr/bin/env python3
"""Merge per-platform MediaDL manifest fragments into one release manifest."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("fragments", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, default=Path("dist/manifest.json"))
    args = parser.parse_args()

    version: str | None = None
    assets: dict[str, dict[str, str]] = {}
    for fragment_path in args.fragments:
        payload = json.loads(fragment_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise SystemExit(f"Manifest fragment root must be an object: {fragment_path}")
        fragment_version = str(payload.get("version", "")).strip()
        if not fragment_version:
            raise SystemExit(f"Manifest fragment has no version: {fragment_path}")
        if version is None:
            version = fragment_version
        elif version != fragment_version:
            raise SystemExit(
                f"Manifest versions disagree: expected {version}, got {fragment_version}"
            )
        raw_assets: Any = payload.get("assets")
        if not isinstance(raw_assets, dict) or not raw_assets:
            raise SystemExit(f"Manifest fragment has no assets: {fragment_path}")
        for key, raw_asset in raw_assets.items():
            if key in assets:
                raise SystemExit(f"Duplicate release platform asset: {key}")
            if not isinstance(raw_asset, dict):
                raise SystemExit(f"Invalid asset entry for {key}")
            url = str(raw_asset.get("url", "")).strip()
            sha256 = str(raw_asset.get("sha256", "")).strip().lower()
            if not url or len(sha256) != 64:
                raise SystemExit(f"Incomplete asset entry for {key}")
            assets[str(key)] = {"url": url, "sha256": sha256}

    assert version is not None
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps({"version": version, "assets": assets}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
