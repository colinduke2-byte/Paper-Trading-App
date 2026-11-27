"""Market discovery: watchlists with layouts, symbol search, screener, market overview and compare.

store.py   persistence of watchlist layouts, saved screens and recent symbols (discovery.json)
stats.py   indicators computed from daily bars (52-week range, moving averages, RSI, performance)
data.py    the per-feed data service: quotes and fundamentals for every symbol, history-derived
           fields, and a call budget so a live provider's rate limit is respected
screener.py filter validation and evaluation
"""
