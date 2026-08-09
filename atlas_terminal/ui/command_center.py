import os
import subprocess

import streamlit as st
import plotly.graph_objects as go

from ui.styles import COLORS, page_header, render_kpi_row, card_start, card_end, apply_terminal_theme
from data import live


def gauge(title, value, tone, max_v=100):
    color = COLORS[tone]
    fig = go.Figure(go.Indicator(
        mode="gauge+number",
        value=value,
        number={"font": {"color": COLORS["text_primary"], "size": 30}},
        title={"text": title, "font": {"color": COLORS["text_secondary"], "size": 13}},
        gauge={
            "axis": {"range": [0, max_v], "tickcolor": COLORS["text_muted"]},
            "bgcolor": COLORS["surface_alt"],
            "borderwidth": 1,
            "bordercolor": COLORS["border"],
            "bar": {"color": color},
            "steps": [
                {"range": [0, 40], "color": COLORS["red_soft"]},
                {"range": [40, 70], "color": COLORS["amber_soft"]},
                {"range": [70, 100], "color": COLORS["green_soft"]},
            ],
        },
    ))
    apply_terminal_theme(fig, height=220)
    st.plotly_chart(fig, width="stretch")


def render_command_center():
    L = live()
    ok = L["pipeline"].get("configured", False)
    val = L["validation"]
    cal = L["calendar"]
    ibkr = L["ibkr"]

    page_header("Command Center", "Real pipeline decision intelligence")

    if not ok:
        card_start("amber")
        st.markdown(f"**PIPELINE NOT CONFIGURED** — set `FOREX_DATA_PATH` (see "
                    f"`docs/integration-forex-data.md`). {L['pipeline'].get('error', '')}")
        card_end()
        return

    next_win = next((w for w in cal if w["status"] == "NOW OPEN"), cal[0] if cal else None)
    core = val.get("core", {})
    boot = val.get("bootstrap", {})
    p_loss = boot.get("p_loss")

    render_kpi_row([
        {"label": "Validation", "value": "PASSED" if val.get("perm_p") and val["perm_p"] < 0.01 else "—",
         "sub": f"perm p = {val.get('perm_p', '—')}", "tone": "green" if val.get("perm_p") and val["perm_p"] < 0.01 else "amber"},
        {"label": "Market Regime", "value": "HOG CYCLE + CORN", "sub": "mechanism family", "tone": "blue"},
        {"label": "Next Trade", "value": next_win["leg"] if next_win else "—",
         "sub": next_win["status"] if next_win else "no window", "tone": "blue"},
        {"label": "Core Sharpe", "value": str(core.get("sharpe", "—")), "sub": "OOS 2015+", "tone": "green"},
    ])

    st.divider()

    g1, g2, g3 = st.columns(3)
    with g1:
        # confidence = probability the account does not lose money (bootstrap)
        gauge("No-Loss Probability", 100.0 - (p_loss or 0.0), "green" if p_loss == 0.0 else "amber")
    with g2:
        # risk score = max drawdown on 0-100 scale (0 = none)
        mdd = abs(core.get("maxdd") or 0.0)
        gauge("Risk Score", min(100.0, mdd * 12), "green" if mdd < 8 else "red", max_v=100)
    with g3:
        # portfolio health = core terminal multiple capped at 10x
        term = core.get("terminal") or 100.0
        gauge("Portfolio Health", min(100.0, term / 10.0), "green", max_v=100)

    st.divider()

    st.markdown("##### Locked Standard (five-stage validation suite)")
    std = L.get("standard", {})
    if std.get("numbers"):
        nums = std["numbers"]
        core = nums.get("portfolio_core") or {}
        boot = nums.get("bootstrap") or {}
        st.markdown(
            f"**Generated {std.get('generated_utc', '?')}** · in-sample "
            f"{std['protocol']['in_sample'][0]} → {std['protocol']['in_sample'][1]} · "
            f"OOS from {std['protocol']['oos_start']} · config T={std['config']['T']}, "
            f"min_n={std['config']['min_n']}"
        )
        render_kpi_row([
            {"label": "Permutation p", "value": f"{nums.get('permutation_p', '—'):.4f}",
             "sub": "bar < 0.01", "tone": "green" if (nums.get('permutation_p') or 1) < 0.01 else "red"},
            {"label": "Core Terminal", "value": f"${core.get('terminal', '—'):.2f}", "tone": "green"},
            {"label": "Core Sharpe", "value": f"{core.get('sharpe', '—'):.2f}", "tone": "green"},
            {"label": "P(losing money)", "value": f"{boot.get('p_loss_pct', '—')}%", "tone": "green"},
        ])
        st.caption("Standard legs: HE_8, HE_4, ZC_12 — the walk-forward killed "
                   "gasoline, cattle, hogs-Oct and hogs-Feb. This is the standard "
                   "every number in this terminal cites.")
    else:
        st.markdown("_Standard file missing — re-run the suite below to generate it._")

    if st.button("Re-run five-stage validation suite", type="primary"):
        root = L["pipeline"].get("root", "")
        with st.spinner("Running the full suite (~1-2 min)..."):
            try:
                proc = subprocess.run(
                    ["python", "scripts/seasonal_validation.py"],
                    cwd=root, capture_output=True, text=True, timeout=600,
                )
                tail = "\n".join((proc.stdout or "").splitlines()[-12:])
                if proc.returncode != 0:
                    tail += "\n[exit %d] " % proc.returncode + (proc.stderr or "")[-300:]
                st.code(tail or "(no output)", language="text")
                st.success("Suite finished — standard refreshed." if proc.returncode == 0
                           else f"Suite failed (exit {proc.returncode}) — see output.")
            except (OSError, subprocess.TimeoutExpired) as exc:
                st.error(f"Could not run suite: {exc}")

    st.divider()

    st.markdown("##### Recommended Action")
    if next_win:
        card_start("green")
        side = "SHORT" if next_win["leg"].endswith(("_8", "_9", "_10")) and next_win["leg"].startswith(("HE", "RB", "LE")) else "LONG"
        st.markdown(
            f"**{next_win['leg']} window {'is OPEN now' if next_win['status'] == 'NOW OPEN' else 'is upcoming'} "
            f"({next_win['entry']} → {next_win['exit']}).**\n\n"
            f"- Validated out-of-sample (2015+), net of real T212 fees\n"
            f"- Core legs only — the rest of the family was killed by the strict walk-forward\n"
            f"- Size by volatility; run stopless to month close (stops were tested and rejected)\n"
            f"- Enter at month open, exit at month close — do not hand-tune"
        )
        card_end()
    else:
        card_start("amber")
        st.markdown("No trade windows on the calendar yet.")
        card_end()

    st.divider()

    left, right = st.columns([2, 1])
    with left:
        st.markdown("##### Validated Legs (OOS 2015+, net of costs)")
        legs = val.get("legs", {})
        if legs:
            rows = [
                {
                    "Leg": k,
                    "Trades": v["n"],
                    "Mean net": f"{v['mean']:+.2f}%" if v["mean"] is not None else "—",
                    "t": f"{v['t']:+.2f}" if v["t"] is not None else "—",
                    "Win": f"{v['win']:.0f}%" if v["win"] is not None else "—",
                }
                for k, v in sorted(legs.items(), key=lambda kv: (kv[1].get("t") or 0), reverse=True)
            ]
            st.dataframe(rows, width="stretch", hide_index=True)
        else:
            st.markdown("_No leg stats parsed (report missing or reformatted)._")

    with right:
        st.markdown("##### Live Alerts")
        card_start("amber" if not ibkr.get("reachable") else "blue")
        st.markdown(
            f"**IBKR gateway** — {'REACHABLE' if ibkr.get('reachable') else 'UNREACHABLE'}\n\n"
            f"{'' if ibkr.get('reachable') else 'Start IB Gateway (socket clients enabled) on 192.168.1.95.'}"
        )
        card_end()
        card_start("blue")
        st.markdown(
            "**Honest limits**\n\n"
            "n ≈ 12 per leg · only 2026 is truly untouched · "
            "roll convention unverified."
        )
        card_end()
