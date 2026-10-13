"""FIX-P R5-21: the privacy texts say what the code now does and what it does not cover."""

from __future__ import annotations

from dourmouse.os_api import privacy as route
from dourmouse.tests.test_fix_PRIV_ui import _OS, node


def test_limits_name_the_never_sent_classes_and_what_is_not_covered():
    text = route._LIMITS
    for phrase in ("Never sent in any mode", "Keychain", ".env", "cookies", "what you type yourself", "files you attach",
                   "names or free text"):
        assert phrase in text, phrase
    assert "—" not in text


def test_consent_and_private_prompts_do_not_overpromise(tmp_path):
    out = node(tmp_path, _OS / "screens" / "settings" / "helpers.js", """
const src = { id: 'mail', label: 'Mail', what: 'message subjects' };
R.allow = h.consentPrompt(src, 'allow');
R.on = h.privateModePrompt(true);
""")
    assert "neither is anything you type or attach yourself" in out["allow"] and "not only one" in out["allow"]
    assert "earlier results from them are removed" in out["on"] and "still sent" in out["on"]
    assert "—" not in out["allow"] + out["on"]


def test_toolbar_and_security_texts():
    thread = (_OS / "kit" / "thread-view.js").read_text(encoding="utf-8")
    assert "Private mode for this window" in thread and "still sent" in thread
    sec = (_OS / "screens" / "security" / "index.js").read_text(encoding="utf-8")
    assert "shows the requests Dourmouse recorded" in sec and "not listed" in sec
    st = (_OS / "screens" / "settings" / "index.js").read_text(encoding="utf-8")
    assert "Never sent in any mode" in st
