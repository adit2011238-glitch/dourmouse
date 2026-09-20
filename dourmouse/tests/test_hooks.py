"""Tests for dourmouse/hooks.py (Domain H, piece 4: deterministic hooks).

Every test clears the module-level registries first/after, matching
message_bus's own set_message_bus(None) isolation convention -- hooks are
process-wide state and must never leak between tests.
"""

from __future__ import annotations

import pytest

from dourmouse import hooks


@pytest.fixture(autouse=True)
def _clean_hooks():
    hooks.clear_hooks()
    yield
    hooks.clear_hooks()


class TestPreToolHooks:
    def test_no_hooks_registered_allows_everything(self):
        assert hooks.run_pre_tool_hooks("any_tool", {}) is None

    def test_a_hook_returning_none_allows(self):
        hooks.register_pre_tool_hook(lambda name, args: None)
        assert hooks.run_pre_tool_hooks("any_tool", {}) is None

    def test_a_hook_returning_a_string_blocks(self):
        hooks.register_pre_tool_hook(lambda name, args: "not on my watch")
        assert hooks.run_pre_tool_hooks("delete_path", {}) == "not on my watch"

    def test_first_blocking_hook_wins_short_circuit(self):
        calls = []

        def first(name, args):
            calls.append("first")
            return "blocked"

        def second(name, args):
            calls.append("second")
            return "also blocked"

        hooks.register_pre_tool_hook(first)
        hooks.register_pre_tool_hook(second)
        assert hooks.run_pre_tool_hooks("x", {}) == "blocked"
        assert calls == ["first"]  # second hook never runs once one blocks

    def test_a_raising_hook_has_no_opinion_never_blocks(self):
        def broken(name, args):
            raise RuntimeError("boom")

        hooks.register_pre_tool_hook(broken)
        assert hooks.run_pre_tool_hooks("x", {}) is None

    def test_a_raising_hook_does_not_stop_a_later_hook_from_blocking(self):
        def broken(name, args):
            raise RuntimeError("boom")

        hooks.register_pre_tool_hook(broken)
        hooks.register_pre_tool_hook(lambda name, args: "real block")
        assert hooks.run_pre_tool_hooks("x", {}) == "real block"

    def test_hook_receives_real_tool_name_and_arguments(self):
        seen = {}

        def capture(name, args):
            seen["name"] = name
            seen["args"] = args
            return None

        hooks.register_pre_tool_hook(capture)
        hooks.run_pre_tool_hooks("delete_path", {"path": "/tmp/x"})
        assert seen == {"name": "delete_path", "args": {"path": "/tmp/x"}}


class TestPostToolHooks:
    def test_fires_with_the_real_result(self):
        seen = []
        hooks.register_post_tool_hook(lambda name, args, result: seen.append((name, args, result)))
        hooks.run_post_tool_hooks("web_search", {"q": "x"}, "ERROR: timed out")
        assert seen == [("web_search", {"q": "x"}, "ERROR: timed out")]

    def test_a_raising_hook_never_breaks_the_call(self):
        hooks.register_post_tool_hook(lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
        hooks.run_post_tool_hooks("x", {}, "ok")  # must not raise

    def test_cannot_alter_the_result_it_is_a_pure_observer(self):
        # There is no return-value contract for post-tool hooks at all --
        # nothing in run_post_tool_hooks reads a hook's return value.
        hooks.register_post_tool_hook(lambda name, args, result: "mutated!")
        hooks.run_post_tool_hooks("x", {}, "original")  # no crash, no effect to observe


class TestStopAndSessionHooks:
    def test_stop_hook_fires_with_the_report(self):
        seen = []
        hooks.register_stop_hook(seen.append)
        report = {"final_text": "done", "transcript": []}
        hooks.run_stop_hooks(report)
        assert seen == [report]

    def test_session_start_hook_fires_with_session_id(self):
        seen = []
        hooks.register_session_start_hook(seen.append)
        hooks.run_session_start_hooks("session_20260920_120000")
        assert seen == ["session_20260920_120000"]

    def test_session_stop_hook_fires_with_session_id(self):
        seen = []
        hooks.register_session_stop_hook(seen.append)
        hooks.run_session_stop_hooks("session_20260920_120000")
        assert seen == ["session_20260920_120000"]

    def test_raising_stop_and_session_hooks_never_break(self):
        def broken(*a):
            raise RuntimeError("boom")

        hooks.register_stop_hook(broken)
        hooks.register_session_start_hook(broken)
        hooks.register_session_stop_hook(broken)
        hooks.run_stop_hooks({})
        hooks.run_session_start_hooks("s")
        hooks.run_session_stop_hooks("s")  # none of these may raise


class TestClearHooks:
    def test_clear_hooks_removes_everything(self):
        hooks.register_pre_tool_hook(lambda n, a: "block")
        hooks.register_post_tool_hook(lambda n, a, r: None)
        hooks.register_stop_hook(lambda r: None)
        hooks.register_session_start_hook(lambda s: None)
        hooks.register_session_stop_hook(lambda s: None)
        hooks.clear_hooks()
        assert hooks.run_pre_tool_hooks("x", {}) is None
        assert hooks._pre_tool_hooks == []
        assert hooks._post_tool_hooks == []
        assert hooks._stop_hooks == []
        assert hooks._session_start_hooks == []
        assert hooks._session_stop_hooks == []
