"""Availability normalization for channel entries and enriched media metadata."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class AvailabilityAction(StrEnum):
    DOWNLOAD = "download"
    SKIP = "skip"
    AUTH_REQUIRED = "auth_required"
    WAIT = "wait"


@dataclass(frozen=True, slots=True)
class AvailabilityDecision:
    action: AvailabilityAction
    reason: str


class AvailabilityPolicy:
    @staticmethod
    def decide(
        availability: str | None,
        *,
        live_status: str | None = None,
    ) -> AvailabilityDecision:
        value = (availability or "unknown").strip().casefold()
        live = (live_status or "").strip().casefold()

        if live in {"is_upcoming", "upcoming"} or value in {"scheduled", "upcoming"}:
            return AvailabilityDecision(
                AvailabilityAction.WAIT,
                "Scheduled media is not available yet.",
            )
        if value in {"private"}:
            return AvailabilityDecision(AvailabilityAction.SKIP, "Private media is unavailable.")
        if value in {"unavailable", "deleted", "removed"}:
            return AvailabilityDecision(AvailabilityAction.SKIP, "Media is unavailable or removed.")
        if value in {
            "needs_auth",
            "subscriber_only",
            "premium_only",
            "members_only",
        }:
            return AvailabilityDecision(
                AvailabilityAction.AUTH_REQUIRED,
                "Media requires authorized signed-in access.",
            )
        return AvailabilityDecision(
            AvailabilityAction.DOWNLOAD,
            "Availability permits a download attempt.",
        )
