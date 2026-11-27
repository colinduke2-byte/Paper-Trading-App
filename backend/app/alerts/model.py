"""Alert records: validation of requests, plain-language descriptions, split adjustment.

An alert is stored as a plain JSON-safe dict (so it persists in state.json as it is and the old
price-alert list loads unchanged). Levels are strings holding Decimals. Fields:

  id, kind, account_id (None = every account), symbol, op, price (price alerts) or value (others),
  params (kind-specific, below), repeat, cooldown (seconds), expires_at, message (the user's note),
  note (a system remark, e.g. a split adjustment), status (active / paused / triggered / expired),
  created_at, triggered_at (last time it fired), fire_count, armed, rearm_at, last_value.

Kinds and their params:
  price      symbol, op above/below, price                      last price reaches the level
  change     symbol, op, value (percent), basis day/reference, reference (price)
  volume     symbol, op above, value; mode level (shares) / average (multiple of the 30-day average)
  indicator  symbol, tf, indicator sma/ema (op crosses_above/crosses_below: the close crosses the
             average), rsi (op above/below, value 0-100), macd (op crosses_above/crosses_below: the
             MACD line crosses its signal line); period. Evaluated when a bar of that timeframe closes.
  option     symbol (an option contract), metric iv (value in percent) / delta, op, value
  position   account, symbol, metric unrealized_pnl / unrealized_pct, op, value
  account    account, metric equity / buying_power / margin_excess / unrealized_pnl / day_pnl, op, value
  reminder   at (time), message; with repeat it comes back every `cooldown` seconds
  calendar   account, events dividend/split/earnings, days_before: upcoming ex-dates (and earnings
             reports, where the feed's research schedules them) of symbols held

Firing rule: an alert fires when its condition is true and it is armed. A one-shot alert is then
done (status triggered). A re-arming alert disarms; it arms again once its condition has been seen
false, and fires again only when the cool-down since the last firing has passed.
"""
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from ..core.assets import base_currency
from ..core.money import D, parse as parse_num
from ..marketdata.feed import TIMEFRAMES
from ..options.symbology import describe as describe_symbol, parse as parse_option

KINDS = ("price", "change", "volume", "indicator", "option", "position", "account", "reminder", "calendar")
ACCOUNT_KINDS = ("position", "account", "calendar")
STATUSES = ("active", "paused", "triggered", "expired")
LEVEL_OPS = ("above", "below")
CROSS_OPS = ("crosses_above", "crosses_below")
INDICATORS = ("sma", "ema", "rsi", "macd")
ACCOUNT_METRICS = {"equity": "Account value", "buying_power": "Buying power", "margin_excess": "Margin excess",
                   "unrealized_pnl": "Unrealised gain/loss", "day_pnl": "Day gain/loss"}
POSITION_METRICS = {"unrealized_pnl": "unrealised gain/loss", "unrealized_pct": "unrealised gain/loss %"}
OPTION_METRICS = {"iv": "Implied volatility", "delta": "Delta"}
CALENDAR_EVENTS = ("dividend", "split", "earnings")  # earnings: feeds whose research has a schedule
DEFAULT_COOLDOWN = 900
MAX_COOLDOWN = 30 * 86400
MAX_MESSAGE = 200
MAX_ALERTS = 10000
TF_LABEL = {"1m": "1-minute", "5m": "5-minute", "15m": "15-minute", "1h": "hourly", "1d": "daily", "1w": "weekly"}
FOUR = Decimal("0.0001")
TIME_LIMIT = Decimal("100000000000")  # seconds since 1970: far beyond any real date


def _fmt(v, places: int = 2) -> str:
    v = D(v)
    if v == v.to_integral_value() and places == 0:
        return f"{v:,.0f}"
    q = v.quantize(Decimal(1).scaleb(-places)) if abs(v) >= 1 or v == 0 else v.normalize()
    return f"{q:,f}"


def money_text(v) -> str:
    v = D(v)
    return ("-" if v < 0 else "") + f"{abs(v):,.2f}"


def level(al: dict) -> Decimal:
    return D(al["price"] if al["kind"] == "price" else al["value"])


def when(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# ---- validation ------------------------------------------------------------------------------
def _number(req: dict, key: str, what: str, alias: Optional[str] = None, most=Decimal("1000000000")) -> Decimal:
    raw = req.get(key, req.get(alias) if alias else None)
    if raw in (None, ""):
        raise ValueError(f"{what} is missing")
    try:
        v = parse_num(raw)
    except (ValueError, ArithmeticError):
        raise ValueError(f"{what} is not a number")
    if abs(v) > most:
        raise ValueError(f"{what} is too large")
    return v


def _op(req: dict, allowed: tuple) -> str:
    op = req.get("op")
    if op not in allowed:
        raise ValueError(f"condition must be one of: {', '.join(allowed)}")
    return op


def _stock(ex, req: dict) -> str:
    symbol = str(req.get("symbol") or "").upper().strip()
    if not symbol:
        raise ValueError("symbol is missing")
    if not ex.is_stock(symbol):
        raise ValueError(f"unknown symbol {symbol}")
    return symbol


def build(ex, req: dict, now: float) -> dict:
    """Validate an alert request and return the record to store (without id)."""
    if not isinstance(req, dict):
        raise ValueError("an alert must be a JSON object")
    kind = req.get("kind", "price")
    if kind not in KINDS:
        raise ValueError(f"alert type must be one of: {', '.join(KINDS)}")
    aid = req.get("account_id") or None
    if aid is not None and (not isinstance(aid, str) or aid not in ex.accounts):
        raise ValueError(f"no such account {aid}")
    if kind in ACCOUNT_KINDS and aid is None:
        raise ValueError("this alert type needs an account")
    al = {"kind": kind, "account_id": aid, "symbol": None, "op": None, "params": {}}
    p = al["params"]
    if kind == "price":
        al["symbol"], al["op"] = _stock(ex, req), _op(req, LEVEL_OPS)
        price = _number(req, "price", "price", alias="value")
        if price <= 0:
            raise ValueError("price must be positive")
        al["price"] = str(price)
    elif kind == "change":
        al["symbol"], al["op"] = _stock(ex, req), _op(req, LEVEL_OPS)
        al["value"] = str(_number(req, "value", "percent change"))
        basis = req.get("basis", "day")
        if basis not in ("day", "reference"):
            raise ValueError("percent change must be measured from the previous close ('day') or from a reference price ('reference')")
        p["basis"] = basis
        if basis == "reference":
            if req.get("reference") not in (None, ""):
                ref = _number(req, "reference", "reference price")
            else:
                q = ex.quote(al["symbol"])
                if q is None or q.last <= 0:
                    raise ValueError(f"no price for {al['symbol']} yet to use as the reference; enter one")
                ref = q.last
            if ref <= 0:
                raise ValueError("reference price must be positive")
            p["reference"] = str(ref)
    elif kind == "volume":
        al["symbol"], al["op"] = _stock(ex, req), "above"
        v = _number(req, "value", "volume level")
        if v <= 0:
            raise ValueError("volume level must be positive")
        al["value"] = str(v)
        mode = req.get("mode", "level")
        if mode not in ("level", "average"):
            raise ValueError("volume must be compared with a level ('level') or a multiple of the average ('average')")
        p["mode"] = mode
    elif kind == "indicator":
        al["symbol"] = _stock(ex, req)
        ind = req.get("indicator")
        if not isinstance(ind, str) or ind not in INDICATORS:
            raise ValueError(f"indicator must be one of: {', '.join(INDICATORS)}")
        tf = req.get("tf", "1d")
        if not isinstance(tf, str) or tf not in TIMEFRAMES:
            raise ValueError(f"timeframe must be one of: {', '.join(TIMEFRAMES)}")
        period = req.get("period", 14 if ind == "rsi" else 20)
        try:
            period = int(period)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("period must be a whole number")
        if ind != "macd" and not 2 <= period <= 200:
            raise ValueError("period must be between 2 and 200 bars")
        p.update(indicator=ind, tf=tf, period=26 if ind == "macd" else period)
        if ind == "rsi":
            al["op"] = _op(req, LEVEL_OPS)
            v = _number(req, "value", "RSI level")
            if not 0 < v < 100:
                raise ValueError("RSI level must be between 0 and 100")
            al["value"] = str(v)
        else:
            al["op"] = _op(req, CROSS_OPS)
            al["value"] = "0"
    elif kind == "option":
        symbol = str(req.get("symbol") or "").upper().strip()
        c = parse_option(symbol)
        if c is None:
            raise ValueError("option alerts need an option contract symbol (for example MKTX261218C00100000)")
        if not ex.is_stock(c.underlying):
            raise ValueError(f"unknown underlying {c.underlying}")
        if c.expiry_ts <= now:
            raise ValueError(f"{describe_symbol(symbol)} has already expired")
        al["symbol"], al["op"] = symbol, _op(req, LEVEL_OPS)
        metric = req.get("metric", "iv")
        if not isinstance(metric, str) or metric not in OPTION_METRICS:
            raise ValueError("option metric must be 'iv' or 'delta'")
        v = _number(req, "value", OPTION_METRICS[metric].lower())
        if metric == "delta" and not -1 <= v <= 1:
            raise ValueError("delta must be between -1 and 1")
        if metric == "iv" and not 0 < v < 1000:
            raise ValueError("implied volatility is entered in percent, between 0 and 1000")
        p.update(metric=metric, underlying=c.underlying)
        al["value"] = str(v)
    elif kind == "position":
        symbol = str(req.get("symbol") or "").upper().strip()
        if not symbol or not (ex.is_stock(symbol) or parse_option(symbol)):
            raise ValueError("choose the position (symbol) to watch")
        metric = req.get("metric", "unrealized_pnl")
        if not isinstance(metric, str) or metric not in POSITION_METRICS:
            raise ValueError("position metric must be 'unrealized_pnl' or 'unrealized_pct'")
        al["symbol"], al["op"] = symbol, _op(req, LEVEL_OPS)
        al["value"] = str(_number(req, "value", POSITION_METRICS[metric]))
        c = parse_option(symbol)
        p.update(metric=metric, underlying=c.underlying if c else symbol)
    elif kind == "account":
        metric = req.get("metric", "equity")
        if not isinstance(metric, str) or metric not in ACCOUNT_METRICS:
            raise ValueError(f"account metric must be one of: {', '.join(ACCOUNT_METRICS)}")
        al["op"] = _op(req, LEVEL_OPS)
        al["value"] = str(_number(req, "value", ACCOUNT_METRICS[metric].lower()))
        p["metric"] = metric
    elif kind == "reminder":
        if req.get("in_minutes") not in (None, ""):
            minutes = _number(req, "in_minutes", "minutes")
            if minutes <= 0:
                raise ValueError("minutes must be more than zero")
            at = now + float(minutes) * 60
        elif req.get("at") not in (None, ""):
            at = float(_number(req, "at", "reminder time", most=TIME_LIMIT))
        else:
            raise ValueError("reminder time is missing")
        if at <= now:
            raise ValueError("reminder time must be in the future")
        if not str(req.get("message") or "").strip():
            raise ValueError("a reminder needs a message")
        p["at"] = at
        al["value"] = "0"
    elif kind == "calendar":
        events = req.get("events") or ["dividend", "split"]
        if not isinstance(events, list) or not events or any(e not in CALENDAR_EVENTS for e in events):
            raise ValueError("calendar events must be a list of: dividend, split, earnings")
        try:
            days = int(req.get("days_before", 3))
        except (TypeError, ValueError, OverflowError):
            raise ValueError("days before must be a whole number")
        if not 0 <= days <= 30:
            raise ValueError("days before must be between 0 and 30")
        p.update(events=sorted(set(events)), days_before=days, seen=[])
        al["value"] = "0"

    repeat = bool(req.get("repeat", False))
    cooldown = req.get("cooldown", DEFAULT_COOLDOWN if repeat else 0)
    try:
        cooldown = int(float(cooldown or 0))
    except (TypeError, ValueError, OverflowError):
        raise ValueError("cool-down must be a number of seconds")
    if not 0 <= cooldown <= MAX_COOLDOWN:
        raise ValueError("cool-down must be between 0 seconds and 30 days")
    if kind == "reminder" and repeat and cooldown < 60:
        raise ValueError("a repeating reminder needs an interval of at least 60 seconds")
    expires_at = req.get("expires_at")
    if req.get("expires_in_hours") not in (None, ""):
        hours = _number(req, "expires_in_hours", "expiry (hours)")
        if hours <= 0:
            raise ValueError("expiry (hours) must be more than zero")
        expires_at = now + float(hours) * 3600
    elif expires_at in (None, ""):
        expires_at = None
    else:
        expires_at = float(_number(req, "expires_at", "expiry time", most=TIME_LIMIT))
        if expires_at <= now:
            raise ValueError("expiry time must be in the future")
    message = str(req.get("message") or "").strip()
    if len(message) > MAX_MESSAGE:
        raise ValueError(f"note must be at most {MAX_MESSAGE} characters")
    al.update(repeat=repeat, cooldown=cooldown, expires_at=expires_at, message=message or None, note=None,
              status="active", created_at=now, triggered_at=None, fire_count=0, armed=True, rearm_at=None,
              last_value=None)
    return al


EDITABLE = ("op", "price", "value", "basis", "reference", "mode", "tf", "indicator", "period", "metric", "at", "in_minutes",
            "expires_in_hours",
            "events", "days_before", "repeat", "cooldown", "expires_at", "message", "account_id", "symbol")


def to_request(al: dict) -> dict:
    """The request that would build this alert again (used to edit it)."""
    req = {"kind": al["kind"], "account_id": al.get("account_id"), "symbol": al.get("symbol"), "op": al.get("op"),
           "repeat": al.get("repeat", False), "cooldown": al.get("cooldown", 0), "expires_at": al.get("expires_at"),
           "message": al.get("message")}
    if al["kind"] == "price":
        req["price"] = al["price"]
    else:
        req["value"] = al.get("value")
    for k, v in (al.get("params") or {}).items():
        if k not in ("seen", "underlying"):
            req[k] = v
    return req


# ---- descriptions ----------------------------------------------------------------------------
def describe(al: dict) -> str:
    k, p, op = al["kind"], al.get("params") or {}, al.get("op")
    sym = al.get("symbol")
    up = op in ("above", "crosses_above")
    if k == "price":
        return f"{sym} {'rises to' if up else 'falls to'} {_fmt(al['price'])}"
    if k == "change":
        v = D(al["value"])
        frm = "on the day" if p.get("basis") == "day" else f"from {_fmt(p['reference'])}"
        return f"{sym} change {frm} {'rises to' if up else 'falls to'} {_fmt(v)}%"
    if k == "volume":
        if p.get("mode") == "average":
            return f"{sym} volume today reaches {_fmt(al['value'])} times its 30-day average"
        unit = base_currency(al["symbol"] or "") or "shares"  # a crypto pair counts coins
        return f"{sym} volume today reaches {_fmt(al['value'], 0)} {unit}"
    if k == "indicator":
        tf = TF_LABEL.get(p.get("tf"), p.get("tf"))
        ind = p.get("indicator")
        if ind in ("sma", "ema"):
            return (f"{sym} closes {'above' if up else 'below'} its {p['period']}-bar {ind.upper()} "
                    f"({tf} bars, crossing)")
        if ind == "rsi":
            return f"{sym} RSI({p['period']}) on {tf} bars {'rises above' if up else 'falls below'} {_fmt(al['value'])}"
        return f"{sym} MACD crosses {'above' if up else 'below'} its signal line ({tf} bars)"
    if k == "option":
        unit = "%" if p.get("metric") == "iv" else ""
        return (f"{OPTION_METRICS[p['metric']]} of {describe_symbol(sym)} {'rises to' if up else 'falls to'} "
                f"{_fmt(al['value'])}{unit}")
    if k == "position":
        what = POSITION_METRICS[p["metric"]]
        v = f"{_fmt(al['value'])}%" if p["metric"] == "unrealized_pct" else money_text(al["value"])
        return f"{describe_symbol(sym)} {what} {'rises to' if up else 'falls to'} {v}"
    if k == "account":
        return f"{ACCOUNT_METRICS[p['metric']]} {'rises to' if up else 'falls to'} {money_text(al['value'])}"
    if k == "reminder":
        every = f", then every {_duration(al['cooldown'])}" if al.get("repeat") else ""
        return f"Reminder at {when(p['at'])}{every}"
    if k == "calendar":
        ev = " and ".join(p.get("events", []))
        noun = "dates" if "earnings" in p.get("events", []) else "ex-dates"
        return f"Upcoming {ev} {noun} of symbols you hold, {p.get('days_before', 0)} days ahead"
    return k


def _duration(s) -> str:
    s = int(s)
    if s % 86400 == 0:
        return f"{s // 86400} day{'s' if s != 86400 else ''}"
    if s % 3600 == 0:
        return f"{s // 3600} hour{'s' if s != 3600 else ''}"
    if s % 60 == 0:
        return f"{s // 60} minute{'s' if s != 60 else ''}"
    return f"{s} seconds"


def value_text(al: dict, v) -> str:
    """The observed value, formatted for the alert's kind."""
    k, p = al["kind"], al.get("params") or {}
    if v is None:
        return ""
    if k in ("price",):
        return _fmt(v)
    if k == "change":
        return f"{_fmt(v)}%"
    if k == "volume":
        return f"{_fmt(v, 0)} shares"
    if k == "indicator":
        return f"{v:.2f}" if isinstance(v, float) else str(v)
    if k == "option":
        return f"{v:.1f}%" if p.get("metric") == "iv" else f"{v:.3f}"
    if k == "position" and p.get("metric") == "unrealized_pct":
        return f"{_fmt(v)}%"
    if k in ("position", "account"):
        return money_text(v)
    return str(v)


# ---- split adjustment ----------------------------------------------------------------------------
def adjust_for_split(al: dict, act) -> bool:
    """Scale an alert's levels for a split of act.symbol. Returns True when it changed."""
    sym, k, p = act.symbol, al["kind"], al.get("params") or {}
    if al.get("status") not in ("active", "paused"):
        return False
    ratio = Decimal(act.old) / Decimal(act.new)
    on = f"for the {act.describe()} on {act.ex_date.isoformat()}"

    def scale(v: Decimal, r: Decimal) -> str:
        new = (v * r).quantize(FOUR)
        return str(new.normalize() if new != new.to_integral_value() else new.quantize(Decimal(1)))

    if k == "price" and al["symbol"] == sym:
        old = D(al["price"])
        al["price"] = scale(old, ratio)
        al["note"] = f"Adjusted from {old.normalize():f} {on}"
        return True
    if k == "change" and al["symbol"] == sym and p.get("basis") == "reference":
        old = D(p["reference"])
        p["reference"] = scale(old, ratio)
        al["note"] = f"Reference adjusted from {old.normalize():f} {on}"
        return True
    if k == "volume" and al["symbol"] == sym and p.get("mode") == "level":
        old = D(al["value"])
        al["value"] = str((old / ratio).quantize(Decimal(1)))
        al["note"] = f"Adjusted from {old.normalize():f} shares {on}"
        return True
    if k == "option" and p.get("underlying") == sym:
        al["status"] = "paused"
        al["note"] = (f"Paused: the contract was adjusted {on}. Create a new alert on the adjusted contract.")
        return True
    if k == "position" and p.get("underlying") == sym and parse_option(al["symbol"]):
        al["status"] = "paused"
        al["note"] = f"Paused: the contract was adjusted {on}. Create a new alert on the adjusted position."
        return True
    return False
