"""Finding 197 (plan 1.2): the wallpaper setting.

Backend: photos are sniffed, shrunk, re-saved as JPEG without EXIF, and stored under the workspace.
Page side (run in node): the contrast guard keeps text at 4.5:1 over the worst possible photo pixel.
"""

from __future__ import annotations

import base64
import io
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from dourmouse import os_api
from dourmouse.os_api import ApiError, Request

_ROOT = Path(__file__).resolve().parents[2]
_OS = _ROOT / "ui" / "assets" / "os"
_NODE = shutil.which("node") or ""


@pytest.fixture(autouse=True)
def _workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))
    return tmp_path


def call(method, path, body=None, query=None):
    handler = os_api.find(method, path)
    assert handler is not None, path
    return handler(Request(server=None, query=query or {}, body=body or {}))


def photo(fmt="PNG", size=(40, 30), exif=False):
    from PIL import Image

    img = Image.new("RGB", size, (200, 30, 30))
    out = io.BytesIO()
    if exif and fmt == "JPEG":
        ex = Image.Exif()
        ex[0x010F] = "SomeCamera"
        img.save(out, fmt, exif=ex)
    else:
        img.save(out, fmt)
    return base64.b64encode(out.getvalue()).decode()


def test_default_is_off_with_three_places():
    status, body = call("GET", "/api/os/wallpaper")
    assert status == 200
    assert body["config"] == {"mode": "off", "places": ["HOME", "PROJECTS", "OFFICE"]}
    assert (body["light"], body["dark"], body["rotation"]) == (False, False, 0)


def test_upload_png_becomes_jpeg_and_turns_mode_on(_workspace):
    _, body = call("POST", "/api/os/wallpaper/upload", {"slot": "light", "data": photo("PNG")})
    assert body["light"] and body["config"]["mode"] == "single"
    saved = (_workspace / "wallpapers" / "light.jpg").read_bytes()
    assert saved[:3] == b"\xff\xd8\xff"
    _, img = call("GET", "/api/os/wallpaper/image", query={"slot": ["light"]})
    assert img["data_url"].startswith("data:image/jpeg;base64,")


def test_exif_is_dropped(_workspace):
    from PIL import Image

    call("POST", "/api/os/wallpaper/upload", {"slot": "dark", "data": photo("JPEG", exif=True)})
    with Image.open(_workspace / "wallpapers" / "dark.jpg") as im:
        assert 0x010F not in im.getexif()


def test_large_photo_is_shrunk(_workspace):
    from PIL import Image

    call("POST", "/api/os/wallpaper/upload", {"slot": "light", "data": photo("PNG", size=(5000, 1000))})
    with Image.open(_workspace / "wallpapers" / "light.jpg") as im:
        assert max(im.size) == 2560


@pytest.mark.parametrize("data,status", [
    (base64.b64encode(b"GIF89a....").decode(), 415),
    (base64.b64encode(b"<svg onload=alert(1)>").decode(), 415),
    ("not base64 !!", 400),
    ("", 400),
])
def test_bad_uploads_refused(data, status):
    with pytest.raises(ApiError) as e:
        call("POST", "/api/os/wallpaper/upload", {"slot": "light", "data": data})
    assert e.value.status == status


def test_png_header_with_garbage_body_refused():
    bad = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"junk" * 10).decode()
    with pytest.raises(ApiError) as e:
        call("POST", "/api/os/wallpaper/upload", {"slot": "light", "data": bad})
    assert e.value.status == 400


def test_unknown_slot_and_mode_refused():
    with pytest.raises(ApiError):
        call("POST", "/api/os/wallpaper/upload", {"slot": "../x", "data": photo()})
    with pytest.raises(ApiError):
        call("GET", "/api/os/wallpaper/image", query={"slot": ["../../etc/passwd"]})
    with pytest.raises(ApiError):
        call("POST", "/api/os/wallpaper/config", {"mode": "party"})
    with pytest.raises(ApiError):
        call("POST", "/api/os/wallpaper/config", {"places": ["HOME", "NOPE"]})


def test_rotation_cap_and_daily_pick(_workspace):
    for _ in range(12):
        call("POST", "/api/os/wallpaper/upload", {"slot": "rotation", "data": photo()})
    with pytest.raises(ApiError) as e:
        call("POST", "/api/os/wallpaper/upload", {"slot": "rotation", "data": photo()})
    assert e.value.status == 409
    _, body = call("GET", "/api/os/wallpaper")
    assert body["rotation"] == 12 and body["config"]["mode"] == "rotate"
    _, img = call("GET", "/api/os/wallpaper/image", query={"slot": ["rotation"]})
    assert img["data_url"]


def test_config_places_and_remove_turns_mode_off():
    call("POST", "/api/os/wallpaper/upload", {"slot": "light", "data": photo()})
    _, body = call("POST", "/api/os/wallpaper/config", {"places": ["SETTINGS", "HOME"]})
    assert body["config"]["places"] == ["HOME", "SETTINGS"]
    _, body = call("POST", "/api/os/wallpaper/remove", {"slot": "light"})
    assert body["light"] is False and body["config"]["mode"] == "off"
    _, img = call("GET", "/api/os/wallpaper/image", query={"slot": ["light"]})
    assert img["data_url"] is None


def test_corrupt_config_falls_back_to_defaults(_workspace):
    (_workspace / "wallpapers").mkdir()
    (_workspace / "wallpapers" / "config.json").write_text("{nope", encoding="utf-8")
    _, body = call("GET", "/api/os/wallpaper")
    assert body["config"]["mode"] == "off"


# ---- page side, in node ----

pytestmark_node = pytest.mark.skipif(not _NODE, reason="node not on PATH in this environment")


def node(tmp_path, body):
    uri = lambda name: (_OS / "core" / name).as_uri()  # noqa: E731
    script = tmp_path / "h.mjs"
    script.write_text(
        f"import {{ surfaceAlpha, pickSlot, placeIsOn, parseColor, resolveColor, FLOOR }} from '{uri('wallpaper.js')}';\n"
        f"import {{ contrast }} from '{uri('prefs.js')}';\nimport {{ composite }} from '{uri('appearance.js')}';\n"
        "const R = {};\n" + body + "\nconsole.log(JSON.stringify(R));\n",
        encoding="utf-8",
    )
    proc = subprocess.run([_NODE, str(script)], capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytestmark_node
def test_surface_alpha_guarantees_contrast_over_worst_pixel(tmp_path):
    r = node(tmp_path, """
const cases = [
  ['#000000', '#ffffff'], ['#6e6e73', '#ffffff'], ['#66666b', '#f5f5f7'],
  ['#ffffff', '#1e1e1e'], ['#a1a1a6', '#1e1e1e'], ['#98989d', '#2b2b2d'],
];
R.rows = cases.map(([text, bg]) => {
  const a = surfaceAlpha({ text, bg, floor: 0.5 });
  const worst = contrast(text, '#000000') < contrast(text, '#ffffff') ? '#000000' : '#ffffff';
  return { a, c: contrast(text, composite(bg, a, worst)) };
});
""")
    for row in r["rows"]:
        assert row["c"] >= 4.5, row
        assert 0.5 <= row["a"] <= 1


@pytestmark_node
def test_pure_helpers(tmp_path):
    r = node(tmp_path, """
R.floor = surfaceAlpha({ text: '#000000', bg: '#ffffff', floor: 0.9 });
R.badText = surfaceAlpha({ text: 'red', bg: '#ffffff' });
R.rgba = parseColor('rgba(0, 0, 0, 0.55)');
R.rgb = parseColor('rgb(255,255,255)');
R.evil = parseColor('url(javascript:1)');
R.resolved = resolveColor('rgba(0,0,0,0.5)', '#ffffff');
const cfg = { mode: 'single', places: ['HOME'] };
R.on = [placeIsOn(cfg, 'HOME'), placeIsOn(cfg, 'SECURITY'), placeIsOn({ mode: 'off', places: ['HOME'] }, 'HOME')];
const s = { config: { mode: 'single', places: [] }, light: true, dark: false, rotation: 0 };
R.slots = [pickSlot(s, 'light'), pickSlot(s, 'dark'), pickSlot({ ...s, config: { mode: 'rotate' }, rotation: 3 }, 'dark'),
  pickSlot({ ...s, config: { mode: 'rotate' }, rotation: 0 }, 'dark'), pickSlot({ ...s, config: { mode: 'off' } }, 'light')];
""")
    assert r["floor"] == 0.9
    assert r["badText"] == 1
    assert r["rgba"] == {"hex": "#000000", "alpha": 0.55}
    assert r["rgb"]["hex"] == "#ffffff"
    assert r["evil"] is None
    assert r["resolved"] == "#808080"
    assert r["on"] == [True, False, False]
    assert r["slots"] == ["light", "light", "rotation", None, None]


@pytestmark_node
def test_settings_rows_render_modes_and_places(tmp_path):
    script = tmp_path / "h.mjs"
    script.write_text(
        "const { wallpaperRows } = await import('" + (_OS / "screens" / "settings" / "wallpaper.js").as_uri() + "');\n"
        "const sum = { config: { mode: 'single', places: ['HOME'] }, light: true, dark: false, rotation: 0, rotation_max: 12, places: ['HOME','SETTINGS'] };\n"
        "console.log(JSON.stringify({ single: String(wallpaperRows(sum, { light: 'data:image/jpeg;base64,AAAA' })),"
        " off: String(wallpaperRows({ ...sum, config: { mode: 'off', places: [] } })), none: String(wallpaperRows(null)) }));\n",
        encoding="utf-8",
    )
    proc = subprocess.run([_NODE, str(script)], capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert 'data-wp-mode="off"' in out["single"] and 'data-wp-pick="light"' in out["single"]
    assert 'data-wp-remove="light"' in out["single"] and 'data-wp-pick="dark"' in out["single"]
    assert re.search(r'data-wp-place="HOME"\s+checked', out["single"])
    assert "data-wp-place" not in out["off"] and "data-wp-pick" not in out["off"]
    assert "Not available" in out["none"]
