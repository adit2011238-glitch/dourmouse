"""Chinese-lab coding backends (v1) — Qwen / GLM / Kimi.

Extends the ``code_backends.py`` multi-backend dispatch with three
OpenAI-compatible providers from Chinese labs, each cheap-or-free-tier,
laying groundwork for a "team of subagents" spread across many providers.
Mirrors ``code_backends.load_backend()`` exactly: same function shape,
same honest ``NOT CONFIGURED`` behavior when a key is absent, same
``(base_url, api_key, model)`` return contract so the existing
``_run_openai_compat`` in ``code_backends.py`` runs these unchanged — this
module intentionally does NOT duplicate that call path.

- ``qwen``  — Alibaba Cloud Model Studio (DashScope), OpenAI-compatible
  endpoint. New DashScope/Bailian accounts get a time-limited signup
  token grant (widely reported in the tens of millions of tokens as of
  research time) — a promotional credit, not a permanently free tier.
  Key: ``QWEN_API_KEY``, falling back to ``DASHSCOPE_API_KEY`` (the name
  Alibaba's own SDKs use, so an existing DashScope setup keeps working
  without a rename).
- ``glm``   — Zhipu AI, now also branded "Z.ai", OpenAI-compatible
  endpoint at bigmodel.cn. GLM-4-Flash / GLM-4.5-Flash class models were
  reported as a genuinely free tier (no card, ongoing) as of research
  time — unlike Qwen/Kimi's one-time signup-credit model. Sign-up was
  reported to require phone verification, which for some regions means a
  Chinese phone number. Key: ``ZHIPU_API_KEY``, falling back to
  ``GLM_API_KEY``.
- ``kimi``  — Moonshot AI, OpenAI-compatible endpoint. Cheap, NOT free:
  research found no standing free tier for direct API access — accounts
  reportedly need a paid top-up (around $1 minimum) before a key works.
  Wired anyway as the cheap tier the task asked for, with that caveat
  spelled out in the NOT CONFIGURED message so nobody expects $0 usage.
  Key: ``MOONSHOT_API_KEY``.

Every path returns REAL output or an honest error (Rules 2.1 / 2.2):
a missing key is NOT CONFIGURED, nothing is ever fabricated. Secrets
come only from env vars (Rule 2.6).

Research note: the above was gathered via WebSearch in August 2026
against a mix of each provider's own docs and third-party aggregator
sites; free/cheap terms for these providers change often and were not
verified against a live key (Rule 2.6 — no account was created and no
key was obtained for this work). Re-check before relying on any of this
for a real deployment.
"""

from __future__ import annotations

import os

_QWEN_DEFAULT_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
_QWEN_DEFAULT_MODEL = "qwen-plus"

_GLM_DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
_GLM_DEFAULT_MODEL = "glm-4-flash"  # the model reported as free-tier

_KIMI_DEFAULT_BASE_URL = "https://api.moonshot.ai/v1"
_KIMI_DEFAULT_MODEL = "kimi-k2.6"  # cheapest non-flagship tier at research time


def load_backend(backend: str) -> tuple[str, str, str]:
    """Resolve (base_url, api_key, model) for a Chinese-lab OpenAI-compatible
    backend: ``qwen``, ``glm``, or ``kimi``.

    Raises RuntimeError('NOT CONFIGURED: ...') when the backend cannot be
    used — never falls back to a fabricated result. Mirrors
    ``code_backends.load_backend()``'s contract so ``code_backends.py``
    can delegate here with a single if-branch.
    """
    name = (backend or "").strip().lower()
    if name in ("qwen", "dashscope"):
        key = os.environ.get("QWEN_API_KEY", "").strip()
        if not key:
            key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
        if not key:
            raise RuntimeError(
                "NOT CONFIGURED: the Qwen coding backend needs QWEN_API_KEY "
                "(or DASHSCOPE_API_KEY) in .env — an Alibaba Cloud Model "
                "Studio / DashScope key. New accounts get a time-limited "
                "signup token grant, not a permanent free tier. Nothing "
                "was run."
            )
        base = os.environ.get("QWEN_BASE_URL", "").strip() or _QWEN_DEFAULT_BASE_URL
        model = os.environ.get("QWEN_MODEL", "").strip() or _QWEN_DEFAULT_MODEL
        return base, key, model
    if name in ("glm", "zhipu", "z.ai", "zai"):
        key = os.environ.get("ZHIPU_API_KEY", "").strip()
        if not key:
            key = os.environ.get("GLM_API_KEY", "").strip()
        if not key:
            raise RuntimeError(
                "NOT CONFIGURED: the GLM coding backend needs ZHIPU_API_KEY "
                "(or GLM_API_KEY) in .env — a Zhipu AI / Z.ai (bigmodel.cn) "
                "key. GLM-4-Flash-class models were reported as a genuinely "
                "free tier at research time. Nothing was run."
            )
        base = os.environ.get("GLM_BASE_URL", "").strip() or _GLM_DEFAULT_BASE_URL
        model = os.environ.get("GLM_MODEL", "").strip() or _GLM_DEFAULT_MODEL
        return base, key, model
    if name in ("kimi", "moonshot"):
        key = os.environ.get("MOONSHOT_API_KEY", "").strip()
        if not key:
            raise RuntimeError(
                "NOT CONFIGURED: the Kimi coding backend needs "
                "MOONSHOT_API_KEY in .env — a Moonshot AI platform key. "
                "NOTE: research found no standing free tier for direct API "
                "access here (the account reportedly needs a paid top-up "
                "before the key works) — wired as the cheap tier, not a "
                "free one. Nothing was run."
            )
        base = os.environ.get("MOONSHOT_BASE_URL", "").strip() or _KIMI_DEFAULT_BASE_URL
        model = os.environ.get("MOONSHOT_MODEL", "").strip() or _KIMI_DEFAULT_MODEL
        return base, key, model
    raise RuntimeError(
        f"ERROR: unknown Chinese-lab code backend {backend!r} — use 'qwen', "
        "'glm' or 'kimi'."
    )
