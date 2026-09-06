"""Deterministic YouTube URL/source classification before network extraction."""

from __future__ import annotations

from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from mediadl.core.errors import InputError
from mediadl.sources.models import SourceDescriptor, SourceKind

_YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
}
_CHANNEL_PREFIXES = {"channel", "c", "user"}
_CHANNEL_TABS = {
    "videos": SourceKind.CHANNEL_VIDEOS,
    "shorts": SourceKind.CHANNEL_SHORTS,
    "streams": SourceKind.CHANNEL_STREAMS,
    "playlists": SourceKind.CHANNEL_PLAYLISTS,
}


class SourceResolver:
    """Resolve user input to a stable source descriptor without downloading media."""

    def resolve(self, value: str) -> SourceDescriptor:
        normalized = self.normalize_input(value)
        parsed = urlparse(normalized)
        host = (parsed.hostname or "").lower()

        if parsed.username or parsed.password:
            raise InputError("Media URLs must not contain embedded credentials")
        if host == "youtu.be":
            return self._resolve_youtu_be(parsed)
        if host not in _YOUTUBE_HOSTS and not host.endswith(".youtube.com"):
            raise InputError("Only YouTube sources are supported in MediaDL v1.0")
        return self._resolve_youtube(parsed)

    @staticmethod
    def normalize_input(value: str) -> str:
        raw = value.strip()
        if not raw:
            raise InputError("A non-empty media URL or @channel handle is required")
        if raw.startswith("@") and "/" not in raw and not any(char.isspace() for char in raw):
            return f"https://www.youtube.com/{raw}"
        if "://" not in raw and raw.lower().startswith(
            ("youtube.com/", "www.youtube.com/", "m.youtube.com/", "youtu.be/")
        ):
            raw = f"https://{raw}"
        parsed = urlparse(raw)
        if parsed.scheme not in {"http", "https"}:
            raise InputError("Media URL must use http or https")
        return raw

    def _resolve_youtu_be(self, parsed: object) -> SourceDescriptor:
        path = getattr(parsed, "path", "")
        video_id = path.strip("/").split("/", 1)[0]
        if not video_id:
            raise InputError("Short YouTube URL is missing a video ID")
        query = urlencode({"v": video_id})
        return SourceDescriptor(
            platform="youtube",
            kind=SourceKind.VIDEO,
            source_key=video_id,
            url=f"https://www.youtube.com/watch?{query}",
        )

    def _resolve_youtube(self, parsed: object) -> SourceDescriptor:
        path = getattr(parsed, "path", "")
        query_string = getattr(parsed, "query", "")
        segments = [segment for segment in path.split("/") if segment]
        query = parse_qs(query_string)

        if segments and segments[0].lower() in {"shorts", "live", "embed", "v"}:
            if len(segments) < 2:
                raise InputError("YouTube video URL is missing a video ID")
            return self._video_descriptor(segments[1])

        if segments and segments[0].lower() == "watch":
            video_id = self._first_query_value(query, "v")
            if not video_id:
                raise InputError("YouTube watch URL is missing the v video ID")
            return self._video_descriptor(video_id)

        if segments and segments[0].lower() == "playlist":
            playlist_id = self._first_query_value(query, "list")
            if not playlist_id:
                raise InputError("YouTube playlist URL is missing the list ID")
            canonical = f"https://www.youtube.com/playlist?{urlencode({'list': playlist_id})}"
            return SourceDescriptor(
                platform="youtube",
                kind=SourceKind.PLAYLIST,
                source_key=playlist_id,
                url=canonical,
            )

        channel = self._channel_parts(segments)
        if channel is not None:
            source_key, base_segments, tail = channel
            root_path = "/" + "/".join(base_segments)
            root = urlunparse(("https", "www.youtube.com", root_path, "", "", ""))
            kind = SourceKind.CHANNEL
            if tail:
                lowered_tail = tail[0].lower()
                if lowered_tail in _CHANNEL_TABS and len(tail) == 1:
                    kind = _CHANNEL_TABS[lowered_tail]
                elif lowered_tail not in {"featured", "about", "community"}:
                    raise InputError("Unsupported YouTube channel subpage")
            url = root if kind is SourceKind.CHANNEL else f"{root}/{tail[0].lower()}"
            return SourceDescriptor(
                platform="youtube",
                kind=kind,
                source_key=source_key,
                url=url,
                root_url=root,
            )

        raise InputError("Could not recognize this YouTube video, playlist, or channel URL")

    @staticmethod
    def _video_descriptor(video_id: str) -> SourceDescriptor:
        if not video_id.strip():
            raise InputError("YouTube video ID cannot be empty")
        canonical = f"https://www.youtube.com/watch?{urlencode({'v': video_id})}"
        return SourceDescriptor(
            platform="youtube",
            kind=SourceKind.VIDEO,
            source_key=video_id,
            url=canonical,
        )

    @staticmethod
    def _first_query_value(query: dict[str, list[str]], name: str) -> str | None:
        values = query.get(name)
        if not values:
            return None
        return values[0].strip() or None

    @staticmethod
    def _channel_parts(
        segments: list[str],
    ) -> tuple[str, list[str], list[str]] | None:
        if not segments:
            return None
        first = segments[0]
        if first.startswith("@") and len(first) > 1:
            return first, [first], segments[1:]
        if first.lower() in _CHANNEL_PREFIXES and len(segments) >= 2:
            source_key = segments[1]
            return source_key, segments[:2], segments[2:]
        return None
