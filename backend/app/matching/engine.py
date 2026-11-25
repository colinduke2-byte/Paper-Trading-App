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
- An order is never matched against the quote that activated it as a linked child.
- On-open and on-close orders never trade continuously. They trade once, in full, in the
  next auction the caller announces with `on_auction`, at the single auction price (the
  last trade price of the quote the caller supplies; both sides trade at it and displayed
  size does not limit it). A limit-on-open/close order whose limit is not met expires.
- Multi-leg orders are all-or-none across legs and across quantity: they fill only when
  the net price of all legs together (buying at the ask, selling at the bid) is at or
  better than the net limit, and then every leg fills in full at its own quote price in
  the same instant. Displayed size does not limit them.

Linked orders:
- One-cancels-other: a fill on one order reduces the others by the same quantity and a
  complete fill cancels them.
- One-triggers-other: children are held until the parent finishes. Bracket and 'exit'
  children are sized to what the parent filled. 'triggered' children keep their own
  quantity and are released only when the parent filled completely.
"""
from dataclasses import dataclass
from decimal import Decimal
from itertools import count
from typing import Optional

from ..core.models import (BUY, SELL, MARKET, LIMIT, STOP, STOP_LIMIT, TRAILING_STOP, DAY, IOC, FOK,
                           HELD, WORKING, PARTIAL, FILLED, CANCELED, EXPIRED, Fill, Order, Quote,
                           AUCTION_TYPES, OPEN_TYPES, CLOSE_TYPES, LOO, LOC, MULTILEG, TRIGGERED)
from ..core.money import ZERO, CENT, cents, px4
from .multileg import net_price


@dataclass
class FillConfig:
    slippage_per_level: Decimal = CENT
    min_price: Decimal = CENT


class MatchingEngine:
    def __init__(self, config: Optional[FillConfig] = None):
        self.cfg = config or FillConfig()
        self.orders: dict[str, Order] = {}
        # The orders that were open when they were last looked at, in the order they were added: so the
        # tick and the rollback snapshot of a request do not walk every order the exchange ever had.
        # Pruned as it is read; an order never becomes open again once it has finished.
        self._open: dict[str, Order] = {}
        self._fill_seq = count(1)

    # ---- queries -------------------------------------------------------
    def active_orders(self, symbol: Optional[str] = None):
        orders = self.orders
        live = [o for k, o in self._open.items() if o.active and orders.get(k) is o]
        if len(live) != len(self._open):
            self._open = {o.id: o for o in live}
        return live if symbol is None else [o for o in live if o.symbol == symbol]

    def reindex(self) -> None:
        """Rebuild the open-order index from `orders` (after `orders` was replaced or restored)."""
        self._open = {k: o for k, o in self.orders.items() if o.active}

    def _add(self, order: Order) -> None:
        self.orders[order.id] = order
        self._open[order.id] = order

    def restore(self, orders, fill_seq: int = 1):
        self.orders = {o.id: o for o in orders}
        self.reindex()
        self._fill_seq = count(fill_seq)

    # ---- commands ------------------------------------------------------
    def submit(self, order: Order, quote: Optional[Quote], now: float, session: str = "regular") -> list:
        order.status = WORKING
        order.created_at = order.updated_at = now
        self._add(order)
        events = [("order", order)]
        self._process(order, quote, now, session, events, initial=True)
        return events

    def submit_linked(self, parent: Order, children: list, quote, now: float, session: str = "regular",
                      oco: bool = True) -> list:
        """One-triggers-other: hold `children` until `parent` finishes.

        With `oco` the children also cancel each other once released (a bracket).
        """
        for c in children:
            c.parent_id = parent.id
            c.oco_group = parent.id if oco else None
            c.status = HELD
            c.created_at = c.updated_at = now
            self._add(c)
        events = self.submit(parent, quote, now, session)
        events.extend(("order", c) for c in children if c.status == HELD)
        return events

    def submit_bracket(self, parent: Order, children: list, quote, now: float, session: str = "regular") -> list:
        return self.submit_linked(parent, children, quote, now, session, oco=True)

    def submit_oco(self, orders: list, quotes: dict, now: float, session: str = "regular") -> list:
        """One-cancels-other: all orders work at once; `quotes` maps symbol to its quote."""
        group = orders[0].id
        events = []
        for o in orders:
            o.oco_group = group
            o.status = WORKING
            o.created_at = o.updated_at = now
            self._add(o)
            events.append(("order", o))
        for o in orders:
            if o.status in (WORKING, PARTIAL):  # an earlier leg may already have canceled this one
                self._process(o, quotes.get(o.symbol), now, session, events, initial=True)
        return events

    def submit_multileg(self, order: Order, quotes: dict, now: float, session: str = "regular") -> list:
        order.status = WORKING
        order.created_at = order.updated_at = now
        self._add(order)
        events = [("order", order)]
        self._process_multileg(order, quotes, now, session, events, initial=True)
        return events

    def on_multileg(self, order_id: str, quotes: dict, now: float, session: str = "regular") -> list:
        """Re-price one working multi-leg order against the current quotes of its legs."""
        events = []
        o = self.orders.get(order_id)
        if o is not None and o.type == MULTILEG and o.status == WORKING:
            self._process_multileg(o, quotes, now, session, events, initial=False)
        return events

    def on_auction(self, kind: str, quote_for, now: float, only=None) -> list:
        """Run the opening ('open') or closing ('close') auction.

        `quote_for(symbol)` returns the quote whose last price is the auction price.
        `only`, when given, is the set of order ids that take part.
        """
        types = OPEN_TYPES if kind == "open" else CLOSE_TYPES
        events = []
        for o in list(self.orders.values()):
            if o.status != WORKING or o.type not in types or (only is not None and o.id not in only):
                continue
            q = quote_for(o.symbol)
            if q is None or q.last <= 0:
                self._finish(o, EXPIRED, now, events, f"no price was available at the {kind}")
                continue
            price = q.last
            if o.type in (LOO, LOC) and (price > o.limit_price if o.side == BUY else price < o.limit_price):
                self._finish(o, EXPIRED, now, events, f"limit price was not reached at the {kind}")
                continue
            self._fill(o, o.remaining, price, now, events)
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
            if o.status in (WORKING, PARTIAL) and o.type != MULTILEG:  # may have been canceled by a sibling this round
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
        if o.type in AUCTION_TYPES or o.type == MULTILEG:
            return  # these never trade continuously: see on_auction and on_multileg
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

    def _process_multileg(self, o: Order, quotes: dict, now: float, session: str, events: list, initial: bool) -> None:
        priced = net_price(o.legs, quotes) if session == "regular" else None
        if priced is None or (o.limit_price is not None and priced[0] > o.limit_price):
            if initial and o.tif in (IOC, FOK):
                self._finish(o, CANCELED, now, events, "the net price was not available for an immediate order")
            return
        net, prices = priced
        o.filled_qty = o.qty
        o.avg_fill_price = px4(net)
        o.status = FILLED
        o.updated_at = now
        for leg, price in zip(o.legs, prices):
            events.append(("fill", Fill(id=f"F{next(self._fill_seq):08d}", order_id=o.id, account_id=o.account_id,
                                        symbol=leg["symbol"], side=leg["side"], qty=o.qty * leg["ratio"],
                                        price=price, ts=now)))
        events.append(("order", o))

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
            self._reduce_siblings(o, qty, now, events)

    def _siblings(self, o: Order):
        return [s for s in self.orders.values() if s.oco_group == o.oco_group and s.id != o.id and s.active]

    def _reduce_siblings(self, o: Order, qty: Decimal, now: float, events: list) -> None:
        """A fill of `qty` on o takes the same quantity off every other order in its group."""
        for s in self._siblings(o):
            if s.status == HELD:
                continue  # not released yet; it is sized when its parent finishes
            left = max(s.remaining - qty, ZERO)
            if o.status == FILLED or (left == 0 and s.filled_qty == 0):
                self._finish(s, CANCELED, now, events, "other side of one-cancels-other filled")
            elif left == 0:  # everything it had not yet filled has been taken by the other side
                s.qty = s.filled_qty
                self._finish(s, FILLED, now, events, "remainder filled on the other side of one-cancels-other")
            else:
                s.qty = s.filled_qty + left
                s.updated_at = now
                events.append(("order", s))

    def _activate_children(self, parent: Order, now: float, events: list) -> None:
        for c in self.orders.values():
            if c.parent_id == parent.id and c.status == HELD:
                if c.role == TRIGGERED:
                    if parent.status == FILLED:
                        c.status = WORKING
                        c.updated_at = now
                        events.append(("order", c))
                    else:
                        self._finish(c, CANCELED, now, events, "triggering order did not fill completely")
                elif parent.filled_qty > 0:
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
