import streamlit as st
import pandas as pd
from datetime import datetime, date

from ui.styles import page_header, render_kpi_row, render_status_pill, card_start, card_end
from data import live

LEG_NAMES = {
    "HE_8": "SHORT hogs Aug", "HE_4": "LONG hogs Apr", "ZC_12": "LONG corn Dec",
    "RB_9": "SHORT gasoline Sep", "HE_10": "SHORT hogs Oct", "HE_2": "LONG hogs Feb",
    "LE_5": "SHORT cattle May",
}


def render_execution_center():
    L = live()
    ok = L["pipeline"].get("configured", False)
    paper = L["paper"]
    ibkr = L["ibkr"]
    cal = L["calendar"]

    page_header("Execution Center", "Paper-trading execution and order flow")

    if not ok:
        card_start("amber")
        st.markdown("**PIPELINE NOT CONFIGURED** — set `FOREX_DATA_PATH`.")
        card_end()
        return

    render_status_pill(
        "IBKR GATEWAY REACHABLE" if ibkr.get("reachable") else "IBKR GATEWAY UNREACHABLE",
        "green" if ibkr.get("reachable") else "red",
    )
    st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)

    closed = paper.get("closed_trades", 0)
    total = paper.get("trades", 0)
    render_kpi_row([
        {"label": "Trades Logged", "value": str(total), "tone": "blue"},
        {"label": "Closed", "value": str(closed), "tone": "green"},
        {"label": "Realised P&L", "value": f"${paper.get('realised_pnl_usd', 0.0):.2f}", "tone": "green"},
    ])

    st.divider()

    st.markdown("##### Paper Order Book")
    if paper.get("log_file"):
        rows = []
        for r in paper.get("open_positions", []):
            rows.append({"Leg": r.get("key", "?"), "Side": LEG_NAMES.get(r.get("key", ""), ""),
                         "Entry": r.get("entry_date", ""), "Price": r.get("entry_price", ""),
                         "Size": r.get("size", ""), "Status": "OPEN"})
        for r in paper.get("closed_trades", []):
            rows.append({"Leg": r.get("key", "?"), "Side": LEG_NAMES.get(r.get("key", ""), ""),
                         "Entry": r.get("entry_date", ""), "Price": r.get("entry_price", ""),
                         "Size": r.get("size", ""),
                         "Status": f"CLOSED {r.get('exit_date', '')} · pnl {r.get('pnl_pct', '')}%"})
        st.dataframe(pd.DataFrame(rows) if rows else pd.DataFrame(),
                     width="stretch", hide_index=True)
        if not rows:
            st.markdown("_Log exists but is empty._")
    else:
        st.markdown("_No log yet — log fills with "
                    "`python scripts/paper_log.py open <LEG> --date ... --price ... --size ...`._")

    st.divider()

    st.markdown("##### Execution Status")
    col1, col2 = st.columns(2)
    with col1:
        st.markdown("##### Next Windows")
        if cal:
            today = date.today()
            lines = []
            for w in cal:
                days = ""
                try:
                    d = datetime.strptime(w["entry"], "%Y-%m-%d").date()
                    days = f" ({max(0, (d - today).days)}d away)" if d >= today else ""
                except ValueError:
                    pass
                lines.append(f"- **{w['leg']}**: {w['entry']} → {w['exit']} "
                             f"[{w['status']}]{days}")
            card_start("blue")
            st.markdown("\n".join(lines))
            card_end()
        else:
            st.markdown("_No windows._")
    with col2:
        st.markdown("##### Gateway")
        card_start("green" if ibkr.get("reachable") else "red")
        st.markdown(
            f"**{ibkr.get('host')}:{ibkr.get('port')}** — "
            f"{'REACHABLE (2s probe)' if ibkr.get('reachable') else 'UNREACHABLE: ' + str(ibkr.get('error', ''))[:60]}"
        )
        card_end()
        card_start("amber")
        st.markdown(
            "Execution route: **manual T212 practice mode** or the IBKR paper "
            "script (`scripts/seasonal_paper_ibkr.py`) once the gateway is up. "
            "3 trades a year — automation is optional."
        )
        card_end()
