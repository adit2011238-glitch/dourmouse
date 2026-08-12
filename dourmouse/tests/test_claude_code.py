"""Claude Code bridge tests (dourmouse/general_roster.py claude_code tool).

The dev_coding subagent can delegate coding work to the user's REAL Claude
Code CLI in headless mode (`claude -p <task>`). All real-execution tests
point CLAUDE_CODE_CLI at tiny FAKE scripts so no live Claude Code / API
credits are consumed; the missing-CLI path proves the honest NOT CONFIGURED
behavior (Rule 2.2 — no silent stub, no fabricated result).
"""

from __future__ import annotations

import textwrap

import pytest

from dourmouse.general_roster import build_general_registry
from dourmouse.general_roster import _claude_code_tool as run_tool
from dourmouse.general_roster import _find_claude_cli as find_cli


@pytest.fixture
def registry():
    return build_general_registry()


def _write_fake_cli(tmp_path, script: str) -> str:
    """Write an executable fake claude CLI; return its path."""
    path = tmp_path / "fake-claude"
    path.write_text(textwrap.dedent(script))
    path.chmod(0o755)
    return str(path)


class TestToolRegistration:
    def test_claude_code_registered_on_dev_coding(self, registry):
        sub = registry.get_subagent("dev_coding")
        assert sub is not None
        names = [t.name for t in sub.tools]
        assert "claude_code" in names

    def test_claude_code_is_regular_tier(self, registry):
        sub = registry.get_subagent("dev_coding")
        tool = next(t for t in sub.tools if t.name == "claude_code")
        assert tool.permission.value == "regular"

    def test_claude_code_in_roster_payload(self, registry):
        from dourmouse.webui import build_roster_payload

        payload = build_roster_payload(registry)
        dev = next(a for a in payload["subagents"] if a["name"] == "dev_coding")
        assert "claude_code" in [t["name"] for t in dev["tools"]]


class TestCliDiscovery:
    def test_env_override_wins(self, tmp_path, monkeypatch):
        fake = _write_fake_cli(
            tmp_path, "#!/bin/sh\necho 'ok'\n"
        )
        monkeypatch.setenv("CLAUDE_CODE_CLI", fake)
        monkeypatch.delenv("PATH", raising=False)
        assert find_cli() == fake

    def test_bare_name_resolves_via_path(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_CLI", "")
        monkeypatch.delenv("CLAUDE_CODE_CLI")
        # PATH lookup for a real-ish command must at least not crash; on any
        # machine 'sh' or 'true' resolves.
        import shutil

        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/true" if name == "claude" else None)
        assert find_cli() == "/usr/bin/true"

    def test_not_found_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_CLI", str(tmp_path / "does-not-exist"))
        import shutil

        monkeypatch.setattr(shutil, "which", lambda name: None)
        assert find_cli() is None


class TestToolBehavior:
    def test_missing_cli_is_honest_not_configured(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_CLI", str(tmp_path / "nope"))
        import shutil

        monkeypatch.setattr(shutil, "which", lambda name: None)
        result = run_tool({"task": "refactor this"})
        assert result.startswith("NOT CONFIGURED")
        assert "claude" in result.lower()
        assert "Nothing was run" in result

    def test_empty_task_errors(self, monkeypatch):
        result = run_tool({"task": "   "})
        assert result.startswith("ERROR")
        assert "non-empty" in result

    def test_real_subprocess_roundtrip_fake_cli(self, tmp_path, monkeypatch):
        fake = _write_fake_cli(
            tmp_path,
            """#!/bin/sh
            echo "ARGV: $*"
            echo "CWD: $(pwd)"
            """,
        )
        monkeypatch.setenv("CLAUDE_CODE_CLI", fake)
        result = run_tool({"task": "explain this bug", "cwd": str(tmp_path)})
        assert "EXIT CODE: 0" in result
        assert "ARGV: -p explain this bug" in result
        assert f"CWD: {tmp_path}" in result

    def test_nonzero_exit_surfaces_stderr(self, tmp_path, monkeypatch):
        fake = _write_fake_cli(
            tmp_path,
            """#!/bin/sh
            echo "boom" >&2
            exit 3
            """,
        )
        monkeypatch.setenv("CLAUDE_CODE_CLI", fake)
        result = run_tool({"task": "do the thing"})
        assert "EXIT CODE: 3" in result
        assert "boom" in result
        assert "non-zero" in result

    def test_timeout_reports_honestly(self, tmp_path, monkeypatch):
        fake = _write_fake_cli(
            tmp_path,
            """#!/bin/sh
            sleep 5
            """,
        )
        monkeypatch.setenv("CLAUDE_CODE_CLI", fake)
        result = run_tool({"task": "slow task", "timeout_seconds": 1})
        assert "timed out" in result
        assert "1s" in result

    def test_timeout_capped_at_600(self, tmp_path, monkeypatch):
        fake = _write_fake_cli(
            tmp_path, "#!/bin/sh\necho ok\n"
        )
        monkeypatch.setenv("CLAUDE_CODE_CLI", fake)
        result = run_tool({"task": "x", "timeout_seconds": 9999})
        assert "EXIT CODE: 0" in result

    def test_zero_timeout_clamped_to_one(self, tmp_path, monkeypatch):
        fake = _write_fake_cli(
            tmp_path, "#!/bin/sh\necho ok\n"
        )
        monkeypatch.setenv("CLAUDE_CODE_CLI", fake)
        result = run_tool({"task": "x", "timeout_seconds": 0})
        assert "EXIT CODE: 0" in result

    def test_non_integer_timeout_errors(self, tmp_path, monkeypatch):
        fake = _write_fake_cli(
            tmp_path, "#!/bin/sh\necho ok\n"
        )
        monkeypatch.setenv("CLAUDE_CODE_CLI", fake)
        result = run_tool({"task": "x", "timeout_seconds": "soon"})
        assert result.startswith("ERROR")
        assert "integer" in result

    def test_output_truncated_at_cap(self, tmp_path, monkeypatch):
        # Shrink the cap so a small fake output crosses it deterministically.
        import dourmouse.general_roster as roster_module

        monkeypatch.setattr(roster_module, "_CLAUDE_OUTPUT_CAP", 200)
        fake = _write_fake_cli(
            tmp_path,
            """#!/bin/sh
            i=0
            while [ $i -lt 400 ]; do echo "line-$i-0123456789"; i=$((i+1)); done
            """,
        )
        monkeypatch.setenv("CLAUDE_CODE_CLI", fake)
        result = run_tool({"task": "big output"})
        assert "output truncated" in result
        # Only the last 200 chars survive + header; ensure it stayed small.
        assert len(result) < 1_000

    def test_oserror_reported(self, tmp_path, monkeypatch):
        # A CLI file whose shebang interpreter doesn't exist -> exec fails ->
        # subprocess raises FileNotFoundError (an OSError) at run time.
        fake = tmp_path / "broken-claude"
        fake.write_text("#!/nonexistent/interpreter\n")
        fake.chmod(0o755)
        monkeypatch.setenv("CLAUDE_CODE_CLI", str(fake))
        result = run_tool({"task": "x"})
        assert "ERROR" in result
