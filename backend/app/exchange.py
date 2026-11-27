"""The simulated broker: wires market data, matching, accounts, risk and options."""
import contextlib
import copy
import json
import logging
import os
import re
import tempfile
import threading
import time as _time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from dataclasses import replace as dc_replace
from itertools import count
from typing import Optional

from .alerts import Alerting
from .core import assets
from .core import calendar as cal
from .core import models as m
from .core.clock import Clock
from .core.assets import is_crypto
from .core.models import Fill, Order, Quote, from_plain, to_plain
from .core.money import CENT, ZERO, D, cents, parse as parse_num, px4
from .ledger.account import Account, Lot, is_long_term
from .lifecycle import settings as life_settings
from .lifecycle.manager import LifecycleManager
from .marketdata.feed import MarketDataProvider
from .matching.engine import FillConfig, MatchingEngine
from .matching.multileg import net_price, payoff
from .options import chain as optchain
from .options.symbology import describe, multiplier, parse as parse_option
from .risk import margin
from .core.errors import NotFound

log = logging.getLogger("paperdesk")

OPTION_FEE = Decimal("0.65")  # per contract
STOCK_FEE = ZERO  # per share
MAX_QTY = Decimal(1_000_000)
MAX_LINKED = 5  # orders in one one-cancels-other or one-triggers-other request
MAX_GTD_DAYS = 366
MAX_NUMBER = Decimal("1000000000")  # no price or quantity in a request may be larger than this
MAX_ORDER_LOTS = 100  # tax lots one closing order may name
MAX_ACCOUNTS = 1000     # entity limits: each one keeps the state file and every tick bounded
MAX_OPEN_ORDERS = 1000  # working orders per account
MAX_WATCHLISTS = 50
CLOSED_AT_EXPIRY = "closed at expiry"  # fill kind: an option closed for its intrinsic value, not exercised
RUNNING_GAP = 120  # seconds: a longer gap between two ticks means the simulator was not running (or was paused)
CLOSE_WAIT = 120  # seconds to wait for the provider's daily close before settling at an approximate price
PROVIDER_CLOSE_WAIT = 1800  # seconds to wait for feed.closing_price to become ready before approximating
# Crypto (docs/CRYPTO.md): 24/7, fully paid, a percentage fee on every fill (one rate, no maker/taker split).
CRYPTO_TYPES = (m.MARKET, m.LIMIT, m.STOP, m.STOP_LIMIT)
CRYPTO_TIFS = (m.DAY, m.GTC, m.IOC)


def tidy_qty(q: Decimal) -> str:
    return f"{q.normalize():f}"


def _in_range(*values) -> None:
    """Refuse absurd numbers before any arithmetic is done with them."""
    for v in values:
        if v is not None and abs(v) > MAX_NUMBER:
            raise OrderRejected("invalid number in order: a value is too large")


class OrderRejected(ValueError):
    pass


class _CryptoTicks:
    """What the matching engine asks for: a crypto pair's price increment, or None for anything else."""

    def __init__(self, ex: "Exchange"):
        self.ex = ex

    def get(self, symbol: str, default=None):
        return self.ex.crypto_spec(symbol).price_increment if is_crypto(symbol) else default


def _amt(x: Decimal) -> str:
    return f"{cents(x):,.2f}"


FIELD_LABELS = {"qty": "quantity", "limit_price": "limit price", "stop_price": "stop price",
                "trail_amount": "trail amount", "trail_percent": "trail percent", "take_profit": "take profit",
                "stop_loss": "stop loss", "notional": "dollar amount"}


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
        self.engine.ticks = _CryptoTicks(self)
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
        self._last_utc_day = None
        self._last_session = None
        self._last_equity_sample = 0.0
        self._last_market_save = 0.0
        self.dirty = False
        self.load_result = None  # what the last load() found (app.storage.store.LoadResult)
        self.before_destructive = None  # set by the data service: snapshot before a reset or delete
        self.faults: dict = {}  # account id -> why it cannot be valued right now (see isolated())
        self._booking: frozenset = frozenset()  # order ids whose fills _handle is booking (reserved_lots)
        self.step_faults: dict = {}  # account id -> {step: why it failed}; cleared only when that step next works
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

    def session_for(self, symbol: str, now: Optional[float] = None) -> str:
        """The session an order on this symbol trades in: crypto is always open."""
        return "regular" if is_crypto(symbol) else self.session(now)

    def is_stock(self, symbol: str) -> bool:
        """True for anything the feed lists (stocks, funds and crypto pairs); False for options."""
        return symbol in self.feed.symbols()

    @staticmethod
    def asset_class(symbol: str) -> str:
        return assets.asset_class(symbol)

    def crypto_spec(self, symbol: str) -> assets.CryptoSpec:
        return assets.spec_from_info(symbol, self.feed.symbols().get(symbol))

    def update_settings(self, account_id: str, changes: dict) -> Account:
        """Apply a settings change. A higher crypto fee is refused while open crypto purchases could then
        cost more than the cash (the fill-time cap would cut them; better to say so now)."""
        a = self.account(account_id)
        old = a.settings
        new = life_settings.update(old, changes)
        buys = [o for o in self.engine.orders.values() if o.account_id == a.id and o.active and o.side == m.BUY
                and is_crypto(o.symbol)]
        if buys and life_settings.number(new, "crypto_fee_rate") > life_settings.number(old, "crypto_fee_rate"):
            a.settings = new
            ok = self._worst_case(a, [], crypto_check=True).ok
            a.settings = old
            if not ok:
                names = ", ".join(f"{o.id} ({o.side} {o.remaining.normalize():f} {o.symbol})" for o in buys)
                raise ValueError(f"the crypto fee cannot be raised to {life_settings.number(new, 'crypto_fee_rate').normalize():f}% "
                                 f"while your open crypto purchases would then cost more than your cash: {names}. "
                                 f"Cancel or reduce them first.")
        a.settings = new
        self.dirty = True
        return a

    @staticmethod
    def crypto_fee_rate(account: Optional[Account]) -> Decimal:
        """Percent of the trade value charged on each crypto fill (an account setting)."""
        settings = account.settings if account is not None else life_settings.defaults()
        return life_settings.number(settings, "crypto_fee_rate")

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
        if len(self.accounts) >= MAX_ACCOUNTS:
            raise ValueError(f"there are already {MAX_ACCOUNTS} accounts; delete one first")
        name = (name or "").strip()[:40] or "Paper account"
        acct = Account(f"A{next(self._acct_seq):04d}", name, type, starting_cash, self.now())
        self.accounts[acct.id] = acct
        self.dirty = True
        return acct

    @contextlib.contextmanager
    def backup_taken(self):
        """The caller has just backed the state up itself (outside the lock): do not take another one."""
        hook, self.before_destructive = self.before_destructive, None
        try:
            yield
        finally:
            self.before_destructive = hook

    def reset_account(self, account_id: str, starting_cash=None) -> Account:
        """Start the account again with fresh cash. Its rule settings and tax-lot method are kept; positions,
        orders, history and the trade journal are cleared."""
        old = self.account(account_id)
        # Validate and build the new account first: a bad value must leave the old state untouched.
        if starting_cash is not None:
            cash = _starting_cash(starting_cash)
        else:
            try:  # an impossible saved value (an old state file) needs new cash, not a copy of itself
                cash = _starting_cash(old.starting_cash)
            except (ValueError, ArithmeticError):
                raise ValueError("this account's starting cash is not valid; give a new starting cash to reset it") from None
        new = Account(old.id, old.name, old.type, cash, self.now())
        new.settings = dict(old.settings)  # a reset keeps the account's rule settings
        new.lot_method = old.lot_method  # ... and its tax-lot method
        new.to_plain()  # the account must be saveable, or the next start-up could not load the state file
        if self.before_destructive:  # backup (PR 19): only once the reset is known to be valid
            self.before_destructive("pre-reset")
        for o in self.engine.active_orders():
            if o.account_id == account_id:
                self.engine.cancel(o.id, self.now(), "account reset")
        self.engine.orders = {k: o for k, o in self.engine.orders.items() if o.account_id != account_id}
        self.engine.reindex()
        self._day_start_wanted.pop(old.id, None)
        self.accounts[old.id] = new
        self.faults.pop(old.id, None)
        self.step_faults.pop(old.id, None)
        self.lifecycle.accrual_retry.pop(old.id, None)  # a fresh account owes nothing for old days
        self.dirty = True
        return new

    def delete_account(self, account_id: str) -> None:
        self.account(account_id)
        if len(self.accounts) == 1:
            raise ValueError("cannot delete the only account")
        if self.before_destructive:
            self.before_destructive("pre-delete")
        self.engine.orders = {k: o for k, o in self.engine.orders.items() if o.account_id != account_id}
        self.engine.reindex()
        del self.accounts[account_id]
        self.faults.pop(account_id, None)
        self.step_faults.pop(account_id, None)
        self.lifecycle.accrual_retry.pop(account_id, None)
        self.dirty = True

    def account(self, account_id: str) -> Account:
        try:
            return self.accounts[account_id]
        except KeyError:
            raise NotFound(f"no such account {account_id}")

    def isolated(self, account, fn, what: str, step: bool = False):
        """Run fn(); if it fails, record a fault on this account and return None instead of raising.

        One damaged account (an impossible number from an old state file, say) must not stop the tick,
        the stream or the account list for every other account. The fault is shown in the account list
        so the user can reset or delete that account.

        Two kinds of fault are kept apart (release review, PR 32): valuing the account (`faults`, cleared
        when a valuation works) and a step of the day (`step=True`: the margin check, interest accrual;
        `step_faults`, cleared only when that same step works again). A new step fault is logged with its
        traceback, written to the account's log and sent to the notifications once.
        """
        try:
            out = fn()
        except Exception as e:  # noqa: BLE001 - any failure of one account is contained
            reason = f"{what} failed: {type(e).__name__}"
            book = self.step_faults.setdefault(account.id, {}) if step else self.faults
            key = what if step else account.id
            if book.get(key) != reason:
                log.exception("account %s: %s", account.id, reason)
                if step:
                    try:
                        self.lifecycle._log(account, self.now(), "account_fault",
                                            f"{what[:1].upper()}{what[1:]} is failing for this account "
                                            f"({type(e).__name__}). Reset or delete the account if it keeps failing.")
                    except Exception:  # noqa: BLE001 - reporting must not raise from here
                        pass
            book[key] = reason
            return None
        if step:
            left = self.step_faults.get(account.id)
            if left and left.pop(what, None) is not None and not left:
                del self.step_faults[account.id]
        return out

    def safe_summary(self, account_id: str) -> dict:
        """summary(), or a flagged row for an account that cannot be valued."""
        a = self.account(account_id)
        out = self.isolated(a, lambda: self.summary(account_id), "valuing the account")
        steps = sorted(self.step_faults.get(account_id, {}).values())
        if out is not None:
            self.faults.pop(account_id, None)  # only a valuation fault clears here; step faults stay
            if steps:
                out["faults"] = steps
            return out
        return {"id": a.id, "name": a.name, "type": a.type, "broken": True, "faults": steps,
                "error": "This account cannot be valued (" + self.faults.get(a.id, "unknown error")
                         + "). Reset it with a new starting cash or delete it."}

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
        # Crypto is fully paid: the cash on hand (never a loan), net of the fee on the purchase.
        # (margin: what a new crypto purchase may spend once the open purchases have filled, within the excess)
        cash_bp = out["buying_power"] if a.type == "cash" else max(min(max(worst.excess, ZERO), worst.buy_cash), ZERO)
        out["crypto_buying_power"] = (cash_bp / (1 + self.crypto_fee_rate(a) / 100)).quantize(CENT, "ROUND_FLOOR")
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
                   "multiplier": mult, "kind": assets.asset_class(sym), "asset_class": assets.asset_class(sym), "day_pnl": None,
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

    # ---- all-or-nothing changes ----------------------------------------------------
    @contextlib.contextmanager
    def _atomic(self, account_id: str):
        """Run one change to an account as a unit: if the block raises, nothing of it remains.

        A submit can book several fills (every leg of a multi-leg order, a bracket entry) and a replace
        edits several fields; a fault half way must not leave an order without its fill, a fill without
        its ledger posting or half an edit. Restored on failure: the account's ledger, lots, fills, closed
        trades, lot id counter, lifecycle state and notifications, its open orders (and any order added), the event queue
        and the fill counter. Order ids used by a failed request are not reused.
        """
        a = self.accounts.get(account_id)
        if a is None:
            yield
            return
        orders = self.engine.orders
        n_orders = len(orders)
        saved = [(o, dict(o.__dict__)) for o in self.engine.active_orders() if o.account_id == account_id]
        led = a.ledger
        n_txn, balances = len(led.transactions), dict(led.balances)
        # The constructor, not copy.copy: several times faster, and an account can hold hundreds of lots.
        lots = {s: [Lot(l.qty, l.price, l.ts, l.cost, l.fill_id, l.id) for l in ls] for s, ls in a.lots.items()}
        lot_seq = a.lot_seq  # tax-lot ids (PR 13): a rolled-back fill must not use one up
        n_fills, n_closed, life = len(a.fills), len(a.closed), a.life.copy()
        # `dirty` is not put back: the save thread sets it when a write fails, and a rolled-back request
        # costs one extra save at most.
        n_events = len(self.events)
        notices = self.alerting.notices
        notes, note_seq = list(notices.logs.get(account_id, ())), notices.seq
        fill_seq = next(self.engine._fill_seq)
        self.engine._fill_seq = count(fill_seq)
        try:
            yield
        except BaseException:
            if self.engine.orders is orders:
                while len(orders) > n_orders:
                    orders.popitem()
            for o, fields in saved:
                o.__dict__.clear()
                o.__dict__.update(fields)
            self.engine.reindex()  # orders open again, in their first order
            del led.transactions[n_txn:]
            led.balances = balances
            a.lots, a.lot_seq = lots, lot_seq
            del a.fills[n_fills:], a.closed[n_closed:], self.events[n_events:]
            a.life = life
            notices.logs[account_id], notices.seq = notes, note_seq
            if not notes:
                del notices.logs[account_id]
            self.engine._fill_seq = count(fill_seq)
            raise

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
            # Never fetched here (this runs under the exchange lock): the order routes warm the listing before
            # they take the lock (server.warm_option_listings); if it is still unknown, say so.
            listing = self.feed.option_listing(contract.underlying, fetch=False)
            if listing is None and getattr(self.feed, "lists_options", False) and not held:
                raise OrderRejected(f"the list of {self.feed.name.capitalize()} option contracts for "
                                    f"{contract.underlying} is still loading; try again in a few seconds")
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
        if is_crypto(symbol) and self.feed.symbols()[symbol].get("tradable") is False:
            raise OrderRejected(f"{symbol} is not tradable right now (the data provider lists it as not tradable)")
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
            if req.get("lots") is not None:
                raise OrderRejected("tax lots can be chosen on single closing orders only, not on multi-leg orders")
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
            raise OrderRejected("several exit orders for one entry must cancel each other: choose "
                                "\"Cancel the others when one fills\" (triggers_oco in the API)")
        return [entry] + children

    def _validate_one(self, account: Account, req: dict, linked: Optional[str] = None) -> list:
        """One order, plus its bracket children when take_profit or stop_loss is given."""
        if linked and any(req.get(k) is not None for k in ("legs", "oco", "triggers")):
            raise OrderRejected("a linked order cannot itself be multi-leg or linked")
        if linked and req.get("lots") is not None:
            raise OrderRejected(f"tax lots can be chosen on single closing orders only, not on {linked} orders")
        try:
            symbol = str(req.get("symbol", "")).upper().strip()
            side, typ = req.get("side"), req.get("type", m.MARKET)
            tif = req.get("tif", m.DAY)
            notional = _field(req, "notional")  # crypto only: a dollar amount instead of a quantity
            qty = (parse_num(req.get("qty"), FIELD_LABELS["qty"]) if req.get("qty") not in (None, "") or notional is None
                   else None)
            limit, stop = _field(req, "limit_price"), _field(req, "stop_price")
            trail_amt, trail_pct = _field(req, "trail_amount"), _field(req, "trail_percent")
            take_profit, stop_loss = _field(req, "take_profit"), _field(req, "stop_loss")
        except (ValueError, ArithmeticError) as e:
            raise OrderRejected(f"invalid number in order: {e}")
        _in_range(qty, notional, limit, stop, trail_amt, trail_pct, take_profit, stop_loss)
        contract = self._check_symbol(account, symbol)
        if side not in (m.BUY, m.SELL):
            raise OrderRejected("side must be buy or sell")
        if typ not in m.ORDER_TYPES:
            raise OrderRejected(f"unknown order type {typ}")
        if tif not in m.TIFS:
            raise OrderRejected(f"unknown time in force {tif}")
        crypto = is_crypto(symbol)
        if crypto:
            qty = self._crypto_rules(account, req, symbol, side, typ, tif, qty, notional, limit, stop, linked,
                                     take_profit is not None or stop_loss is not None)
        elif notional is not None:
            raise OrderRejected("a dollar amount can only be given for crypto; enter a quantity")
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
        if not crypto and (qty <= 0 or qty > MAX_QTY):
            raise OrderRejected("quantity must be positive and at most 1,000,000")
        if contract and qty != qty.to_integral_value():
            raise OrderRejected("options trade in whole contracts")
        if not crypto and qty != qty.quantize(Decimal("0.0001")):
            raise OrderRejected("quantity supports at most 4 decimal places")
        if not crypto and qty != qty.to_integral_value() and (typ not in (m.MARKET, m.LIMIT) or tif != m.DAY):
            raise OrderRejected("fractional shares are limited to market and limit day orders")
        for label, v in (("limit price", limit), ("stop price", stop), ("trail amount", trail_amt),
                         ("trail percent", trail_pct), ("take profit", take_profit), ("stop loss", stop_loss)):
            if v is not None and v <= 0:
                raise OrderRejected(f"{label} must be positive")
        for label, v in (("limit price", limit), ("stop price", stop), ("take profit", take_profit), ("stop loss", stop_loss)):
            if not crypto and v is not None and v != v.quantize(CENT if v >= 1 else Decimal("0.0001")):
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
        if req.get("lots") is not None:
            if take_profit is not None or stop_loss is not None:
                raise OrderRejected("tax lots can be chosen on single closing orders only, not on bracket orders")
            entry.lots = self.validate_lots(account, symbol, side, qty, req["lots"])
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

    # ---- tax lots ----------------------------------------------------------
    def reserved_lots(self, account_id: str, exclude: Optional[str] = None) -> dict:
        """Lot id -> quantity still to be closed by the account's open orders that named it."""
        out: dict = {}
        # Open orders, plus the orders whose fills are being booked right now (see _handle): never the whole
        # order history, which grows with every order and made each fill slower.
        orders = self.engine.orders
        candidates = list(self.engine.active_orders()) + [orders[i] for i in sorted(self._booking) if i in orders]
        for o in {c.id: c for c in candidates}.values():
            if o.account_id != account_id or not o.lots or o.id == exclude:
                continue
            wants = [(e["lot_id"], D(e["qty"]) - D(e.get("filled", "0"))) for e in o.lots]
            wants = [(lid, w) for lid, w in wants if w > 0]
            if not wants:
                continue
            if not o.active:
                # The engine marks an order filled before its fills are booked: within one batch of
                # events its lots stay reserved for the fills still to come.
                if o.status != m.FILLED:
                    continue
                acct = self.accounts.get(account_id)
                pending = o.filled_qty - sum((f.qty for f in (acct.fills if acct else ()) if f.order_id == o.id), ZERO)
                if pending <= 0:
                    continue
                capped = []
                for lid, w in wants:
                    capped.append((lid, min(w, pending)))
                    pending -= min(w, pending)
                wants = capped
            for lid, w in wants:
                if w > 0:
                    out[lid] = out.get(lid, ZERO) + w
        return out

    def _cancel_broken_lot_orders(self, account: Account, now: float) -> None:
        """Cancel open orders whose named lots no longer hold what they still need to close.

        Lots named by an order are skipped by other trades while unnamed lots remain, but a larger
        sale, an assignment or an exercise can still close them. The order is then canceled rather
        than left to close lots nobody chose (or to open a short position).
        """
        mine = [o for o in self.engine.active_orders() if o.account_id == account.id and o.lots]
        if not mine:
            return  # nothing names a lot: no need to look at every lot on every fill
        held = {l.id: abs(l.qty) for ls in account.lots.values() for l in ls}
        # Several orders can each name part of one lot, so what they still need is counted together:
        # the oldest orders keep their claim and the newest ones that no longer fit are canceled.
        claimed: dict = {}
        for o in sorted(mine, key=lambda o: (o.created_at, o.id)):
            need: dict = {}
            for e in o.lots:
                need[e["lot_id"]] = need.get(e["lot_id"], ZERO) + max(D(e["qty"]) - D(e.get("filled", "0")), ZERO)
            short = [lid for lid, q in need.items() if q > 0 and claimed.get(lid, ZERO) + q > held.get(lid, ZERO)]
            if short:
                self._handle(self.engine.cancel(o.id, now, f"Canceled: lot {', '.join(short)} named by this order "
                                                           f"was closed by another trade"))
            else:
                for lid, q in need.items():
                    claimed[lid] = claimed.get(lid, ZERO) + q

    def validate_lots(self, account: Account, symbol: str, side: str, qty: Decimal, raw) -> list:
        """Check a closing order's chosen lots. Returns the plan stored on the order."""
        if not isinstance(raw, list) or not all(isinstance(e, dict) for e in raw):
            raise OrderRejected("lots must be a list like [{\"lot_id\": \"L1\", \"qty\": 5}]")
        if not raw:
            raise OrderRejected("lots is empty: name at least one lot, or leave lots out to use the account's lot method")
        if len(raw) > MAX_ORDER_LOTS:
            raise OrderRejected(f"an order can name at most {MAX_ORDER_LOTS} lots")
        held = {l.id: l for l in account.lots.get(symbol, ())}
        reserved = self.reserved_lots(account.id)
        plan, seen, total = [], set(), ZERO
        for e in raw:
            lot_id = str(e.get("lot_id", "")).strip()
            try:
                q = parse_num(e.get("qty"))
            except (ValueError, ArithmeticError):
                raise OrderRejected(f"lot {lot_id or '(blank)'}: quantity is not a number")
            if lot_id in seen:
                raise OrderRejected(f"lot {lot_id} is named twice")
            seen.add(lot_id)
            lot = held.get(lot_id)
            if lot is None:
                other, _ = account.find_lot(lot_id)
                raise OrderRejected(f"lot {lot_id or '(blank)'} is not an open lot of {symbol}" +
                                    (f" (it belongs to {other})" if other else ""))
            if (lot.qty > 0) == (side == m.BUY):
                raise OrderRejected(f"lot {lot_id} is a {'long' if lot.qty > 0 else 'short'} lot; "
                                    f"{'sell' if lot.qty > 0 else 'buy'} to close it")
            if is_crypto(symbol):  # crypto lots (PR 14) are named in the coin's own quantity step
                step = self.crypto_spec(symbol).qty_increment
                if q <= 0 or q > MAX_QTY or not assets.on_step(q, step):
                    raise OrderRejected(f"lot {lot_id}: quantity must be positive, at most 1,000,000, and a multiple "
                                        f"of {step.normalize():f}")
            elif q <= 0 or q > MAX_QTY or q != q.quantize(Decimal("0.0001")):
                raise OrderRejected(f"lot {lot_id}: quantity must be positive, at most 1,000,000, with at most "
                                    f"4 decimal places")
            free = abs(lot.qty) - reserved.get(lot_id, ZERO)
            if q > free:
                taken = reserved.get(lot_id, ZERO)
                raise OrderRejected(f"lot {lot_id} has {tidy_qty(abs(lot.qty))} open" +
                                    (f", of which {tidy_qty(taken)} is already named by another open order" if taken else "") +
                                    f"; {tidy_qty(q)} is more than the {tidy_qty(max(free, ZERO))} available")
            total += q
            plan.append({"lot_id": lot_id, "qty": str(q), "filled": "0"})
        if total != qty:
            raise OrderRejected(f"the chosen lots add up to {tidy_qty(total)}, but the order is for {tidy_qty(qty)}")
        return plan

    def lot_preview(self, account: Account, o: Order, price: Decimal) -> list:
        """The lots a closing order would close if it filled now at `price` (estimates)."""
        if o.legs:
            return []
        plan = [dict(e) for e in o.lots] if o.lots else None
        picks = account.select_lots(o.symbol, o.side, o.qty, price, self.now(), plan,
                                    self.reserved_lots(account.id, exclude=o.id))
        lots, mult, out = account.lots.get(o.symbol, []), multiplier(o.symbol), []
        for idx, take, method, fallback in picks:
            lot = lots[idx]
            cost = lot.cost if take == abs(lot.qty) else cents(lot.cost * take / abs(lot.qty))
            value = cents(take * price * mult)
            gain = value - cost if lot.qty > 0 else cost - value
            out.append({"lot_id": lot.id, "qty": take, "opened_at": lot.ts, "open_price": lot.price,
                        "cost_basis": cost, "estimated_proceeds": value, "estimated_gain": gain,
                        "term": "long" if is_long_term(lot, self.now()) else "short", "method": method})
        return out

    def _crypto_rules(self, account: Account, req: dict, symbol: str, side: str, typ: str, tif: str,
                      qty: Optional[Decimal], notional: Optional[Decimal], limit, stop, linked, bracket: bool) -> Decimal:
        """Crypto order rules. Returns the quantity (worked out from a dollar amount when one is given)."""
        if linked or bracket:
            raise OrderRejected("crypto orders cannot be bracket, one-cancels-other or one-triggers-other orders")
        if typ in m.AUCTION_TYPES:
            raise OrderRejected("on-open and on-close orders do not apply to crypto, which trades 24 hours a day")
        if typ not in CRYPTO_TYPES:
            raise OrderRejected("crypto orders can be market, limit, stop or stop-limit orders")
        if typ == m.STOP and side == m.BUY:
            raise OrderRejected("a crypto stop order to buy needs a limit price (use stop-limit): a market fill above "
                                "the stop could cost more than your cash")
        if tif not in CRYPTO_TIFS:
            raise OrderRejected("crypto orders can be day, good-till-canceled or immediate-or-cancel orders")
        if req.get("extended_hours"):
            raise OrderRejected("extended hours do not apply to crypto, which trades 24 hours a day")
        spec = self.crypto_spec(symbol)
        for label, v in (("limit price", limit), ("stop price", stop)):
            if v is not None and v > 0 and not assets.on_step(v, spec.price_increment):
                raise OrderRejected(f"the {label} for {symbol} must be a multiple of {spec.price_increment.normalize():f}")
        for label, v in (("limit price", limit), ("stop price", stop)):
            if v is not None and v <= 0:
                raise OrderRejected(f"the {label} must be positive")
        if qty is not None and notional is not None:
            raise OrderRejected("give a quantity or a dollar amount, not both")
        if notional is not None:
            if notional <= 0:
                raise OrderRejected("the dollar amount must be positive")
            if notional != notional.quantize(CENT):
                raise OrderRejected("the dollar amount has more than 2 decimal places")
            if typ not in (m.MARKET, m.LIMIT):
                raise OrderRejected("a dollar amount can be given for market and limit orders; enter a quantity for stop orders")
            q = self.quote(symbol)
            ref = limit if typ == m.LIMIT and limit is not None else (q.ask if side == m.BUY else q.bid) if q else None
            if ref is None or ref <= 0:
                raise OrderRejected(f"no quote available for {symbol}, so the dollar amount cannot be converted")
            step = spec.qty_increment
            qty = assets.floor_to(notional / ref, step)
            if typ == m.MARKET and side == m.BUY:
                # A market buy walks the book, so size it on the walked price: the spend never exceeds the amount.
                while qty > 0 and cents(qty * self.engine._walk(q, side, qty)) > notional:
                    smaller = assets.floor_to(notional / self.engine._walk(q, side, qty), step)
                    qty = smaller if smaller < qty else qty - step
        if qty is None or qty <= 0:
            raise OrderRejected("quantity must be positive")
        whole = side == m.SELL and qty == account.position(symbol)  # selling all of it is always allowed
        if not whole and not assets.on_step(qty, spec.qty_increment):
            raise OrderRejected(f"the quantity of {symbol} must be a multiple of {spec.qty_increment.normalize():f}")
        if qty < spec.min_order_size and not whole:
            extra = f" (that amount buys {qty.normalize():f})" if notional is not None else ""
            raise OrderRejected(f"the minimum order for {symbol} is {spec.min_order_size.normalize():f}{extra}")
        if side == m.SELL:
            self._no_crypto_short(account, symbol, qty)
        return qty

    def _no_crypto_short(self, account: Account, symbol: str, qty: Decimal, ignore: Optional[str] = None) -> None:
        held = account.position(symbol)
        selling = sum((o.remaining for o in self.engine.orders.values() if o.account_id == account.id and o.active
                       and o.symbol == symbol and o.side == m.SELL and o.id != ignore), ZERO)
        if qty + selling > held:
            have = f"you hold {held.normalize():f}" if held > 0 else "you hold none"
            if selling > 0:
                have += f" and open orders already sell {selling.normalize():f}"
            raise OrderRejected(f"crypto cannot be sold short: {have}")

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

    def _fee(self, symbol: str, qty: Decimal, price: Optional[Decimal] = None, account: Optional[Account] = None) -> Decimal:
        """Commission on a fill. Crypto pays a percentage of the trade value, rounded to the cent."""
        if is_crypto(symbol):
            if price is None:
                raise ValueError("a crypto fee needs the price")
            return cents(qty * price * self.crypto_fee_rate(account) / 100)
        return cents(qty * (OPTION_FEE if multiplier(symbol) == 100 else STOCK_FEE))

    class _Worst:
        def __init__(self, ok, reason, excess, requirement, cash_after):
            self.ok, self.reason, self.excess, self.requirement, self.cash_after = ok, reason, excess, requirement, cash_after

    def _worst_case(self, account: Account, new_orders: list, crypto_check: bool = False) -> "_Worst":
        """Check the account under every scenario: all open buys fill, all open sells fill, neither.

        One-cancels-other groups count once per symbol. Held bracket and exit children are
        ignored because they only ever reduce the position their entry order opened. A held
        triggered order (own quantity) is counted together with the entry that must fill
        before it. A multi-leg order is all-or-none, so it is applied as one unit: each
        scenario is checked with no multi-leg order filled, with all of them filled and
        with each one filled alone.

        Crypto is never bought with borrowed money: when the request being checked is a crypto
        purchase (or `crypto_check` is set), a margin account must also keep cash at or above zero
        once all its open purchases have filled. Other requests are not held to that, so a resting
        crypto purchase does not change what a stock order may do (the fill-time cap in _handle
        keeps crypto fills within cash whatever fills first). `buy_cash` on the result is the cash
        left when every open purchase fills: what a new crypto purchase may spend.
        Cash accounts use `_cash_worst` instead, a reservation that holds for every subset of fills.
        """
        if account.type == "cash":
            return self._cash_worst(account, new_orders)
        crypto_check = crypto_check or any(is_crypto(o.symbol) and o.side == m.BUY and not o.legs for o in new_orders)
        live = [o for o in self.engine.orders.values() if o.account_id == account.id
                and (o.status in (m.WORKING, m.PARTIAL) or (o.status == m.HELD and o.role == m.TRIGGERED))]
        live += [o for o in new_orders if o.id not in self.engine.orders]
        singles = [o for o in live if not o.legs]
        combos = [o for o in live if o.legs]
        by_id = {o.id: o for o in singles}
        combo_sets = [[]] + ([combos] if combos else []) + ([[c] for c in combos] if len(combos) > 1 else [])
        base = account.positions()
        result, buy_cash = None, None
        for side in (None, m.BUY, m.SELL):
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
                    strict = o.id not in self.engine.orders  # a new order; working ones never fail here
                    crypto = is_crypto(o.symbol)
                    px = self._estimate_price(o, strict) if crypto else None
                    cash -= self._fee(o.symbol, qty, px, account)
                    cash -= sign * cents(qty * (px or self._estimate_price(o, strict)) * multiplier(o.symbol))

                for o in singles if side is not None else ():
                    if o.side != side or o.id in applied:
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
                if side == m.BUY and not combo_set:
                    buy_cash = cash if buy_cash is None else min(buy_cash, cash)
                if crypto_check and side == m.BUY and cash < 0 and account.type == "margin":
                    w = Exchange._Worst(False, f"crypto is not bought on margin: this order and your open purchases "
                                               f"need {_amt(account.cash - cash)} but cash is {_amt(account.cash)}",
                                        ev.excess, ev.requirement, cash)
                if result is None or (result.ok and not w.ok) or (w.ok == result.ok and w.excess < result.excess):
                    result = w
        result.buy_cash = account.cash if buy_cash is None else buy_cash
        return result

    def _cash_worst(self, account: Account, new_orders: list, exclude=frozenset()) -> "_Worst":
        """The pre-trade check of a cash account: a reservation that holds whichever open orders fill.

        A cash account cannot borrow, so nothing is counted on that is not certain. Every open order
        is counted as if it fills completely: a buy reserves its full cost and fees, a sale counts
        as gone the shares or contracts it sells. Nothing an unfilled order would bring in is
        available to any other order: not the money from a sale, not the premium of a write, not
        the shares a buy would add, not the cash a buy-back would free. An order's own proceeds or
        premium, and the cash its own buy-back frees, only offset what that same order needs (its
        fees, its cost, or the cash that secures the put it writes), never more. Because each
        order then adds a reservation of zero or more, every subset of fills is covered, whatever
        fills and in whichever order.

        Orders that can only fill together are one unit, valued at their worst: a one-cancels-other
        group can fill at most the size of its largest order, at the price of its most expensive
        member; an order with held children (bracket or one-triggers-other) is counted with and
        without its children, and its own proceeds may pay for its children but for nothing else.
        A multi-leg order is all-or-none. An open order that cannot be priced right now cannot fill
        either, so only what it sells is counted; it is priced again before it can fill. Orders in
        `exclude` (and their held children) are left out.
        """
        new_ids = {o.id for o in new_orders}
        live = [o for o in self.engine.orders.values() if o.account_id == account.id and o.active
                and o.id not in exclude and o.parent_id not in exclude]
        live += [o for o in new_orders if o.id not in self.engine.orders]
        live.sort(key=lambda o: (o.id in new_ids, o.created_at, o.id))
        base = account.positions()
        close_left = {s: max(q, ZERO) for s, q in base.items()}  # units a sale closes rather than opens
        release_left = {s: max(-q, ZERO) for s, q in base.items()}  # short units a buy-back closes

        def price(o):
            try:
                px = self._estimate_price(o)
            except OrderRejected:
                if o.id in new_ids:
                    raise
                return None  # cannot fill until it has a price; checked again before then
            if o.type in m.CLOSE_TYPES and o.limit_price is None and (aq := self._auction_price(o.symbol)) is not None \
                    and aq.ask > 0 and aq.bid > 0:
                # It fills at the last regular-session price, which need not be the current one.
                alt = self.engine._walk(aq, o.side, o.remaining)
                px = max(px, alt) if o.side == m.BUY else min(px, alt)
            return px

        def put_cash(symbol):
            c = parse_option(symbol)
            return c.strike * 100 if c is not None and c.kind == "P" else None

        freed_calls: dict = {}  # call symbol -> contracts the unit being valued buys back

        def one(symbol, side, qty, px, credit_write=True):
            """(net cash, requirement this order adds, requirement it frees) of trading qty."""
            mult, k = multiplier(symbol), put_cash(symbol)
            # A crypto fee (PR 14) is a share of the trade value; an unpriced order cannot fill until priced.
            fee = (self._fee(symbol, qty, px, account) if px is not None else ZERO) if is_crypto(symbol) \
                else self._fee(symbol, qty)
            if side == m.BUY:
                freed = min(qty, release_left.get(symbol, ZERO))
                release_left[symbol] = release_left.get(symbol, ZERO) - freed
                if freed > 0 and k is None and parse_option(symbol) is not None:
                    freed_calls[symbol] = freed_calls.get(symbol, ZERO) + freed
                cost = cents(qty * px * mult) if px is not None else ZERO
                return -cost - fee, ZERO, cents(freed * k) if k is not None else ZERO
            closing = min(qty, close_left.get(symbol, ZERO))
            close_left[symbol] = close_left.get(symbol, ZERO) - closing
            proceeds = cents(qty * px * mult) if px is not None else ZERO
            adds = cents((qty - closing) * k) if k is not None and credit_write else ZERO
            return proceeds - fee, adds, ZERO

        def value(net, adds, freed):
            return min(adds, net + freed)  # what it may take from the account; its own money offsets only itself

        groups: dict = {}  # top-level units: one-cancels-other groups, single orders, multi-leg orders
        children: dict = {}  # parent id -> {group key: [held children]}
        by_id = {o.id: o for o in live}
        for o in live:
            parent = by_id.get(o.parent_id) if o.parent_id is not None else None
            if parent is not None:
                if o.role != m.TRIGGERED:  # an exit is re-sized to what its entry fills when it is released
                    o = dc_replace(o, qty=parent.qty, filled_qty=ZERO)
                children.setdefault(o.parent_id, {}).setdefault(o.oco_group or o.id, []).append(o)
            else:
                groups.setdefault(o.oco_group or o.id, []).append(o)

        def group_effect(members):
            """((net cash, adds, frees), {symbol: units sold}) of a one-cancels-other group, or of one order."""
            cap = max(o.remaining for o in members)
            sold: dict = {}
            for o in members:
                if o.side == m.SELL:
                    sold[o.symbol] = min(cap, sold.get(o.symbol, ZERO) + o.remaining)
            if len(members) == 1:
                o = members[0]
                return one(o.symbol, o.side, o.remaining, price(o)), sold
            # Fills on one take the same quantity off the others, so the group fills at most `cap`. Per
            # symbol, count that much of its most expensive member there. A write in a group does not
            # offset itself.
            per_symbol: dict = {}
            for o in members:
                n = min(cap, sum((x.remaining for x in members if x.symbol == o.symbol), ZERO))
                net, adds, freed = one(o.symbol, o.side, n, price(o), credit_write=False)
                if o.symbol not in per_symbol or net + freed < sum(per_symbol[o.symbol][::2]):
                    per_symbol[o.symbol] = (net, adds, freed)
            if len(per_symbol) == 1:
                return next(iter(per_symbol.values())), sold
            # Different symbols are added up, but only what each may take: what one member brings in (a
            # sale's proceeds) is never there when another fills, because filling one cancels the rest.
            # (Adding the nets let a sale at 8.00 pay for a stop buy that filled at once: cash -163.30.)
            return (sum((value(*v) for v in per_symbol.values()), ZERO), ZERO, ZERO), sold

        def own_cover(deltas, freed=None):
            """A unit that fills as a whole may cover the calls it writes with the shares it buys itself,
            and sell the shares behind the short calls it buys back itself (`freed`)."""
            out = dict(deltas)
            for symbol, n in sorted((freed or {}).items()):
                u = parse_option(symbol).underlying
                if out.get(u, ZERO) < 0:
                    out[u] += min(-out[u], n * 100)
            for symbol in sorted(out):
                c = parse_option(symbol)
                if c is not None and c.kind == "C" and out[symbol] < 0 and out.get(c.underlying, ZERO) >= 100:
                    covered = min(-out[symbol], out[c.underlying] // 100)
                    out[symbol] += covered
                    out[c.underlying] -= covered * 100
            return out

        cash = account.cash
        pos = dict(base)
        for key, members in groups.items():
            if len(members) == 1 and members[0].legs:
                o = members[0]
                net, adds, freed = ZERO, ZERO, ZERO
                try:
                    net = -self._multileg_cost(o)
                except OrderRejected:
                    if o.id in new_ids:
                        raise
                net -= self._multileg_fee(o)
                freed_calls.clear()
                for l in o.legs:
                    _, a_, f_ = one(l["symbol"], l["side"], o.qty * l["ratio"], None)
                    adds, freed = adds + a_, freed + f_
                for symbol, d in own_cover(self._leg_deltas(o), freed_calls).items():
                    pos[symbol] = pos.get(symbol, ZERO) + min(d, ZERO)
                cash += value(net, adds, freed)
                continue
            freed_calls.clear()
            (net, adds, freed), sold = group_effect(members)
            kids = [children.get(x.id, {}) for x in members]
            if not any(kids):
                cash += value(net, adds, freed)
                for symbol, n in sold.items():
                    pos[symbol] = pos.get(symbol, ZERO) - n
                continue
            # An order with held children (never part of a standalone one-cancels-other group): its children
            # can only fill once it has, so its own proceeds and shares may serve them, and nothing else.
            parent = members[0]
            parent_freed = dict(freed_calls) if len(members) == 1 else {}  # what the parent alone buys back
            deltas = {parent.symbol: parent.remaining if parent.side == m.BUY else -parent.remaining}
            k_net, k_freed = ZERO, ZERO
            for kid_group in kids[0].values():
                (c_net, _, c_freed), c_sold = group_effect(kid_group)
                k_net, k_freed = k_net + c_net, k_freed + c_freed
                for symbol, n in c_sold.items():
                    deltas[symbol] = deltas.get(symbol, ZERO) - n
            cash += min(value(net, adds, freed), value(net + k_net, adds, freed + k_freed))
            for symbol, d in own_cover(deltas, parent_freed).items():
                pos[symbol] = pos.get(symbol, ZERO) + min(d, ZERO)
        pos = {s: q for s, q in pos.items() if q != 0}
        ev = margin.evaluate("cash", cash, pos, self._marks(pos))
        return Exchange._Worst(ev.ok, ev.reason, ev.excess, ev.requirement, cash)

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

    def _not_riskier(self, account: Account, deltas: dict, cash_delta: Decimal, exclude: Optional[str] = None) -> bool:
        """True when applying these position changes (and paying cash_delta) adds no risk.

        Closing a position is normally always allowed, but not when it is what covers
        another one: selling the long leg of a spread, or the shares behind a covered call,
        leaves an uncovered short option and has to pass the full check. Everything that
        other open orders are already selling counts as sold, so two orders cannot each
        lean on the cover the other one removes.
        """
        before = account.positions()
        for x in self.engine.orders.values():
            if x.account_id == account.id and x.status in (m.WORKING, m.PARTIAL) and x.id != exclude:
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

    def _closes(self, account: Account, symbol: str, side: str, qty: Decimal, exclude: Optional[str] = None) -> bool:
        """True when trading qty of symbol only reduces the position held, open orders included."""
        pos = account.position(symbol)
        if pos == 0 or (pos > 0) == (side == m.BUY):
            return False
        same_side = sum((x.remaining for x in self.engine.orders.values()
                         if x.account_id == account.id and x.symbol == symbol and x.side == side
                         and x.status in (m.WORKING, m.PARTIAL) and x.id != exclude), ZERO)
        return same_side + qty <= abs(pos)

    def _reduces_risk(self, account: Account, o: Order, exclude: Optional[str] = None) -> bool:
        """True when the order only closes part of an existing position and uncovers nothing.

        `exclude` names an open order to leave out of the other open orders (the order itself, when
        it is being changed); `o.qty` is then what is still to fill."""
        if o.legs:
            return (all(self._closes(account, l["symbol"], l["side"], o.qty * l["ratio"]) for l in o.legs)
                    and self._not_riskier(account, self._leg_deltas(o), -self._multileg_cost(o) - self._multileg_fee(o)))
        if not self._closes(account, o.symbol, o.side, o.qty, exclude):
            return False
        sign = 1 if o.side == m.BUY else -1
        px = self._estimate_price(o)
        paid = sign * cents(o.qty * px * multiplier(o.symbol)) + self._fee(o.symbol, o.qty, px, account)
        return self._not_riskier(account, {o.symbol: sign * o.qty}, -paid, exclude)

    def _risk_check(self, account: Account, orders: list) -> "_Worst":
        """Pre-trade check for one request (a single order, a linked set or a multi-leg order).

        Returns the worst case; `closing` is set when the request only reduces risk and is
        therefore allowed even if the account is under its requirement.
        """
        entry = orders[0]
        children = [o for o in orders[1:] if o.parent_id == entry.id]
        if account.type == "cash":
            # Only a sale that closes part of a holding and uncovers nothing skips the check. A buy,
            # even one that closes a short option, must be paid for (the cash it frees counts for it),
            # and the children of a linked order are counted with it.
            counted = orders
            standalone = len(orders) == 1 or not children  # one order, or a one-cancels-other group
            closing = standalone and all(
                not (o.side == m.BUY if not o.legs else any(l["side"] == m.BUY for l in o.legs))
                and self._reduces_risk(account, o) for o in orders)
        elif children or len(orders) == 1:  # single, bracket or one-triggers-other
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
        fee = self._fee(entry.symbol, entry.qty, price, a)
        crypto = is_crypto(entry.symbol)
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
            which, word = ("open", "opening") if entry.type in m.OPEN_TYPES else ("close", "closing")
            warnings.append(f"This order waits for the next market {which} and trades once, at the {word} price."
                            + (" In this session mode the market never closes, so that is the next UTC midnight."
                               if self.session_mode == "always" else ""))
            if entry.limit_price is not None:
                warnings.append(f"If the {word} price is worse than the limit, the order expires unfilled.")
        elif self.session_for(entry.symbol) != "regular" and not entry.extended_hours:
            warnings.append("The market is closed; the order will wait for the regular session.")
        if crypto:
            rate = self.crypto_fee_rate(a).normalize()
            warnings.append(f"Crypto is fully paid (no margin, no short selling). A fee of {rate:f}% of the trade value "
                            f"is charged on each fill.")
            if entry.tif == m.DAY:
                warnings.append("Crypto trades 24 hours a day, 7 days a week; a day order expires at midnight UTC.")
        if entry.good_till:
            warnings.append(f"This order expires at the close on {entry.good_till} if it has not filled.")
        if len(orders) > 1 and orders[1].parent_id != entry.id:
            warnings.append("One-cancels-other: a fill on one of these orders reduces or cancels the others.")
        if any(o.role in (m.EXIT, m.TRIGGERED) for o in orders[1:]):
            warnings.append("The triggered orders stay on hold until this order fills.")
        return {"ok": ok, "reason": None if ok else rule or self._shortfall(a, worst, entry), "symbol": entry.symbol,
                "description": describe(entry.symbol), "side": entry.side, "qty": entry.qty,
                "asset_class": assets.asset_class(entry.symbol),
                "fee_rate": self.crypto_fee_rate(a) if crypto else None,
                "estimated_price": price, "estimated_notional": notional, "estimated_fees": fee,
                "estimated_total": notional + fee if entry.side == m.BUY else notional - fee,
                "position_before": pos, "position_after": after_pos, "buying_power_before": before["buying_power"],
                "buying_power_after": bp_after if not closing else None, "closing": closing, "warnings": warnings,
                "lots": self.lot_preview(a, entry, price),
                "linked_orders": [{"symbol": o.symbol, "description": describe(o.symbol), "side": o.side, "qty": o.qty,
                                   "type": o.type, "role": o.role} for o in orders[1:]]}

    def submit(self, account_id: str, req: dict) -> list:
        # Validation and the risk and rule checks change nothing but the order id counter, so the rollback
        # snapshot is only taken once they have passed (a rejected order costs no copy).
        a = self.account(account_id)
        if not isinstance(req, dict):
            raise OrderRejected("an order must be a JSON object")
        if req.get("client_id"):
            for o in self.engine.orders.values():
                if o.account_id == account_id and o.client_id == str(req["client_id"])[:64]:
                    return [o] + [c for c in self.engine.orders.values()
                                  if c.id != o.id and (c.parent_id == o.id or c.oco_group == o.id)]
        if sum(1 for o in self.engine.active_orders() if o.account_id == account_id) >= MAX_OPEN_ORDERS:
            raise OrderRejected(f"this account already has {MAX_OPEN_ORDERS} working orders; cancel some first")
        orders = self._validate(a, req)
        entry = orders[0]
        worst = self._risk_check(a, orders)
        if not (worst.closing or worst.ok):
            raise OrderRejected(self._shortfall(a, worst, entry))
        rule = self._rule_check(a, orders, worst)  # pattern day trader and settled-cash rules
        if rule:
            raise OrderRejected(rule)
        with self._atomic(account_id):
            self._place(orders)
        return orders

    def _place(self, orders: list) -> None:
        entry = orders[0]
        now = self.now()
        sess, q = self.session_for(entry.symbol, now), self._tradable(self.quote(entry.symbol))  # a stale quote fills nothing
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

    def cancel(self, account_id: str, order_id: str) -> Order:
        o = self.engine.orders.get(order_id)
        if o is None or o.account_id != account_id:
            raise NotFound("no such order")
        if not o.active:
            raise ValueError("order is no longer open")
        with self._atomic(account_id):
            self._handle(self.engine.cancel(order_id, self.now(), "canceled by user"))
        return o

    def cancel_all(self, account_id: str) -> int:
        n = 0
        with self._atomic(account_id):
            for o in list(self.engine.active_orders()):
                if o.account_id == account_id and o.active:
                    self._handle(self.engine.cancel(o.id, self.now(), "canceled by user"))
                    n += 1
        return n

    def replace(self, account_id: str, order_id: str, req: dict) -> Order:
        with self._atomic(account_id):
            return self._replace(account_id, order_id, req)

    def _replace(self, account_id: str, order_id: str, req: dict) -> Order:
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
        if is_crypto(o.symbol):
            spec = self.crypto_spec(o.symbol)
            rest = None if qty is None else qty - o.filled_qty  # what the changed order still has to fill
            whole = qty is not None and o.side == m.SELL and rest == a.position(o.symbol)
            if rest is not None and rest > 0 and not whole and (not assets.on_step(rest, spec.qty_increment)
                                                                or rest < spec.min_order_size):
                raise OrderRejected(f"what is left to fill of {o.symbol} must be a multiple of "
                                    f"{spec.qty_increment.normalize():f} and at least {spec.min_order_size.normalize():f}")
            for label, v in (("limit price", limit), ("stop price", stop)):
                if v is not None and not assets.on_step(v, spec.price_increment):
                    raise OrderRejected(f"the {label} for {o.symbol} must be a multiple of {spec.price_increment.normalize():f}")
            if qty is not None and o.side == m.SELL and qty > o.qty:
                self._no_crypto_short(a, o.symbol, qty - o.filled_qty, ignore=o.id)
        elif qty is not None and (qty > MAX_QTY or qty != qty.quantize(Decimal("0.0001"))):
            raise ValueError("invalid quantity")
        if qty is not None and qty != o.qty and o.lots:
            raise OrderRejected("this order closes chosen tax lots; cancel it and enter a new one to change its quantity")
        old = (o.qty, o.limit_price, o.stop_price)
        self._handle(self.engine.replace(order_id, self.now(), qty, limit, stop))
        # Held bracket and exit children are re-sized to what their entry filled when they are
        # released, so editing them cannot add risk; a held triggered order keeps its own size.
        if a.type == "cash":
            # Every open order of a cash account is reserved for, held children included. Only a sale
            # that still just closes part of a holding, and uncovers nothing, skips the check.
            closing = o.side == m.SELL and o.status != m.HELD and self._reduces_risk(
                a, dc_replace(o, qty=o.remaining, filled_qty=ZERO), exclude=o.id)
            checked = True
        else:
            closing = self._reduces_risk_after(a, o)
            checked = o.status != m.HELD or o.role == m.TRIGGERED
        if checked and not (after := self._worst_case(a, [])).ok and not closing:
            o.qty, o.limit_price, o.stop_price = old
            raise OrderRejected(self._shortfall(a, after, o, "the change"))
        if o.status != m.HELD:  # pattern day trader and settled-cash rules, as on submit
            rule = self.lifecycle.pre_trade(a, o, None if closing else self._worst_case(a, []))
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
            if is_crypto(symbol):
                raise OrderRejected("multi-leg orders are for options and their underlying stock, not crypto")
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
        if any(o.type in m.CLOSE_TYPES for o in due):  # the closing auction fills before the feed moves on
            self._enforce_cash_orders(now, accounts={o.account_id for o in due})
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
        self._enforce_cash_orders(now, sess if not self._due_open else None)
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
    def _names_lots(self, fill) -> Optional[Order]:
        order = self.engine.orders.get(fill.order_id)
        return order if order is not None and order.lots and order.symbol == fill.symbol else None

    def _clip_lot_fill(self, acct: Account, order: Order, fill: Fill) -> list:
        """Limit a lot order's fill to what its named lots still hold. Returns the lots that fell short.

        The engine has already counted the whole fill on the order; the part the named lots cannot
        cover is taken off again, so the order never closes a lot it did not name or opens a short.
        """
        closing_long = fill.side == m.SELL
        held = {l.id: abs(l.qty) for l in acct.lots.get(fill.symbol, ()) if (l.qty > 0) == closing_long}
        can, short = ZERO, []
        for e in order.lots:
            want = max(D(e["qty"]) - D(e.get("filled", "0")), ZERO)
            have = held.get(e["lot_id"], ZERO)
            can += min(want, have)
            if want > have:
                short.append(e["lot_id"])
        if fill.qty <= can:
            return []
        order.filled_qty -= fill.qty - can
        order.status = m.PARTIAL if order.filled_qty > 0 else m.WORKING
        fill.qty = can
        return short or [e["lot_id"] for e in order.lots]

    def _handle(self, events: list) -> None:
        outer = self._booking
        self._booking = outer | {ev[1].order_id for ev in events if ev[0] == "fill" and ev[1].order_id}
        try:
            self._handle_batch(events)
        finally:
            self._booking = outer

    def _handle_batch(self, events: list) -> None:
        # Within one batch, fills of orders that name lots are booked first, so a plain order filling
        # in the same step cannot take lots another order named before that order's fill is booked.
        first = [ev for ev in events if ev[0] == "fill" and self._names_lots(ev[1]) is not None]
        if first:
            ids = {id(ev) for ev in first}
            events = first + [ev for ev in events if id(ev) not in ids]
        for kind, obj in events:
            if kind == "fill":
                acct = self.accounts.get(obj.account_id)
                if acct is None:
                    continue
                if is_crypto(obj.symbol) and obj.side == m.BUY and obj.kind == "trade" and not self._cap_crypto_buy(acct, obj):
                    continue
                order = self.engine.orders.get(obj.order_id)
                lot_order = self._names_lots(obj)
                cut = self._clip_lot_fill(acct, lot_order, obj) if lot_order is not None else []
                if obj.qty > 0:
                    obj.fee = self._fee(obj.symbol, obj.qty, obj.price, acct)
                    acct.apply_fill(obj, multiplier(obj.symbol), lot_order.lots if lot_order else None,
                                    self.reserved_lots(acct.id, exclude=obj.order_id))
                    self.lifecycle.on_fill(acct, obj)
                    if order:
                        order.fees += obj.fee
                    self.events.append({"type": "fill", "account_id": obj.account_id, "fill": obj})
                if cut:
                    booked = [f for f in acct.fills if f.order_id == lot_order.id]
                    q = sum((f.qty for f in booked), ZERO)
                    lot_order.avg_fill_price = px4(sum((f.price * f.qty for f in booked), ZERO) / q) if q else None
                    self._handle(self.engine.cancel(lot_order.id, obj.ts, f"Canceled: lot {', '.join(cut)} named by this "
                                                                          f"order was closed by another trade"))
                self._cancel_broken_lot_orders(acct, obj.ts)
            else:
                self.events.append({"type": "order", "account_id": obj.account_id, "order": obj})
            self.dirty = True

    def _affordable(self, acct: Account, symbol: str, price: Decimal) -> Decimal:
        """The most of a crypto pair cash can pay for at this price, fee included (never a loan)."""
        step = self.crypto_spec(symbol).qty_increment
        cash = max(acct.cash, ZERO)
        if price <= 0:
            return ZERO
        q = assets.floor_to(cash / (price * (1 + self.crypto_fee_rate(acct) / 100)), step)
        while q > 0 and cents(q * price) + self._fee(symbol, q, price, acct) > cash:
            q -= step
        return max(q, ZERO)

    def _cap_crypto_buy(self, acct: Account, fill: Fill) -> bool:
        """Fill-time guard: a crypto purchase never takes cash below zero, whatever happened since entry.

        A fill larger than the cash affords is cut to what it affords and the rest of the order is canceled.
        Returns False when nothing at all can be bought (the fill is dropped).
        """
        can = self._affordable(acct, fill.symbol, fill.price)
        if fill.qty <= can:
            return True
        order = self.engine.orders.get(fill.order_id)
        cut = fill.qty - can
        fill.qty = can
        if order is not None:
            order.filled_qty -= cut
            notional = sum((f.qty * f.price for f in acct.fills if f.order_id == order.id), ZERO) + can * fill.price
            order.avg_fill_price = (notional / order.filled_qty).quantize(Decimal("1e-9")).normalize() if order.filled_qty else None
            if order.avg_fill_price is not None and order.avg_fill_price.as_tuple().exponent > 0:
                order.avg_fill_price = order.avg_fill_price.quantize(Decimal(1))
            order.status = m.PARTIAL if order.filled_qty > 0 else m.WORKING
            reason = (f"canceled: the price moved to {fill.price.normalize():f} and cash ({_amt(acct.cash)}) pays for "
                      f"{'only ' + format(can.normalize(), 'f') if can > 0 else 'none'} of the remaining "
                      f"{(can + cut).normalize():f} including the fee; crypto is never bought with borrowed money")
            self.engine.cancel(order.id, fill.ts, reason)
            self.events.append({"type": "order", "account_id": order.account_id, "order": order})
        return can > 0

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
                self._handle(self.engine.on_quote(q, now, "regular" if is_crypto(q.symbol) else sess))
        changed = {q.symbol for q in quotes}
        for sym in sorted(active_symbols):  # a set's order changes with the hash seed; fills must not
            c = parse_option(sym)
            if c and c.underlying in changed and c.expiry_ts > now and (oq := self._tradable(self.quote(sym))):
                self._handle(self.engine.on_quote(oq, now, sess))
        self._process_expirations(now, prev_tick)
        self._settle_day_start(now)
        self.lifecycle.after_step(now)  # margin calls and liquidation
        self.alerting.on_tick(quotes, now)
        if now - self._last_equity_sample >= 60:
            self._last_equity_sample = now
            for a in list(self.accounts.values()):
                s = self.isolated(a, lambda: self.summary(a.id), "valuing the account")
                if s is None or s["prices_unavailable"]:  # a position cannot be valued: a sample now would value it at zero
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
        utc_day = int(now // 86400)  # crypto day orders end at the UTC day roll, whatever the stock session does
        if self.session_mode != "always" and self._last_utc_day is not None and utc_day != self._last_utc_day:
            self._handle(self.engine.expire(now, lambda o: o.tif == m.DAY and o.parent_id is None and is_crypto(o.symbol)))
        self._last_utc_day = utc_day
        if self.session_mode != "always" and self._last_session is not None and sess != self._last_session:
            if self._last_session == "regular":
                self._handle(self.engine.expire(now, lambda o: o.tif == m.DAY and not o.extended_hours and o.parent_id is None
                                                and not is_crypto(o.symbol)))
            if sess == "closed":
                self._handle(self.engine.expire(now, lambda o: o.tif == m.DAY and o.parent_id is None
                                                and not is_crypto(o.symbol)))
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
        settled = set()
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
                    a.apply_fill(f, multiplier(symbol), reserved=self.reserved_lots(a.id))
                    a.ledger.transactions[-1].memo += f" (settlement: {note})"
                    self.lifecycle.on_fill(a, f)  # exercise and assignment proceeds settle T+1
                    self.events.append({"type": "fill", "account_id": a.id, "fill": f})
                    self._cancel_broken_lot_orders(a, now)

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
                        if long:
                            # Exercising a long option is a choice, so it may only use what nothing
                            # else needs. An assignment is an obligation and uses whatever is there.
                            settle = min(settle, self._free_to_exercise(a, sym, c, buys_stock))
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
                settled.add(a.id)
                self.dirty = True
        if settled:
            self._enforce_cash_orders(now, accounts=settled, expired=frozenset(settled))

    def _free_to_exercise(self, a: Account, sym: str, c, buys_stock: bool) -> Decimal:
        """How many contracts of a long option a cash account can exercise without using cash or
        shares that something else relies on.

        A call is paid for only with cash that is spare in every scenario of the pre-trade check:
        not the cash that secures short puts and not the cash reserved for open buy orders. A put
        delivers only shares that cover no other short call and that no open order is selling.
        """
        if buys_stock:
            worst = self._worst_case(a, [])  # a cash account: open orders that cannot be priced are left out
            spare = worst.cash_after - worst.requirement if worst.ok else ZERO
            return max(spare, ZERO) // (c.strike * 100)
        shares = a.position(c.underlying)
        for other, held in a.positions().items():
            oc = parse_option(other)
            if oc is not None and other != sym and held < 0 and oc.kind == "C" and oc.underlying == c.underlying:
                shares += held * 100
        for o in self.engine.orders.values():
            if o.account_id == a.id and o.active:
                selling = self._leg_deltas(o) if o.legs else {o.symbol: -o.remaining if o.side == m.SELL else ZERO}
                shares += min(selling.get(c.underlying, ZERO), ZERO)
        return max(shares, ZERO) // 100

    CANT_PAY = "The account can no longer pay for this order at the current price"
    CANT_COVER = "The account no longer has the cash or shares this order needs if it fills"
    USED_AT_EXPIRY = "The cash or shares reserved for this order were used to settle an option at expiry"

    def _enforce_cash_orders(self, now: float, sess: Optional[str] = None, accounts=None,
                             expired: frozenset = frozenset()) -> None:
        """Cancel the open orders a cash account can no longer cover, before they can fill.

        The pre-trade check reserves for an order when it is entered. Later events can use what
        was reserved or move the price: an option settles at expiry, or a stop or other market-type
        buy would now fill well above the price it was reserved at. A cash account must never go
        below zero or short, so whenever its open orders no longer pass the check (`_cash_worst`),
        the fewest orders needed are canceled: orders are set aside newest first until the rest
        pass, then each one set aside is put back, oldest first, if the rest still pass without it.
        The check holds for every subset of fills, so what is left is safe whatever fills.

        With `sess`, accounts none of whose orders can trade in that session are skipped (they are
        checked on the first tick on which they can). `accounts` limits the check to those accounts;
        `expired` names the accounts in which an option has just been settled. The matching engine
        itself knows nothing of cash.
        """
        open_by_account: dict = {}
        for o in self.engine.orders.values():
            if o.status in (m.WORKING, m.PARTIAL):
                open_by_account.setdefault(o.account_id, []).append(o)
        for account_id, orders in open_by_account.items():
            a = self.accounts.get(account_id)
            if a is None or a.type != "cash" or (accounts is not None and account_id not in accounts):
                continue
            if sess is not None and not any(self.engine._eligible(o, sess) for o in orders):
                continue
            passes = lambda out: self._cash_worst(a, [], exclude=frozenset(x.id for x in out)).ok
            if passes(()):
                continue
            newest_first = sorted(orders, key=lambda o: (o.created_at, o.id), reverse=True)
            out = []
            for o in newest_first:
                out.append(o)
                if passes(out):
                    break
            else:
                # Not even without every open order: the account itself is short (after an assignment,
                # for example). Only sales that close part of a holding and uncover nothing may stay.
                out = [o for o in orders if o.legs or o.side == m.BUY or not self._reduces_risk(
                    a, dc_replace(o, qty=o.remaining, filled_qty=ZERO), exclude=o.id)]
            if passes(out):
                for o in sorted(out, key=lambda o: (o.created_at, o.id)):  # keep what is not needed, oldest first
                    rest = [x for x in out if x is not o]
                    if passes(rest):
                        out = rest
            for o in sorted(out, key=lambda o: (o.created_at, o.id)):
                if not o.active:
                    continue  # already ended with its entry or its group
                cause = self.USED_AT_EXPIRY if account_id in expired else (
                    self.CANT_PAY if o.legs or o.side == m.BUY else self.CANT_COVER)
                self._handle(self.engine.cancel(o.id, now, cause))  # the order event also raises a notification

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
    # app/storage does the file work (format, segments, recovery, backups): docs/STATE_FORMAT.md.
    def _path(self, name: str) -> str:
        return os.path.join(self.data_dir, name)

    @property
    def store(self):
        """The StateStore of this exchange's data directory (None without one)."""
        if not self.data_dir:
            return None
        st = getattr(self, "_store", None)
        if st is None or st.dir != self.data_dir:
            from .storage.store import StateStore
            st = self._store = StateStore(self.data_dir)
        return st

    @staticmethod
    def _write(path: str, data: dict) -> None:
        from .storage.fsutil import atomic_write_bytes
        atomic_write_bytes(path, json.dumps(data).encode("utf-8"))

    @staticmethod
    def _read_text(path: str) -> str:
        """The whole file in one open-read-close (with the Windows retry of storage.fsutil.read_bytes)."""
        from .storage.fsutil import read_bytes
        return read_bytes(path).decode("utf-8")

    def document_head(self) -> dict:
        """Everything in the state document except the accounts and orders."""
        return {"watchlists": self.watchlists, "alerts": self.alerting.to_plain(),
                "notifications": self.alerting.notices.to_plain(), "seq": self._peek_sequences(),
                "lifecycle": self.lifecycle.to_plain()}

    def to_document(self) -> dict:
        """The complete state as one self-contained document (backups, archives, migrations)."""
        from .storage.schema import CURRENT_VERSION
        return {"version": CURRENT_VERSION, "accounts": [a.to_plain() for a in self.accounts.values()],
                "orders": [to_plain(o) for o in self.engine.orders.values()], **self.document_head()}

    def save_plan(self, force: bool = False):
        """Serialise what needs saving (pure: no file I/O). Call under the exchange lock; hand the result to
        self.store.write() outside it. None when there is nothing to save or saving must wait."""
        st = self.store
        if st is None or st.should_wait(force):
            return None
        state = None
        accounts = orders = head = None
        if self.dirty or force:
            # Cleared before the state is gathered (a change made meanwhile marks it dirty again) and set
            # back if the write fails, so a failed write is retried on the next save.
            self.dirty = False
            accounts, orders, head = list(self.accounts.values()), list(self.engine.orders.values()), self.document_head()
        market = None
        now = self.now()
        if force or now - self._last_market_save >= 60:
            self._last_market_save = now
            market = self.feed.snapshot() or None
        if accounts is None and market is None:
            return None
        from . import __version__ as app_version
        if accounts is None:  # only the market is due
            from .storage.store import SavePlan, encode
            plan = SavePlan(0, market=encode(market))
            with st._meta:
                st._seq += 1
                plan.seq = st._seq
            return plan
        return st.plan(accounts, orders, head, market, app_version)

    def write_plan(self, plan) -> bool:
        """Write a plan from save_plan(). On failure the state stays marked as changed, so it is saved again."""
        if plan is None:
            return True
        ok = self.store.write(plan)
        if not ok:
            self.dirty = True
            if plan.market is not None:
                self._last_market_save = float("-inf")
        return ok

    def save(self, force: bool = False) -> bool:
        st = self.store
        if st is None:
            return True
        if st.blocked:
            return False  # recovery: the files on disk must not be overwritten
        if st.should_wait(force):
            return st.error is None  # waiting out the back-off after a failed save
        os.makedirs(self.data_dir, exist_ok=True)
        return self.write_plan(self.save_plan(force))

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

    def apply_document(self, data: dict) -> None:
        """Replace the whole state with a validated, current-version document. Builds the accounts and orders
        first, so a document that fails there leaves the exchange as it was."""
        accounts = {a["id"]: Account.from_plain(a) for a in data["accounts"]}
        for a in accounts.values():
            self._drop_unpriced_samples(a)
        seq = data["seq"]
        orders = [from_plain(Order, o) for o in data["orders"]]
        self.accounts = accounts
        self.engine.restore(orders, seq["fill"])
        self.watchlists = data.get("watchlists", {})
        self.alerting.restore(data.get("alerts", []), data.get("notifications"))
        self._order_seq, self._acct_seq = count(seq["order"]), count(seq["account"])
        self._alert_seq, self._expiry_seq = count(seq["alert"]), count(seq["expiry"])
        self.lifecycle.restore(data.get("lifecycle"))

    def load(self) -> bool:
        """Load the saved state. False when there is none, or when it is damaged or from a newer version:
        then self.store.recovery says what happened, and nothing is saved until it is resolved."""
        st = self.store
        if st is None:
            return False
        res = st.load()
        self.load_result = res
        if res.market is not None:
            self.feed.restore(res.market)
        if res.market_problem:
            st.warnings.append(res.market_problem)
        if res.status != "ok":
            return False
        try:
            self.apply_document(res.doc)
        except (KeyError, TypeError, ValueError, ArithmeticError, AttributeError, IndexError) as e:
            st._damaged(f"state.json could not be loaded ({type(e).__name__}: {e})", None, "")
            self.accounts, self.engine.orders = {}, {}
            return False
        st.adopt(self.accounts.values())
        from .storage.schema import account_problems
        for a in self.accounts.values():
            for p in account_problems(a):
                st.warnings.append(p)
        return True
