import mediadl.engines.js_runtime as js_runtime_module
from mediadl.engines.js_runtime import (
    JavaScriptRuntime,
    detect_js_runtime,
    probe_js_runtimes,
)


def test_js_runtime_detection_prefers_supported_deno_then_node() -> None:
    available = {"node": "/opt/node", "deno": "/opt/deno"}
    versions = {"/opt/node": "v22.12.0", "/opt/deno": "deno 2.4.5"}
    runtime = detect_js_runtime(available.get, versions.get)

    assert runtime == JavaScriptRuntime("deno", "/opt/deno", "deno 2.4.5")
    assert runtime.ytdlp_options() == {
        "js_runtimes": {"deno": {"path": "/opt/deno"}},
    }


def test_js_runtime_detection_uses_supported_node_when_deno_is_missing() -> None:
    runtime = detect_js_runtime(
        {"node": "/usr/bin/node"}.get,
        lambda _: "v22.11.0",
    )

    assert runtime == JavaScriptRuntime("node", "/usr/bin/node", "v22.11.0")


def test_js_runtime_rejects_node_20_as_unsupported() -> None:
    finder = {"node": "/usr/bin/node"}.get
    runtime = detect_js_runtime(finder, lambda _: "v20.20.2")
    probes = probe_js_runtimes(finder, lambda _: "v20.20.2")

    assert runtime is None
    assert len(probes) == 1
    assert probes[0].name == "node"
    assert probes[0].supported is False
    assert probes[0].requirement == ">=22.0.0"


def test_bundled_deno_is_preferred_over_unsupported_system_node(monkeypatch) -> None:
    monkeypatch.setattr(js_runtime_module, "_bundled_deno_path", lambda: "/bundle/deno")
    finder = {"node": "/usr/bin/node"}.get
    versions = {"/bundle/deno": "deno 2.9.5", "/usr/bin/node": "v20.20.2"}

    runtime = detect_js_runtime(finder, versions.get)
    probes = probe_js_runtimes(finder, versions.get)

    assert runtime == JavaScriptRuntime("deno", "/bundle/deno", "deno 2.9.5", bundled=True)
    assert probes[0].bundled is True
    assert probes[0].supported is True
    assert probes[1].name == "node"
    assert probes[1].supported is False


def test_js_runtime_detection_is_optional() -> None:
    assert detect_js_runtime(lambda _: None) is None
