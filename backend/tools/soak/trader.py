"""The seeded random trader: turns a random generator and the current market into concrete commands.

Every command is a small JSON-able dict, so a run is fully described by its seed and the list of
commands, and a failing run can be replayed and minimised without the generator (see sim.replay).
The trader only reads state; the simulator applies the commands.
"""
import random
from datetime import timedelta
from decimal import Decimal

from app.core import calendar as cal
from app.core import models as m
from app.core.money import CENT, cents
from app.lifecycle import settings as cfg
from app.marketdata.replay.samples import SAMPLES
from app.options import chain as optchain
from app.options.symbology import build, parse as parse_option

STOCK_TYPES = (m.MARKET, m.LIMIT, m.STOP, m.STOP_LIMIT, m.TRAILING_STOP)
TIFS = (m.DAY, m.GTC, m.IOC, m.FOK, m.GTD)
AUCTIONS = m.AUCTION_TYPES

# Multi-leg templates: (name, [(kind or "S" for stock, strike offset in steps, side, ratio, far expiry?)])
TEMPLATES = [
    ("bull_call", [("C", 0, "buy", 1, 0), ("C", 1, "sell", 1, 0)]),
    ("bear_call", [("C", 0, "sell", 1, 0), ("C", 1, "buy", 1, 0)]),
    ("bull_put", [("P", 0, "sell", 1, 0), ("P", -1, "buy", 1, 0)]),
    ("bear_put", [("P", 0, "buy", 1, 0), ("P", -1, "sell", 1, 0)]),
    ("straddle", [("C", 0, "buy", 1, 0), ("P", 0, "buy", 1, 0)]),
    ("short_straddle", [("C", 0, "sell", 1, 0), ("P", 0, "sell", 1, 0)]),
    ("strangle", [("C", 1, "buy", 1, 0), ("P", -1, "buy", 1, 0)]),
    ("short_strangle", [("C", 1, "sell", 1, 0), ("P", -1, "sell", 1, 0)]),
    ("iron_condor", [("P", -2, "buy", 1, 0), ("P", -1, "sell", 1, 0), ("C", 1, "sell", 1, 0), ("C", 2, "buy", 1, 0)]),
    ("iron_butterfly", [("P", -1, "buy", 1, 0), ("P", 0, "sell", 1, 0), ("C", 0, "sell", 1, 0), ("C", 1, "buy", 1, 0)]),
    ("butterfly", [("C", -1, "buy", 1, 0), ("C", 0, "sell", 2, 0), ("C", 1, "buy", 1, 0)]),
    ("put_butterfly", [("P", -1, "buy", 1, 0), ("P", 0, "sell", 2, 0), ("P", 1, "buy", 1, 0)]),
    ("calendar", [("C", 0, "sell", 1, 0), ("C", 0, "buy", 1, 1)]),
    ("put_calendar", [("P", 0, "sell", 1, 0), ("P", 0, "buy", 1, 1)]),
    ("diagonal", [("C", 1, "sell", 1, 0), ("C", 0, "buy", 1, 1)]),
    ("covered_call", [("S", 0, "buy", 100, 0), ("C", 1, "sell", 1, 0)]),
    ("protective_put", [("S", 0, "buy", 100, 0), ("P", -1, "buy", 1, 0)]),
    ("collar", [("S", 0, "buy", 100, 0), ("P", -1, "buy", 1, 0), ("C", 1, "sell", 1, 0)]),
    ("ratio_spread", [("C", 0, "buy", 1, 0), ("C", 1, "sell", 2, 0)]),
    ("risk_reversal", [("P", -1, "sell", 1, 0), ("C", 1, "buy", 1, 0)]),
    ("short_put_stock", [("S", 0, "sell", 100, 0), ("P", -1, "sell", 1, 0)]),
]

# (op, weight)
OPS = [
    ("tick", 30), ("run", 7), ("stock", 18), ("option", 8), ("close", 11), ("multileg", 6), ("bracket", 4),
    ("oco", 3), ("oto", 3), ("auction", 3), ("preview", 3), ("replace", 4), ("cancel", 4), ("cancel_all", 0.4),
    ("settings", 1.0), ("lot_method", 0.5), ("shock", 2.0), ("offline", 0.3), ("reload", 0.25), ("reset", 0.12),
    ("replay", 0.06), ("lever", 1.5), ("squeeze", 1.0), ("create_account", 0.05), ("delete_account", 0.03), ("journal", 0.5), ("alert", 0.4),
]


def _px(v: Decimal) -> str:
    v = max(v, Decimal("0.01"))
    return str(cents(v) if v >= 1 else v.quantize(Decimal("0.0001")))


class Trader:
    def __init__(self, seed: int):
        self.rng = random.Random(f"soak-trader-{seed}")
        names, weights = zip(*OPS)
        self._names, self._cum = names, []
        total = 0.0
        for w in weights:
            total += w
            self._cum.append(total)
        self._total = total

    # ---- helpers ------------------------------------------------------------------------
    def _pick_op(self) -> str:
        x = self.rng.random() * self._total
        for name, c in zip(self._names, self._cum):
            if x < c:
                return name
        return self._names[-1]

    def _symbol(self, ex) -> str:
        syms = sorted(ex.feed.symbols())
        # a handful of favourites trade most, so positions build up and get closed
        fav = syms[:6]
        return self.rng.choice(fav if self.rng.random() < 0.6 else syms)

    def _account(self, ex) -> str:
        return self.rng.choice(sorted(ex.accounts))

    def _equity(self, ex, aid) -> Decimal:
        a = ex.account(aid)
        return max(a.cash, Decimal(1000))

    def _qty(self, ex, aid, price: Decimal, mult: int = 1) -> int:
        frac = self.rng.choice((0.002, 0.01, 0.03, 0.1, 0.3, 0.6, 1.5))
        n = int(float(self._equity(ex, aid)) * frac / max(float(price) * mult, 0.01))
        return max(1, min(n, 50000))

    def _gtd(self, ex) -> str:
        today = cal.ny_date(ex.now())
        d = today + timedelta(days=self.rng.choice((0, 1, 2, 5, 14, 40)))
        if ex.session_mode == "us":
            while not cal.is_trading_day(d):
                d += timedelta(days=1)
        return d.isoformat()

    def _contract(self, ex, underlying: str, far: int = 0, offset: int = 0, kind: str = "C", base_exp=None):
        q = ex.feed.quote(underlying)
        exps = optchain.expirations(cal.ny_date(ex.now()))
        exps = [e for e in exps if cal.close_ts(e) > ex.now()]
        if not exps or q is None:
            return None
        if base_exp is None:
            i = min(int(self.rng.expovariate(0.7)), len(exps) - 1)
            base_exp = exps[i]
        exp = base_exp
        if far:
            later = [e for e in exps if e > base_exp]
            if not later:
                return None
            exp = later[min(int(self.rng.expovariate(1.0)), len(later) - 1)]
        spot = float(q.last)
        step = optchain.strike_step(spot)
        atm = (Decimal(repr(spot)) / step).to_integral_value() * step
        strike = atm + step * offset
        if strike <= 0:
            return None
        return build(underlying, exp, kind, strike), base_exp

    def _single(self, ex, aid, symbol, side, qty, allow_fraction=True) -> dict:
        r = self.rng
        q = ex.quote(symbol)
        last = q.last if q else Decimal(10)
        typ = r.choice(STOCK_TYPES)
        tif = r.choice(TIFS)
        req = {"symbol": symbol, "side": side, "qty": qty, "type": typ, "tif": tif}
        away = Decimal(repr(r.choice((0.0, 0.001, 0.005, 0.02, 0.08)))) * r.choice((1, -1))
        if typ in (m.LIMIT, m.STOP_LIMIT):
            req["limit_price"] = _px(last * (1 + away))
        if typ in (m.STOP, m.STOP_LIMIT):
            sign = 1 if side == m.BUY else -1
            req["stop_price"] = _px(last * (1 + sign * abs(away) + sign * Decimal("0.001")))
        if typ == m.TRAILING_STOP:
            if r.random() < 0.5:
                req["trail_amount"] = _px(last * Decimal("0.01") * r.choice((1, 3, 10)))
            else:
                req["trail_percent"] = r.choice(("0.5", "2", "7.5"))
        if tif == m.GTD:
            req["good_till_date"] = self._gtd(ex)
        if typ == m.LIMIT and r.random() < 0.2:
            req["extended_hours"] = True
        if allow_fraction and parse_option(symbol) is None and r.random() < 0.08:
            req.update(qty=str(Decimal(qty) + Decimal(r.choice(("0.5", "0.25", "0.0001")))), type=r.choice((m.MARKET, m.LIMIT)),
                       tif=m.DAY)
            if req["type"] == m.LIMIT:
                req["limit_price"] = _px(last * (1 + away))
            req.pop("good_till_date", None)
            req.pop("stop_price", None)
            req.pop("trail_amount", None)
            req.pop("trail_percent", None)
        if r.random() < 0.03:
            req["client_id"] = f"c{r.randrange(40)}"
        return req

    # ---- generators ---------------------------------------------------------------------
    def next(self, sim) -> dict:
        ex = sim.ex
        op = self._pick_op()
        if not ex.accounts and op not in ("tick", "run", "create_account"):
            op = "create_account"
        gen = getattr(self, "_gen_" + op)
        cmd = gen(ex, sim)
        return cmd if cmd is not None else self._gen_tick(ex, sim)

    def _gen_tick(self, ex, sim):
        dt = self.rng.choice((1, 1, 2, 5, 15, 30, 60, 60, 120, 300, 900, 1800, 3600))
        return {"op": "tick", "dt": dt}

    def _gen_run(self, ex, sim):
        return {"op": "run", "dt": self.rng.choice((3600, 4 * 3600, 6.5 * 3600, 86400)),
                "every": self.rng.choice((300, 900, 1800))}

    def _gen_stock(self, ex, sim):
        aid = self._account(ex)
        sym = self._symbol(ex)
        q = ex.quote(sym)
        if q is None:
            return None
        side = self.rng.choice((m.BUY, m.BUY, m.SELL))
        return {"op": "submit", "acct": aid, "req": self._single(ex, aid, sym, side, self._qty(ex, aid, q.last))}

    def _gen_option(self, ex, sim):
        aid = self._account(ex)
        u = self._symbol(ex)
        got = self._contract(ex, u, offset=self.rng.randint(-3, 3), kind=self.rng.choice("CP"))
        if got is None:
            return None
        sym, _ = got
        side = self.rng.choice((m.BUY, m.SELL))
        req = self._single(ex, aid, sym, side, self.rng.choice((1, 1, 2, 5, 10, 30)), allow_fraction=False)
        if req["type"] in (m.STOP, m.STOP_LIMIT, m.TRAILING_STOP) and self.rng.random() < 0.7:
            req["type"] = m.LIMIT
            q = ex.quote(sym)
            req["limit_price"] = _px((q.mid if q else Decimal(1)) * Decimal(repr(self.rng.choice((0.9, 1.0, 1.1)))))
            req.pop("stop_price", None)
        return {"op": "submit", "acct": aid, "req": req}

    def _gen_close(self, ex, sim):
        held = [(aid, s, q) for aid in sorted(ex.accounts) for s, q in sorted(ex.account(aid).positions().items())]
        if not held:
            return self._gen_stock(ex, sim)
        aid, sym, pos = self.rng.choice(held)
        side = m.SELL if pos > 0 else m.BUY
        qty = abs(pos)
        if self.rng.random() < 0.4 and qty > 1:
            qty = max(1, int(qty * Decimal(repr(self.rng.random())))) if qty == qty.to_integral_value() else qty
        req = {"symbol": sym, "side": side, "qty": str(qty)}
        q = ex.quote(sym)
        if self.rng.random() < 0.3 and q is not None:
            ref = q.bid if side == m.SELL else q.ask
            req.update(type=m.LIMIT, limit_price=_px(ref), tif=self.rng.choice((m.DAY, m.GTC)))
        return {"op": "submit", "acct": aid, "req": req}

    def _gen_multileg(self, ex, sim):
        aid = self._account(ex)
        u = self._symbol(ex)
        name, spec = self.rng.choice(TEMPLATES)
        base = None
        legs = []
        flip = self.rng.random() < 0.25
        for kind, off, side, ratio, far in spec:
            if flip:
                side = m.SELL if side == m.BUY else m.BUY
            if kind == "S":
                legs.append({"symbol": u, "side": side, "ratio": ratio})
                continue
            got = self._contract(ex, u, far=far, offset=off, kind=kind, base_exp=base)
            if got is None:
                return None
            sym, base = got
            legs.append({"symbol": sym, "side": side, "ratio": ratio})
        req = {"legs": legs, "qty": self.rng.choice((1, 1, 2, 3, 10)), "tif": self.rng.choice((m.DAY, m.GTC, m.IOC, m.FOK, m.GTD))}
        if req["tif"] == m.GTD:
            req["good_till_date"] = self._gtd(ex)
        if self.rng.random() < 0.5:
            quotes = {l["symbol"]: ex.quote(l["symbol"]) for l in legs}
            from app.matching.multileg import net_price
            priced = net_price(legs, quotes)
            if priced is not None:
                net = priced[0] + Decimal(repr(self.rng.choice((-0.3, -0.05, 0.0, 0.05, 0.2))))
                req.update(type=m.LIMIT, limit_price=str(cents(abs(net))), price_effect="debit" if net >= 0 else "credit")
        return {"op": "submit", "acct": aid, "req": req, "template": name}

    def _gen_bracket(self, ex, sim):
        aid = self._account(ex)
        sym = self._symbol(ex)
        q = ex.quote(sym)
        if q is None:
            return None
        side = self.rng.choice((m.BUY, m.SELL))
        sign = 1 if side == m.BUY else -1
        req = {"symbol": sym, "side": side, "qty": self._qty(ex, aid, q.last), "tif": self.rng.choice((m.DAY, m.GTC, m.GTD))}
        if req["tif"] == m.GTD:
            req["good_till_date"] = self._gtd(ex)
        ref = q.last
        if self.rng.random() < 0.5:
            ref = q.last * (1 - sign * Decimal("0.002"))
            req.update(type=m.LIMIT, limit_price=_px(ref))
        w = Decimal(repr(self.rng.choice((0.002, 0.01, 0.05))))
        if self.rng.random() < 0.85:
            req["take_profit"] = _px(ref * (1 + sign * w))
        if self.rng.random() < 0.85 or "take_profit" not in req:
            req["stop_loss"] = _px(ref * (1 - sign * w))
        return {"op": "submit", "acct": aid, "req": req}

    def _gen_oco(self, ex, sim):
        aid = self._account(ex)
        n = self.rng.choice((2, 2, 3))
        subs = []
        base = self._symbol(ex)
        qty = None
        for _ in range(n):
            sym = base if self.rng.random() < 0.6 else self._symbol(ex)
            q = ex.quote(sym)
            if q is None:
                return None
            qty = qty or self._qty(ex, aid, q.last)
            sub = self._single(ex, aid, sym, self.rng.choice((m.BUY, m.SELL)), qty, allow_fraction=False)
            if sub["tif"] in (m.IOC, m.FOK):
                sub["tif"] = m.GTC
            if self.rng.random() < 0.3:
                sub["qty"] = max(1, qty // 2)
            subs.append(sub)
        return {"op": "submit", "acct": aid, "req": {"oco": subs}}

    def _gen_oto(self, ex, sim):
        aid = self._account(ex)
        sym = self._symbol(ex)
        q = ex.quote(sym)
        if q is None:
            return None
        side = self.rng.choice((m.BUY, m.SELL))
        entry = self._single(ex, aid, sym, side, self._qty(ex, aid, q.last), allow_fraction=False)
        if entry["tif"] in (m.IOC, m.FOK):
            entry["tif"] = m.DAY
        children = []
        exit_side = m.SELL if side == m.BUY else m.BUY
        for _ in range(self.rng.choice((1, 2))):
            if self.rng.random() < 0.6:  # an exit: no quantity
                c = self._single(ex, aid, sym, exit_side, 1, allow_fraction=False)
                c.pop("qty")
                c.pop("symbol")
                c.pop("side")
            else:
                other = self._symbol(ex)
                oq = ex.quote(other)
                c = self._single(ex, aid, other, self.rng.choice((m.BUY, m.SELL)),
                                 self._qty(ex, aid, oq.last if oq else Decimal(50)), allow_fraction=False)
            c["tif"] = self.rng.choice((m.GTC, m.GTD))
            if c["tif"] == m.GTD:
                c["good_till_date"] = self._gtd(ex)
            children.append(c)
        req = dict(entry, triggers=children)
        if len(children) > 1 or self.rng.random() < 0.3:
            req["triggers_oco"] = True
        return {"op": "submit", "acct": aid, "req": req}

    def _gen_auction(self, ex, sim):
        aid = self._account(ex)
        sym = self._symbol(ex)
        q = ex.quote(sym)
        if q is None:
            return None
        typ = self.rng.choice(AUCTIONS)
        side = self.rng.choice((m.BUY, m.SELL))
        req = {"symbol": sym, "side": side, "qty": self._qty(ex, aid, q.last), "type": typ}
        if typ in (m.LOO, m.LOC):
            req["limit_price"] = _px(q.last * Decimal(repr(self.rng.choice((0.98, 1.0, 1.02)))))
        return {"op": "submit", "acct": aid, "req": req}

    def _gen_preview(self, ex, sim):
        cmd = self.rng.choice((self._gen_stock, self._gen_multileg, self._gen_bracket, self._gen_oco, self._gen_option))(ex, sim)
        if cmd is None or cmd["op"] != "submit":
            return None
        return dict(cmd, op="preview")

    def _open_orders(self, ex):
        return [o for o in ex.engine.orders.values() if o.active]

    def _gen_replace(self, ex, sim):
        live = self._open_orders(ex)
        if not live:
            return None
        o = self.rng.choice(live)
        req = {}
        r = self.rng
        if r.random() < 0.5:
            req["qty"] = str(max(Decimal(1), o.qty + r.choice((-2, -1, 1, 5)) * max(Decimal(1), (o.qty / 10).to_integral_value())))
        if o.limit_price is not None and r.random() < 0.6:
            req["limit_price"] = _px(abs(o.limit_price) * Decimal(repr(r.choice((0.99, 1.0, 1.01)))))
        if o.stop_price is not None and r.random() < 0.4:
            req["stop_price"] = _px(o.stop_price * Decimal(repr(r.choice((0.99, 1.01)))))
        if not req:
            req["qty"] = str(o.qty)
        return {"op": "replace", "acct": o.account_id, "order": o.id, "req": req}

    def _gen_cancel(self, ex, sim):
        live = self._open_orders(ex)
        if not live:
            return None
        o = self.rng.choice(live)
        return {"op": "cancel", "acct": o.account_id, "order": o.id}

    def _gen_cancel_all(self, ex, sim):
        return {"op": "cancel_all", "acct": self._account(ex)}

    def _gen_settings(self, ex, sim):
        r = self.rng
        changes = {}
        for k in r.sample(sorted(cfg.SPEC), r.randint(1, 3)):
            kind, default, _, _, bounds = cfg.SPEC[k]
            if kind == "bool":
                changes[k] = r.random() < 0.5
            elif k == "margin_call_grace_hours":
                changes[k] = r.choice(("0", "0.5", "1", "48", "720"))
            else:
                changes[k] = r.choice((bounds[0], default, bounds[1], str(Decimal(default) * 2)))
        return {"op": "settings", "acct": self._account(ex), "changes": changes}

    def _gen_lot_method(self, ex, sim):
        return {"op": "lot_method", "acct": self._account(ex), "method": self.rng.choice(("fifo", "lifo", "highest_cost"))}

    def _gen_shock(self, ex, sim):
        held = sorted({(parse_option(s).underlying if parse_option(s) else s)
                       for a in ex.accounts.values() for s in a.positions()})
        sym = self.rng.choice(held) if held and self.rng.random() < 0.7 else self._symbol(ex)
        factor = self.rng.choice(("0.5", "0.7", "0.85", "0.95", "1.05", "1.2", "1.5", "2"))
        return {"op": "shock", "symbol": sym, "factor": factor}

    def _margin_accounts(self, ex):
        return [aid for aid in sorted(ex.accounts) if ex.account(aid).type == "margin"]

    def _gen_lever(self, ex, sim):
        """A margin account uses (nearly) all its buying power, long or short: the setup for a margin call."""
        accts = self._margin_accounts(ex)
        if not accts:
            return None
        aid = self.rng.choice(accts)
        sym = self._symbol(ex)
        q = ex.quote(sym)
        bp = ex.summary(aid)["buying_power"]
        if q is None or bp <= 0:
            return None
        qty = int(bp * Decimal(repr(self.rng.choice((0.5, 0.9, 0.98)))) / q.ask)
        if qty < 1:
            return None
        return {"op": "submit", "acct": aid, "req": {"symbol": sym, "side": self.rng.choice((m.BUY, m.BUY, m.SELL)),
                                                       "qty": qty}}

    def _gen_squeeze(self, ex, sim):
        """Move the largest position of a margin account against it."""
        accts = [aid for aid in self._margin_accounts(ex) if ex.account(aid).positions()]
        if not accts:
            return None
        a = ex.account(self.rng.choice(accts))
        rows = sorted(((abs(q) * ex.mark(s) * (100 if parse_option(s) else 1), s, q) for s, q in a.positions().items()),
                      reverse=True)
        _, sym, q = rows[0]
        c = parse_option(sym)
        under = c.underlying if c else sym
        # long stock or long calls / short puts lose when the underlying falls
        long_delta = (q > 0) if not c else ((q > 0) == (c.kind == "C"))
        factor = self.rng.choice(("0.55", "0.7", "0.8")) if long_delta else self.rng.choice(("1.3", "1.5", "1.9"))
        return {"op": "shock", "symbol": under, "factor": factor}

    def _gen_offline(self, ex, sim):
        return {"op": "offline", "days": self.rng.choice((0.3, 1, 1, 2, 3, 7, 16, 45)), "force": self.rng.random() < 0.7}

    def _gen_reload(self, ex, sim):
        return {"op": "reload", "force": self.rng.random() < 0.7}

    def _gen_reset(self, ex, sim):
        return {"op": "reset", "acct": self._account(ex),
                "cash": self.rng.choice((None, "5000", "50000", "250000"))}

    def _gen_replay(self, ex, sim):
        r = self.rng
        actions = []
        for _ in range(r.randint(1, 6)):
            x = r.random()
            if x < 0.4:
                actions.append(["submit", {"symbol": r.choice(("NVLX", "ORBT", "MKTX")), "side": r.choice(("buy", "sell")),
                                           "qty": r.randint(1, 20), "type": r.choice(("market", "market_on_close"))}])
            elif x < 0.8:
                actions.append(["step", r.choice((1, 60, 600, 3600))])
            else:
                actions.append(["seek", r.choice((1800, 4 * 3600, 7 * 3600))])
        return {"op": "replay", "sample": r.randrange(len(SAMPLES)), "actions": actions}

    def _gen_create_account(self, ex, sim):
        if len(ex.accounts) >= 8:
            return None
        return {"op": "create_account", "type": self.rng.choice(("margin", "cash")),
                "cash": self.rng.choice(("3000", "30000", "300000")), "lot_method": self.rng.choice(("fifo", "lifo", "highest_cost"))}

    def _gen_delete_account(self, ex, sim):
        if len(ex.accounts) <= 3:
            return None
        return {"op": "delete_account", "acct": self._account(ex)}

    def _gen_journal(self, ex, sim):
        aid = self._account(ex)
        a = ex.account(aid)
        if a.closed and self.rng.random() < 0.5:
            return {"op": "journal", "acct": aid, "target_type": "trade", "target_id": f"C{self.rng.randint(1, len(a.closed))}",
                    "note": self.rng.choice(("plan", "mistake: chased", "üñí ✓ 日本"))}
        mine = [o.id for o in ex.engine.orders.values() if o.account_id == aid]
        if not mine:
            return None
        return {"op": "journal", "acct": aid, "target_type": "order", "target_id": self.rng.choice(mine),
                "note": "entry", "tags": ["soak", self.rng.choice(("a", "b"))]}

    def _gen_alert(self, ex, sim):
        sym = self._symbol(ex)
        q = ex.feed.quote(sym)
        return {"op": "alert", "symbol": sym, "dir": self.rng.choice(("above", "below")),
                "price": _px(q.last * Decimal(repr(self.rng.choice((0.9, 1.1))))) if q else "10"}
