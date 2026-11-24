"""The simulated broker: wires market data, matching, accounts, risk and options."""
import json
import os
import tempfile
from decimal import Decimal
from itertools import count
from typing import Optional

from .core import calendar as cal
from .core import models as m
from .core.clock import Clock
from .core.models import Fill, Order, Quote, from_plain, to_plain
from .core.money import CENT, ZERO, D, cents, parse as parse_num
from .ledger.account import Account
from .marketdata.feed import MarketDataProvider
from .matching.engine import FillConfig, MatchingEngine
from .options import chain as optchain
from .options.symbology import describe, multiplier, parse as parse_option
from .risk import margin

OPTION_FEE = Decimal("0.65")  # per contract
STOCK_FEE = ZERO  # per share
MAX_QTY = Decimal(1_000_000)


class OrderRejected(ValueError):
    pass


class Exchange:
    def __init__(self, feed: MarketDataProvider, clock: Optional[Clock] = None, data_dir: Optional[str] = None,
                 session_mode: str = "always", fill_config: Optional[FillConfig] = None):
        self.feed = feed
        self.clock = clock or Clock()
        self.data_dir = data_dir
        self.session_mode = session_mode  # 'always' or 'us'
        self.engine = MatchingEngine(fill_config)
        self.accounts: dict[str, Account] = {}
        self.watchlists: dict[str, list] = {}
        self.alerts: list[dict] = []
        self.events: list[dict] = []  # drained by the API layer after each tick
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
        return optchain.option_quote(c, float(u.last), self.feed.base_vol(c.underlying), self.now())

    def mark(self, symbol: str) -> Decimal:
        q = self.quote(symbol)
        if q is None:
            return ZERO
        return q.last if self.is_stock(symbol) else cents(q.mid)

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
        starting_cash = D(starting_cash)
        if starting_cash <= 0 or starting_cash > Decimal("1000000000"):
            raise ValueError("starting cash must be between 0 and 1,000,000,000")
        name = (name or "").strip()[:40] or "Paper account"
        acct = Account(f"A{next(self._acct_seq):04d}", name, type, starting_cash, self.now())
        self.accounts[acct.id] = acct
        self.dirty = True
        return acct

    def reset_account(self, account_id: str, starting_cash=None) -> Account:
        old = self.account(account_id)
        for o in self.engine.active_orders():
            if o.account_id == account_id:
                self.engine.cancel(o.id, self.now(), "account reset")
        self.engine.orders = {k: o for k, o in self.engine.orders.items() if o.account_id != account_id}
        new = Account(old.id, old.name, old.type, D(starting_cash) if starting_cash is not None else old.starting_cash, self.now())
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
            raise KeyError(f"no such account {account_id}")

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
        return {"id": a.id, "name": a.name, "type": a.type, "starting_cash": a.starting_cash, "cash": a.cash,
                "equity": eq, "long_market_value": cents(long_mv), "short_market_value": cents(short_mv),
                "initial_requirement": init, "maintenance_requirement": maint,
                "excess_liquidity": cents(eq - maint), "buying_power": bp["stock"], "option_buying_power": bp["option"],
                "realized_pnl": a.realized, "unrealized_pnl": unrealized, "fees": a.fees_paid,
                "total_pnl": cents(eq - a.starting_cash), "day_pnl": cents(eq - a.day_start_equity),
                "margin_call": a.margin_call}

    def positions(self, account_id: str) -> list:
        a = self.account(account_id)
        rows = []
        for sym, qty in sorted(a.positions().items()):
            mult = multiplier(sym)
            mark = self.mark(sym)
            cost = a.cost_basis(sym)
            mv = cents(qty * mark * mult)
            row = {"symbol": sym, "description": describe(sym), "qty": qty, "avg_cost": a.avg_cost(sym), "mark": mark,
                   "market_value": mv, "cost_basis": cost, "unrealized_pnl": mv - cost,
                   "unrealized_pct": float((mv - cost) / abs(cost) * 100) if cost else 0.0,
                   "multiplier": mult, "kind": "option" if mult == 100 else "stock", "day_pnl": None}
            c = parse_option(sym)
            if c:
                u = self.feed.quote(c.underlying)
                snap = optchain.option_snapshot(c, float(u.last), self.feed.base_vol(c.underlying), self.now())
                row.update(delta=snap["delta"] * float(qty) * 100, theta=snap["theta"] * float(qty) * 100,
                           underlying=c.underlying, expiry=c.expiry.isoformat())
            else:
                q = self.feed.quote(sym)
                if q and q.prev_close:
                    row["day_pnl"] = cents(qty * (q.last - q.prev_close))
                row["delta"] = float(qty)
            rows.append(row)
        return rows

    def orders(self, account_id: str, status: str = "all") -> list:
        self.account(account_id)
        rows = [o for o in self.engine.orders.values() if o.account_id == account_id]
        if status == "open":
            rows = [o for o in rows if o.active]
        elif status == "closed":
            rows = [o for o in rows if not o.active]
        return sorted(rows, key=lambda o: (o.created_at, o.id), reverse=True)

    def performance(self, account_id: str) -> dict:
        a = self.account(account_id)
        pnls = [D(c["pnl"]) for c in a.closed]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        gross_win, gross_loss = sum(wins, ZERO), -sum(losses, ZERO)
        peak, max_dd = a.starting_cash, ZERO
        for _, e in a.equity_history:
            e = D(e)
            peak = max(peak, e)
            if peak > 0:
                max_dd = max(max_dd, (peak - e) / peak)
        s = self.summary(account_id)
        return {"closed_trades": len(pnls), "wins": len(wins), "losses": len(losses),
                "win_rate": float(len(wins) / len(pnls) * 100) if pnls else None,
                "average_win": cents(gross_win / len(wins)) if wins else None,
                "average_loss": cents(-gross_loss / len(losses)) if losses else None,
                "profit_factor": float(gross_win / gross_loss) if gross_loss else None,
                "largest_win": max(wins) if wins else None, "largest_loss": min(losses) if losses else None,
                "realized_pnl": a.realized, "fees": a.fees_paid,
                "total_return_pct": float((s["equity"] - a.starting_cash) / a.starting_cash * 100),
                "max_drawdown_pct": float(max_dd * 100), "equity_curve": a.equity_history}

    # ---- order entry -----------------------------------------------------
    def _validate(self, account: Account, req: dict) -> list:
        """Turn an API request into Order objects (entry order first). Raises OrderRejected."""
        try:
            symbol = str(req.get("symbol", "")).upper().strip()
            side, typ = req.get("side"), req.get("type", m.MARKET)
            tif = req.get("tif", m.DAY)
            qty = parse_num(req.get("qty"))
            num = lambda k: parse_num(req[k]) if req.get(k) not in (None, "") else None
            limit, stop = num("limit_price"), num("stop_price")
            trail_amt, trail_pct = num("trail_amount"), num("trail_percent")
            take_profit, stop_loss = num("take_profit"), num("stop_loss")
        except (ValueError, ArithmeticError) as e:
            raise OrderRejected(f"invalid number in order: {e}")
        contract = parse_option(symbol)
        if not self.is_stock(symbol):
            if contract is None or not self.is_stock(contract.underlying):
                raise OrderRejected(f"unknown symbol {symbol or '(blank)'}")
            if contract.expiry_ts <= self.now():
                raise OrderRejected("this option has expired")
            if contract.strike <= 0 or (contract.strike * 2) % 1 != 0:
                raise OrderRejected("strike is not listed")
            if contract.expiry not in optchain.expirations(cal.ny_date(self.now())) and account.position(symbol) == 0:
                raise OrderRejected("expiry is not listed")
        if side not in (m.BUY, m.SELL):
            raise OrderRejected("side must be buy or sell")
        if typ not in m.ORDER_TYPES:
            raise OrderRejected(f"unknown order type {typ}")
        if tif not in m.TIFS:
            raise OrderRejected(f"unknown time in force {tif}")
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
        if typ in (m.LIMIT, m.STOP_LIMIT) and limit is None:
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
        if typ != m.LIMIT and typ != m.STOP_LIMIT:
            limit = None
        if typ not in (m.STOP, m.STOP_LIMIT):
            stop = None
        ext = bool(req.get("extended_hours")) and typ == m.LIMIT
        oid = lambda: f"O{next(self._order_seq):08d}"
        client_id = str(req["client_id"])[:64] if req.get("client_id") else None
        entry = Order(id=oid(), account_id=account.id, symbol=symbol, side=side, qty=qty, type=typ, limit_price=limit,
                      stop_price=stop, trail_amount=trail_amt if typ == m.TRAILING_STOP else None,
                      trail_percent=trail_pct if typ == m.TRAILING_STOP else None, tif=tif, extended_hours=ext,
                      client_id=client_id)
        orders = [entry]
        if take_profit is not None or stop_loss is not None:
            if typ not in (m.MARKET, m.LIMIT) or tif in (m.IOC, m.FOK) or qty != qty.to_integral_value():
                raise OrderRejected("bracket orders need a whole-share market or limit entry (day or GTC)")
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
        return orders

    def _estimate_price(self, o: Order) -> Decimal:
        q = self.quote(o.symbol)
        if q is None or q.ask <= 0:
            raise OrderRejected(f"no quote available for {o.symbol}")
        if o.limit_price is not None:
            if o.side == m.BUY:
                return o.limit_price
            return max(o.limit_price, q.bid)
        px = self.engine._walk(q, o.side, o.remaining)
        if o.stop_price is not None:
            return max(px, o.stop_price) if o.side == m.BUY else min(px, o.stop_price)
        return px

    def _fee(self, symbol: str, qty: Decimal) -> Decimal:
        return cents(qty * (OPTION_FEE if multiplier(symbol) == 100 else STOCK_FEE))

    class _Worst:
        def __init__(self, ok, reason, excess, requirement, cash_after):
            self.ok, self.reason, self.excess, self.requirement, self.cash_after = ok, reason, excess, requirement, cash_after

    def _worst_case(self, account: Account, new_orders: list) -> "_Worst":
        """Check the account under two scenarios: every open buy fills, every open sell fills.

        One-cancels-other groups count once. Held bracket children are ignored because
        they only ever reduce the position their entry order opened.
        """
        working = [o for o in self.engine.orders.values()
                   if o.account_id == account.id and o.status in (m.WORKING, m.PARTIAL)] + list(new_orders)
        base = account.positions()
        result = None
        for side in (None, m.BUY, m.SELL):
            pos, cash, seen = dict(base), account.cash, {}
            if side is not None:
                for o in working:
                    if o.side != side:
                        continue
                    qty = o.remaining
                    if o.oco_group:  # only the largest leg of a group can fill
                        prior = seen.get(o.oco_group, ZERO)
                        if qty <= prior:
                            continue
                        seen[o.oco_group], qty = qty, qty - prior
                    price = self._estimate_price(o)
                    sign = 1 if side == m.BUY else -1
                    pos[o.symbol] = pos.get(o.symbol, ZERO) + sign * qty
                    cash -= sign * cents(qty * price * multiplier(o.symbol)) + self._fee(o.symbol, qty)
            pos = {s: q for s, q in pos.items() if q != 0}
            ev = margin.evaluate(account.type, cash, pos, self._marks(pos))
            w = Exchange._Worst(ev.ok, ev.reason, ev.excess, ev.requirement, cash)
            if result is None or (result.ok and not w.ok) or (w.ok == result.ok and (
                    w.excess < result.excess if account.type == "margin"
                    else w.cash_after - w.requirement < result.cash_after - result.requirement)):
                result = w
        return result

    def _reduces_risk(self, account: Account, o: Order) -> bool:
        """True when the order only closes part of an existing position."""
        pos = account.position(o.symbol)
        if pos == 0 or (pos > 0) == (o.side == m.BUY):
            return False
        same_side = sum((x.remaining for x in self.engine.orders.values()
                         if x.account_id == account.id and x.symbol == o.symbol and x.side == o.side
                         and x.status in (m.WORKING, m.PARTIAL)), ZERO)
        return same_side + o.qty <= abs(pos)

    def preview(self, account_id: str, req: dict) -> dict:
        a = self.account(account_id)
        orders = self._validate(a, req)
        entry = orders[0]
        price = self._estimate_price(entry)
        mult = multiplier(entry.symbol)
        notional = cents(entry.qty * price * mult)
        fee = self._fee(entry.symbol, entry.qty)
        before = self.summary(account_id)
        closing = self._reduces_risk(a, entry)
        worst = self._worst_case(a, [entry])
        ok = closing or worst.ok
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
        if self.session() != "regular" and not entry.extended_hours:
            warnings.append("The market is closed; the order will wait for the regular session.")
        return {"ok": ok, "reason": None if ok else worst.reason, "symbol": entry.symbol,
                "description": describe(entry.symbol), "side": entry.side, "qty": entry.qty,
                "estimated_price": price, "estimated_notional": notional, "estimated_fees": fee,
                "estimated_total": notional + fee if entry.side == m.BUY else notional - fee,
                "position_before": pos, "position_after": after_pos, "buying_power_before": before["buying_power"],
                "buying_power_after": bp_after if not closing else None, "closing": closing, "warnings": warnings}

    def submit(self, account_id: str, req: dict) -> list:
        a = self.account(account_id)
        if req.get("client_id"):
            for o in self.engine.orders.values():
                if o.account_id == account_id and o.client_id == str(req["client_id"])[:64]:
                    return [o] + [c for c in self.engine.orders.values() if c.parent_id == o.id]
        orders = self._validate(a, req)
        entry = orders[0]
        if not self._reduces_risk(a, entry):
            worst = self._worst_case(a, [entry])
            if not worst.ok:
                raise OrderRejected(worst.reason or "order rejected by risk check")
        now, q = self.now(), self.quote(entry.symbol)
        if len(orders) > 1:
            events = self.engine.submit_bracket(entry, orders[1:], q, now, self.session(now))
        else:
            events = self.engine.submit(entry, q, now, self.session(now))
        self._handle(events)
        return orders

    def cancel(self, account_id: str, order_id: str) -> Order:
        o = self.engine.orders.get(order_id)
        if o is None or o.account_id != account_id:
            raise KeyError("no such order")
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
            raise KeyError("no such order")
        a = self.account(account_id)
        try:
            num = lambda k: parse_num(req[k]) if req.get(k) not in (None, "") else None
            qty, limit, stop = num("qty"), num("limit_price"), num("stop_price")
        except (ValueError, ArithmeticError) as e:
            raise ValueError(f"invalid number: {e}")
        for v in (qty, limit, stop):
            if v is not None and v <= 0:
                raise ValueError("values must be positive")
        if qty is not None and (qty > MAX_QTY or qty != qty.quantize(Decimal("0.0001"))):
            raise ValueError("invalid quantity")
        old = (o.qty, o.limit_price, o.stop_price)
        self._handle(self.engine.replace(order_id, self.now(), qty, limit, stop))
        if o.status != m.HELD and not self._worst_case(a, []).ok and not self._reduces_risk_after(a, o):
            o.qty, o.limit_price, o.stop_price = old
            raise OrderRejected("insufficient buying power for the change")
        return o

    def _reduces_risk_after(self, account: Account, o: Order) -> bool:
        pos = account.position(o.symbol)
        return pos != 0 and (pos > 0) != (o.side == m.BUY) and o.remaining <= abs(pos)

    # ---- event handling ----------------------------------------------------
    def _handle(self, events: list) -> None:
        for kind, obj in events:
            if kind == "fill":
                acct = self.accounts.get(obj.account_id)
                if acct is None:
                    continue
                obj.fee = self._fee(obj.symbol, obj.qty)
                acct.apply_fill(obj, multiplier(obj.symbol))
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
        self._roll_sessions(now, sess)
        quotes = self.feed.step(now)
        active_symbols = {o.symbol for o in self.engine.orders.values() if o.status in (m.WORKING, m.PARTIAL)}
        for q in quotes:
            if q.symbol in active_symbols:
                self._handle(self.engine.on_quote(q, now, sess))
        changed = {q.symbol for q in quotes}
        for sym in active_symbols:
            c = parse_option(sym)
            if c and c.underlying in changed and c.expiry_ts > now:
                self._handle(self.engine.on_quote(self.quote(sym), now, sess))
        self._process_expirations(now)
        self._check_alerts(quotes, now)
        if now - self._last_equity_sample >= 60:
            self._last_equity_sample = now
            for a in self.accounts.values():
                eq = self.summary(a.id)["equity"]
                a.equity_history.append([int(now), str(eq)])
                if len(a.equity_history) > 6000:
                    a.equity_history = a.equity_history[::2]
            self.dirty = True
        return quotes

    def _roll_sessions(self, now: float, sess: str) -> None:
        day = int(now // 86400)
        if self._last_day is not None and day != self._last_day:
            for a in self.accounts.values():
                a.day_start_equity = self.summary(a.id)["equity"]
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

    def _process_expirations(self, now: float) -> None:
        for o in self.engine.active_orders():
            c = parse_option(o.symbol)
            if c is not None and c.expiry_ts <= now:
                self._handle(self.engine.cancel(o.id, now, "option expired", status=m.EXPIRED))
        for a in self.accounts.values():
            for sym, qty in list(a.positions().items()):
                c = parse_option(sym)
                if c is None or c.expiry_ts > now:
                    continue
                u = self.feed.quote(c.underlying)
                spot = u.last if u else ZERO
                itm = (spot - c.strike) if c.kind == "C" else (c.strike - spot)
                long = qty > 0
                n = abs(qty)

                def book(symbol, side, q, price, kind):
                    f = Fill(id=f"X{next(self._expiry_seq):08d}", order_id=None, account_id=a.id, symbol=symbol,
                             side=side, qty=q, price=price, ts=now, kind=kind)
                    a.apply_fill(f, multiplier(symbol))
                    self.events.append({"type": "fill", "account_id": a.id, "fill": f})

                if itm >= CENT:
                    kind = "exercise" if long else "assignment"
                    book(sym, m.SELL if long else m.BUY, n, ZERO, kind)
                    # A call holder buys the stock; a put holder sells it. The writer takes the other side.
                    buys_stock = (c.kind == "C") == long
                    book(c.underlying, m.BUY if buys_stock else m.SELL, n * 100, c.strike, kind)
                else:
                    book(sym, m.SELL if long else m.BUY, n, ZERO, "expiration")
                self.dirty = True

    # ---- alerts and watchlists ----------------------------------------------
    def add_alert(self, symbol: str, op: str, price) -> dict:
        symbol = symbol.upper().strip()
        if not self.is_stock(symbol):
            raise ValueError(f"unknown symbol {symbol}")
        if op not in ("above", "below"):
            raise ValueError("alert must be 'above' or 'below'")
        price = parse_num(price)
        if price <= 0:
            raise ValueError("price must be positive")
        alert = {"id": f"AL{next(self._alert_seq):05d}", "symbol": symbol, "op": op, "price": str(price),
                 "created_at": self.now(), "triggered_at": None}
        self.alerts.append(alert)
        self.dirty = True
        return alert

    def remove_alert(self, alert_id: str) -> None:
        self.alerts = [a for a in self.alerts if a["id"] != alert_id]
        self.dirty = True

    def _check_alerts(self, quotes: list, now: float) -> None:
        if not self.alerts:
            return
        last = {q.symbol: q.last for q in quotes}
        for al in self.alerts:
            if al["triggered_at"] is None and al["symbol"] in last:
                px, level = last[al["symbol"]], D(al["price"])
                if (al["op"] == "above" and px >= level) or (al["op"] == "below" and px <= level):
                    al["triggered_at"] = now
                    self.events.append({"type": "alert", "alert": dict(al), "last": px})
                    self.dirty = True

    def drain_events(self) -> list:
        out, self.events = self.events, []
        return out

    # ---- persistence -----------------------------------------------------------
    def _path(self, name: str) -> str:
        return os.path.join(self.data_dir, name)

    @staticmethod
    def _write(path: str, data: dict) -> None:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)

    def save(self, force: bool = False) -> None:
        if not self.data_dir:
            return
        os.makedirs(self.data_dir, exist_ok=True)
        if self.dirty or force:
            self.dirty = False
            self._write(self._path("state.json"), {
                "version": 1,
                "accounts": [a.to_plain() for a in self.accounts.values()],
                "orders": [to_plain(o) for o in self.engine.orders.values()],
                "watchlists": self.watchlists, "alerts": self.alerts,
                "seq": self._peek_sequences()})
        now = self.now()
        if force or now - self._last_market_save >= 60:
            self._last_market_save = now
            snap = self.feed.snapshot()
            if snap:
                self._write(self._path("market.json"), snap)

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
            with open(self._path("market.json")) as f:
                self.feed.restore(json.load(f))
        except (OSError, ValueError):
            pass
        try:
            with open(self._path("state.json")) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return False
        self.accounts = {a["id"]: Account.from_plain(a) for a in data["accounts"]}
        seq = data["seq"]
        self.engine.restore([from_plain(Order, o) for o in data["orders"]], seq["fill"])
        self.watchlists, self.alerts = data.get("watchlists", {}), data.get("alerts", [])
        self._order_seq, self._acct_seq = count(seq["order"]), count(seq["account"])
        self._alert_seq, self._expiry_seq = count(seq["alert"]), count(seq["expiry"])
        return True
