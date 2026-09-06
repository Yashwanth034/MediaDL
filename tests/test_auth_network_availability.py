from pathlib import Path

import pytest

import mediadl.engines.ytdlp as ytdlp_module
from mediadl.auth.options import AuthConfig
from mediadl.core.errors import DownloadError, InputError
from mediadl.engines.network import NetworkPolicy
from mediadl.engines.ytdlp import YtDlpAdapter
from mediadl.engines.ytdlp_failures import FailureCategory, YtDlpFailureClassifier
from mediadl.sources.availability import AvailabilityAction, AvailabilityPolicy


class FakeYDL:
    created_options: list[dict[str, object]] = []
    error: Exception | None = None

    def __init__(self, options: dict[str, object]) -> None:
        type(self).created_options.append(options)

    def __enter__(self) -> "FakeYDL":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def extract_info(self, url: str, *, download: bool) -> dict[str, object]:
        if type(self).error is not None:
            raise type(self).error
        return {"id": "abc", "title": "Example", "url": url, "download": download}


@pytest.fixture(autouse=True)
def reset_fake() -> None:
    FakeYDL.created_options = []
    FakeYDL.error = None


def test_browser_auth_builds_typed_ytdlp_cookie_source() -> None:
    auth = AuthConfig(browser="Chrome", browser_profile="Profile 2")

    assert auth.enabled
    assert auth.ytdlp_options() == {"cookiesfrombrowser": ("chrome", "Profile 2", None, None)}


def test_browser_container_and_validation() -> None:
    auth = AuthConfig(browser="firefox", browser_container="work")
    assert auth.ytdlp_options()["cookiesfrombrowser"] == (
        "firefox",
        None,
        None,
        "work",
    )

    with pytest.raises(InputError, match="not both"):
        AuthConfig(browser="chrome", cookie_file=Path("cookies.txt"))
    with pytest.raises(InputError, match="requires a browser"):
        AuthConfig(browser_profile="Default")
    with pytest.raises(InputError, match="Unsupported"):
        AuthConfig(browser="unknown-browser")


def test_cookie_file_must_exist_and_is_resolved(tmp_path: Path) -> None:
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")

    options = AuthConfig(cookie_file=cookie_file).ytdlp_options()
    assert options == {"cookiefile": str(cookie_file.resolve())}

    with pytest.raises(InputError, match="does not exist"):
        AuthConfig(cookie_file=tmp_path / "missing.txt").ytdlp_options()


def test_network_policy_defaults_and_overrides() -> None:
    defaults = NetworkPolicy().ytdlp_options()
    assert defaults["socket_timeout"] == 30.0
    assert defaults["retries"] == 5
    assert defaults["fragment_retries"] == 5
    assert defaults["extractor_retries"] == 3
    assert defaults["file_access_retries"] == 3
    assert defaults["concurrent_fragment_downloads"] == 4
    assert "ratelimit" not in defaults

    custom = NetworkPolicy(
        socket_timeout_seconds=15,
        retries=2,
        fragment_retries=4,
        extractor_retries=1,
        file_access_retries=2,
        concurrent_fragment_downloads=8,
        request_sleep_seconds=0.5,
        rate_limit_bytes_per_second=1_000_000,
    ).ytdlp_options()
    assert custom["socket_timeout"] == 15
    assert custom["concurrent_fragment_downloads"] == 8
    assert custom["sleep_interval_requests"] == 0.5
    assert custom["ratelimit"] == 1_000_000


def test_network_policy_rejects_invalid_values() -> None:
    invalid = (
        lambda: NetworkPolicy(socket_timeout_seconds=0),
        lambda: NetworkPolicy(retries=-1),
        lambda: NetworkPolicy(fragment_retries=-1),
        lambda: NetworkPolicy(extractor_retries=-1),
        lambda: NetworkPolicy(file_access_retries=-1),
        lambda: NetworkPolicy(concurrent_fragment_downloads=0),
        lambda: NetworkPolicy(concurrent_fragment_downloads=17),
        lambda: NetworkPolicy(request_sleep_seconds=-0.1),
        lambda: NetworkPolicy(rate_limit_bytes_per_second=0),
    )
    for factory in invalid:
        with pytest.raises(InputError):
            factory()


@pytest.mark.parametrize(
    ("message", "category", "retryable"),
    [
        ("HTTP Error 429: Too Many Requests", FailureCategory.RATE_LIMIT, True),
        ("HTTP Error 503: Service Unavailable", FailureCategory.SERVER, True),
        ("Connection reset by peer", FailureCategory.NETWORK, True),
        ("Read timed out", FailureCategory.NETWORK, True),
        ("Premiere will begin in 2 hours", FailureCategory.LIVE_NOT_READY, True),
        ("This video is private", FailureCategory.PRIVATE, False),
        ("Video has been removed", FailureCategory.DELETED, False),
        ("This video is not available in your country", FailureCategory.GEO_BLOCKED, False),
        ("Sign in to confirm your age", FailureCategory.AGE_RESTRICTED, False),
        ("Sign in to confirm you're not a bot", FailureCategory.AUTH_REQUIRED, False),
        ("This video is DRM protected", FailureCategory.DRM, False),
        (
            "Download paused because free disk space fell below the safety reserve",
            FailureCategory.DISK_SPACE,
            True,
        ),
        ("No space left on device", FailureCategory.DISK_SPACE, True),
        ("n challenge solving failed", FailureCategory.EXTRACTOR, False),
        ("Signature solving failed", FailureCategory.EXTRACTOR, False),
        ("unable to extract yt initial data", FailureCategory.EXTRACTOR, False),
        ("No title found in player responses", FailureCategory.EXTRACTOR, False),
        ("Video unavailable", FailureCategory.UNAVAILABLE, False),
        ("This video is unavailable", FailureCategory.UNAVAILABLE, False),
        ("This content is unavailable", FailureCategory.UNAVAILABLE, False),
        ("Extractor exploded strangely", FailureCategory.UNKNOWN, False),
    ],
)
def test_failure_classifier_retryability(
    message: str,
    category: FailureCategory,
    retryable: bool,
) -> None:
    result = YtDlpFailureClassifier.classify(message)
    assert result.category is category
    assert result.retryable is retryable


def test_failure_classifier_never_suggests_drm_bypass() -> None:
    result = YtDlpFailureClassifier.classify("Protected by DRM")
    assert result.category is FailureCategory.DRM
    assert "will not bypass DRM" in result.user_message


@pytest.mark.parametrize(
    ("availability", "live_status", "action"),
    [
        ("public", None, AvailabilityAction.DOWNLOAD),
        ("unknown", None, AvailabilityAction.DOWNLOAD),
        ("private", None, AvailabilityAction.SKIP),
        ("deleted", None, AvailabilityAction.SKIP),
        ("removed", None, AvailabilityAction.SKIP),
        ("members_only", None, AvailabilityAction.AUTH_REQUIRED),
        ("premium_only", None, AvailabilityAction.AUTH_REQUIRED),
        ("scheduled", None, AvailabilityAction.WAIT),
        ("public", "is_upcoming", AvailabilityAction.WAIT),
    ],
)
def test_availability_policy(
    availability: str,
    live_status: str | None,
    action: AvailabilityAction,
) -> None:
    assert AvailabilityPolicy.decide(availability, live_status=live_status).action is action


def test_adapter_applies_auth_and_network_as_protected_internal_options(tmp_path: Path) -> None:
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    adapter = YtDlpAdapter(
        ydl_factory=FakeYDL,
        auth=AuthConfig(cookie_file=cookie_file),
        network_policy=NetworkPolicy(retries=2, request_sleep_seconds=0.25),
    )

    adapter.extract_info("https://example.invalid/watch?v=abc")
    options = FakeYDL.created_options[-1]
    assert options["cookiefile"] == str(cookie_file.resolve())
    assert options["retries"] == 2
    assert options["sleep_interval_requests"] == 0.25

    for protected in (
        "cookiefile",
        "retries",
        "socket_timeout",
        "concurrent_fragment_downloads",
        "ratelimit",
        "js_runtimes",
    ):
        with pytest.raises(InputError, match="cannot be overridden"):
            adapter.extract_info(
                "https://example.invalid/watch?v=abc",
                extra_options={protected: "override"},
            )


def test_adapter_surfaces_retryable_category(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeEngineError(Exception):
        pass

    monkeypatch.setattr(ytdlp_module, "_YtDlpDownloadError", FakeEngineError)
    adapter = YtDlpAdapter(ydl_factory=FakeYDL)
    FakeYDL.error = FakeEngineError("HTTP Error 429: Too Many Requests")

    with pytest.raises(DownloadError) as caught:
        adapter.extract_info("https://example.invalid/watch?v=abc")

    assert caught.value.category == "rate_limit"
    assert caught.value.retryable is True
    assert caught.value.exit_code == 6


def test_adapter_surfaces_permanent_category(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeEngineError(Exception):
        pass

    monkeypatch.setattr(ytdlp_module, "_YtDlpDownloadError", FakeEngineError)
    adapter = YtDlpAdapter(ydl_factory=FakeYDL)
    FakeYDL.error = FakeEngineError("This video is private")

    with pytest.raises(DownloadError) as caught:
        adapter.extract_info("https://example.invalid/watch?v=abc")

    assert caught.value.category == "private"
    assert caught.value.retryable is False
    assert caught.value.exit_code == 7
