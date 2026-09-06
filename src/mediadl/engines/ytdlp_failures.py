"""Stable yt-dlp failure categorization and retryability decisions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class FailureCategory(StrEnum):
    NETWORK = "network"
    RATE_LIMIT = "rate_limit"
    SERVER = "server"
    PRIVATE = "private"
    DELETED = "deleted"
    GEO_BLOCKED = "geo_blocked"
    AUTH_REQUIRED = "auth_required"
    AGE_RESTRICTED = "age_restricted"
    LIVE_NOT_READY = "live_not_ready"
    DISK_SPACE = "disk_space"
    DRM = "drm"
    EXTRACTOR = "extractor"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class FailureInfo:
    category: FailureCategory
    retryable: bool
    user_message: str


_HTTP_SERVER_RE = re.compile(r"\b(?:http error\s*)?(?:500|502|503|504)\b", re.IGNORECASE)


class YtDlpFailureClassifier:
    """Classify common yt-dlp failures without making ambiguous errors retry forever."""

    @staticmethod
    def classify(message: str) -> FailureInfo:
        text = " ".join(str(message).strip().split())
        lower = text.casefold()

        if _contains(
            lower,
            "free disk space fell below the safety reserve",
            "download paused because free disk space fell below",
            "no space left on device",
            "disk full",
        ):
            return FailureInfo(
                FailureCategory.DISK_SPACE,
                True,
                "Download paused because the destination is low on free disk space.",
            )

        if _contains(lower, "drm protected", "protected by drm", "drm protection"):
            return FailureInfo(
                FailureCategory.DRM,
                False,
                "This media is DRM-protected and MediaDL will not bypass DRM.",
            )
        if _contains(lower, "private video", "video is private", "this video is private"):
            return FailureInfo(FailureCategory.PRIVATE, False, "This video is private.")
        if _contains(lower, "video has been removed", "deleted video", "video was removed"):
            return FailureInfo(FailureCategory.DELETED, False, "This video has been removed.")
        if _contains(
            lower,
            "not available in your country",
            "not available in your region",
            "geo-restricted",
            "geographically restricted",
        ):
            return FailureInfo(
                FailureCategory.GEO_BLOCKED,
                False,
                "This media is not available in the current region.",
            )
        if _contains(lower, "sign in to confirm your age", "age-restricted", "age restricted"):
            return FailureInfo(
                FailureCategory.AGE_RESTRICTED,
                False,
                "This media requires authorized age-verified access.",
            )
        if _contains(
            lower,
            "login required",
            "sign in to confirm you're not a bot",
            "sign in to confirm you’re not a bot",
            "cookies are required",
            "members-only",
            "members only",
            "join this channel",
            "youtube premium",
        ):
            return FailureInfo(
                FailureCategory.AUTH_REQUIRED,
                False,
                "This media requires authorized signed-in access.",
            )
        if _contains(lower, "429", "too many requests", "rate limit", "rate-limit"):
            return FailureInfo(
                FailureCategory.RATE_LIMIT,
                True,
                "The service is rate-limiting requests; retry later with backoff.",
            )
        if _HTTP_SERVER_RE.search(lower) or _contains(
            lower,
            "server error",
            "service unavailable",
            "bad gateway",
            "gateway timeout",
        ):
            return FailureInfo(
                FailureCategory.SERVER,
                True,
                "The remote service returned a temporary server error.",
            )
        if _contains(
            lower,
            "timed out",
            "timeout",
            "connection reset",
            "connection aborted",
            "remote end closed",
            "temporary failure in name resolution",
            "name or service not known",
            "network is unreachable",
            "connection refused",
        ):
            return FailureInfo(
                FailureCategory.NETWORK,
                True,
                "A temporary network error interrupted the request.",
            )
        if _contains(
            lower,
            "not yet available",
            "will begin",
            "scheduled for",
            "premiere will begin",
            "upcoming live event",
        ):
            return FailureInfo(
                FailureCategory.LIVE_NOT_READY,
                True,
                "This scheduled/live media is not available yet.",
            )
        if _contains(
            lower,
            "no supported javascript runtime could be found",
            "challenge solver",
            "challenge solving failed",
            "signature solving failed",
            "unable to extract yt initial data",
            "incomplete data received in embedded initial data",
            "no title found in player responses",
        ):
            return FailureInfo(
                FailureCategory.EXTRACTOR,
                False,
                "YouTube extraction failed before availability could be determined.",
            )
        if _contains(
            lower,
            "video unavailable",
            "video is unavailable",
            "this video is unavailable",
            "no longer available",
            "unavailable video",
            "this content is unavailable",
        ):
            return FailureInfo(
                FailureCategory.UNAVAILABLE,
                False,
                "This media is unavailable.",
            )
        return FailureInfo(
            FailureCategory.UNKNOWN,
            False,
            "The media request failed with an unclassified error.",
        )


def _contains(text: str, *needles: str) -> bool:
    return any(needle in text for needle in needles)
