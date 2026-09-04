"""ui/console.html — "send it"/"confirm" typed while the turn that opened the
approval box is still streaming must resolve it directly, not queue behind
it (v13.8, real live-reproduced bug).

Live-reproduced during the Google Workspace email-send test: a "SEND THIS
EMAIL?" card appeared (a real gmail_send tool call, confirmation genuinely
pending server-side), but gpt-oss:20b kept narrating past it for 170+
seconds. Typing "send it" / "CONFIRM" into the composer while that was still
happening got pushed into queueFor(key) (submit()'s busy branch) instead of
being sent -- so it sat inert until the still-running turn finally finished,
at which point it was replayed as a BRAND NEW chat message with no pending
confirmation left to resolve, producing a zero-tool-call "I'm sorry, but I
can't send emails." answer instead of ever actually sending.

The server has ALWAYS supported exactly this: webui.py's
_is_imperative_affirm intercept runs on a **separate HTTP request**, before
`with self.server.session_lock:`, specifically so it is NOT blocked behind
the in-flight turn (see that block's own comment: "the thread that's
actually waiting on the confirmation is blocked holding session_lock ...
taking the lock here would deadlock"). The bug was entirely client-side --
the request never even reached the server, because submit() queued it.

Fix: track the single open approval box per screen (pendingApprovalByScreen,
set by addApproval / cleared by its own decide()); submit()'s busy branch now
checks for an exact imperative-affirm phrase match (mirroring webui.py's
_IMPERATIVE_AFFIRM_PHRASES/_normalize_affirm_text verbatim) and, if the
screen has exactly one open box, resolves it directly via that box's own
decide(true) -- the same POST /api/confirm path a manual click already used
-- instead of queuing.
"""

from __future__ import annotations

import re
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CONSOLE_HTML = _PROJECT_ROOT / "ui" / "console.html"

# Kept in sync with dourmouse/webui.py's _IMPERATIVE_AFFIRM_PHRASES verbatim
# -- if that set changes, this test (and the client-side mirror it checks
# for) must change with it.
_SERVER_PHRASES = {
    "yes", "y", "yeah", "yep", "confirm", "confirmed", "approve", "approved",
    "send", "send it", "go", "go ahead", "do it", "do it now", "send now",
    "yes send", "yes send it", "yes go ahead", "yes do it", "ok send it",
    "ok go ahead", "okay send it", "confirm it",
}


def _extract_inline_script() -> str:
    html = _CONSOLE_HTML.read_text(encoding="utf-8")
    m = re.search(r"<script>(.*?)</script>", html, re.S)
    assert m, "ui/console.html has no inline <script>...</script> block"
    return m.group(1)


class TestServerPhraseSetIsUnchanged:
    """Sanity check on this test file's own fixture -- if webui.py's set
    ever drifts, this fails loudly instead of the client silently going
    stale against it."""

    def test_matches_webui_py(self):
        webui = (_PROJECT_ROOT / "dourmouse" / "webui.py").read_text(encoding="utf-8")
        m = re.search(r"_IMPERATIVE_AFFIRM_PHRASES = frozenset\(\s*\{(.*?)\}\s*\)", webui, re.S)
        assert m, "_IMPERATIVE_AFFIRM_PHRASES not found in webui.py"
        phrases = set(re.findall(r'"([^"]+)"', m.group(1)))
        assert phrases == _SERVER_PHRASES


class TestClientMirrorsThePhraseSet:
    def test_all_server_phrases_present_in_client_set(self):
        script = _extract_inline_script()
        m = re.search(r"const IMPERATIVE_AFFIRM_PHRASES = new Set\(\[(.*?)\]\);", script, re.S)
        assert m, "IMPERATIVE_AFFIRM_PHRASES not found in console.html"
        client_phrases = set(re.findall(r'"([^"]+)"', m.group(1)))
        assert client_phrases == _SERVER_PHRASES

    def test_isImperativeAffirm_normalizes_like_the_server(self):
        script = _extract_inline_script()
        assert "function isImperativeAffirm(text){" in script
        # Same normalization as _normalize_affirm_text: lower, strip
        # trailing punctuation/whitespace, collapse internal whitespace.
        assert 'toLowerCase().replace(/[!.\\s]+$/,"")' in script


class TestPendingApprovalTrackedPerScreen:
    def test_declared_alongside_the_other_per_screen_maps(self):
        script = _extract_inline_script()
        assert "const pendingApprovalByScreen = {};" in script

    def test_addApproval_registers_it_when_given_a_screen_key(self):
        script = _extract_inline_script()
        m = re.search(r"function addApproval\(node, evt, declineMap, screenKey\)\{(.*?)\n\}\n", script, re.S)
        assert m, "addApproval with the new screenKey param not found"
        body = m.group(1)
        assert "pendingApprovalByScreen[screenKey] = {id: evt.id, decide};" in body

    def test_decide_clears_it_only_if_still_the_same_box(self):
        script = _extract_inline_script()
        m = re.search(r"function addApproval\(node, evt, declineMap, screenKey\)\{(.*?)\n\}\n", script, re.S)
        assert m
        body = m.group(1)
        assert "pendingApprovalByScreen[screenKey] && pendingApprovalByScreen[screenKey].id===evt.id" in body
        assert "delete pendingApprovalByScreen[screenKey];" in body

    def test_call_site_passes_the_targets_own_screen(self):
        script = _extract_inline_script()
        assert 'addApproval(node,e,myDeclines,targetScreen)' in script


class TestSubmitBypassesTheQueueForAnOpenApproval:
    """The actual fix: submit()'s busy branch must check for a pending
    approval + affirm phrase BEFORE falling through to queueFor(key).push,
    or this whole feature is dead code."""

    def test_busy_branch_checks_pending_approval_before_queuing(self):
        script = _extract_inline_script()
        m = re.search(r"async function submit\(\)\{(.*?)\n\}\n", script, re.S)
        assert m, "submit() not found"
        body = m.group(1)
        busy_idx = body.index("if(busyByScreen[key]){")
        pending_idx = body.index("pendingApprovalByScreen[key]")
        queue_push_idx = body.index("queueFor(key).push(text)")
        assert busy_idx < pending_idx < queue_push_idx, (
            "the pending-approval check must run inside the busy branch, "
            "before the queue push"
        )

    def test_resolves_via_decide_true_not_a_fresh_run(self):
        script = _extract_inline_script()
        m = re.search(r"async function submit\(\)\{(.*?)\n\}\n", script, re.S)
        assert m
        body = m.group(1)
        assert "pending.decide(true)" in body
        # Must return immediately after resolving -- never fall through to
        # queueFor(key).push for the same reply.
        idx = body.index("pending.decide(true);")
        tail = body[idx:idx + 40]
        assert "return;" in tail
