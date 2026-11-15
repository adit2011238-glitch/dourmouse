"""The privacy check before any passage leaves this Mac to be embedded (plan 2.1; finding 206).

Stage 2.1 builds the gate and the queue; the Gemini embedder that calls it is plan 2.3. The rules:

* A local embedder (``remote = False``) sends nothing anywhere, so it is always allowed.
* A remote embedder never sees: anything while Private mode is on; passages of tier T2 or T3; passages that hold a
  secret (a key, token, password or ``.env`` line); passages from a folder that has no ``allow`` consent record
  (no record means ask, so nothing is sent), or whose nearest record is ``deny``.
* A passage with no folder (a remembered fact, a saved page, a paper card) is not folder-gated; the tier, secret and
  Private mode rules still apply.
"""

from __future__ import annotations

import re
from typing import Any

#: A line that looks like a credential: ``NAME_KEY=...``, a bearer token, a private key block or a long hex/base64 key.
_SECRET_LINE = re.compile(
    r"(?im)(?:^[ \t]*(?:export[ \t]+)?[A-Za-z][A-Za-z0-9_]*(?:KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL)[A-Za-z0-9_]*"
    r"[ \t]*[=:][ \t]*['\"]?[^\s'\"]{6,})"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|\bAKIA[0-9A-Z]{16}\b|\bsk-[A-Za-z0-9_-]{20,}|\bghp_[A-Za-z0-9]{30,}|\bAIza[0-9A-Za-z_-]{30,}"
    r"|\bBearer[ \t]+[A-Za-z0-9._~+/=-]{20,}"
)


def holds_secret(text: str) -> bool:
    return bool(_SECRET_LINE.search(text or ""))


def may_embed(passage: dict[str, Any], *, remote: bool, privacy_store: Any = None) -> tuple[bool, str]:
    """(allowed, reason). ``passage`` needs ``text``, ``tier`` and ``folder``. The reason is plain words for the
    Library screen, never the text."""
    if not remote:
        return True, ""
    from dourmouse import privacy

    store = privacy_store or privacy.PrivacyStore()
    if store.private_mode():
        return False, "Private mode is on"
    if str(passage.get("tier") or "T1") in ("T2", "T3"):
        return False, "marked sensitive"
    if holds_secret(str(passage.get("text") or "")):
        return False, "looks like it holds a key or password"
    folder = str(passage.get("folder") or "")
    if folder:
        decision = store.folder_consent_for(folder)
        if decision == "deny":
            return False, "you denied this folder"
        if decision != "allow":
            return False, "this folder has no consent yet"
    return True, ""
