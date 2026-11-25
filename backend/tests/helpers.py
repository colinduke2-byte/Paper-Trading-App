from decimal import Decimal
from itertools import count

from app.core.clock import SimClock
from app.core.models import Order, Quote
from app.exchange import Exchange
from app.marketdata.feed import SimFeed

D = Decimal
_ids = count(1)
T0 = 1795500000.0  # 2026-11-24 06:00 UTC (01:00 in New York, a Tuesday)


def q(bid, ask, last=None, bid_size=1000, ask_size=1000, last_size=100, trade=True, symbol="ABC"):
    return Quote(symbol=symbol, bid=D(str(bid)), ask=D(str(ask)), bid_size=D(bid_size), ask_size=D(ask_size),
                 last=D(str(last if last is not None else ask)), last_size=D(last_size) if trade else D(0), trade=trade)


def order(side, qty, type="market", **kw):
    for k in ("limit_price", "stop_price", "trail_amount", "trail_percent"):
        if k in kw and kw[k] is not None:
            kw[k] = D(str(kw[k]))
    return Order(id=f"T{next(_ids)}", account_id="A1", symbol=kw.pop("symbol", "ABC"), side=side, qty=D(str(qty)), type=type, **kw)


def fills(events):
    return [e for k, e in events if k == "fill"]


def make_exchange(tmp=None, **prices):
    clock = SimClock(T0)
    feed = SimFeed()
    ex = Exchange(feed, clock, data_dir=tmp)
    for sym, px in (prices or {"ABC": "100.00"}).items():
        px = D(str(px))
        feed.set_quote(sym, px - D("0.01"), px, px)
    ex.tick()
    return ex, feed, clock


def move(ex, feed, clock, symbol, price, seconds=1, **kw):
    px = D(str(price))
    clock.advance(seconds)
    feed.set_quote(symbol, px - D("0.01"), px, px, **kw)
    ex.tick()
