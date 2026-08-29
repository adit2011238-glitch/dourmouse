"""Unit tests for the All-Hands mode + slash commands (v5.22.9) — hermetic.

The runner's brains are injected fakes (no network, no CLI), the synthesizer
is monkeypatched, and Freebuff dispatch is faked — only the deterministic
orchestration logic is exercised: parallel fan-out, honest error cards,
synthesis-after-all, snapshots, and the slash-command execution paths.
"""

import time

import pytest

from dourmouse import all_hands, general_roster
from dourmouse.all_hands import AllHandsRunner


def _wait_done(runner, run_id, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = runner.snapshot(run_id)
        if snap and snap["status"] != "running":
            return snap
        time.sleep(0.05)
    raise AssertionError("run did not finish in time")


# -- slash parsing --------------------------------------------------------- #

class TestParseSlash:
    def test_known_command(self):
        assert all_hands.parse_slash("/claude refactor this") == ("claude", "refactor this")

    def test_all_command(self):
        assert all_hands.parse_slash("/all research ev charging") == ("all", "research ev charging")

    def test_unknown_command_is_none(self):
        assert all_hands.parse_slash("/bogus do a thing") is None

    def test_plain_prompt_is_none(self):
        assert all_hands.parse_slash("check my inbox") is None

    def test_empty_text_after_command(self):
        assert all_hands.parse_slash("/codex") == ("codex", "")

    def test_slash_in_middle_is_not_command(self):
        assert all_hands.parse_slash("type /claude later") is None

    def test_whitespace_trimmed(self):
        assert all_hands.parse_slash("  /freebuff draft a memo  ") == ("freebuff", "draft a memo")


class TestDetectAllHands:
    @pytest.mark.parametrize("prompt", [
        "use all resources to find the best laptop",
        "use all your resources on this goal",
        "all hands on deck for the launch",
        "research this use all of the resources you have",
    ])
    def test_detects_all_hands_phrasings(self, prompt):
        assert all_hands.detect_all_hands(prompt) is True

    @pytest.mark.parametrize("prompt", [
        "check my inbox",
        "what is btc worth",
        "use resources wisely",
    ])
    def test_does_not_detect_normal_prompts(self, prompt):
        assert all_hands.detect_all_hands(prompt) is False


# -- the runner ------------------------------------------------------------ #

class TestAllHandsRunner:
    def _runner(self, monkeypatch):
        brains = {
            "claude": lambda task: "CLAUDE RESULT",
            "nvidia": lambda task: "NVIDIA RESULT",
            "deepseek": lambda task: "DEEPSEEK RESULT",
            "codex": lambda task: (_ for _ in ()).throw(
                RuntimeError("NOT CONFIGURED: no OpenAI key")),
            "web": lambda task: "WEB RESULT",
        }
        runner = AllHandsRunner(brain_runners=brains)
        monkeypatch.setattr(all_hands, "_default_brain",
                            lambda task, system=None, force_backend=None: "SYNTHESIS")
        return runner

    def test_fanout_collects_every_brain(self, monkeypatch):
        runner = self._runner(monkeypatch)
        run_id = runner.start("the goal")
        snap = _wait_done(runner, run_id)
        assert snap["status"] == "done"
        assert snap["goal"] == "the goal"
        by = {k: b["status"] for k, b in snap["brains"].items()}
        assert by == {"claude": "done", "nvidia": "done", "deepseek": "done",
                      "codex": "error", "web": "done"}
        assert snap["brains"]["claude"]["result"] == "CLAUDE RESULT"
        # The honest NOT CONFIGURED card is a red error, never fabricated.
        assert "NOT CONFIGURED" in snap["brains"]["codex"]["error"]
        assert snap["synthesis"] == "SYNTHESIS"

    def test_running_and_snapshot_shapes(self, monkeypatch):
        runner = self._runner(monkeypatch)
        run_id = runner.start("g2")
        snap = runner.snapshot(run_id)
        assert snap is not None
        assert set(snap.keys()) >= {"id", "goal", "status", "brains", "synthesis", "started"}
        # Every brain starts in the roster before any result lands.
        assert set(snap["brains"].keys()) == {"claude", "nvidia", "deepseek", "codex", "web"}
        for b in snap["brains"].values():
            assert b["status"] in ("pending", "running", "done", "error")
        assert runner.snapshot("bogus") is None

    def test_all_runs_newest_first(self, monkeypatch):
        runner = self._runner(monkeypatch)
        a = runner.start("a"); b = runner.start("b")
        _wait_done(runner, a); _wait_done(runner, b)
        ids = [r["id"] for r in runner.all_runs()]
        assert ids == [a, b]  # insertion order

    def test_empty_goal_rejected(self):
        runner = AllHandsRunner()
        with pytest.raises(ValueError):
            runner.start("   ")

    def test_running_count(self, monkeypatch):
        brains = {"claude": lambda task: time.sleep(0.3) or "slow",
                  "nvidia": lambda task: "fast"}
        runner = AllHandsRunner(brain_runners=brains)
        monkeypatch.setattr(all_hands, "_default_brain",
                            lambda task, system=None, force_backend=None: "S")
        run_id = runner.start("g")
        snap = runner.snapshot(run_id)
        assert snap["status"] == "running"
        assert runner.running_count() >= 1
        _wait_done(runner, run_id)

    def test_long_error_keeps_the_end_not_the_start(self, monkeypatch):
        """v13: a real bug fixed here, live-caught through an actual /all
        directive against codex — a real, honest CLI error ("You've hit
        your usage limit... try again at Sep 17th, 2026") sat at the END
        of a long exception string (CLI stderr is boilerplate banner + the
        full echoed prompt FIRST, the actual failure LAST). The old
        `[:600]`/`[:240]` truncation kept the banner and threw away the
        one line that explained what actually went wrong."""
        banner = "boilerplate CLI banner and the full echoed prompt " * 20
        real_reason = "You've hit your usage limit. Try again at Sep 17th, 2026."
        long_message = banner + real_reason
        assert len(long_message) > 600  # the scenario this fix targets

        def boom(task):
            raise RuntimeError(long_message)

        runner = AllHandsRunner(brain_runners={"claude": boom})
        monkeypatch.setattr(all_hands, "_default_brain",
                            lambda task, system=None, force_backend=None: "SYNTHESIS")
        run_id = runner.start("g")
        snap = _wait_done(runner, run_id)
        error = snap["brains"]["claude"]["error"]
        assert real_reason in error
        # The real point of the fix: the real reason must survive AT ALL —
        # with the old [:600] head-truncation it never appeared anywhere
        # in the stored error, since the banner alone already exceeded 600
        # chars before the real reason even started.
        assert error.endswith(real_reason)


# -- slash execution ------------------------------------------------------- #

class TestRunSlash:
    def test_all_without_goal_honest(self, monkeypatch):
        monkeypatch.setattr(all_hands, "start_all_hands",
                            lambda goal, owner=None: "run-1")
        out = all_hands.run_slash("all", "  ")
        assert out["ok"] is False
        assert "requires a goal" in out["text"]

    def test_all_starts_run(self, monkeypatch):
        monkeypatch.setattr(all_hands, "start_all_hands",
                            lambda goal, owner=None: "run-abc")
        out = all_hands.run_slash("all", "research ev", owner="u@x.com")
        assert out["ok"] is True
        assert out["run_id"] == "run-abc"

    def test_backend_slash_runs_and_streams_text(self, monkeypatch):
        monkeypatch.setattr(all_hands, "_default_brain",
                            lambda task, force_backend=None: "CLAUDE ANSWER")
        out = all_hands.run_slash("claude", "refactor")
        assert out["ok"] is True and out["text"] == "CLAUDE ANSWER"

    def test_backend_slash_empty_task_honest(self):
        out = all_hands.run_slash("codex", "")
        assert out["ok"] is False

    def test_backend_slash_surfaces_not_configured(self, monkeypatch):
        def boom(task, force_backend=None):
            raise RuntimeError("NOT CONFIGURED: no OpenAI key")
        monkeypatch.setattr(all_hands, "_default_brain", boom)
        out = all_hands.run_slash("chatgpt", "draft")
        assert out["ok"] is False
        assert "NOT CONFIGURED" in out["text"]

    def test_unknown_command_honest(self):
        out = all_hands.run_slash("bogus", "x")
        assert out["ok"] is False
        assert "unknown slash" in out["text"]

    def test_freebuff_slash_dispatches(self, monkeypatch):
        import dourmouse.freebuff_bridge as fb
        monkeypatch.setattr(fb, "freebuff_projects",
                            lambda: [{"path": "/Users/me/proj", "thread_count": 1}])
        monkeypatch.setattr(
            fb, "freebuff_dispatch",
            lambda prompt, project: {"thread": {"id": "tid-123", "title": "T"},
                                     "posted": {"ok": True}},
        )
        out = all_hands.run_slash("freebuff", "draft a memo")
        assert out["ok"] is True
        assert "tid-123" in out["text"]

    def test_freebuff_slash_honest_not_configured(self, monkeypatch):
        import dourmouse.freebuff_bridge as fb
        monkeypatch.setattr(fb, "freebuff_projects",
                            lambda: (_ for _ in ()).throw(fb.FreebuffNotAvailable("app down")))
        out = all_hands.run_slash("freebuff", "draft")
        assert out["ok"] is False
        assert "NOT CONFIGURED" in out["text"]


class TestDefaultBrainWebSearch:
    """v13: a real bug fixed, live-caught through an actual /all directive
    — str.partition() returns exactly a 3-tuple, never 4 values. Every
    call to the web brain raised "ValueError: not enough values to unpack
    (expected 4, got 3)" before this fix — reported as an honest error
    card (the try/except in _run_brain caught it), but a crash all the
    same, on every single All-Hands run."""

    def test_wrapped_task_extracts_the_real_goal_not_the_wrapper(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            general_roster, "_web_search_tool",
            lambda args: (captured.update(query=args["query"]), "RESULT")[1],
        )
        wrapped = "You are one independent worker...\n\nGOAL/TASK: latest Python release"
        result = all_hands._default_brain(wrapped, force_backend="web")
        assert result == "RESULT"
        assert captured["query"] == "latest Python release"

    def test_unwrapped_task_used_as_is(self, monkeypatch):
        """No 'GOAL/TASK: ' marker at all (the reviewer-hardened case this
        function's own docstring already called out) — the whole task
        string is the query."""
        captured = {}
        monkeypatch.setattr(
            general_roster, "_web_search_tool",
            lambda args: (captured.update(query=args["query"]), "RESULT")[1],
        )
        result = all_hands._default_brain("plain query with no marker", force_backend="web")
        assert result == "RESULT"
        assert captured["query"] == "plain query with no marker"

    def test_never_raises_valueerror_on_a_realistic_wrapped_task(self, monkeypatch):
        monkeypatch.setattr(general_roster, "_web_search_tool", lambda args: "ok")
        # Must not raise — this exact shape is what _run_brain always sends.
        all_hands._default_brain(
            f"{all_hands._BRAIN_SYSTEM}\n\nGOAL/TASK: what happened today",
            force_backend="web",
        )


class TestDefaultBrainCodexUsesCliFirst:
    """v13: a real bug fixed, live-caught the same way — codex fell
    straight through to the raw API-key-only path (load_backend +
    _run_openai_compat), reporting "NOT CONFIGURED" even with a real,
    signed-in Codex CLI (confirmed live via /api/connections). claude was
    already special-cased to use run_code_task (CLI first, API-key
    fallback — see run_code_task's own docstring); codex needed the same
    treatment and was simply missing from that branch."""

    def test_codex_routes_through_run_code_task_not_the_raw_api_path(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            all_hands.code_backends, "run_code_task",
            lambda backend, task, timeout=120, cwd=None: calls.append((backend, task)) or "CLI RESULT",
        )
        # If this fell through to the old path, load_backend would be
        # called instead — make that loudly wrong if it happens.
        monkeypatch.setattr(
            all_hands.code_backends, "load_backend",
            lambda name: (_ for _ in ()).throw(AssertionError("should not reach the raw API path")),
        )
        result = all_hands._default_brain("do the task", force_backend="codex")
        assert result == "CLI RESULT"
        assert calls and calls[0][0] == "codex"

    def test_claude_still_routes_through_run_code_task_unchanged(self, monkeypatch):
        """Regression guard: fixing codex must not disturb claude's
        already-working, already-tested CLI-first path."""
        calls = []
        monkeypatch.setattr(
            all_hands.code_backends, "run_code_task",
            lambda backend, task, timeout=120, cwd=None: calls.append(backend) or "CLAUDE RESULT",
        )
        result = all_hands._default_brain("do the task", force_backend="claude")
        assert result == "CLAUDE RESULT"
        assert calls == ["claude"]


class TestSynthesisFallback:
    """v13: a real bug fixed, live-caught through an actual /all directive
    — the synthesizer was hardcoded to force_backend="nvidia" with no
    fallback. Live result before this fix: CLAUDE answered correctly
    ("Paris.") in 7.6s, every other brain reported an honest error, and
    the synthesis came back completely EMPTY because the one hardcoded
    synth backend (nvidia) is the one currently 403'ing on every real call
    (external, documented elsewhere). The entire point of All-Hands is the
    merged final answer — a run where a real brain succeeded but the user
    sees nothing is the worst failure mode this feature specifically can
    have."""

    def test_nvidia_failure_falls_back_to_ollama_for_synthesis(self, monkeypatch):
        def fake_default_brain(task, system=None, force_backend=None):
            if force_backend == "nvidia":
                raise RuntimeError("403 Forbidden")
            if force_backend == "ollama":
                return "SYNTHESIS FROM OLLAMA"
            return "BRAIN RESULT"

        monkeypatch.setattr(all_hands, "_default_brain", fake_default_brain)
        runner = AllHandsRunner(brain_runners={"claude": lambda task: "CLAUDE RESULT"})
        run_id = runner.start("the goal")
        snap = _wait_done(runner, run_id)
        assert snap["status"] == "done"
        assert snap["error"] is None
        assert snap["synthesis"] == "SYNTHESIS FROM OLLAMA"
        assert snap["synth_backend"] == "ollama"

    def test_nvidia_success_still_wins_no_unnecessary_fallback(self, monkeypatch):
        calls = []

        def fake_default_brain(task, system=None, force_backend=None):
            calls.append(force_backend)
            if force_backend == "nvidia":
                return "SYNTHESIS FROM NVIDIA"
            return "BRAIN RESULT"

        monkeypatch.setattr(all_hands, "_default_brain", fake_default_brain)
        runner = AllHandsRunner(brain_runners={"claude": lambda task: "CLAUDE RESULT"})
        run_id = runner.start("the goal")
        snap = _wait_done(runner, run_id)
        assert snap["synthesis"] == "SYNTHESIS FROM NVIDIA"
        assert snap["synth_backend"] == "nvidia"
        assert calls.count("ollama") == 0  # never fell back when nvidia worked

    def test_both_nvidia_and_ollama_failing_reports_honestly_not_silently(self, monkeypatch):
        def fake_default_brain(task, system=None, force_backend=None):
            if force_backend in ("nvidia", "ollama"):
                raise RuntimeError(f"{force_backend} is down")
            return "BRAIN RESULT"

        monkeypatch.setattr(all_hands, "_default_brain", fake_default_brain)
        runner = AllHandsRunner(brain_runners={"claude": lambda task: "CLAUDE RESULT"})
        run_id = runner.start("the goal")
        snap = _wait_done(runner, run_id)
        assert snap["status"] == "done"
        assert snap["synthesis"] is None
        assert "nvidia is down" in snap["error"]
        assert "ollama is down" in snap["error"]
