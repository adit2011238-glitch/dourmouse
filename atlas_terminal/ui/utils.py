"""Shared formatting helpers for the ATLAS terminal UI."""


def format_currency(value: float, decimals: int = 0) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.{decimals}f}"


def format_percent(value: float, decimals: int = 2, signed: bool = True) -> str:
    sign = "+" if signed and value > 0 else ""
    return f"{sign}{value:.{decimals}f}%"


def pnl_tone(value: float) -> str:
    """Map a numeric value to a KPI/status tone: green (positive), red (negative), neutral."""
    if value > 0:
        return "green"
    if value < 0:
        return "red"
    return "blue"


def sentiment_badge(label: str) -> str:
    """Return HTML for a small sentiment badge (Bullish/Bearish/Neutral)."""
    tone = {
        "bullish": "green", "positive": "green",
        "bearish": "red", "negative": "red",
        "neutral": "amber",
    }.get(str(label).strip().lower(), "neutral")
    return f'<span class="sentiment-badge tone-{tone}">{label}</span>'
