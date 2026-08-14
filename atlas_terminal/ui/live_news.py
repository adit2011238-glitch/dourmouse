import os
import streamlit as st
import requests
from dotenv import load_dotenv

from ui.styles import page_header
from ui.utils import sentiment_badge
from data import live

# Load .env file (atlas_terminal/.env first, then repo .env)
load_dotenv()
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", ".env"))


def render_live_news():
    L = live()

    page_header("Live Market News", "Real headlines + the real economic calendar")

    st.markdown("##### Economic Calendar (real events archive, next 72h)")
    evs = L["events"]
    if evs:
        for e in evs:
            impact = str(e.get("impact", "")).lower()
            tone = {"high": "green", "medium": "amber"}.get(impact, "neutral")
            badge = f'<span class="sentiment-badge tone-{tone}">{e.get("impact", "?")}</span>'
            fc = e.get("forecast")
            pv = e.get("previous")
            meta = f"fcst={fc} prev={pv}" if fc is not None or pv is not None else ""
            st.markdown(
                f'<div class="news-card">'
                f'<div class="news-top"><span>{e.get("country", "")}</span>'
                f'<span>{e.get("when", "")}</span></div>'
                f'<div class="news-headline">{e.get("title", "")}</div>'
                f'<div class="news-meta">{badge} {meta}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )
    else:
        st.markdown("_No upcoming high/medium-impact events in the window._")

    st.divider()
    st.markdown("##### Headlines (MarketAux)")

    api_key = os.getenv("MARKETAUX_API_KEY")
    if not api_key:
        st.error("MARKETAUX_API_KEY not found in .env — headline feed disabled "
                 "(calendar above is real data and works without it).")
        return

    try:
        response = requests.get(
            "https://api.marketaux.com/v1/news/all",
            params={
                "api_token": api_key,
                "symbols": "EURUSD,GBPUSD,XAUUSD,XAGUSD,ZC,HE,CORN,HOG",
                "language": "en",
                "limit": 15,
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        articles = data.get("data", [])
        if not articles:
            st.warning("No news returned.")
            return
        for article in articles:
            headline = article.get("title", "")
            source = article.get("source", "")
            url = article.get("url", "")
            time_str = article.get("published_at", "")[:16].replace("T", " ")
            entities = article.get("entities") or []
            symbols = ", ".join(sorted({e.get("symbol") for e in entities if e.get("symbol")}))
            sentiment_html = ""
            scores = [e.get("sentiment_score") for e in entities if e.get("sentiment_score") is not None]
            if scores:
                avg_score = sum(scores) / len(scores)
                label = "Bullish" if avg_score > 0.15 else "Bearish" if avg_score < -0.15 else "Neutral"
                sentiment_html = sentiment_badge(label)
            headline_html = (
                f'<a class="news-headline" href="{url}" target="_blank">{headline}</a>'
                if url else f'<span class="news-headline">{headline}</span>'
            )
            st.markdown(
                f"""
<div class="news-card">
  <div class="news-top"><span>{source or "UNKNOWN SOURCE"}</span><span>{time_str}</span></div>
  {headline_html}
  <div class="news-meta">
    {f'<span>{symbols}</span>' if symbols else ''}
    {sentiment_html}
  </div>
</div>
""",
                unsafe_allow_html=True,
            )
    except Exception as e:
        st.error("MarketAux request failed.")
        st.exception(e)
