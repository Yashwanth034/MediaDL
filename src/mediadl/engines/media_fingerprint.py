"""Local FFmpeg/Chromaprint media fingerprint adapter."""

from __future__ import annotations

import json
import shutil
import struct
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from mediadl.core.errors import DependencyError, InputError


@dataclass(frozen=True, slots=True)
class MediaProbe:
    duration_seconds: float | None
    has_audio: bool
    has_video: bool


@dataclass(frozen=True, slots=True)
class MediaFingerprint:
    duration_seconds: float | None
    has_audio: bool
    has_video: bool
    audio_words: tuple[int, ...] | None
    video_hashes: tuple[int, ...] | None


CommandRunner = Callable[..., subprocess.CompletedProcess[bytes]]


class FFmpegFingerprintEngine:
    """Generate robust local audio and sampled-video fingerprints without network APIs."""

    def __init__(
        self,
        *,
        ffmpeg: str = "ffmpeg",
        ffprobe: str = "ffprobe",
        runner: CommandRunner = subprocess.run,
        sample_fractions: Sequence[float] = (0.05, 0.2, 0.4, 0.6, 0.8, 0.95),
        timeout_seconds: float = 60.0,
    ) -> None:
        fractions = tuple(float(value) for value in sample_fractions)
        if not fractions or any(value < 0 or value > 1 for value in fractions):
            raise InputError("Video sample fractions must be between 0 and 1")
        if timeout_seconds <= 0:
            raise InputError("Fingerprint timeout must be positive")
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.runner = runner
        self.sample_fractions = fractions
        self.timeout_seconds = timeout_seconds

    def available(self) -> bool:
        return shutil.which(self.ffmpeg) is not None and shutil.which(self.ffprobe) is not None

    def probe(self, path: Path) -> MediaProbe:
        self._validate_file(path)
        command = [
            self.ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type",
            "-of",
            "json",
            str(path),
        ]
        result = self._run(command)
        try:
            payload = json.loads(result.stdout.decode("utf-8"))
            streams = payload.get("streams", [])
            format_info = payload.get("format", {})
            duration_raw = format_info.get("duration")
            duration = float(duration_raw) if duration_raw not in {None, "N/A"} else None
            types = {
                str(stream.get("codec_type")) for stream in streams if isinstance(stream, dict)
            }
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise InputError(f"ffprobe returned invalid media metadata: {exc}") from exc
        if duration is not None and duration < 0:
            duration = None
        return MediaProbe(duration, "audio" in types, "video" in types)

    def fingerprint(self, path: Path) -> MediaFingerprint:
        probe = self.probe(path)
        audio = self.audio_fingerprint(path) if probe.has_audio else None
        video = self.video_fingerprint(path, probe=probe) if probe.has_video else None
        return MediaFingerprint(
            duration_seconds=probe.duration_seconds,
            has_audio=probe.has_audio,
            has_video=probe.has_video,
            audio_words=audio,
            video_hashes=video,
        )

    def audio_fingerprint(self, path: Path) -> tuple[int, ...]:
        """Return raw Chromaprint words using FFmpeg's built-in chromaprint muxer."""

        self._validate_file(path)
        command = [
            self.ffmpeg,
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-ar",
            "11025",
            "-f",
            "chromaprint",
            "-fp_format",
            "raw",
            "pipe:1",
        ]
        result = self._run(command)
        payload = result.stdout
        if not payload or len(payload) % 4 != 0:
            raise InputError("FFmpeg produced an invalid Chromaprint fingerprint")
        count = len(payload) // 4
        return tuple(struct.unpack(f"<{count}I", payload))

    def video_fingerprint(
        self,
        path: Path,
        *,
        probe: MediaProbe | None = None,
    ) -> tuple[int, ...]:
        """Sample normalized 16×16 grayscale frames as compact integer signatures."""

        self._validate_file(path)
        probe = probe or self.probe(path)
        if not probe.has_video:
            raise InputError("Media file has no video stream to fingerprint")
        duration = probe.duration_seconds
        if duration is None or duration <= 0:
            raise InputError("Video duration is required for sampled fingerprinting")

        hashes: list[int] = []
        for fraction in self.sample_fractions:
            timestamp = max(0.0, min(duration * fraction, max(0.0, duration - 0.001)))
            command = [
                self.ffmpeg,
                "-v",
                "error",
                "-ss",
                f"{timestamp:.6f}",
                "-i",
                str(path),
                "-map",
                "0:v:0",
                "-frames:v",
                "1",
                "-vf",
                "scale=16:16:flags=area,format=gray",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "gray",
                "pipe:1",
            ]
            result = self._run(command)
            if len(result.stdout) != 256:
                raise InputError("FFmpeg could not produce a normalized video sample frame")
            hashes.append(_frame_signature(result.stdout))
        return tuple(hashes)

    def _run(self, command: list[str]) -> subprocess.CompletedProcess[bytes]:
        try:
            result = self.runner(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=self.timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise DependencyError(f"Required media tool was not found: {command[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise InputError(f"Media fingerprinting timed out while running {command[0]}") from exc
        if result.returncode != 0:
            detail = _safe_stderr(result.stderr)
            raise InputError(f"Media fingerprinting failed with {command[0]}: {detail}")
        return result

    @staticmethod
    def _validate_file(path: Path) -> None:
        if not path.is_file():
            raise InputError(f"Media fingerprint path does not exist or is not a file: {path}")


def _frame_signature(frame: bytes) -> int:
    if len(frame) != 256:
        raise InputError("Video sample frame must contain exactly 256 grayscale pixels")
    return int.from_bytes(frame, "big")


def _safe_stderr(value: bytes) -> str:
    try:
        text = value.decode("utf-8", errors="replace")
    except Exception:  # pragma: no cover - bytes.decode with replace is defensive
        return "unknown media tool failure"
    text = " ".join(text.strip().split())
    return text[-500:] or "unknown media tool failure"
