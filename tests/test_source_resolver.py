import pytest

from mediadl.core.errors import InputError
from mediadl.sources.models import SourceKind
from mediadl.sources.resolver import SourceResolver


@pytest.fixture
def resolver() -> SourceResolver:
    return SourceResolver()


def test_resolves_watch_short_and_shorts_video_urls(resolver: SourceResolver) -> None:
    watch = resolver.resolve("https://www.youtube.com/watch?v=abc123&list=PLignored")
    short = resolver.resolve("https://youtu.be/xyz789?t=3")
    shorts_video = resolver.resolve("https://www.youtube.com/shorts/short123")

    assert watch.kind is SourceKind.VIDEO
    assert watch.source_key == "abc123"
    assert short.kind is SourceKind.VIDEO
    assert short.source_key == "xyz789"
    assert shorts_video.kind is SourceKind.VIDEO
    assert shorts_video.source_key == "short123"


def test_resolves_playlist_and_normalizes_scheme(resolver: SourceResolver) -> None:
    playlist = resolver.resolve("youtube.com/playlist?list=PL12345")

    assert playlist.kind is SourceKind.PLAYLIST
    assert playlist.source_key == "PL12345"
    assert playlist.url == "https://www.youtube.com/playlist?list=PL12345"


def test_resolves_handle_shorthand_and_channel_tabs(resolver: SourceResolver) -> None:
    root = resolver.resolve("@ExampleChannel")
    videos = resolver.resolve("https://youtube.com/@ExampleChannel/videos")
    shorts = resolver.resolve("https://youtube.com/@ExampleChannel/shorts")
    streams = resolver.resolve("https://youtube.com/@ExampleChannel/streams")
    playlists = resolver.resolve("https://youtube.com/@ExampleChannel/playlists")

    assert root.kind is SourceKind.CHANNEL
    assert root.source_key == "@ExampleChannel"
    assert videos.kind is SourceKind.CHANNEL_VIDEOS
    assert shorts.kind is SourceKind.CHANNEL_SHORTS
    assert streams.kind is SourceKind.CHANNEL_STREAMS
    assert playlists.kind is SourceKind.CHANNEL_PLAYLISTS
    assert shorts.root_url == "https://www.youtube.com/@ExampleChannel"


def test_resolves_channel_id_and_legacy_channel_forms(resolver: SourceResolver) -> None:
    channel = resolver.resolve("https://www.youtube.com/channel/UCabc123/videos")
    legacy_user = resolver.resolve("https://www.youtube.com/user/Example/streams")
    custom = resolver.resolve("https://www.youtube.com/c/Example/shorts")

    assert channel.source_key == "UCabc123"
    assert channel.kind is SourceKind.CHANNEL_VIDEOS
    assert legacy_user.kind is SourceKind.CHANNEL_STREAMS
    assert custom.kind is SourceKind.CHANNEL_SHORTS


def test_channel_root_can_be_converted_to_tab(resolver: SourceResolver) -> None:
    root = resolver.resolve("@Example")
    videos = root.for_tab(SourceKind.CHANNEL_VIDEOS)

    assert videos.kind is SourceKind.CHANNEL_VIDEOS
    assert videos.url == "https://www.youtube.com/@Example/videos"
    assert videos.source_key == root.source_key


def test_shorts_video_is_not_confused_with_channel_shorts_tab(resolver: SourceResolver) -> None:
    video = resolver.resolve("https://youtube.com/shorts/VIDEOID")
    tab = resolver.resolve("https://youtube.com/@Example/shorts")

    assert video.kind is SourceKind.VIDEO
    assert tab.kind is SourceKind.CHANNEL_SHORTS


def test_rejects_missing_ids_unknown_hosts_and_credentials(resolver: SourceResolver) -> None:
    invalid = (
        "https://youtube.com/watch",
        "https://youtube.com/playlist",
        "https://example.com/watch?v=abc",
        "ftp://youtube.com/watch?v=abc",
        "https://user:secret@youtube.com/watch?v=abc",
    )
    for value in invalid:
        with pytest.raises(InputError):
            resolver.resolve(value)


def test_rejects_unsupported_channel_subpages(resolver: SourceResolver) -> None:
    with pytest.raises(InputError, match="Unsupported"):
        resolver.resolve("https://youtube.com/@Example/store")
