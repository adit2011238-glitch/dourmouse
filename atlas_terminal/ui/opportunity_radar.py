import streamlit as st
import pandas as pd

from ui.styles import page_header, render_kpi_row, card_start, card_end
from data import live

LEG_SIDES = {
    "HE_8": "SHORT lean hogs, August", "HE_4": "LONG lean hogs, April",
    "RB_9": "SHORT gasoline, September", "HE_10": "SHORT lean hogs, October",
    "HE_2": "LONG lean hogs, February", "ZC_12": "LONG corn, December",
    "LE_5": "SHORT live cattle, May",
}


def render_opportunity_radar():
    L = live()
    ok = L["pipeline"].get("configured", False)
    cal = L["calendar"]
    evs = L["events"]
    val = L["validation"]
    legs = val.get("legs", {})

    page_header("Opportunity Radar", "Validated trade windows and upcoming catalysts")

    if not ok:
        card_start("amber")
        st.markdown("**PIPELINE NOT CONFIGURED** — set `FOREX_DATA_PATH`.")
        card_end()
        return

    st.markdown("##### Trade Windows (live calendar)")
    if cal:
        df = pd.DataFrame([{
            "Leg": w["leg"],
            "Side": LEG_SIDES.get(w["leg"], ""),
            "Entry": w["entry"],
            "Exit": w["exit"],
            "Status": w["status"],
        } for w in cal])
        st.dataframe(df, width="stretch", hide_index=True)
    else:
        st.markdown("_Calendar returned no windows._")

    st.divider()

    st.markdown("##### Upcoming Catalysts (events archive, next 72h)")
    if evs:
        ev_df = pd.DataFrame([{
            "When (UTC)": e["when"],
            "Impact": e["impact"],
            "Event": e["title"],
            "Country": e["country"],
        } for e in evs])
        st.dataframe(ev_df, width="stretch", hide_index=True)
    else:
        st.markdown("_No upcoming high/medium-impact events in the window._")

    st.divider()

    if cal:
        st.markdown("##### Inspect a Window")
        pick = st.selectbox("Select window", [w["leg"] for w in cal])
        win = next(w for w in cal if w["leg"] == pick)
        stat = legs.get(pick, {})
        card_start("green" if win["status"] == "NOW OPEN" else "blue")
        st.markdown(
            f"**{pick} — {LEG_SIDES.get(pick, '')}** &nbsp;&#183;&nbsp; "
            f"window {win['entry']} → {win['exit']} [{win['status']}]\n\n"
            + (f"- OOS: {stat.get('n', '—')} trades, mean net "
               f"{stat.get('mean', '—')}% (t = {stat.get('t', '—')}), "
               f"win {stat.get('win', '—')}%\n"
               f"- Enter at month open, exit at month close, size by volatility"
               if stat else "- No OOS stats parsed for this leg.")
        )
        card_end()
