"""
styles.py
=====================================================================
Premium institutional trading terminal theme for Streamlit.
Inspired by Bloomberg Terminal, BlackRock Aladdin, TradingView Desktop,
and Goldman Sachs Marquee.

Usage
-----
    import streamlit as st
    from styles import inject_custom_css

    st.set_page_config(page_title="Terminal", layout="wide")
    inject_custom_css()

Optional helpers (status pills / KPI cards) are also exposed for
convenience — see bottom of file.
=====================================================================
"""

import streamlit as st


# ---------------------------------------------------------------------------
# Design tokens (kept here so they can be reused / imported elsewhere)
# ---------------------------------------------------------------------------

COLORS = {
    "bg": "#0F1116",
    "bg_alt": "#14171F",
    "surface": "#161A23",
    "surface_alt": "#1B202B",
    "border": "rgba(255, 255, 255, 0.08)",
    "border_strong": "rgba(255, 255, 255, 0.14)",
    "text_primary": "#E9EBF1",
    "text_secondary": "#9BA3B4",
    "text_muted": "#5F6779",
    "green": "#22C55E",
    "green_soft": "rgba(34, 197, 94, 0.12)",
    "red": "#EF4444",
    "red_soft": "rgba(239, 68, 68, 0.12)",
    "amber": "#F59E0B",
    "amber_soft": "rgba(245, 158, 11, 0.12)",
    "blue": "#3B82F6",
    "blue_soft": "rgba(59, 130, 246, 0.12)",
}


# ---------------------------------------------------------------------------
# Main CSS block
# ---------------------------------------------------------------------------

_CUSTOM_CSS = """
<style>

/* =====================================================================
   1. FONTS & GLOBAL VARIABLES
===================================================================== */
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600;700&display=swap');

:root {
    --bg: #0F1116;
    --bg-alt: #14171F;
    --surface: rgba(22, 26, 35, 0.72);
    --surface-solid: #161A23;
    --surface-alt: rgba(27, 32, 43, 0.72);
    --border: rgba(255, 255, 255, 0.08);
    --border-strong: rgba(255, 255, 255, 0.16);
    --text-primary: #E9EBF1;
    --text-secondary: #9BA3B4;
    --text-muted: #5F6779;

    --green: #22C55E;
    --green-soft: rgba(34, 197, 94, 0.12);
    --green-glow: rgba(34, 197, 94, 0.35);
    --red: #EF4444;
    --red-soft: rgba(239, 68, 68, 0.12);
    --red-glow: rgba(239, 68, 68, 0.35);
    --amber: #F59E0B;
    --amber-soft: rgba(245, 158, 11, 0.12);
    --amber-glow: rgba(245, 158, 11, 0.35);
    --blue: #3B82F6;
    --blue-soft: rgba(59, 130, 246, 0.12);
    --blue-glow: rgba(59, 130, 246, 0.35);

    --radius: 12px;
    --radius-sm: 8px;
    --radius-lg: 16px;

    --shadow-sm: 0 2px 8px rgba(0, 0, 0, 0.25);
    --shadow-md: 0 8px 24px rgba(0, 0, 0, 0.35);
    --shadow-lg: 0 16px 48px rgba(0, 0, 0, 0.45);
    --shadow-glow-blue: 0 0 0 1px rgba(59, 130, 246, 0.25), 0 8px 24px rgba(59, 130, 246, 0.15);

    --transition-fast: 120ms cubic-bezier(0.4, 0, 0.2, 1);
    --transition-med: 220ms cubic-bezier(0.4, 0, 0.2, 1);
}

/* =====================================================================
   2. GLOBAL RESET / BASE
===================================================================== */
html, body, [class*="css"] {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    color: var(--text-primary);
}

.stApp {
    background:
        radial-gradient(circle at 15% 0%, rgba(59, 130, 246, 0.06) 0%, transparent 45%),
        radial-gradient(circle at 85% 100%, rgba(34, 197, 94, 0.05) 0%, transparent 45%),
        var(--bg);
    background-attachment: fixed;
}

/* Tighten default block spacing for a dense terminal feel */
.block-container {
    padding-top: 1.4rem !important;
    padding-bottom: 1.4rem !important;
    padding-left: 2rem !important;
    padding-right: 2rem !important;
    max-width: 100% !important;
}

h1, h2, h3, h4, h5, h6 {
    font-family: 'Inter', sans-serif;
    font-weight: 700;
    color: var(--text-primary);
    letter-spacing: -0.02em;
    margin-bottom: 0.4rem !important;
}

h1 { font-size: 1.65rem !important; }
h2 { font-size: 1.3rem !important; }
h3 { font-size: 1.08rem !important; }

p, span, label, div {
    letter-spacing: -0.005em;
}

code, pre, .stCodeBlock, .stCode {
    font-family: 'JetBrains Mono', monospace !important;
}

hr {
    border-color: var(--border) !important;
    margin: 0.75rem 0 !important;
}

/* =====================================================================
   3. HIDE STREAMLIT BRANDING / CHROME
===================================================================== */
#MainMenu { visibility: hidden; }
footer { visibility: hidden; }
header[data-testid="stHeader"] {
    background: transparent;
    height: 0;
}
div[data-testid="stToolbar"] { display: none; }
div[data-testid="stDecoration"] { display: none; }
div[data-testid="stStatusWidget"] { display: none; }
a[href*="streamlit.io/cloud"],
.viewerBadge_container__1QSob,
.styles_viewerBadge__1yB5_,
.viewerBadge_link__1S137,
.viewerBadge_text__1JaDK { display: none !important; }
#stDecoration { display: none; }

/* =====================================================================
   4. CUSTOM SCROLLBARS
===================================================================== */
::-webkit-scrollbar {
    width: 10px;
    height: 10px;
}
::-webkit-scrollbar-track {
    background: var(--bg-alt);
}
::-webkit-scrollbar-thumb {
    background: linear-gradient(180deg, #2A3040, #1E2330);
    border-radius: 8px;
    border: 2px solid var(--bg-alt);
}
::-webkit-scrollbar-thumb:hover {
    background: linear-gradient(180deg, #3B82F6, #2563EB);
}
* {
    scrollbar-width: thin;
    scrollbar-color: #2A3040 var(--bg-alt);
}

/* =====================================================================
   5. GLASSMORPHISM CARDS / CONTAINERS
===================================================================== */
div[data-testid="stVerticalBlockBorderWrapper"],
div[data-testid="stExpander"] {
    background: var(--surface);
    backdrop-filter: blur(18px) saturate(140%);
    -webkit-backdrop-filter: blur(18px) saturate(140%);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    box-shadow: var(--shadow-sm);
    transition: border-color var(--transition-med), box-shadow var(--transition-med), transform var(--transition-med);
}

div[data-testid="stVerticalBlockBorderWrapper"]:hover {
    border-color: var(--border-strong);
    box-shadow: var(--shadow-md);
}

/* Generic content "card" utility class you can wrap markdown in */
.terminal-card {
    background: var(--surface);
    backdrop-filter: blur(18px) saturate(140%);
    -webkit-backdrop-filter: blur(18px) saturate(140%);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 18px 20px;
    box-shadow: var(--shadow-sm);
    transition: all var(--transition-med);
}
.terminal-card:hover {
    border-color: var(--border-strong);
    box-shadow: var(--shadow-md);
    transform: translateY(-2px);
}

/* =====================================================================
   6. SIDEBAR
===================================================================== */
section[data-testid="stSidebar"] {
    background: linear-gradient(180deg, #12141C 0%, #0D0F15 100%);
    border-right: 1px solid var(--border);
}
section[data-testid="stSidebar"] > div {
    padding-top: 1.2rem;
}
section[data-testid="stSidebar"] .block-container {
    padding-left: 1.1rem !important;
    padding-right: 1.1rem !important;
}
section[data-testid="stSidebar"] h1,
section[data-testid="stSidebar"] h2,
section[data-testid="stSidebar"] h3 {
    color: var(--text-primary);
    font-size: 0.95rem !important;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    opacity: 0.85;
}
section[data-testid="stSidebar"] label {
    color: var(--text-secondary) !important;
    font-size: 0.82rem !important;
    font-weight: 500;
}
section[data-testid="stSidebar"] hr {
    border-color: var(--border) !important;
}

/* =====================================================================
   7. BUTTONS
===================================================================== */
.stButton > button,
.stDownloadButton > button,
.stFormSubmitButton > button {
    background: linear-gradient(180deg, #1E2430 0%, #171B24 100%);
    color: var(--text-primary);
    border: 1px solid var(--border-strong);
    border-radius: var(--radius-sm);
    padding: 0.5rem 1.1rem;
    font-weight: 600;
    font-size: 0.85rem;
    letter-spacing: 0.01em;
    box-shadow: var(--shadow-sm);
    transition: all var(--transition-fast);
}
.stButton > button:hover,
.stDownloadButton > button:hover,
.stFormSubmitButton > button:hover {
    border-color: var(--blue);
    color: #fff;
    box-shadow: var(--shadow-glow-blue);
    transform: translateY(-1px);
}
.stButton > button:active {
    transform: translateY(0px) scale(0.98);
}

/* Primary buttons (kind="primary") */
.stButton > button[kind="primary"],
.stFormSubmitButton > button[kind="primary"] {
    background: linear-gradient(135deg, #3B82F6 0%, #2563EB 100%);
    border: 1px solid rgba(59, 130, 246, 0.5);
    color: #fff;
}
.stButton > button[kind="primary"]:hover {
    box-shadow: 0 0 0 1px rgba(59, 130, 246, 0.6), 0 8px 28px rgba(59, 130, 246, 0.4);
    filter: brightness(1.08);
}

/* =====================================================================
   8. TABS
===================================================================== */
.stTabs [data-baseweb="tab-list"] {
    gap: 4px;
    background: var(--surface-alt);
    padding: 5px;
    border-radius: var(--radius-sm);
    border: 1px solid var(--border);
}
.stTabs [data-baseweb="tab"] {
    height: 38px;
    border-radius: 7px;
    color: var(--text-secondary);
    font-weight: 600;
    font-size: 0.83rem;
    background: transparent;
    transition: all var(--transition-fast);
}
.stTabs [data-baseweb="tab"]:hover {
    color: var(--text-primary);
    background: rgba(255, 255, 255, 0.04);
}
.stTabs [aria-selected="true"] {
    background: linear-gradient(135deg, #3B82F6 0%, #2563EB 100%) !important;
    color: #fff !important;
    box-shadow: 0 4px 14px rgba(59, 130, 246, 0.35);
}
.stTabs [data-baseweb="tab-highlight"] { display: none; }
.stTabs [data-baseweb="tab-border"] { display: none; }

/* =====================================================================
   9. INPUTS / SELECTS / SLIDERS
===================================================================== */
.stTextInput input,
.stNumberInput input,
.stDateInput input,
.stTimeInput input,
.stTextArea textarea {
    background: var(--surface-alt) !important;
    border: 1px solid var(--border) !important;
    border-radius: var(--radius-sm) !important;
    color: var(--text-primary) !important;
    font-size: 0.86rem !important;
    transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
}
.stTextInput input:focus,
.stNumberInput input:focus,
.stTextArea textarea:focus {
    border-color: var(--blue) !important;
    box-shadow: 0 0 0 3px var(--blue-soft) !important;
}

div[data-baseweb="select"] > div {
    background: var(--surface-alt) !important;
    border: 1px solid var(--border) !important;
    border-radius: var(--radius-sm) !important;
    color: var(--text-primary) !important;
}
div[data-baseweb="select"] > div:hover {
    border-color: var(--border-strong) !important;
}

.stSlider [data-baseweb="slider"] > div > div {
    background: linear-gradient(90deg, #3B82F6, #22C55E) !important;
}
.stSlider [role="slider"] {
    background: #fff !important;
    box-shadow: 0 0 0 4px var(--blue-soft) !important;
}

.stCheckbox label, .stRadio label { color: var(--text-secondary) !important; }

/* =====================================================================
   10. METRICS
===================================================================== */
div[data-testid="stMetric"] {
    background: var(--surface);
    backdrop-filter: blur(16px);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 14px 18px 12px 18px;
    box-shadow: var(--shadow-sm);
    transition: all var(--transition-med);
    position: relative;
    overflow: hidden;
}
div[data-testid="stMetric"]::before {
    content: "";
    position: absolute;
    top: 0; left: 0;
    width: 3px; height: 100%;
    background: linear-gradient(180deg, var(--blue), transparent);
}
div[data-testid="stMetric"]:hover {
    border-color: var(--border-strong);
    box-shadow: var(--shadow-md);
    transform: translateY(-2px);
}
div[data-testid="stMetricLabel"] {
    color: var(--text-secondary) !important;
    font-size: 0.72rem !important;
    font-weight: 600 !important;
    text-transform: uppercase;
    letter-spacing: 0.06em;
}
div[data-testid="stMetricValue"] {
    color: var(--text-primary) !important;
    font-family: 'JetBrains Mono', monospace !important;
    font-size: 1.55rem !important;
    font-weight: 700 !important;
}
div[data-testid="stMetricDelta"] {
    font-family: 'JetBrains Mono', monospace !important;
    font-size: 0.8rem !important;
    font-weight: 600 !important;
}

/* =====================================================================
   11. KPI CARD (custom utility — use with the render_kpi_card helper)
===================================================================== */
.kpi-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 12px;
    margin-bottom: 14px;
}
.kpi-card {
    background: var(--surface);
    backdrop-filter: blur(16px) saturate(140%);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 16px 18px;
    box-shadow: var(--shadow-sm);
    transition: all var(--transition-med);
    position: relative;
    overflow: hidden;
}
.kpi-card:hover {
    transform: translateY(-3px);
    border-color: var(--border-strong);
    box-shadow: var(--shadow-md);
}
.kpi-card .kpi-label {
    color: var(--text-secondary);
    font-size: 0.7rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.07em;
    margin-bottom: 6px;
}
.kpi-card .kpi-value {
    font-family: 'JetBrains Mono', monospace;
    font-size: 1.5rem;
    font-weight: 700;
    color: var(--text-primary);
    line-height: 1.1;
}
.kpi-card .kpi-sub {
    margin-top: 6px;
    font-size: 0.78rem;
    font-weight: 600;
    font-family: 'JetBrains Mono', monospace;
}
.kpi-card.accent-green::before,
.kpi-card.accent-red::before,
.kpi-card.accent-amber::before,
.kpi-card.accent-blue::before {
    content: "";
    position: absolute;
    top: 0; left: 0;
    width: 3px; height: 100%;
}
.kpi-card.accent-green::before { background: linear-gradient(180deg, var(--green), transparent); }
.kpi-card.accent-red::before   { background: linear-gradient(180deg, var(--red), transparent); }
.kpi-card.accent-amber::before { background: linear-gradient(180deg, var(--amber), transparent); }
.kpi-card.accent-blue::before  { background: linear-gradient(180deg, var(--blue), transparent); }

.kpi-card.accent-green .kpi-sub { color: var(--green); }
.kpi-card.accent-red .kpi-sub   { color: var(--red); }
.kpi-card.accent-amber .kpi-sub { color: var(--amber); }
.kpi-card.accent-blue .kpi-sub  { color: var(--blue); }

/* =====================================================================
   12. STATUS PILLS
===================================================================== */
.status-pill {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 3px 11px;
    border-radius: 999px;
    font-size: 0.72rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    border: 1px solid transparent;
}
.status-pill::before {
    content: "";
    width: 6px;
    height: 6px;
    border-radius: 50%;
    box-shadow: 0 0 6px currentColor;
}
.status-pill.pill-green {
    background: var(--green-soft);
    color: var(--green);
    border-color: rgba(34, 197, 94, 0.3);
}
.status-pill.pill-red {
    background: var(--red-soft);
    color: var(--red);
    border-color: rgba(239, 68, 68, 0.3);
}
.status-pill.pill-amber {
    background: var(--amber-soft);
    color: var(--amber);
    border-color: rgba(245, 158, 11, 0.3);
}
.status-pill.pill-blue {
    background: var(--blue-soft);
    color: var(--blue);
    border-color: rgba(59, 130, 246, 0.3);
}
.status-pill.pill-neutral {
    background: rgba(255, 255, 255, 0.06);
    color: var(--text-secondary);
    border-color: var(--border);
}

/* =====================================================================
   13. DATAFRAMES / TABLES
===================================================================== */
div[data-testid="stDataFrame"], div[data-testid="stTable"] {
    border: 1px solid var(--border);
    border-radius: var(--radius);
    overflow: hidden;
    box-shadow: var(--shadow-sm);
}
div[data-testid="stDataFrame"] * {
    font-family: 'JetBrains Mono', monospace !important;
    font-size: 0.82rem !important;
}
div[data-testid="stDataFrame"] [role="columnheader"] {
    background: var(--surface-solid) !important;
    color: var(--text-secondary) !important;
    font-weight: 700 !important;
    text-transform: uppercase;
    font-size: 0.7rem !important;
    letter-spacing: 0.05em;
    border-bottom: 1px solid var(--border-strong) !important;
}
div[data-testid="stDataFrame"] [role="row"]:hover {
    background: rgba(59, 130, 246, 0.06) !important;
}
div[data-testid="stDataFrame"] [role="gridcell"] {
    border-bottom: 1px solid var(--border) !important;
    color: var(--text-primary) !important;
}

table {
    border-collapse: collapse;
    width: 100%;
}
thead tr th {
    background: var(--surface-solid) !important;
    color: var(--text-secondary) !important;
    text-transform: uppercase;
    font-size: 0.7rem !important;
    letter-spacing: 0.05em;
    border-bottom: 1px solid var(--border-strong) !important;
}
tbody tr td {
    border-bottom: 1px solid var(--border) !important;
    color: var(--text-primary) !important;
}
tbody tr:hover td {
    background: rgba(59, 130, 246, 0.06) !important;
}

/* =====================================================================
   14. ALERTS / TOASTS / BADGES
===================================================================== */
div[data-testid="stAlert"] {
    border-radius: var(--radius-sm);
    border: 1px solid var(--border);
    backdrop-filter: blur(12px);
    font-size: 0.86rem;
}

/* =====================================================================
   15. EXPANDER
===================================================================== */
div[data-testid="stExpander"] summary {
    font-weight: 600;
    font-size: 0.88rem;
    color: var(--text-primary);
    padding: 4px 2px;
}
div[data-testid="stExpander"] summary:hover {
    color: var(--blue);
}

/* =====================================================================
   16. PROGRESS BAR
===================================================================== */
div[data-testid="stProgress"] > div > div {
    background: linear-gradient(90deg, var(--blue), var(--green)) !important;
    border-radius: 999px;
}
div[data-testid="stProgress"] > div {
    background: var(--surface-alt) !important;
    border-radius: 999px;
}

/* =====================================================================
   17. PLOTLY / CHART CONTAINERS
===================================================================== */
div[data-testid="stPlotlyChart"], .js-plotly-plot {
    border-radius: var(--radius);
    overflow: hidden;
}

/* =====================================================================
   18. MICRO ANIMATIONS
===================================================================== */
@keyframes pulseGlow {
    0%, 100% { box-shadow: 0 0 0 0 rgba(34, 197, 94, 0.4); }
    50% { box-shadow: 0 0 0 6px rgba(34, 197, 94, 0); }
}
.pulse-live { animation: pulseGlow 1.8s ease-in-out infinite; border-radius: 50%; }

@keyframes fadeSlideIn {
    from { opacity: 0; transform: translateY(6px); }
    to { opacity: 1; transform: translateY(0); }
}
.block-container > div { animation: fadeSlideIn 260ms ease-out; }

/* =====================================================================
   20. TICKER BAR
===================================================================== */
.ticker-bar {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 22px;
    background: var(--surface-solid);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 7px 16px;
    margin-bottom: 14px;
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.78rem;
    color: var(--text-secondary);
}
.ticker-bar b {
    font-family: 'Inter', sans-serif;
    color: var(--text-primary);
    letter-spacing: 0.04em;
    font-weight: 700;
    margin-right: 4px;
}
.ticker-bar .tkr-up { color: var(--green); font-weight: 600; }
.ticker-bar .tkr-down { color: var(--red); font-weight: 600; }
.ticker-bar .tkr-flat { color: var(--text-secondary); font-weight: 600; }
.ticker-bar .tkr-sep { color: var(--text-muted); }

/* =====================================================================
   21. SIDEBAR MODULE NAV (st.sidebar.radio restyled as a menu list)
===================================================================== */
section[data-testid="stSidebar"] .stRadio[data-testid="stWidgetLabel"] { display: none; }
section[data-testid="stSidebar"] div[role="radiogroup"] {
    gap: 1px;
}
section[data-testid="stSidebar"] div[role="radiogroup"] label {
    width: 100%;
    padding: 7px 10px 7px 12px;
    border-radius: 6px;
    border-left: 2px solid transparent;
    color: var(--text-secondary) !important;
    font-size: 0.78rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    transition: all var(--transition-fast);
}
section[data-testid="stSidebar"] div[role="radiogroup"] label:hover {
    background: rgba(255, 255, 255, 0.04);
    color: var(--text-primary) !important;
}
section[data-testid="stSidebar"] div[role="radiogroup"] label[data-checked="true"],
section[data-testid="stSidebar"] div[role="radiogroup"] label:has(input:checked) {
    background: var(--blue-soft);
    border-left: 2px solid var(--blue);
    color: var(--text-primary) !important;
}
section[data-testid="stSidebar"] div[role="radiogroup"] label > div:first-child {
    display: none;
}
section[data-testid="stSidebar"] div[role="radiogroup"] label p {
    font-size: 0.78rem !important;
    font-weight: 600 !important;
    letter-spacing: 0.05em;
}

.nav-eyebrow {
    color: var(--text-muted);
    font-size: 0.66rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    margin: 14px 0 4px 2px;
}

/* =====================================================================
   22. DATAFRAME ZEBRA STRIPING
===================================================================== */
div[data-testid="stDataFrame"] [role="row"]:nth-child(even) [role="gridcell"] {
    background: rgba(255, 255, 255, 0.015);
}

/* =====================================================================
   23. NEWS CARDS
===================================================================== */
.news-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 12px 16px;
    margin-bottom: 8px;
    transition: all var(--transition-fast);
}
.news-card:hover {
    border-color: var(--border-strong);
    background: var(--surface-alt);
}
.news-card .news-top {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 6px;
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.7rem;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.04em;
}
.news-card .news-headline {
    font-size: 0.92rem;
    font-weight: 600;
    color: var(--text-primary);
    text-decoration: none;
    line-height: 1.35;
}
.news-card .news-headline:hover { color: var(--blue); }
.news-card .news-meta {
    margin-top: 6px;
    display: flex;
    gap: 8px;
    align-items: center;
    font-size: 0.72rem;
    color: var(--text-secondary);
}

.sentiment-badge {
    display: inline-flex;
    align-items: center;
    padding: 1px 8px;
    border-radius: 4px;
    font-size: 0.64rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.05em;
}
.sentiment-badge.tone-green { background: var(--green-soft); color: var(--green); }
.sentiment-badge.tone-red { background: var(--red-soft); color: var(--red); }
.sentiment-badge.tone-amber { background: var(--amber-soft); color: var(--amber); }
.sentiment-badge.tone-neutral { background: rgba(255, 255, 255, 0.06); color: var(--text-secondary); }

/* =====================================================================
   24. PAGE HEADER
===================================================================== */
.page-header { margin-bottom: 4px; }
.page-header .page-eyebrow {
    color: var(--text-muted);
    font-size: 0.68rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.1em;
}
.page-header h1 {
    margin: 2px 0 0 0 !important;
    font-size: 1.35rem !important;
}
.page-header .page-subtitle {
    color: var(--text-secondary);
    font-size: 0.85rem;
    margin-top: 2px;
}

/* =====================================================================
   19. RESPONSIVE LAYOUT
===================================================================== */
@media (max-width: 1200px) {
    .block-container { padding-left: 1.2rem !important; padding-right: 1.2rem !important; }
    .kpi-grid { grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); }
}
@media (max-width: 768px) {
    .block-container { padding-left: 0.8rem !important; padding-right: 0.8rem !important; }
    div[data-testid="stMetricValue"] { font-size: 1.2rem !important; }
    .kpi-grid { grid-template-columns: 1fr 1fr; gap: 8px; }
    h1 { font-size: 1.3rem !important; }
}

</style>
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def inject_custom_css() -> None:
    """Inject the full premium terminal theme into the current Streamlit app.

    Call once, right after `st.set_page_config(...)`.
    """
    st.markdown(_CUSTOM_CSS, unsafe_allow_html=True)


def status_pill(label: str, tone: str = "neutral") -> str:
    """Return HTML for a status pill.

    tone: one of "green", "red", "amber", "blue", "neutral"
    """
    tone = tone if tone in {"green", "red", "amber", "blue", "neutral"} else "neutral"
    return f'<span class="status-pill pill-{tone}">{label}</span>'


def render_status_pill(label: str, tone: str = "neutral") -> None:
    """Render a status pill directly into the app."""
    st.markdown(status_pill(label, tone), unsafe_allow_html=True)


def kpi_card_html(label: str, value: str, sub: str = "", tone: str = "blue") -> str:
    """Return HTML for a single KPI card.

    tone: one of "green", "red", "amber", "blue"
    """
    tone = tone if tone in {"green", "red", "amber", "blue"} else "blue"
    sub_html = f'<div class="kpi-sub">{sub}</div>' if sub else ""
    return (
        f'<div class="kpi-card accent-{tone}">'
        f'<div class="kpi-label">{label}</div>'
        f'<div class="kpi-value">{value}</div>'
        f'{sub_html}'
        f'</div>'
    )


def render_kpi_row(cards: list) -> None:
    """Render a responsive row of KPI cards.

    `cards` is a list of dicts: {"label": str, "value": str, "sub": str, "tone": str}
    """
    html = '<div class="kpi-grid">'
    for c in cards:
        html += kpi_card_html(
            label=c.get("label", ""),
            value=c.get("value", ""),
            sub=c.get("sub", ""),
            tone=c.get("tone", "blue"),
        )
    html += "</div>"
    st.markdown(html, unsafe_allow_html=True)


def card_start(tone: str = "") -> None:
    """Open a `.terminal-card` div — pair with `card_end()` around content.

    tone: optional accent for the left border — one of "green", "red", "amber", "blue".
    """
    style = f' style="border-left:3px solid {COLORS[tone]};"' if tone in {"green", "red", "amber", "blue"} else ""
    st.markdown(f'<div class="terminal-card"{style}>', unsafe_allow_html=True)


def card_end() -> None:
    """Close a `.terminal-card` div opened with `card_start()`."""
    st.markdown("</div>", unsafe_allow_html=True)


def page_header(title: str, subtitle: str = "") -> None:
    """Render a compact page title block (replaces repeated title/caption/divider)."""
    sub_html = f'<div class="page-subtitle">{subtitle}</div>' if subtitle else ""
    st.markdown(
        f'<div class="page-header">'
        f'<h1>{title}</h1>'
        f'{sub_html}'
        f'</div>',
        unsafe_allow_html=True,
    )
    st.markdown('<hr/>', unsafe_allow_html=True)


def apply_terminal_theme(fig, height: int = 240):
    """Apply the institutional dark theme to a Plotly figure in place; returns it."""
    fig.update_layout(
        paper_bgcolor=COLORS["surface_alt"],
        plot_bgcolor=COLORS["surface_alt"],
        font=dict(family="Inter, sans-serif", color=COLORS["text_secondary"], size=12),
        margin=dict(l=12, r=12, t=32, b=12),
        height=height,
        showlegend=False,
    )
    fig.update_xaxes(gridcolor=COLORS["border"], zeroline=False, showline=False)
    fig.update_yaxes(gridcolor=COLORS["border"], zeroline=False, showline=False)
    return fig


# Backwards compatibility
def load_css():
    inject_custom_css()