"""Wave 4 review fixes (finding #171 addendum)."""
from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
MAIN = (ROOT / "electron" / "main.js").read_text(encoding="utf-8")


def test_a_crash_during_the_one_reload_reaches_the_give_up_path():
    handler = MAIN[MAIN.index('wc.on("render-process-gone"'):][:900]
    assert "consoleForcedCrash" in handler, "our own forced crash must not count twice"
    assert "if (consoleRecovering) consoleRecovering = false;" in handler
    assert handler.index("consoleRecovering = false") < handler.index('recoverConsole(win, "crashed")')


def test_a_server_that_hangs_at_start_cannot_restart_forever():
    assert MAIN.count("proc._dmReady = true;") == 2, "first start and restart both mark a server that answered"
    plan = MAIN[MAIN.index("function planServerRestart()"):][:600]
    assert "serverFailedStarts > SUPERVISOR.maxRestarts" in plan
    exit_fn = MAIN[MAIN.index("function onServerExit("):][:500]
    assert "serverFailedStarts += 1" in exit_fn and "serverFailedStarts = 0" in exit_fn


def test_command_z_is_left_to_text_fields():
    boot = (ROOT / "ui" / "assets" / "os" / "boot.js").read_text(encoding="utf-8")
    assert "{ editable: s.id !== 'undo' }" in boot
