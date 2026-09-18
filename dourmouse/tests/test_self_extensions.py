"""Tests for dourmouse/self_extensions.py -- Domain D, the single most
architecturally sensitive item in docs/COMMERCIAL_GRADE_MASTER_REQUIREMENTS.md.
The autouse _workspace_isolated fixture (conftest.py) already redirects
DOURMOUSE_WORKSPACE to a per-test tmp dir; tests that call through
build_general_registry() or the approval subprocess set it explicitly too,
matching test_schedules.py's own established convention."""

from __future__ import annotations

from pathlib import Path

from dourmouse import self_extensions as se
from dourmouse.dispatch import Permission

_HANDLER_OK = (
    "def handle(arguments: dict) -> str:\n"
    "    a = arguments.get(\"a\")\n"
    "    b = arguments.get(\"b\")\n"
    "    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):\n"
    "        return \"ERROR: 'a' and 'b' must both be numbers.\"\n"
    "    return str(a + b)\n"
)

def _test_ok(tool_name: str) -> str:
    return (
        "from dourmouse.self_extensions import load_approved\n"
        "from dourmouse.dispatch import Permission\n\n\n"
        "def test_adds_two_numbers():\n"
        f"    mod = load_approved({tool_name!r})\n"
        "    assert mod.handle({\"a\": 2, \"b\": 3}) == \"5\"\n\n\n"
        "def test_rejects_non_numeric_input():\n"
        f"    mod = load_approved({tool_name!r})\n"
        "    assert mod.handle({\"a\": \"x\", \"b\": 3}).startswith(\"ERROR\")\n\n\n"
        "def test_permission_is_forced():\n"
        f"    mod = load_approved({tool_name!r})\n"
        "    assert mod.TOOL_SPEC.permission is Permission.REQUIRES_CONFIRMATION\n"
    )


def _test_failing(tool_name: str) -> str:
    return (
        "from dourmouse.self_extensions import load_approved\n\n\n"
        "def test_this_assertion_is_deliberately_wrong():\n"
        f"    mod = load_approved({tool_name!r})\n"
        "    assert mod.handle({\"a\": 2, \"b\": 3}) == \"999\"\n"
    )


_TEST_OK = _test_ok("add_two_numbers")  # only ever stored as opaque text in CRUD-only tests below
_TEST_FAILING = _test_failing("self_ext_broken")

_PARAMS = {
    "type": "object",
    "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
    "required": ["a", "b"],
}


class TestSyntaxCheck:
    def test_valid_python_passes(self):
        assert se.check_syntax(_HANDLER_OK) is None

    def test_invalid_python_reports_a_real_syntax_error(self):
        err = se.check_syntax("def handle(arguments: dict) -> str\n    return 'missing colon'")
        assert err is not None and "SyntaxError" in err


class TestValidateToolName:
    def test_a_normal_name_is_accepted(self):
        assert se.validate_tool_name("add_two_numbers", existing_names=frozenset()) is None

    def test_rejects_bad_characters(self):
        assert se.validate_tool_name("Add-Two-Numbers!", existing_names=frozenset()) is not None

    def test_rejects_a_collision_with_the_live_registry(self):
        err = se.validate_tool_name("web_search", existing_names=frozenset({"web_search"}))
        assert err is not None and "already exists" in err

    def test_rejects_too_short(self):
        assert se.validate_tool_name("ab", existing_names=frozenset()) is not None


class TestStore:
    def test_add_and_list_roundtrip(self, tmp_path):
        store = se.SelfExtensions(tmp_path / "drafts.jsonl")
        entry = store.add_draft(
            capability_gap="no way to add two numbers",
            tool_name="add_two_numbers",
            description="Adds two numbers.",
            parameters_schema=_PARAMS,
            handler_source=_HANDLER_OK,
            test_source=_TEST_OK,
        )
        assert entry["status"] == "DRAFTED"
        assert store.list()[0]["id"] == entry["id"]
        assert store.get(entry["id"])["tool_name"] == "add_two_numbers"

    def test_list_filters_by_status(self, tmp_path):
        store = se.SelfExtensions(tmp_path / "drafts.jsonl")
        store.add_draft(
            capability_gap="x", tool_name="one", description="d",
            parameters_schema={}, handler_source=_HANDLER_OK, test_source=_TEST_OK,
        )
        assert len(store.list(status="DRAFTED")) == 1
        assert len(store.list(status="APPROVED")) == 0

    def test_get_unknown_id_returns_none(self, tmp_path):
        store = se.SelfExtensions(tmp_path / "drafts.jsonl")
        assert store.get("no-such-id") is None

    def test_persists_across_instances(self, tmp_path):
        path = tmp_path / "drafts.jsonl"
        se.SelfExtensions(path).add_draft(
            capability_gap="x", tool_name="one", description="d",
            parameters_schema={}, handler_source=_HANDLER_OK, test_source=_TEST_OK,
        )
        assert len(se.SelfExtensions(path).list()) == 1


class TestReject:
    def test_rejects_a_real_draft(self, tmp_path):
        store = se.SelfExtensions(tmp_path / "drafts.jsonl")
        entry = store.add_draft(
            capability_gap="x", tool_name="one", description="d",
            parameters_schema={}, handler_source=_HANDLER_OK, test_source=_TEST_OK,
        )
        result = se.reject(entry["id"], reason="not needed", store=store)
        assert result["ok"] is True
        assert store.get(entry["id"])["status"] == "REJECTED"
        assert store.get(entry["id"])["decision_reason"] == "not needed"

    def test_unknown_id_is_a_real_error(self, tmp_path):
        store = se.SelfExtensions(tmp_path / "drafts.jsonl")
        result = se.reject("no-such-id", store=store)
        assert result["ok"] is False

    def test_cannot_reject_an_already_decided_draft(self, tmp_path):
        store = se.SelfExtensions(tmp_path / "drafts.jsonl")
        entry = store.add_draft(
            capability_gap="x", tool_name="one", description="d",
            parameters_schema={}, handler_source=_HANDLER_OK, test_source=_TEST_OK,
        )
        se.reject(entry["id"], store=store)
        second = se.reject(entry["id"], store=store)
        assert second["ok"] is False


class TestApprove:
    """The real, high-stakes path: writes a real module, sanity-imports it,
    runs its own real test in a real subprocess. Each of these genuinely
    spins up pytest as a subprocess -- slower than a mocked test, but
    mocking away the actual test-run is exactly the "side channel" Domain
    D's own acceptance test 4 says must never exist."""

    def test_a_genuinely_correct_draft_is_approved_for_real(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))
        store = se.SelfExtensions()
        entry = store.add_draft(
            capability_gap="no way to add two numbers",
            tool_name="self_ext_add_numbers",
            description="Adds two numbers.",
            parameters_schema=_PARAMS,
            handler_source=_HANDLER_OK,
            test_source=_test_ok("self_ext_add_numbers"),
        )
        result = se.approve(entry["id"], store=store)
        assert result["ok"] is True, result.get("error")
        assert store.get(entry["id"])["status"] == "APPROVED"
        # the real module really exists and really works
        mod = se.load_approved("self_ext_add_numbers")
        assert mod.handle({"a": 2, "b": 3}) == "5"
        assert mod.TOOL_SPEC.permission is Permission.REQUIRES_CONFIRMATION
        assert mod.TOOL_SPEC.name == "self_ext_add_numbers"
        # the drafted test file never lingers in the real tracked tests dir
        import dourmouse

        leftover = Path(dourmouse.__file__).resolve().parent / "tests" / "test_self_ext_self_ext_add_numbers.py"
        assert not leftover.exists()
        # a real, permanent changelog entry
        changelog = (tmp_path / "self_extensions" / "CHANGELOG.md")
        assert changelog.exists()
        assert "self_ext_add_numbers" in changelog.read_text(encoding="utf-8")

    def test_a_draft_whose_own_test_fails_is_never_silently_merged(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))
        store = se.SelfExtensions()
        entry = store.add_draft(
            capability_gap="x", tool_name="self_ext_broken", description="d",
            parameters_schema=_PARAMS, handler_source=_HANDLER_OK, test_source=_TEST_FAILING,
        )
        result = se.approve(entry["id"], store=store)
        assert result["ok"] is False
        assert "test failed" in result["error"]
        assert store.get(entry["id"])["status"] == "APPROVAL_FAILED"
        # the module must not be left behind as if it were real
        assert not (tmp_path / "self_extensions" / "approved" / "self_ext_broken.py").exists()
        assert not (tmp_path / "self_extensions" / "CHANGELOG.md").exists()

    def test_a_name_collision_with_a_real_tool_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))
        store = se.SelfExtensions()
        entry = store.add_draft(
            capability_gap="x", tool_name="web_search", description="d",
            parameters_schema=_PARAMS, handler_source=_HANDLER_OK, test_source=_TEST_OK,
        )
        result = se.approve(entry["id"], store=store)
        assert result["ok"] is False
        assert "already exists" in result["error"]
        assert store.get(entry["id"])["status"] == "APPROVAL_FAILED"

    def test_bad_syntax_caught_again_at_approval_time(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))
        store = se.SelfExtensions()
        entry = store.add_draft(
            capability_gap="x", tool_name="self_ext_bad_syntax", description="d",
            parameters_schema=_PARAMS, handler_source="def handle(:\n  broken",
            test_source=_TEST_OK,
        )
        result = se.approve(entry["id"], store=store)
        assert result["ok"] is False
        assert "SyntaxError" in result["error"]

    def test_unknown_draft_id_is_a_real_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))
        result = se.approve("no-such-id")
        assert result["ok"] is False

    def test_cannot_approve_an_already_decided_draft_twice(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))
        store = se.SelfExtensions()
        entry = store.add_draft(
            capability_gap="x", tool_name="self_ext_twice", description="d",
            parameters_schema=_PARAMS, handler_source=_HANDLER_OK,
            test_source=_test_ok("self_ext_twice"),
        )
        first = se.approve(entry["id"], store=store)
        assert first["ok"] is True
        second = se.approve(entry["id"], store=store)
        assert second["ok"] is False
        assert "already" in second["error"]


class TestListApprovedNames:
    def test_empty_when_nothing_approved(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))
        assert se.list_approved_names() == []

    def test_lists_a_real_approved_extension(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))
        store = se.SelfExtensions()
        entry = store.add_draft(
            capability_gap="x", tool_name="self_ext_listed", description="d",
            parameters_schema=_PARAMS, handler_source=_HANDLER_OK,
            test_source=_test_ok("self_ext_listed"),
        )
        se.approve(entry["id"], store=store)
        assert se.list_approved_names() == ["self_ext_listed"]
