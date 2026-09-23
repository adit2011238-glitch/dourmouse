"""OS-2 (finding #078): which native shell a real launch actually opens.

Two real native shells exist. electron/main.js is the better one -- it embeds
a real Chromium BrowserView wired over CDP to the same live Playwright session
browser_agent.py drives -- and it was fully built and verified once, but
NOTHING EVER LAUNCHED IT. Every entry point ran the older pywebview shell.

These pin the selection rule, including the two things that make it safe:
electron/node_modules is gitignored (~408MB, never committed), so a fresh
clone genuinely has no Electron and must still start; and an EXPLICIT request
for a shell that is not there must say so rather than failing silently.
"""

from __future__ import annotations

import pytest

from dourmouse import desktop


@pytest.fixture
def fake_electron(tmp_path, monkeypatch):
    """A real on-disk electron/ layout, rooted where desktop.py looks.

    Fakes the FILESYSTEM, not the function under test -- the resolution logic
    is what these tests exist to exercise, so stubbing it would prove nothing
    (the house convention in docs/TESTING.md, after two real bugs survived
    exactly that mistake)."""
    root = tmp_path / "repo"
    (root / "dourmouse").mkdir(parents=True)
    electron = root / "electron"
    (electron / "node_modules" / ".bin").mkdir(parents=True)
    (electron / "main.js").write_text("// fake")
    binary = electron / "node_modules" / ".bin" / "electron"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    monkeypatch.setattr(desktop, "__file__", str(root / "dourmouse" / "desktop.py"))
    return {"root": root, "electron": electron, "binary": binary}


class TestElectronShellArgv:
    def test_finds_a_real_installed_shell(self, fake_electron):
        argv = desktop._electron_shell_argv()
        assert argv is not None
        assert argv[0] == str(fake_electron["binary"])
        assert argv[1] == str(fake_electron["electron"])

    def test_absent_node_modules_is_not_an_error(self, fake_electron):
        # The normal state of a fresh clone: node_modules is gitignored.
        fake_electron["binary"].unlink()
        assert desktop._electron_shell_argv() is None

    def test_a_non_executable_binary_is_refused(self, fake_electron):
        # A half-finished or interrupted npm install leaves a file that is
        # not runnable. Handing that to execv would fail at launch.
        fake_electron["binary"].chmod(0o644)
        assert desktop._electron_shell_argv() is None

    def test_a_missing_main_js_is_refused(self, fake_electron):
        (fake_electron["electron"] / "main.js").unlink()
        assert desktop._electron_shell_argv() is None


class TestResolveShellChoice:
    def test_auto_prefers_electron_when_it_is_really_there(self, fake_electron, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_SHELL", raising=False)
        assert desktop._resolve_shell_choice() == "electron"

    def test_auto_falls_back_when_electron_is_absent(self, fake_electron, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_SHELL", raising=False)
        fake_electron["binary"].unlink()
        assert desktop._resolve_shell_choice() == "pywebview"

    def test_pywebview_is_honoured_even_when_electron_exists(self, fake_electron, monkeypatch):
        # The real opt-out. It must win over availability.
        monkeypatch.setenv("DOURMOUSE_SHELL", "pywebview")
        assert desktop._resolve_shell_choice() == "pywebview"

    def test_explicit_electron_is_honoured(self, fake_electron, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_SHELL", "electron")
        assert desktop._resolve_shell_choice() == "electron"

    def test_explicit_electron_that_is_absent_says_so_and_still_starts(
        self, fake_electron, monkeypatch, capsys
    ):
        # Refusing to open the app at all would be worse than degrading, but
        # degrading SILENTLY would leave the user wondering why the pane is
        # not the Chromium one. Both halves are asserted.
        monkeypatch.setenv("DOURMOUSE_SHELL", "electron")
        fake_electron["binary"].unlink()
        assert desktop._resolve_shell_choice() == "pywebview"
        err = capsys.readouterr().err
        assert "not installed" in err
        assert "npm install" in err  # the real fix, not just the complaint

    def test_an_unknown_value_warns_and_uses_auto(self, fake_electron, monkeypatch, capsys):
        monkeypatch.setenv("DOURMOUSE_SHELL", "chrome")
        assert desktop._resolve_shell_choice() == "electron"  # auto, and it is available
        assert "auto/electron/pywebview" in capsys.readouterr().err

    @pytest.mark.parametrize("raw", ["AUTO", "  electron  ", "PyWebView", ""])
    def test_values_are_read_case_and_whitespace_insensitively(
        self, fake_electron, monkeypatch, raw
    ):
        monkeypatch.setenv("DOURMOUSE_SHELL", raw)
        # None of these should warn or crash; each resolves to a real choice.
        assert desktop._resolve_shell_choice() in ("electron", "pywebview")

    def test_case_folding_actually_maps_to_the_right_shell(self, fake_electron, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_SHELL", "PyWebView")
        assert desktop._resolve_shell_choice() == "pywebview"


class TestLaunchItselfIsUnaffected:
    def test_the_selection_lives_in_main_not_in_launch(self):
        # Load-bearing: launch() is called directly by many hermetic tests and
        # must keep getting the pywebview path byte for byte. If the shell
        # choice ever migrates INTO launch(), those tests start exec'ing a
        # real Electron binary.
        import inspect

        src = inspect.getsource(desktop.launch)
        assert "_resolve_shell_choice" not in src
        assert "execv" not in src

    def test_the_env_var_name_is_the_documented_one(self):
        assert desktop._SHELL_ENV == "DOURMOUSE_SHELL"
