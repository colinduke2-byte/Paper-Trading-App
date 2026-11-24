"""Choose the market data feed from environment variables (and an optional .env file).

Variables (all optional):
  PAPERDESK_FEED                 auto (default) | alpaca | synthetic
  APCA_API_KEY_ID                Alpaca key id (Alpaca's own variable name)
  APCA_API_SECRET_KEY            Alpaca secret key
  APCA_API_BASE_URL              Alpaca trading host for the asset list; default https://paper-api.alpaca.markets
  PAPERDESK_ALPACA_FEED          iex (default, free) | sip | delayed_sip
  PAPERDESK_ALPACA_OPTIONS_FEED  indicative (default, free) | opra
  PAPERDESK_ALPACA_RPM           REST budget per minute; default 190 (the free plan allows 200)
  FINNHUB_API_KEY                optional secondary provider: earnings calendar and fundamentals
"""
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..core import envfile

log = logging.getLogger("paperdesk.config")

LIVE_WATCHLIST = ["SPY", "QQQ", "IWM", "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA"]


def project_root() -> Path:
    return envfile.project_root()


def load_dotenv(path: Path, environ=None) -> list:
    """Read KEY=VALUE lines into the environment without overriding variables already set.

    Returns the names it set (never the values). Missing file: does nothing. See app.core.envfile.
    """
    return envfile.load_into(path, environ)


@dataclass
class Selection:
    feed: object
    session_mode: str
    data_dir: str
    watchlist: list = field(default_factory=list)
    reason: Optional[str] = None


def wanted_symbols(ex, clients=None) -> list:
    """Symbols that need live prices, most important first: open orders, positions, what the UI shows, watchlists."""
    out = []
    for o in list(ex.engine.orders.values()):
        if o.active:
            out.append(o.symbol)
    for a in list(ex.accounts.values()):
        out.extend(sorted(a.positions()))
    for sub in list((clients or {}).values()):
        out.extend(sub.get("symbols") or [])
    for syms in list(ex.watchlists.values()):
        out.extend(syms)
    return list(dict.fromkeys(out))


def select_feed(now: float, data_dir: str, environ=None, dotenv: Optional[Path] = None, synthetic_seed: int = 7,
                start: bool = True, **alpaca_kw) -> Selection:
    """Pick the feed. Never raises for configuration problems: it falls back to synthetic and says why."""
    from .synthetic import SyntheticFeed
    environ = os.environ if environ is None else environ
    load_dotenv(dotenv if dotenv is not None else envfile.env_file_path(environ), environ)
    choice = (environ.get("PAPERDESK_FEED") or "auto").strip().lower()
    key, secret = environ.get("APCA_API_KEY_ID", "").strip(), environ.get("APCA_API_SECRET_KEY", "").strip()
    reason = None
    if choice not in ("auto", "alpaca", "synthetic"):
        reason = f"PAPERDESK_FEED={choice!r} is not recognised (use auto, alpaca or synthetic); using simulated prices."
    elif choice == "synthetic":
        reason = "PAPERDESK_FEED is set to synthetic."
    elif not key or not secret:
        reason = ("No Alpaca API key is set (APCA_API_KEY_ID and APCA_API_SECRET_KEY), so PaperDesk is showing "
                  "simulated prices for fictional companies. See docs/DATA_PROVIDERS.md.")
    else:
        sel = _alpaca(now, data_dir, environ, key, secret, start, **alpaca_kw)
        if sel is not None and not isinstance(sel, str):
            return sel
        reason = sel
    mode = environ.get("PAPERDESK_SESSIONS", "always")
    feed = SyntheticFeed(seed=synthetic_seed, now=now, always_open=mode == "always")
    feed.fallback_reason = reason
    from .research import ResearchHub
    feed.research = ResearchHub(simulated_reason="The synthetic feed uses fictional companies, so there is no "
                                                 "real news, calendar or fundamental data for them.")
    return Selection(feed, mode, data_dir, [], reason)


def _alpaca(now, data_dir, environ, key, secret, start, **kw):
    from .alpaca import PAPER_TRADING_URL, AlpacaFeed
    from .finnhub import FinnhubClient
    from .http import ProviderError
    from .research import ResearchHub
    live_dir = str(Path(data_dir) / "alpaca")  # real-symbol accounts never mix with fictional ones
    try:
        feed = AlpacaFeed(key, secret, feed=environ.get("PAPERDESK_ALPACA_FEED", "iex").strip().lower(),
                          options_feed=environ.get("PAPERDESK_ALPACA_OPTIONS_FEED", "indicative").strip().lower(),
                          trading_url=environ.get("APCA_API_BASE_URL", PAPER_TRADING_URL).strip().rstrip("/"),
                          rpm=float(environ.get("PAPERDESK_ALPACA_RPM", "190")), data_dir=live_dir, **kw)
    except ValueError as e:
        return f"Alpaca settings are invalid: {e}. Using simulated prices."
    try:
        feed.load_assets()
    except ProviderError as e:
        if e.status in (401, 403):
            return (f"Alpaca rejected the API key (HTTP {e.status}). Check APCA_API_KEY_ID and APCA_API_SECRET_KEY "
                    f"(paper keys need APCA_API_BASE_URL unset or https://paper-api.alpaca.markets). Using simulated prices.")
        return f"Could not reach Alpaca to load the symbol list ({e.message}). Using simulated prices."
    fh_key = environ.get("FINNHUB_API_KEY", "").strip()
    feed.research = ResearchHub(alpaca=feed.rest, finnhub=FinnhubClient(fh_key) if fh_key else None)
    if start:
        feed.start()
    mode = environ.get("PAPERDESK_SESSIONS", "us")
    log.info("market data: Alpaca %s feed, %d symbols", feed.data_feed, len(feed.info))
    return Selection(feed, mode, live_dir, list(LIVE_WATCHLIST), None)


def attach(sel: Selection, ex) -> None:
    """Let a live feed see which symbols the exchange needs."""
    if hasattr(sel.feed, "interest_source"):
        sel.feed.interest_source = lambda: wanted_symbols(ex)
