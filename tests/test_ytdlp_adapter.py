from __future__ import annotations

from typing import Any

import pytest

import mediadl.engines.ytdlp as ytdlp_module
from mediadl.core.errors import DependencyError, DownloadError, InputError
from mediadl.engines.js_runtime import JavaScriptRuntime
from mediadl.engines.ytdlp import YtDlpAdapter


class FakeYDL:
    created_options: list[dict[str, Any]] = []
    response: Any = {"id": "abc", "title": "Example"}
    seen_calls: list[tuple[str, bool]] = []
    error: Exception | None = None
    warning_before_error: str | None = None

    def __init__(self, options: dict[str, Any]) -> None:
        self.options = options
        type(self).created_options.append(options)

    def __enter__(self) -> FakeYDL:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        return None

    def extract_info(self, url: str, *, download: bool) -> Any:
        type(self).seen_calls.append((url, download))
        if type(self).warning_before_error is not None:
            self.options["logger"].warning(type(self).warning_before_error)
        if type(self).error is not None:
            raise type(self).error
        return type(self).response


@pytest.fixture(autouse=True)
def reset_fake() -> None:
    FakeYDL.created_options = []
    FakeYDL.seen_calls = []
    FakeYDL.response = {"id": "abc", "title": "Example"}
    FakeYDL.error = None
    FakeYDL.warning_before_error = None


def test_extract_info_uses_metadata_only_options() -> None:
    adapter = YtDlpAdapter(ydl_factory=FakeYDL)

    info = adapter.extract_info(
        "https://example.invalid/watch?v=abc",
        flat=True,
        playlist_items="1:10",
    )

    assert info["id"] == "abc"
    assert FakeYDL.seen_calls == [("https://example.invalid/watch?v=abc", False)]
    options = FakeYDL.created_options[-1]
    assert options["skip_download"] is True
    assert options["extract_flat"] == "in_playlist"
    assert options["playlist_items"] == "1:10"
    assert options["quiet"] is True
    assert options["noprogress"] is True


def test_detected_js_runtime_is_passed_as_protected_ytdlp_option() -> None:
    adapter = YtDlpAdapter(
        ydl_factory=FakeYDL,
        js_runtime_detector=lambda: JavaScriptRuntime("node", "/opt/node"),
    )

    adapter.extract_info("https://example.invalid/watch?v=abc")

    options = FakeYDL.created_options[-1]
    assert options["js_runtimes"] == {"node": {"path": "/opt/node"}}
    with pytest.raises(InputError, match="cannot be overridden"):
        adapter.extract_info(
            "https://example.invalid/watch?v=abc",
            extra_options={"js_runtimes": {"deno": {}}},
        )


def test_download_adds_progress_and_postprocessor_hooks_without_allowing_override() -> None:
    adapter = YtDlpAdapter(ydl_factory=FakeYDL)
    events: list[dict[str, Any]] = []
    post_events: list[dict[str, Any]] = []

    adapter.download(
        "https://example.invalid/watch?v=abc",
        options={"format": "best"},
        progress_hook=events.append,
        postprocessor_hook=post_events.append,
    )

    options = FakeYDL.created_options[-1]
    assert FakeYDL.seen_calls[-1][1] is True
    assert options["format"] == "best"
    assert options["progress_hooks"] == [events.append]
    assert options["postprocessor_hooks"] == [post_events.append]

    with pytest.raises(InputError, match="cannot be overridden"):
        adapter.download(
            "https://example.invalid/watch?v=abc",
            options={"postprocessor_hooks": [object()]},
        )


def test_empty_url_is_rejected_before_engine_call() -> None:
    adapter = YtDlpAdapter(ydl_factory=FakeYDL)

    with pytest.raises(InputError, match="non-empty"):
        adapter.extract_info("   ")

    assert FakeYDL.created_options == []


def test_missing_metadata_is_a_stable_download_error() -> None:
    adapter = YtDlpAdapter(ydl_factory=FakeYDL)
    FakeYDL.response = None

    with pytest.raises(DownloadError, match="no usable metadata"):
        adapter.extract_info("https://example.invalid/watch?v=abc")


def test_ytdlp_error_is_redacted_classified_and_wrapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeEngineError(Exception):
        pass

    monkeypatch.setattr(ytdlp_module, "_YtDlpDownloadError", FakeEngineError)
    adapter = YtDlpAdapter(ydl_factory=FakeYDL)
    FakeYDL.error = FakeEngineError("token=secret-value request failed")

    with pytest.raises(DownloadError) as caught:
        adapter.extract_info("https://example.invalid/watch?v=abc")

    assert "secret-value" not in str(caught.value)
    assert caught.value.category == "unknown"
    assert caught.value.retryable is False
    assert str(caught.value) == "The media request failed with an unclassified error."


def test_youtube_request_fails_fast_without_supported_js_runtime() -> None:
    adapter = YtDlpAdapter(
        ydl_factory=FakeYDL,
        js_runtime_detector=lambda: None,
        ejs_available=lambda: True,
    )

    with pytest.raises(DependencyError, match="supported JavaScript runtime"):
        adapter.extract_info("https://www.youtube.com/watch?v=abc")

    assert FakeYDL.created_options == []


def test_youtube_request_fails_fast_without_ejs_solver() -> None:
    adapter = YtDlpAdapter(
        ydl_factory=FakeYDL,
        js_runtime_detector=lambda: JavaScriptRuntime("node", "/opt/node", "v22.12.0"),
        ejs_available=lambda: False,
    )

    with pytest.raises(DependencyError, match="EJS challenge-solver"):
        adapter.extract_info("https://www.youtube.com/watch?v=abc")

    assert FakeYDL.created_options == []


def test_extraction_warning_prevents_false_unavailable_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeEngineError(Exception):
        pass

    monkeypatch.setattr(ytdlp_module, "_YtDlpDownloadError", FakeEngineError)
    adapter = YtDlpAdapter(ydl_factory=FakeYDL)
    FakeYDL.warning_before_error = (
        "[youtube] No title found in player responses; falling back to title from initial data"
    )
    FakeYDL.error = FakeEngineError("Video unavailable")

    with pytest.raises(DownloadError) as caught:
        adapter.extract_info("https://example.invalid/watch?v=abc")

    assert caught.value.category == "extractor"
    assert caught.value.retryable is False
    assert "before availability could be determined" in str(caught.value)


def test_installed_ytdlp_reports_version() -> None:
    assert YtDlpAdapter.is_available()
    assert YtDlpAdapter(ydl_factory=FakeYDL).version != "unavailable"
