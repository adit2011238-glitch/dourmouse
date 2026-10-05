#!/usr/bin/env python3
"""Speed budget check for the installed Dourmouse app (phase I2, finding #171).

Launches an ISOLATED copy of ``~/Applications/Dourmouse.app`` (its own ports, workspace,
config folder, data folder and app name, so nothing of the owner's is read or written),
measures it, stops it by process tree, and compares the numbers with the budget in
``~/Documents/DOURMOUSE/PERF_BUDGET.md`` (the same numbers are in BUDGET below).

Measured, all from the moment ``open`` is called:

  server_ready_s        the server answers GET /api/os/ping
  first_screen_ready_s  the console window shows the HOME screen with its data in place
  app_boot_s            launch to the moment the app asked for the server to start
  screen_switch_ms      per screen (HOME, BROWSER, MEDIA, SETTINGS): the hash changes until the
                        screen root is mounted and no longer says "loading"; first visit (module
                        import included) and second visit
  memory_mb             resident memory of the server process and of the app (main process and
                        all helpers) after the idle period (default 60 s)

    .venv/bin/python scripts/perf_check.py
    .venv/bin/python scripts/perf_check.py --idle 10 --json
    .venv/bin/python scripts/perf_check.py --check      # exit 1 when a number is over budget

It refuses the owner's ports (8765, 9333, 9334) and any port something already listens on. It
never uses pkill: it stops only the processes it started, found through the process tree under
the isolated copy's own main process. Needs the Playwright package (already a project
dependency) for the in-page timing; without it only the process-level numbers are reported.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

OWNER_PORTS = (8765, 9333, 9334)
SCREENS = ("HOME", "BROWSER", "MEDIA", "SETTINGS")
DEFAULT_APP = Path.home() / "Applications" / "Dourmouse.app"

# The budget. A number over its line fails --check. Set from the measurements recorded in
# PERF_BUDGET.md (two clean runs, 2026-10-05) with about 50 to 100 percent headroom, because
# the first screen swung from 7.7 to 13 s between two runs of the same build on an idle Mac;
# tighten it when the numbers have been stable for a while.
BUDGET: dict[str, float] = {
    "server_ready_s": 4.0,
    "first_screen_ready_s": 20.0,
    "screen_switch_cold_ms": 3500.0,
    "screen_switch_warm_ms": 1000.0,
    "server_rss_mb": 700.0,
    "app_rss_mb": 1300.0,
}


# --------------------------------------------------------------------------- #
# pure helpers (tested in dourmouse/tests/test_perf_check.py)
# --------------------------------------------------------------------------- #


def refuse_owner_ports(*ports: int) -> None:
    """Exit with a clear message when any port is one the owner's live app uses, is out of
    range, or repeats."""
    seen: set[int] = set()
    for port in ports:
        if port in OWNER_PORTS:
            raise SystemExit(f"refusing port {port}: it belongs to the owner's running app (8765, 9333, 9334)")
        if not 1024 <= port <= 65535:
            raise SystemExit(f"port {port} is outside 1024-65535")
        if port in seen:
            raise SystemExit(f"port {port} was given twice; each of the three ports must differ")
        seen.add(port)


def port_in_use(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def parse_ps(text: str) -> list[dict[str, Any]]:
    """Rows of ``ps -axo pid=,ppid=,rss=,command=`` as dicts (rss in KB)."""
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) < 4:
            continue
        try:
            rows.append({"pid": int(parts[0]), "ppid": int(parts[1]), "rss_kb": int(parts[2]), "command": parts[3]})
        except ValueError:
            continue
    return rows


def descendants(rows: list[dict[str, Any]], root: int) -> list[dict[str, Any]]:
    """The process ``root`` and everything below it, found through parent ids."""
    children: dict[int, list[dict[str, Any]]] = {}
    by_pid: dict[int, dict[str, Any]] = {}
    for row in rows:
        by_pid[row["pid"]] = row
        children.setdefault(row["ppid"], []).append(row)
    out: list[dict[str, Any]] = []
    stack = [root]
    seen: set[int] = set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        if pid in by_pid:
            out.append(by_pid[pid])
        stack.extend(child["pid"] for child in children.get(pid, []))
    return out


def parse_lsof_pids(text: str) -> list[int]:
    """Process ids from ``lsof -t`` output."""
    return [int(tok) for tok in text.split() if tok.isdigit()]


def listener_pid(port: int) -> int | None:
    """The process listening on a loopback port. The isolated copy's main Electron process is the
    one holding its own DevTools port; it carries no such argument on its command line (the switch
    is set inside the app), so the port is how it is told apart from the owner's app."""
    out = subprocess.run(
        ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"], capture_output=True, text=True, check=False
    ).stdout
    pids = parse_lsof_pids(out)
    return pids[0] if pids else None


def split_memory(rows: list[dict[str, Any]], main_pid: int) -> dict[str, Any]:
    """Resident memory in MB of the server (the dourmouse.webui process and anything below it)
    and of the app (the main process and every helper that is not part of the server)."""
    tree = descendants(rows, main_pid)
    server_roots = [r["pid"] for r in tree if "dourmouse.webui" in r["command"]]
    server_pids: set[int] = set()
    for pid in server_roots:
        server_pids.update(r["pid"] for r in descendants(rows, pid))
    server_kb = sum(r["rss_kb"] for r in tree if r["pid"] in server_pids)
    app_kb = sum(r["rss_kb"] for r in tree if r["pid"] not in server_pids)
    return {
        "server_rss_mb": round(server_kb / 1024, 1),
        "app_rss_mb": round(app_kb / 1024, 1),
        "server_pids": sorted(server_pids),
        "app_process_count": len([r for r in tree if r["pid"] not in server_pids]),
    }


def over_budget(result: dict[str, Any], budget: dict[str, float] | None = None) -> list[str]:
    """Names of the measured numbers that are above the budget (missing numbers are not blamed)."""
    budget = BUDGET if budget is None else budget
    flat: dict[str, float] = {}
    for key in ("server_ready_s", "first_screen_ready_s"):
        if isinstance(result.get(key), (int, float)):
            flat[key] = float(result[key])
    mem = result.get("memory_mb") or {}
    for key in ("server_rss_mb", "app_rss_mb"):
        if isinstance(mem.get(key), (int, float)):
            flat[key] = float(mem[key])
    cold = [v["cold"] for v in (result.get("screen_switch_ms") or {}).values() if isinstance(v.get("cold"), (int, float)) and v["cold"] >= 0]
    warm = [v["warm"] for v in (result.get("screen_switch_ms") or {}).values() if isinstance(v.get("warm"), (int, float)) and v["warm"] >= 0]
    if cold:
        flat["screen_switch_cold_ms"] = max(cold)
    if warm:
        flat["screen_switch_warm_ms"] = max(warm)
    return sorted(k for k, v in flat.items() if k in budget and v > budget[k])


# --------------------------------------------------------------------------- #
# the measurement
# --------------------------------------------------------------------------- #


def _ps_rows() -> list[dict[str, Any]]:
    out = subprocess.run(["ps", "-axo", "pid=,ppid=,rss=,command="], capture_output=True, text=True, check=False).stdout
    return parse_ps(out)


def _get(url: str, timeout: float = 1.5) -> int:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - callers pass fixed loopback http URLs
            return int(resp.status)
    except Exception:  # noqa: BLE001 - not up yet
        return 0


def _stop_tree(main_pid: int | None, grace: float = 15.0) -> list[int]:
    """Stop the isolated copy. The tree is read first (a killed main process leaves its server
    re-parented, out of reach of a parent id search), then the main process is asked to quit
    (its own before-quit stops its server), and whatever of that same snapshot is still alive
    after the grace period, with the same command line, is ended. Only pids from the snapshot
    are ever signalled. Returns the pids that had to be forced."""
    if main_pid is None:
        return []
    snapshot = {r["pid"]: r["command"] for r in descendants(_ps_rows(), main_pid)}
    with contextlib.suppress(ProcessLookupError):
        os.kill(main_pid, 15)

    def alive() -> list[int]:
        now = {r["pid"]: r["command"] for r in _ps_rows()}
        return [pid for pid, cmd in snapshot.items() if now.get(pid) == cmd]

    deadline = time.time() + grace
    while time.time() < deadline and alive():
        time.sleep(0.4)
    forced = alive()
    for pid in forced:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, 9)
    return forced


SWITCH_JS = """
async ([slug, id]) => {
  const t0 = performance.now();
  location.hash = '#/' + slug;
  return await new Promise((resolve) => {
    const tick = () => {
      const el = document.querySelector('#body [data-screen="' + id + '"]');
      const state = el && el.dataset ? el.dataset.state : undefined;
      if (el && state !== 'loading') return resolve({ ms: performance.now() - t0, state: state || '' });
      if (performance.now() - t0 > 20000) return resolve({ ms: -1, state: 'timeout' });
      requestAnimationFrame(tick);
    };
    tick();
  });
}
"""

READY_JS = """
() => {
  const el = document.querySelector('#body [data-screen="HOME"]');
  if (!el) return false;
  const state = el.dataset ? el.dataset.state : undefined;
  return state !== 'loading';
}
"""


def measure(app: Path, ui_port: int, cdp_port: int, pane_port: int, idle_s: float, keep_dir: bool = False) -> dict[str, Any]:
    refuse_owner_ports(ui_port, cdp_port, pane_port)
    for port in (ui_port, cdp_port, pane_port):
        if port_in_use(port):
            raise SystemExit(f"port {port} already has a listener; pick another")
    if not (app / "Contents" / "MacOS").is_dir():
        raise SystemExit(f"{app} is not an app bundle")
    tmp = Path(tempfile.mkdtemp(prefix="dm_perf_"))
    for name in ("ws", "cfg", "ud"):
        (tmp / name).mkdir()
    app_name = f"DourmousePerf{ui_port}"
    result: dict[str, Any] = {
        "app": str(app),
        "isolated_dir": str(tmp),
        "ports": {"ui": ui_port, "cdp": cdp_port, "pane": pane_port},
        "measured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "screen_switch_ms": {},
        "notes": [],
    }
    env_args: list[str] = []
    for key, value in {
        "DOURMOUSE_UI_PORT": str(ui_port),
        "DOURMOUSE_ELECTRON_CDP_PORT": str(cdp_port),
        "DOURMOUSE_ELECTRON_PANE_PORT": str(pane_port),
        "DOURMOUSE_WORKSPACE": str(tmp / "ws"),
        "DOURMOUSE_CONFIG_DIR": str(tmp / "cfg"),
        "DOURMOUSE_USER_DATA_DIR": str(tmp / "ud"),
        "DOURMOUSE_ELECTRON_APP_NAME": app_name,
        # configured, so "/" opens the OS shell (an empty config dir would redirect to the setup
        # wizard); a local-model setting, not a credential
        "DOURMOUSE_LLM_BACKEND": "ollama",
    }.items():
        env_args += ["--env", f"{key}={value}"]
    main_pid: int | None = None
    try:
        launched_wall = time.time()
        t0 = time.monotonic()
        subprocess.run(
            ["open", "-n", "-a", str(app), "--stdout", str(tmp / "out.log"), "--stderr", str(tmp / "err.log"), *env_args],
            check=True,
        )
        result["open_returned_s"] = round(time.monotonic() - t0, 3)

        # the main process, found through its own DevTools port, so it can be stopped whatever happens next
        end_pid = t0 + 30
        while time.monotonic() < end_pid and main_pid is None:
            main_pid = listener_pid(cdp_port)
            if main_pid is None:
                time.sleep(0.1)
        result["main_pid"] = main_pid
        if main_pid is not None:
            result["devtools_port_open_s"] = round(time.monotonic() - t0, 2)

        base = f"http://127.0.0.1:{ui_port}"
        deadline = t0 + 90
        while time.monotonic() < deadline:
            if _get(f"{base}/api/os/ping") == 200:
                result["server_ready_s"] = round(time.monotonic() - t0, 2)
                break
            time.sleep(0.05)
        else:
            result["notes"].append("the server did not answer /api/os/ping within 90 s")
            return result
        # when did the app ask for the server? (first line the supervisor wrote)
        log = tmp / "ud" / "logs" / "server.log"
        try:
            for line in log.read_text(errors="replace").splitlines():
                if line.startswith("[supervisor] ") and "spawning" in line:
                    stamp = line.split(" ", 2)[1]
                    from datetime import datetime

                    spawned = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
                    result["app_boot_s"] = round(spawned - launched_wall, 2)
                    result["spawn_to_ready_s"] = round(result["server_ready_s"] - result["app_boot_s"], 2)
                    break
        except OSError:
            result["notes"].append("no supervisor log line found (an older main.js?)")

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            result["notes"].append("playwright is not installed: only process level numbers were taken")
            sync_playwright = None  # type: ignore[assignment]

        if sync_playwright is not None:
            # wait for the console window to exist before attaching a DevTools client to it
            page_url = ""
            end = time.monotonic() + 60
            while time.monotonic() < end and not page_url:
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{cdp_port}/json/list", timeout=1.5) as resp:
                        pages = json.loads(resp.read().decode())
                    for p in pages:
                        u = str(p.get("url", ""))
                        if u.startswith(base + "/") and p.get("type") == "page" and "/map" not in u and "atlas-lab" not in u:
                            page_url = u
                            break
                except Exception:  # noqa: BLE001, S110 - the window is not up yet; the loop retries
                    pass
                if not page_url:
                    time.sleep(0.1)
            with sync_playwright() as pw:
                browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}", timeout=30000)
                page = None
                for ctx in browser.contexts:
                    for pg in ctx.pages:
                        if pg.url.startswith(base + "/") and "/map" not in pg.url and "atlas-lab" not in pg.url:
                            page = pg
                if page is None:
                    result["notes"].append("the console window page was not found over DevTools")
                else:
                    try:
                        page.wait_for_function(READY_JS, timeout=60000, polling=50)
                        result["first_screen_ready_s"] = round(time.monotonic() - t0, 2)
                    except Exception as exc:  # noqa: BLE001
                        result["notes"].append(f"HOME never became ready: {str(exc)[:120]}")
                    result["screen_switch_ms"] = _measure_switches(page)
                    result["page_navigation_ms"] = page.evaluate(
                        "() => { const n = performance.getEntriesByType('navigation')[0]; return n ? { domContentLoaded: Math.round(n.domContentLoadedEventEnd), load: Math.round(n.loadEventEnd) } : null; }"
                    )
                browser.close()

        if idle_s > 0:
            time.sleep(idle_s)
        rows = _ps_rows()
        if main_pid is not None:
            mem = split_memory(rows, main_pid)
            result["memory_mb"] = {k: mem[k] for k in ("server_rss_mb", "app_rss_mb")}
            result["memory_detail"] = {"app_process_count": mem["app_process_count"], "idle_s": idle_s, "server_pids": mem["server_pids"]}
        else:
            result["notes"].append("the app's main process was not found, memory not measured")
    finally:
        forced = _stop_tree(main_pid)
        if forced:
            result["notes"].append(f"had to force-end {len(forced)} process(es) after the quit request")
        result["stopped_clean"] = not forced
        if not keep_dir:
            shutil.rmtree(tmp, ignore_errors=True)
            result["isolated_dir_removed"] = True
    return result


def _measure_switches(page: Any) -> dict[str, dict[str, Any]]:
    """First visit then second visit of each screen. HOME is already showing at the start, so
    its first visit is the cold start itself; it is measured here after leaving and returning."""
    out: dict[str, dict[str, Any]] = {s: {} for s in SCREENS}
    order = ["BROWSER", "MEDIA", "SETTINGS", "HOME"]
    for label, rounds in (("cold", order), ("warm", order)):
        for screen in rounds:
            res = page.evaluate(SWITCH_JS, [screen.lower(), screen])
            out[screen][label] = round(res["ms"], 1)
            out[screen][label + "_state"] = res["state"]
            time.sleep(0.3)
    out["HOME"]["note"] = "HOME's first screen is part of first_screen_ready_s; its cold number here is a return to HOME"
    return out


def render(result: dict[str, Any], flagged: list[str]) -> str:
    lines = [f"Dourmouse speed check, {result.get('measured_at', '')}", ""]
    for key in ("app_boot_s", "spawn_to_ready_s", "server_ready_s", "first_screen_ready_s"):
        if key in result:
            lines.append(f"  {key:<24}{result[key]:>8} s")
    for screen, v in (result.get("screen_switch_ms") or {}).items():
        lines.append(f"  switch {screen:<17}cold {v.get('cold', '-'):>8} ms   warm {v.get('warm', '-'):>8} ms")
    mem = result.get("memory_mb") or {}
    if mem:
        lines.append(f"  server memory           {mem.get('server_rss_mb', '-'):>8} MB")
        lines.append(f"  app memory              {mem.get('app_rss_mb', '-'):>8} MB  ({(result.get('memory_detail') or {}).get('app_process_count', '?')} processes)")
    lines.append("")
    lines.append("over budget: " + (", ".join(flagged) if flagged else "none"))
    for note in result.get("notes", []):
        lines.append(f"note: {note}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--app", default=str(DEFAULT_APP), help="the app bundle to measure (default ~/Applications/Dourmouse.app)")
    ap.add_argument("--ui-port", type=int, default=18890)
    ap.add_argument("--cdp-port", type=int, default=19390)
    ap.add_argument("--pane-port", type=int, default=19391)
    ap.add_argument("--idle", type=float, default=60.0, help="seconds to wait before reading memory (default 60)")
    ap.add_argument("--json", action="store_true", help="print only the JSON result")
    ap.add_argument("--out", default="", help="also write the JSON result to this file")
    ap.add_argument("--check", action="store_true", help="exit 1 when a number is over the budget")
    ap.add_argument("--keep", action="store_true", help="keep the isolated data folder")
    args = ap.parse_args(argv)
    result = measure(Path(args.app).expanduser(), args.ui_port, args.cdp_port, args.pane_port, args.idle, keep_dir=args.keep)
    flagged = over_budget(result)
    result["budget"] = BUDGET
    result["over_budget"] = flagged
    if args.out:
        Path(args.out).expanduser().write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2) if args.json else render(result, flagged))
    return 1 if (args.check and flagged) else 0


if __name__ == "__main__":
    sys.exit(main())
