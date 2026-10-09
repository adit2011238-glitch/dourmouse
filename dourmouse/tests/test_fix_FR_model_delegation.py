"""FR fix P5-30: delegate()'s timeout must be enforced."""

from __future__ import annotations

import threading
import time

from dourmouse import model_delegation as md
from dourmouse.model_delegation import DelegationResult, DelegationTask, delegate


def test_one_stuck_task_does_not_hold_back_the_finished_answers(monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(md, "_DELEGATE_GRACE_S", 0.2)
    monkeypatch.setattr(md, "route_for", lambda agent, allow_cloud=True: md.LOCAL)

    def runner(task, timeout):
        if task.prompt == "stuck":
            release.wait(10)  # a stalled local model turn
        return DelegationResult(task=task, ok=True, text=f"answer to {task.prompt}", model_used=md.LOCAL)

    monkeypatch.setattr(md, "_run_local", runner)
    tasks = [DelegationTask(prompt="a"), DelegationTask(prompt="stuck"), DelegationTask(prompt="b")]
    started = time.monotonic()
    try:
        results = delegate(tasks, timeout=0.3, max_workers=3)
        elapsed = time.monotonic() - started
    finally:
        release.set()
    assert elapsed < 3.0  # before the fix: blocked until the stuck worker ended (10 s)
    assert [r.ok for r in results] == [True, False, True]
    assert results[0].text == "answer to a" and results[2].text == "answer to b"
    assert "timed out" in results[1].error


def test_run_local_stops_a_turn_that_outlives_its_timeout(monkeypatch):
    """_run_local used to ignore its timeout argument entirely."""
    import dourmouse.dispatch as dispatch_mod

    seen = {}

    def fake_run(messages, registry, **kw):
        stop = kw["should_stop"]
        seen["stop_before"] = stop()
        time.sleep(1.2)
        seen["stop_after"] = stop()
        return {"final_text": "", "transcript": [], "messages": messages}

    monkeypatch.setattr(dispatch_mod, "run_dispatch_messages", fake_run)
    monkeypatch.setattr("dourmouse.general_roster.build_general_registry", lambda: object())
    result = md._run_local(DelegationTask(prompt="slow", agent="news"), timeout=1.0)
    assert seen["stop_before"] is False and seen["stop_after"] is True
    assert result.ok is False and "did not finish" in result.error
