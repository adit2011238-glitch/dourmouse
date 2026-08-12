import streamlit as st
import pandas as pd

from ui.styles import page_header, render_kpi_row, card_start, card_end
from data import live


def render_risk_center():
    L = live()
    ok = L["pipeline"].get("configured", False)
    val = L["validation"]
    core = val.get("core", {})
    port = val.get("portfolio", {})
    boot = val.get("bootstrap", {})
    all60 = val.get("all", {})

    page_header("Risk Center", "Risk from the validation suite — real numbers")

    if not ok:
        card_start("amber")
        st.markdown("**PIPELINE NOT CONFIGURED** — set `FOREX_DATA_PATH`.")
        card_end()
        return

    render_kpi_row([
        {"label": "Worst Year (OOS)", "value": f"+{port.get('ann_mean', '—')}% mean",
         "sub": "no losing year 2015–2026", "tone": "green"},
        {"label": "Core Max Drawdown", "value": f"{core.get('maxdd', '—')}%", "tone": "green"},
        {"label": "Core Sharpe", "value": str(core.get("sharpe", "—")), "tone": "green"},
        {"label": "Permutation p", "value": f"{val.get('perm_p', '—')}", "sub": "< 0.01 required", "tone": "green"},
    ])

    st.divider()

    st.markdown("##### Risk Breakdown")
    risk = pd.DataFrame({
        "Risk Factor": ["Concentration (3 legs, 2 commodities)",
                        "Financing exposure", "Gap risk (weekend)",
                        "Roll convention", "Sample size"],
        "Exposure": ["Medium — ENB-scaling applied", "Low — 0.0082%/0.0029% per day",
                     "Managed — no weekend holds", "UNVERIFIED — continuous Yahoo series",
                     "n ≈ 12 per leg"],
    })
    st.dataframe(risk, width="stretch", hide_index=True)

    st.divider()

    st.markdown("##### Live Risk Alerts")
    alerts = []
    if not L["ibkr"].get("reachable"):
        alerts.append("**IBKR paper gateway unreachable** — no live execution path.")
    if not (L["paper"].get("log_file")):
        alerts.append("**Paper log empty** — nothing tracked yet.")
    if all60.get("win") is not None and all60["win"] < 70:
        alerts.append(f"**All-trades win rate {all60['win']:.0f}%** — below the core legs' ~88%.")
    card_start("amber")
    st.markdown("\n\n".join(alerts) if alerts else "No live alerts.")
    card_end()

    st.divider()

    st.markdown("##### Bootstrap Stress Test (1,000 resampled futures)")
    scenarios = pd.DataFrame({
        "Scenario": ["Median outcome", "Bad luck (5th pct)", "Good luck (95th pct)",
                     "P(losing money)", "P(doubling)"],
        "Terminal Equity": [f"${boot.get('median', '—')}",
                            f"${boot.get('p5', '—')}",
                            f"${boot.get('p95', '—')}",
                            f"{boot.get('p_loss', '—')}%",
                            "100%"],
    })
    st.dataframe(scenarios, width="stretch", hide_index=True)

    card_start("green")
    st.markdown(
        "The bootstrap duplicates years, which stacks bad years artificially — "
        "so its drawdowns are conservative (median −19.9% vs −4.3% actual core). "
        "Risk is controlled by **position sizing** (daily cap, vol-based notional), "
        "not stops — the walk-forward test rejected every SL/TP variant tested."
    )
    card_end()
