"""The lifecycle job: the exchange calls it from a few one-line hooks.

Hooks (all in `Exchange`):
- `before_step(now)`: once per new business date, before the feed moves prices. Runs the
  end of each elapsed day (interest and borrow-fee accrual, monthly posting) and the
  start of each new day (settlement, corporate actions, dividend payments).
- `after_step(now)`: every tick. Starts, clears and enforces margin calls.
- `on_fill(account, fill)`: after every engine fill is booked. Records unsettled
  proceeds and day trades.
- `pre_trade(account, order, worst)`: before an order is accepted. Returns a rejection
  reason for the pattern day trader rule or the settled-cash rule, else None.
- `extend_summary(account, summary)`: adds settled cash, day-trade count and accruals.

Conventions (documented choices):
- A "day" is the New York trading date in "us" session mode and the UTC date in
  "always" mode, where every day is a business day.
- Interest and borrow fees accrue every calendar day on the balance at the end of that
  day (actual/360) and are posted to the ledger on the first day of the next month.
  The sub-cent remainder carries forward.
- Settlement is T+1 business day for trade fills. Only positive cash (sale proceeds,
  option premium received) is tracked as unsettled; purchases reduce cash at once, so
  settled cash = cash - unsettled proceeds.
- Holders of record are the positions at the start of the ex-date (the end of the
  previous day). The dividend is posted on the pay date.
- A margin call starts when equity falls below the maintenance requirement. If it is
  still below after the grace period, open orders are canceled and positions are
  closed with market orders, largest maintenance requirement first, during the regular
  session only.
"""
from datetime import date, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from itertools import count
from typing import Optional

from ..core import models as m
from ..core.models import Fill, Order
from ..core.money import ZERO, D, cents
from ..ledger.ledger import CASH
from ..options.symbology import multiplier, parse
from ..risk import margin
from . import settings as cfg
from . import splits
from .actions import DIVIDEND, SPLIT, CorporateAction
from .days import Days

DIVIDEND_INCOME = "income:dividends"
SHORT_DIVIDEND_EXPENSE = "expense:short_dividends"
INTEREST_EXPENSE = "expense:margin_interest"
BORROW_EXPENSE = "expense:borrow_fees"
DAY_COUNT = Decimal(360)
PDT_WINDOW = 5
PDT_LIMIT = 3
MAX_CATCH_UP_DAYS = 400
ACCRUAL_PLACES = Decimal("0.0000000001")
FOUR = Decimal("0.0001")


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _fmt(x: Decimal) -> str:
    return f"{cents(x):,.2f}"


class LifecycleManager:
    def __init__(self, ex):
        self.ex = ex
        self.last_date: Optional[date] = None
        self.applied: set = set()  # corporate action ids already applied to the accounts
        self._seq = count(1)

    @property
    def days(self) -> Days:
        return Days(self.ex.session_mode)

    def today(self) -> date:
        return self.days.today(self.ex.now())

    def next_fill_id(self) -> str:
        return f"L{next(self._seq):08d}"

    # ---- helpers ---------------------------------------------------------------
    def _event(self, account, kind: str, message: str, **extra) -> None:
        self.ex.events.append({"type": "lifecycle", "account_id": account.id if account else None, "kind": kind,
                               "message": message, **extra})
        self.ex.dirty = True

    def _log(self, account, ts: float, kind: str, message: str, postings=(), ref: str = "") -> None:
        """Record a step in the account's ledger (a memo-only entry when no cash moves) and as an event."""
        account.ledger.post(ts, kind, message, list(postings), ref=ref)
        self._event(account, kind, message)

    def _state(self, account):
        pos = account.positions()
        marks = self.ex._marks(pos)
        eq = margin.equity(account.cash, pos, marks)
        maint = margin.requirement(pos, marks, maintenance=True)
        return pos, marks, eq, maint

    # ---- day roll ----------------------------------------------------------------
    def before_step(self, now: float) -> None:
        today = self.days.today(now)
        if self.last_date is None:
            self.last_date = today
            self.ex.dirty = True
            return
        if today <= self.last_date:
            return
        d = max(self.last_date, today - timedelta(days=MAX_CATCH_UP_DAYS))
        by_day: dict = {}
        for act in self.ex.feed.corporate_actions(d + timedelta(days=1), today):
            if d < act.ex_date <= today:
                by_day.setdefault(act.ex_date, []).append(act)
        while d < today:
            nxt = d + timedelta(days=1)
            for a in list(self.ex.accounts.values()):
                self._accrue(a, d)
                if nxt.month != d.month:
                    self._post_accruals(a, now, d)
            self._start_of_day(nxt, sorted(by_day.get(nxt, []), key=lambda x: (x.kind != SPLIT, x.symbol)), now)
            d = nxt
        self.last_date = today
        self.ex.dirty = True

    def _start_of_day(self, day: date, actions: list, now: float) -> None:
        iso = day.isoformat()
        keep_from = self.days.window_start(day, PDT_WINDOW * 2).isoformat()
        for a in self.ex.accounts.values():
            a.life.unsettled = [e for e in a.life.unsettled if e[0] > iso]
            a.life.day_trades = [e for e in a.life.day_trades if e[0] >= keep_from]
        for act in actions:
            if act.id in self.applied:
                continue
            if act.kind == SPLIT:
                self._apply_split(act, now)
            elif act.kind == DIVIDEND:
                self._record_dividend(act, now)
            self.ex.feed.apply_corporate_action(act)
            self.applied.add(act.id)
            self.ex._last_market_save = float("-inf")  # save the adjusted market with the accounts
        for a in self.ex.accounts.values():
            self._pay_dividends(a, day, now)

    # ---- interest and borrow fees ---------------------------------------------------
    def _accrue(self, a, day: date) -> None:
        if a.type != "margin":
            return
        s = a.settings
        if s["margin_interest"]:
            debit = max(-a.cash, ZERO)
            if debit:
                a.life.accrued_interest += (debit * cfg.number(s, "margin_interest_rate") / 100 / DAY_COUNT
                                            ).quantize(ACCRUAL_PLACES)
        if s["borrow_fee"]:
            shorts = {sym: q for sym, q in a.positions().items() if q < 0 and not parse(sym)}
            if shorts:
                mv = sum((-q * self.ex.mark(sym) for sym, q in shorts.items()), ZERO)
                a.life.accrued_borrow += (mv * cfg.number(s, "borrow_fee_rate") / 100 / DAY_COUNT
                                          ).quantize(ACCRUAL_PLACES)

    def _post_accruals(self, a, ts: float, last_day: date) -> None:
        month = last_day.strftime("%B %Y")
        for attr, acct, label, key in (("accrued_interest", INTEREST_EXPENSE, "Margin interest", "margin_interest_rate"),
                                       ("accrued_borrow", BORROW_EXPENSE, "Short borrow fee", "borrow_fee_rate")):
            amount = cents(getattr(a.life, attr))
            if amount > 0:
                rate = cfg.number(a.settings, key).normalize()
                a.ledger.post(ts, attr.replace("accrued_", ""), f"{label} for {month} ({rate:f}% a year, actual/360)",
                              [(acct, amount), (CASH, -amount)])
                setattr(a.life, attr, getattr(a.life, attr) - amount)
                self._event(a, label.lower(), f"{label} of {_fmt(amount)} charged for {month}")

    # ---- dividends -----------------------------------------------------------------
    def _record_dividend(self, act: CorporateAction, now: float) -> None:
        for a in self.ex.accounts.values():
            if not a.settings["dividends"]:
                continue
            qty = a.position(act.symbol)
            if qty == 0:
                continue
            amount = cents(qty * act.amount)
            a.life.pending_dividends.append({"action": act.id, "symbol": act.symbol, "qty": str(qty),
                                             "rate": str(act.amount), "amount": str(amount),
                                             "ex_date": act.ex_date.isoformat(), "pay_date": act.pay_date.isoformat()})
            verb = "will receive" if amount > 0 else "will be charged"
            self._event(a, "dividend_record", f"{act.symbol} went ex-dividend: you {verb} {_fmt(abs(amount))} "
                                              f"on {act.pay_date.isoformat()} for {qty.normalize():f} shares")

    def _pay_dividends(self, a, day: date, now: float) -> None:
        iso = day.isoformat()
        due = [p for p in a.life.pending_dividends if p["pay_date"] <= iso]
        if not due:
            return
        a.life.pending_dividends = [p for p in a.life.pending_dividends if p["pay_date"] > iso]
        for p in due:
            amount, qty = D(p["amount"]), D(p["qty"])
            rate = D(p["rate"]).normalize()
            if amount > 0:
                memo = f"Dividend: {rate:f} per share on {qty.normalize():f} {p['symbol']} (ex-date {p['ex_date']})"
                a.ledger.post(now, "dividend", memo, [(CASH, amount), (DIVIDEND_INCOME, -amount)], ref=p["action"])
                self._event(a, "dividend", f"Dividend of {_fmt(amount)} paid on {p['symbol']}")
                if a.settings["dividend_reinvestment"]:
                    self._reinvest(a, p["symbol"], amount, now)
            elif amount < 0:
                memo = (f"Dividend charged on short position: {rate:f} per share on {abs(qty).normalize():f} "
                        f"{p['symbol']} (ex-date {p['ex_date']})")
                a.ledger.post(now, "dividend", memo, [(CASH, amount), (SHORT_DIVIDEND_EXPENSE, -amount)], ref=p["action"])
                self._event(a, "dividend", f"Dividend of {_fmt(-amount)} charged on short {p['symbol']}")

    def _reinvest(self, a, symbol: str, amount: Decimal, now: float) -> None:
        price = self.ex.mark(symbol)
        if price <= 0:
            self._event(a, "reinvestment", f"Dividend on {symbol} not reinvested: no price available")
            return
        qty = (amount / price).quantize(FOUR, ROUND_FLOOR)
        if qty <= 0:
            return
        f = Fill(id=self.next_fill_id(), order_id=None, account_id=a.id, symbol=symbol, side=m.BUY, qty=qty,
                 price=price, ts=now, kind="reinvestment")
        a.apply_fill(f, 1)
        self._event(a, "reinvestment", f"Reinvested the {symbol} dividend: bought {qty.normalize():f} shares at {price}")

    # ---- splits ----------------------------------------------------------------------
    def _apply_split(self, act: CorporateAction, now: float) -> None:
        sym = act.symbol
        reason = f"Canceled because of the {act.describe()} effective {act.ex_date.isoformat()}"
        on_sym = lambda s: s == sym or ((c := parse(s)) is not None and c.underlying == sym)
        for _ in range(3):  # canceling a partly filled entry releases its bracket children
            active = self.ex.engine.active_orders()
            open_ = [o for o in active if on_sym(o.symbol) or any(on_sym(l["symbol"]) for l in o.legs or ())]
            # A linked group (one-cancels-other, one-triggers-other, bracket) is one instruction: when any
            # member is on the split symbol, cancel the whole group so no half of it is left working.
            ids = {o.id for o in open_}
            groups = {o.oco_group for o in open_ if o.oco_group} | ids | {o.parent_id for o in open_ if o.parent_id}
            open_ += [o for o in active if o.id not in ids and (o.oco_group in groups or o.parent_id in groups
                                                                or o.id in groups)]
            if not open_:
                break
            for o in open_:
                self.ex._handle(self.ex.engine.cancel(o.id, now, reason))
        q = self.ex.feed.quote(sym)
        for a in self.ex.accounts.values():
            price = q.last if q is not None and q.last > 0 else a.avg_cost(sym)
            marks = {s: self.ex.mark(s) for s in a.lots if (c := parse(s)) is not None and c.underlying == sym}
            res = splits.split_stock(a, act, price, now)
            if res:
                self._event(a, "split", res["message"])
            for note in splits.split_options(a, act, marks, now, self.next_fill_id):
                if "fill" in note:
                    self.ex.events.append({"type": "fill", "account_id": a.id, "fill": note["fill"]})
                self._event(a, "split", note["message"])

    # ---- fills -----------------------------------------------------------------------
    def on_fill(self, a, fill: Fill) -> None:
        if fill.kind != "trade":
            return
        day = self.days.today(fill.ts)
        txn = a.ledger.transactions[-1] if a.ledger.transactions else None
        cash = sum((v for acct, v in txn.postings if acct == CASH), ZERO) if txn and txn.ref == fill.id else ZERO
        if a.settings["settlement"] and cash > 0:
            a.life.unsettled.append([self.days.add(day, 1).isoformat(), cash, fill.id])
        self._track_day_trade(a, fill, day.isoformat())

    def _track_day_trade(self, a, fill: Fill, iso: str) -> None:
        life = a.life
        if life.opened_date != iso:
            life.opened, life.opened_date = {}, iso
        signed = fill.qty if fill.side == m.BUY else -fill.qty
        after = a.position(fill.symbol)
        before = after - signed
        closing = min(fill.qty, abs(before)) if before != 0 and (before > 0) != (signed > 0) else ZERO
        opening = fill.qty - closing
        opened = life.opened.get(fill.symbol, ZERO)
        if closing > 0:
            day_trade = min(closing, opened)
            if day_trade > 0:
                opened -= day_trade
                if not any(e[0] == iso and e[2] == fill.order_id for e in life.day_trades):
                    life.day_trades.append([iso, fill.symbol, fill.order_id])
        if opening > 0:
            opened = opening if closing > 0 else opened + opening
        if opened > 0:
            life.opened[fill.symbol] = opened
        else:
            life.opened.pop(fill.symbol, None)

    # ---- pre-trade checks --------------------------------------------------------------
    def day_trade_count(self, a, now: Optional[float] = None) -> int:
        today = self.days.today(self.ex.now() if now is None else now)
        start = self.days.window_start(today, PDT_WINDOW).isoformat()
        return sum(1 for e in a.life.day_trades if e[0] >= start)

    def would_day_trade(self, a, order: Order) -> bool:
        pos = a.position(order.symbol)
        if pos == 0 or (pos > 0) == (order.side == m.BUY):
            return False
        return a.life.opened_date == self.today().isoformat() and a.life.opened.get(order.symbol, ZERO) > 0

    def pre_trade(self, a, order: Order, worst) -> Optional[str]:
        """Reason to reject the order, or None. `worst` is the exchange's worst-case check (None when closing)."""
        s = a.settings
        if a.type == "margin" and s["pattern_day_trader"] and self.would_day_trade(a, order):
            n = self.day_trade_count(a)
            if n >= PDT_LIMIT:
                _, _, eq, _ = self._state(a)
                minimum = cfg.number(s, "pdt_min_equity")
                if eq < minimum:
                    return (f"Pattern day trader rule: this order would be your {_ordinal(n + 1)} day trade in "
                            f"{PDT_WINDOW} business days. Margin accounts with less than {_fmt(minimum)} equity may "
                            f"make at most {PDT_LIMIT} (account equity is {_fmt(eq)}).")
        if a.type == "cash" and worst is not None and s["settlement"] and s["settled_cash_only"]:
            unsettled = a.life.unsettled_total()
            if unsettled > 0 and worst.cash_after - unsettled < worst.requirement:
                needed = a.cash - worst.cash_after + worst.requirement
                available = max(a.cash - unsettled, ZERO)
                first = min(e[0] for e in a.life.unsettled)
                return (f"Cash accounts can only buy with settled cash: this order and your open orders need "
                        f"{_fmt(needed)}, but settled cash is {_fmt(available)}. {_fmt(unsettled)} from recent sales "
                        f"is still settling (next settlement {first}).")
        return None

    # ---- summary -------------------------------------------------------------------------
    def extend_summary(self, a, out: dict) -> None:
        unsettled = a.life.unsettled_total()
        out["settled_cash"] = a.cash - unsettled
        out["unsettled_cash"] = unsettled
        s = a.settings
        if a.type == "cash" and s["settlement"] and s["settled_cash_only"] and unsettled > 0:
            out["buying_power"] = max(out["buying_power"] - unsettled, ZERO)
            out["option_buying_power"] = max(out["option_buying_power"] - unsettled, ZERO)
        n = self.day_trade_count(a)
        minimum = cfg.number(s, "pdt_min_equity")
        pdt_on = a.type == "margin" and s["pattern_day_trader"]
        out["day_trades"] = n
        out["day_trades_left"] = (max(PDT_LIMIT - n, 0) if pdt_on and out["equity"] < minimum else None)
        out["accrued_interest"] = cents(a.life.accrued_interest)
        out["accrued_borrow_fee"] = cents(a.life.accrued_borrow)
        out["pending_dividends"] = sum((D(p["amount"]) for p in a.life.pending_dividends), ZERO)
        since = a.life.margin_call_since
        out["margin_call_since"] = since
        out["liquidation_at"] = (since + float(cfg.number(s, "margin_call_grace_hours")) * 3600
                                 if since is not None and s["margin_call_liquidation"] else None)

    # ---- margin calls ----------------------------------------------------------------------
    def after_step(self, now: float) -> None:
        for a in list(self.ex.accounts.values()):
            if a.type != "margin" or (not a.lots and a.life.margin_call_since is None):
                continue
            pos, marks, eq, maint = self._state(a)
            life = a.life
            if pos and eq < maint:
                if life.margin_call_since is None:
                    life.margin_call_since, life.margin_call_deficit = now, maint - eq
                    grace = cfg.number(a.settings, "margin_call_grace_hours").normalize()
                    tail = (f" Positions will be liquidated if it is not met within {grace:f} hours."
                            if a.settings["margin_call_liquidation"] else "")
                    self._log(a, now, "margin_call",
                              f"Margin call: equity {_fmt(eq)} is below the {_fmt(maint)} maintenance requirement "
                              f"(deficit {_fmt(maint - eq)}).{tail}")
                elif (a.settings["margin_call_liquidation"] and self.ex.session(now) == "regular"
                      and now >= life.margin_call_since + float(cfg.number(a.settings, "margin_call_grace_hours")) * 3600):
                    self.liquidate(a, now)
                    pos, marks, eq, maint = self._state(a)
                    if not pos or eq >= maint:
                        self._clear_call(a, now, eq, maint, "after liquidation")
            elif life.margin_call_since is not None:
                self._clear_call(a, now, eq, maint, "")

    def _clear_call(self, a, now, eq, maint, how: str) -> None:
        a.life.margin_call_since, a.life.margin_call_deficit = None, ZERO
        self._log(a, now, "margin_call", f"Margin call met{(' ' + how) if how else ''}: equity {_fmt(eq)}, "
                                         f"maintenance requirement {_fmt(maint)}.")

    def liquidate(self, a, now: float, max_steps: int = 50) -> list:
        """Close positions until equity is back above maintenance. Returns the steps taken."""
        ex = self.ex
        for _ in range(3):
            open_ = [o for o in ex.engine.active_orders() if o.account_id == a.id]
            if not open_:
                break
            for o in open_:
                ex._handle(ex.engine.cancel(o.id, now, "Canceled for margin call liquidation"))
        if any(o.account_id == a.id for o in ex.engine.active_orders()):
            return []
        steps = []
        for _ in range(max_steps):
            pos, marks, eq, maint = self._state(a)
            if not pos or eq >= maint:
                break
            deficit = maint - eq
            best = fallback = None
            for sym, q in pos.items():
                quote = ex.quote(sym)
                if quote is None or (quote.bid if q > 0 else quote.ask) <= 0:
                    continue  # nothing to sell into (or buy from)
                without = {k: v for k, v in pos.items() if k != sym}
                marginal = maint - margin.requirement(without, marks, maintenance=True)
                if marginal < 0:
                    continue
                if marginal == 0:
                    # Closing it frees nothing now (a covered short call), but it unlocks the stock that covers it.
                    if fallback is None:
                        fallback = (marginal, sym, q, abs(q), maint)
                    continue
                mult = multiplier(sym)
                slip = max((marks[sym] - quote.bid) if q > 0 else (quote.ask - marks[sym]), ZERO) * mult
                per_unit = marginal / abs(q) - slip
                qty = abs(q) if per_unit <= 0 else (deficit / per_unit).to_integral_value(ROUND_CEILING)
                qty = min(max(qty, Decimal(1)), abs(q))
                after = self._reduced(pos, sym, q, qty)
                req_after = margin.requirement(after, marks, maintenance=True)
                if req_after >= maint and qty < abs(q):  # a partial close does not help (e.g. stock covering calls)
                    qty = abs(q)
                    req_after = margin.requirement(self._reduced(pos, sym, q, qty), marks, maintenance=True)
                if req_after >= maint:
                    continue
                if best is None or marginal > best[0]:
                    best = (marginal, sym, q, qty, req_after)
            best = best or fallback
            if best is None:
                self._event(a, "liquidation", "Margin call liquidation paused: no position can be closed to "
                                              "reduce the requirement right now (no usable quote).")
                break
            _, sym, q, qty, req_after = best
            side = m.SELL if q > 0 else m.BUY
            o = Order(id=f"O{next(ex._order_seq):08d}", account_id=a.id, symbol=sym, side=side, qty=qty,
                      type=m.MARKET, tif=m.DAY, role="liquidation")
            ex._handle(ex.engine.submit(o, ex.quote(sym), now, ex.session(now)))
            _, _, eq_after, maint_after = self._state(a)
            steps.append({"symbol": sym, "side": side, "qty": qty, "requirement_before": maint,
                          "requirement_after": maint_after, "equity_before": eq, "equity_after": eq_after,
                          "order_id": o.id, "status": o.status})
            self._log(a, now, "liquidation",
                      f"Margin call liquidation: {side} {qty.normalize():f} {sym} at market (order {o.id}, {o.status}); "
                      f"equity {_fmt(eq)} -> {_fmt(eq_after)}, maintenance requirement {_fmt(maint)} -> "
                      f"{_fmt(maint_after)}.", ref=o.id)
            if o.active:
                ex._handle(ex.engine.cancel(o.id, now, "Unfilled part of a liquidation order; retried next tick"))
                break
        return steps

    @staticmethod
    def _reduced(pos: dict, sym: str, q: Decimal, qty: Decimal) -> dict:
        out = dict(pos)
        rest = q - qty if q > 0 else q + qty
        if rest == 0:
            out.pop(sym)
        else:
            out[sym] = rest
        return out

    # ---- persistence ---------------------------------------------------------------------------
    def to_plain(self) -> dict:
        n = next(self._seq)
        self._seq = count(n)
        return {"last_date": self.last_date.isoformat() if self.last_date else None,
                "applied": sorted(self.applied), "seq": n}

    def restore(self, d) -> None:
        if not isinstance(d, dict):
            return
        self.last_date = date.fromisoformat(d["last_date"]) if d.get("last_date") else None
        self.applied = set(d.get("applied", []))
        self._seq = count(int(d.get("seq", 1)))

    # ---- reporting ------------------------------------------------------------------------------
    def upcoming(self, days: int = 120, symbol: Optional[str] = None) -> list:
        today = self.today()
        acts = self.ex.feed.corporate_actions(today - timedelta(days=30), today + timedelta(days=days))
        return sorted((x for x in acts if symbol is None or x.symbol == symbol), key=lambda x: (x.ex_date, x.symbol))
