"""Live NVIDIA API key validation for first-run onboarding (v2.6).

The 401/403 failure mode this fixes: start.command used to accept a key based
ONLY on its format (nvapi-<token>, length >= 16), so an invalid, revoked, or
inference-restricted key was written to .env and only failed later, mid-chat,
with an opaque auth error. This module closes that gap:

- ``validate_key_live`` makes a REAL 1-token chat completion through the same
  OpenAI-compatible client the app uses (openai SDK -> NVIDIA NIM), so a key
  that passes /v1/models but is forbidden from the configured model is caught
  at onboarding time.
- Every failure is mapped to a clear, actionable message (401 = invalid /
  revoked, 403 = valid key but no access to THIS model — the exact trap that
  produced the earlier 401, 429 = rate limited, network = unreachable).
- ``main`` reads the key from STDIN (never argv, so it can't leak via `ps`),
  and never prints more than a masked fragment (Rule 2.6).

The client is injectable (``client_factory``) so tests exercise the real
module with a fake client and zero network (Rule 2.1 discipline).
"""

from __future__ import annotations

import os
import sys
from typing import Any, Callable

from openai import (
    APIConnectionError,
    APIStatusError,
    AuthenticationError,
    PermissionDeniedError,
    RateLimitError,
)

from dourmouse.config import NVIDIA_DEFAULT_BASE_URL, NVIDIA_DEFAULT_MODEL

_DEFAULT_TIMEOUT_SECONDS = 30.0
_MIN_KEY_LENGTH = 16


def _mask(key: str) -> str:
    """Mask a key for display: first 9 chars + last 4 (never the middle)."""
    if len(key) <= 16:
        return f"{key[:4]}…{key[-4:]}" if len(key) > 8 else "***"
    return f"{key[:9]}…{key[-4:]}"


def _default_client_factory(api_key: str, base_url: str) -> Any:
    """Build the same OpenAI-compatible client dispatch.py uses."""
    from openai import OpenAI

    return OpenAI(api_key=api_key, base_url=base_url, timeout=_DEFAULT_TIMEOUT_SECONDS)


def validate_key_live(
    api_key: str,
    *,
    base_url: str | None = None,
    model: str | None = None,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    client_factory: Callable[[str, str], Any] | None = None,
) -> tuple[bool, str]:
    """Validate a candidate key with a REAL 1-token call. Returns (ok, message).

    ``base_url``/``model`` default to the same values the engine uses (env
    overrides honored), and ``client_factory`` is the test seam — production
    uses the real openai client, tests inject a fake one.
    """
    key = (api_key or "").strip()
    if not key:
        return False, "No API key provided."
    if len(key) < _MIN_KEY_LENGTH:
        return (
            False,
            f"REJECTED: key {_mask(key)} looks too short — expected 'nvapi-…' "
            f"(NVIDIA keys are ≥ {_MIN_KEY_LENGTH} chars).",
        )
    if not key.startswith("nvapi-"):
        return (
            False,
            f"REJECTED: key {_mask(key)} does not start with 'nvapi-' — NVIDIA "
            "NIM keys always do. Double-check the paste.",
        )

    # Explicit args win, then env overrides, then the shared defaults — the
    # same precedence config.py uses (so a custom NVIDIA_MODEL in .env is
    # validated against the real model, not the default).
    base_url = (
        base_url
        or os.environ.get("NVIDIA_BASE_URL")
        or NVIDIA_DEFAULT_BASE_URL
    )
    model = model or os.environ.get("NVIDIA_MODEL") or NVIDIA_DEFAULT_MODEL
    factory = client_factory or _default_client_factory

    try:
        client = factory(key, base_url)
        client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
        )
    except AuthenticationError as exc:
        return (
            False,
            f"REJECTED: key {_mask(key)} is invalid, expired, or revoked "
            f"(HTTP 401). Get a fresh key at https://build.nvidia.com. ({exc})",
        )
    except PermissionDeniedError as exc:
        return (
            False,
            f"REJECTED: key {_mask(key)} is valid but has NO access to model "
            f"'{model}' (HTTP 403). This key cannot run that model — check "
            f"NVIDIA_MODEL or use a key granted access to it. ({exc})",
        )
    except RateLimitError as exc:
        return (
            False,
            f"UNAVAILABLE: rate limited (HTTP 429) — wait a moment and retry. ({exc})",
        )
    except APIConnectionError as exc:
        return (
            False,
            f"UNAVAILABLE: could not reach NVIDIA at {base_url} — check your "
            f"network connection. ({exc})",
        )
    except APIStatusError as exc:
        return (
            False,
            f"ERROR: NVIDIA returned HTTP {getattr(exc, 'status_code', '?')} — {exc}",
        )
    except Exception as exc:  # surface the real failure, never guess (Rule 2.7)
        return False, f"ERROR: unexpected failure during key validation: {exc}"

    return True, f"VALID: key {_mask(key)} authenticated and can run '{model}' (HTTP 200)."


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: validate a key live, exit 0/1.

    Default mode reads the key from STDIN (one line) so it never appears in
    argv or the process list — what start.command uses. ``--check-existing``
    instead validates the key ALREADY in .env (the stale-key trap that
    produced the earlier 401), via the same loader the engine uses.

    The printed message passes through DlpFilter so even an exception that
    echoed a credential can never leak one (Rule 2.6 defense in depth).
    """
    args = argv if argv is not None else sys.argv[1:]
    if "--check-existing" in args:
        from dourmouse.config import load_nvidia_config

        try:
            config = load_nvidia_config()
        except ValueError as exc:
            print(f"  {exc}")
            return 1
        ok, message = validate_key_live(
            config.api_key, base_url=config.base_url, model=config.model
        )
    else:
        key = ""
        for line in sys.stdin:
            line = line.strip()
            if line:
                key = line
                break
        ok, message = validate_key_live(key)

    from dourmouse.governance import DlpFilter

    safe, _ = DlpFilter().redact(message)
    print(f"  {safe}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
