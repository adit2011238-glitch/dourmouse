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
def _agent_router_model_off(monkeypatch):
    """Same real leak class as _memory_remote_isolated/_hands_free_off
    above: this developer's REAL .env now sets
    DOURMOUSE_AGENT_ROUTER_AUTO=1 (2026-09-15, user-directed — see
    dourmouse/agent_router_model.py's own docstring), which
    dourmouse.config's module-level load_dotenv() picks up into
    os.environ the moment ANY test imports it — completely independent
    of any individual test's own monkeypatch. Live-caught the same day
    this flag was added: with it leaking through, dozens of unrelated
    dispatch/self_dispatch tests started making REAL network calls to
    this machine's own local Ollama daemon (each with the router's real
    6s timeout on the critical path), turning a ~6 minute suite into a
    ~16 minute one and breaking a real timing-sensitive concurrency
    test outright. Tests that want to exercise the router explicitly
    set DOURMOUSE_AGENT_ROUTER_AUTO=1 themselves (see
    test_dispatch.py::TestLocalAgentRouterModelWinsOverKeywordScorer),
    same override-the-fixture convention every other isolation fixture
    here already uses."""
    monkeypatch.delenv("DOURMOUSE_AGENT_ROUTER_AUTO", raising=False)


@pytest.fixture(autouse=True)
def _claude_front_mode_off(monkeypatch):
    """Same "hermetic by default, opt in explicitly" convention as every
    other fixture in this file: dispatch.py's Claude-front mode now
    defaults to ON (the user's own explicit ask — Claude-front by
    default, real Claude Code CLI subprocess calls for a plain unmatched
    query and every heavy-workflow agent). Left on by default, every
    dispatch test unrelated to this specific feature (per-agent model
    overrides, fast-lane routing, the plain "no override" client-
    construction tests, ...) would silently start routing through a real
    subprocess call instead of the fake/local client they were actually
    written to exercise — confirmed: 11 real test failures the moment
    this default flipped, none of them about Claude-front mode itself.

    A real env var, not a function monkeypatch on config.py — matching
    every other fixture here (_denoise_off sets DOURMOUSE_DENOISE=0 the
    same way), and deliberately so: dispatch._orchestrator_backend_mode()
    checks this env var BEFORE ever consulting
    config.claude_front_mode_enabled(), so setting it here means
    config.py's OWN tests (TestClaudeFrontModeSetting) exercise the real,
    unpatched function untouched — a function-level monkeypatch here
    would have silently broken every one of those instead.
    Tests that specifically exercise Claude-front mode
    (TestOrchestratorBackendMode and friends) explicitly override this
    env var themselves."""
    monkeypatch.setenv("DOURMOUSE_ORCHESTRATOR_MODE", "off")


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


# Env keys that dourmouse/desktop_rag.py reads to decide whether the desktop
# spatial-vault RAG bridge is configured. If any of the first three (HOST/
# USER/KEY) are present, desktop_rag_status() will shell out to a real ssh
# subprocess against the real remote desktop.
_DESKTOP_RAG_ENV_KEYS = (
    "DOURMOUSE_DESKTOP_RAG_HOST",
    "DOURMOUSE_DESKTOP_RAG_USER",
    "DOURMOUSE_DESKTOP_RAG_KEY",
    "DOURMOUSE_DESKTOP_RAG_DB",
    "DOURMOUSE_DESKTOP_RAG_INDEX",
    "DOURMOUSE_DESKTOP_RAG_TABLE",
    "DOURMOUSE_DESKTOP_RAG_MODEL",
    "DOURMOUSE_DESKTOP_RAG_PYTHON",
    "DOURMOUSE_DESKTOP_RAG_ID_FILTER_SQL",
    "DOURMOUSE_DESKTOP_RAG_ID_ORDER_SQL",
    "DOURMOUSE_DESKTOP_RAG_TIMEOUT",
    "DOURMOUSE_DESKTOP_RAG_PROBE_TIMEOUT",
)


@pytest.fixture(autouse=True)
def _desktop_rag_env_isolated(monkeypatch):
    """Same real leak class as _memory_remote_isolated / _ollama_cloud_isolated
    above. config.py's module-level load_dotenv() pulls this dev machine's real
    .env into os.environ on import, and this machine's .env sets
    DOURMOUSE_DESKTOP_RAG_HOST/USER/KEY. Any test that reaches
    model_context.claude_orchestrator_preamble() (test_model_context.py,
    test_google_workspace_agent.py) then has desktop_rag.desktop_rag_status()
    fire a REAL ssh subprocess at the real remote desktop — bounded (~26s
    worst case via desktop_rag's own timeouts) but real, slow, and
    network-dependent, violating Rule 2.1 (never touches the network).

    test_desktop_rag.py had its own local copy of this fixture; it now lives
    here so all three files share one. Tests that genuinely want the bridge
    configured set the env vars themselves, same override-the-fixture
    convention as every other isolation fixture in this file.
    """
    for key in _DESKTOP_RAG_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _goal_runtime_off(monkeypatch):
    """2026-09-18: goal_runtime_enabled() flipped from opt-in to opt-out
    (a real live bug it was causing, see its own docstring) -- meaning
    every test that spins up a real run_server() would now ALSO start a
    real background GoalRuntime worker thread ticking every 5 real
    seconds, where before the same test silently got an inert one. Left
    unguarded, that thread would tick against whatever the process-wide
    goal-store singleton holds at that moment -- including goals a
    completely unrelated test created in the same singleton, if that
    test's own isolation is imperfect -- and _run_task really does call
    ChatSession.ask() against real backends. Same "hermetic by default,
    opt in explicitly" convention as every other fixture in this file:
    tests that specifically exercise the real runtime-starting-in-webui
    behavior (test_goal_runtime.py's own tests construct GoalRuntime
    directly and are unaffected by this) set the env var themselves.
    """
    monkeypatch.setenv("DOURMOUSE_GOAL_RUNTIME", "0")


@pytest.fixture(autouse=True)
def _fresh_fetch_politeness(monkeypatch):
    """Finding #094: the process-wide politeness gate caches robots.txt per
    host and remembers when each host may next be fetched. Tests serve pages
    from 127.0.0.1 on reused ports, so a shared gate would carry one test's
    robots rules and delays into the next. Each test gets a fresh gate with
    no minimum interval; the politeness tests configure their own."""
    from dourmouse.research_pipeline import politeness

    monkeypatch.setattr(politeness, "POLITENESS", politeness.Politeness(min_interval=0.0))


@pytest.fixture(autouse=True)
def _security_sentry_off(monkeypatch):
    """Finding #084: sentry_runtime_enabled() is default ON, so every test
    that built a real run_server() also started a real SentryRuntime that
    shelled out to arp, lsof and the firewall tool against the real Mac
    and never stopped. Hundreds of them piled up across one suite run
    (arp -a was being spawned every few seconds by the end), loading the
    machine enough to fail timing-sensitive tests, and they wrote into
    the real workspace sentry.db. Same hermetic-by-default convention as
    _goal_runtime_off above: tests of the runtime itself construct
    SentryRuntime directly or set the variable themselves.
    """
    monkeypatch.setenv("DOURMOUSE_SECURITY_SENTRY_LOOP", "0")
    # Finding #101: the same for the Downloads watcher, which would otherwise
    # poll the developer's real ~/Downloads from every server a test builds.
    monkeypatch.setenv("DOURMOUSE_DOWNLOADS_WATCH", "0")
