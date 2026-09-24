"""Finding #084: default store paths must follow DOURMOUSE_WORKSPACE at call
time. Import-time constants (``DEFAULT_DB = workspace_dir() / ...``) froze the
real workspace during test collection, so the suite wrote thousands of test
rows into the developer's real office_log.db and sentry.db."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from dourmouse import mcp_client, office_logger
from dourmouse.device_wiki import store as wiki_store
from dourmouse.research_mesh import pipeline as mesh_pipeline
from dourmouse.research_pipeline import store as research_store
from dourmouse.security import sentry

_DEFAULTS = [
    (office_logger.default_db, ("office", "office_log.db")),
    (mesh_pipeline.default_db, ("research_mesh", "qualification.db")),
    (research_store.default_db, ("research_pipeline", "research.db")),
    (sentry.default_db, ("security", "sentry.db")),
    (wiki_store.default_db, ("device_wiki", "wiki.db")),
    (mcp_client.default_config, ("mcp_servers.json",)),
]


@pytest.mark.parametrize(("fn", "parts"), _DEFAULTS)
def test_default_path_follows_the_workspace_set_after_import(fn, parts, tmp_path, monkeypatch):
    for name in ("a", "b"):
        ws = tmp_path / name
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(ws))
        assert fn() == ws.joinpath(*parts)


def test_office_logger_with_no_path_writes_into_the_current_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))
    log = office_logger.OfficeLogger()
    assert (tmp_path / "office" / "office_log.db").is_file()
    assert log is not None


def test_no_module_freezes_a_workspace_path_at_import_time():
    """AST guard for the whole class: no module-level assignment in the
    package may call workspace_dir(). Resolve it inside a function."""
    root = Path(__file__).resolve().parent.parent
    offenders = []
    for path in root.rglob("*.py"):
        if "tests" in path.relative_to(root).parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                        and sub.func.id == "workspace_dir"):
                    offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert offenders == []
