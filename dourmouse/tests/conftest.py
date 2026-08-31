"""Shared test fixtures (v5.6)."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _workspace_isolated(tmp_path_factory, monkeypatch):
    """v5.22.14 (audit fix): redirect the default workspace to a per-test
    tmp dir so NO test can write sessions/facts into the REAL workspace
    (Rule 2.1 hermetic — pre-fix, HTTP-based tests leaked stub sessions
    like "draft the quarterly report" into workspace/sessions/ on live
    runs). Tests that need a specific workspace set DOURMOUSE_WORKSPACE
    themselves, which overrides this fixture's value.

    v5.x: uses tmp_path_factory.mktemp("ws") instead of tmp_path — the
    per-test tmp_path is NAMED AFTER THE TEST FUNCTION, and that name is
    embedded into every sandboxed tool description via _sandbox_path_note
    ("'path' is RELATIVE to the workspace root <path>"), which leaks the
    test name as searchable tokens into the registry. A query containing a
    word from the test name (e.g. "run a terminal command" vs
    test_run_terminal_ranks_system_first) then scored a spurious haystack
    hit and flipped agent ranking. A fixed short basename has no query-
    meaningful tokens; mktemp still returns a unique dir per test.
    """
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path_factory.mktemp("ws")))


@pytest.fixture(autouse=True)
def _neuro_off(monkeypatch):
    """v5.6: keep the neural orchestrator hermetic.

    No test may read or write the REAL workspace/neuro store (learned state
    is runtime state, and planner/dispatch blend in live predictions once
    the net is trained). Tests that exercise the net opt in by setting
    DOURMOUSE_NET=1 + DOURMOUSE_NET_DIR=<tmp> inside the test.
    """
    monkeypatch.setenv("DOURMOUSE_NET", "0")


@pytest.fixture(autouse=True)
def _memory_remote_isolated(monkeypatch):
    """v13.4 (hermetic-test-caught, real bug — same failure mode
    _user_config_isolated below already documents once): this developer's
    REAL project .env now sets DOURMOUSE_MEMORY_REMOTE_URL (the shared RAG
    database genuinely moved to another machine, 2026-08-31) —
    dourmouse.config's own module-level load_dotenv() picks that up into
    os.environ the moment ANY test imports it, completely independent of
    any individual test's own monkeypatch.setenv/delenv calls (those only
    revert what THAT test changed, not a value already set before the
    test ran). Caught immediately: test_open_default_store_returns_none_
    when_fts5_missing started returning a live RemoteMemoryStore instead
    of the local-mode None it asserts, purely because it ran on this
    specific machine's real .env instead of a clean one. Tests that want
    remote mode set the two env vars themselves (see test_learn.py's own
    test_open_default_store_uses_remote_when_configured), same override-
    the-fixture convention every other isolation fixture here already
    uses.
    """
    monkeypatch.delenv("DOURMOUSE_MEMORY_REMOTE_URL", raising=False)
    monkeypatch.delenv("DOURMOUSE_MEMORY_REMOTE_TOKEN", raising=False)


@pytest.fixture(autouse=True)
def _hands_free_off(monkeypatch):
    """Same real leak class as _memory_remote_isolated above, applied
    proactively: no test server should ever try to open a real
    microphone (dourmouse/hands_free.py, dourmouse/wakeword.py) just
    because DOURMOUSE_HANDS_FREE happens to be set in this developer's
    real .env. run_server()'s own hands-free wiring is wrapped so a
    disabled/failed start never crashes server startup either way, but
    tests should never even attempt it."""
    monkeypatch.delenv("DOURMOUSE_HANDS_FREE", raising=False)
    monkeypatch.delenv("DOURMOUSE_WAKEWORD", raising=False)


@pytest.fixture(autouse=True)
def _denoise_off(monkeypatch):
    """v13.5: dourmouse/audio_denoise.py's RnnoiseDenoiser is real and
    live-verified (see test_audio_denoise.py, which explicitly opts back
    in) but constructing one loads a real ctypes C library — genuine,
    measurable startup latency that broke an existing timing-sensitive
    test_hands_free.py test (a 0.05s window for the fake stream to open
    was no longer enough once record_utterance() started constructing a
    real RnnoiseDenoiser by default before opening the stream at all).
    Same "hermetic by default, opt in explicitly" convention as every
    other fixture in this file: DOURMOUSE_DENOISE=0 here means
    create_default() returns None on a cheap env check, no library load,
    unless a test sets the env var itself (test_audio_denoise.py does).
    """
    monkeypatch.setenv("DOURMOUSE_DENOISE", "0")


@pytest.fixture(autouse=True)
def _ollama_cloud_isolated(monkeypatch):
    """v13.5: the SAME real leak class as _memory_remote_isolated and
    _hands_free_off above, caught proactively this time (before it broke
    anything) rather than live — this developer's real .env sets a real
    OLLAMA_API_KEY (Ollama Cloud key, wired for the first time in this
    pass; see config.load_ollama_config's own docstring for the "silently
    discarded API key" bug it fixes). Once that env var actually DOES
    something (before this fix it was read into nothing, so leaking it
    into a test process was harmless), any test asserting the default
    LOCAL Ollama config (api_key=="", base_url==127.0.0.1) would start
    failing on this specific machine the same way test_open_default_
    store_returns_none_when_fts5_missing already did once for
    DOURMOUSE_MEMORY_REMOTE_URL. Tests that want cloud mode set
    OLLAMA_API_KEY themselves, same override-the-fixture convention every
    other isolation fixture here already uses.
    """
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_CLOUD_MODEL", raising=False)
    # v13.5: same real leak, same session — this developer's .env now also
    # sets DOURMOUSE_FAST_LANE_MODEL_SWAP=0 (see config.
    # fast_lane_model_swap_enabled's own docstring), which broke every
    # existing fast-lane test asserting the swap TO qwen2.5:7b/qwen3:4b
    # actually happens (they never touched this brand-new env var
    # themselves, same as every prior incident in this file).
    monkeypatch.delenv("DOURMOUSE_FAST_LANE_MODEL_SWAP", raising=False)


@pytest.fixture(autouse=True)
def _gdelt_poller_off(monkeypatch):
    """v13.6: same real leak class as _hands_free_off/_denoise_off above,
    caught proactively before it ever bit — no test server should ever
    open a real network connection to data.gdeltproject.org just because
    a test happens to call run_server(reporting=True, live_polling=True).
    test_gdelt_graph.py opts back in explicitly (monkeypatching the fetch
    functions rather than really re-enabling the poller) the same way
    test_audio_denoise.py opts back into denoising."""
    monkeypatch.setenv("DOURMOUSE_GDELT_POLLER", "0")


@pytest.fixture(autouse=True)
def _user_config_isolated(tmp_path_factory, monkeypatch):
    """v13 (hermetic-test-caught, real bug): every test touching
    orchestrator-model settings, Grounded Mode, or (new) the MCP bridge's
    config file was silently reading/writing the REAL developer's
    ``~/Library/Application Support/Dourmouse/.env`` via
    config.user_config_dir() — no isolation existed for it at all, unlike
    DOURMOUSE_WORKSPACE above. Concretely caught: DOURMOUSE_GROUNDED_MODE=1,
    persisted during Grounded Mode's own earlier live verification on this
    machine, leaked into unrelated dispatch tests and silently added an
    extra grounded-mode nudge turn, exhausting fake clients sized for the
    setting-off case (test_planner.py::TestPlanEventInTranscript). Same
    fixed-short-basename reasoning as _workspace_isolated above: a bare
    "cfg" avoids leaking the test name as a query-meaningful token should
    anything ever embed this path into a tool description the way
    _sandbox_path_note does for the workspace root.
    """
    monkeypatch.setenv("DOURMOUSE_CONFIG_DIR", str(tmp_path_factory.mktemp("cfg")))
