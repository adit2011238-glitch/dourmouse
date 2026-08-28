"""System tray presence + instant camera/mic kill-switch (Vision stage 3).

Why this exists (roadmap: "Vision — the more ambitious version"): an ambient
assistant that can eventually listen and watch continuously (stage 4
wake-word, stage 5 continuous hand tracking) is NON-NEGOTIABLE about one
thing per the design brief — it must never be ambiguous about whether it's
watching/listening. This module ships the safety precondition BEFORE any of
that continuous capture exists:

1. A persisted, cross-process ``KillSwitchState`` (mic_enabled,
   camera_enabled) that future stages are REQUIRED to check via
   ``mic_allowed()`` / ``camera_allowed()`` before arming a continuous mic or
   camera capture. This is the contract stage 4/5 must honor — see the
   docstring on ``KillSwitchState`` below.
2. A real system tray icon (pystray) with ONE control — "Kill camera + mic
   NOW" — that flips both flags off atomically, with no confirmation dialog
   to fight through, and an icon that VISUALLY encodes mic/camera state
   (two colored dots baked into the tray bitmap itself, not just a tooltip
   — the task explicitly calls out that a tooltip alone is not enough).

Honesty (Rule 2.1/2.2), matching dourmouse/desktop.py's pattern exactly:
pystray+Pillow are imported lazily so this module (and its tests) work
without them installed; ``_import_pystray()`` raises a clear NOT CONFIGURED
error naming the exact fix instead of failing with an opaque ImportError.

What this does NOT do (read before assuming more than is here): today
(2026, stage 2/3 of the roadmap) nothing in this repo does continuous mic or
camera capture — dourmouse/voice.py is push-to-talk only and the browser
hand/face tracking in ui/index.html is gated by an explicit Start/Stop
button the user clicks. So flipping this kill switch off does NOT reach
into an in-progress browser vision session or voice.py today — there is
nothing continuous running yet to interrupt. What it DOES do is give stage
4 and stage 5 a single, already-tested, already-wired place to check before
they ever turn a continuous mic or camera on. Wiring an ACTUAL continuous
capture path into this switch is out of scope here by design (that work
belongs to stage 4/5, and touching voice.py / webui.py / ui/index.html to
pre-wire it was explicitly out of scope for this task).

Run it standalone:
    .venv/bin/python -m dourmouse.tray
"""

from __future__ import annotations

import json
import os
import sys
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

_STATE_ENV = "DOURMOUSE_PRIVACY_STATE"
_WORKSPACE_ENV = "DOURMOUSE_WORKSPACE"


def _state_path() -> Path:
    """Where the kill-switch flag lives — same workspace convention as
    dourmouse/live_feeds.py's tasks.json and dourmouse/learn.py's memory db
    (DOURMOUSE_WORKSPACE, default ``workspace/``), so one env var controls
    every DourMouse data file consistently. ``DOURMOUSE_PRIVACY_STATE`` is
    an explicit override (used by tests) that wins outright."""
    override = os.environ.get(_STATE_ENV, "").strip()
    if override:
        return Path(override)
    root = Path(os.environ.get(_WORKSPACE_ENV, "").strip() or "workspace")
    return root / "privacy_state.json"


@dataclass
class KillSwitchState:
    """The persisted, cross-process privacy flag.

    This is the CONTRACT stage 4 (wake-word) and stage 5 (continuous hand
    tracking) must honor: before either arms a continuous mic or camera
    capture, it MUST call ``mic_allowed()`` / ``camera_allowed()`` (or load
    this state directly) and refuse to start when the answer is False.
    dourmouse/overlay.py (stage 2) reads this SAME state so the overlay and
    the tray never disagree about whether the mic/camera are armed.

    Defaults to both enabled — nothing is silently pre-disabled; a fresh
    install starts from "nothing has been killed yet", matching the honest
    fact that nothing continuous exists to kill today.
    """

    mic_enabled: bool = True
    camera_enabled: bool = True
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_state(path: Path | None = None) -> KillSwitchState:
    """Read the persisted state. Honest defaults (both enabled) when the
    file is missing, unreadable, or corrupt — this NEVER raises, so a
    reader (the overlay, a future stage-4/5 gate) always gets a usable
    answer instead of crashing on a bad file."""
    p = path or _state_path()
    if not p.exists():
        return KillSwitchState()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return KillSwitchState()
    if not isinstance(raw, dict):
        return KillSwitchState()
    return KillSwitchState(
        mic_enabled=bool(raw.get("mic_enabled", True)),
        camera_enabled=bool(raw.get("camera_enabled", True)),
        updated_at=str(raw.get("updated_at") or ""),
    )


def save_state(state: KillSwitchState, path: Path | None = None) -> None:
    p = path or _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state.to_dict(), indent=2) + "\n", encoding="utf-8")


def mic_allowed(path: Path | None = None) -> bool:
    """The check future continuous-mic code (stage 4) must call before it
    ever opens an audio stream continuously."""
    return load_state(path).mic_enabled


def camera_allowed(path: Path | None = None) -> bool:
    """The check future continuous-camera code (stage 5) must call before
    it ever arms continuous hand/face tracking."""
    return load_state(path).camera_enabled


class KillSwitch:
    """Thread-safe in-process wrapper around the persisted state file.

    One instance per tray process. ``kill_all()`` is THE control the task
    requires: one call, no confirmation, both mic and camera off at once.
    Individual ``set_mic`` / ``set_camera`` exist for the tray's checkbox
    items, but there is deliberately no single "restore everything" menu
    action bound anywhere — re-arming is two separate deliberate clicks,
    never as thoughtless as killing both.
    """

    def __init__(
        self,
        path: Path | None = None,
        on_change: Callable[[KillSwitchState], None] | None = None,
    ) -> None:
        self._path = path or _state_path()
        self._lock = threading.Lock()
        self._state = load_state(self._path)
        self._on_change = on_change

    @property
    def state(self) -> KillSwitchState:
        with self._lock:
            return KillSwitchState(**asdict(self._state))

    def kill_all(self) -> KillSwitchState:
        """The kill-switch: both mic and camera off, atomically, no
        confirmation dialog. This is the ONE control the task requires."""
        return self._set(mic_enabled=False, camera_enabled=False)

    def set_mic(self, enabled: bool) -> KillSwitchState:
        return self._set(mic_enabled=bool(enabled))

    def set_camera(self, enabled: bool) -> KillSwitchState:
        return self._set(camera_enabled=bool(enabled))

    def _set(self, **kwargs: Any) -> KillSwitchState:
        with self._lock:
            data = asdict(self._state)
            data.update(kwargs)
            data["updated_at"] = datetime.now().isoformat(timespec="seconds")
            self._state = KillSwitchState(**data)
            save_state(self._state, self._path)
            snapshot = KillSwitchState(**asdict(self._state))
        if self._on_change is not None:
            try:
                self._on_change(snapshot)
            except Exception:  # noqa: BLE001 -- a bad callback must not corrupt state
                pass
        return snapshot


def _import_pystray() -> tuple[Any, Any, Any]:
    """Lazy import so tray.py (and its tests) work without pystray/Pillow
    installed — the same seam shape as dourmouse.desktop._import_webview."""
    try:
        import pystray  # type: ignore
        from PIL import Image, ImageDraw  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "NOT CONFIGURED: the system tray needs pystray + Pillow — run "
            "`.venv/bin/python -m pip install -r requirements-desktop.txt`."
        ) from exc
    return pystray, Image, ImageDraw


_ICON_SIZE = 64
_COLOR_ON = (58, 220, 100, 255)
_COLOR_OFF = (200, 60, 52, 255)
_COLOR_BG = (24, 24, 28, 255)


def _build_icon_image(image_module: Any, draw_module: Any, *,
                       mic_enabled: bool, camera_enabled: bool) -> Any:
    """Draw the tray bitmap itself so mic/camera state is a LIT/DARK visual
    indicator baked into the icon — not just tooltip text (the task is
    explicit that a tooltip alone does not satisfy "unambiguously shows
    current state"). Left dot = mic, right dot = camera; green = armed,
    red = killed."""
    img = image_module.new("RGBA", (_ICON_SIZE, _ICON_SIZE), (0, 0, 0, 0))
    draw = draw_module.Draw(img)
    draw.rounded_rectangle(
        [4, 4, _ICON_SIZE - 4, _ICON_SIZE - 4], radius=14, fill=_COLOR_BG
    )
    mic_color = _COLOR_ON if mic_enabled else _COLOR_OFF
    cam_color = _COLOR_ON if camera_enabled else _COLOR_OFF
    draw.ellipse([12, 23, 30, 41], fill=mic_color)
    draw.ellipse([34, 23, 52, 41], fill=cam_color)
    return img


class TrayApp:
    """The real system tray presence.

    Split into ``_build_icon()`` (pure construction, safe to call and
    inspect headlessly — no display/menu-bar backend required) and
    ``run()`` (blocks on the platform event loop — needs a live desktop
    session, not runnable in this environment). Tests exercise the former
    and the action handlers directly; ``run()`` itself is uncovered here by
    necessity and needs a live desktop session to confirm.
    """

    def __init__(self, kill_switch: KillSwitch | None = None,
                 icon_factory: Callable[[], tuple[Any, Any, Any]] | None = None) -> None:
        self._kill_switch = kill_switch or KillSwitch()
        self._icon_factory = icon_factory or _import_pystray
        self._icon: Any = None

    def _title(self) -> str:
        s = self._kill_switch.state
        mic = "mic on" if s.mic_enabled else "MIC KILLED"
        cam = "cam on" if s.camera_enabled else "CAM KILLED"
        return f"DourMouse — {mic} / {cam}"

    def _refresh_icon(self) -> None:
        if self._icon is None:
            return
        _, image_module, draw_module = self._icon_factory()
        s = self._kill_switch.state
        self._icon.icon = _build_icon_image(
            image_module, draw_module, mic_enabled=s.mic_enabled,
            camera_enabled=s.camera_enabled,
        )
        self._icon.title = self._title()
        try:
            self._icon.update_menu()
        except Exception:  # noqa: BLE001 -- cosmetic refresh only
            pass

    # -- menu actions (pystray calls these as action(icon, item)) -------- #

    def _on_kill_all(self, icon: Any, item: Any) -> None:  # noqa: ARG002
        self._kill_switch.kill_all()
        self._refresh_icon()

    def _on_toggle_mic(self, icon: Any, item: Any) -> None:  # noqa: ARG002
        s = self._kill_switch.state
        self._kill_switch.set_mic(not s.mic_enabled)
        self._refresh_icon()

    def _on_toggle_camera(self, icon: Any, item: Any) -> None:  # noqa: ARG002
        s = self._kill_switch.state
        self._kill_switch.set_camera(not s.camera_enabled)
        self._refresh_icon()

    def _on_quit(self, icon: Any, item: Any) -> None:  # noqa: ARG002
        icon.stop()

    def _build_icon(self) -> Any:
        pystray, image_module, draw_module = self._icon_factory()
        s = self._kill_switch.state
        menu = pystray.Menu(
            pystray.MenuItem(
                "Kill camera + mic NOW", self._on_kill_all, default=True,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Mic enabled", self._on_toggle_mic,
                checked=lambda item: self._kill_switch.state.mic_enabled,  # noqa: ARG005
            ),
            pystray.MenuItem(
                "Camera enabled", self._on_toggle_camera,
                checked=lambda item: self._kill_switch.state.camera_enabled,  # noqa: ARG005
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._on_quit),
        )
        icon_image = _build_icon_image(
            image_module, draw_module, mic_enabled=s.mic_enabled,
            camera_enabled=s.camera_enabled,
        )
        return pystray.Icon("dourmouse", icon_image, self._title(), menu)

    def run(self) -> None:
        """Blocking: enters the OS tray event loop. Needs a live desktop
        session — cannot run headlessly, and was not runnable/verifiable in
        this sandboxed environment."""
        self._icon = self._build_icon()
        self._icon.run()


def launch() -> int:
    try:
        TrayApp().run()
        return 0
    except RuntimeError as exc:
        print(f"[TRAY] {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(launch())
