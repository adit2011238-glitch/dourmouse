"""Hermetic tests for the Dell autostart scripts (dell/*.ps1).

The scripts are plain files (no PowerShell here to execute) — these tests pin
the invariants that matter: the documented task name, node path, run level,
reversibility, and that the README documents the scripts.
"""

from __future__ import annotations

from pathlib import Path

_DELL = Path(__file__).resolve().parents[2] / "dell"


def _read(name: str) -> str:
    return (_DELL / name).read_text(encoding="utf-8")


class TestInstallAutostart:
    def test_script_exists_and_has_documented_task_name(self):
        src = _read("install_autostart.ps1")
        assert 'TaskName = "DOURMOUSE-ComputeNode"' in src
        assert '-TaskName $TaskName' in src

    def test_uses_documented_node_layout_and_run_level(self):
        src = _read("install_autostart.ps1")
        assert '"C:\\dourmouse-node"' in src
        assert '.venv\\Scripts\\python.exe' in src
        assert 'dell_server.py' in src
        assert '-RunLevel Limited' in src

    def test_no_execution_time_limit_for_long_running_server(self):
        src = _read("install_autostart.ps1")
        assert '-ExecutionTimeLimit (New-TimeSpan -Seconds 0)' in src
        assert '-RestartCount 3' in src

    def test_optional_ollama_task_is_separate(self):
        src = _read("install_autostart.ps1")
        assert '"DOURMOUSE-Ollama"' in src
        assert '-Argument "serve"' in src

    def test_ascii_only_for_windows_powershell_51(self):
        src = _read("install_autostart.ps1")
        assert src.isascii()


class TestRemoveAutostart:
    def test_script_exists_and_removes_documented_task(self):
        src = _read("remove_autostart.ps1")
        assert 'TaskName = "DOURMOUSE-ComputeNode"' in src
        assert 'Unregister-ScheduledTask -TaskName $name -Confirm:$false' in src

    def test_node_dir_deletion_is_guarded(self):
        src = _read("remove_autostart.ps1")
        assert '-RemoveNodeDir' in src
        assert 'no dell_server.py' in src

    def test_ascii_only_for_windows_powershell_51(self):
        src = _read("remove_autostart.ps1")
        assert src.isascii()


class TestReadmeDocumentsScripts:
    def test_readme_points_at_both_scripts(self):
        src = _read("README.md")
        assert "install_autostart.ps1" in src
        assert "remove_autostart.ps1" in src

    def test_readme_manual_commands_kept_as_reference(self):
        src = _read("README.md")
        assert 'New-ScheduledTaskTrigger -AtStartup' in src
        assert 'Unregister-ScheduledTask -TaskName "DOURMOUSE-ComputeNode" -Confirm:$false' in src
