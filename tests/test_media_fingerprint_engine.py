import json
import shutil
import struct
import subprocess
from pathlib import Path
from typing import Any

import pytest

from mediadl.core.errors import InputError
from mediadl.dedupe.smart import DuplicateClassification, SmartDuplicateClassifier
from mediadl.engines.media_fingerprint import FFmpegFingerprintEngine, MediaProbe


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.frame_number = 0

    def __call__(self, command: list[str], **_: Any) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(command)
        if command[0] == "ffprobe":
            payload = {
                "streams": [{"codec_type": "video"}, {"codec_type": "audio"}],
                "format": {"duration": "12.5"},
            }
            return subprocess.CompletedProcess(command, 0, json.dumps(payload).encode(), b"")
        if "chromaprint" in command:
            return subprocess.CompletedProcess(
                command,
                0,
                struct.pack("<4I", 1, 2, 3, 4),
                b"",
            )
        if "rawvideo" in command:
            self.frame_number += 1
            return subprocess.CompletedProcess(
                command,
                0,
                bytes([self.frame_number]) * 256,
                b"",
            )
        raise AssertionError(f"Unexpected command: {command}")


def test_fingerprint_engine_builds_probe_audio_and_video_signatures(tmp_path: Path) -> None:
    path = tmp_path / "media ; not-shell.mp4"
    path.write_bytes(b"placeholder")
    runner = FakeRunner()
    engine = FFmpegFingerprintEngine(
        runner=runner,
        sample_fractions=(0.25, 0.75),
    )

    fingerprint = engine.fingerprint(path)

    assert fingerprint.duration_seconds == 12.5
    assert fingerprint.has_audio
    assert fingerprint.has_video
    assert fingerprint.audio_words == (1, 2, 3, 4)
    assert fingerprint.video_hashes is not None
    assert len(fingerprint.video_hashes) == 2
    assert len(runner.calls) == 4
    assert all(isinstance(call, list) for call in runner.calls)
    assert any(str(path) in call for call in runner.calls)


def test_video_fingerprint_rejects_media_without_video(tmp_path: Path) -> None:
    path = tmp_path / "audio.m4a"
    path.write_bytes(b"placeholder")
    engine = FFmpegFingerprintEngine(runner=FakeRunner(), sample_fractions=(0.5,))

    with pytest.raises(InputError, match="no video"):
        engine.video_fingerprint(path, probe=MediaProbe(10.0, True, False))


def test_engine_rejects_missing_files_and_invalid_configuration(tmp_path: Path) -> None:
    with pytest.raises(InputError, match="fractions"):
        FFmpegFingerprintEngine(sample_fractions=())
    with pytest.raises(InputError, match="fractions"):
        FFmpegFingerprintEngine(sample_fractions=(1.1,))
    with pytest.raises(InputError, match="timeout"):
        FFmpegFingerprintEngine(timeout_seconds=0)

    engine = FFmpegFingerprintEngine(runner=FakeRunner())
    with pytest.raises(InputError, match="does not exist"):
        engine.probe(tmp_path / "missing.mp4")


def test_engine_surfaces_tool_failures_without_shell_execution(tmp_path: Path) -> None:
    path = tmp_path / "media.mp4"
    path.write_bytes(b"placeholder")

    def failing_runner(command: list[str], **_: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(command, 1, b"", b"decoder failed")

    engine = FFmpegFingerprintEngine(runner=failing_runner)
    with pytest.raises(InputError, match="decoder failed"):
        engine.probe(path)


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="FFmpeg integration tools are unavailable",
)
def test_real_ffmpeg_reencode_and_visual_variant_classification(tmp_path: Path) -> None:
    original = tmp_path / "original.mp4"
    reencoded = tmp_path / "reencoded.mp4"
    variant = tmp_path / "variant.mp4"

    _run_ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=10:duration=4",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=4",
            "-shortest",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            str(original),
        ]
    )
    _run_ffmpeg(
        [
            "-i",
            str(original),
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "30",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            str(reencoded),
        ]
    )
    _run_ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            "smptebars=size=160x90:rate=10:duration=4",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=4",
            "-shortest",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            str(variant),
        ]
    )

    engine = FFmpegFingerprintEngine(sample_fractions=(0.1, 0.3, 0.5, 0.7, 0.9))
    original_fp = engine.fingerprint(original)
    reencoded_fp = engine.fingerprint(reencoded)
    variant_fp = engine.fingerprint(variant)
    classifier = SmartDuplicateClassifier()

    same_result = classifier.compare(original_fp, reencoded_fp)
    variant_result = classifier.compare(original_fp, variant_fp)

    assert same_result.classification is DuplicateClassification.SAME_MEDIA
    assert same_result.evidence.audio_similarity is not None
    assert same_result.evidence.audio_similarity >= 0.95
    assert same_result.evidence.video_similarity is not None
    assert same_result.evidence.video_similarity >= 0.90
    assert variant_result.classification is DuplicateClassification.AUDIO_VARIANT
    assert variant_result.evidence.audio_similarity is not None
    assert variant_result.evidence.audio_similarity >= 0.95
    assert variant_result.evidence.video_similarity is not None
    assert variant_result.evidence.video_similarity < 0.72


def _run_ffmpeg(arguments: list[str]) -> None:
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *arguments],
        capture_output=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr.decode("utf-8", errors="replace"))
