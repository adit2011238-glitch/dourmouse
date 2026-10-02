"""The only code in app_driver that touches macOS.

Two backends behind one small interface:

* ``PyObjCBackend``: the real Accessibility API (``AXUIElement``) through
  PyObjC, the same primitives ``app_control_ax.py`` already uses. Keystrokes
  are posted to the target pid (``CGEventPostToPid``), never to whatever
  happens to be frontmost.
* ``OsaBackend``: JXA through ``osascript`` and System Events, used only when
  PyObjC does not import. Inputs travel as one JSON argument, never spliced
  into script text. System Events types into the frontmost app, so the same
  script checks the frontmost pid first and refuses if it differs.

The module-level backend is injectable (``set_backend``); tests use a fake
and make no macOS calls.

Tree shape returned by ``read_tree``: ``{"windows": [node, ...],
"truncated": bool}`` where a node is ``{role, subrole, title, description,
placeholder, value, secure, enabled, focused, x, y, w, h, handle, children}``.
``handle`` is opaque (an AXUIElement, or an index path for the fallback) and
never leaves the driver.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from typing import Any, Protocol

from .errors import AppDriverError

NOT_TRUSTED_MESSAGE = (
    "Accessibility permission is not granted to the process running Dourmouse. "
    "Grant it in System Settings > Privacy & Security > Accessibility, then retry. "
    "Nothing was read or done."
)

_VALUE_CHARS = 200


class Backend(Protocol):
    name: str

    def is_trusted(self) -> bool: ...
    def running_apps(self) -> list[dict[str, Any]]: ...
    def frontmost_pid(self) -> int | None: ...
    def activate(self, pid: int) -> bool: ...
    def read_tree(self, pid: int, max_depth: int, max_nodes: int) -> dict[str, Any]: ...
    def press(self, pid: int, handle: Any) -> None: ...
    def focus(self, pid: int, handle: Any) -> None: ...
    def post_text(self, pid: int, text: str) -> None: ...
    def post_key(self, pid: int, keycode: int, modifiers: list[str]) -> None: ...
    def scroll(self, pid: int, area_handle: Any, orientation: str, delta: float) -> tuple[float, float]: ...


def _short(value: Any) -> str | None:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        text = str(value)
        return text[:_VALUE_CHARS] + ("..." if len(text) > _VALUE_CHARS else "")
    return None


class PyObjCBackend:
    name = "pyobjc"

    def __init__(self) -> None:
        import ApplicationServices as AS  # noqa: N812 - Apple's module name
        import Quartz
        from AppKit import NSRunningApplication, NSWorkspace

        self.AS, self.Q = AS, Quartz
        self._ws, self._nsapp = NSWorkspace, NSRunningApplication

    def is_trusted(self) -> bool:
        try:
            return bool(self.AS.AXIsProcessTrusted())
        except Exception:  # noqa: BLE001 - a probe answers, it never crashes the caller
            return False

    def running_apps(self) -> list[dict[str, Any]]:
        out = []
        for app in self._ws.sharedWorkspace().runningApplications():
            if app.activationPolicy() != 0:  # NSApplicationActivationPolicyRegular only
                continue
            out.append({
                "name": str(app.localizedName() or ""),
                "bundle_id": str(app.bundleIdentifier() or ""),
                "pid": int(app.processIdentifier()),
                "frontmost": bool(app.isActive()),
            })
        return out

    def frontmost_pid(self) -> int | None:
        app = self._ws.sharedWorkspace().frontmostApplication()
        return int(app.processIdentifier()) if app is not None else None

    def activate(self, pid: int) -> bool:
        app = self._nsapp.runningApplicationWithProcessIdentifier_(pid)
        return bool(app is not None and app.activateWithOptions_(0))

    def _err(self, err: int, what: str) -> AppDriverError:
        if err == -25211:
            return AppDriverError("not_trusted", NOT_TRUSTED_MESSAGE)
        from dourmouse.app_control_ax import _ax_error_message

        return AppDriverError("failed", f"{what}: {_ax_error_message(err)}")

    def _attr(self, element: Any, name: str) -> Any:
        try:
            err, value = self.AS.AXUIElementCopyAttributeValue(element, name, None)
        except Exception:  # noqa: BLE001 - one unreadable attribute is just absent
            return None
        if err == -25211:
            raise self._err(err, "read")
        return value if err == 0 else None

    def _point(self, value: Any, kind: Any) -> tuple[float, float] | None:
        if value is None:
            return None
        try:
            ok, point = self.AS.AXValueGetValue(value, kind, None)
        except Exception:  # noqa: BLE001 - a position we cannot decode is reported as absent
            return None
        if not ok:
            return None
        return (float(getattr(point, "x", getattr(point, "width", 0.0))), float(getattr(point, "y", getattr(point, "height", 0.0))))

    def _node(self, element: Any, depth: int, max_depth: int, budget: list[int], flags: dict[str, bool]) -> dict[str, Any]:
        AS = self.AS
        budget[0] -= 1
        role = _short(self._attr(element, AS.kAXRoleAttribute)) or ""
        subrole = _short(self._attr(element, AS.kAXSubroleAttribute)) or ""
        secure = "AXSecureTextField" in (role, subrole)
        pos = self._point(self._attr(element, AS.kAXPositionAttribute), AS.kAXValueCGPointType)
        size = self._point(self._attr(element, AS.kAXSizeAttribute), AS.kAXValueCGSizeType)
        node = {
            "role": role,
            "subrole": subrole,
            "title": _short(self._attr(element, AS.kAXTitleAttribute)) or "",
            "description": _short(self._attr(element, AS.kAXDescriptionAttribute)) or "",
            "placeholder": _short(self._attr(element, "AXPlaceholderValue")) or "",
            # A secure field's value is never read, not even to redact it.
            "value": None if secure else _short(self._attr(element, AS.kAXValueAttribute)),
            "secure": secure,
            "enabled": self._attr(element, AS.kAXEnabledAttribute) is not False,
            "focused": bool(self._attr(element, AS.kAXFocusedAttribute)),
            "x": pos[0] if pos else None, "y": pos[1] if pos else None,
            "w": size[0] if size else None, "h": size[1] if size else None,
            "handle": element,
            "children": [],
        }
        if depth < max_depth:
            for child in list(self._attr(element, AS.kAXChildrenAttribute) or []):
                if budget[0] <= 0:
                    flags["truncated"] = True
                    break
                node["children"].append(self._node(child, depth + 1, max_depth, budget, flags))
        elif self._attr(element, AS.kAXChildrenAttribute):
            flags["truncated"] = True
        return node

    def read_tree(self, pid: int, max_depth: int, max_nodes: int) -> dict[str, Any]:
        AS = self.AS
        app = AS.AXUIElementCreateApplication(pid)
        AS.AXUIElementSetMessagingTimeout(app, 1.0)  # an unresponsive app must not hang the server
        err, windows = AS.AXUIElementCopyAttributeValue(app, AS.kAXWindowsAttribute, None)
        if err == -25205:  # no value: the app has no windows
            windows = []
        elif err != 0:
            raise self._err(err, "could not read the app's windows")
        budget, flags = [max_nodes], {"truncated": False}
        out = []
        for window in list(windows or []):
            if budget[0] <= 0:
                flags["truncated"] = True
                break
            out.append(self._node(window, 0, max_depth, budget, flags))
        return {"windows": out, "truncated": flags["truncated"]}

    def press(self, pid: int, handle: Any) -> None:
        err = self.AS.AXUIElementPerformAction(handle, self.AS.kAXPressAction)
        if err != 0:
            raise self._err(err, "the click was not performed")

    def focus(self, pid: int, handle: Any) -> None:
        err = self.AS.AXUIElementSetAttributeValue(handle, self.AS.kAXFocusedAttribute, True)
        if err != 0:
            raise self._err(err, "the field could not be focused")

    def post_text(self, pid: int, text: str) -> None:
        Q = self.Q
        units = len(text.encode("utf-16-le")) // 2
        for down in (True, False):
            event = Q.CGEventCreateKeyboardEvent(None, 0, down)
            Q.CGEventKeyboardSetUnicodeString(event, units, text)
            Q.CGEventPostToPid(pid, event)

    def post_key(self, pid: int, keycode: int, modifiers: list[str]) -> None:
        from dourmouse.app_control_ax import _MODIFIER_FLAGS

        Q = self.Q
        flags = 0
        for m in modifiers:
            flags |= _MODIFIER_FLAGS[m]
        for down in (True, False):
            event = Q.CGEventCreateKeyboardEvent(None, keycode, down)
            if flags:
                Q.CGEventSetFlags(event, flags)
            Q.CGEventPostToPid(pid, event)

    def scroll(self, pid: int, area_handle: Any, orientation: str, delta: float) -> tuple[float, float]:
        AS = self.AS
        attr = AS.kAXVerticalScrollBarAttribute if orientation == "vertical" else AS.kAXHorizontalScrollBarAttribute
        bar = self._attr(area_handle, attr)
        if bar is None:
            raise AppDriverError("failed", f"this scroll area has no {orientation} scroll bar")
        old = self._attr(bar, AS.kAXValueAttribute)
        if not isinstance(old, (int, float)):
            raise AppDriverError("failed", "the scroll bar does not report a position")
        new = min(1.0, max(0.0, float(old) + delta))
        err = AS.AXUIElementSetAttributeValue(bar, AS.kAXValueAttribute, new)
        if err != 0:
            raise self._err(err, "the scroll bar could not be moved")
        return float(old), new


#: The fallback's whole program. One op per call, all inputs in argv[0] as JSON.
JXA_PROGRAM = r"""
function run(argv) {
  var a = JSON.parse(argv[0]);
  var se = Application('System Events');
  function out(x) { return JSON.stringify(x); }
  function attr(el, n) { try { return el.attributes.byName(n).value(); } catch (e) { return null; } }
  function str(v) { if (v === null || v === undefined) return null; if (typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean') { var s = String(v); return s.length > 200 ? s.slice(0, 200) + '...' : s; } return null; }
  if (a.op === 'trusted') return out({ok: se.uiElementsEnabled()});
  if (a.op === 'apps') {
    var ps = se.processes.whose({backgroundOnly: false})();
    var r = [];
    for (var i = 0; i < ps.length; i++) { var p = ps[i]; var b = ''; try { b = p.bundleIdentifier(); } catch (e) {} r.push({name: p.name(), bundle_id: b || '', pid: p.unixId(), frontmost: p.frontmost()}); }
    return out({ok: r});
  }
  if (a.op === 'frontmost') { var f = se.processes.whose({frontmost: true})(); return out({ok: f.length ? f[0].unixId() : null}); }
  var procs = se.processes.whose({unixId: a.pid})();
  if (!procs.length) return out({error: 'not_running'});
  var proc = procs[0];
  function resolve(path) { var el = proc.windows()[path[0]]; for (var i = 1; i < path.length; i++) el = el.uiElements()[path[i]]; return el; }
  if (a.op === 'activate') { proc.frontmost = true; return out({ok: true}); }
  if (a.op === 'tree') {
    var budget = a.max_nodes, truncated = false;
    function node(el, depth, path) {
      budget--;
      var role = str(attr(el, 'AXRole')) || '', sub = str(attr(el, 'AXSubrole')) || '';
      var secure = role === 'AXSecureTextField' || sub === 'AXSecureTextField';
      var pos = attr(el, 'AXPosition'), size = attr(el, 'AXSize');
      var n = {role: role, subrole: sub, title: str(attr(el, 'AXTitle')) || '', description: str(attr(el, 'AXDescription')) || '',
               placeholder: str(attr(el, 'AXPlaceholderValue')) || '', value: secure ? null : str(attr(el, 'AXValue')), secure: secure,
               enabled: attr(el, 'AXEnabled') !== false, focused: attr(el, 'AXFocused') === true,
               x: pos ? pos[0] : null, y: pos ? pos[1] : null, w: size ? size[0] : null, h: size ? size[1] : null, handle: path, children: []};
      var kids = []; try { kids = el.uiElements(); } catch (e) {}
      if (depth < a.max_depth) { for (var i = 0; i < kids.length; i++) { if (budget <= 0) { truncated = true; break; } n.children.push(node(kids[i], depth + 1, path.concat([i]))); } }
      else if (kids.length) truncated = true;
      return n;
    }
    var ws = proc.windows(), w = [];
    for (var i = 0; i < ws.length; i++) { if (budget <= 0) { truncated = true; break; } w.push(node(ws[i], 0, [i])); }
    return out({ok: {windows: w, truncated: truncated}});
  }
  if (a.op === 'press') { resolve(a.path).actions.byName('AXPress').perform(); return out({ok: true}); }
  if (a.op === 'focus') { resolve(a.path).attributes.byName('AXFocused').value = true; return out({ok: true}); }
  var front = se.processes.whose({frontmost: true})();
  var frontPid = front.length ? front[0].unixId() : null;
  if (a.op === 'text' || a.op === 'key') {
    if (frontPid !== a.pid) return out({error: 'not_frontmost'});
    if (a.op === 'text') se.keystroke(a.text); else se.keyCode(a.keycode, {using: a.using});
    return out({ok: true});
  }
  if (a.op === 'scroll') {
    var bars = resolve(a.path).scrollBars();
    for (var i = 0; i < bars.length; i++) {
      var o = attr(bars[i], 'AXOrientation');
      if ((a.orientation === 'vertical') === (o === 'AXVerticalOrientation')) {
        var old = Number(bars[i].value()); var nv = Math.min(1, Math.max(0, old + a.delta)); bars[i].value = nv; return out({ok: [old, nv]});
      }
    }
    return out({error: 'no_scroll_bar'});
  }
  return out({error: 'unknown op'});
}
"""

_OSA_MODIFIERS = {"command": "command down", "cmd": "command down", "option": "option down", "alt": "option down",
                  "shift": "shift down", "control": "control down", "ctrl": "control down"}

Runner = Callable[[list[str], float], "subprocess.CompletedProcess[str]"]


def _default_runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, stdin=subprocess.DEVNULL, check=False)


class OsaBackend:
    name = "osascript"

    def __init__(self, runner: Runner | None = None) -> None:
        self._run_proc = runner or _default_runner

    def _call(self, op: str, timeout: float = 10.0, **args: Any) -> Any:
        argv = ["osascript", "-l", "JavaScript", "-e", JXA_PROGRAM, json.dumps({"op": op, **args})]
        try:
            proc = self._run_proc(argv, timeout)
        except subprocess.TimeoutExpired as exc:
            raise AppDriverError("failed", f"System Events did not answer within {timeout:.0f}s ({op})") from exc
        except OSError as exc:
            raise AppDriverError("unavailable", f"osascript could not be run: {exc}") from exc
        stderr = (proc.stderr or "").strip()
        if proc.returncode != 0:
            if "-1719" in stderr or "-25211" in stderr or "assistive access" in stderr.lower():
                raise AppDriverError("not_trusted", NOT_TRUSTED_MESSAGE)
            raise AppDriverError("failed", f"System Events failed ({op}): {stderr[:300] or 'no detail'}")
        try:
            reply = json.loads((proc.stdout or "").strip() or "{}")
        except ValueError as exc:
            raise AppDriverError("failed", f"System Events returned something unreadable ({op})") from exc
        if "error" in reply:
            code = {"not_running": "not_running", "not_frontmost": "not_frontmost"}.get(reply["error"], "failed")
            raise AppDriverError(code, f"System Events refused ({op}): {reply['error']}")
        return reply.get("ok")

    def is_trusted(self) -> bool:
        try:
            return bool(self._call("trusted"))
        except AppDriverError:
            return False

    def running_apps(self) -> list[dict[str, Any]]:
        return list(self._call("apps") or [])

    def frontmost_pid(self) -> int | None:
        value = self._call("frontmost")
        return int(value) if value is not None else None

    def activate(self, pid: int) -> bool:
        return bool(self._call("activate", pid=pid))

    def read_tree(self, pid: int, max_depth: int, max_nodes: int) -> dict[str, Any]:
        return self._call("tree", timeout=30.0, pid=pid, max_depth=max_depth, max_nodes=max_nodes)

    def press(self, pid: int, handle: Any) -> None:
        self._call("press", pid=pid, path=list(handle))

    def focus(self, pid: int, handle: Any) -> None:
        self._call("focus", pid=pid, path=list(handle))

    def post_text(self, pid: int, text: str) -> None:
        self._call("text", pid=pid, text=text)

    def post_key(self, pid: int, keycode: int, modifiers: list[str]) -> None:
        self._call("key", pid=pid, keycode=keycode, using=[_OSA_MODIFIERS[m] for m in modifiers])

    def scroll(self, pid: int, area_handle: Any, orientation: str, delta: float) -> tuple[float, float]:
        old, new = self._call("scroll", pid=pid, path=list(area_handle), orientation=orientation, delta=delta)
        return float(old), float(new)


_backend: Backend | None = None
_backend_error: str | None = None


def set_backend(backend: Backend | None) -> None:
    """Inject a backend (tests), or None to pick the real one again."""
    global _backend, _backend_error
    _backend, _backend_error = backend, None


def get_backend() -> Backend:
    global _backend, _backend_error
    if _backend is not None:
        return _backend
    if sys.platform != "darwin":
        raise AppDriverError("unavailable", f"app driving is macOS only (this machine is {sys.platform}).")
    try:
        _backend = PyObjCBackend()
    except Exception as exc:  # noqa: BLE001 - PyObjC missing is the documented fallback case
        _backend_error = f"PyObjC unavailable ({type(exc).__name__}: {exc}); using osascript"
        _backend = OsaBackend()
    return _backend


def backend_note() -> str | None:
    return _backend_error
