import streamlit as st
import pandas as pd

from ui.styles import page_header, render_kpi_row, card_start, card_end
from data import live

LEG_NAMES = {
    "HE_8": "SHORT hogs Aug", "HE_4": "LONG hogs Apr", "ZC_12": "LONG corn Dec",
    "RB_9": "SHORT gasoline Sep", "HE_10": "SHORT hogs Oct", "HE_2": "LONG hogs Feb",
    "LE_5": "SHORT cattle May",
}


def render_portfolio_intelligence():
    L = live()
    ok = L["pipeline"].get("configured", False)
    paper = L["paper"]

    page_header("Portfolio Intelligence", "The $100 paper account")

    if not ok:
        card_start("amber")
        st.markdown("**PIPELINE NOT CONFIGURED** — set `FOREX_DATA_PATH`.")
        card_end()
        return

    open_pos = paper.get("open_positions", []) if paper.get("log_file") else []
    realized = paper.get("realised_pnl_usd", 0.0)
    total = paper.get("trades", 0)

    render_kpi_row([
        {"label": "Account (start)", "value": "$100", "tone": "blue"},
        {"label": "Realised P&L", "value": f"${realized:,.2f}", "tone": "green" if realized >= 0 else "red"},
        {"label": "Open Positions", "value": str(len(open_pos)), "tone": "blue"},
        {"label": "Trades Logged", "value": str(total), "tone": "blue"},
    ])

    st.divider()

    st.markdown("##### Paper Positions")
    if paper.get("log_file"):
        rows = []
        for r in paper.get("open_positions", []):
            rows.append({"Leg": r.get("key", "?"), "Side": LEG_NAMES.get(r.get("key", ""), ""),
                         "Entry": r.get("entry_date", ""), "Price": r.get("entry_price", ""),
                         "Size": r.get("size", ""), "Status": "OPEN"})
        for r in paper.get("closed_trades", []):
            rows.append({"Leg": r.get("key", "?"), "Side": LEG_NAMES.get(r.get("key", ""), ""),
                         "Entry": r.get("entry_date", ""), "Price": r.get("entry_price", ""),
                         "Size": r.get("size", ""), "Status": f"CLOSED {r.get('exit_date', '')}"})
        if rows:
            st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
        else:
            st.markdown("_Log exists but is empty._")
    else:
        st.markdown("_No paper log yet (`reports/paper_log.csv` missing). "
                    "Log entries with `scripts/paper_log.py open/close`._")

    st.divider()

    left, right = st.columns(2)
    with left:
        st.markdown("##### Account Summary")
        card_start("green")
        st.markdown(
            "- Validated account: **$100 start**, $5/day risk cap, 8.5% equity floor\n"
            "- Core portfolio (HE_8, HE_4, ZC_12): $100 → **$438** OOS, Sharpe 3.34\n"
            "- All-gated portfolio: $100 → **$409**, Sharpe 1.43, 12/12 positive years\n"
            "- Bootstrap: **P(losing money) = 0%** across 1,000 draws\n"
            "- Annual return: mean **+20.5%**, std ±9.0%, worst year **+7.9%**"
        )
        card_end()

    with right:
        st.markdown("##### Risk Warnings")
        card_start("amber")
        st.markdown(
            "- Only 2026 is a truly untouched year\n"
            "- n ≈ 12 per leg — t-stats rest on ~a dozen trades\n"
            "- Roll convention still unverified\n"
            "- IBKR gateway "
            + ("reachable" if L["ibkr"].get("reachable") else "**unreachable**")
        )
        card_end()
