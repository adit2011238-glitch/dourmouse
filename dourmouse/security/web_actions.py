"""The console's security actions (finding #109): one POST endpoint,
/api/security/action, behind the server's normal auth. A click in the
console is the owner acting directly (the page asks "are you sure?" before
every action that changes the machine), the same way the chat's approval
gate is the owner acting. Every action returns {"ok": ..., ...}; a refusal
is ok=false with the reason, never an exception.
"""

from __future__ import annotations

import time
from typing import Any


def _str(body: dict[str, Any], key: str) -> str:
    v = body.get(key)
    if not isinstance(v, str) or not v.strip():
        raise ValueError(f"{key} is required")
    return v.strip()


def handle_action(body: dict[str, Any]) -> dict[str, Any]:
    from . import lockdown as ld
    from . import response as rs

    action = body.get("action")
    try:
        if action == "lockdown_add":
            bl = ld.Blocklist.load()
            kind = body.get("kind")
            entry = _str(body, "entry")
            row = bl.add_site(entry) if kind == "site" else bl.add_app(entry) if kind == "app" else None
            if row is None:
                raise ValueError("kind must be 'site' or 'app'")
            bl.save()
            if bl.active:
                ld.write_hosts_request(bl)
            return {"ok": True, "added": row, "lockdown": ld.status(bl, check_sites=False)}
        if action == "lockdown_remove":
            bl = ld.Blocklist.load()
            removed = bl.remove(_str(body, "entry"))
            bl.save()
            if bl.active:
                ld.write_hosts_request(bl)
            return {"ok": removed, "lockdown": ld.status(bl, check_sites=False),
                    **({} if removed else {"error": "not on the blocklist"})}
        if action == "lockdown_start":
            return {"ok": True, "lockdown": ld.start()}
        if action == "lockdown_stop":
            return {"ok": True, "lockdown": ld.stop()}
        if action == "block_domain":
            return {"ok": True, "lockdown": ld.block_domain_always(_str(body, "domain"), str(body.get("reason") or ""))}
        if action == "unblock_domain":
            return {"ok": True, "lockdown": ld.unblock_domain_always(_str(body, "domain"))}
        if action == "kill_process":
            pid = body.get("pid")
            if not isinstance(pid, int) or isinstance(pid, bool):
                raise ValueError("pid must be a number")
            return rs.kill_process(pid, expect_name=body.get("expect_name") or None)
        if action == "quarantine":
            return rs.quarantine_file(_str(body, "path"), reason=str(body.get("reason") or ""))
        if action == "restore":
            return rs.restore(_str(body, "id"))
        if action == "disable_startup_item":
            return rs.disable_startup_item(_str(body, "path"), reason=str(body.get("reason") or ""))
        if action == "report":
            from .report import build_report, save_report

            r = build_report()
            return {"ok": True, "report": r, "saved_to": str(save_report(r))}
        if action == "analyze":
            from .analyst import analyze
            from .sentry import run_scan

            scan = run_scan(write_alerts=False)
            findings = [{"kind": f.kind, "severity": f.severity, "title": f.title, "detail": f.detail,
                         "recommended_action": f.recommended_action} for f in scan.all_findings]
            analysis = {**analyze(findings), "at": time.time()}
            return {"ok": bool(analysis.get("ok")), "analysis": analysis,
                    **({} if analysis.get("ok") else {"error": analysis.get("error", "")})}
        if action == "self_audit":
            from .self_audit import run_self_audit

            return {"ok": True, **run_self_audit()}
        if action == "privacy_mode":
            from .privacy import set_privacy_mode

            return {"ok": True, **set_privacy_mode(bool(body.get("on")))}
        if action == "browser_history":
            from . import browser_history as bh

            hours = body.get("hours", 24)
            if not isinstance(hours, (int, float)) or isinstance(hours, bool) or not 0 < hours <= 24 * 90:
                raise ValueError("hours must be a number between 0 and 2160")
            r = bh.recent_history(hours=float(hours))
            bl = ld.Blocklist.load()
            blocked = {s["domain"] for s in bl.always} | ({s["domain"] for s in bl.sites} if bl.active else set())
            # Local display only: the console runs on this Mac, nothing here goes to a model.
            return {"ok": True, "visits": len(r["visits"]), "top_domains": bh.top_domains(r["visits"]),
                    "searches": r["searches"][:30], "blocked_visits": bh.blocked_visits(r["visits"], blocked)[:50],
                    "sources": r["sources"]}
        if action == "diagnose":
            from .connectivity import diagnose

            return {"ok": True, **diagnose(_str(body, "target"))}
    except (ValueError, rs.ResponseRefused) as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": False, "error": f"unknown action {action!r}"}
