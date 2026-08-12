"""World Monitor global-intelligence bridge (v5.12).

Reads the World Monitor platform (worldmonitor.app) — a real-time global
intelligence API: market data, country risk/briefs, conflict events, news
intelligence, natural disasters, cyber threats, sanctions, forecasts,
supply-chain data, and 50+ more MCP tools.

Uses the official ``worldmonitor_sdk`` (stdlib-only, MIT). Two surfaces:

- **Keyless** (no API key needed): the public health status
  (``/api/health?compact=1``) and the MCP tool catalog (``tools/list``,
  59 tools). These always work.
- **Keyed** (``WORLDMONITOR_API_KEY`` / ``WM_API_KEY`` in .env, e.g.
  ``wm_...`` from worldmonitor.app/pro): every data tool via ``tools/call``.
  Without a key the data tools report NOT CONFIGURED honestly (Rule 2.2) —
  never a fabricated quote, brief, or risk score.

Deterministic (Rule 2.8): the client is injectable so tests swap a fake
transport and never touch the network. Secrets come only from env (Rule
2.6). The SDK raises typed errors (``MCPError`` for JSON-RPC rejections,
``APIError`` for transport failures); we surface the REAL message — a
missing key, a rate limit, or a dead endpoint is reported, never masked.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from worldmonitor_sdk import (  # type: ignore[import-untyped]  # no official stubs; used structurally
    APIError,
    Client,
    MCPError,
)

# Output caps so a huge feed can never blow the model's context (same
# bounded-window guard philosophy as the Freebuff bridge).
_MAX_CATALOG = 60
_MAX_TOOL_RESULT_CHARS = 8_000

# Short probe timeout: check_connections() polls from the HUD, and its
# docstring promises "no remote network calls" — when it does probe here,
# a dead API must not stall the report for the SDK's 30s default.
_PROBE_TIMEOUT = 3.0

# A tiny allow-list guard: only the tool names the MCP server actually
# serves can be called. Re-checked live each run by the catalog — a name
# outside this list is refused up front (never a blind proxy to the API).
_ALLOWED_TOOLS = frozenset(
    {
        "get_market_data",
        "get_conflict_events",
        "get_aviation_status",
        "get_news_intelligence",
        "get_natural_disasters",
        "get_military_posture",
        "get_cyber_threats",
        "get_economic_data",
        "get_country_macro",
        "get_eu_housing_cycle",
        "get_eu_quarterly_gov_debt",
        "get_eu_industrial_production",
        "get_prediction_markets",
        "get_sanctions_data",
        "get_displacement_data",
        "get_health_signals",
        "get_energy_intelligence",
        "get_climate_data",
        "get_infrastructure_status",
        "get_supply_chain_data",
        "get_tariff_trends",
        "get_chokepoint_status",
        "get_positive_events",
        "get_radiation_data",
        "get_research_signals",
        "get_forecast_predictions",
        "get_forecast_scorecard",
        "get_social_velocity",
        "get_temporal_anomalies",
        "get_test_site_seismicity",
        "get_china_decision_signals",
        "get_procurement_opportunities",
        "get_world_brief",
        "get_country_brief",
        "get_country_risk",
        "get_consumer_prices",
        "get_airspace",
        "get_maritime_activity",
        "analyze_situation",
        "generate_forecasts",
        "search_flights",
        "search_flight_prices_by_date",
        "get_commodity_geo",
        "get_signal_convergence",
        "get_focal_points",
        "simulate_infrastructure_cascade",
        "get_military_surge",
        "get_population_exposure",
        "get_alert_digest",
        "get_hotspot_escalation",
        "search_intel_history",
        "get_intel_timeline",
        "get_similar_events",
        "get_company_intelligence",
        "describe_tool",
        "classify_event",
        "extract_entities",
        "get_news_clusters",
        "get_keyword_spikes",
    }
)


class WorldMonitorNotAvailable(RuntimeError):
    """The World Monitor API is unreachable or rejected the request."""


def worldmonitor_configured() -> bool:
    """True when a World Monitor API key is present in env (never the key)."""
    return bool(os.environ.get("WORLDMONITOR_API_KEY", "").strip()) or bool(
        os.environ.get("WM_API_KEY", "").strip()
    )


def _client(transport: Callable[..., Any] | None = None, timeout: float | None = None) -> Client:
    """Build the SDK client from env; transport/timeout are injectable.

    The probe timeout is short by default so a dead API never stalls a HUD
    poll (the SDK's 30s default is reserved for real data calls)."""
    return Client(transport=transport, timeout=timeout or _PROBE_TIMEOUT)


def worldmonitor_status() -> dict[str, Any]:
    """Honest keyless status: API health (compact) + whether a key is set.

    Never raises. ``ok`` is True when the public health endpoint answers —
    the keyed data tools may still report NOT CONFIGURED without a key.
    Uses the short probe timeout so a dead API never stalls a poll.
    """
    key = worldmonitor_configured()
    base: dict[str, Any] = {"ok": False, "key_configured": key, "detail": ""}
    try:
        client = _client()
        health = client.get("/api/health?compact=1")
    except (APIError, MCPError, OSError, ValueError) as exc:
        base["detail"] = f"World Monitor API unreachable: {exc}"
        return base
    if not isinstance(health, dict):
        base["detail"] = "World Monitor health returned an unexpected shape"
        return base
    status = str(health.get("status", "")).upper()
    summary = health.get("summary") or {}
    total = summary.get("total")
    crit = summary.get("crit", 0)
    base["ok"] = status == "WARNING" or status == "OK"
    base["detail"] = (
        f"public health: {status} · {total} signals · {crit} critical"
        + ("" if key else " · no API key (data tools need WORLDMONITOR_API_KEY)")
    )
    return base


def worldmonitor_catalog() -> list[dict[str, str]]:
    """The public MCP tool catalog (name + one-line purpose), keyless.

    Raises WorldMonitorNotAvailable on any failure — the caller renders it
    honestly (Rule 2.2).
    """
    try:
        result = _client().list_tools()
    except (APIError, MCPError, OSError, ValueError) as exc:
        raise WorldMonitorNotAvailable(f"World Monitor tool catalog unavailable: {exc}") from exc
    tools = result.get("tools") if isinstance(result, dict) else None
    if not isinstance(tools, list):
        raise WorldMonitorNotAvailable("World Monitor tool catalog returned an unexpected shape")
    out: list[dict[str, str]] = []
    for t in tools[:_MAX_CATALOG]:
        if not isinstance(t, dict):
            continue
        name = str(t.get("name", ""))
        desc = (t.get("description") or "").strip().splitlines()[0] if t.get("description") else ""
        out.append({"name": name, "description": desc[:200]})
    return out


def _tool_names_text() -> str:
    """Compact catalog for the tool handler (names only, capped)."""
    try:
        catalog = worldmonitor_catalog()
    except WorldMonitorNotAvailable as exc:
        return f"WORLD MONITOR CATALOG (reported honestly): {exc}"
    lines = [f"- {t['name']}: {t['description']}" for t in catalog]
    return "WORLD MONITOR CATALOG (live, keyless):\n" + "\n".join(lines)


def _is_known_tool(name: str) -> bool:
    """Is ``name`` a tool the server actually serves today?

    Checks the LIVE keyless catalog first (the source of truth — the API
    adds/removes tools and the catalog is what the model reads), then falls
    back to the frozen ``_ALLOWED_TOOLS`` set only when the catalog is
    unreachable, so an offline API never blocks a call that the safety
    floor already permits (reviewer fix: catalog/call_tool drift)."""
    try:
        catalog = worldmonitor_catalog()
    except WorldMonitorNotAvailable:
        return name in _ALLOWED_TOOLS
    return any(t["name"] == name for t in catalog)


def worldmonitor_call_tool(name: str, arguments: dict[str, Any] | None = None) -> Any:
    """Call one World Monitor data tool by name (keyed; honest errors).

    Requires a key (WORLDMONITOR_API_KEY / WM_API_KEY) — without one raises
    WorldMonitorNotAvailable('NOT CONFIGURED ...'). The tool name is checked
    against the LIVE catalog (keyless, cheap) with the frozen allow-list as
    an offline fallback — a name the catalog advertises is always callable,
    and anything else is refused before any network call. Real API errors
    (auth, rate limit, dead endpoint) surface with the SDK's message — never
    fabricated data (Rule 2.2).
    """
    name = (name or "").strip()
    if not name:
        raise WorldMonitorNotAvailable("worldmonitor_call_tool requires a non-empty 'tool_name'.")
    if not _is_known_tool(name):
        raise WorldMonitorNotAvailable(
            f"Unknown World Monitor tool {name!r} — use worldmonitor_catalog to see the "
            "available tools (honest, nothing was called)."
        )
    if not worldmonitor_configured():
        raise WorldMonitorNotAvailable(
            "NOT CONFIGURED: World Monitor data tools need WORLDMONITOR_API_KEY "
            "(or WM_API_KEY) in .env — get one at worldmonitor.app/pro. Nothing "
            "was called and no data was fabricated."
        )
    args = dict(arguments or {})
    try:
        result = _client().call_tool(name, args)
    except (MCPError, APIError) as exc:
        raise WorldMonitorNotAvailable(f"World Monitor {name} failed: {exc}") from exc
    except OSError as exc:
        raise WorldMonitorNotAvailable(f"World Monitor API unreachable: {exc}") from exc
    return result


# --------------------------------------------------------------------------- #
# Tool handlers (plain text for the model)
# --------------------------------------------------------------------------- #

def _worldmonitor_status_tool(arguments: dict[str, Any]) -> str:
    st = worldmonitor_status()
    return f"WORLD MONITOR STATUS (live): {'OK' if st['ok'] else 'DEGRADED'} — {st['detail']}"


def _worldmonitor_catalog_tool(arguments: dict[str, Any]) -> str:
    return _tool_names_text()


def _worldmonitor_call_tool(arguments: dict[str, Any]) -> str:
    name = (arguments.get("tool_name") or "").strip()
    args = arguments.get("arguments") or {}
    if not isinstance(args, dict):
        return "ERROR: worldmonitor_call_tool 'arguments' must be a JSON object."
    try:
        result = worldmonitor_call_tool(name, args)
    except WorldMonitorNotAvailable as exc:
        return f"WORLD MONITOR (reported honestly): {exc}"
    text = result if isinstance(result, str) else _to_json(result)
    if not text:
        return "WORLD MONITOR (reported honestly): empty response."
    return f"WORLD MONITOR {name} (live):\n" + text[:_MAX_TOOL_RESULT_CHARS]


def _to_json(value: Any) -> str:
    import json

    try:
        return json.dumps(value, indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


# --------------------------------------------------------------------------- #
# ToolSpecs for the roster
# --------------------------------------------------------------------------- #

def _spec(name: str, description: str, handler: Any, props: dict[str, Any], required: list[str] | None = None) -> Any:
    from dourmouse.dispatch import ToolSpec

    return ToolSpec(
        name=name,
        description=description,
        parameters={"type": "object", "properties": props, "required": required or []},
        handler=handler,
    )


def build_worldmonitor_tool_specs() -> list[Any]:
    """The v5.12 World Monitor ToolSpecs for the ``worldmonitor`` subagent.

    Keyless: status + catalog. Keyed: one generic call_tool that reaches
    all 59 MCP tools (the model reads the catalog, then calls by name).
    """
    return [
        _spec(
            "worldmonitor_status",
            "World Monitor global-intelligence API status: public health "
            "(signal counts, critical alerts) and whether a data key is "
            "configured. Keyless. Use first to check the connection.",
            _worldmonitor_status_tool,
            {},
        ),
        _spec(
            "worldmonitor_catalog",
            "List the World Monitor MCP tool catalog (59 tools: market "
            "data, country risk/briefs, conflict events, news intelligence, "
            "natural disasters, cyber threats, sanctions, forecasts, "
            "supply-chain data, and more) with one-line purposes. Keyless. "
            "Use this to pick a tool_name for worldmonitor_call_tool.",
            _worldmonitor_catalog_tool,
            {},
        ),
        _spec(
            "worldmonitor_call_tool",
            "Call ONE World Monitor data tool by name (from "
            "worldmonitor_catalog) with its JSON arguments, e.g. "
            "tool_name=get_market_data with {\"symbols\": [\"GC=F\",\"BTC\"]} "
            "or tool_name=get_country_risk with {\"country_code\": \"IR\"}. "
            "Requires WORLDMONITOR_API_KEY in .env (worldmonitor.app/pro) — "
            "honestly NOT CONFIGURED without it. Returns the real data; "
            "never fabricates quotes or scores.",
            _worldmonitor_call_tool,
            {
                "tool_name": {
                    "type": "string",
                    "description": "the MCP tool name from worldmonitor_catalog",
                },
                "arguments": {
                    "type": "object",
                    "description": "JSON arguments for the tool (see its description in the catalog)",
                },
            },
            ["tool_name"],
        ),
    ]
