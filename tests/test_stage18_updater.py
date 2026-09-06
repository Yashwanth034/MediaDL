from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from mediadl.core.errors import InputError
from mediadl.core.updater import (
    DEFAULT_UPDATE_MANIFEST_URL,
    ReleaseAsset,
    ReleaseManifest,
    UpdateCheck,
    auto_update_if_due,
    check_update,
    download_verified_asset,
    install_verified_update,
    load_manifest,
    platform_key,
)


def test_platform_key_normalizes_supported_os_and_architectures() -> None:
    assert platform_key(system="Linux", machine="x86_64") == "linux-x86_64"
    assert platform_key(system="Linux", machine="aarch64") == "linux-arm64"
    assert platform_key(system="Darwin", machine="arm64") == "macos-arm64"
    assert platform_key(system="Windows", machine="AMD64") == "windows-x86_64"


def test_manifest_and_update_check_select_exact_platform_asset(tmp_path: Path) -> None:
    binary = tmp_path / "mdl"
    binary.write_bytes(b"new binary")
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    payload = {
        "version": "1.2.3",
        "assets": {
            "linux-x86_64": {
                "url": binary.as_uri(),
                "sha256": digest,
            }
        },
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    manifest = load_manifest(manifest_path.as_uri())
    check = check_update(
        manifest,
        current_version="1.2.2",
        target_platform="linux-x86_64",
    )

    assert check.update_available
    assert check.latest_version == "1.2.3"
    assert check.asset.sha256 == digest


def test_verified_download_refuses_checksum_mismatch_and_removes_staged_file(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"content")
    destination = tmp_path / "staged"
    asset = ReleaseAsset(url=source.as_uri(), sha256="0" * 64)

    with pytest.raises(InputError, match="checksum mismatch"):
        download_verified_asset(asset, destination=destination)

    assert not destination.exists()


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX executable-mode semantics are not meaningful on Windows",
)
def test_posix_update_replaces_target_atomically_after_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "mdl"
    target.write_bytes(b"old")
    target.chmod(0o755)
    staged = tmp_path / "mdl-new"
    staged.write_bytes(b"new")
    monkeypatch.setattr("mediadl.core.updater.platform.system", lambda: "Linux")

    result = install_verified_update(staged, executable=target)

    assert result == "installed"
    assert target.read_bytes() == b"new"
    assert not staged.exists()
    assert target.stat().st_mode & 0o100


def test_default_update_feed_points_to_latest_official_release_asset() -> None:
    assert DEFAULT_UPDATE_MANIFEST_URL.endswith("/releases/latest/download/manifest.json")


def test_auto_update_check_is_throttled_between_invocations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    asset = ReleaseAsset(url="https://example.invalid/mdl", sha256="a" * 64)
    check = UpdateCheck(
        current_version="1.0.0",
        latest_version="1.0.0",
        platform_key="linux-x86_64",
        asset=asset,
    )

    monkeypatch.setattr(
        "mediadl.core.updater.load_manifest",
        lambda url, timeout=20.0: calls.append(url) or object(),
    )
    monkeypatch.setattr("mediadl.core.updater.check_update", lambda *_args, **_kwargs: check)

    state = tmp_path / "update-check.json"
    first = auto_update_if_due(current_version="1.0.0", state_file=state, now=1000)
    second = auto_update_if_due(current_version="1.0.0", state_file=state, now=1001)

    assert first.status == "current"
    assert second.status == "not_due"
    assert calls == [DEFAULT_UPDATE_MANIFEST_URL]


def test_auto_update_installs_newer_checksum_verified_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = ReleaseAsset(url="https://example.invalid/mdl", sha256="b" * 64)
    check = UpdateCheck(
        current_version="1.0.0",
        latest_version="1.1.0",
        platform_key="linux-x86_64",
        asset=asset,
    )
    staged = tmp_path / "mdl-1.1.0"
    staged.write_bytes(b"verified")

    monkeypatch.setattr("mediadl.core.updater.load_manifest", lambda *_args, **_kwargs: object())
    monkeypatch.setattr("mediadl.core.updater.check_update", lambda *_args, **_kwargs: check)
    monkeypatch.setattr("mediadl.core.updater.stage_update", lambda _check: staged)
    monkeypatch.setattr(
        "mediadl.core.updater.install_verified_update",
        lambda _staged, executable=None: "installed",
    )

    result = auto_update_if_due(
        current_version="1.0.0",
        state_file=tmp_path / "state.json",
        now=1000,
    )

    assert result.status == "installed"
    assert result.latest_version == "1.1.0"


def test_manifest_rejects_insecure_asset_urls() -> None:
    with pytest.raises(InputError, match="HTTPS"):
        ReleaseManifest.from_dict(
            {
                "version": "1.0.0",
                "assets": {
                    "linux-x86_64": {
                        "url": "http://example.invalid/mdl",
                        "sha256": "a" * 64,
                    }
                },
            }
        )
