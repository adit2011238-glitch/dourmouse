"""AGENTSMITH routes (finding #149): board, draft, approve.

The invariants pinned here: the review payload carries the exact module text
and its hash; approve refuses without the hash of what the owner read and
refuses when the draft changed after it was opened; a model has no way to reach
approval; and "approved" is reported separately from "live in the running
server".
"""

from __future__ import annotations

import hashlib
import http.client
import json
import threading

import pytest

from dourmouse import self_extensions as se
from dourmouse.general_roster import build_general_registry

HANDLER = "def handle(arguments: dict) -> str:\n    return 'hi ' + str(arguments.get('who', ''))\n"
TEST = "from dourmouse.self_extensions import load_approved\n\n\ndef test_it():\n    assert load_approved('greet_someone').handle({'who': 'x'}) == 'hi x'\n"
SCHEMA = {"type": "object", "properties": {"who": {"type": "string"}}, "required": ["who"]}


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("DOURMOUSE_CONFIG_DIR", str(tmp_path / "cfg"))
    from dourmouse.webui import run_server

    srv = run_server(build_general_registry(), port=0, client=None, config=None)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=2)


def call(srv, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=20)
    conn.request(method, path, body=json.dumps(body) if body is not None else None)
    resp = conn.getresponse()
    data = json.loads(resp.read() or b"{}")
    conn.close()
    return resp.status, data


def add(name="greet_someone", **kw):
    args = {"capability_gap": "cannot greet", "tool_name": name, "description": "Greets someone by name.",
            "parameters_schema": SCHEMA, "handler_source": HANDLER, "test_source": TEST}
    args.update(kw)
    return se.SelfExtensions().add_draft(**args)


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class TestBoard:
    def test_empty_when_nothing_was_drafted(self, server):
        status, data = call(server, "GET", "/api/os/agentsmith/board")
        assert status == 200 and data["drafts"] == [] and data["total"] == 0 and data["restart_needed"] == 0

    def test_lists_light_fields_newest_first_and_never_the_source(self, server):
        add("first_one_ok")
        add("second_one_ok")
        _, data = call(server, "GET", "/api/os/agentsmith/board")
        assert [d["tool_name"] for d in data["drafts"]] == ["second_one_ok", "first_one_ok"]
        assert data["counts"] == {"DRAFTED": 2}
        for d in data["drafts"]:
            assert "handler_source" not in d and "test_source" not in d and "module_preview" not in d
            assert d["parameters_schema"] == SCHEMA and d["status"] == "DRAFTED"

    def test_goal_id_is_shown_only_when_the_draft_has_one(self, server):
        add("with_goal_ok", goal_id="goal-7")
        add("no_goal_okay")
        _, data = call(server, "GET", "/api/os/agentsmith/board")
        by = {d["tool_name"]: d for d in data["drafts"]}
        assert by["with_goal_ok"]["goal_id"] == "goal-7" and by["no_goal_okay"]["goal_id"] is None

    def test_an_approved_tool_is_not_live_until_the_running_registry_has_it(self, server):
        e = add()
        store = se.SelfExtensions()
        store._set_decision(e["id"], "APPROVED", "test passed, module written")
        _, data = call(server, "GET", "/api/os/agentsmith/board")
        assert data["drafts"][0]["live"] is False and data["restart_needed"] == 1
        assert hasattr(server.registry, "tool_names")  # the attribute the route reads
        live = frozenset(server.registry.tool_names) | {"greet_someone"}
        server.registry = type("R", (), {"tool_names": live})()
        _, data = call(server, "GET", "/api/os/agentsmith/board")
        assert data["drafts"][0]["live"] is True and data["restart_needed"] == 0

    def test_live_is_unknown_not_true_when_the_server_cannot_say(self, server):
        e = add()
        se.SelfExtensions()._set_decision(e["id"], "APPROVED", "ok")
        server.registry = object()
        _, data = call(server, "GET", "/api/os/agentsmith/board")
        assert data["drafts"][0]["live"] is None and data["restart_needed"] == 0

    def test_only_approved_drafts_carry_a_live_flag(self, server):
        add()
        _, data = call(server, "GET", "/api/os/agentsmith/board")
        assert "live" not in data["drafts"][0]

    def test_a_corrupt_line_in_the_store_does_not_break_the_board(self, server):
        add()
        path = se._drafts_path()
        path.write_text(path.read_text() + "{not json\n", encoding="utf-8")
        status, data = call(server, "GET", "/api/os/agentsmith/board")
        assert status == 200 and data["total"] == 1


class TestDraft:
    def test_the_review_payload_has_the_exact_module_and_its_hash(self, server):
        e = add()
        status, data = call(server, "GET", f"/api/os/agentsmith/draft?id={e['id']}")
        d = data["draft"]
        assert status == 200 and d["module_preview"] == se.render_module_source(e)
        assert d["preview_sha256"] == sha(d["module_preview"]) and d["module_lines"] == d["module_preview"].count("\n") + 1
        assert HANDLER.strip() in d["module_preview"] and "REQUIRES_CONFIRMATION" in d["module_preview"]
        assert d["test_source"] == TEST and d["too_large_to_review"] is False

    def test_an_unknown_or_malformed_id(self, server):
        assert call(server, "GET", "/api/os/agentsmith/draft?id=ext-999")[0] == 404
        assert call(server, "GET", "/api/os/agentsmith/draft")[0] == 400
        assert call(server, "GET", "/api/os/agentsmith/draft?id=../x")[0] == 400
        assert call(server, "GET", "/api/os/agentsmith/draft?id=" + "a" * 41)[0] == 400

    def test_a_draft_too_large_to_read_is_not_sent_and_says_so(self, server, monkeypatch):
        from dourmouse.os_api import agentsmith

        monkeypatch.setattr(agentsmith, "MAX_REVIEW_CHARS", 100)
        e = add()
        _, data = call(server, "GET", f"/api/os/agentsmith/draft?id={e['id']}")
        d = data["draft"]
        assert d["too_large_to_review"] is True and d["module_preview"] == "" and d["test_source"] == ""

    def test_an_approved_draft_reports_whether_the_file_on_disk_still_matches(self, server):
        e = add()
        module, digest = se._write_approved_module(e)
        store = se.SelfExtensions()
        store._set_module_hash(e["id"], digest)
        store._set_decision(e["id"], "APPROVED", "ok")
        _, data = call(server, "GET", f"/api/os/agentsmith/draft?id={e['id']}")
        assert data["draft"]["on_disk_matches"] is True and data["draft"]["on_disk_sha256"] == digest
        module.write_text(module.read_text() + "\n# tampered\n")
        _, data = call(server, "GET", f"/api/os/agentsmith/draft?id={e['id']}")
        assert data["draft"]["on_disk_matches"] is False

    def test_an_approved_draft_whose_file_is_missing_is_reported_not_ok(self, server):
        e = add()
        store = se.SelfExtensions()
        store._set_module_hash(e["id"], "0" * 64)
        store._set_decision(e["id"], "APPROVED", "ok")
        _, data = call(server, "GET", f"/api/os/agentsmith/draft?id={e['id']}")
        assert data["draft"]["on_disk_matches"] is False and data["draft"]["on_disk_sha256"] is None


class TestApprove:
    @pytest.fixture
    def approved_calls(self, monkeypatch):
        calls = []

        def fake(entry_id, **kw):
            calls.append(entry_id)
            return {"ok": True, "entry": {"id": entry_id, "tool_name": "greet_someone", "status": "APPROVED", "module_sha256": "a" * 64}, "note": "approved and merged. Restart the server for this tool to become callable."}

        monkeypatch.setattr(se, "approve", fake)
        return calls

    def test_approves_only_the_bytes_that_were_read(self, server, approved_calls):
        e = add()
        _, det = call(server, "GET", f"/api/os/agentsmith/draft?id={e['id']}")
        status, data = call(server, "POST", "/api/os/agentsmith/approve", {"id": e["id"], "sha256": det["draft"]["preview_sha256"]})
        assert status == 200 and data["ok"] is True and approved_calls == [e["id"]]
        assert "Restart the server" in data["note"]

    def test_a_missing_or_malformed_hash_is_refused_and_nothing_runs(self, server, approved_calls):
        e = add()
        for body in ({"id": e["id"]}, {"id": e["id"], "sha256": ""}, {"id": e["id"], "sha256": "abc"}, {"id": e["id"], "sha256": "Z" * 64}):
            assert call(server, "POST", "/api/os/agentsmith/approve", body)[0] == 400
        assert approved_calls == []

    def test_a_draft_that_changed_after_it_was_opened_is_refused(self, server, approved_calls):
        e = add()
        _, det = call(server, "GET", f"/api/os/agentsmith/draft?id={e['id']}")
        seen = det["draft"]["preview_sha256"]
        path = se._drafts_path()
        path.write_text(path.read_text().replace("hi ", "HACKED "), encoding="utf-8")
        status, data = call(server, "POST", "/api/os/agentsmith/approve", {"id": e["id"], "sha256": seen})
        assert status == 409 and "changed after it was opened" in data["error"] and approved_calls == []

    def test_a_wrong_hash_is_refused(self, server, approved_calls):
        e = add()
        assert call(server, "POST", "/api/os/agentsmith/approve", {"id": e["id"], "sha256": "0" * 64})[0] == 409
        assert approved_calls == []

    def test_unknown_id_and_bad_id(self, server, approved_calls):
        assert call(server, "POST", "/api/os/agentsmith/approve", {"id": "ext-404", "sha256": "0" * 64})[0] == 404
        assert call(server, "POST", "/api/os/agentsmith/approve", {"id": "../x", "sha256": "0" * 64})[0] == 400
        assert call(server, "POST", "/api/os/agentsmith/approve", {"sha256": "0" * 64})[0] == 400
        assert approved_calls == []

    def test_a_draft_too_large_to_read_cannot_be_approved(self, server, approved_calls, monkeypatch):
        from dourmouse.os_api import agentsmith

        e = add()
        _, det = call(server, "GET", f"/api/os/agentsmith/draft?id={e['id']}")
        monkeypatch.setattr(agentsmith, "MAX_REVIEW_CHARS", 100)
        status, data = call(server, "POST", "/api/os/agentsmith/approve", {"id": e["id"], "sha256": det["draft"]["preview_sha256"]})
        assert status == 409 and "too large" in data["error"] and approved_calls == []

    def test_a_failed_approval_is_the_real_reason_and_the_draft_is_marked(self, server, monkeypatch):
        e = add(test_source="def test_it():\n    assert False, 'nope'\n")
        _, det = call(server, "GET", f"/api/os/agentsmith/draft?id={e['id']}")
        monkeypatch.setattr(se, "_write_and_run_test", lambda entry, digest: (False, "1 failed: nope"))
        status, data = call(server, "POST", "/api/os/agentsmith/approve", {"id": e["id"], "sha256": det["draft"]["preview_sha256"]})
        assert status == 409 and "the draft's own test failed" in data["error"] and "nope" in data["error"]
        _, board = call(server, "GET", "/api/os/agentsmith/board")
        assert board["drafts"][0]["status"] == "APPROVAL_FAILED" and "nope" in board["drafts"][0]["decision_reason"]
        assert not (se._approved_dir() / "greet_someone.py").exists(), "a failing draft leaves no module behind"

    def test_a_real_approval_writes_the_forced_confirmation_module_and_is_not_live_yet(self, server, monkeypatch):
        e = add()
        _, det = call(server, "GET", f"/api/os/agentsmith/draft?id={e['id']}")
        monkeypatch.setattr(se, "_write_and_run_test", lambda entry, digest: (True, "1 passed"))
        status, data = call(server, "POST", "/api/os/agentsmith/approve", {"id": e["id"], "sha256": det["draft"]["preview_sha256"]})
        assert status == 200 and data["entry"]["status"] == "APPROVED"
        assert data["entry"]["module_sha256"] == det["draft"]["preview_sha256"], "the hash approved is the hash recorded"
        _, board = call(server, "GET", "/api/os/agentsmith/board")
        assert board["drafts"][0]["live"] is False and board["restart_needed"] == 1
        _, after = call(server, "GET", f"/api/os/agentsmith/draft?id={e['id']}")
        assert after["draft"]["on_disk_matches"] is True
        assert call(server, "POST", "/api/os/agentsmith/approve", {"id": e["id"], "sha256": det["draft"]["preview_sha256"]})[0] == 409, "an approved draft cannot be approved again"

    def test_no_route_lets_a_model_approve(self):
        from dourmouse.os_api import routes

        assert ("POST", "/api/os/agentsmith/approve") in routes()
        names = {t.name for sub in build_general_registry().all_subagents() for t in sub.tools}
        assert not any("approve" in n and ("extension" in n or "tool" in n) for n in names), "no chat tool approves a self-extension"
