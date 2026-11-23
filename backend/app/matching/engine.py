"""Order matching against a quote stream.

Pure and deterministic: no clock, no I/O, no account knowledge. The caller feeds
quotes and gets back events: ("order", Order) for every state change and
("fill", Fill) for every execution.

Fill model (conservative by design):
- Market orders (and triggered stops) fill in full, walking a synthetic book:
  each level holds the displayed size and is one slippage step worse.
- Limit orders fill only when the opposite quote reaches the limit, at the quote
  price, for at most the displayed size per quote update. A resting limit also
  fills at its limit price when a new print trades strictly through it.
- Stops trigger on the last trade price.
- An order is never matched against the quote that activated it as a bracket child.
"""
from dataclasses import dataclass
from decimal import Decimal
from itertools import count
from typing import Optional

from ..core.models import (BUY, SELL, MARKET, LIMIT, STOP, STOP_LIMIT, TRAILING_STOP, DAY, IOC, FOK,
                           HELD, WORKING, PARTIAL, FILLED, CANCELED, EXPIRED, Fill, Order, Quote)
from ..core.money import ZERO, CENT, cents, px4


@dataclass
class FillConfig:
    slippage_per_level: Decimal = CENT
    min_price: Decimal = CENT


class MatchingEngine:
    def __init__(self, config: Optional[FillConfig] = None):
        self.cfg = config or FillConfig()
        self.orders: dict[str, Order] = {}
        self._fill_seq = count(1)

    # ---- queries -------------------------------------------------------
    def active_orders(self, symbol: Optional[str] = None):
        return [o for o in self.orders.values() if o.active and (symbol is None or o.symbol == symbol)]

    def restore(self, orders, fill_seq: int = 1):
        self.orders = {o.id: o for o in orders}
        self._fill_seq = count(fill_seq)

    # ---- commands ------------------------------------------------------
    def submit(self, order: Order, quote: Optional[Quote], now: float, session: str = "regular") -> list:
        order.status = WORKING
        order.created_at = order.updated_at = now
        self.orders[order.id] = order
        events = [("order", order)]
        self._process(order, quote, now, session, events, initial=True)
        return events

    def submit_bracket(self, parent: Order, children: list, quote, now: float, session: str = "regular") -> list:
        for c in children:
            c.parent_id = parent.id
            c.oco_group = parent.id
            c.status = HELD
            c.created_at = c.updated_at = now
            self.orders[c.id] = c
        events = self.submit(parent, quote, now, session)
        events.extend(("order", c) for c in children if c.status == HELD)
        return events

    def cancel(self, order_id: str, now: float, reason: Optional[str] = None, status: str = CANCELED) -> list:
        o = self.orders.get(order_id)
        if o is None or not o.active:
            return []
        events = []
        self._finish(o, status, now, events, reason)
        return events

    def replace(self, order_id: str, now: float, qty=None, limit_price=None, stop_price=None) -> list:
        o = self.orders.get(order_id)
        if o is None or not o.active:
            raise ValueError("order is not open")
        if qty is not None:
            if qty <= o.filled_qty:
                raise ValueError("new quantity must exceed the filled quantity")
            o.qty = qty
        if limit_price is not None:
            if o.limit_price is None:
                raise ValueError("order has no limit price")
            o.limit_price = limit_price
        if stop_price is not None:
            if o.stop_price is None or o.type == TRAILING_STOP or o.triggered:
                raise ValueError("stop price cannot be changed on this order")
            o.stop_price = stop_price
        o.updated_at = now
        return [("order", o)]

    def expire(self, now: float, predicate) -> list:
        events = []
        for o in list(self.orders.values()):
            if o.active and predicate(o):
                self._finish(o, EXPIRED, now, events, "expired")
        return events

    def on_quote(self, quote: Quote, now: float, session: str = "regular") -> list:
        events = []
        for o in [o for o in self.orders.values() if o.symbol == quote.symbol and o.status in (WORKING, PARTIAL)]:
            if o.status in (WORKING, PARTIAL):  # may have been canceled by a sibling this round
                self._process(o, quote, now, session, events, initial=False)
        return events

    # ---- internals -----------------------------------------------------
    @staticmethod
    def _eligible(o: Order, session: str) -> bool:
        if session == "regular":
            return True
        if session == "closed":
            return False
        return o.extended_hours and o.type == LIMIT

    def _update_trigger(self, o: Order, q: Quote) -> None:
        ref = q.last
        if o.type == TRAILING_STOP:
            if o.side == SELL:
                o.trail_ref = ref if o.trail_ref is None else max(o.trail_ref, ref)
                off = o.trail_amount if o.trail_amount is not None else o.trail_ref * o.trail_percent / 100
                o.stop_price = cents(o.trail_ref - off)
            else:
                o.trail_ref = ref if o.trail_ref is None else min(o.trail_ref, ref)
                off = o.trail_amount if o.trail_amount is not None else o.trail_ref * o.trail_percent / 100
                o.stop_price = cents(o.trail_ref + off)
        hit = ref >= o.stop_price if o.side == BUY else ref <= o.stop_price
        if hit:
            o.triggered = True

    def _walk(self, q: Quote, side: str, qty: Decimal) -> Decimal:
        """Volume-weighted price for taking qty from a synthetic book."""
        top, size = (q.ask, q.ask_size) if side == BUY else (q.bid, q.bid_size)
        size = max(size, Decimal(1))
        step = self.cfg.slippage_per_level if side == BUY else -self.cfg.slippage_per_level
        n = int(qty // size)
        rem = qty - n * size
        if rem == 0:
            n, rem = n - 1, size
        # n full levels 0..n-1, then the remainder at level n
        cost = size * (n * top + step * n * (n - 1) / 2) + rem * (top + step * n)
        return max(px4(cost / qty), self.cfg.min_price)

    def _process(self, o: Order, q: Optional[Quote], now: float, session: str, events: list, initial: bool) -> None:
        immediate = o.tif in (IOC, FOK)
        if q is None or q.ask <= 0 or q.last <= 0 or not self._eligible(o, session):
            if initial and immediate:
                self._finish(o, CANCELED, now, events, "no market available for an immediate order")
            return
        if o.type in (STOP, STOP_LIMIT, TRAILING_STOP) and not o.triggered:
            self._update_trigger(o, q)
            if not o.triggered:
                return
            events.append(("order", o))
        as_market = o.type == MARKET or o.type in (STOP, TRAILING_STOP)
        remaining = o.remaining
        if as_market:
            self._fill(o, remaining, self._walk(q, o.side, remaining), now, events)
            return
        top, size = (q.ask, q.ask_size) if o.side == BUY else (q.bid, q.bid_size)
        crossed = top > 0 and (top <= o.limit_price if o.side == BUY else top >= o.limit_price)
        qty, price = ZERO, None
        if crossed and size > 0:
            qty, price = min(remaining, size), top
        elif not initial and q.trade and q.last_size > 0 and (
                q.last < o.limit_price if o.side == BUY else q.last > o.limit_price):
            qty, price = min(remaining, q.last_size), o.limit_price
        if o.tif == FOK and qty < remaining:
            self._finish(o, CANCELED, now, events, "fill-or-kill could not fill in full")
            return
        if qty > 0:
            self._fill(o, qty, price, now, events)
        if initial and o.tif == IOC and o.active:
            self._finish(o, CANCELED, now, events, "unfilled part of immediate-or-cancel order")

    def _fill(self, o: Order, qty: Decimal, price: Decimal, now: float, events: list) -> None:
        prev_notional = (o.avg_fill_price or ZERO) * o.filled_qty
        o.filled_qty += qty
        o.avg_fill_price = px4((prev_notional + price * qty) / o.filled_qty)
        o.status = FILLED if o.filled_qty >= o.qty else PARTIAL
        o.updated_at = now
        fill = Fill(id=f"F{next(self._fill_seq):08d}", order_id=o.id, account_id=o.account_id,
                    symbol=o.symbol, side=o.side, qty=qty, price=price, ts=now)
        events.append(("fill", fill))
        events.append(("order", o))
        if o.status == FILLED:
            self._activate_children(o, now, events)
        if o.oco_group:
            self._reduce_siblings(o, now, events)

    def _siblings(self, o: Order):
        return [s for s in self.orders.values() if s.oco_group == o.oco_group and s.id != o.id and s.active]

    def _reduce_siblings(self, o: Order, now: float, events: list) -> None:
        for s in self._siblings(o):
            if o.status == FILLED:
                self._finish(s, CANCELED, now, events, "other side of one-cancels-other filled")
            else:
                s.qty = s.filled_qty + o.remaining
                s.updated_at = now
                events.append(("order", s))

    def _activate_children(self, parent: Order, now: float, events: list) -> None:
        for c in self.orders.values():
            if c.parent_id == parent.id and c.status == HELD:
                if parent.filled_qty > 0:
                    c.qty = parent.filled_qty
                    c.status = WORKING
                    c.updated_at = now
                    events.append(("order", c))
                else:
                    self._finish(c, CANCELED, now, events, "entry order did not fill")

    def _finish(self, o: Order, status: str, now: float, events: list, reason: Optional[str] = None) -> None:
        o.status = status
        o.reason = reason
        o.updated_at = now
        events.append(("order", o))
        # An entry order that ends early releases its bracket children.
        self._activate_children(o, now, events)
