"""Reusable output-format and quality policy for yt-dlp."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mediadl.core.errors import InputError
from mediadl.core.formats import OutputFormat
from mediadl.core.quality import AudioQuality, VideoQuality, VideoQualityMode


@dataclass(frozen=True, slots=True)
class FormatPolicy:
    output_format: OutputFormat
    quality_label: str
    yt_dlp_options: dict[str, Any]
    requires_ffmpeg: bool


class FormatPolicyBuilder:
    @classmethod
    def build(cls, output_format: OutputFormat, quality: str = "best") -> FormatPolicy:
        if output_format.is_video:
            return cls._video(output_format, VideoQuality.parse(quality))
        return cls._audio(output_format, AudioQuality.parse(quality))

    @classmethod
    def _video(cls, output_format: OutputFormat, quality: VideoQuality) -> FormatPolicy:
        selector = cls._video_selector(output_format, quality)
        options: dict[str, Any] = {"format": selector}

        if output_format in {OutputFormat.MP4, OutputFormat.WEBM, OutputFormat.MKV}:
            options["merge_output_format"] = output_format.value

        return FormatPolicy(
            output_format=output_format,
            quality_label=quality.label,
            yt_dlp_options=options,
            requires_ffmpeg=True,
        )

    @classmethod
    def _video_selector(cls, output_format: OutputFormat, quality: VideoQuality) -> str:
        if output_format is OutputFormat.ORIGINAL:
            if quality.mode is VideoQualityMode.LOWEST:
                return "worstvideo*[protocol^=m3u8]+worstaudio[protocol^=m3u8]/worst"
            video_filter, combined_filter = cls._height_filters(quality)
            hls = (
                f"bestvideo*[protocol^=m3u8]{video_filter}+bestaudio[protocol^=m3u8]/"
                f"best[protocol^=m3u8]{combined_filter}"
            )
            return f"{hls}/bestvideo*{video_filter}+bestaudio/best{combined_filter}"

        if quality.mode is VideoQualityMode.LOWEST:
            return "worstvideo*+worstaudio/worst"

        video_filter, combined_filter = cls._height_filters(quality)

        if output_format is OutputFormat.MP4:
            hls = (
                f"bestvideo*[ext=mp4][protocol^=m3u8]{video_filter}+"
                f"bestaudio[protocol^=m3u8]/best[ext=mp4][protocol^=m3u8]{combined_filter}"
            )
            compatible = (
                f"bestvideo*[ext=mp4]{video_filter}+bestaudio[ext=m4a]/"
                f"best[ext=mp4]{combined_filter}"
            )
            generic = f"bestvideo*{video_filter}+bestaudio/best{combined_filter}"
            return f"{hls}/{compatible}/{generic}"

        if output_format is OutputFormat.WEBM:
            compatible = (
                f"bestvideo*[ext=webm]{video_filter}+bestaudio[ext=webm]/"
                f"best[ext=webm]{combined_filter}"
            )
            generic = f"bestvideo*{video_filter}+bestaudio/best{combined_filter}"
            return f"{compatible}/{generic}"

        if output_format is OutputFormat.MKV:
            hls = (
                f"bestvideo*[protocol^=m3u8]{video_filter}+bestaudio[protocol^=m3u8]/"
                f"best[protocol^=m3u8]{combined_filter}"
            )
            generic = f"bestvideo*{video_filter}+bestaudio/best{combined_filter}"
            return f"{hls}/{generic}"

        return f"bestvideo*{video_filter}+bestaudio/best{combined_filter}"

    @staticmethod
    def _height_filters(quality: VideoQuality) -> tuple[str, str]:
        if quality.mode is VideoQualityMode.BEST:
            return "", ""
        assert quality.height is not None
        operator = "=" if quality.mode is VideoQualityMode.EXACT else "<="
        height_filter = f"[height{operator}{quality.height}]"
        return height_filter, height_filter

    @classmethod
    def _audio(cls, output_format: OutputFormat, quality: AudioQuality) -> FormatPolicy:
        lossless = output_format in {OutputFormat.FLAC, OutputFormat.WAV}
        if lossless and quality.bitrate_kbps is not None:
            format_name = output_format.value.upper()
            raise InputError(f"{format_name} uses lossless quality; use --quality best")

        format_selector = cls._audio_selector(output_format)
        postprocessor: dict[str, Any] = {
            "key": "FFmpegExtractAudio",
            "preferredcodec": output_format.value,
        }
        if output_format not in {OutputFormat.FLAC, OutputFormat.WAV}:
            postprocessor["preferredquality"] = (
                "0" if quality.bitrate_kbps is None else str(quality.bitrate_kbps)
            )

        return FormatPolicy(
            output_format=output_format,
            quality_label=quality.label,
            yt_dlp_options={
                "format": format_selector,
                "postprocessors": [postprocessor],
            },
            requires_ffmpeg=True,
        )

    @staticmethod
    def _audio_selector(output_format: OutputFormat) -> str:
        hls = "bestaudio[protocol^=m3u8]"
        if output_format is OutputFormat.M4A:
            return f"bestaudio[ext=m4a]/{hls}/bestaudio/best"
        if output_format is OutputFormat.OPUS:
            return f"bestaudio[acodec^=opus]/bestaudio[ext=webm]/{hls}/bestaudio/best"
        return f"{hls}/bestaudio/best"
