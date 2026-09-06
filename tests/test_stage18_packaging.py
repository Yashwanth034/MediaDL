from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_posix_installer_is_syntax_valid() -> None:
    result = subprocess.run(
        ["sh", "-n", str(ROOT / "packaging" / "install.sh")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX installer test")
def test_posix_installer_is_idempotent_and_installs_once_on_path(tmp_path: Path) -> None:
    fake = tmp_path / "fake-mdl"
    fake.write_text(
        "#!/bin/sh\n"
        'if [ "${1:-}" = "--version" ]; then echo \'MediaDL test\'; exit 0; fi\n'
        "echo fake\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    env = os.environ.copy()
    env["HOME"] = str(home)
    env.pop("XDG_BIN_HOME", None)
    env["PATH"] = "/usr/bin:/bin"

    for _ in range(2):
        result = subprocess.run(
            ["sh", str(ROOT / "packaging" / "install.sh"), str(fake)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr

    installed = home / ".local" / "bin" / "mdl"
    assert installed.is_file()
    assert os.access(installed, os.X_OK)
    profile = (home / ".profile").read_text(encoding="utf-8")
    assert profile.count("# MediaDL user binary path") == 1
    expected = f'export PATH="{home / ".local" / "bin"}:$PATH"'
    assert profile.count(expected) == 1


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX installer test")
def test_posix_installer_persists_custom_xdg_bin_home(tmp_path: Path) -> None:
    fake = tmp_path / "fake-mdl"
    fake.write_text(
        '#!/bin/sh\nif [ "${1:-}" = "--version" ]; then echo \'MediaDL test\'; exit 0; fi\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    custom_bin = tmp_path / "custom bin"
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["XDG_BIN_HOME"] = str(custom_bin)
    env["PATH"] = os.defpath

    result = subprocess.run(
        ["sh", str(ROOT / "packaging" / "install.sh"), str(fake)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (custom_bin / "mdl").is_file()
    profile = (home / ".profile").read_text(encoding="utf-8")
    assert f'export PATH="{custom_bin}:$PATH"' in profile


def test_windows_installer_contains_user_path_and_pre_replace_smoke_check() -> None:
    script = (ROOT / "packaging" / "install.ps1").read_text(encoding="utf-8")
    assert "[Environment]::SetEnvironmentVariable('Path'" in script
    assert "& $Temp --version" in script
    assert "Move-Item -LiteralPath $Temp -Destination $Target -Force" in script


def test_standalone_builder_help_does_not_require_pyinstaller_to_be_installed() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "packaging" / "build_standalone.py"), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--base-url" in result.stdout


def test_release_manifest_merger_combines_distinct_platform_fragments(tmp_path: Path) -> None:
    linux = tmp_path / "linux.json"
    windows = tmp_path / "windows.json"
    linux.write_text(
        '{"version":"1.0.0","assets":{"linux-x86_64":{"url":"https://x/linux","sha256":"'
        + ("a" * 64)
        + '"}}}',
        encoding="utf-8",
    )
    windows.write_text(
        '{"version":"1.0.0","assets":{"windows-x86_64":{"url":"https://x/windows","sha256":"'
        + ("b" * 64)
        + '"}}}',
        encoding="utf-8",
    )
    output = tmp_path / "manifest.json"

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "packaging" / "merge_manifests.py"),
            str(linux),
            str(windows),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    text = output.read_text(encoding="utf-8")
    assert '"linux-x86_64"' in text
    assert '"windows-x86_64"' in text
