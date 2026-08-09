"""ATLAS Terminal — dourmouse edition (v8.0, real data).

The terminal reads the REAL forex-data research pipeline through
``data.live()`` (which wraps ``dourmouse.forex_ops`` + the validation
report). When ``FOREX_DATA_PATH`` is unset, modules show an honest
NOT CONFIGURED state — never fabricated numbers (Rule 2.2).

Run:  streamlit run atlas_terminal.py
"""

from datetime import datetime

import streamlit as st

from ui.styles import inject_custom_css, render_status_pill, render_kpi_row
from ui.command_center import render_command_center
from ui.opportunity_radar import render_opportunity_radar
from ui.research_center import render_research_center
from ui.strategy_lab import render_strategy_lab
from ui.portfolio_intelligance import render_portfolio_intelligence
from ui.risk_center import render_risk_center
from ui.alpha_center import render_alpha_center
from ui.live_news import render_live_news
from ui.ai_analyst import render_ai_analyst
from ui.execution_center import render_execution_center
from data import live

st.set_page_config(page_title="ATLAS Terminal", layout="wide", initial_sidebar_state="expanded")
inject_custom_css()

# ===========================================================
# REAL SNAPSHOT (loaded once per interaction)
# ===========================================================
LIVE = live()
PIPE = LIVE["pipeline"]
VAL = LIVE["validation"]
CAL = LIVE["calendar"]
EVS = LIVE["events"]
PAPER = LIVE["paper"]
IBKR = LIVE["ibkr"]
CONFIGURED = PIPE.get("configured", False)

# ===========================================================
# SESSION STATE (derived from real data, honest defaults)
# ===========================================================
if "execution_active" not in st.session_state:
    st.session_state.execution_active = IBKR.get("reachable", False)
if "paper_balance" not in st.session_state:
    st.session_state.paper_balance = 100.0  # the validated $100 paper account
if "market_regime" not in st.session_state:
    st.session_state.market_regime = "Seasonal (hog cycle + corn harvest)" if CONFIGURED else "UNCONFIGURED"
if "atlas_score" not in st.session_state:
    st.session_state.atlas_score = 0
if "confidence" not in st.session_state:
    st.session_state.confidence = 0

# ===========================================================
# MODULE REGISTRY
# ===========================================================
MODULES = {
    "Command Center": render_command_center,
    "Opportunity Radar": render_opportunity_radar,
    "Research": render_research_center,
    "Strategy Lab": render_strategy_lab,
    "Portfolio": render_portfolio_intelligence,
    "Risk": render_risk_center,
    "Alpha": render_alpha_center,
    "News": render_live_news,
    "AI Analyst": render_ai_analyst,
    "Execution": render_execution_center,
}

# ===========================================================
# SIDEBAR
# ===========================================================
with st.sidebar:
    st.markdown(
        '<div style="font-family:\'Inter\',sans-serif;font-weight:800;'
        'font-size:1.05rem;letter-spacing:0.04em;color:var(--text-primary);">ATLAS</div>'
        '<div style="color:var(--text-muted);font-size:0.72rem;letter-spacing:0.03em;'
        'margin-bottom:10px;">DECISION INTELLIGENCE PLATFORM</div>',
        unsafe_allow_html=True,
    )
    render_status_pill(
        "PIPELINE ONLINE" if CONFIGURED else "NOT CONFIGURED",
        "green" if CONFIGURED else "red",
    )

    st.markdown('<div class="nav-eyebrow">Modules</div>', unsafe_allow_html=True)
    selected_module = st.radio("Modules", list(MODULES.keys()), label_visibility="collapsed")

    st.markdown('<div class="nav-eyebrow">Session</div>', unsafe_allow_html=True)
    ticker = st.text_input("Search", "HE" if CONFIGURED else "—")
    timeframe = st.selectbox("Timeframe", ["1m", "5m", "15m", "1H", "4H", "1D"], index=5)
    venue = st.selectbox("Exchange", ["CME FUTURES", "SPOT FX", "CFD"])

    st.divider()
    st.markdown(
        f'<div style="font-family:\'JetBrains Mono\',monospace;font-size:0.7rem;'
        f'color:var(--text-muted);">MODE&nbsp;&nbsp;PAPER TRADING · $100</div>',
        unsafe_allow_html=True,
    )

# ===========================================================
# TOP TICKER STRIP — real pipeline facts, never quotes
# ===========================================================
if CONFIGURED:
    next_win = next((w for w in CAL if w["status"] == "NOW OPEN"), CAL[0] if CAL else None)
    strip_inner = (
        f"<b>ATLAS</b><span class='tkr-sep'>|</span>"
        f"DATA <span class='tkr-up'>{PIPE['total_bars']:,} bars</span>"
        f"<span class='tkr-sep'>|</span>"
        f"FX {PIPE['fx_pairs']} pairs"
        f"<span class='tkr-sep'>|</span>"
        f"COMMODITIES {PIPE['commodities']}"
        f"<span class='tkr-sep'>|</span>"
        + (f"TRADE {next_win['leg']} {next_win['entry']}→{next_win['exit']} [{next_win['status']}]"
           if next_win else "NO TRADE WINDOW")
        + f"<span class='tkr-sep'>|</span>"
        + ("IBKR UP" if IBKR.get("reachable") else "IBKR DOWN")
    )
else:
    strip_inner = (
        f"<b>ATLAS</b><span class='tkr-sep'>|</span>"
        f"<span style='color:var(--text-muted);'>SET FOREX_DATA_PATH TO ENABLE REAL DATA</span>"
    )

st.markdown(f'<div class="ticker-bar">{strip_inner}</div>', unsafe_allow_html=True)

# ===========================================================
# CONTEXT HEADER
# ===========================================================
left, right = st.columns([4, 1])
with left:
    st.markdown(
        f'<div style="font-family:\'JetBrains Mono\',monospace;font-size:0.82rem;'
        f'color:var(--text-secondary);display:flex;gap:20px;flex-wrap:wrap;">'
        f'<span><span style="color:var(--text-muted);">UTC</span>&nbsp; '
        f'{datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")}</span>'
        f'<span><span style="color:var(--text-muted);">SYMBOL</span>&nbsp; {ticker}</span>'
        f'<span><span style="color:var(--text-muted);">VENUE</span>&nbsp; {venue}</span>'
        f'<span><span style="color:var(--text-muted);">TF</span>&nbsp; {timeframe}</span>'
        + (f'<span><span style="color:var(--text-muted);">ROOT</span>&nbsp; {PIPE.get("root", "")}</span>'
           if CONFIGURED else "")
        + "</div>",
        unsafe_allow_html=True,
    )
with right:
    render_status_pill("ONLINE" if CONFIGURED else "OFFLINE", "green" if CONFIGURED else "red")

st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)

# ===========================================================
# ACTIVE MODULE
# ===========================================================
MODULES[selected_module]()

# ===========================================================
# FOOTER — real KPIs
# ===========================================================
st.divider()

if CONFIGURED:
    paper_open = len(PAPER.get("open_positions", [])) if PAPER.get("log_file") else 0
    realized = PAPER.get("realised_pnl_usd", 0.0)
    core = VAL.get("core", {})
    footer_kpis = [
        {"label": "Paper Account", "value": "$100", "sub": "validated start", "tone": "blue"},
        {"label": "Realised P&L", "value": f"${realized:.2f}", "tone": "green" if realized >= 0 else "red"},
        {"label": "Open Positions", "value": str(paper_open), "tone": "blue"},
        {"label": "Core Sharpe", "value": f"{core.get('sharpe', '—')}", "tone": "green"},
    ]
else:
    footer_kpis = [
        {"label": "Paper Account", "value": "$100", "tone": "blue"},
        {"label": "Pipeline", "value": "NOT CONFIGURED", "tone": "red"},
        {"label": "Open Positions", "value": "—", "tone": "blue"},
        {"label": "Core Sharpe", "value": "—", "tone": "blue"},
    ]
render_kpi_row(footer_kpis)

st.markdown(
    '<div style="text-align:center;color:var(--text-muted);font-size:0.72rem;'
    'letter-spacing:0.03em;margin-top:6px;">'
    'ATLAS QUANTITATIVE INTELLIGENCE PLATFORM &nbsp;&#183;&nbsp; '
    'VALIDATED SEASONAL STRATEGY &nbsp;&#183;&nbsp; REAL PIPELINE DATA '
    '&nbsp;&#183;&nbsp; PAPER TRADING &nbsp;&#183;&nbsp; v8.0 · dourmouse'
    '</div>',
    unsafe_allow_html=True,
)
