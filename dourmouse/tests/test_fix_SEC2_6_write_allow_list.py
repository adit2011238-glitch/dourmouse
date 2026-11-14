"""SEC2-6 (high): write_path, apply_patch and apply_search_replace run with
no approval and protected only a list of named folders, so files that run
or configure other tools later could be written: <repo>/sitecustomize.py,
conftest.py, ~/.config/git/config, ~/.claude/*, ~/.cursor/mcp.json and more.

Owner decision 2026-11-13: these tools write without asking only inside the
workspace (uploads included), the projects on the bookshelf, ~/Documents,
~/Downloads and ~/Desktop. Anywhere else asks the owner first. Files in
Dourmouse's own folder and hidden settings in the home folder are refused.
"""

from __future__ import annotations

import pytest

from dourmouse import system_access as sa
from dourmouse.dispatch import _argument_gate
from dourmouse.general_roster import build_general_registry


@pytest.fixture()
def places(tmp_path, monkeypatch):
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    ws = repo / "workspace"
    proj = tmp_path / "code" / "myproject"
    for d in (home / "Documents", home / "Downloads", home / "Desktop", home / "Music", ws, proj):
        d.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(ws))
    monkeypatch.setattr(sa, "_PROJECT_ROOT", repo)
    monkeypatch.setattr(sa, "_bookshelf_roots", lambda: [proj.resolve()])
    return {"home": home, "repo": repo, "ws": ws, "proj": proj}


@pytest.fixture(scope="module")
def write_spec():
    return build_general_registry().lookup("write_path")


_REFUSED = [
    ("repo", "sitecustomize.py"), ("repo", "conftest.py"), ("repo", "CLAUDE.md"), ("repo", "AGENTS.md"),
    ("repo", ".mcp.json"), ("repo", "pyproject.toml"), ("repo", "Makefile"), ("repo", ".github/workflows/ci.yml"),
    ("repo", ".claude/commands/x.md"), ("home", ".config/git/config"), ("home", ".claude/CLAUDE.md"),
    ("home", ".claude/skills/x/SKILL.md"), ("home", ".codex/AGENTS.md"), ("home", ".cursor/mcp.json"),
    ("home", ".tmux.conf"), ("home", ".vimrc"), ("home", ".oh-my-zsh/custom/x.zsh"),
    ("home", ".CONFIG/git/config"),
]


@pytest.mark.parametrize(("base", "rel"), _REFUSED)
def test_files_that_run_or_configure_tools_later_are_refused(places, base, rel):
    target = places[base] / rel
    for tool, args in (
        (sa._write_path_tool, {"path": str(target), "content": "open('/tmp/SEC2_MARKER','w')"}),
        (sa._apply_patch_tool, {"path": str(target), "diff": ""}),
        (sa._apply_search_replace_tool, {"path": str(target), "patch": ""}),
    ):
        out = tool(args)
        assert out.startswith("REFUSED"), (tool.__name__, out)
    assert not target.exists()


@pytest.mark.parametrize(("base", "rel"), [
    ("home", "Documents/notes.txt"), ("home", "Downloads/a.txt"), ("home", "Desktop/a.txt"),
    ("ws", "notes/a.txt"), ("ws", "uploads/report.md"), ("proj", "src/app.py"), ("proj", "conftest.py"),
])
def test_the_owners_folders_are_written_without_asking(places, write_spec, base, rel):
    target = places[base] / rel
    args = {"path": str(target), "content": "hello"}
    assert _argument_gate(write_spec, args, "system") is None
    assert sa._write_path_tool(args).startswith("WROTE")
    assert target.read_text() == "hello"


@pytest.mark.parametrize(("base", "rel"), [
    ("home", "Music/a.txt"), ("home", "Library/Application Support/Claude/claude_desktop_config.json"),
    ("home", "Library/Python/3.14/lib/python/site-packages/x.pth"), ("repo", "docs/notes.md"),
])
def test_anywhere_else_asks_the_owner_first(places, write_spec, base, rel):
    target = places[base] / rel
    decision = _argument_gate(write_spec, {"path": str(target), "content": "x"}, "system")
    assert decision is not None and decision[0] == "confirm"
    assert str(target) in decision[1]
    assert "Documents" in decision[1]


def test_the_patch_tools_ask_the_same_way(places):
    registry = build_general_registry()
    target = places["home"] / "Music" / "a.txt"
    for name, extra in (("apply_patch", {"diff": ""}), ("apply_search_replace", {"patch": ""})):
        decision = _argument_gate(registry.lookup(name), {"path": str(target), **extra}, "system")
        assert decision is not None and decision[0] == "confirm", name


def test_an_unattended_schedule_cannot_write_outside_the_allowed_folders(places, write_spec):
    from dourmouse.dispatch import _execute_tool

    target = places["home"] / "Music" / "a.txt"
    out = _execute_tool(write_spec, {"path": str(target), "content": "x"}, lambda _prompt: False)
    assert not target.exists()
    assert "WROTE" not in out


def test_a_symlink_out_of_documents_is_judged_by_where_it_points(places, write_spec):
    link = places["home"] / "Documents" / "sneaky"
    link.symlink_to(places["home"] / "Music")
    decision = _argument_gate(write_spec, {"path": str(link / "a.txt"), "content": "x"}, "system")
    assert decision is not None and decision[0] == "confirm"
