"""The simulated broker: wires market data, matching, accounts, risk and options."""
import json
import os
import re
import tempfile
import threading
import time as _time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from itertools import count
from typing import Optional

from .alerts import Alerting
from .core import calendar as cal
from .core import models as m
from .core.clock import Clock
from .core.models import Fill, Order, Quote, from_plain, to_plain
from .core.money import CENT, ZERO, D, cents, parse as parse_num
from .ledger.account import Account
from .lifecycle.manager import LifecycleManager
from .marketdata.feed import MarketDataProvider
from .matching.engine import FillConfig, MatchingEngine
from .matching.multileg import net_price, payoff
from .options import chain as optchain
from .options.symbology import describe, multiplier, parse as parse_option
from .risk import margin
from .core.errors import NotFound

OPTION_FEE = Decimal("0.65")  # per contract
STOCK_FEE = ZERO  # per share
MAX_QTY = Decimal(1_000_000)
MAX_LINKED = 5  # orders in one one-cancels-other or one-triggers-other request
MAX_GTD_DAYS = 366
MAX_NUMBER = Decimal("1000000000")  # no price or quantity in a request may be larger than this
CLOSED_AT_EXPIRY = "closed at expiry"  # fill kind: an option closed for its intrinsic value, not exercised
RUNNING_GAP = 120  # seconds: a longer gap between two ticks means the simulator was not running (or was paused)
CLOSE_WAIT = 120  # seconds to wait for the provider's daily close before settling at an approximate price
PROVIDER_CLOSE_WAIT = 1800  # seconds to wait for feed.closing_price to become ready before approximating


def _in_range(*values) -> None:
    """Refuse absurd numbers before any arithmetic is done with them."""
    for v in values:
        if v is not None and abs(v) > MAX_NUMBER:
            raise OrderRejected("invalid number in order: a value is too large")


class OrderRejected(ValueError):
    pass


FIELD_LABELS = {"qty": "quantity", "limit_price": "limit price", "stop_price": "stop price",
                "trail_amount": "trail amount", "trail_percent": "trail percent", "take_profit": "take profit",
                "stop_loss": "stop loss"}


def _field(req: dict, key: str) -> Optional[Decimal]:
    """An optional number from an order request; raises ValueError naming the field."""
    v = req.get(key)
    return None if v is None or v == "" else parse_num(v, FIELD_LABELS.get(key, key.replace("_", " ")))


def _usd(x: Decimal) -> str:
    x = cents(x)
    return f"-${-x:,.2f}" if x < 0 else f"${x:,.2f}"


def _starting_cash(value) -> Decimal:
    v = D(value)
    if v <= 0 or v > Decimal("1000000000"):
        raise ValueError("starting cash must be more than 0 and at most 1,000,000,000")
    if v != cents(v):
        raise ValueError("starting cash can have at most 2 decimal places")
    return v


class Exchange:
    def __init__(self, feed: MarketDataProvider, clock: Optional[Clock] = None, data_dir: Optional[str] = None,
                 session_mode: str = "always", fill_config: Optional[FillConfig] = None):
        self.feed = feed
        self.clock = clock or Clock()
        self.data_dir = data_dir
        self.session_mode = session_mode  # 'always' or 'us'
        self.engine = MatchingEngine(fill_config)
        self._last_tick = None  # time of the previous tick in this run (None right after a start)
        self._due_open = set()  # on-open orders whose auction is this tick, once the feed has stepped
        self._session_quotes: dict[str, Quote] = {}  # last quote each symbol had in a regular session
        self.accounts: dict[str, Account] = {}
        self.watchlists: dict[str, list] = {}
        self.alerting = Alerting(self)  # alerts and the notification centre (app/alerts)
        self.events = self.alerting.stream()  # drained by the API layer after each tick; feeds notifications
        self._order_seq = count(1)
        self._acct_seq = count(1)
        self._alert_seq = count(1)
        self._fill_seq = 1
        self._expiry_seq = count(1)
        self._last_day = None
        self._last_session = None
        self._last_equity_sample = 0.0
        self._last_market_save = 0.0
        self.dirty = False
        # Expiry settlement prices: (underlying, expiry ISO) -> (price, exact, note); fetches of the provider's
        # daily close in progress or finished: key -> {"started": ts, "done": bool}.
        self._settle_prices: dict = {}
        self._close_fetches: dict = {}
        self.background = lambda fn: threading.Thread(target=fn, daemon=True, name="expiry-close").start()
        self._expiry_waiting: set = set()  # (account id, option) expired but waiting for a price
        # account id -> "now" (the day rolled while running) or "restart" (it rolled while the simulator was
        # closed): the day start equity is set once every position has a price
        self._day_start_wanted: dict = {}
        self.lifecycle = LifecycleManager(self)  # corporate actions, settlement, interest, PDT, margin calls

    # ---- time and market state -----------------------------------------
    def now(self) -> float:
        return self.clock.now()

    def session(self, now: Optional[float] = None) -> str:
        if self.session_mode == "always":
            return "regular"
        return cal.session(self.now() if now is None else now)

    def is_stock(self, symbol: str) -> bool:
        return symbol in self.feed.symbols()

    def quote(self, symbol: str) -> Optional[Quote]:
        if self.is_stock(symbol):
            return self.feed.quote(symbol)
        c = parse_option(symbol)
        if c is None:
            return None
        provided = self.feed.option_quote(symbol)  # real option data when the feed has it
        if provided is not None:
            return provided
        u = self.feed.quote(c.underlying)
        if u is None:
            return None
        now = self.now()
        oq = optchain.option_quote(c, float(u.last), self.feed.base_vol(c.underlying), now,
                                   self.feed.event_variance(c.underlying, c.expiry_ts, now))
        oq.stale = u.stale  # a model price from a stale underlying is stale too
        return oq

    @staticmethod
    def _tradable(q: Optional[Quote]) -> Optional[Quote]:
        """The quote an order may fill against now: None when there is none or it is stale (the order rests)."""
        return None if q is None or q.stale else q

    def quote_source(self, symbol: str) -> str:
        """Where quote(symbol) comes from: the feed's name, or "model" for a modelled option price."""
        if not self.is_stock(symbol) and parse_option(symbol) is not None and self.feed.option_quote(symbol) is None:
            return "model"
        return self.feed.name

    def mark(self, symbol: str) -> Decimal:
        q = self.quote(symbol)
        if q is None:
            return ZERO
        if self.is_stock(symbol):
            return q.last
        # An option quote with no ask (a live feed with an empty book) is marked at its last trade, not at 0.
        return cents(q.mid) if q.ask > 0 else q.last

    def price(self, symbol: str) -> Optional[Decimal]:
        """The mark, or None when there is no price (never a zero standing in for a missing price)."""
        q = self.quote(symbol)
        if q is None or (self.is_stock(symbol) and q.last <= 0):
            return None
        return self.mark(symbol)

    def _marks(self, symbols) -> dict:
        marks = {}
        for s in symbols:
            marks[s] = self.mark(s)
            c = parse_option(s)
            if c:
                marks[c.underlying] = self.mark(c.underlying)
        return marks

    # ---- accounts --------------------------------------------------------
    def create_account(self, name: str, type: str = "margin", starting_cash=Decimal("100000")) -> Account:
        starting_cash = _starting_cash(starting_cash)
        name = (name or "").strip()[:40] or "Paper account"
        acct = Account(f"A{next(self._acct_seq):04d}", name, type, starting_cash, self.now())
        self.accounts[acct.id] = acct
        self.dirty = True
        return acct

    def reset_account(self, account_id: str, starting_cash=None) -> Account:
        """Start the account again with fresh cash. Its rule settings and tax-lot method are kept; positions,
        orders, history and the trade journal are cleared."""
        old = self.account(account_id)
        cash = _starting_cash(starting_cash) if starting_cash is not None else old.starting_cash
        for o in self.engine.active_orders():
            if o.account_id == account_id:
                self.engine.cancel(o.id, self.now(), "account reset")
        self.engine.orders = {k: o for k, o in self.engine.orders.items() if o.account_id != account_id}
        new = Account(old.id, old.name, old.type, cash, self.now())
        new.settings = dict(old.settings)  # a reset keeps the account's rule settings
        new.lot_method = old.lot_method  # ... and its tax-lot method
        self._day_start_wanted.pop(old.id, None)
        self.accounts[old.id] = new
        self.dirty = True
        return new

    def delete_account(self, account_id: str) -> None:
        self.account(account_id)
        if len(self.accounts) == 1:
            raise ValueError("cannot delete the only account")
        self.engine.orders = {k: o for k, o in self.engine.orders.items() if o.account_id != account_id}
        del self.accounts[account_id]
        self.dirty = True

    def account(self, account_id: str) -> Account:
        try:
            return self.accounts[account_id]
        except KeyError:
            raise NotFound(f"no such account {account_id}")

    def summary(self, account_id: str) -> dict:
        a = self.account(account_id)
        pos = a.positions()
        marks = self._marks(pos)
        eq = margin.equity(a.cash, pos, marks)
        long_mv = sum((q * marks[s] * multiplier(s) for s, q in pos.items() if q > 0), ZERO)
        short_mv = sum((q * marks[s] * multiplier(s) for s, q in pos.items() if q < 0), ZERO)
        init = margin.requirement(pos, marks) if a.type == "margin" else ZERO
        maint = margin.requirement(pos, marks, maintenance=True) if a.type == "margin" else ZERO
        # Buying power also reserves for open orders that add risk.
        worst = self._worst_case(a, [])
        if a.type == "margin":
            excess = max(worst.excess, ZERO)
            bp = {"stock": cents(excess / margin.INITIAL), "option": cents(excess)}
        else:
            avail = max(worst.cash_after - worst.requirement, ZERO) if worst.ok else ZERO
            bp = {"stock": cents(avail), "option": cents(avail)}
        cost = sum((a.cost_basis(s) for s in pos), ZERO)
        unrealized = cents(long_mv + short_mv - cost)
        a.margin_call = a.type == "margin" and eq < maint
        out = {"id": a.id, "name": a.name, "type": a.type, "starting_cash": a.starting_cash, "cash": a.cash,
                "equity": eq, "long_market_value": cents(long_mv), "short_market_value": cents(short_mv),
                "initial_requirement": init, "maintenance_requirement": maint,
                "excess_liquidity": cents(eq - maint), "buying_power": bp["stock"], "option_buying_power": bp["option"],
                "realized_pnl": a.realized, "unrealized_pnl": unrealized, "fees": a.fees_paid,
                "total_pnl": cents(eq - a.starting_cash), "day_pnl": cents(eq - a.day_start_equity),
                "margin_call": a.margin_call}
        self.lifecycle.extend_summary(a, out)
        return out

    def positions(self, account_id: str) -> list:
        a = self.account(account_id)
        rows = []
        for sym, qty in sorted(a.positions().items()):
            mult = multiplier(sym)
            c = parse_option(sym)
            u = self.feed.quote(c.underlying if c else sym)
            # No price yet (a live feed right after a start): show the position without a value, never at zero.
            q = self.quote(sym)
            priced = q is not None and (q.last > 0 if not c else q.mid > 0 or u is not None and u.last > 0)
            mark = self.mark(sym) if priced else None
            cost = a.cost_basis(sym)
            mv = cents(qty * mark * mult) if priced else None
            row = {"symbol": sym, "description": describe(sym), "qty": qty, "avg_cost": a.avg_cost(sym), "mark": mark,
                   "market_value": mv, "cost_basis": cost, "unrealized_pnl": mv - cost if priced else None,
                   "unrealized_pct": (float((mv - cost) / abs(cost) * 100) if cost else 0.0) if priced else None,
                   "multiplier": mult, "kind": "option" if mult == 100 else "stock", "day_pnl": None,
                   "price_unavailable": not priced}
            if c:
                row.update(delta=None, theta=None, underlying=c.underlying, expiry=c.expiry.isoformat())
                if u is not None and u.last > 0:
                    snap = optchain.option_snapshot(c, float(u.last), self.feed.base_vol(c.underlying), self.now(),
                                                    self.feed.event_variance(c.underlying, c.expiry_ts, self.now()))
                    row.update(delta=snap["delta"] * float(qty) * 100, theta=snap["theta"] * float(qty) * 100)
            else:
                q = self.feed.quote(sym)
                if q and q.prev_close:
                    row["day_pnl"] = cents(qty * (q.last - q.prev_close))
                row["delta"] = float(qty)
            rows.append(self._annotate_position(row, sym, mark))
        return rows

    def _annotate_position(self, row: dict, sym: str, mark: Optional[Decimal]) -> dict:
        """Price source, price note and corporate action notices of a position row (only when there is one).
        mark is None (or not positive) when the position has no price."""
        mark = mark if mark is not None else ZERO
        src = self.quote_source(sym)
        if src != self.feed.name or mark <= 0:
            row["price_source"] = src if mark > 0 else None
        note = self._position_note(sym, src, mark)
        if note:
            row["price_note"] = note
        q = self.quote(sym)
        if q is not None and getattr(q, "stale", False):
            row["stale"] = True
        notices = self.lifecycle.position_notices(sym)
        if notices:
            row["notices"] = notices
        return row

    def _position_note(self, sym: str, src: str, mark: Decimal) -> Optional[str]:
        """Why a position's mark is not a current market price, or None."""
        if mark <= 0:
            return f"Not valued: {self.feed.no_price_reason(sym)}"
        c = parse_option(sym)
        if c is not None:
            if self.feed.simulated or src != "model":
                return None
            listing = self.feed.option_listing(c.underlying, fetch=False)  # never block a tick on the network
            if listing is not None and c.strike not in listing.get(c.expiry, ()):
                return (f"{self.feed.name.capitalize()} no longer lists this contract (expired, delisted or adjusted); "
                        f"it is valued by the option model, not market data. It can still be closed.")
            return "No option quote from the data provider; valued by the option model, not market data."
        n = self.feed.quote_note(sym)
        return n["note"] if n and n.get("stale") else None

    def orders(self, account_id: str, status: str = "all") -> list:
        self.account(account_id)
        rows = [o for o in self.engine.orders.values() if o.account_id == account_id]
        if status == "open":
            rows = [o for o in rows if o.active]
        elif status == "closed":
            rows = [o for o in rows if not o.active]
        return sorted(rows, key=lambda o: (o.created_at, o.id), reverse=True)

    # ---- order entry -----------------------------------------------------
    def _check_symbol(self, account: Account, symbol: str):
        """Reject an unknown or unlisted symbol. Returns the option contract, or None for a stock."""
        contract = parse_option(symbol)
        if not self.is_stock(symbol):
            if contract is None or not self.is_stock(contract.underlying):
                raise OrderRejected(f"unknown symbol {symbol or '(blank)'}")
            if contract.expiry_ts <= self.now():
                raise OrderRejected("this option has expired")
            # A position held at an unlisted strike or expiry (an option adjusted by a split, a contract the
            # provider no longer lists) can still be closed.
            held = account.position(symbol) != 0
            if contract.strike <= 0:
                raise OrderRejected("strike is not listed")
            listing = self.feed.option_listing(contract.underlying)
            if listing is not None:  # a live feed: its real contracts, not the model calendar
                strikes = listing.get(contract.expiry)
                if strikes is None and not held:
                    raise OrderRejected(f"expiry is not listed: {self.feed.name.capitalize()} lists no "
                                        f"{contract.underlying} options expiring {contract.expiry.isoformat()}")
                if strikes is not None and contract.strike not in strikes and not held:
                    raise OrderRejected(f"strike is not listed: {self.feed.name.capitalize()} lists no "
                                        f"{contract.strike.normalize():f} strike for {contract.underlying} on "
                                        f"{contract.expiry.isoformat()}")
                return contract
            if (contract.strike * 2) % 1 != 0 and not held:
                raise OrderRejected("strike is not listed")
            if contract.expiry not in optchain.expirations(cal.ny_date(self.now())) and not held:
                raise OrderRejected("expiry is not listed")
            return contract
        return None

    def _good_till(self, req: dict, tif: str) -> tuple:
        """(expiry time, date text) of a good-till-date order; (None, None) for any other.

        The order expires at the regular close of that day. In session mode 'always' the
        market never closes, so the close is the end of that UTC day.
        """
        raw = req.get("good_till_date")
        if tif != m.GTD:
            if raw not in (None, ""):
                raise OrderRejected("a good-till date needs time in force gtd")
            return None, None
        if raw in (None, ""):
            raise OrderRejected("good-till date is required for a good-till-date order")
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(raw)):
                raise ValueError(raw)
            day = date.fromisoformat(str(raw))
        except ValueError:
            raise OrderRejected("good-till date must look like 2026-12-31")
        if self.session_mode == "always":
            at = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc).timestamp()
        else:
            if not cal.is_trading_day(day):
                raise OrderRejected(f"the market is closed on {day.isoformat()}; pick a trading day")
            at = cal.close_ts(day)
        if at <= self.now():
            raise OrderRejected("good-till date has already passed")
        if at > self.now() + MAX_GTD_DAYS * 86400:
            raise OrderRejected("good-till date can be at most one year ahead")
        return at, day.isoformat()

    def _validate(self, account: Account, req: dict) -> list:
        """Turn an API request into Order objects (entry order first). Raises OrderRejected.

        One request is one of: a single order (optionally a bracket with take_profit and/or
        stop_loss), a one-cancels-other group ("oco": [order, order, ...]), a
        one-triggers-other chain (an order plus "triggers": [order, ...]) or a multi-leg
        order ("legs": [...]).
        """
        if not isinstance(req, dict):
            raise OrderRejected("an order must be a JSON object")
        if req.get("legs") is not None:
            return [self._validate_multileg(account, req)]
        if req.get("oco") is not None:
            return self._validate_oco(account, req)
        if req.get("triggers") is not None:
            return self._validate_oto(account, req)
        return self._validate_one(account, req)

    def _linked_list(self, raw, least: int, name: str) -> list:
        if not isinstance(raw, list) or not all(isinstance(r, dict) for r in raw):
            raise OrderRejected(f"{name} must be a list of orders")
        if not least <= len(raw) <= MAX_LINKED - (least == 1):
            raise OrderRejected(f"a linked order takes at most {MAX_LINKED} orders" if len(raw) >= least
                                else f"{name} needs at least {least} order{'s' if least > 1 else ''}")
        return raw

    def _validate_oco(self, account: Account, req: dict) -> list:
        """Standalone one-cancels-other: every order works at once; a fill on one reduces the rest."""
        if req.get("triggers") is not None:
            raise OrderRejected("send either oco or triggers, not both")
        orders = []
        for sub in self._linked_list(req["oco"], 2, "oco"):
            [o] = self._validate_one(account, sub, linked="one-cancels-other")
            if o.qty != o.qty.to_integral_value():
                raise OrderRejected("one-cancels-other orders need whole quantities")
            orders.append(o)
        for o in orders:
            o.oco_group = orders[0].id
        orders[0].client_id = str(req["client_id"])[:64] if req.get("client_id") else orders[0].client_id
        return orders

    def _validate_oto(self, account: Account, req: dict) -> list:
        """One-triggers-other: the first order works now, the triggered orders wait for it.

        A triggered order without a quantity is an exit: it must close the entry (same
        symbol, opposite side) and takes whatever quantity the entry filled, like a bracket
        leg. A triggered order with its own quantity is released only when the entry
        fills completely, and is canceled if the entry ends any other way.
        """
        raw = self._linked_list(req["triggers"], 1, "triggers")
        [entry] = self._validate_one(account, {k: v for k, v in req.items() if k != "triggers"}, linked="one-triggers-other")
        if entry.qty != entry.qty.to_integral_value():
            raise OrderRejected("one-triggers-other orders need whole quantities")
        oco = bool(req.get("triggers_oco"))
        children = []
        for sub in raw:
            sub = dict(sub)
            sub.setdefault("tif", m.GTC)
            exit_only = sub.get("qty") in (None, "")
            if exit_only:
                sub["qty"] = str(entry.qty)
                sub.setdefault("symbol", entry.symbol)
                sub.setdefault("side", m.SELL if entry.side == m.BUY else m.BUY)
            [c] = self._validate_one(account, sub, linked="triggered")
            if c.tif not in (m.GTC, m.GTD):
                raise OrderRejected("triggered orders must be good-till-canceled or good-till-date")
            if exit_only and (c.symbol != entry.symbol or c.side == entry.side):
                raise OrderRejected("a triggered order without a quantity must close the entry: same symbol, opposite side")
            c.role = m.EXIT if exit_only else m.TRIGGERED
            c.parent_id, c.status = entry.id, m.HELD
            c.oco_group = entry.id if oco else None
            children.append(c)
        if not oco and sum(c.role == m.EXIT for c in children) > 1:
            raise OrderRejected("several exits for one entry must be one-cancels-other: set triggers_oco")
        return [entry] + children

    def _validate_one(self, account: Account, req: dict, linked: Optional[str] = None) -> list:
        """One order, plus its bracket children when take_profit or stop_loss is given."""
        if linked and any(req.get(k) is not None for k in ("legs", "oco", "triggers")):
            raise OrderRejected("a linked order cannot itself be multi-leg or linked")
        try:
            symbol = str(req.get("symbol", "")).upper().strip()
            side, typ = req.get("side"), req.get("type", m.MARKET)
            tif = req.get("tif", m.DAY)
            qty = parse_num(req.get("qty"), FIELD_LABELS["qty"])
            limit, stop = _field(req, "limit_price"), _field(req, "stop_price")
            trail_amt, trail_pct = _field(req, "trail_amount"), _field(req, "trail_percent")
            take_profit, stop_loss = _field(req, "take_profit"), _field(req, "stop_loss")
        except (ValueError, ArithmeticError) as e:
            raise OrderRejected(f"invalid number in order: {e}")
        _in_range(qty, limit, stop, trail_amt, trail_pct, take_profit, stop_loss)
        contract = self._check_symbol(account, symbol)
        if side not in (m.BUY, m.SELL):
            raise OrderRejected("side must be buy or sell")
        if typ not in m.ORDER_TYPES:
            raise OrderRejected(f"unknown order type {typ}")
        if tif not in m.TIFS:
            raise OrderRejected(f"unknown time in force {tif}")
        auction = typ in m.AUCTION_TYPES
        if auction:
            if contract:
                raise OrderRejected("on-open and on-close orders are for stocks only")
            if "tif" in req and tif != m.DAY:
                raise OrderRejected("on-open and on-close orders trade in one auction and take no time in force")
            if req.get("extended_hours"):
                raise OrderRejected("on-open and on-close orders cannot trade in extended hours")
        if linked and (take_profit is not None or stop_loss is not None):
            raise OrderRejected(f"a {linked} order cannot also carry take profit or stop loss")
        if linked and tif in (m.IOC, m.FOK):
            raise OrderRejected(f"{linked} orders cannot be immediate-or-cancel or fill-or-kill")
        expire_at, good_till = self._good_till(req, tif)
        if qty <= 0 or qty > MAX_QTY:
            raise OrderRejected("quantity must be positive and at most 1,000,000")
        if contract and qty != qty.to_integral_value():
            raise OrderRejected("options trade in whole contracts")
        if qty != qty.quantize(Decimal("0.0001")):
            raise OrderRejected("quantity supports at most 4 decimal places")
        if qty != qty.to_integral_value() and (typ not in (m.MARKET, m.LIMIT) or tif != m.DAY):
            raise OrderRejected("fractional shares are limited to market and limit day orders")
        for label, v in (("limit price", limit), ("stop price", stop), ("trail amount", trail_amt),
                         ("trail percent", trail_pct), ("take profit", take_profit), ("stop loss", stop_loss)):
            if v is not None and v <= 0:
                raise OrderRejected(f"{label} must be positive")
        for label, v in (("limit price", limit), ("stop price", stop), ("take profit", take_profit), ("stop loss", stop_loss)):
            if v is not None and v != v.quantize(CENT if v >= 1 else Decimal("0.0001")):
                raise OrderRejected(f"{label} has too many decimal places")
        if typ in (m.LIMIT, m.STOP_LIMIT, m.LOO, m.LOC) and limit is None:
            raise OrderRejected("limit price is required")
        if typ in (m.STOP, m.STOP_LIMIT) and stop is None:
            raise OrderRejected("stop price is required")
        if typ == m.TRAILING_STOP:
            if (trail_amt is None) == (trail_pct is None):
                raise OrderRejected("give exactly one of trail amount or trail percent")
            if trail_pct is not None and trail_pct >= 100:
                raise OrderRejected("trail percent must be below 100")
        if typ in (m.STOP, m.STOP_LIMIT, m.TRAILING_STOP) and tif in (m.IOC, m.FOK):
            raise OrderRejected("stop orders cannot be immediate-or-cancel or fill-or-kill")
        if typ not in (m.LIMIT, m.STOP_LIMIT, m.LOO, m.LOC):
            limit = None
        if typ not in (m.STOP, m.STOP_LIMIT):
            stop = None
        ext = bool(req.get("extended_hours")) and typ == m.LIMIT
        oid = lambda: f"O{next(self._order_seq):08d}"
        client_id = str(req["client_id"])[:64] if req.get("client_id") else None
        entry = Order(id=oid(), account_id=account.id, symbol=symbol, side=side, qty=qty, type=typ, limit_price=limit,
                      stop_price=stop, trail_amount=trail_amt if typ == m.TRAILING_STOP else None,
                      trail_percent=trail_pct if typ == m.TRAILING_STOP else None, extended_hours=ext,
                      tif=(m.OPG if typ in m.OPEN_TYPES else m.CLS) if auction else tif,
                      client_id=client_id, expire_at=expire_at, good_till=good_till,
                      auction_at=self._next_auction(typ) if auction else None)
        orders = [entry]
        if take_profit is not None or stop_loss is not None:
            if typ not in (m.MARKET, m.LIMIT) or tif in (m.IOC, m.FOK) or qty != qty.to_integral_value():
                raise OrderRejected("bracket orders need a whole-share market or limit entry (day, GTC or GTD)")
            ref = limit if limit is not None else (self.quote(symbol).last if self.quote(symbol) else None)
            exit_side = m.SELL if side == m.BUY else m.BUY
            better = (lambda a, b: a > b) if side == m.BUY else (lambda a, b: a < b)
            if ref is not None:
                if take_profit is not None and not better(take_profit, ref):
                    raise OrderRejected("take profit must be on the profitable side of the entry price")
                if stop_loss is not None and not better(ref, stop_loss):
                    raise OrderRejected("stop loss must be on the losing side of the entry price")
            if take_profit is not None:
                orders.append(Order(id=oid(), account_id=account.id, symbol=symbol, side=exit_side, qty=qty, type=m.LIMIT,
                                    limit_price=take_profit, tif=m.GTC, role="take_profit"))
            if stop_loss is not None:
                orders.append(Order(id=oid(), account_id=account.id, symbol=symbol, side=exit_side, qty=qty, type=m.STOP,
                                    stop_price=stop_loss, tif=m.GTC, role="stop_loss"))
            for c in orders[1:]:
                c.parent_id = c.oco_group = entry.id
                c.status = m.HELD
        return orders

    def _estimate_price(self, o: Order, strict: bool = True) -> Decimal:
        """Price used for the risk check. `strict` (a new order) rejects when there is nothing to price
        against, naming the cause; for orders already working the best available reference is used."""
        q = self.quote(o.symbol)
        if q is None or q.last <= 0:
            if not strict:
                return o.limit_price or o.stop_price or self.mark(o.symbol)
            raise OrderRejected(self.feed.no_price_reason(o.symbol))
        if q.stale and strict and o.type == m.MARKET and self.session() == "regular":
            n = self.feed.quote_note(o.symbol)
            raise OrderRejected(f"The quote for {describe(o.symbol)} is stale, so a market order has no current price "
                                f"to fill at. Use a limit order, or wait for a new quote."
                                + (f" {n['note']}" if n and n.get("note") else ""))
        if o.limit_price is not None:
            if o.side == m.BUY:
                return o.limit_price
            return max(o.limit_price, q.bid)
        top = q.ask if o.side == m.BUY else q.bid
        if top <= 0 and q.provider_book:  # a simulated or model quote with a zero bid still walks its book
            if strict and o.type == m.MARKET and self.session() == "regular":
                raise OrderRejected(self._one_sided_reason(o.symbol, o.side))
            px = q.last  # a stop is priced at the last trade until it triggers; it waits for a real quote to fill
        else:
            px = self.engine._walk(q, o.side, o.remaining)
        if o.stop_price is not None:
            return max(px, o.stop_price) if o.side == m.BUY else min(px, o.stop_price)
        return px

    def _one_sided_reason(self, symbol: str, side: str) -> str:
        want = "ask" if side == m.BUY else "bid"
        verb = "buy" if side == m.BUY else "sell"
        n = self.feed.quote_note(symbol)
        tail = f" {n['note']}" if n and n.get("note") else ""
        return (f"No {want} for {describe(symbol)} right now, so a market {verb} has no price to fill at. "
                f"Use a limit order, or wait for {'a' if want == 'bid' else 'an'} {want}.{tail}")

    def _fee(self, symbol: str, qty: Decimal) -> Decimal:
        return cents(qty * (OPTION_FEE if multiplier(symbol) == 100 else STOCK_FEE))

    class _Worst:
        def __init__(self, ok, reason, excess, requirement, cash_after):
            self.ok, self.reason, self.excess, self.requirement, self.cash_after = ok, reason, excess, requirement, cash_after

    def _worst_case(self, account: Account, new_orders: list) -> "_Worst":
        """Check the account under every scenario: all open buys fill, all open sells fill, neither.

        One-cancels-other groups count once per symbol. Held bracket and exit children are
        ignored because they only ever reduce the position their entry order opened. A held
        triggered order (own quantity) is counted together with the entry that must fill
        before it. A multi-leg order is all-or-none, so it is applied as one unit: each
        scenario is checked with no multi-leg order filled, with all of them filled and
        with each one filled alone.

        A cash account cannot borrow, so it gets one more scenario: buys and sells all fill,
        but the money from the sells has not arrived. That stops a resting purchase from
        spending the cash that secures a resting short put. (A sale that must fill before
        the order it triggers is the exception: its money is there by then.)
        """
        live = [o for o in self.engine.orders.values() if o.account_id == account.id
                and (o.status in (m.WORKING, m.PARTIAL) or (o.status == m.HELD and o.role == m.TRIGGERED))]
        live += [o for o in new_orders if o.id not in self.engine.orders]
        singles = [o for o in live if not o.legs]
        combos = [o for o in live if o.legs]
        by_id = {o.id: o for o in singles}
        triggering = {o.parent_id for o in singles if o.status == m.HELD}
        combo_sets = [[]] + ([combos] if combos else []) + ([[c] for c in combos] if len(combos) > 1 else [])
        base = account.positions()
        result = None
        for side in (None, m.BUY, m.SELL) + (("both",) if account.type == "cash" else ()):
            for combo_set in combo_sets:
                pos, cash, seen, applied = dict(base), account.cash, {}, set()

                def apply(o):
                    nonlocal cash
                    applied.add(o.id)
                    qty = o.remaining
                    if o.oco_group:  # only the largest order of a group can fill
                        prior = seen.get((o.oco_group, o.symbol), ZERO)
                        if qty <= prior:
                            return
                        seen[(o.oco_group, o.symbol)], qty = qty, qty - prior
                    sign = 1 if o.side == m.BUY else -1
                    pos[o.symbol] = pos.get(o.symbol, ZERO) + sign * qty
                    cash -= self._fee(o.symbol, qty)
                    if sign > 0 or side != "both" or o.id in triggering:
                        strict = o.id not in self.engine.orders  # a new order; working ones never fail here
                        cash -= sign * cents(qty * self._estimate_price(o, strict) * multiplier(o.symbol))

                for o in singles if side is not None else ():
                    if (o.side != side and side != "both") or o.id in applied:
                        continue
                    if o.status == m.HELD:  # it can only fill after its entry has filled completely
                        parent = by_id.get(o.parent_id)
                        if parent is not None and parent.id not in applied and parent.status != m.HELD:
                            apply(parent)
                    apply(o)
                for o in combo_set:
                    for symbol, delta in self._leg_deltas(o).items():
                        pos[symbol] = pos.get(symbol, ZERO) + delta
                    cash -= self._multileg_cost(o) + self._multileg_fee(o)
                pos = {s: q for s, q in pos.items() if q != 0}
                ev = margin.evaluate(account.type, cash, pos, self._marks(pos))
                w = Exchange._Worst(ev.ok, ev.reason, ev.excess, ev.requirement, cash)
                if result is None or (result.ok and not w.ok) or (w.ok == result.ok and (
                        w.excess < result.excess if account.type == "margin"
                        else w.cash_after - w.requirement < result.cash_after - result.requirement)):
                    result = w
        return result

    def _shortfall(self, account: Account, worst: "_Worst", entry: Optional[Order] = None,
                   what: str = "this order") -> str:
        """The risk check's reason with the amounts: what the order needs and what is available.

        Both are measured against the account with its other open orders counted (as the buying power
        shown in the app is), so the difference is exactly what this order would add."""
        reason = worst.reason or "order rejected by risk check"
        if not reason.startswith("insufficient"):
            return reason  # structural: short selling or uncovered calls in a cash account
        before = self._worst_case(account, [])
        if account.type == "margin":
            option = entry is not None and (bool(entry.legs) or multiplier(entry.symbol) == 100)
            scale = Decimal(1) if option else margin.INITIAL
            have = max(before.excess, ZERO) / scale if before.ok else ZERO
            need = (before.excess - worst.excess) / scale if before.ok else (-worst.excess) / scale
            kind = "option buying power" if option else "buying power"
        else:
            room = lambda w: w.cash_after - w.requirement
            have = max(room(before), ZERO) if before.ok else ZERO
            need = room(before) - room(worst) if before.ok else -room(worst)
            kind = "cash"
        if what == "the change" and reason in ("insufficient buying power", "insufficient cash"):
            reason = "insufficient buying power for the change"
        text = f"{reason}: {what} needs {_usd(max(need, ZERO))} of {kind} and {_usd(have)} is available"
        if entry is not None and entry.stop_price is not None and entry.type in (m.STOP, m.STOP_LIMIT) \
                and entry.side == m.BUY:
            text += (f". A resting stop order reserves buying power as if it fills at its stop price ({_usd(entry.stop_price)}) "
                     "or the current price, whichever is higher")
        pos = {k: v for k, v in account.positions().items() if v != 0}
        ev = margin.evaluate(account.type, account.cash, pos, self._marks(pos))
        if account.type == "margin":
            free = max(ev.excess, ZERO) / scale if ev.ok else ZERO
        else:
            free = max(account.cash - ev.requirement, ZERO) if ev.ok else ZERO
        if cents(free - have) > 0:
            whose = "your open orders, this one at its current size" if what == "the change" else "your other open orders"
            text += f" ({_usd(free - have)} is reserved by {whose})"
        return text

    def _not_riskier(self, account: Account, deltas: dict, cash_delta: Decimal) -> bool:
        """True when applying these position changes (and paying cash_delta) adds no risk.

        Closing a position is normally always allowed, but not when it is what covers
        another one: selling the long leg of a spread, or the shares behind a covered call,
        leaves an uncovered short option and has to pass the full check. Everything that
        other open orders are already selling counts as sold, so two orders cannot each
        lean on the cover the other one removes.
        """
        before = account.positions()
        for x in self.engine.orders.values():
            if x.account_id == account.id and x.status in (m.WORKING, m.PARTIAL):
                pending = self._leg_deltas(x) if x.legs else {x.symbol: x.remaining if x.side == m.BUY else -x.remaining}
                for symbol, delta in pending.items():
                    if delta < 0:
                        before[symbol] = before.get(symbol, ZERO) + delta
        after = dict(before)
        for symbol, delta in deltas.items():
            after[symbol] = after.get(symbol, ZERO) + delta
        before = {s: q for s, q in before.items() if q != 0}
        after = {s: q for s, q in after.items() if q != 0}
        marks = self._marks(set(before) | set(after))
        if account.type == "margin":
            # Buying back a short position never uncovers anything, whatever it does to the numbers.
            if all(d > 0 and account.position(s) < 0 and d <= -account.position(s) for s, d in deltas.items()):
                return True
            return margin.requirement(after, marks) <= margin.requirement(before, marks)
        # Cash account: nothing may become uncovered, and the cash that secures short puts must
        # still be there after paying for the trade (or at least be no further short than it was).
        ev_before = margin.evaluate("cash", account.cash, before, marks)
        ev_after = margin.evaluate("cash", account.cash + cash_delta, after, marks)
        if ev_after.structural or ev_before.structural:
            return ev_before.structural
        spare_before, spare_after = account.cash - ev_before.requirement, account.cash + cash_delta - ev_after.requirement
        return spare_after >= 0 or spare_after >= spare_before

    def _closes(self, account: Account, symbol: str, side: str, qty: Decimal) -> bool:
        """True when trading qty of symbol only reduces the position held, open orders included."""
        pos = account.position(symbol)
        if pos == 0 or (pos > 0) == (side == m.BUY):
            return False
        same_side = sum((x.remaining for x in self.engine.orders.values()
                         if x.account_id == account.id and x.symbol == symbol and x.side == side
                         and x.status in (m.WORKING, m.PARTIAL)), ZERO)
        return same_side + qty <= abs(pos)

    def _reduces_risk(self, account: Account, o: Order) -> bool:
        """True when the order only closes part of an existing position and uncovers nothing."""
        if o.legs:
            return (all(self._closes(account, l["symbol"], l["side"], o.qty * l["ratio"]) for l in o.legs)
                    and self._not_riskier(account, self._leg_deltas(o), -self._multileg_cost(o) - self._multileg_fee(o)))
        if not self._closes(account, o.symbol, o.side, o.qty):
            return False
        sign = 1 if o.side == m.BUY else -1
        paid = sign * cents(o.qty * self._estimate_price(o) * multiplier(o.symbol)) + self._fee(o.symbol, o.qty)
        return self._not_riskier(account, {o.symbol: sign * o.qty}, -paid)

    def _risk_check(self, account: Account, orders: list) -> "_Worst":
        """Pre-trade check for one request (a single order, a linked set or a multi-leg order).

        Returns the worst case; `closing` is set when the request only reduces risk and is
        therefore allowed even if the account is under its requirement.
        """
        entry = orders[0]
        children = [o for o in orders[1:] if o.parent_id == entry.id]
        if children or len(orders) == 1:  # single, bracket or one-triggers-other
            counted = [entry] + [c for c in children if c.role == m.TRIGGERED]
            closing = len(counted) == 1 and self._reduces_risk(account, entry)
        else:  # one-cancels-other
            counted = orders
            closing = all(self._reduces_risk(account, o) for o in orders)
        worst = self._worst_case(account, counted)
        worst.closing = closing
        if not closing and worst.ok:
            # A position without a price would count at zero, which hides the risk of a short one.
            held = set(account.positions()) | {s for o in counted for s in ([l["symbol"] for l in o.legs] if o.legs else [o.symbol])}
            missing = self.lifecycle._missing_prices(held, self._marks(held))
            if missing:
                worst.ok = False
                worst.reason = (f"no price is available yet for {', '.join(missing)}, so the account cannot be "
                                f"valued; orders that only reduce positions are still accepted")
        return worst

    def _rule_check(self, account: Account, orders: list, worst: "_Worst") -> Optional[str]:
        """Lifecycle pre-trade rules (pattern day trader, settled cash) for one request, or None.

        Every order the request can fill right away is checked: the entry, each order of a
        one-cancels-other group and each leg of a multi-leg order. Held children of a bracket
        or one-triggers-other chain are covered by the cash check of their entry (the worst
        case counts them) and, like bracket exits, are not checked again when released.
        """
        entry = orders[0]
        if entry.legs:
            checked = [Order(id=entry.id, account_id=account.id, symbol=l["symbol"], side=l["side"],
                             qty=entry.qty * l["ratio"], type=m.MARKET) for l in entry.legs]
        elif len(orders) > 1 and orders[1].parent_id != entry.id:  # standalone one-cancels-other
            checked = orders
        else:
            checked = [entry]
        for o in checked:
            rule = self.lifecycle.pre_trade(account, o, None if worst.closing else worst)
            if rule:
                return rule
        return None

    def preview(self, account_id: str, req: dict) -> dict:
        a = self.account(account_id)
        if isinstance(req, dict) and req.get("legs") is not None:
            return self.preview_multileg(account_id, req)
        orders = self._validate(a, req)
        entry = orders[0]
        price = self._estimate_price(entry)
        mult = multiplier(entry.symbol)
        notional = cents(entry.qty * price * mult)
        fee = self._fee(entry.symbol, entry.qty)
        before = self.summary(account_id)
        worst = self._risk_check(a, orders)
        closing = worst.closing
        ok = closing or worst.ok
        rule = self._rule_check(a, orders, worst) if ok else None
        ok = ok and rule is None
        pos = a.position(entry.symbol)
        after_pos = pos + (entry.qty if entry.side == m.BUY else -entry.qty)
        if a.type == "margin":
            bp_after = cents(max(worst.excess, ZERO) / margin.INITIAL)
        else:
            bp_after = cents(max(worst.cash_after - worst.requirement, ZERO))
        warnings = []
        if after_pos < 0 and pos >= 0 and mult == 1:
            warnings.append("This order opens a short position.")
        if after_pos < 0 and mult == 100:
            warnings.append("This order writes (sells to open) option contracts.")
        if entry.type == m.MARKET:
            warnings.append("Market orders fill at the available price, which can differ from the estimate.")
        q = self.quote(entry.symbol)
        if q is not None and q.provider_book and entry.type in (m.MARKET, m.STOP, m.TRAILING_STOP) and \
                (q.ask if entry.side == m.BUY else q.bid) <= 0:
            want = "ask" if entry.side == m.BUY else "bid"
            warnings.append(f"There is no {want} for {describe(entry.symbol)} right now; the order cannot fill until "
                            f"there is one, and the estimate uses the last price.")
        n = self.feed.quote_note(entry.symbol)
        if n and n.get("stale") and n.get("note"):
            warnings.append(n["note"])
        if entry.type in m.AUCTION_TYPES:
            which = "open" if entry.type in m.OPEN_TYPES else "close"
            warnings.append(f"This order waits for the next market {which} and trades once, at the {which}ing price."
                            + (" In this session mode the market never closes, so that is the next UTC midnight."
                               if self.session_mode == "always" else ""))
            if entry.limit_price is not None:
                warnings.append(f"If the {which}ing price is worse than the limit, the order expires unfilled.")
        elif self.session() != "regular" and not entry.extended_hours:
            warnings.append("The market is closed; the order will wait for the regular session.")
        if entry.good_till:
            warnings.append(f"This order expires at the close on {entry.good_till} if it has not filled.")
        if len(orders) > 1 and orders[1].parent_id != entry.id:
            warnings.append("One-cancels-other: a fill on one of these orders reduces or cancels the others.")
        if any(o.role in (m.EXIT, m.TRIGGERED) for o in orders[1:]):
            warnings.append("The triggered orders stay on hold until this order fills.")
        return {"ok": ok, "reason": None if ok else rule or self._shortfall(a, worst, entry), "symbol": entry.symbol,
                "description": describe(entry.symbol), "side": entry.side, "qty": entry.qty,
                "estimated_price": price, "estimated_notional": notional, "estimated_fees": fee,
                "estimated_total": notional + fee if entry.side == m.BUY else notional - fee,
                "position_before": pos, "position_after": after_pos, "buying_power_before": before["buying_power"],
                "buying_power_after": bp_after if not closing else None, "closing": closing, "warnings": warnings,
                "linked_orders": [{"symbol": o.symbol, "description": describe(o.symbol), "side": o.side, "qty": o.qty,
                                   "type": o.type, "role": o.role} for o in orders[1:]]}

    def submit(self, account_id: str, req: dict) -> list:
        a = self.account(account_id)
        if not isinstance(req, dict):
            raise OrderRejected("an order must be a JSON object")
        if req.get("client_id"):
            for o in self.engine.orders.values():
                if o.account_id == account_id and o.client_id == str(req["client_id"])[:64]:
                    return [o] + [c for c in self.engine.orders.values()
                                  if c.id != o.id and (c.parent_id == o.id or c.oco_group == o.id)]
        orders = self._validate(a, req)
        entry = orders[0]
        worst = self._risk_check(a, orders)
        if not (worst.closing or worst.ok):
            raise OrderRejected(self._shortfall(a, worst, entry))
        rule = self._rule_check(a, orders, worst)  # pattern day trader and settled-cash rules
        if rule:
            raise OrderRejected(rule)
        now = self.now()
        sess, q = self.session(now), self._tradable(self.quote(entry.symbol))  # a stale quote fills nothing
        if entry.legs:
            events = self.engine.submit_multileg(entry, self._leg_quotes(entry.legs, tradable=True), now, sess)
        elif len(orders) == 1:
            events = self.engine.submit(entry, q, now, sess)
        elif orders[1].parent_id == entry.id:  # bracket or one-triggers-other
            events = self.engine.submit_linked(entry, orders[1:], q, now, sess, oco=orders[1].oco_group == entry.id)
        else:  # standalone one-cancels-other
            events = self.engine.submit_oco(orders, {o.symbol: self._tradable(self.quote(o.symbol)) for o in orders},
                                            now, sess)
        self._handle(events)
        return orders

    def cancel(self, account_id: str, order_id: str) -> Order:
        o = self.engine.orders.get(order_id)
        if o is None or o.account_id != account_id:
            raise NotFound("no such order")
        if not o.active:
            raise ValueError("order is no longer open")
        self._handle(self.engine.cancel(order_id, self.now(), "canceled by user"))
        return o

    def cancel_all(self, account_id: str) -> int:
        n = 0
        for o in list(self.engine.active_orders()):
            if o.account_id == account_id and o.active:
                self._handle(self.engine.cancel(o.id, self.now(), "canceled by user"))
                n += 1
        return n

    def replace(self, account_id: str, order_id: str, req: dict) -> Order:
        o = self.engine.orders.get(order_id)
        if o is None or o.account_id != account_id:
            raise NotFound("no such order")
        a = self.account(account_id)
        if o.legs:
            raise OrderRejected("a multi-leg order cannot be changed; cancel it and enter a new one")
        try:
            qty, limit, stop = _field(req, "qty"), _field(req, "limit_price"), _field(req, "stop_price")
        except (ValueError, ArithmeticError) as e:
            raise OrderRejected(f"invalid number in the change: {e}")
        _in_range(qty, limit, stop)
        for v in (qty, limit, stop):
            if v is not None and v <= 0:
                raise ValueError("values must be positive")
        if qty is not None and qty > o.qty and o.oco_group:
            # the group's size is what its orders had when it was entered; fills on one have
            # already been taken off the others, and raising one would let the group over-fill
            raise OrderRejected("the quantity of a one-cancels-other order cannot be raised")
        if qty is not None and (qty > MAX_QTY or qty != qty.quantize(Decimal("0.0001"))):
            raise ValueError("invalid quantity")
        old = (o.qty, o.limit_price, o.stop_price)
        self._handle(self.engine.replace(order_id, self.now(), qty, limit, stop))
        # Held bracket and exit children are re-sized to what their entry filled when they are
        # released, so editing them cannot add risk; a held triggered order keeps its own size.
        if (o.status != m.HELD or o.role == m.TRIGGERED) and not (after := self._worst_case(a, [])).ok \
                and not self._reduces_risk_after(a, o):
            o.qty, o.limit_price, o.stop_price = old
            raise OrderRejected(self._shortfall(a, after, o, "the change"))
        if o.status != m.HELD:  # pattern day trader and settled-cash rules, as on submit
            rule = self.lifecycle.pre_trade(a, o, None if self._reduces_risk_after(a, o) else self._worst_case(a, []))
            if rule:
                o.qty, o.limit_price, o.stop_price = old
                raise OrderRejected(rule)
        return o

    def _reduces_risk_after(self, account: Account, o: Order) -> bool:
        pos = account.position(o.symbol)
        return pos != 0 and (pos > 0) != (o.side == m.BUY) and o.remaining <= abs(pos)

    # ---- multi-leg orders ---------------------------------------------------
    def _validate_multileg(self, account: Account, req: dict) -> Order:
        """Two to four legs on one underlying, priced as one order. Raises OrderRejected."""
        raw = req.get("legs")
        if req.get("oco") is not None or req.get("triggers") is not None:
            raise OrderRejected("multi-leg orders cannot be linked to other orders")
        if not isinstance(raw, list) or not all(isinstance(l, dict) for l in raw) or not 2 <= len(raw) <= 4:
            raise OrderRejected("a multi-leg order needs two to four legs")
        for key in ("take_profit", "stop_loss", "stop_price", "trail_amount", "trail_percent"):
            if req.get(key) not in (None, ""):
                raise OrderRejected("multi-leg orders are market or net-limit orders without stops or brackets")
        try:
            qty = parse_num(req.get("qty", 1), FIELD_LABELS["qty"])
            limit = _field(req, "limit_price")
            ratios = [parse_num(l.get("ratio", 1), f"leg {i} ratio") for i, l in enumerate(raw, 1)]
        except (ValueError, ArithmeticError) as e:
            raise OrderRejected(f"invalid number in order: {e}")
        _in_range(qty, limit, *ratios)
        typ, tif, effect = req.get("type", m.MARKET), req.get("tif", m.DAY), req.get("price_effect")
        if typ not in (m.MARKET, m.LIMIT):
            raise OrderRejected("multi-leg orders are market or limit orders")
        if tif not in m.TIFS:
            raise OrderRejected(f"unknown time in force {tif}")
        if req.get("extended_hours"):
            raise OrderRejected("multi-leg orders trade in the regular session only")
        if qty <= 0 or qty != qty.to_integral_value() or qty > 10000:
            raise OrderRejected("quantity must be a whole number from 1 to 10,000")
        legs, underlying, stock_legs = [], None, 0
        for l, ratio in zip(raw, ratios):
            symbol = str(l.get("symbol", "")).upper().strip()
            contract = self._check_symbol(account, symbol)
            if l.get("side") not in (m.BUY, m.SELL):
                raise OrderRejected("each leg needs a side: buy or sell")
            if ratio <= 0 or ratio != ratio.to_integral_value() or ratio > (100 if contract else 10000):
                raise OrderRejected("option leg ratios are whole numbers up to 100" if contract
                                    else "the stock leg ratio is whole shares per unit, up to 10,000")
            leg_underlying = contract.underlying if contract else symbol
            if underlying not in (None, leg_underlying):
                raise OrderRejected("all legs must be on the same underlying")
            underlying = leg_underlying
            stock_legs += contract is None
            if symbol in (x["symbol"] for x in legs):
                raise OrderRejected(f"{describe(symbol)} appears in more than one leg")
            legs.append({"symbol": symbol, "side": l["side"], "ratio": int(ratio)})
        if stock_legs > 1 or stock_legs == len(legs):
            raise OrderRejected("a multi-leg order has option legs and at most one stock leg")
        if typ == m.LIMIT:
            if limit is None:
                raise OrderRejected("net limit price is required")
            if limit < 0:
                raise OrderRejected("net limit price cannot be negative; use price_effect debit or credit")
            if limit != limit.quantize(CENT):
                raise OrderRejected("net limit price has too many decimal places")
            if limit != 0 and effect not in ("debit", "credit"):
                raise OrderRejected("price_effect must be debit (you pay) or credit (you receive)")
            limit = -limit if effect == "credit" and limit != 0 else limit
        else:
            limit = None
        expire_at, good_till = self._good_till(req, tif)
        return Order(id=f"O{next(self._order_seq):08d}", account_id=account.id, symbol=underlying, side=m.BUY,
                     qty=qty, type=m.MULTILEG, limit_price=limit, tif=tif, legs=legs, expire_at=expire_at,
                     good_till=good_till, client_id=str(req["client_id"])[:64] if req.get("client_id") else None)

    def _leg_quotes(self, legs: list, tradable: bool = False) -> dict:
        if tradable:  # for matching: a stale leg quote counts as none, so the order waits
            return {l["symbol"]: self._tradable(self.quote(l["symbol"])) for l in legs}
        return {l["symbol"]: self.quote(l["symbol"]) for l in legs}

    @staticmethod
    def _leg_deltas(o: Order) -> dict:
        return {l["symbol"]: o.qty * l["ratio"] * (1 if l["side"] == m.BUY else -1) for l in o.legs}

    def _multileg_net(self, o: Order) -> Decimal:
        """Net price per unit used for risk: the limit (the worst price it may fill at) or the market."""
        if o.limit_price is not None:
            return o.limit_price
        priced = net_price(o.legs, self._leg_quotes(o.legs))
        if priced is None:
            raise OrderRejected(f"every leg of the {o.symbol} order needs a quote, and a leg to be sold needs a bid above zero")
        return priced[0]

    def _multileg_cost(self, o: Order) -> Decimal:
        return cents(self._multileg_net(o) * 100 * o.qty)

    def _multileg_fee(self, o: Order) -> Decimal:
        return sum((self._fee(l["symbol"], o.qty * l["ratio"]) for l in o.legs), ZERO)

    def preview_multileg(self, account_id: str, req: dict) -> dict:
        """Net price, payoff, margin effect and fees of a multi-leg order, without placing it."""
        a = self.account(account_id)
        o = self._validate_multileg(a, req)
        quotes = self._leg_quotes(o.legs)
        priced = net_price(o.legs, quotes)
        if priced is None:
            raise OrderRejected(f"every leg of the {o.symbol} order needs a quote, and a leg to be sold needs a bid above zero")
        market_net, prices = priced
        net = o.limit_price if o.limit_price is not None else market_net  # the price the payoff is based on
        effect = lambda x: "debit" if x > 0 else "credit" if x < 0 else "even"
        shape = payoff(o.legs, net)
        scale = lambda v: None if v is None else cents(v * o.qty)
        fees = self._multileg_fee(o)
        worst = self._risk_check(a, [o])
        ok = worst.closing or worst.ok
        rule = self._rule_check(a, [o], worst) if ok else None
        ok = ok and rule is None
        before_pos = a.positions()
        after_pos = dict(before_pos)
        for symbol, delta in self._leg_deltas(o).items():
            after_pos[symbol] = after_pos.get(symbol, ZERO) + delta
        after_pos = {s: q for s, q in after_pos.items() if q != 0}
        marks = self._marks(set(before_pos) | set(after_pos))
        cash_after = a.cash - cents(net * 100 * o.qty) - fees
        if a.type == "margin":
            req_before, req_after = margin.requirement(before_pos, marks), margin.requirement(after_pos, marks)
            bp_after = cents(max(worst.excess, ZERO) / margin.INITIAL)
        else:
            req_before = margin.evaluate("cash", a.cash, before_pos, marks).requirement
            req_after = margin.evaluate("cash", cash_after, after_pos, marks).requirement
            bp_after = cents(max(worst.cash_after - worst.requirement, ZERO))
        # Exchange.quote() uses the provider's option quote when the feed has one, else the model.
        sources = {l["symbol"]: self.quote_source(l["symbol"]) for l in o.legs}
        modeled = [sym for sym in sources if sources[sym] == "model"]
        simulated = bool(modeled) or self.feed.simulated
        warnings = []
        if modeled and len(modeled) == sum(parse_option(sym) is not None for sym in sources):
            warnings.append("Option prices are simulated model prices, not market data.")
        elif modeled:
            warnings.append("Some legs use simulated model prices, not market data: "
                            + ", ".join(describe(sym) for sym in modeled) + ".")
        if not shape["at_expiry"]:
            warnings.append("The legs expire on different dates, so profit and break-even prices depend on what "
                            "the later options are worth when the first one expires and are not shown.")
        if shape["max_loss_unlimited"]:
            warnings.append("The possible loss on this order is not limited.")
        if o.limit_price is None:
            warnings.append("Market orders fill at the available prices, which can differ from the estimate.")
        if self.session() != "regular":
            warnings.append("The market is closed; the order will wait for the regular session.")
        rows = []
        for l, px in zip(o.legs, prices):
            q, held = quotes[l["symbol"]], a.position(l["symbol"])
            leg_qty = o.qty * l["ratio"]
            rows.append({"symbol": l["symbol"], "description": describe(l["symbol"]), "side": l["side"],
                         "ratio": l["ratio"], "qty": leg_qty, "bid": q.bid, "ask": q.ask, "estimated_price": px,
                         "source": sources[l["symbol"]],
                         "position_before": held, "position_after": held + (leg_qty if l["side"] == m.BUY else -leg_qty)})
        return {"ok": ok, "reason": None if ok else rule or self._shortfall(a, worst, o), "underlying": o.symbol, "qty": o.qty,
                "type": m.LIMIT if o.limit_price is not None else m.MARKET, "tif": o.tif, "legs": rows,
                "net_price": abs(net), "price_effect": effect(net),
                "market_net_price": abs(market_net), "market_price_effect": effect(market_net),
                "marketable": o.limit_price is None or market_net <= o.limit_price,
                "estimated_cost": cents(net * 100 * o.qty), "estimated_fees": fees,
                "estimated_total": cents(net * 100 * o.qty) + fees,
                "max_profit": scale(shape["max_profit"]), "max_profit_unlimited": shape["max_profit_unlimited"],
                "max_loss": scale(shape["max_loss"]), "max_loss_unlimited": shape["max_loss_unlimited"],
                "break_evens": shape["break_evens"], "payoff_at_expiry": shape["at_expiry"],
                "requirement_before": req_before, "requirement_after": req_after,
                "margin_effect": req_after - req_before,
                "buying_power_before": self.summary(account_id)["buying_power"],
                "buying_power_after": None if worst.closing else bp_after, "closing": worst.closing,
                "simulated": simulated, "warnings": warnings}

    # ---- auctions, good-till-date and multi-leg matching (called from tick) ----
    def _next_auction(self, typ: str) -> float:
        """Time of the next open (for on-open orders) or close (on-close) after now.

        With real sessions that is the start or the end of the next regular session. In
        session mode 'always' the market never closes, so the next UTC midnight is both:
        the close on the last prices of the old day, then the open on the first prices of
        the new one.
        """
        now = self.now()
        if self.session_mode == "always":
            return (now // 86400 + 1) * 86400
        day = cal.ny_date(now)
        for _ in range(30):
            if cal.is_trading_day(day):
                at = (datetime.combine(day, cal.OPEN, tzinfo=cal.NY).timestamp() if typ in m.OPEN_TYPES
                      else cal.close_ts(day))
                if at > now:
                    return at
            day += timedelta(days=1)
        raise OrderRejected("no trading session was found in the next 30 days")

    def _auction_price(self, symbol: str) -> Optional[Quote]:
        """Closing price: the last quote the symbol had in a regular session, else its current quote."""
        return self._session_quotes.get(symbol) or self.quote(symbol)

    def _orders_before_feed(self, now: float, sess: str) -> None:
        """Run the closing auction and expire good-till-date orders, before the feed moves on.

        Every on-open or on-close order carries the time of the one auction it waits for
        (`auction_at`, saved with the order). It trades on the first tick at or after that
        time. If the simulator was not running when the time passed (the first tick after a
        start finds it already in the past), the auction was missed and the order expires
        unfilled instead of silently waiting for another day's auction.
        """
        due = [o for o in self.engine.orders.values() if o.type in m.AUCTION_TYPES and o.status == m.WORKING
               and (o.auction_at is None or o.auction_at <= now)]
        self._due_open = set()
        for o in due:
            which = "open" if o.type in m.OPEN_TYPES else "close"
            if o.auction_at is None:  # saved before auction times existed: wait for the next one
                o.auction_at = self._next_auction(o.type)
            elif self._last_tick is None or self._last_tick >= o.auction_at:
                self._handle(self.engine.cancel(o.id, now, f"the {which} passed while the simulator was not running",
                                                status=m.EXPIRED))
            elif which == "open":
                self._due_open.add(o.id)
        closing = {o.id for o in due if o.status == m.WORKING and o.type in m.CLOSE_TYPES and o.auction_at <= now}
        if closing:
            self._handle(self.engine.on_auction("close", self._auction_price, now, only=closing))
        self._handle(self.engine.expire(now, lambda o: o.tif == m.GTD and o.expire_at is not None and o.expire_at <= now))

    def _orders_after_feed(self, quotes: list, now: float, sess: str) -> None:
        """Run the opening auction on the first quotes of the session, then re-price multi-leg orders."""
        if self._due_open:
            due, self._due_open = self._due_open, set()
            self._handle(self.engine.on_auction("open", self.quote, now, only=due))
        if sess == "regular":
            for q in quotes:
                self._session_quotes[q.symbol] = q
        self._last_tick = now
        changed = {q.symbol for q in quotes}
        for o in [o for o in self.engine.orders.values() if o.legs and o.status == m.WORKING]:
            if any(c is not None and c.expiry_ts <= now for c in (parse_option(l["symbol"]) for l in o.legs)):
                self._handle(self.engine.cancel(o.id, now, "option expired", status=m.EXPIRED))
            elif o.symbol in changed:
                self._handle(self.engine.on_multileg(o.id, self._leg_quotes(o.legs, tradable=True), now, sess))

    # ---- event handling ----------------------------------------------------
    def _handle(self, events: list) -> None:
        for kind, obj in events:
            if kind == "fill":
                acct = self.accounts.get(obj.account_id)
                if acct is None:
                    continue
                obj.fee = self._fee(obj.symbol, obj.qty)
                acct.apply_fill(obj, multiplier(obj.symbol))
                self.lifecycle.on_fill(acct, obj)
                order = self.engine.orders.get(obj.order_id)
                if order:
                    order.fees += obj.fee
                self.events.append({"type": "fill", "account_id": obj.account_id, "fill": obj})
            else:
                self.events.append({"type": "order", "account_id": obj.account_id, "order": obj})
            self.dirty = True

    # ---- the heartbeat -----------------------------------------------------
    def tick(self) -> list:
        """Advance the market one step and process everything that depends on it."""
        now = self.now()
        sess = self.session(now)
        prev_tick = self._last_tick  # None on the first tick after a start
        self._after_restart(now, prev_tick)
        self._orders_before_feed(now, sess)
        self._roll_sessions(now, sess)
        self.lifecycle.before_step(now)  # day roll: accruals, settlement, corporate actions, dividends
        quotes = self.feed.step(now)
        self._orders_after_feed(quotes, now, sess)
        active_symbols = {o.symbol for o in self.engine.orders.values() if o.status in (m.WORKING, m.PARTIAL)}
        for q in quotes:
            if q.symbol in active_symbols and not q.stale:  # never fill against a stale quote
                self._handle(self.engine.on_quote(q, now, sess))
        changed = {q.symbol for q in quotes}
        for sym in active_symbols:
            c = parse_option(sym)
            if c and c.underlying in changed and c.expiry_ts > now and (oq := self._tradable(self.quote(sym))):
                self._handle(self.engine.on_quote(oq, now, sess))
        self._process_expirations(now, prev_tick)
        self._settle_day_start(now)
        self.lifecycle.after_step(now)  # margin calls and liquidation
        self.alerting.on_tick(quotes, now)
        if now - self._last_equity_sample >= 60:
            self._last_equity_sample = now
            for a in self.accounts.values():
                s = self.summary(a.id)
                if s["prices_unavailable"]:  # a position cannot be valued: a sample now would value it at zero
                    continue
                eq = s["equity"]
                a.equity_history.append([int(now), str(eq)])
                if len(a.equity_history) > 6000:
                    a.equity_history = a.equity_history[::2]
            self.dirty = True
        return quotes

    def _roll_sessions(self, now: float, sess: str) -> None:
        # Day P&L restarts with the trading day: the New York date in "us" mode, the UTC day in "always"
        # mode (where day orders also expire at that boundary, the next on-open/on-close auction time).
        day = (cal.trading_day(now, "us" if self.session_mode == "us" else "always") - cal.EPOCH).days
        if self._last_day is not None and day != self._last_day:
            for a in self.accounts.values():
                self._day_start_wanted[a.id] = "now"
            self._settle_day_start(now)
            if self.session_mode == "always":
                self._handle(self.engine.expire(now, lambda o: o.tif == m.DAY and o.parent_id is None))
            self.dirty = True
        self._last_day = day
        if self.session_mode != "always" and self._last_session is not None and sess != self._last_session:
            if self._last_session == "regular":
                self._handle(self.engine.expire(now, lambda o: o.tif == m.DAY and not o.extended_hours and o.parent_id is None))
            if sess == "closed":
                self._handle(self.engine.expire(now, lambda o: o.tif == m.DAY and o.parent_id is None))
        self._last_session = sess

    # ---- the first tick after a start or a pause ------------------------------------------------
    def _day_order_end(self, o: Order) -> float:
        """When a day order's own session ends, by its creation time and the session calendar.

        This is when the live roll (_roll_sessions) expires it: in session mode 'always' at the next UTC
        midnight; with real sessions at the regular close of the day it was placed for, or at the end of
        the extended session (20:00 New York) for an extended-hours order or one placed after the close.
        An order placed while the market is closed is for the next trading day.
        """
        t = o.created_at
        if self.session_mode == "always":  # the end of its trading day (the UTC day)
            return cal.trading_day_start(cal.trading_day(t, "always") + timedelta(days=1), "always")
        d, sess = cal.trading_day(t, "us"), cal.session(t)
        post_end = lambda day: datetime.combine(day, cal.POST_CLOSE, tzinfo=cal.NY).timestamp()
        if sess == "closed":
            if not (cal.is_trading_day(d) and cal.ny_datetime(t).time() < cal.PRE_OPEN):
                d += timedelta(days=1)
                while not cal.is_trading_day(d):
                    d += timedelta(days=1)
            return post_end(d) if o.extended_hours else cal.close_ts(d)
        if o.extended_hours or sess == "post":
            return post_end(d)
        return cal.close_ts(d)

    def _mode(self) -> str:
        return "us" if self.session_mode == "us" else "always"

    def _after_restart(self, now: float, prev_tick: Optional[float]) -> None:
        """Catch up, on the first tick after a start or a pause, on what the live roll would have done.

        - Day orders whose own session ended meanwhile expire (good-till-date orders and on-open or
          on-close orders already carry their end time and expire in _orders_before_feed).
        - After a start on a later trading day, the day start equity is reset (see _settle_day_start).
        """
        if prev_tick is not None and now - prev_tick <= RUNNING_GAP:
            return
        for o in sorted(self.engine.active_orders(), key=lambda o: o.id):
            if o.active and o.tif == m.DAY and o.parent_id is None and self._day_order_end(o) <= now:
                self._handle(self.engine.cancel(o.id, now, "the trading day it was placed for ended while the "
                                                           "simulator was not running", status=m.EXPIRED))
        last = self.lifecycle.last_date  # the trading day of the last tick before the start (saved)
        if prev_tick is None and last is not None and last < cal.trading_day(now, self._mode()):
            for a in self.accounts.values():
                self._day_start_wanted[a.id] = "restart"

    def _settle_day_start(self, now: float) -> None:
        """Set the day start equity of accounts that wait for it, once all their positions have prices.

        After a restart on a later day it is the last equity sample taken before today began, if that
        sample is from the previous business day or later (nothing happened in between); otherwise the
        current equity, with a note.
        """
        days = self.lifecycle.days
        today = cal.trading_day(now, self._mode())  # the one definition of the trading day (core/calendar)
        start = cal.trading_day_start(today, self._mode())
        prev_day = days.current_or_previous(today - timedelta(days=1))
        for aid, how in list(self._day_start_wanted.items()):
            a = self.accounts.get(aid)
            if a is None:
                self._day_start_wanted.pop(aid)
                continue
            s = self.summary(aid)
            if s["prices_unavailable"]:
                continue
            self._day_start_wanted.pop(aid)
            self.dirty = True
            if how == "restart":
                before = [r for r in a.equity_history if r[0] < start]
                if before and cal.trading_day(before[-1][0], self._mode()) >= prev_day:
                    a.day_start_equity = D(before[-1][1])
                    continue
                self.lifecycle._event(a, "day_start", "Day P&L is measured from the equity when prices first "
                                                      "arrived after the start, because no equity was recorded at "
                                                      "the end of the previous trading day.")
            a.day_start_equity = s["equity"]

    def _settlement_price(self, c, now: float, prev_tick: Optional[float]):
        """(price, exact, note) to settle contracts of c's underlying and expiry with, or None to wait.

        The settlement price is the underlying's closing price on the expiry date:
        - the simulator was running at the close: the last regular-session price it saw;
        - otherwise the feed's closing_price(symbol, day) when it has that method (it never blocks; "not
          ready" is asked again on later ticks for up to PROVIDER_CLOSE_WAIT seconds);
        - for a feed without it: the daily bar for that date in the feed's bar store (synthetic and replay
          feeds), or for a live feed the provider's daily bar fetched in the background (never on the
          tick path) for up to CLOSE_WAIT seconds;
        - otherwise the first price available now, marked as approximate.
        Never a missing or zero price: while there is none, the contracts wait.
        """
        u, iso = c.underlying, c.expiry.isoformat()
        key = (u, iso)
        if key in self._settle_prices:
            return self._settle_prices[key]
        result = None
        if prev_tick is not None and prev_tick < c.expiry_ts and c.expiry_ts - prev_tick <= RUNNING_GAP:
            q = self._session_quotes.get(u) or self.feed.quote(u)
            if q is not None and q.last > 0:
                result = (q.last, True, f"{u} closed at {q.last:f} on {iso}")
        closing_price = getattr(self.feed, "closing_price", None)  # MarketDataProvider.closing_price (provider stream)
        if result is None and closing_price is not None:
            asked = self._close_fetches.setdefault(key, {"started": now, "done": False})
            try:
                cp = closing_price(u, c.expiry)
            except Exception:  # a provider fault: treat as "no close available"
                cp = None
            if cp is not None and not cp.ready and now - asked["started"] < PROVIDER_CLOSE_WAIT:
                return None  # the feed is fetching it off the tick path: ask again on a later tick
            if cp is not None and cp.ready and cp.price is not None and cp.price > 0:
                px = cents(D(cp.price))
                result = (px, True, f"{u} closed at {px:f} on {iso}" + (f" ({cp.note})" if cp.note else ""))
            asked["done"] = True
        elif result is None:
            day_ts = int((c.expiry - date(1970, 1, 1)).total_seconds())
            for r in reversed(self.feed.store.daily.get(u) or ()):
                if r[0] == day_ts and r[4] > 0:
                    px = cents(D(repr(r[4])))
                    result = (px, True, f"{u} closed at {px:f} on {iso}")
                    break
        if result is None and closing_price is None and not self.feed.simulated:
            fetch = self._close_fetches.get(key)
            if fetch is None:
                fetch = self._close_fetches[key] = {"started": now, "done": False}
                self._fetch_close(key, c.expiry, now)
            if key in self._settle_prices:  # the fetch finished at once
                return self._settle_prices[key]
            if not fetch["done"] and now - fetch["started"] < CLOSE_WAIT:
                return None
        if result is None:
            q = self.feed.quote(u)
            if q is None or q.last <= 0:
                return None
            result = (q.last, False, f"approximate price: {u}'s closing price on {iso} was not available, so the "
                                     f"first price after the expiry ({q.last:f}) was used")
        self._settle_prices[key] = result
        return result

    def _fetch_close(self, key: tuple, expiry: date, now: float) -> None:
        """Ask the provider, off the tick path, for the underlying's daily bar on the expiry date."""
        u, iso = key
        day_ts = int((expiry - date(1970, 1, 1)).total_seconds())
        limit = max(5, int((now - day_ts) // 86400) + 5)
        fetch = self._close_fetches[key]

        def run():
            try:
                for b in self.feed.bars(u, "1d", limit):
                    if int(b["time"]) == day_ts and b["close"] > 0:
                        px = cents(D(repr(b["close"])))
                        self._settle_prices[key] = (px, True, f"{u} closed at {px:f} on {iso} (provider daily bar)")
                        break
            except Exception:  # a provider fault: fall back to the first available price
                pass
            finally:
                fetch["done"] = True

        self.background(run)

    def _process_expirations(self, now: float, prev_tick: Optional[float] = None) -> None:
        for o in self.engine.active_orders():
            c = parse_option(o.symbol)
            if c is not None and c.expiry_ts <= now:
                self._handle(self.engine.cancel(o.id, now, "option expired", status=m.EXPIRED))
        for a in self.accounts.values():
            expiring = []
            for sym, qty in a.positions().items():
                c = parse_option(sym)
                if c is None or c.expiry_ts > now:
                    continue
                price = self._settlement_price(c, now, prev_tick)
                if price is None:  # no price for the underlying yet: settle when one arrives
                    if (a.id, sym) not in self._expiry_waiting:
                        self._expiry_waiting.add((a.id, sym))
                        self.lifecycle._event(a, "expiration", f"{describe(sym)} has expired and will be settled "
                                                               f"when a price for {c.underlying} arrives.")
                    continue
                expiring.append((sym, qty, c, price))
            if a.type == "cash":
                # Obligations first, then whatever brings cash in before whatever needs it:
                # short calls (deliver shares), short puts (pay), long puts (deliver), long calls (pay).
                expiring.sort(key=lambda r: ((r[1] > 0) * 2 + ((r[2].kind == "P") != (r[1] > 0)), r[0]))
            for sym, qty, c, (spot, exact, note) in expiring:
                itm = (spot - c.strike) if c.kind == "C" else (c.strike - spot)
                long = qty > 0
                n = abs(qty)

                def book(symbol, side, q, price, kind):
                    f = Fill(id=f"X{next(self._expiry_seq):08d}", order_id=None, account_id=a.id, symbol=symbol,
                             side=side, qty=q, price=price, ts=now, kind=kind)
                    a.apply_fill(f, multiplier(symbol))
                    a.ledger.transactions[-1].memo += f" (settlement: {note})"
                    self.lifecycle.on_fill(a, f)  # exercise and assignment proceeds settle T+1
                    self.events.append({"type": "fill", "account_id": a.id, "fill": f})

                if itm >= CENT:
                    kind = "exercise" if long else "assignment"
                    # A call holder buys the stock; a put holder sells it. The writer takes the other side.
                    buys_stock = (c.kind == "C") == long
                    settle = n
                    if a.type == "cash":
                        # A cash account cannot borrow money or shares. Contracts it cannot settle
                        # in shares are closed for their intrinsic value in cash instead, as a
                        # broker would close them just before the last bell.
                        settle = min(n, (max(a.cash, ZERO) // (c.strike * 100)) if buys_stock
                                     else (max(a.position(c.underlying), ZERO) // 100))
                    if settle > 0:
                        book(sym, m.SELL if long else m.BUY, settle, ZERO, kind)
                        book(c.underlying, m.BUY if buys_stock else m.SELL, settle * 100, c.strike, kind)
                    if n > settle:
                        book(sym, m.SELL if long else m.BUY, n - settle, itm, CLOSED_AT_EXPIRY)
                else:
                    book(sym, m.SELL if long else m.BUY, n, ZERO, "expiration")
                if not exact:
                    self.lifecycle._log(a, now, "expiration", f"{describe(sym)} was settled at an {note}.")
                self._expiry_waiting.discard((a.id, sym))
                self.dirty = True

    # ---- alerts and watchlists ----------------------------------------------
    @property
    def alerts(self) -> list:
        """Every alert record (app/alerts/model.py). Price alerts keep the original fields."""
        return self.alerting.list()

    def add_alert(self, symbol: str, op: str, price) -> dict:
        return self.alerting.create({"kind": "price", "symbol": symbol, "op": op, "price": price})

    def remove_alert(self, alert_id: str) -> None:
        self.alerting.delete(alert_id)

    def drain_events(self) -> list:
        out = list(self.events)
        self.events.clear()
        return out

    # ---- persistence -----------------------------------------------------------
    def _path(self, name: str) -> str:
        return os.path.join(self.data_dir, name)

    @staticmethod
    def _write(path: str, data: dict) -> None:
        # The temp file is created in the target directory, so the replace never crosses volumes.
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
            # On Windows os.replace raises PermissionError while another process (a virus scanner, an
            # editor, a backup tool) briefly holds the target open, so retry for a short while.
            for attempt in range(10):
                try:
                    os.replace(tmp, path)
                    return
                except PermissionError:
                    if attempt == 9:
                        raise
                    _time.sleep(0.05 * (attempt + 1))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def save(self, force: bool = False) -> None:
        if not self.data_dir:
            return
        os.makedirs(self.data_dir, exist_ok=True)
        if self.dirty or force:
            # Cleared before the state is gathered (a change made meanwhile marks it dirty again) and set
            # back if the write fails, so a failed write is retried on the next save.
            self.dirty = False
            try:
                self._write(self._path("state.json"), {
                    "version": 1,
                    "accounts": [a.to_plain() for a in self.accounts.values()],
                    "orders": [to_plain(o) for o in self.engine.orders.values()],
                    "watchlists": self.watchlists, "alerts": self.alerting.to_plain(),
                    "notifications": self.alerting.notices.to_plain(),
                    "seq": self._peek_sequences(), "lifecycle": self.lifecycle.to_plain()})
            except BaseException:
                self.dirty = True
                raise
        now = self.now()
        if force or now - self._last_market_save >= 60:
            self._last_market_save = now
            snap = self.feed.snapshot()
            if snap:
                self._write(self._path("market.json"), snap)

    @staticmethod
    def _drop_unpriced_samples(a: Account) -> int:
        """Remove equity samples that earlier versions recorded before any price had arrived after a start.

        Such a sample valued every position at zero, so it equals the ledger cash at that moment. It is
        removed only when all of these hold, which a real sample cannot satisfy short of an exact-to-the-
        cent coincidence: it is the first sample after a gap of more than RUNNING_GAP seconds (a start),
        the account held at least one stock position then (a stock always has a positive price), and the
        sample equals the cash at that moment exactly. Returns the number removed.
        """
        hist = a.equity_history
        if not hist:
            return 0
        txns = sorted(a.ledger.transactions, key=lambda t: t.ts)
        keep, cash, held, i, prev_ts, dropped = [], ZERO, {}, 0, float(a.created_at), 0
        for row in hist:
            ts = float(row[0])
            while i < len(txns) and txns[i].ts < ts + 1:
                for acct, v in txns[i].postings:
                    if acct == "cash":
                        cash += v
                    elif acct.startswith("pos:"):
                        held[acct[4:]] = held.get(acct[4:], ZERO) + v
                i += 1
            stock_held = any(v != 0 and parse_option(sym) is None for sym, v in held.items())
            if ts - prev_ts > RUNNING_GAP and stock_held and D(row[1]) == cash:
                dropped += 1
            else:
                keep.append(row)
            prev_ts = ts
        a.equity_history = keep
        return dropped

    def _peek_sequences(self) -> dict:
        """Read the next id of each counter without using it up."""
        out = {}
        for key, owner, attr in (("order", self, "_order_seq"), ("account", self, "_acct_seq"),
                                 ("alert", self, "_alert_seq"), ("fill", self.engine, "_fill_seq"),
                                 ("expiry", self, "_expiry_seq")):
            n = next(getattr(owner, attr))
            setattr(owner, attr, count(n))
            out[key] = n
        return out

    def load(self) -> bool:
        if not self.data_dir:
            return False
        try:
            with open(self._path("market.json"), encoding="utf-8") as f:
                self.feed.restore(json.load(f))
        except (OSError, ValueError):
            pass
        try:
            with open(self._path("state.json"), encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return False
        self.accounts = {a["id"]: Account.from_plain(a) for a in data["accounts"]}
        for a in self.accounts.values():
            self._drop_unpriced_samples(a)
        seq = data["seq"]
        self.engine.restore([from_plain(Order, o) for o in data["orders"]], seq["fill"])
        self.watchlists = data.get("watchlists", {})
        self.alerting.restore(data.get("alerts", []), data.get("notifications"))
        self._order_seq, self._acct_seq = count(seq["order"]), count(seq["account"])
        self._alert_seq, self._expiry_seq = count(seq["alert"]), count(seq["expiry"])
        self.lifecycle.restore(data.get("lifecycle"))
        return True
