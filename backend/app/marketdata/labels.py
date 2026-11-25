"""Where the prices behind a report come from, in words. The same source as the app's top banner
(the active feed's ``simulated`` flag and ``status_info()``), so reports never call real prices
simulated or the reverse."""


def price_source(feed) -> dict:
    """{"simulated", "price_source", "price_note"} for the active feed (live, or a replay sandbox's)."""
    simulated = bool(getattr(feed, "simulated", True))
    name = getattr(feed, "name", "")
    try:
        info = feed.status_info() or {}
    except Exception:  # a report must not fail because a provider's status is unavailable
        info = {}
    if name == "replay":
        source = "simulated replay data" if simulated else "recorded market data (replay)"
    elif simulated:
        source = "simulated prices"
    else:
        exch = info.get("data_exchange")
        source = f"{info.get('data_source') or name} market data" + (f" ({exch})" if exch else "")
    if simulated:
        note = "All figures use simulated prices."
    else:
        note = f"Figures use {source}. Trading is still simulated: paper money only."
    return {"simulated": simulated, "price_source": source, "price_note": note}


def default_benchmark(feed, is_stock) -> str:
    """The benchmark shown when none is chosen: the fictional market index for simulated data, SPY for real
    data (MKTX is a real company there, not an index). Empty when the feed lists neither."""
    simulated = bool(getattr(feed, "simulated", True))
    for sym in (("MKTX", "SPY") if simulated else ("SPY", "QQQ", "IWM")):
        if is_stock(sym):
            return sym
    return ""
