#!/usr/bin/env python3
"""Smoke-test the standalone MediaDL binary built for the current native platform."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from mediadl.core.updater import platform_key  # noqa: E402


def main() -> int:
    key = platform_key()
    suffix = ".exe" if key.startswith("windows-") else ""
    binary = ROOT / "dist" / f"mdl-{key}{suffix}"
    if not binary.is_file():
        raise SystemExit(f"Standalone binary does not exist: {binary}")

    subprocess.run([str(binary), "--version"], check=True)
    subprocess.run([str(binary), "--help"], check=True, stdout=subprocess.DEVNULL)

    with tempfile.TemporaryDirectory(prefix="mediadl-smoke-") as temporary:
        home = Path(temporary)
        empty_path = home / "empty-path"
        empty_path.mkdir()
        env = os.environ.copy()
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)
        env["XDG_CONFIG_HOME"] = str(home / "config")
        env["XDG_DATA_HOME"] = str(home / "data")
        env["XDG_STATE_HOME"] = str(home / "state")
        # Prove the standalone does not accidentally rely on host Node/Deno.
        # FFmpeg is intentionally hidden here, so doctor should fail only because
        # that external media dependency is absent while bundled Deno/EJS remain OK.
        env["PATH"] = str(empty_path)
        isolated_doctor = subprocess.run(
            [str(binary), "doctor"],
            check=False,
            env=env,
            capture_output=True,
            text=True,
        )
        if isolated_doctor.returncode != 1:
            raise SystemExit(
                f"Empty-PATH doctor should fail only for FFmpeg; got {isolated_doctor.returncode}"
            )
        if "bundled with MediaDL" not in isolated_doctor.stdout:
            raise SystemExit("Standalone doctor did not detect bundled Deno")
        if "yt-dlp EJS" not in isolated_doctor.stdout:
            raise SystemExit("Standalone doctor did not report bundled yt-dlp EJS support")
        if "FFmpeg" not in isolated_doctor.stdout or "fail" not in isolated_doctor.stdout:
            raise SystemExit("Empty-PATH doctor did not report required FFmpeg as missing")

        # With the normal host PATH restored, this build machine has FFmpeg and
        # the complete installation must report healthy.
        healthy_env = env.copy()
        healthy_env["PATH"] = os.environ.get("PATH", "")
        subprocess.run(
            [str(binary), "doctor"],
            check=True,
            env=healthy_env,
            stdout=subprocess.DEVNULL,
        )

    print(f"standalone-smoke=ok platform={key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
