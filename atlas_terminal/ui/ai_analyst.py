import streamlit as st

from ui.styles import page_header, render_kpi_row, render_status_pill, card_start, card_end
from data import live

LEG_NAMES = {
    "HE_8": "SHORT lean hogs, August", "HE_4": "LONG lean hogs, April",
    "ZC_12": "LONG corn, December", "RB_9": "SHORT gasoline, September",
    "HE_10": "SHORT lean hogs, October", "HE_2": "LONG lean hogs, February",
    "LE_5": "SHORT live cattle, May",
}


def render_ai_analyst():
    L = live()
    ok = L["pipeline"].get("configured", False)
    val = L["validation"]
    legs = val.get("legs", {})

    page_header("AI Analyst", "Evidence-based read of the validated strategy")

    if not ok:
        card_start("amber")
        st.markdown("**PIPELINE NOT CONFIGURED** — set `FOREX_DATA_PATH`.")
        card_end()
        return

    c1, c2 = st.columns([1, 2])
    with c1:
        leg = st.selectbox("Leg", list(LEG_NAMES.keys()))
    with c2:
        question = st.text_area(
            "Ask ATLAS", "Is this leg worth paper-trading, and what is the evidence?",
        )

    if st.button("Generate Analysis", type="primary"):
        stat = legs.get(leg, {})
        rec = "TRADE (paper)" if stat and (stat.get("t") or 0) >= 2.0 else "SKIP"
        confidence = min(99, int(abs(stat.get("t") or 0) * 17 + 30)) if stat else 0

        with st.spinner("Reading the validation record..."):
            summary_points = []
            if stat:
                summary_points.append(
                    f"OOS evidence: {stat['n']} blind trades, mean net {stat.get('mean', '—')}%, "
                    f"t = {stat.get('t', '—')}, win {stat.get('win', '—')}%"
                )
                summary_points.append("Net of real T212 costs (spread×2.5 + financing)")
                summary_points.append("Direction re-derived per walk-forward step from strictly prior data")
            else:
                summary_points.append("No out-of-sample stats parsed for this leg")
            risk_points = [
                "n ≈ 12 per leg — the t-stat rests on ~a dozen trades",
                "2015–2025 was seen by the earlier study; only 2026 is untouched",
                "Roll convention (continuous Yahoo series) unverified",
                "No SL/TP: the walk-forward rejected every stop variant tested",
            ]

        st.divider()

        top_left, top_right = st.columns([3, 1])
        with top_left:
            st.markdown(f"##### Investment Opinion &nbsp;&middot;&nbsp; {leg} — {LEG_NAMES.get(leg, '')}")
        with top_right:
            render_status_pill(rec, "green" if rec == "TRADE (paper)" else "red")

        render_kpi_row([
            {"label": "Evidence Confidence", "value": f"{confidence}%", "tone": "green"},
            {"label": "Holding Period", "value": "Full calendar month", "tone": "blue"},
            {"label": "Style", "value": "Enter open / exit close", "tone": "blue"},
        ])

        bull, bear = st.columns(2)
        with bull:
            st.markdown("##### Supporting Evidence")
            card_start("green")
            st.markdown("\n".join(f"- {p}" for p in summary_points))
            card_end()
        with bear:
            st.markdown("##### Risk Assessment")
            card_start("red")
            st.markdown("\n".join(f"- {p}" for p in risk_points))
            card_end()

        st.markdown("##### Strategy Explanation")
        card_start("blue")
        st.markdown(
            f"ATLAS recommends **{rec.lower()}** for **{leg}** ({LEG_NAMES.get(leg, '')}) "
            f"based on the deterministic five-stage validation record. "
            f"Enter at the month open, exit at the month close, size by volatility "
            f"under the $5/day cap — no stops, no targets (both were tested and "
            f"rejected by the strict walk-forward)."
        )
        card_end()

    st.caption("Deterministic rule-based read of the validation record — no LLM in "
               "this path yet. The evidence is the five-stage suite in "
               "`reports/VALIDATION_REPORT.md`.")
