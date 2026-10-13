"""Backend for the privacy section of SETTINGS and the egress section of SECURITY (PRIV-4, PRIV-5).

Reads: ``GET /api/os/settings/privacy`` (consent per source, Private mode, the provider checklist,
and this chat's own Private mode when ``tab_id`` is given) and ``GET /api/os/privacy/egress`` (the
egress ledger: metadata only, never content).

Writes, all owner-only (request_guard.OWNER_ONLY_POST, and under the owner-only
``/api/os/settings/`` prefix as well): consent for one source, the global Private mode, one chat's
Private mode, and the owner's own "I checked this provider's retention and training settings" tick.
No tool can reach these: the model's tools never write ``privacy.json``.
"""

from __future__ import annotations

from typing import Any

from . import ApiError, Request, route

_LIMITS = (
    "Every model Dourmouse uses runs in the cloud, so whatever a model reads leaves this Mac. "
    "Card and bank numbers, ID numbers, phone numbers, street addresses and other people's email "
    "addresses are swapped for placeholders before sending; names and other free text are not. "
    "Never sent in any mode: password vault entries, Keychain items, .env files and keys, SSH keys, browser "
    "cookies and tokens, the owner secret. Not covered: what you type yourself, files you attach, and "
    "names or free text inside a result. Claude and Codex can read nothing on this Mac by themselves in "
    "chat mode; they only get what Dourmouse's own tools return. The requests listed in SECURITY are the "
    "ones Dourmouse recorded."
)


def _sources(store: Any) -> list[dict[str, Any]]:
    from dourmouse import privacy

    rows = []
    for sid, info in privacy.SOURCES.items():
        row = {"id": sid, "label": info["label"], "what": info["what"],
               "decision": store.consent(sid) or "ask"}
        if sid == "contacts":
            row["note"] = "No tool reads your contacts yet; this switch applies as soon as one does."
        rows.append(row)
    return rows


@route("GET", "/api/os/settings/privacy")
def summary(req: Request) -> tuple[int, dict[str, Any]]:
    from dourmouse import privacy

    store = privacy.PrivacyStore()
    tab_id = req.arg("tab_id").strip()
    payload: dict[str, Any] = {
        "ok": True,
        "limits": _LIMITS,
        "sources": _sources(store),
        "private_mode": store.private_mode(),
        "providers": privacy.provider_table(store),
    }
    if tab_id:
        payload["chat"] = {"tab_id": tab_id, "private": store.chat_private(tab_id),
                           "effective": store.private_for(tab_id)}
    note = store.note or str(store.snapshot().get("recovery_note") or "")
    if note:
        payload["note"] = note
    return 200, payload


def _bool_of(value: Any, what: str) -> bool:
    if not isinstance(value, bool):
        raise ApiError(400, f"{what} must be true or false")
    return value


@route("POST", "/api/os/settings/privacy/consent")
def set_consent(req: Request) -> tuple[int, dict[str, Any]]:
    """Body ``{source, decision: allow|deny|ask}``."""
    from dourmouse import privacy

    store = privacy.PrivacyStore()
    try:
        store.set_consent(str(req.body.get("source") or ""), str(req.body.get("decision") or ""))
    except ValueError as exc:
        raise ApiError(400, str(exc)) from exc
    return 200, {"ok": True, "sources": _sources(store)}


@route("POST", "/api/os/settings/privacy/private_mode")
def set_private_mode(req: Request) -> tuple[int, dict[str, Any]]:
    """Body ``{enabled}``: Private mode for every chat."""
    from dourmouse import privacy

    store = privacy.PrivacyStore()
    store.set_private_mode(_bool_of(req.body.get("enabled"), "enabled"))
    return 200, {"ok": True, "private_mode": store.private_mode()}


@route("POST", "/api/os/settings/privacy/chat")
def set_chat_private(req: Request) -> tuple[int, dict[str, Any]]:
    """Body ``{tab_id, enabled}``: Private mode for one chat (the chat toolbar)."""
    from dourmouse import privacy

    store = privacy.PrivacyStore()
    tab_id = str(req.body.get("tab_id") or "").strip()
    try:
        store.set_chat_private(tab_id, _bool_of(req.body.get("enabled"), "enabled"))
    except ValueError as exc:
        raise ApiError(400, str(exc)) from exc
    return 200, {"ok": True, "tab_id": tab_id, "private": store.chat_private(tab_id),
                 "effective": store.private_for(tab_id)}


@route("POST", "/api/os/settings/privacy/provider")
def set_provider(req: Request) -> tuple[int, dict[str, Any]]:
    """Body ``{id, verified}``: the owner's own check of one provider's
    retention and training settings (the code cannot verify them)."""
    from dourmouse import privacy

    store = privacy.PrivacyStore()
    try:
        store.set_provider_verified(str(req.body.get("id") or ""), _bool_of(req.body.get("verified"), "verified"))
    except ValueError as exc:
        raise ApiError(400, str(exc)) from exc
    return 200, {"ok": True, "providers": privacy.provider_table(store)}


@route("GET", "/api/os/privacy/egress")
def egress(req: Request) -> tuple[int, dict[str, Any]]:
    """The newest ledger rows (``limit``, at most 500) and a short summary."""
    from dourmouse import privacy

    try:
        limit = max(1, min(int(req.arg("limit", "100") or 100), 500))
    except ValueError as exc:
        raise ApiError(400, "limit must be a number") from exc
    rows = privacy.read_egress(limit)
    by_provider: dict[str, int] = {}
    t2 = 0
    for r in rows:
        by_provider[r.get("provider") or "unknown"] = by_provider.get(r.get("provider") or "unknown", 0) + 1
        if (r.get("tiers") or {}).get("T2"):
            t2 += 1
    return 200, {
        "ok": True,
        "rows": rows,
        "summary": {"requests": len(rows), "by_provider": by_provider, "with_sensitive": t2,
                    "write_failures": privacy.egress_failures(), "chain": privacy.egress_chain_status()},
        "path": str(privacy.egress_path()),
    }
