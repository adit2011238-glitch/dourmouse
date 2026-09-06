"""Coverage for 57109f4 ("Persist login/prefs/chats across restarts").

Zero test coverage existed for this feature before this file (verified:
``grep -rliE 'persist.*login|persist.*session|session.*persist'
dourmouse/tests/ tests/`` returned no hits). test_google_auth.py already
pins ``session_ttl_seconds() == _SESSION_TTL.total_seconds()`` and the
"a session survives years later" behavior; this file covers what was still
untested: that the AuthStore-side check and the cookie's Max-Age genuinely
come from the SAME source (not two hardcoded numbers that could drift), that
``desktop.launch()`` actually starts the real webview non-private with a
real on-disk storage path (private_mode=True is pywebview's default and
would silently wipe cookies/localStorage on every quit), and that
``chat.most_recent_session_file()`` is wired at the real ``__main__`` entry
point specifically rather than inside ``desktop.launch()`` itself -- per
57109f4's own commit message, resolving it inside launch() live-caught a
real test hang (a hermetic test call reaching into the real, shared,
growing workspace/sessions/ directory).

No GUI, no network, no camera/mic: webview is exercised only via the
``webview_loader`` fake-injection seam, exactly like test_desktop.py.
"""

from __future__ import annotations

import inspect
from datetime import timedelta
from pathlib import Path

import pytest

from dourmouse import chat, desktop, google_auth, webui
from dourmouse.google_auth import AuthStore
from dourmouse.tests.test_webui import _echo_registry

_DESKTOP_SRC = Path(desktop.__file__).read_text(encoding="utf-8")
_WEBUI_SRC = Path(webui.__file__).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Fake webview -- MUST match the exact signature c4ee954 fixed (accepts
# private_mode/storage_path kwargs). That commit's own diagnosis: a
# _FakeWebview.start() that doesn't accept **kwargs raises TypeError against
# the real desktop.launch() call, which launch()'s `except Exception`
# quietly turns into an indefinite-hang browser fallback. Copied verbatim
# (same shape as test_desktop.py's and test_live_runtime.py's own copies --
# this repo's established pattern is one small local fake per test file,
# not a shared import) rather than hand-rolled.
# --------------------------------------------------------------------------- #

class _FakeWindow:
    def __init__(self, title, url=None, **kwargs):
        self.title = title
        self.url = url
        self.kwargs = kwargs
        self.shown = False

    def show(self) -> None:
        self.shown = True


class _FakeWebview:
    def __init__(self):
        self.windows: list[_FakeWindow] = []
        self.started = False
        self.start_kwargs: dict = {}

    def create_window(self, title, url=None, **kwargs):
        win = _FakeWindow(title, url, **kwargs)
        self.windows.append(win)
        return win

    def start(self, **kwargs):
        self.started = True
        self.start_kwargs = kwargs


def _loader(fake: _FakeWebview):
    return lambda: fake


@pytest.fixture(autouse=True)
def _no_real_vision_helpers(monkeypatch):
    """Same guard test_desktop.py applies file-wide: never let a hermetic
    launch() call actually spawn the in-process tray/overlay/wakeword
    (mic-capture) helpers."""
    monkeypatch.setenv(desktop._VISION_AUTOSTART_ENV, "0")


# --------------------------------------------------------------------------- #
# google_auth.py: one shared TTL source, not two hardcoded numbers
# --------------------------------------------------------------------------- #

class TestSessionTtlSingleSource:
    def test_session_ttl_seconds_is_3650_days(self):
        assert google_auth.session_ttl_seconds() == 3650 * 24 * 60 * 60

    def test_authstore_check_and_public_accessor_move_together(self, tmp_path, monkeypatch):
        """Patch the ONE module-level constant both sites are documented to
        derive from and confirm both the AuthStore-side expiry check and
        session_ttl_seconds() track it in lockstep -- proving there isn't a
        second, independently-hardcoded number backing either one. If
        AuthStore.session_email() read some other hardcoded TTL, a session
        created just past the patched (tiny) window would still validate
        here and this test would fail."""
        store = AuthStore(tmp_path / "auth.db")
        try:
            tiny_ttl = timedelta(seconds=2)
            monkeypatch.setattr(google_auth, "_SESSION_TTL", tiny_ttl)
            assert google_auth.session_ttl_seconds() == 2

            sid = store.create_session("u@example.com")
            assert store.session_email(sid) == "u@example.com"

            import datetime as _dt
            real_datetime = google_auth.datetime

            class _FutureDatetime(real_datetime):
                @classmethod
                def now(cls, tz=None):
                    return real_datetime.now(tz) + _dt.timedelta(hours=1)

            monkeypatch.setattr(google_auth, "datetime", _FutureDatetime)
            # An hour past a 2-second TTL: expired under the patched value.
            assert store.session_email(sid) is None
        finally:
            store.close()

    def test_webui_login_cookie_sites_call_the_shared_accessor(self):
        """Both real Set-Cookie sites that grant a session
        (_handle_google_callback's cookie and _handle_auth_claim's cookie)
        must compute Max-Age by calling google_auth.session_ttl_seconds()
        inline, not by embedding a second literal number (the pre-fix bug
        this feature's commit describes: webui.py used to hardcode 2592000
        directly at each Set-Cookie site)."""
        max_age_call_sites = [
            line for line in _WEBUI_SRC.splitlines()
            if "Max-Age={google_auth.session_ttl_seconds()}" in line
        ]
        assert len(max_age_call_sites) >= 2, (
            "expected both login Set-Cookie sites to call "
            "google_auth.session_ttl_seconds() inline"
        )
        # No hardcoded 30-day-in-seconds literal survives anywhere nearby.
        assert "2592000" not in _WEBUI_SRC


# --------------------------------------------------------------------------- #
# desktop.py: webview.start() must be non-private with a real storage path
# --------------------------------------------------------------------------- #

class TestDesktopWebviewPersistence:
    def test_launch_starts_webview_non_private_with_real_storage_path(
        self, monkeypatch, tmp_path
    ):
        """Root cause 57109f4 fixed: pywebview's own webview.start()
        defaults to private_mode=True, an incognito-style context that
        wipes cookies AND localStorage on every quit regardless of the
        server-side session TTL. desktop.launch() must call
        webview.start(private_mode=False, storage_path=<real on-disk
        path>) -- not just call start() at all (every existing test in
        test_desktop.py already proves that much)."""
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))
        monkeypatch.setenv("DOURMOUSE_UI_PORT", "0")
        monkeypatch.setenv("DOURMOUSE_LEARN", "0")
        expected_path = desktop._webview_storage_path()

        fake = _FakeWebview()
        code = desktop.launch(_echo_registry(), port=0, webview_loader=_loader(fake))

        assert code == 0
        assert fake.started is True
        assert fake.start_kwargs.get("private_mode") is False
        assert fake.start_kwargs.get("storage_path") == str(expected_path)
        # Real, on-disk, and actually the workspace/webview_storage
        # convention -- not a tmp/throwaway/in-memory stand-in.
        assert Path(fake.start_kwargs["storage_path"]).is_dir()
        assert Path(fake.start_kwargs["storage_path"]).name == "webview_storage"
        assert Path(fake.start_kwargs["storage_path"]).parent == tmp_path


# --------------------------------------------------------------------------- #
# chat.py: most_recent_session_file() wired at __main__, not inside launch()
# --------------------------------------------------------------------------- #

class TestMostRecentSessionFileWiring:
    def test_launch_itself_never_calls_most_recent_session_file(
        self, monkeypatch, tmp_path
    ):
        """57109f4's own commit message: resolving "the most recent
        session" INSIDE launch() live-caught a real test hang -- every
        hermetic test calls launch() directly and must never reach into
        the real, shared, growing workspace/sessions/ directory. Guard it
        dynamically: if launch() ever calls most_recent_session_file()
        again, this fails loudly instead of just hanging."""
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))
        monkeypatch.setenv("DOURMOUSE_UI_PORT", "0")
        monkeypatch.setenv("DOURMOUSE_LEARN", "0")

        def _must_not_be_called(*_a, **_kw):
            raise AssertionError(
                "desktop.launch() must not call chat.most_recent_session_file() "
                "itself -- that belongs at the __main__ entry point only"
            )

        monkeypatch.setattr(chat, "most_recent_session_file", _must_not_be_called)
        fake = _FakeWebview()
        code = desktop.launch(_echo_registry(), port=0, webview_loader=_loader(fake))
        assert code == 0

    def test_launch_source_never_calls_most_recent_session_file(self):
        """Static complement to the dynamic guard above: launch()'s CODE
        (not its docstring, which explains in prose why it deliberately
        does NOT do this) must never actually call the symbol -- belt and
        suspenders against, e.g., a lazy/conditional import + call that the
        monkeypatch above wouldn't reliably intercept."""
        launch_src = inspect.getsource(desktop.launch)
        code_lines = [
            line for line in launch_src.splitlines()
            if not line.strip().startswith("#")
        ]
        code_only = "\n".join(code_lines)
        # Strip the docstring, which legitimately names the symbol in prose
        # explaining why the real call site is __main__, not here.
        code_only = code_only.split('"""', 2)[-1] if code_only.count('"""') >= 2 else code_only
        assert "most_recent_session_file(" not in code_only

    def test_dunder_main_entry_point_wires_it_explicitly(self):
        """The real desktop entry point -- this module's own __main__
        block -- must be the one place that resolves
        chat.most_recent_session_file() and threads it into launch() as
        session_file, restoring the "resume my chats" behavior for the
        actual app without any hermetic test ever touching real disk
        state as a side effect of calling launch()."""
        assert '__name__ == "__main__"' in _DESKTOP_SRC
        main_block = _DESKTOP_SRC.split('if __name__ == "__main__":', 1)[1]
        assert "from dourmouse.chat import most_recent_session_file" in main_block
        assert "session_file=most_recent_session_file()" in main_block
