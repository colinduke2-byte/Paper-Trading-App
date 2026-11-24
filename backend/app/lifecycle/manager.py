"""The lifecycle job: the exchange calls it from a few one-line hooks.

Hooks (all in `Exchange`):
- `before_step(now)`: once per new business date, before the feed moves prices. Runs the
  end of each elapsed day (interest and borrow-fee accrual, monthly posting) and the
  start of each new day (settlement, corporate actions, dividend payments). Corporate
  actions are fetched from the feed one day at a time, so an action that depends on an
  earlier one (a dividend after a split) is announced with the feed already adjusted.
- `after_step(now)`: every tick. Starts, clears and enforces margin calls.
- `on_fill(account, fill)`: after a fill is booked (trades, exercise, assignment and
  split settlements). Records unsettled proceeds and, for trades, day trades.
- `pre_trade(account, order, worst)`: before an order is accepted or changed. Returns a
  rejection reason for the pattern day trader rule or the settled-cash rule, else None.
- `extend_summary(account, summary)`: adds settled cash, day-trade count and accruals,
  and counts dividends receivable or payable in equity.

Conventions (documented choices):
- A "day" is the New York trading date in "us" session mode and the UTC date in
  "always" mode, where every day is a business day. The clock may jump forward by any
  amount in one step (replay): every elapsed day is processed in order.
- Interest and borrow fees accrue every calendar day on the balance at the end of that
  day (actual/360) and are posted to the ledger on the first day of the next month.
  The sub-cent remainder carries forward.
- Settlement is T+1 business day for every fill that brings cash in (sales, option
  premium, exercise and assignment proceeds, split settlements). Purchases reduce cash
  at once, so settled cash = cash - unsettled proceeds. Cash in lieu and dividends are
  paid as settled cash.
- Holders of record are the positions at the start of the ex-date. The dividend is
  posted on the pay date; until then it is counted in equity as receivable (long) or
  payable (short), because the price drops on the ex-date. On the ex-date open buy
  limits, sell stops and sell trailing-stop references on the stock are lowered by the
  dividend (the usual broker convention), so the drop alone does not trigger them.
- Pattern day trader rule: a day trade is counted once per opening order (lot) that is
  closed on the day it was opened. A margin account under the minimum equity that has
  made 3 day trades in the last 5 business days cannot place new opening orders; closing
  orders, and bracket children that only close, are never blocked.
- A margin call starts when equity falls below the maintenance requirement. It is
  cleared only when equity is back above maintenance by 2%, or has stayed at or above
  maintenance for 5 minutes; brief recoveries keep the original start time. If it is
  still open after the grace period, open orders are canceled and positions are closed
  with market orders, largest maintenance requirement first, during the regular session
  only, never leaving a short option uncovered that was covered before.
- No margin check runs while any held symbol (or the underlying of a held option) has
  no price; the summary lists those symbols in `prices_unavailable` instead.
- No margin check runs either while a held symbol has a split the feed knows about that is due
  but not yet applied to the accounts, or while the start of the day waits for corporate action
  data: the price may already be on the new basis while positions are not, which would look like a
  large loss or gain. A held symbol with a corporate action PaperDesk cannot apply (merger,
  spin-off, ...) in the last few days does not stop margin calls, but liquidation waits and says
  why: the price jump may not be a real loss. Both show in the summary as `margin_paused`.
  Prices the feed marks stale are listed in `stale_prices`; they still value the account.
- Margin call and liquidation steps go to the events stream and the account's lifecycle
  log; the ledger only records money and position movements.
- Corporate actions from a live feed (docs/DATA_PROVIDERS.md, "Corporate actions"): the feed
  fetches them on its own thread. If the data for the next day is not in yet (the feed raises
  `ActionsPending`), the day roll stops before that day (nothing of it is done, so nothing is done
  twice) and is retried on a later tick; the day is never skipped. A feed that cannot supply them
  at all (`ActionsUnavailable`) is reported once and the days roll on without them. A feed with
  `late_actions` also announces actions found after their ex-date (providers can publish late):
  they are applied on the next day roll, holders taken from the positions at that time, and
  logged as late. An action is applied once: by id, and by its `revision_key` when the feed gives
  one, so a re-issued or amended record is not applied again (the change is logged instead).
  Kinds the lifecycle cannot process (mergers, spin-offs, ...) arrive as notices, shown on the
  affected positions and in the account log.
"""
from datetime import date, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from itertools import count
from typing import Optional

from ..core import models as m
from ..core.models import Fill, Order
from ..core.money import CENT, ZERO, D, cents
from ..ledger.ledger import CASH
from ..options.symbology import multiplier, parse
from ..risk import margin
from . import settings as cfg
from . import splits
from .actions import DIVIDEND, SPLIT, ActionsPending, ActionsUnavailable, CorporateAction
from .days import Days

DIVIDEND_INCOME = "income:dividends"
SHORT_DIVIDEND_EXPENSE = "expense:short_dividends"
INTEREST_EXPENSE = "expense:margin_interest"
BORROW_EXPENSE = "expense:borrow_fees"
DAY_COUNT = Decimal(360)
PDT_WINDOW = 5
PDT_LIMIT = 3
MAX_CATCH_UP_DAYS = 400
LATE_DAYS = 10  # how far back a feed with late_actions is asked for actions published after their ex-date
NOTICE_LIMIT = 5  # unsupported corporate action notices kept per symbol
ACCRUAL_PLACES = Decimal("0.0000000001")
FOUR = Decimal("0.0001")
CLEAR_BUFFER = Decimal("0.02")  # a call clears at once when equity is 2% above maintenance
CLEAR_SECONDS = 300  # ... or after equity has stayed at or above maintenance this long
LOG_LIMIT = 200
# "closed at expiry" (orders branch): a cash account's option closed for intrinsic value at expiry settles like a sale.
SETTLING_KINDS = ("trade", "exercise", "assignment", "split_settlement", "closed at expiry")


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _fmt(x: Decimal) -> str:
    return f"{cents(x):,.2f}"


def uncovered(pos: dict) -> dict:
    """Short option contracts not covered, per (underlying, 'C' or 'P').

    A short call is covered by 100 long shares, or by a long call on the same underlying that
    expires no earlier (a spread or calendar: its risk is limited). A short put likewise by 100
    short shares or a long put expiring no earlier.
    """
    legs: dict = {}
    for s, q in pos.items():
        c = parse(s)
        if c is not None:
            d = legs.setdefault((c.underlying, c.kind), {"short": [], "long": []})
            d["short" if q < 0 else "long"].append((c.expiry, abs(q)))
    out = {}
    for (u, kind), d in legs.items():
        stock = pos.get(u, ZERO)
        pool = (max(stock, ZERO) if kind == "C" else max(-stock, ZERO)) // 100
        longs = sorted(d["long"], reverse=True)
        j, left = 0, ZERO
        for exp, n in sorted(d["short"], reverse=True):  # latest short first; any long seen so far covers it
            while j < len(longs) and longs[j][0] >= exp:
                pool += longs[j][1]
                j += 1
            use = min(pool, n)
            pool -= use
            left += n - use
        out[(u, kind)] = left
    return out


class LifecycleManager:
    def __init__(self, ex):
        self.ex = ex
        self.last_date: Optional[date] = None
        self.applied: set = set()  # corporate action ids already applied to the accounts
        self.split_records: dict = {}  # id -> plain record of every applied split, to repair the feed
        self.records: dict = {}  # id -> plain record of every applied action from a feed with revision keys
        self.notices: dict = {}  # symbol -> [notice]: corporate actions PaperDesk could not apply
        self.since: Optional[date] = None  # first day this lifecycle ran: late actions never reach before it
        self.waiting: Optional[dict] = None  # {"day", "reason", "since"} while the day roll waits for the feed
        self._unavailable_reported: Optional[str] = None
        self._paused: dict = {}  # account id -> reason last reported for paused margin checks
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

    def _log(self, account, ts: float, kind: str, message: str) -> None:
        """A step worth keeping (margin calls, liquidation): the events stream plus the account's log."""
        account.life.log.append([ts, kind, message])
        del account.life.log[:-LOG_LIMIT]
        self._event(account, kind, message)

    def _missing_prices(self, pos: dict, marks: dict) -> list:
        return sorted(s for s, v in marks.items() if v <= 0)

    def _holds(self, pos: dict) -> tuple:
        """(reasons margin checks are paused, reasons liquidation is paused) for these positions."""
        if not pos:
            return [], []
        unders = {(c.underlying if (c := parse(s)) else s) for s in pos}
        checks, liq = [], []
        if self.waiting is not None:
            checks.append(f"the start of {self.waiting['day']} is waiting for corporate action data, so splits "
                          f"and dividends for that day are not applied yet")
        holds = {}
        if hasattr(self.ex.feed, "margin_holds"):
            try:
                holds = self.ex.feed.margin_holds(self.today(), self.applied, self.since)
            except Exception:  # a feed problem must not stop the tick
                holds = {}
        for sym in sorted(unders):
            for h in holds.get(sym, ()):
                (checks if h["kind"] == "split_pending" else liq).append(h["reason"])
        return checks, liq

    def _state(self, account):
        """Positions, marks, equity (with dividends receivable or payable) and maintenance requirement."""
        pos = account.positions()
        marks = self.ex._marks(pos)
        eq = margin.equity(account.cash, pos, marks) + self.pending_dividends(account)
        maint = margin.requirement(pos, marks, maintenance=True)
        return pos, marks, eq, maint

    @staticmethod
    def pending_dividends(a) -> Decimal:
        return sum((D(p["amount"]) for p in a.life.pending_dividends), ZERO)

    # ---- day roll ----------------------------------------------------------------
    def before_step(self, now: float) -> None:
        today = self.days.today(now)
        if self.last_date is None:
            self.last_date = self.since = today
            self.ex.dirty = True
            return
        if today <= self.last_date:
            return
        d = max(self.last_date, today - timedelta(days=MAX_CATCH_UP_DAYS))
        while d < today:
            nxt = d + timedelta(days=1)
            # Ask the feed for this day only, after earlier days' actions were applied to it. This comes
            # first: if the data is not in yet, nothing of day d's end or nxt's start has been done.
            fetched = self._actions_for_day(nxt, now)
            if fetched is None:
                break  # wait for the feed; a later tick retries from the same day
            acts, notices = fetched
            for a in list(self.ex.accounts.values()):
                self._accrue(a, d)
                if nxt.month != d.month:
                    self._post_accruals(a, now, d)
            self._start_of_day(nxt, sorted(acts, key=lambda x: (x.kind != SPLIT, x.ex_date, x.symbol)), now)
            self._unsupported(nxt, notices, now)
            d = nxt
            self.last_date = d
        self.ex.dirty = True

    def _actions_for_day(self, day: date, now: float):
        """(actions effective on `day`, notices) from the feed, or None while the feed is not ready."""
        feed = self.ex.feed
        late = bool(getattr(feed, "late_actions", False))
        start = day
        if late and self.since is not None:
            start = max(day - timedelta(days=LATE_DAYS), self.since + timedelta(days=1))
        try:
            acts = feed.corporate_actions(start, day)
        except ActionsPending as e:
            if self.waiting is None or self.waiting["day"] != day.isoformat() or self.waiting["reason"] != e.reason:
                self.waiting = {"day": day.isoformat(), "reason": e.reason, "since": now}
                self._event(None, "corporate_actions_wait",
                            f"The start of {day.isoformat()} is waiting for corporate action data: {e.reason} "
                            f"Settlement, dividends and splits for that day follow once it arrives.")
            return None
        except ActionsUnavailable as e:
            acts = []
            if self._unavailable_reported != e.reason:
                self._unavailable_reported = e.reason
                self._event(None, "corporate_actions_unavailable",
                            f"Corporate actions are not available, so dividends and splits are not applied: {e.reason}")
        if self.waiting is not None:
            self.waiting = None
        # Earlier ex-dates (late feeds only) are passed on every day of the window: one never applied yet is
        # applied late, one already applied is skipped (and a changed record is logged) in _start_of_day.
        acts = [x for x in acts if x.ex_date == day or (late and start <= x.ex_date < day)]
        notices = []
        if hasattr(feed, "unsupported_actions"):
            try:
                notices = feed.unsupported_actions(start, day)
            except (ActionsPending, ActionsUnavailable):
                notices = []
        return acts, notices

    def _start_of_day(self, day: date, actions: list, now: float) -> None:
        iso = day.isoformat()
        keep_from = self.days.window_start(day, PDT_WINDOW * 2).isoformat()
        for a in self.ex.accounts.values():
            a.life.unsettled = [e for e in a.life.unsettled if e[0] > iso]
            a.life.day_trades = [e for e in a.life.day_trades if e[0] >= keep_from]
        for act in actions:
            if self._already_applied(act, now):
                continue
            if act.ex_date < day:
                self._event(None, "corporate_action_late",
                            f"The {act.describe()} (ex-date {act.ex_date.isoformat()}) was published late by the data "
                            f"provider and is applied now; holders are the positions at the start of {iso}.")
            if act.kind == SPLIT:
                self._apply_split(act, now)
                self.split_records[act.id] = act.to_plain()
            elif act.kind == DIVIDEND:
                self._record_dividend(act, now)
                self._adjust_orders_for_dividend(act, now)
            self.ex.feed.apply_corporate_action(act)
            self.applied.add(act.id)
            if act.revision_key:
                self.applied.add(act.revision_key)
                self.records[act.id] = act.to_plain()
            self.ex._last_market_save = float("-inf")  # save the adjusted market with the accounts
        for a in self.ex.accounts.values():
            self._pay_dividends(a, day, now)

    def _already_applied(self, act: CorporateAction, now: float) -> bool:
        """True when this action (or an earlier record of the same event) was applied. A changed record
        is not applied a second time; the difference is logged once."""
        known = self.records.get(act.id)
        if act.id not in self.applied and not (act.revision_key and act.revision_key in self.applied):
            return False
        if known is None and act.revision_key:
            known = next((r for r in self.records.values() if r.get("revision_key") == act.revision_key), None)
        if known is not None:
            before = CorporateAction.from_plain(known)
            mark = f"amended:{act.id}:{act.to_plain()['amount']}:{act.new}:{act.old}:{act.pay_date}"
            if not before.same_terms(act) and mark not in self.applied:
                self.applied.add(mark)
                self._event(None, "corporate_action_amended",
                            f"The data provider changed the {before.describe()} (ex-date {before.ex_date.isoformat()}) "
                            f"after it was applied; it now reads {act.describe()}"
                            + (f", paid {act.pay_date.isoformat()}" if act.pay_date else "")
                            + ". The original terms were kept; nothing was applied twice.")
        return True

    def _unsupported(self, day: date, notices: list, now: float) -> None:
        """Corporate actions PaperDesk cannot apply: shown on the affected positions and logged, never silent."""
        iso = day.isoformat()
        for n in notices:
            key = f"notice:{n['id']}"
            if key in self.applied or (n.get("date") or iso) > iso:
                continue
            self.applied.add(key)
            sym = n["symbol"]
            row = {"id": n["id"], "type": n.get("type"), "date": n.get("date"), "message": n["message"]}
            self.notices[sym] = (self.notices.get(sym, []) + [row])[-NOTICE_LIMIT:]
            for a in self.ex.accounts.values():
                held = any(s == sym or ((c := parse(s)) is not None and c.underlying == sym) for s in a.lots)
                ordered = any(o.account_id == a.id and (o.symbol == sym or ((oc := parse(o.symbol)) is not None and oc.underlying == sym))
                              for o in self.ex.engine.active_orders())
                if held or ordered:
                    self._log(a, now, "corporate_action_unsupported", n["message"])

    def position_notices(self, symbol: str) -> list:
        """Notices for a position: its own symbol's, and for an option its underlying's."""
        c = parse(symbol)
        return list(self.notices.get(symbol, [])) + (list(self.notices.get(c.underlying, [])) if c else [])

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
            # A symbol with no price accrues nothing that day (it cannot be valued).
            mv = sum((-q * self.ex.mark(sym) for sym, q in shorts.items()), ZERO)
            if mv > 0:
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

    def _adjust_orders_for_dividend(self, act: CorporateAction, now: float) -> None:
        """Lower buy limits, sell stops and sell trailing references by the dividend."""
        amt = act.amount
        lower = lambda v: max(v - amt, CENT)
        for o in self.ex.engine.active_orders(act.symbol):
            changed = []
            if o.side == m.BUY and o.type == m.LIMIT and o.limit_price is not None:
                o.limit_price = lower(o.limit_price)
                changed.append("limit")
            if o.side == m.SELL and o.type in (m.STOP, m.STOP_LIMIT) and o.stop_price is not None and not o.triggered:
                o.stop_price = lower(o.stop_price)
                changed.append("stop")
                if o.type == m.STOP_LIMIT and o.limit_price is not None:
                    o.limit_price = lower(o.limit_price)
                    changed.append("limit")
            if o.side == m.SELL and o.type == m.TRAILING_STOP and not o.triggered and o.trail_ref is not None:
                o.trail_ref = lower(o.trail_ref)
                if o.stop_price is not None:
                    o.stop_price = lower(o.stop_price)
                changed.append("trailing reference")
            if changed:
                o.reason = (f"{' and '.join(changed).capitalize()} price lowered by the {amt.normalize():f} dividend "
                            f"(ex-date {act.ex_date.isoformat()})")
                o.updated_at = now
                self.ex.events.append({"type": "order", "account_id": o.account_id, "order": o})
                acct = self.ex.accounts.get(o.account_id)
                if acct:
                    self._log(acct, now, "dividend_adjustment", f"Order {o.id} on {o.symbol}: {o.reason}")

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
        self.ex.events.append({"type": "fill", "account_id": a.id, "fill": f})
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
            # Without a price, cash in lieu uses the average cost and options settle at their cost (no gain or loss).
            price = q.last if q is not None and q.last > 0 else a.avg_cost(sym)
            marks = {}
            for s in a.lots:
                c = parse(s)
                if c is not None and c.underlying == sym:
                    mk = self.ex.mark(s) if q is not None and q.last > 0 else ZERO
                    marks[s] = mk if mk > 0 else abs(a.avg_cost(s))
            res = splits.split_stock(a, act, price, now)
            if res:
                self._event(a, "split", res["message"])
            for note in splits.split_options(a, act, marks, now, self.next_fill_id):
                if "fill" in note:
                    self.on_fill(a, note["fill"])
                    self.ex.events.append({"type": "fill", "account_id": a.id, "fill": note["fill"]})
                self._event(a, "split", note["message"])
        ratio = Decimal(act.old) / Decimal(act.new)
        for al in self.ex.alerts:
            if al.get("symbol") == sym and al.get("triggered_at") is None:
                old = D(al["price"])
                new = (old * ratio).quantize(FOUR)
                al["price"] = str(new.normalize() if new != new.to_integral_value() else new.quantize(Decimal(1)))
                al["note"] = f"Adjusted from {old.normalize():f} for the {act.describe()} on {act.ex_date.isoformat()}"
                self.ex.dirty = True

    # ---- fills -----------------------------------------------------------------------
    def on_fill(self, a, fill: Fill) -> None:
        if fill.kind not in SETTLING_KINDS:
            return
        day = self.days.today(fill.ts)
        txn = a.ledger.transactions[-1] if a.ledger.transactions else None
        cash = sum((v for acct, v in txn.postings if acct == CASH), ZERO) if txn and txn.ref == fill.id else ZERO
        if a.settings["settlement"] and cash > 0:
            a.life.unsettled.append([self.days.add(day, 1).isoformat(), cash, fill.id])
        if fill.kind == "trade":
            self._track_day_trade(a, fill, day.isoformat())

    def _track_day_trade(self, a, fill: Fill, iso: str) -> None:
        """Day trades: one per opening lot (opening order) that is closed on the day it opened."""
        life = a.life
        if life.opened_date != iso:
            life.opened, life.opened_date = {}, iso
        signed = fill.qty if fill.side == m.BUY else -fill.qty
        after = a.position(fill.symbol)
        before = after - signed
        closing = min(fill.qty, abs(before)) if before != 0 and (before > 0) != (signed > 0) else ZERO
        opening = fill.qty - closing
        lots = life.opened.get(fill.symbol, [])
        rest = closing
        for lot in lots:
            if rest <= 0:
                break
            take = min(rest, lot[1])
            lot[1] -= take
            rest -= take
            if take > 0 and not lot[2]:
                lot[2] = True
                life.day_trades.append([iso, fill.symbol, lot[0] or fill.order_id])
        lots = [l for l in lots if l[1] > 0]
        if opening > 0:
            if closing > 0:
                lots = []  # the position went through zero
            if lots and lots[-1][0] is not None and lots[-1][0] == fill.order_id:
                lots[-1][1] += opening  # another partial fill of the same opening order
            else:
                lots.append([fill.order_id, opening, False])
        if lots:
            life.opened[fill.symbol] = lots
        else:
            life.opened.pop(fill.symbol, None)

    # ---- pre-trade checks --------------------------------------------------------------
    def day_trade_count(self, a, now: Optional[float] = None) -> int:
        today = self.days.today(self.ex.now() if now is None else now)
        start = self.days.window_start(today, PDT_WINDOW).isoformat()
        return sum(1 for e in a.life.day_trades if e[0] >= start)

    def pdt_restricted(self, a, eq: Optional[Decimal] = None) -> bool:
        s = a.settings
        if a.type != "margin" or not s["pattern_day_trader"] or self.day_trade_count(a) < PDT_LIMIT:
            return False
        if eq is None:
            _, _, eq, _ = self._state(a)
        return eq < cfg.number(s, "pdt_min_equity")

    def _pdt_release_date(self, a) -> Optional[date]:
        """First business day on which the count falls below the limit again."""
        today = self.today()
        d = today
        for _ in range(PDT_WINDOW * 3):
            d = self.days.add(d, 1)
            start = self.days.window_start(d, PDT_WINDOW).isoformat()
            if sum(1 for e in a.life.day_trades if e[0] >= start) < PDT_LIMIT:
                return d
        return None

    def pre_trade(self, a, order: Order, worst) -> Optional[str]:
        """Reason to reject the order, or None. `worst` is the exchange's worst-case check; it is None
        for an order that only closes part of a position, and closing orders are never blocked here."""
        s = a.settings
        if worst is not None and self.pdt_restricted(a):
            _, _, eq, _ = self._state(a)
            n = self.day_trade_count(a)
            release = self._pdt_release_date(a)
            when = f" New opening orders are allowed again on {release.isoformat()}." if release else ""
            return (f"Pattern day trader rule: this account has made {n} day trades in the last {PDT_WINDOW} "
                    f"business days and its equity ({_fmt(eq)}) is below {_fmt(cfg.number(s, 'pdt_min_equity'))}, "
                    f"so it cannot open new positions that could become a {_ordinal(n + 1)} day trade. "
                    f"Closing orders are still allowed.{when}")
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
        pending = self.pending_dividends(a)
        out["pending_dividends"] = pending
        if pending:  # the price has dropped by the dividend; the cash follows on the pay date
            out["equity"] = out["equity"] + pending
            out["excess_liquidity"] = cents(out["equity"] - out["maintenance_requirement"])
            out["total_pnl"] = cents(out["equity"] - a.starting_cash)
            out["day_pnl"] = cents(out["equity"] - a.day_start_equity)
        pos = a.positions()
        missing = self._missing_prices(pos, self.ex._marks(pos)) if pos else []
        out["prices_unavailable"] = missing
        checks, liq_holds = self._holds(pos)
        out["margin_paused"] = checks + liq_holds
        stale = set()
        for held in pos:
            for sym in (held, c.underlying) if (c := parse(held)) else (held,):
                q = self.ex.quote(sym)
                if q is not None and q.stale:
                    stale.add(sym)
        out["stale_prices"] = sorted(stale)
        a.margin_call = (a.type == "margin" and not missing and not checks
                         and out["equity"] < out["maintenance_requirement"])
        out["margin_call"] = a.margin_call
        n = self.day_trade_count(a)
        pdt_on = a.type == "margin" and s["pattern_day_trader"]
        out["day_trades"] = n
        out["day_trades_left"] = (max(PDT_LIMIT - n, 0) if pdt_on and out["equity"] < cfg.number(s, "pdt_min_equity")
                                  else None)
        out["accrued_interest"] = max(cents(a.life.accrued_interest), ZERO) + ZERO
        out["accrued_borrow_fee"] = max(cents(a.life.accrued_borrow), ZERO) + ZERO
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
            missing = self._missing_prices(pos, marks)
            if missing:  # cannot value the account: no call is started, cleared or enforced
                if life.prices_missing != missing:
                    life.prices_missing = missing
                    self._event(a, "prices_unavailable", f"Margin checks paused: no price for {', '.join(missing)}.")
                continue
            life.prices_missing = []
            checks, liq_holds = self._holds(pos)
            if checks:  # a known split is not applied yet: the numbers would be wrong, so do not act on them
                key = "; ".join(checks)
                if self._paused.get(a.id) != key:
                    self._paused[a.id] = key
                    self._event(a, "margin_paused", f"Margin checks paused: {key}.")
                continue
            self._paused.pop(a.id, None)
            if pos and eq < maint:
                life.margin_call_ok_since = None
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
                    if liq_holds:
                        if not life.liquidation_paused:
                            life.liquidation_paused = True
                            self._log(a, now, "liquidation", "Margin call liquidation paused: "
                                      + "; ".join(liq_holds) + ". Close or adjust the position yourself if needed.")
                    else:
                        self.liquidate(a, now)
            elif life.margin_call_since is not None:
                if not pos or eq >= maint * (1 + CLEAR_BUFFER):
                    self._clear_call(a, now, eq, maint)
                elif life.margin_call_ok_since is None:
                    life.margin_call_ok_since = now
                elif now - life.margin_call_ok_since >= CLEAR_SECONDS:
                    self._clear_call(a, now, eq, maint)

    def _clear_call(self, a, now, eq, maint) -> None:
        a.life.margin_call_since, a.life.margin_call_deficit = None, ZERO
        a.life.margin_call_ok_since, a.life.liquidation_paused = None, False
        self._log(a, now, "margin_call", f"Margin call met: equity {_fmt(eq)}, maintenance requirement {_fmt(maint)}.")

    def _safe_qty(self, pos: dict, sym: str, q: Decimal, want: Decimal, before: dict) -> Decimal:
        """Largest close quantity up to `want` that leaves no short option less covered than before."""
        ok = lambda x: all(v <= before.get(k, ZERO) for k, v in uncovered(self._reduced(pos, sym, q, x)).items())
        if ok(want):
            return want
        lo, hi = 0, int(want)  # ok(lo) holds; search whole units
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if ok(Decimal(mid)):
                lo = mid
            else:
                hi = mid - 1
        return Decimal(lo)

    def liquidate(self, a, now: float, max_steps: int = 50) -> list:
        """Close positions until equity is back above maintenance. Returns the steps taken.

        Each step closes part of the position whose removal frees the most maintenance requirement,
        sized to cover the deficit including the spread. A step never raises the requirement and
        never leaves a short option uncovered that was covered before it: stock or long options that
        cover short options are only sold down to the cover, and when nothing else helps the covered
        short options are bought back first.
        """
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
            if not pos or eq >= maint or self._missing_prices(pos, marks):
                break
            deficit = maint - eq
            before_unc = uncovered(pos)
            best = fallback = None
            for sym, q in pos.items():
                quote = ex.quote(sym)
                if quote is None or quote.stale or (quote.bid if q > 0 else quote.ask) <= 0:
                    continue  # nothing to sell into (or buy from), or only a stale quote
                short_option = q < 0 and parse(sym) is not None
                without = {k: v for k, v in pos.items() if k != sym}
                marginal = maint - margin.requirement(without, marks, maintenance=True)
                mult = multiplier(sym)
                if marginal > 0:
                    slip = max((marks[sym] - quote.bid) if q > 0 else (quote.ask - marks[sym]), ZERO) * mult
                    per_unit = marginal / abs(q) - slip
                    want = abs(q) if per_unit <= 0 else (deficit / per_unit).to_integral_value(ROUND_CEILING)
                    want = min(max(want, Decimal(1)), abs(q))
                    qty = self._safe_qty(pos, sym, q, want, before_unc)
                    if qty > 0:
                        req_after = margin.requirement(self._reduced(pos, sym, q, qty), marks, maintenance=True)
                        if req_after >= maint and qty < abs(q):  # a partial close frees nothing: try it all
                            full = self._safe_qty(pos, sym, q, abs(q), before_unc)
                            r = margin.requirement(self._reduced(pos, sym, q, full), marks, maintenance=True)
                            if r < req_after:
                                qty, req_after = full, r
                        if req_after < maint and (best is None or marginal > best[0]):
                            best = (marginal, sym, q, qty)
                            continue
                if short_option:  # buying back a short option never uncovers anything
                    req_after = margin.requirement(without, marks, maintenance=True)
                    if req_after <= maint and (fallback is None or req_after < fallback[0]):
                        fallback = (req_after, sym, q, abs(q))
            choice = best or fallback
            if choice is None:
                pair = self._pair_to_close(pos, marks, eq, maint)
                if pair is not None:  # a spread or calendar whose legs only help when closed together
                    choice = pair
            if choice is None:
                if not a.life.liquidation_paused:
                    a.life.liquidation_paused = True
                    self._log(a, now, "liquidation", "Margin call liquidation paused: no position can be closed to "
                                                     "reduce the requirement right now (no usable quote).")
                break
            _, sym, q, qty = choice
            if isinstance(q, list):  # (requirement, underlying, legs, units): close a covered pair as one order
                side = "close"  # buys back the short leg and sells the long leg
                o = Order(id=f"O{next(ex._order_seq):08d}", account_id=a.id, symbol=sym, side=m.BUY, qty=qty,
                          type=m.MULTILEG, tif=m.DAY, role="liquidation", legs=q)
                ex._handle(ex.engine.submit_multileg(o, ex._leg_quotes(q), now, ex.session(now)))
                sym = " / ".join(l["symbol"] for l in q)
            else:
                side = m.SELL if q > 0 else m.BUY
                o = Order(id=f"O{next(ex._order_seq):08d}", account_id=a.id, symbol=sym, side=side, qty=qty,
                          type=m.MARKET, tif=m.DAY, role="liquidation")
                ex._handle(ex.engine.submit(o, ex.quote(sym), now, ex.session(now)))
            a.life.liquidation_paused = False
            _, _, eq_after, maint_after = self._state(a)
            steps.append({"symbol": sym, "side": side, "qty": qty, "requirement_before": maint,
                          "requirement_after": maint_after, "equity_before": eq, "equity_after": eq_after,
                          "order_id": o.id, "status": o.status, "positions": a.positions()})
            self._log(a, now, "liquidation",
                      f"Margin call liquidation: {side} {qty.normalize():f} {sym} at market (order {o.id}, {o.status}); "
                      f"equity {_fmt(eq)} -> {_fmt(eq_after)}, maintenance requirement {_fmt(maint)} -> "
                      f"{_fmt(maint_after)}.")
            if o.active:
                ex._handle(ex.engine.cancel(o.id, now, "Unfilled part of a liquidation order; retried next tick"))
                break
        return steps

    def _pair_to_close(self, pos: dict, marks: dict, eq: Decimal, maint: Decimal):
        """A short option closed together with what covers it, as one all-or-none multi-leg order.

        Used when no single position can be closed: with strategy margin a long option needs its full
        value, so neither leg of a spread or calendar frees requirement alone, and the shares behind a
        covered call cannot be sold while the call is open. The cover is a long option of the same
        kind expiring no earlier, or 100 shares per contract (long for calls, short for puts). Returns
        (requirement after, underlying, legs, units) for the smallest close that meets the call, else
        the one leaving the lowest requirement below the current one, or None.

        The cover taken away must not be what another short option relies on: a pair is only
        considered if no short option is less covered after it than before (the shares behind a
        later-dated call cannot be used to close an earlier one that long calls already cover)."""
        ex = self.ex
        before_unc = uncovered(pos)
        usable = lambda sym, side: (q := ex.quote(sym)) is not None and not q.stale and \
            (q.ask if side == m.BUY else q.bid) > 0
        best = None
        for s_sym, s_q in pos.items():
            sc = parse(s_sym)
            if sc is None or s_q >= 0 or not usable(s_sym, m.BUY):
                continue
            covers = [(l_sym, l_q, 1) for l_sym, l_q in pos.items()
                      if (lc := parse(l_sym)) is not None and l_q > 0 and lc.underlying == sc.underlying
                      and lc.kind == sc.kind and lc.expiry >= sc.expiry]
            stock = pos.get(sc.underlying, ZERO)
            if (stock >= 100 and sc.kind == "C") or (stock <= -100 and sc.kind == "P"):
                covers.append((sc.underlying, stock, 100))
            for c_sym, c_q, ratio in covers:
                c_side = m.SELL if c_q > 0 else m.BUY
                if not usable(c_sym, c_side):
                    continue
                legs = [{"symbol": s_sym, "side": m.BUY, "ratio": 1}, {"symbol": c_sym, "side": c_side, "ratio": ratio}]
                closed = lambda n: self._reduced(self._reduced(pos, s_sym, s_q, Decimal(n)), c_sym, c_q, Decimal(n * ratio))
                top = int(min(-s_q, abs(c_q) // ratio))
                while top > 0 and any(v > before_unc.get(k, ZERO) for k, v in uncovered(closed(top)).items()):
                    top -= 1  # that many units would take cover another short option relies on
                for n in range(1, top + 1):
                    after = closed(n)
                    if any(v > before_unc.get(k, ZERO) for k, v in uncovered(after).items()):
                        continue
                    req = margin.requirement(after, marks, maintenance=True)
                    q_s, q_c = ex.quote(s_sym), ex.quote(c_sym)
                    c_slip = max((marks[c_sym] - q_c.bid) if c_q > 0 else (q_c.ask - marks[c_sym]), ZERO)
                    slip = n * (max(q_s.ask - marks[s_sym], ZERO) * 100 + c_slip * ratio * multiplier(c_sym))
                    meets = eq - slip >= req
                    if req < maint and (meets or n == top):
                        cand = (not meets, req, sc.underlying, legs, Decimal(n))
                        if best is None or cand[:2] < best[:2]:
                            best = cand
                        break
        return best[1:] if best else None

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
        out = {"last_date": self.last_date.isoformat() if self.last_date else None,
               "applied": sorted(self.applied), "seq": n,
               "splits": [self.split_records[k] for k in sorted(self.split_records)]}
        if self.since is not None:
            out["since"] = self.since.isoformat()
        if self.records:
            out["records"] = [self.records[k] for k in sorted(self.records)]
        if self.notices:
            out["notices"] = self.notices
        return out

    def restore(self, d) -> None:
        if not isinstance(d, dict):
            return
        self.last_date = date.fromisoformat(d["last_date"]) if d.get("last_date") else None
        self.applied = set(d.get("applied", []))
        self._seq = count(int(d.get("seq", 1)))
        self.split_records = {r["id"]: r for r in d.get("splits", [])}
        self.records = {r["id"]: r for r in d.get("records", [])}
        self.notices = {k: list(v) for k, v in (d.get("notices") or {}).items()}
        # State written before `since` existed: late actions never reach back before the saved day.
        self.since = date.fromisoformat(d["since"]) if d.get("since") else self.last_date
        self.repair_feed()

    def repair_feed(self) -> None:
        """Re-apply to the feed any split the accounts already reflect but the feed's saved market lacks
        (market.json missing, rejected or older than state.json)."""
        acts = sorted((CorporateAction.from_plain(r) for r in self.split_records.values()), key=lambda x: x.ex_date)
        for act in acts:
            if act.id not in self.ex.feed.applied_actions:
                self.ex.feed.apply_corporate_action(act)
                self.ex._last_market_save = float("-inf")

    # ---- reporting ------------------------------------------------------------------------------
    def upcoming(self, days: int = 120, symbol: Optional[str] = None) -> list:
        today = self.today()
        try:
            acts = self.ex.feed.corporate_actions(today - timedelta(days=30), today + timedelta(days=days))
        except ActionsPending as e:  # show what the feed has so far
            acts = e.known
        except ActionsUnavailable:
            acts = []
        return sorted((x for x in acts if symbol is None or x.symbol == symbol), key=lambda x: (x.ex_date, x.symbol))
