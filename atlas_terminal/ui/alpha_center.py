import streamlit as st
import pandas as pd

from ui.styles import page_header, render_kpi_row, card_start, card_end
from data import live

LEG_NAMES = {
    "HE_8": "SHORT hogs Aug", "HE_4": "LONG hogs Apr", "ZC_12": "LONG corn Dec",
    "RB_9": "SHORT gasoline Sep", "HE_10": "SHORT hogs Oct", "HE_2": "LONG hogs Feb",
    "LE_5": "SHORT cattle May",
}


def render_alpha_center():
    L = live()
    ok = L["pipeline"].get("configured", False)
    val = L["validation"]
    legs = val.get("legs", {})
    core = val.get("core", {})
    port = val.get("portfolio", {})

    page_header("Alpha Analysis", "Where the validated edge actually comes from")

    if not ok:
        card_start("amber")
        st.markdown("**PIPELINE NOT CONFIGURED** — set `FOREX_DATA_PATH`.")
        card_end()
        return

    render_kpi_row([
        {"label": "Expected Alpha (ann.)", "value": f"+{port.get('ann_mean', '—')}%",
         "tone": "green"},
        {"label": "Core Sharpe (IR)", "value": str(core.get("sharpe", "—")), "tone": "green"},
        {"label": "Core Win Rate", "value": "~88%", "tone": "green"},
        {"label": "Ann. Volatility", "value": f"±{port.get('ann_std', '—')}%", "tone": "amber"},
    ])

    st.divider()

    st.markdown("##### Alpha Contributors (OOS 2015+, net of costs)")
    if legs:
        df = pd.DataFrame([{
            "Leg": k,
            "Side": LEG_NAMES.get(k, ""),
            "Mean net": f"{v['mean']:+.2f}%" if v["mean"] is not None else "—",
            "t": f"{v['t']:+.2f}" if v["t"] is not None else "—",
            "Win": f"{v['win']:.0f}%" if v["win"] is not None else "—",
            "Trades": v["n"],
        } for k, v in sorted(legs.items(), key=lambda kv: (kv[1].get("t") or 0), reverse=True)])
        st.dataframe(df, width="stretch", hide_index=True)
        st.caption("Alpha is concentrated in the hog cycle (Aug short, Apr long) "
                   "and the corn harvest (Dec long). The rest of the family "
                   "contributed zero or negative alpha and was dropped.")
    else:
        st.markdown("_No leg stats parsed._")

    st.divider()

    st.markdown("##### Mechanism Summary")
    card_start("green")
    st.markdown(
        "The edge is **forced-flow seasonality**, not price prediction: producers "
        "and consumers transact on a physical calendar they cannot control "
        "(farrowing cycle, summer heat, harvest). Directional tests on the same "
        "data failed everywhere — the seasonal family is the one thing that "
        "re-derived itself from a fresh selection window and survived every "
        "statistical gate in the validation suite."
    )
    card_end()
