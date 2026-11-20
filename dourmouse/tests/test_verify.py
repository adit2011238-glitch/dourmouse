"""Finding 208 (plan PL-07): state-changing tools are read back and the app writes the outcome."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from dourmouse import dispatch, system_access, verify
from dourmouse.os_api import Request, verification


def _spec(name):
    registry_specs = {}
    for sub in [system_access.build_system_subagent()] if hasattr(system_access, "build_system_subagent") else []:
        for tool in sub.tools:
            registry_specs[tool.name] = tool
    return registry_specs[name]


@pytest.fixture
def seen(monkeypatch):
    events = []
    from dourmouse import execution_policy

    monkeypatch.setattr(execution_policy, "_sink", lambda kind, st, sid, actor, payload: events.append((kind, sid, payload)))
    return events


def _run(spec, args):
    return dispatch._execute_tool(spec, args, lambda prompt: True)


def test_write_path_is_read_back_and_verified(tmp_path, seen):
    target = tmp_path / "note.txt"
    out = _run(_spec("write_path"), {"path": str(target), "content": "hello"})
    assert out.startswith("WROTE") and "Verified" in out.splitlines()[-1]
    outcome = verify.take_last()
    assert outcome.status == verify.VERIFIED and outcome.tool_class == "save"
    kinds = [e[0] for e in seen]
    assert "action.verified" in kinds
    assert [e for e in seen if e[0] == "action.verified"][0][2]["status"] == "verified"


def test_a_tool_that_says_wrote_but_did_not_is_failed(tmp_path):
    lying = dispatch.ToolSpec(
        name="write_path", description="x", parameters={"type": "object", "properties": {}},
        handler=lambda a: f"WROTE {a['path']} (5 chars)",
    )
    out = _run(lying, {"path": str(tmp_path / "ghost.txt"), "content": "hello"})
    assert "Failed" in out.splitlines()[-1] and "not readable" in out
    assert verify.take_last().status == verify.FAILED


def test_content_that_differs_is_failed(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("other")
    lying = dispatch.ToolSpec(name="write_path", description="x", parameters={"type": "object", "properties": {}},
                              handler=lambda a: "WROTE it")
    out = _run(lying, {"path": str(target), "content": "hello"})
    assert verify.take_last().status == verify.FAILED and "does not hold" in out


def test_delete_path_is_verified_when_gone_and_failed_when_still_there(tmp_path):
    gone = tmp_path / "gone.txt"
    gone.write_text("x")
    out = _run(_spec("delete_path"), {"path": str(gone)})
    assert out.startswith("DELETED") and verify.take_last().status == verify.VERIFIED
    still = tmp_path / "still.txt"
    still.write_text("x")
    lying = dispatch.ToolSpec(name="delete_path", description="x", parameters={"type": "object", "properties": {}},
                              handler=lambda a: "DELETED it")
    _run(lying, {"path": str(still)})
    assert verify.take_last().status == verify.FAILED


def test_search_replace_and_patch_are_verified_against_the_file(tmp_path):
    target = tmp_path / "code.py"
    target.write_text("a = 1\nb = 2\n")
    patch = "<<<<<<< SEARCH\na = 1\n=======\na = 10\n>>>>>>> REPLACE\n"
    out = _run(_spec("apply_search_replace"), {"path": str(target), "patch": patch})
    assert "Verified" in out.splitlines()[-1], out
    verify.take_last()
    noop = dispatch.ToolSpec(name="apply_search_replace", description="x", parameters={"type": "object", "properties": {}},
                             handler=lambda a: "APPLIED 1 block")
    _run(noop, {"path": str(target), "patch": "<<<<<<< SEARCH\nb = 2\n=======\nb = 99\n>>>>>>> REPLACE\n"})
    assert verify.take_last().status == verify.FAILED
    diff = "--- a\n+++ a\n@@ -1,2 +1,2 @@\n-a = 10\n+a = 11\n b = 2\n"
    _run(dispatch.ToolSpec(name="apply_patch", description="x", parameters={"type": "object", "properties": {}},
                           handler=lambda a: "PATCHED"), {"path": str(target), "diff": diff})
    assert verify.take_last().status == verify.FAILED


def test_a_tool_with_no_read_back_is_not_checked_with_a_reason():
    sent = dispatch.ToolSpec(name="send_message", description="x", parameters={"type": "object", "properties": {}},
                             handler=lambda a: "Message sent")
    out = _run(sent, {"to": "x", "text": "y"})
    outcome = verify.take_last()
    assert outcome.status == verify.NOT_CHECKED and "cannot be read back" in out


def test_read_only_tools_and_failed_calls_get_no_outcome():
    reader = dispatch.ToolSpec(name="read_path", description="x", parameters={"type": "object", "properties": {}},
                               handler=lambda a: "contents")
    assert "Dourmouse check" not in _run(reader, {"path": "/x"})
    assert verify.take_last() is None
    failed = dispatch.ToolSpec(name="write_path", description="x", parameters={"type": "object", "properties": {}},
                               handler=lambda a: "ERROR: could not write")
    assert "Dourmouse check" not in _run(failed, {"path": "/x", "content": ""})
    assert verify.take_last() is None


def test_a_crashing_check_reads_as_not_checked_never_as_verified(monkeypatch):
    monkeypatch.setitem(verify.VERIFIERS, "write_path", lambda *a: 1 / 0)
    outcome = verify.verify("write_path", {"path": "/x", "content": ""}, "WROTE x")
    assert outcome.status == verify.NOT_CHECKED


def test_audit_flags_a_claim_with_no_verified_step():
    note = verify.audit_final_answer("Done. I sent the email to Sam.", [])
    assert "sent" in note and "no step of that kind ran" in note
    note = verify.audit_final_answer("I've saved the file.", [{"status": "failed", "class": "save"}])
    assert "no step of that kind was verified" in note
    note = verify.audit_final_answer("I sent it.", [{"status": "not_checked", "class": "send"}])
    assert "unconfirmed" in note


def test_audit_stays_quiet_when_a_step_was_verified_or_nothing_is_claimed():
    assert verify.audit_final_answer("I saved the file.", [{"status": "verified", "class": "save"}]) == ""
    assert verify.audit_final_answer("Here is how to delete a file: use rm.", []) == ""
    assert verify.audit_final_answer("The file was deleted.", [{"status": "verified", "class": "delete"}]) == ""
    assert verify.audit_final_answer("The file was deleted.", [])  # no step at all


def test_day_counts_and_route(monkeypatch):
    now = time.time()
    events = [
        {"kind": "action.verified", "ts": now, "subject_id": "write_path", "payload": {"status": "verified"}},
        {"kind": "action.verified", "ts": now, "subject_id": "write_path", "payload": {"status": "failed"}},
        {"kind": "action.verified", "ts": now, "subject_id": "send_message", "payload": {"status": "not_checked"}},
        {"kind": "action.verified", "ts": now - 999999, "subject_id": "write_path", "payload": {"status": "verified"}},
        {"kind": "action.executed", "ts": now, "subject_id": "write_path", "payload": {}},
    ]
    out = verify.day_counts(events, now - 3600)
    assert out["counts"] == {"verified": 1, "not_checked": 1, "failed": 1} and out["total"] == 3
    assert out["by_tool"]["write_path"]["failed"] == 1
    log = SimpleNamespace(events_since_time=lambda since, prefix: events)
    status, body = verification.counts(Request(server=SimpleNamespace(office_log=log), query={"hours": ["24"]}))
    assert status == 200 and body["total"] == 3
    with pytest.raises(Exception):
        verification.counts(Request(server=SimpleNamespace(office_log=log), query={"hours": ["nope"]}))


# -- through the real dispatch loop, with a scripted model standing in (the live backend returns 402) --------- #

def _loop_registry(tool):
    from dourmouse.dispatch import DispatchRegistry, Subagent

    registry = DispatchRegistry()
    registry.register_subagent(Subagent(name="echo_agent", domain="Test", description="writes files", tools=(tool,)))
    return registry


def _run_forced(prompt, registry, client):
    messages = [{"role": "system", "content": dispatch.system_message(registry)}, {"role": "user", "content": prompt}]
    return dispatch.run_dispatch_messages(messages, registry, client=client, forced_agent="echo_agent",
                                          confirmation_gate=lambda p: True)


def _scripted(*steps):
    import json as _json

    from dourmouse.tests.test_dispatch import FakeClient, _FakeMessage, _FakeResponse, _FakeToolCall

    out = []
    for step in steps:
        if isinstance(step, str):
            out.append(_FakeResponse(_FakeMessage(content=step)))
        else:
            name, args = step
            out.append(_FakeResponse(_FakeMessage(tool_calls=[_FakeToolCall("c1", name, _json.dumps(args))])))
    return FakeClient(out)


def test_loop_stores_outcome_on_the_transcript_and_stays_quiet_when_the_claim_is_backed(tmp_path, monkeypatch):
    monkeypatch.setenv("DOURMOUSE_FAST_LANE", "0")
    target = tmp_path / "out.txt"
    registry = _loop_registry(_spec("write_path"))
    report = _run_forced("save hello to a file", registry, _scripted(
        ("write_path", {"path": str(target), "content": "hello"}), "I saved the file."))
    results = [e for e in report["transcript"] if e["type"] == "tool_result"]
    assert results and results[0]["verify"]["status"] == "verified"
    assert "Dourmouse check" not in report["final_text"]


def test_loop_audit_flags_a_claim_after_a_failed_step(tmp_path, monkeypatch):
    monkeypatch.setenv("DOURMOUSE_FAST_LANE", "0")
    lying = dispatch.ToolSpec(name="write_path", description="write a file",
                              parameters={"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}},
                              handler=lambda a: "WROTE nothing")
    report = _run_forced("save hello", _loop_registry(lying), _scripted(
        ("write_path", {"path": str(tmp_path / "x.txt"), "content": "hello"}), "I saved the file."))
    assert [e for e in report["transcript"] if e["type"] == "tool_result"][0]["verify"]["status"] == "failed"
    assert "no step of that kind was verified" in report["final_text"]


def test_event_log_returns_events_since_a_time(tmp_path):
    from dourmouse.office_logger import OfficeLogger

    log = OfficeLogger(tmp_path / "office.db")
    log.append_event("action.verified", "tool", "write_path", "a", {"status": "verified"})
    log.append_event("action.executed", "tool", "write_path", "a", {})
    got = log.events_since_time(time.time() - 60, "action.verified")
    assert len(got) == 1 and got[0]["payload"]["status"] == "verified"
    assert log.events_since_time(time.time() + 60, "action.verified") == []
