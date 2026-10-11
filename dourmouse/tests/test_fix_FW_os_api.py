"""FW: os_api fixes (P3-45 route table load race, P4-42 AGENTSMITH review hash,
P5-37 apps/allow with a bundle id only)."""
from __future__ import annotations

import hashlib
import threading
import time
import types

import pytest

from dourmouse import os_api
from dourmouse import self_extensions as se
from dourmouse.os_api import ApiError, Request

# -- P3-45 --------------------------------------------------------------- #

def test_concurrent_first_requests_never_see_a_half_loaded_route_table(monkeypatch):
    routes: dict = {}
    monkeypatch.setattr(os_api, "_ROUTES", routes)
    monkeypatch.setattr(os_api, "_FAILED", {})
    monkeypatch.setattr(os_api, "_loaded", False)
    monkeypatch.setattr(os_api.pkgutil, "iter_modules", lambda path: [types.SimpleNamespace(name="slowmod")])

    def slow_import(name):
        time.sleep(0.4)
        os_api.route("GET", "/api/os/slow")(lambda req: (200, {"ok": True}))

    monkeypatch.setattr(os_api.importlib, "import_module", slow_import)
    results: list = []
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()
        results.append(os_api.find("GET", "/api/os/slow"))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert len(results) == 2 and all(r is not None for r in results), results


def test_load_is_reentrant_for_a_module_that_calls_find_while_importing(monkeypatch):
    monkeypatch.setattr(os_api, "_ROUTES", {})
    monkeypatch.setattr(os_api, "_FAILED", {})
    monkeypatch.setattr(os_api, "_loaded", False)
    monkeypatch.setattr(os_api.pkgutil, "iter_modules", lambda path: [types.SimpleNamespace(name="m")])
    monkeypatch.setattr(os_api.importlib, "import_module", lambda name: os_api.find("GET", "/x"))
    assert os_api.find("GET", "/x") is None  # returns instead of recursing or deadlocking


# -- P4-42 --------------------------------------------------------------- #

HANDLER = "def handle(arguments: dict) -> str:\n    return 'hi'\n"
TEST = "def test_it():\n    assert True\n"
SCHEMA = {"type": "object", "properties": {}, "required": []}


@pytest.fixture
def smith(monkeypatch, tmp_path):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("DOURMOUSE_CONFIG_DIR", str(tmp_path / "cfg"))
    calls: list = []
    monkeypatch.setattr(se, "approve", lambda did, **kw: calls.append(did) or {"ok": True, "entry": {}, "note": ""})
    return calls


def _draft():
    return se.SelfExtensions().add_draft(
        capability_gap="gap", tool_name="greet_someone", description="Greets.",
        parameters_schema=SCHEMA, handler_source=HANDLER, test_source=TEST)


def _call(path, query=None, body=None, method="GET"):
    from dourmouse.os_api import agentsmith  # noqa: F401  (registers the routes)

    handler = os_api.find(method, path)
    try:
        return handler(Request(server=types.SimpleNamespace(registry=None), query=query or {}, body=body or {}))
    except ApiError as exc:
        return exc.status, {"error": str(exc)}


def test_the_review_hash_covers_the_test_source_too(smith):
    e = _draft()
    _, det = _call("/api/os/agentsmith/draft", query={"id": [e["id"]]})
    reviewed = det["draft"]["preview_sha256"]
    # The record is rewritten after the owner opened it: only the test changes.
    path = se._drafts_path()
    path.write_text(path.read_text().replace("assert True", "import os; os.system('id')"), encoding="utf-8")
    status, data = _call("/api/os/agentsmith/approve", method="POST", body={"id": e["id"], "sha256": reviewed})
    assert status == 409 and "changed after it was opened" in data["error"]
    assert smith == []


def test_an_unchanged_draft_still_approves_and_the_module_hash_is_reported_separately(smith):
    e = _draft()
    _, det = _call("/api/os/agentsmith/draft", query={"id": [e["id"]]})
    d = det["draft"]
    assert d["module_preview_sha256"] == hashlib.sha256(d["module_preview"].encode()).hexdigest()
    assert d["preview_sha256"] != d["module_preview_sha256"]
    status, _ = _call("/api/os/agentsmith/approve", method="POST", body={"id": e["id"], "sha256": d["preview_sha256"]})
    assert status == 200 and smith == [e["id"]]


def test_the_module_hash_alone_no_longer_approves(smith):
    e = _draft()
    _, det = _call("/api/os/agentsmith/draft", query={"id": [e["id"]]})
    status, _ = _call("/api/os/agentsmith/approve", method="POST",
                      body={"id": e["id"], "sha256": det["draft"]["module_preview_sha256"]})
    assert status == 409 and smith == []


# -- P5-37 --------------------------------------------------------------- #

def test_allow_by_bundle_id_does_not_adopt_an_unbundled_running_apps_name(monkeypatch):
    from dourmouse import app_driver

    monkeypatch.setattr(app_driver, "running_apps", lambda: [{"name": "Unbundled Helper", "bundle_id": ""}])
    seen = {}
    monkeypatch.setattr(app_driver, "allow", lambda name, bundle_id, by=None: seen.update(name=name, bundle_id=bundle_id) or {"name": name})
    monkeypatch.setattr(app_driver, "list_allowed", lambda: [])
    from dourmouse.app_driver import safety

    monkeypatch.setattr(safety, "audit", lambda *a, **k: None)
    handler = os_api.find("POST", "/api/os/apps/allow")
    status, _ = handler(Request(server=None, body={"bundle_id": "com.example.Foo"}))
    assert status == 200
    assert seen["bundle_id"] == "com.example.Foo"
    assert seen["name"] != "Unbundled Helper"


def test_allow_by_bundle_id_still_finds_the_running_app_with_that_bundle_id(monkeypatch):
    from dourmouse import app_driver
    from dourmouse.app_driver import safety

    monkeypatch.setattr(app_driver, "running_apps", lambda: [
        {"name": "Other", "bundle_id": ""}, {"name": "Foo", "bundle_id": "com.example.Foo"}])
    seen = {}
    monkeypatch.setattr(app_driver, "allow", lambda name, bundle_id, by=None: seen.update(name=name, bundle_id=bundle_id) or {"name": name})
    monkeypatch.setattr(app_driver, "list_allowed", lambda: [])
    monkeypatch.setattr(safety, "audit", lambda *a, **k: None)
    status, _ = os_api.find("POST", "/api/os/apps/allow")(Request(server=None, body={"bundle_id": "com.example.Foo"}))
    assert status == 200 and seen == {"name": "Foo", "bundle_id": "com.example.Foo"}


def test_allow_by_name_unchanged(monkeypatch):
    from dourmouse import app_driver
    from dourmouse.app_driver import safety

    monkeypatch.setattr(app_driver, "running_apps", lambda: [{"name": "TextEdit", "bundle_id": "com.apple.TextEdit"}])
    seen = {}
    monkeypatch.setattr(app_driver, "allow", lambda name, bundle_id, by=None: seen.update(name=name, bundle_id=bundle_id) or {"name": name})
    monkeypatch.setattr(app_driver, "list_allowed", lambda: [])
    monkeypatch.setattr(safety, "audit", lambda *a, **k: None)
    os_api.find("POST", "/api/os/apps/allow")(Request(server=None, body={"app": "textedit"}))
    assert seen == {"name": "TextEdit", "bundle_id": "com.apple.TextEdit"}
