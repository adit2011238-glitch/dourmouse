"""Unit tests for the All-Hands mode + slash commands (v5.22.9) — hermetic.

The runner's brains are injected fakes (no network, no CLI), the synthesizer
is monkeypatched, and Freebuff dispatch is faked — only the deterministic
orchestration logic is exercised: parallel fan-out, honest error cards,
synthesis-after-all, snapshots, and the slash-command execution paths.
"""

import time

import pytest

from dourmouse import all_hands
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
