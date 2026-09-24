"""Finding #118 (OS-6): inside a project's chat the file and coding tools
work in the project's own folder, parallel branches included, and the scope
never leaks into the next request."""

from __future__ import annotations

import contextvars
import threading

from dourmouse import project_scope
from dourmouse.general_roster import build_general_registry


def test_file_tools_write_into_the_project_folder(tmp_path):
    project = tmp_path / "My Thesis"
    registry = build_general_registry()
    write = registry.lookup("write_file")
    project_scope.enter({"name": "My Thesis", "path": str(project)})
    try:
        out = write.handler({"path": "notes/plan.md", "content": "chapter 1"})
        assert "ERROR" not in out.upper()[:10], out
        assert (project / "notes" / "plan.md").read_text(encoding="utf-8") == "chapter 1"
        listing = registry.lookup("list_files").handler({"path": "notes"})
        assert "plan.md" in listing
        escape = write.handler({"path": "../outside.txt", "content": "x"})
        assert not (tmp_path / "outside.txt").exists() and "outside" not in listing
        assert escape  # refused with a message, never written
    finally:
        project_scope.leave()
    assert project_scope.current_project() is None


def test_the_scope_reaches_a_copied_context_but_not_a_plain_thread(tmp_path):
    project_scope.enter({"name": "p", "path": str(tmp_path)})
    try:
        seen = {}
        ctx = contextvars.copy_context()
        t = threading.Thread(target=lambda: seen.update(copied=ctx.run(project_scope.current_project)))
        t.start()
        t.join()
        t2 = threading.Thread(target=lambda: seen.update(plain=project_scope.current_project()))
        t2.start()
        t2.join()
        assert seen["copied"]["name"] == "p" and seen["plain"] is None
    finally:
        project_scope.leave()


def test_no_project_means_the_normal_workspace():
    from dourmouse.config import workspace_dir
    from dourmouse.general_roster import _workspace_root

    project_scope.enter(None)
    try:
        assert _workspace_root() == workspace_dir()
    finally:
        project_scope.leave()
