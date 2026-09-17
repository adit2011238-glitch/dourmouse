"""Google AI Studio (Gemini) LLM backend — real REST + SSE, stdlib only.

The third real LLM backend alongside the two that already exist:

- ``dispatch.py`` — Ollama / NVIDIA NIM / OmniRoute over the
  OpenAI-compatible ``openai`` client.
- ``code_backends.py`` — the user's real Claude Code CLI (``claude -p``),
  a subprocess whose ``stream-json`` events are parsed live.
- **this module** — Google AI Studio's own REST API
  (``generativelanguage.googleapis.com``), streamed as Server-Sent Events.

Same non-negotiable contract as both of those (Rules 2.1 / 2.2): every
entry point returns a REAL result or an honest error. A missing key is
``NOT CONFIGURED: ...``; an HTTP failure surfaces the provider's own
status line and response body verbatim; a safety block or an empty
candidate surfaces the real ``finishReason``/``blockReason``. Nothing here
ever fabricates output, silently substitutes another backend, or swallows
a refusal into an empty string.

**Nothing in this module has been exercised against the live API.** As of
2026-09-04 this machine has no ``GEMINI_API_KEY`` and no
``GOOGLE_AI_STUDIO_KEY`` — confirmed by reading the project ``.env`` (98
lines, no Gemini entry) and the process environment. So ``gemini_configured()``
is False here today and every path below degrades to the honest
NOT CONFIGURED error. The request/response shapes, the endpoint, the auth
header and the model id were taken from Google's own current published
REST documentation (see the citations on each constant), NOT from a live
round-trip. The first real call on a machine that HAS a key is still an
unverified step; do not read the passing test suite as proof of the wire
format.

-- Why stdlib urllib and not requests / google-generativeai ---------------
Checked before writing a line of this (2026-09-04): ``requirements.txt``
pins neither. ``google-generativeai`` is not installed in ``.venv`` at all
(``ModuleNotFoundError``), and ``requests`` is present only as somebody
else's transitive dependency — nothing in this repo declares it, so
building a REQUIRED code path on it would be depending on an accident of
the current lockfile. ``world_pulse.py`` already established the house
answer for exactly this situation: real HTTP over stdlib
``urllib.request`` with honest ``RuntimeError``s, and a single
module-level function as the seam tests monkeypatch instead of reaching
into ``urllib`` (see its ``_http_get`` / ``_http_post``). This follows
that precedent, so Gemini support adds ZERO new dependencies.

-- Injectable transport ---------------------------------------------------
``stream_gemini`` / ``call_gemini`` take a keyword-only ``transport``
which defaults to ``_urllib_transport``. A transport is any callable

    transport(url, *, data: bytes, headers: dict[str, str], timeout: float)
        -> Iterable[str]      # decoded response lines, newline stripped

so a test supplies a list of literal SSE lines and NO test ever touches
the network (Rule 2.1 hermetic). The real transport is the only place
``urllib`` appears.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Callable, Iterable, Iterator

from dourmouse.config import (
    GEMINI_DEFAULT_BASE_URL,
    GEMINI_DEFAULT_MODEL,
    GEMINI_ENV_KEYS,
    load_gemini_config,
)

# Re-exported so callers can ``from dourmouse.gemini_backend import
# GEMINI_DEFAULT_MODEL`` without also importing config — the same
# public-alias convention config.py already uses for the NVIDIA/Ollama/
# OmniRoute defaults (NVIDIA_DEFAULT_MODEL et al). config.py stays the
# single source of config truth (Integration Rule 7); this module never
# defines a second, drifting copy of the model id.
__all__ = [
    "GEMINI_DEFAULT_BASE_URL",
    "GEMINI_DEFAULT_MODEL",
    "GEMINI_ENV_KEYS",
    "call_gemini",
    "gemini_configured",
    "gemini_status",
    "stream_gemini",
]

#: Honest, non-spoofed User-Agent. Deliberately NOT ``live_feeds._UA``:
#: that string pretends to be Chrome because the feeds it scrapes are
#: public web pages that block obvious bots. This is a first-party API
#: call against a key-authenticated endpoint — there is nothing to work
#: around and every reason for Google's logs to say what actually called.
_UA = "dourmouse/4.0.0 (+https://github.com/; stdlib-urllib)"

#: Default wall-clock budget for one streamed generation, in seconds.
#: 300s matches the order of magnitude ``code_backends`` already allows a
#: single CLI coding run (it clamps to 600) — a long reasoning answer from
#: a Flash-class model with a 65k output ceiling can genuinely run minutes,
#: and a short timeout here would present as a truncated reply, which is
#: precisely the failure mode this repo has already paid for three times
#: (see ``_MAX_OUTPUT_TOKENS_CEILING`` below).
_DEFAULT_TIMEOUT = 300.0
#: Same clamp range ``code_backends.stream_claude`` uses for its timeout.
_MIN_TIMEOUT = 1.0
_MAX_TIMEOUT = 600.0

#: How much of a failing response body is quoted back in the RuntimeError.
#: Gemini's error envelope is small JSON ({"error": {"code", "message",
#: "status"}}), so 2000 chars carries the whole real message; the cap only
#: exists so an HTML error page from an intercepting proxy cannot dump a
#: whole document into a log line. The body is quoted VERBATIM up to that
#: point — never summarized, never replaced with a friendlier string.
_ERROR_BODY_CAP = 2000

#: Hard ceiling used ONLY to clamp an explicitly-passed ``max_tokens``.
#:
#: Read this together with dispatch.py's ``_DEFAULT_MAX_TOKENS`` comment,
#: which documents in detail how a "reasonable-looking" response cap has
#: bitten this codebase three separate times in three separate modules
#: (the v8.10 brevity fix, personality_profile.py, call_nvidia) — always
#: the same way: the model spends the budget on REASONING before it emits
#: content, so a tight cap does not shorten a reply, it truncates one.
#:
#: That risk is HIGHER here, not lower. Gemini 3.x models think by default
#: and their thought tokens are billed against the same output budget
#: (they arrive as ``parts[].thought == true`` — see ``_split_parts``), so
#: any cap sized against the visible answer is wrong by the size of the
#: reasoning.
#:
#: The design consequence: when ``max_tokens`` is None (the default and the
#: overwhelmingly common case) this module sends NO ``maxOutputTokens`` at
#: all, so the model applies its own full ceiling. There is deliberately no
#: house default number to get wrong. The value below is used only to keep
#: a caller-supplied number inside the model's real limit.
#:
#: 65,536 is gemini-3.5-flash's real documented output token limit
#: (ai.google.dev model page, read 2026-09-04: input 1,048,576 / output
#: 65,536). Input context is 1M tokens, i.e. ~32x dispatch's entire 32,768
#: Ollama window — so nothing in this module needs a context-trimming
#: strategy of its own.
_MAX_OUTPUT_TOKENS_CEILING = 65_536
#: Floor for an explicit cap, matching ``dispatch._default_max_tokens``'s
#: own floor and for the same stated reason: below 256 the cap cannot fit
#: even a leaked-reasoning preamble, which is the exact failure it exists
#: to prevent.
_MIN_OUTPUT_TOKENS = 256

#: A transport is a callable returning decoded, newline-stripped response
#: lines. See the module docstring for the exact signature and why this
#: seam exists.
GeminiTransport = Callable[..., Iterable[str]]


# --------------------------------------------------------------------------- #
# Configuration / honest status
# --------------------------------------------------------------------------- #

def _api_key() -> str:
    """The real Gemini key from env, or "" — never a guess, never cached.

    Read at CALL time rather than import time on purpose: ``config.py``
    populates ``os.environ`` from .env at import, but a key added later
    (firstrun wizard, a test's monkeypatch, an operator exporting one into
    a running shell before restart) must be picked up without a code
    change. This is the same read-fresh discipline
    ``config.orchestrator_model_setting`` documents for its own setting.
    """
    return load_gemini_config().api_key


def _key_source() -> str:
    """Which env var actually supplied the key ("" when none did).

    Returned in status text so an operator can tell WHICH of the two names
    is live. The key VALUE is never returned by anything in this module —
    same rule connections.py states for every other credential probe.
    """
    for name in GEMINI_ENV_KEYS:
        if os.environ.get(name, "").strip():
            return name
    return ""


def gemini_configured() -> bool:
    """True only when a real Gemini API key is present in the environment.

    Presence of a key is NOT a claim that the key is valid, in quota, or
    that the endpoint is reachable — none of which can be established
    without spending a real request. This is deliberately the same honesty
    boundary ``connections.py`` draws for NVIDIA ("NVIDIA_API_KEY present",
    not "NVIDIA works"). On this machine today it returns False.
    """
    return bool(_api_key())


def gemini_status() -> dict[str, Any]:
    """``{"ok": bool, "detail": str, "hint": str}`` — the exact shape every
    probe in ``connections.py`` returns, so this can be dropped into that
    report without translation. Never raises, never returns the key.
    """
    source = _key_source()
    if not source:
        return {
            "ok": False,
            "detail": (
                "GEMINI_API_KEY MISSING (GOOGLE_AI_STUDIO_KEY also unset)"
            ),
            "hint": (
                "add GEMINI_API_KEY to .env — a free key is issued at "
                "https://aistudio.google.com/apikey"
            ),
        }
    cfg = load_gemini_config()
    return {
        "ok": True,
        "detail": f"{source} present · model {cfg.model}",
        # Still a hint, not a boast: a present key has not been proven to
        # work. Says exactly what would prove it.
        "hint": (
            "key present but unverified — the first real call reports the "
            "provider's own error if it is invalid or out of quota"
        ),
    }


def _require_key() -> str:
    """The real key, or raise the honest NOT CONFIGURED error (Rule 2.2).

    Wording mirrors ``code_backends.stream_claude``'s missing-CLI error:
    what is missing, how to fix it, and an explicit statement that NOTHING
    was run — so a caller can never mistake this for a failed attempt.
    """
    key = _api_key()
    if not key:
        raise RuntimeError(
            "NOT CONFIGURED: no Gemini API key. Set GEMINI_API_KEY (or "
            "GOOGLE_AI_STUDIO_KEY) in .env — a free key is issued at "
            "https://aistudio.google.com/apikey. No request was sent."
        )
    return key


# --------------------------------------------------------------------------- #
# Request construction
# --------------------------------------------------------------------------- #

def _resolve_model(model: str | None) -> str:
    """Explicit argument wins, then GEMINI_MODEL/env config, then the
    module default. Deterministic (Rule 2.8), no probing, no guessing."""
    name = (model or "").strip()
    return name or load_gemini_config().model or GEMINI_DEFAULT_MODEL


def _endpoint(model: str, base_url: str | None = None) -> str:
    """The real streaming REST URL for ``model``.

    ``:streamGenerateContent`` with ``alt=sse`` is the documented way to
    get incremental Server-Sent Events rather than one buffered JSON array
    (ai.google.dev/api/generate-content, read 2026-09-04). Without
    ``alt=sse`` the same path streams a JSON ARRAY, which cannot be parsed
    line-by-line — so the query parameter is load-bearing, not cosmetic.

    NOTE on the API's own status: Google's current docs file the
    generateContent family under "Gemini Generate Content API (Legacy)"
    and point new work at a newer Interactions API. It is documented and
    serving, not deprecated-with-a-date, and it is the endpoint this task
    specifies — recorded here so whoever revisits this knows the label
    exists and was seen, rather than rediscovering it as a surprise.

    The API KEY is NOT put in this URL. Google documents a ``?key=``
    query parameter and an ``x-goog-api-key`` header as equivalent; the
    header is used (see ``_headers``) because a URL travels into proxy
    access logs, exception messages and this module's own error strings,
    and a secret must not ride along in any of them.
    """
    base = (base_url or GEMINI_DEFAULT_BASE_URL).rstrip("/")
    return f"{base}/models/{model}:streamGenerateContent?alt=sse"


def _headers(key: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "User-Agent": _UA,
        "x-goog-api-key": key,
    }


def _clamp_max_tokens(max_tokens: int | None) -> int | None:
    """None stays None — see ``_MAX_OUTPUT_TOKENS_CEILING`` for why the
    no-cap default is the whole point. A supplied value is clamped into
    the model's real documented range instead of being sent through to be
    rejected (or, worse, silently honoured at a truncating size)."""
    if max_tokens is None:
        return None
    try:
        value = int(max_tokens)
    except (TypeError, ValueError):
        return None
    return max(_MIN_OUTPUT_TOKENS, min(value, _MAX_OUTPUT_TOKENS_CEILING))


def _build_body(
    prompt: str,
    *,
    system: str | None,
    max_tokens: int | None,
) -> dict[str, Any]:
    """The real ``GenerateContentRequest`` body.

    Shape per ai.google.dev/api/generate-content (read 2026-09-04):
    ``contents`` is required and is a list of turns each carrying
    ``role`` + ``parts``; ``systemInstruction`` is a separate top-level
    field (NOT a "system"-role turn — Gemini has no such role, unlike the
    OpenAI-compatible backends in dispatch.py, and sending one would be
    rejected); ``generationConfig`` carries generation knobs.

    ``generationConfig`` is omitted entirely when there is nothing to put
    in it, so the request cannot accidentally pin a default this module
    never intended to choose.
    """
    body: dict[str, Any] = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}]
    }
    if system and system.strip():
        body["systemInstruction"] = {"parts": [{"text": system}]}
    capped = _clamp_max_tokens(max_tokens)
    if capped is not None:
        body["generationConfig"] = {"maxOutputTokens": capped}
    return body


# --------------------------------------------------------------------------- #
# Transport (the ONLY place urllib appears — the seam tests replace)
# --------------------------------------------------------------------------- #

def _urllib_transport(
    url: str,
    *,
    data: bytes,
    headers: dict[str, str],
    timeout: float,
) -> Iterator[str]:
    """Real streamed POST over stdlib urllib, yielding decoded lines.

    Same honest-error contract as ``world_pulse._http_post``: an HTTP
    status error, a network error and a timeout each raise a RuntimeError
    naming what actually happened. The HTTPError branch additionally reads
    and quotes the response BODY, because Google puts the useful part of
    the failure there (``{"error": {"message": "API key not valid. ..."}}``)
    and reporting only "HTTP 400" would be throwing away the real answer.

    Because this is a generator the request is not sent until the first
    line is pulled — so those errors surface at iteration time, which is
    where ``_stream_events`` handles them.
    """
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 - fixed https API host
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = (exc.read() or b"").decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - the status line is still real news
            body = ""
        raise RuntimeError(
            f"Gemini API HTTP {exc.code} {exc.reason}: "
            f"{body[:_ERROR_BODY_CAP].strip() or '(empty response body)'}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"network error calling Gemini: {exc.reason}") from exc
    except TimeoutError as exc:
        raise RuntimeError(f"timeout calling Gemini after {timeout}s: {exc}") from exc
    with resp:
        for raw in resp:
            yield raw.decode("utf-8", errors="replace").rstrip("\r\n")


# --------------------------------------------------------------------------- #
# SSE / response parsing
# --------------------------------------------------------------------------- #

def _iter_sse_payloads(lines: Iterable[str]) -> Iterator[dict[str, Any]]:
    """Decode an ``alt=sse`` byte stream into JSON chunk objects.

    SSE framing: each event is one or more ``field: value`` lines followed
    by a blank line. Gemini only ever sends ``data:`` fields, one complete
    JSON object per event, so no multi-line data accumulation is needed —
    but ``event:``/``id:``/``:``-comment lines and blank keep-alives are
    tolerated and skipped rather than treated as corruption.

    A ``data:`` line that is not valid JSON is skipped, matching how
    ``code_backends.stream_claude`` skips an unparseable stream-json line:
    one malformed frame must not abort a generation that is otherwise
    producing real text. A stream that produces NO usable payload at all
    is caught by the caller, which raises rather than returning "".
    """
    for line in lines:
        if not line or line.startswith(":"):
            continue
        field, sep, value = line.partition(":")
        if not sep or field.strip() != "data":
            continue
        value = value.strip()
        if not value or value == "[DONE]":
            continue
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            yield payload


def _split_parts(payload: dict[str, Any]) -> tuple[list[str], list[str]]:
    """``(answer_texts, thought_texts)`` from one streamed chunk.

    Gemini 3.x returns thought summaries in the SAME ``parts`` array as the
    answer, distinguished only by ``"thought": true`` on the part. Routing
    those to ``on_thinking`` rather than ``on_delta`` is what keeps
    reasoning out of the reply — the exact leak that produced the v8.10
    brevity bug and the personality_profile bug on the other backends,
    where visible chain-of-thought ended up rendered AS the answer.
    """
    answers: list[str] = []
    thoughts: list[str] = []
    for candidate in payload.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("content")
        if not isinstance(content, dict):
            continue
        for part in content.get("parts") or []:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if not isinstance(text, str) or not text:
                continue
            if part.get("thought") is True:
                thoughts.append(text)
            else:
                answers.append(text)
    return answers, thoughts


def _extract_usage(payload: dict[str, Any]) -> dict[str, int]:
    """``usageMetadata`` -> the dict shape ``usage_tracker`` already takes.

    NOT an invented shape. ``usage_tracker.record_ollama_usage`` reads
    exactly ``prompt_tokens`` / ``completion_tokens``, and
    ``dispatch._usage_of`` (the extraction every OpenAI-compatible backend
    already funnels through) produces ``prompt_tokens`` /
    ``completion_tokens`` / ``total_tokens``. Gemini's own field names are
    ``promptTokenCount`` / ``candidatesTokenCount`` / ``totalTokenCount``
    (ai.google.dev/api/generate-content), so this is a rename, nothing
    more.

    Deliberately no ``cost_usd``: that is ``record_claude_usage``'s field
    and it is real there only because the Claude CLI reports a real
    ``total_cost_usd``. Gemini's REST response carries no price, and
    multiplying tokens by a rate looked up from a docs page would be a
    fabricated number (Rule 2.2). Tokens are real; the dollar figure is
    simply not available, and this reports nothing rather than a guess.

    Missing fields are OMITTED, never zero-filled — same rule
    ``stream_claude``'s usage extraction states for the Claude path.
    """
    meta = payload.get("usageMetadata")
    if not isinstance(meta, dict):
        return {}
    mapping = (
        ("promptTokenCount", "prompt_tokens"),
        ("candidatesTokenCount", "completion_tokens"),
        ("totalTokenCount", "total_tokens"),
    )
    out: dict[str, int] = {}
    for source, dest in mapping:
        value = meta.get(source)
        if isinstance(value, int) and not isinstance(value, bool):
            out[dest] = value
    return out


def _raise_if_error(payload: dict[str, Any]) -> None:
    """Surface an error envelope that arrived INSIDE the stream.

    Most failures are HTTP status errors caught in the transport, but a
    fault raised after the response headers are already on the wire can
    only arrive as a ``data: {"error": ...}`` frame. Reported verbatim —
    never downgraded into an empty result.
    """
    error = payload.get("error")
    if not isinstance(error, dict):
        return
    code = error.get("code")
    status = error.get("status")
    message = error.get("message")
    label = " ".join(str(x) for x in (code, status) if x)
    raise RuntimeError(
        f"Gemini API error{(' ' + label) if label else ''}: "
        f"{message if message else json.dumps(error)[:_ERROR_BODY_CAP]}"
    )


def _block_reason(payload: dict[str, Any]) -> str:
    """The real ``promptFeedback.blockReason``, or "".

    A prompt refused before generation starts produces this and no
    candidates at all. Reporting it is the difference between "the model
    declined, here is why" and a silent empty reply (Rule 2.2).
    """
    feedback = payload.get("promptFeedback")
    if not isinstance(feedback, dict):
        return ""
    reason = feedback.get("blockReason")
    return str(reason) if reason else ""


def _finish_reason(payload: dict[str, Any]) -> str:
    for candidate in payload.get("candidates") or []:
        if isinstance(candidate, dict) and candidate.get("finishReason"):
            return str(candidate["finishReason"])
    return ""


#: ``finishReason`` values that mean the generation completed normally.
#: Anything else (SAFETY, RECITATION, MAX_TOKENS, ...) is a real event the
#: caller is told about rather than left to infer from a short answer.
_CLEAN_FINISH = {"STOP", "FINISH_REASON_STOP", ""}


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def stream_gemini(
    prompt: str,
    *,
    model: str | None = None,
    system: str | None = None,
    on_delta: Callable[[str], None],
    on_thinking: Callable[[str], None] | None = None,
    on_usage: Callable[[dict[str, int]], None] | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
    base_url: str | None = None,
    transport: GeminiTransport | None = None,
) -> str:
    """Stream one real Gemini generation live; return the full answer text.

    ``on_delta(text)`` fires for every answer fragment as it arrives.
    ``on_thinking(text)`` fires for thought-summary fragments
    (``parts[].thought``) when supplied — these are NEVER passed to
    ``on_delta``. ``on_usage(usage)`` fires AT MOST ONCE, after the stream
    ends, with the real token counts (see below).

    Returns the concatenated answer text. It is deliberately NOT truncated
    on the way out — ``code_backends`` tail-cuts its CLI output at 20,000
    chars for context-budget reasons, but the deltas here have already been
    delivered to the caller in full, so trimming the return value would
    only make the returned string disagree with what the user watched
    arrive. Callers that need a context-bounded copy already have
    ``dispatch._max_tool_result_chars`` for that.

    Honest failure (Rule 2.2), in every case naming what really happened:
      * no key -> ``NOT CONFIGURED: ...`` and no request is sent;
      * HTTP/network/timeout -> the provider's own status and body verbatim;
      * an in-stream ``{"error": ...}`` frame -> raised verbatim;
      * a prompt blocked before generation -> the real ``blockReason``;
      * a stream that yields no text at all -> raised, never "".

    A generation that DID produce text but stopped for a non-STOP reason
    (SAFETY, RECITATION, MAX_TOKENS) returns that text with an explicit
    ``[gemini: stopped early — finishReason=X]`` marker appended AND
    streamed through ``on_delta``. Deliberate: silently returning a
    truncated answer as if it were complete is the single failure mode this
    repo has paid for most often (see dispatch._DEFAULT_MAX_TOKENS), and
    raising would throw away real work the user already watched arrive.
    Making the truncation visible is the only option that does neither.

    ``on_usage`` fires ONCE, not per chunk, and carries the LAST
    ``usageMetadata`` seen. Gemini repeats a growing usageMetadata on
    successive chunks, so the last one is the complete accounting for the
    call — and ``usage_tracker``'s recorders INCREMENT a persisted running
    total, so calling back per chunk would inflate the user's usage bar by
    the number of frames. Fired outside the parse loop for that reason.
    """
    key = _require_key()
    resolved_model = _resolve_model(model)
    send = transport or _urllib_transport
    timeout = max(_MIN_TIMEOUT, min(float(timeout), _MAX_TIMEOUT))
    body = _build_body(prompt, system=system, max_tokens=None)

    lines = send(
        _endpoint(resolved_model, base_url),
        data=json.dumps(body).encode("utf-8"),
        headers=_headers(key),
        timeout=timeout,
    )

    chunks: list[str] = []
    last_usage: dict[str, int] = {}
    finish = ""
    blocked = ""
    saw_payload = False

    for payload in _iter_sse_payloads(lines):
        saw_payload = True
        _raise_if_error(payload)
        blocked = _block_reason(payload) or blocked
        answers, thoughts = _split_parts(payload)
        for text in answers:
            chunks.append(text)
            on_delta(text)
        if on_thinking:
            for text in thoughts:
                on_thinking(text)
        usage = _extract_usage(payload)
        if usage:
            last_usage = usage
        finish = _finish_reason(payload) or finish

    if on_usage and last_usage:
        try:
            on_usage(last_usage)
        except Exception:  # noqa: BLE001 - usage tracking must never break a real reply
            pass

    text = "".join(chunks)
    if not text:
        if blocked:
            raise RuntimeError(
                f"Gemini refused the prompt before generating: "
                f"blockReason={blocked}. Nothing was generated."
            )
        if finish and finish not in _CLEAN_FINISH:
            raise RuntimeError(
                f"Gemini returned no text: finishReason={finish} (honest)."
            )
        if not saw_payload:
            raise RuntimeError(
                "Gemini returned an empty stream — no SSE data frames at "
                "all (honest). Nothing was generated."
            )
        raise RuntimeError("Gemini returned no output (honest).")

    if finish and finish not in _CLEAN_FINISH:
        marker = f"\n\n[gemini: stopped early — finishReason={finish}]"
        on_delta(marker)
        text += marker
    return text


def call_gemini(
    prompt: str,
    *,
    model: str | None = None,
    system: str | None = None,
    max_tokens: int | None = None,
    on_usage: Callable[[dict[str, int]], None] | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
    base_url: str | None = None,
    transport: GeminiTransport | None = None,
) -> str:
    """One-shot Gemini call; returns the full answer text.

    Implemented over the same streaming endpoint and the same parser as
    ``stream_gemini`` rather than a second ``:generateContent`` code path —
    one wire format to keep correct, one place a shape change has to be
    fixed, and the identical honest-error behaviour for free. The only
    differences are that deltas go to a local buffer instead of a callback
    and that ``max_tokens`` is honoured here. ``on_usage`` has the exact
    same one-shot, last-frame-wins contract as ``stream_gemini``'s own (see
    its docstring) — a caller (``model_delegation.py``) already assumed
    this parameter existed and defensively caught the ``TypeError`` it
    always raised until now, so usage/cost tracking for every one-shot
    Gemini delegation call was permanently a no-op.

    ``max_tokens`` defaults to None, which sends NO ``maxOutputTokens`` and
    lets the model use its own full ceiling. Read
    ``_MAX_OUTPUT_TOKENS_CEILING``'s comment before changing that: a
    "reasonable-looking" cap here does not shorten replies, it truncates
    them, and this repo has fixed that same bug in three other modules.
    """
    key = _require_key()
    resolved_model = _resolve_model(model)
    send = transport or _urllib_transport
    timeout = max(_MIN_TIMEOUT, min(float(timeout), _MAX_TIMEOUT))
    body = _build_body(prompt, system=system, max_tokens=max_tokens)

    lines = send(
        _endpoint(resolved_model, base_url),
        data=json.dumps(body).encode("utf-8"),
        headers=_headers(key),
        timeout=timeout,
    )

    chunks: list[str] = []
    last_usage: dict[str, int] = {}
    finish = ""
    blocked = ""
    saw_payload = False
    for payload in _iter_sse_payloads(lines):
        saw_payload = True
        _raise_if_error(payload)
        blocked = _block_reason(payload) or blocked
        answers, _thoughts = _split_parts(payload)
        chunks.extend(answers)
        usage = _extract_usage(payload)
        if usage:
            last_usage = usage
        finish = _finish_reason(payload) or finish

    if on_usage and last_usage:
        try:
            on_usage(last_usage)
        except Exception:  # noqa: BLE001 - usage tracking must never break a real reply
            pass

    text = "".join(chunks)
    if not text:
        if blocked:
            raise RuntimeError(
                f"Gemini refused the prompt before generating: "
                f"blockReason={blocked}. Nothing was generated."
            )
        if finish and finish not in _CLEAN_FINISH:
            raise RuntimeError(
                f"Gemini returned no text: finishReason={finish} (honest)."
            )
        if not saw_payload:
            raise RuntimeError(
                "Gemini returned an empty stream — no SSE data frames at "
                "all (honest). Nothing was generated."
            )
        raise RuntimeError("Gemini returned no output (honest).")
    if finish and finish not in _CLEAN_FINISH:
        text += f"\n\n[gemini: stopped early — finishReason={finish}]"
    return text
