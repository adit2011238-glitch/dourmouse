import streamlit as st
import pandas as pd

from ui.styles import page_header, render_kpi_row, card_start, card_end
from data import live


def render_strategy_lab():
    L = live()
    ok = L["pipeline"].get("configured", False)
    val = L["validation"]
    legs = val.get("legs", {})
    core = val.get("core", {})
    all60 = val.get("all", {})

    page_header("Strategy Lab", "The validated commodity-seasonal system")

    if not ok:
        card_start("amber")
        st.markdown("**PIPELINE NOT CONFIGURED** — set `FOREX_DATA_PATH`.")
        card_end()
        return

    col1, col2 = st.columns(2)
    with col1:
        st.markdown("##### Strategy Generation")
        st.markdown(
            "Strategies are produced by the **research pipeline**, not generated "
            "in the UI. The mechanism family (hog cycle + corn harvest) was "
            "optimised in-sample (2000–2014) and validated by a five-stage "
            "no-lookahead suite. Generating new legs in the UI would be "
            "unvalidated — this terminal only trades what passed."
        )
        st.caption("The 7-leg family was screened; the walk-forward killed "
                   "gasoline, cattle, hogs-Oct and hogs-Feb. Only the core three trade.")

    with col2:
        st.markdown("##### Current Champion")
        render_kpi_row([
            {"label": "Strategy", "value": "Seasonal Core", "tone": "blue"},
            {"label": "Sharpe", "value": str(core.get("sharpe", "—")), "tone": "green"},
        ])
        render_kpi_row([
            {"label": "Win Rate (core)", "value": "~88%", "tone": "green"},
            {"label": "Max Drawdown", "value": f"{core.get('maxdd', '—')}%", "tone": "red"},
        ])

    st.divider()

    st.markdown("##### Validated Legs (OOS 2015+, net of real fees)")
    if legs:
        df = pd.DataFrame([{
            "Leg": k,
            "Trades": v["n"],
            "Mean net": f"{v['mean']:+.2f}%" if v["mean"] is not None else "—",
            "Median": f"{v['median']:+.2f}%" if v["median"] is not None else "—",
            "Std": f"{v['std']:.2f}%" if v["std"] is not None else "—",
            "t": f"{v['t']:+.2f}" if v["t"] is not None else "—",
            "Win": f"{v['win']:.0f}%" if v["win"] is not None else "—",
        } for k, v in sorted(legs.items(), key=lambda kv: (kv[1].get("t") or 0), reverse=True)])
        st.dataframe(df, width="stretch", hide_index=True)
        if all60.get("mean") is not None:
            st.caption(f"All 60 holdout trades: mean {all60['mean']:+.2f}%, "
                       f"t = {all60['t']:+.2f}, win {all60['win']:.0f}%.")
    else:
        st.markdown("_No leg stats parsed._")
