import pytest

from mediadl.core.errors import InputError
from mediadl.core.formats import OutputFormat
from mediadl.core.quality import AudioQuality, VideoQuality, VideoQualityMode
from mediadl.downloads.format_policy import FormatPolicyBuilder


def test_video_quality_parses_common_forms() -> None:
    assert VideoQuality.parse("best").mode is VideoQualityMode.BEST
    assert VideoQuality.parse("lowest").mode is VideoQualityMode.LOWEST

    limited = VideoQuality.parse("1080p")
    assert limited.mode is VideoQualityMode.AT_MOST
    assert limited.height == 1080
    assert limited.label == "1080p"

    exact = VideoQuality.parse("EXACT:1440P")
    assert exact.mode is VideoQualityMode.EXACT
    assert exact.height == 1440
    assert exact.label == "exact:1440p"


def test_video_quality_rejects_invalid_or_unsafe_height() -> None:
    for value in ("", "ultra", "100p", "20000p", "exact:abc"):
        with pytest.raises(InputError):
            VideoQuality.parse(value)


def test_audio_quality_parses_best_and_bitrate() -> None:
    assert AudioQuality.parse("best").bitrate_kbps is None
    assert AudioQuality.parse("0").label == "best"
    assert AudioQuality.parse("320").bitrate_kbps == 320
    assert AudioQuality.parse("192kbps").label == "192k"


def test_audio_quality_rejects_invalid_bitrate() -> None:
    for value in ("31", "513", "lossless", "abc"):
        with pytest.raises(InputError):
            AudioQuality.parse(value)


def test_mp4_best_policy_prefers_compatible_streams_with_generic_fallback() -> None:
    policy = FormatPolicyBuilder.build(OutputFormat.MP4, "best")

    assert policy.requires_ffmpeg is True
    assert policy.yt_dlp_options["merge_output_format"] == "mp4"
    selector = policy.yt_dlp_options["format"]
    assert "bestvideo*[ext=mp4]+bestaudio[ext=m4a]" in selector
    assert "bestvideo*+bestaudio" in selector


def test_video_height_policy_is_at_most_by_default() -> None:
    policy = FormatPolicyBuilder.build(OutputFormat.MP4, "1080")

    assert "[height<=1080]" in policy.yt_dlp_options["format"]
    assert policy.quality_label == "1080p"


def test_video_exact_policy_does_not_fall_back_to_lower_height() -> None:
    policy = FormatPolicyBuilder.build(OutputFormat.WEBM, "exact:720")

    selector = policy.yt_dlp_options["format"]
    assert "[height=720]" in selector
    assert "[height<=720]" not in selector
    assert policy.quality_label == "exact:720p"


def test_mkv_and_original_use_generic_video_selection() -> None:
    mkv = FormatPolicyBuilder.build(OutputFormat.MKV, "1440p")
    original = FormatPolicyBuilder.build(OutputFormat.ORIGINAL, "best")

    assert mkv.yt_dlp_options["merge_output_format"] == "mkv"
    assert "[height<=1440]" in mkv.yt_dlp_options["format"]
    assert original.requires_ffmpeg is True
    assert "merge_output_format" not in original.yt_dlp_options
    assert "protocol^=m3u8" in original.yt_dlp_options["format"]
    limited_original = FormatPolicyBuilder.build(OutputFormat.ORIGINAL, "720p")
    assert "[height<=720]" in limited_original.yt_dlp_options["format"]
    lowest_original = FormatPolicyBuilder.build(OutputFormat.ORIGINAL, "lowest")
    assert "protocol^=m3u8" in lowest_original.yt_dlp_options["format"]


def test_audio_formats_build_expected_postprocessors() -> None:
    mp3 = FormatPolicyBuilder.build(OutputFormat.MP3, "320")
    m4a = FormatPolicyBuilder.build(OutputFormat.M4A, "best")
    opus = FormatPolicyBuilder.build(OutputFormat.OPUS, "192")

    assert mp3.yt_dlp_options["postprocessors"][0]["preferredcodec"] == "mp3"
    assert mp3.yt_dlp_options["postprocessors"][0]["preferredquality"] == "320"
    assert m4a.yt_dlp_options["format"].startswith("bestaudio[ext=m4a]")
    assert "bestaudio[protocol^=m3u8]" in m4a.yt_dlp_options["format"]
    assert opus.yt_dlp_options["format"].startswith("bestaudio[acodec^=opus]")
    assert "bestaudio[protocol^=m3u8]" in opus.yt_dlp_options["format"]
    assert "bestaudio[protocol^=m3u8]" in mp3.yt_dlp_options["format"]


def test_lossless_audio_rejects_bitrate_and_uses_no_lossy_quality_field() -> None:
    for output_format in (OutputFormat.FLAC, OutputFormat.WAV):
        policy = FormatPolicyBuilder.build(output_format, "best")
        postprocessor = policy.yt_dlp_options["postprocessors"][0]
        assert "preferredquality" not in postprocessor
        with pytest.raises(InputError, match="lossless"):
            FormatPolicyBuilder.build(output_format, "320")
