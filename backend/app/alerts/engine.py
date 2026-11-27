"""The alert engine: evaluated on every exchange tick at a cost bounded by what changed.

Indexes (rebuilt from the stored alerts on load; only active alerts are indexed):
- by symbol: price, change, volume and position alerts under their symbol; option alerts and
  position alerts on options also under the underlying (a model option price moves with it).
  A tick evaluates only the alerts indexed under the symbols whose quotes changed.
- indicator series: one incremental calculator per (symbol, timeframe, indicator, period), shared
  by every alert that uses it. A tick costs one comparison per series until a bar closes; then the
  closed bar is read from the bar store and pushed (never a re-read of the whole history).
- account alerts: grouped by account; the account summary is computed once per tick per account
  that has any, only when some quote changed.
- a time heap for reminders and expiry times; calendar alerts run once per new day.

Everything reads time from the exchange clock, so replay (a sandbox exchange with its own engine)
behaves the same as live, and alerts made in a replay stay in the replay's exchange.

Alerts are created and evaluated under the exchange lock (docs/CONCURRENCY.md), so nothing here may ask
a data provider. Price history is read with `feed.bars_ready`, which answers from memory or returns
None; a live feed then loads the history on its own thread. Until it has arrived the alert is not
evaluated (its last value says so), and the series is seeded or advanced on a later tick. Calendar
alerts whose feed cannot list corporate actions yet are tried again a few seconds later.
"""
import heapq
from decimal import Decimal
from typing import Optional

from ..core.errors import NotFound
from ..core.money import ZERO, D, cents
from ..marketdata.feed import TIMEFRAMES
from ..options import chain as optchain
from ..options import pricing
from ..options.symbology import describe as describe_symbol, multiplier, parse as parse_option
from . import indicators, model
from .notifications import EventStream, NotificationCentre

FEED_CHECK_SECONDS = 5
PRICES_CHECK_SECONDS = 15
CALENDAR_RETRY_SECONDS = 5  # exchange time between tries while the feed cannot list corporate actions yet
WAITING_FOR_HISTORY = "waiting for price history"
SYMBOL_KINDS = ("price", "change", "volume", "position", "option")
WEEK_OFFSET = 4 * 86400  # weekly bars start on Monday (see BarStore)


def bucket_start(tf: str, now: float) -> float:
    sec = TIMEFRAMES[tf]
    off = WEEK_OFFSET if tf == "1w" else 0
    return (now - off) // sec * sec + off


class Series:
    """One incremental indicator over closed bars of one symbol and timeframe."""

    def __init__(self, symbol: str, tf: str, indicator: str, period: int):
        self.symbol, self.tf, self.indicator, self.period = symbol, tf, indicator, period
        self.alerts: set = set()
        self.reset()

    def reset(self) -> None:
        self.seeded = False  # history pushed; until then the series is not advanced or evaluated
        self.waiting = False  # a bar closed and the history after `mark` has not been read yet
        self.mark = None  # feed.history_mark() taken when that bar closed
        self.calc = indicators.make(self.indicator, self.period)
        self.last_time: Optional[float] = None  # time of the last closed bar pushed
        self.bucket: Optional[float] = None  # start of the bar that was forming at the last check
        self.diff = self.prev_diff = None
        self.close: Optional[float] = None

    def push(self, close: float) -> None:
        self.calc.push(close)
        self.close = close
        v = self.calc.value
        self.prev_diff = self.diff
        if v is None:
            self.diff = None
        elif self.indicator in ("sma", "ema"):
            self.diff = close - v
        elif self.indicator == "macd":
            self.diff = v[0] - v[1]
        else:
            self.diff = v  # RSI: the value itself

    @property
    def crossed_up(self) -> bool:
        return self.prev_diff is not None and self.diff is not None and self.prev_diff <= 0 < self.diff

    @property
    def crossed_down(self) -> bool:
        return self.prev_diff is not None and self.diff is not None and self.prev_diff >= 0 > self.diff


class Alerting:
    """Owns the alerts and the notification centre of one exchange."""

    def __init__(self, ex):
        self.ex = ex
        self.notices = NotificationCentre(ex)
        self.alerts: dict[str, dict] = {}  # id -> record, in creation order
        self._by_symbol: dict[str, set] = {}
        self._by_account: dict[str, set] = {}  # account alerts
        self._calendar: set = set()
        self._series: dict[tuple, Series] = {}
        self._series_by_symbol: dict[str, set] = {}
        self._heap: list = []  # (time, alert id)
        self._avg_volume: dict = {}  # (symbol, day start) -> average daily volume
        self._cal_day = None
        self._cal_waiting: set = set()  # calendar alerts whose feed could not list actions yet
        self._cal_retry_at = float("-inf")
        self._feed_ok = True
        self._feed_checked = float("-inf")
        self._prices_checked = float("-inf")
        self._missing: dict[str, tuple] = {}
        self.evaluations = 0  # alert conditions evaluated (the cost measure the benchmark test reads)

    def stream(self) -> EventStream:
        return EventStream(self.notices.from_event)

    # ---- the store -------------------------------------------------------------------------
    def list(self, account_id: Optional[str] = None) -> list:
        if account_id is None:
            return list(self.alerts.values())
        return [a for a in self.alerts.values() if a.get("account_id") in (None, account_id)]

    def create(self, req: dict) -> dict:
        if len(self.alerts) >= model.MAX_ALERTS:
            raise ValueError(f"there are already {model.MAX_ALERTS} alerts; delete some first")
        al = model.build(self.ex, req, self.ex.now())
        al = {"id": f"AL{next(self.ex._alert_seq):05d}", **al}
        self.alerts[al["id"]] = al
        self._index(al)
        self.ex.dirty = True
        return al

    def get(self, alert_id: str, account_id: Optional[str] = None) -> dict:
        al = self.alerts.get(alert_id)
        if al is None or (account_id is not None and al.get("account_id") not in (None, account_id)):
            raise NotFound(f"no such alert {alert_id}")
        return al

    def update(self, alert_id: str, changes: dict) -> dict:
        al = self.get(alert_id)
        if not isinstance(changes, dict):
            raise ValueError("changes must be a JSON object")
        status = changes.get("status")
        fields = {k: v for k, v in changes.items() if k in model.EDITABLE}
        if fields:
            req = {**model.to_request(al), **fields}
            new = model.build(self.ex, req, self.ex.now())
            self._unindex(al)
            keep = {k: al[k] for k in ("id", "created_at", "fire_count", "triggered_at")}
            if al["status"] == "paused" and status is None:
                new["status"] = "paused"  # editing a paused alert keeps it paused
            if al["kind"] == "calendar":
                new["params"]["seen"] = al["params"].get("seen", [])
            al.clear()
            al.update(new, **keep)
            self._index(al)
        if status is not None:
            if status not in ("active", "paused"):
                raise ValueError("status can be set to 'active' or 'paused'")
            now = self.ex.now()
            if status == "active" and (al["status"] == "expired" or (al.get("expires_at") and al["expires_at"] <= now)):
                raise ValueError("this alert has expired; change or remove its expiry time first")
            if status == "active" and al["kind"] == "reminder" and not al["repeat"] and al["params"]["at"] <= now:
                raise ValueError("this reminder's time has passed; set a new time")
            self._unindex(al)
            al["status"] = status
            if status == "active":
                al["armed"], al["rearm_at"] = True, None
                if al["kind"] == "reminder" and al["repeat"] and al["params"]["at"] <= now:
                    al["rearm_at"] = now + al["cooldown"]
            self._index(al)
        self.ex.dirty = True
        return al

    def delete(self, alert_id: str) -> None:
        al = self.alerts.pop(alert_id, None)
        if al is not None:
            self._unindex(al)
            self.ex.dirty = True

    # ---- indexes ------------------------------------------------------------------------------
    def _symbols_for(self, al: dict) -> list:
        k, p = al["kind"], al.get("params") or {}
        if k in ("price", "change", "volume"):
            return [al["symbol"]]
        if k == "option":
            return [p["underlying"], al["symbol"]]
        if k == "position":
            return sorted({al["symbol"], p.get("underlying", al["symbol"])})
        return []

    def _index(self, al: dict) -> None:
        if al.get("expires_at") and al["status"] in ("active", "paused"):
            heapq.heappush(self._heap, (al["expires_at"], al["id"]))
        if al["status"] != "active":
            return
        k, aid = al["kind"], al["id"]
        for s in self._symbols_for(al):
            self._by_symbol.setdefault(s, set()).add(aid)
        if k == "indicator":
            p = al["params"]
            key = (al["symbol"], p["tf"], p["indicator"], p["period"])
            series = self._series.get(key)
            if series is None:
                series = self._series[key] = Series(*key)
                self._series_by_symbol.setdefault(al["symbol"], set()).add(key)
                self._seed(series)
            series.alerts.add(aid)
            if not series.seeded:
                al["last_value"] = WAITING_FOR_HISTORY
        elif k == "account":
            self._by_account.setdefault(al["account_id"], set()).add(aid)
        elif k == "calendar":
            self._calendar.add(aid)
        elif k == "reminder":
            heapq.heappush(self._heap, (self._reminder_due(al), aid))

    def _unindex(self, al: dict) -> None:
        aid = al["id"]
        for s in self._symbols_for(al):
            ids = self._by_symbol.get(s)
            if ids is not None:
                ids.discard(aid)
                if not ids:
                    del self._by_symbol[s]
        if al["kind"] == "indicator":
            p = al["params"]
            key = (al["symbol"], p["tf"], p["indicator"], p["period"])
            series = self._series.get(key)
            if series is not None:
                series.alerts.discard(aid)
                if not series.alerts:
                    del self._series[key]
                    keys = self._series_by_symbol.get(al["symbol"], set())
                    keys.discard(key)
                    if not keys:
                        self._series_by_symbol.pop(al["symbol"], None)
        ids = self._by_account.get(al.get("account_id"))
        if ids is not None:
            ids.discard(aid)
            if not ids:
                del self._by_account[al["account_id"]]
        self._calendar.discard(aid)
        self._cal_waiting.discard(aid)
        # heap entries are dropped lazily when they come due

    @staticmethod
    def _reminder_due(al: dict) -> float:
        p = al["params"]
        return al["rearm_at"] if al.get("rearm_at") else p["at"]

    # ---- the tick -------------------------------------------------------------------------------
    def on_tick(self, quotes: list, now: float) -> None:
        self._due(now)
        if quotes and self.alerts:
            changed = {}
            for q in quotes:
                changed[q.symbol] = q
            seen = set()
            for sym in changed:
                for aid in tuple(self._by_symbol.get(sym, ())):
                    if aid in seen:
                        continue
                    seen.add(aid)
                    al = self.alerts.get(aid)
                    if al is not None and al["status"] == "active":
                        self._eval_symbol_alert(al, now)
            for sym in changed:
                for key in tuple(self._series_by_symbol.get(sym, ())):
                    self._advance_series(self._series[key], now)
            for aid_acct in tuple(self._by_account):
                self._eval_account(aid_acct, now)
        if self._calendar:
            day = self.ex.lifecycle.today()
            todo = ()
            if day != self._cal_day:
                self._cal_day = day
                todo = tuple(self._calendar)
            elif self._cal_waiting and now >= self._cal_retry_at:
                todo = tuple(self._cal_waiting)
            if todo:
                self._cal_waiting = set()
                for aid in todo:
                    al = self.alerts.get(aid)
                    if al is not None and al["status"] == "active" and self._eval_calendar(al, now) is False:
                        self._cal_waiting.add(aid)
                self._cal_retry_at = now + CALENDAR_RETRY_SECONDS
        self._monitor(now)

    def _due(self, now: float) -> None:
        while self._heap and self._heap[0][0] <= now:
            t, aid = heapq.heappop(self._heap)
            al = self.alerts.get(aid)
            if al is None or al["status"] not in ("active", "paused"):
                continue
            if al.get("expires_at") and al["expires_at"] <= now and t == al["expires_at"]:
                self._expire(al, now)
                continue
            if al["kind"] == "reminder" and al["status"] == "active" and t == self._reminder_due(al):
                self.evaluations += 1
                self._fire(al, None, now)
                if al["repeat"]:
                    nxt = t + al["cooldown"]
                    if nxt <= now:  # the clock jumped (replay seek): skip the missed repeats, come back next time
                        nxt = now + al["cooldown"] - (now - t) % al["cooldown"]
                    al["rearm_at"] = nxt
                    heapq.heappush(self._heap, (nxt, aid))

    def _expire(self, al: dict, now: float) -> None:
        self._unindex(al)
        al["status"] = "expired"
        self.ex.dirty = True
        self.notices.add(al.get("account_id"), "alert", "info", "Alert expired",
                         f"Alert expired without firing again: {model.describe(al)}.", symbol=al.get("symbol"),
                         alert_id=al["id"])

    # ---- conditions -------------------------------------------------------------------------------
    @staticmethod
    def _cond(op: str, v, lvl) -> bool:
        return v >= lvl if op == "above" else v <= lvl

    def _check(self, al: dict, cond: bool, value, now: float, extra: str = "") -> None:
        """Apply the firing rule to one evaluation."""
        al["last_value"] = model.value_text(al, value)
        if not cond:
            if not al["armed"]:
                al["armed"] = True
            return
        if not al["armed"] or (al.get("rearm_at") and now < al["rearm_at"]):
            return
        self._fire(al, value, now, extra)

    def _fire(self, al: dict, value, now: float, extra: str = "") -> None:
        al["triggered_at"] = now
        al["fire_count"] = al.get("fire_count", 0) + 1
        self.ex.dirty = True
        if al["kind"] == "reminder":
            title, cat, sev = "Reminder", "reminder", "info"
            text = al.get("message") or "Reminder"
        else:
            cat, sev = "alert", "warning"
            title = f"Alert: {al['symbol']}" if al.get("symbol") else "Alert"
            seen = model.value_text(al, value)
            text = f"{model.describe(al)}" + (f": now {seen}" if seen else "") + "."
            if extra:
                text += " " + extra
            if al.get("message"):
                text += f" Note: {al['message']}"
        if not al["repeat"]:
            self._unindex(al)
            al["status"] = "triggered"
        elif al["kind"] != "reminder":
            al["armed"] = False
            al["rearm_at"] = now + al["cooldown"]
        symbol = al.get("symbol")
        self.ex.events.append({"type": "alert", "account_id": al.get("account_id"), "alert": dict(al),
                               "last": value if isinstance(value, Decimal) else None})
        self.notices.add(al.get("account_id"), cat, sev, title, text,
                         symbol=(parse_option(symbol).underlying if symbol and parse_option(symbol) else symbol),
                         alert_id=al["id"])

    def _eval_symbol_alert(self, al: dict, now: float) -> None:
        self.evaluations += 1
        k, p = al["kind"], al["params"]
        if k == "price":
            q = self.ex.feed.quote(al["symbol"])
            if q is not None and q.last > 0:
                self._check(al, self._cond(al["op"], q.last, D(al["price"])), q.last, now)
        elif k == "change":
            q = self.ex.feed.quote(al["symbol"])
            if q is None or q.last <= 0:
                return
            base = q.prev_close if p["basis"] == "day" else D(p["reference"])
            if not base:
                return
            pct = ((q.last - base) / base * 100).quantize(Decimal("0.01"))
            self._check(al, self._cond(al["op"], pct, D(al["value"])), pct, now)
        elif k == "volume":
            q = self.ex.feed.quote(al["symbol"])
            if q is None:
                return
            lvl = D(al["value"])
            if p["mode"] == "average":
                avg = self._average_volume(al["symbol"], now)
                if avg is None:
                    al["last_value"] = "no volume history yet"
                    return
                lvl = lvl * avg
            self._check(al, q.volume >= lvl, q.volume, now)
        elif k == "option":
            c = parse_option(al["symbol"])
            if c.expiry_ts <= now:
                self._unindex(al)
                al["status"] = "expired"
                al["note"] = "The contract expired."
                self.ex.dirty = True
                return
            v = self._option_metric(c, al["symbol"], p["metric"], now)
            if v is not None:
                self._check(al, self._cond(al["op"], v, float(al["value"])), v, now)
        elif k == "position":
            a = self.ex.accounts.get(al["account_id"])
            if a is None:
                return self.delete(al["id"])
            qty = a.position(al["symbol"])
            if qty == 0:
                al["last_value"] = "no position"
                return
            mark = self.ex.mark(al["symbol"])
            if mark <= 0:
                return
            cost = a.cost_basis(al["symbol"])
            pnl = cents(qty * mark * multiplier(al["symbol"]) - cost)
            v = pnl if p["metric"] == "unrealized_pnl" else ((pnl / abs(cost) * 100).quantize(Decimal("0.01")) if cost else ZERO)
            self._check(al, self._cond(al["op"], v, D(al["value"])), v, now)

    def _option_metric(self, c, symbol: str, metric: str, now: float) -> Optional[float]:
        u = self.ex.feed.quote(c.underlying)
        if u is None or u.last <= 0:
            return None
        S = float(u.last)
        provided = self.ex.feed.option_quote(symbol)
        if provided is None:
            snap = optchain.option_snapshot(c, S, self.ex.feed.base_vol(c.underlying), now)
            iv, delta = snap["iv"], snap["delta"]
        else:
            T = max(c.expiry_ts - now, 0.0) / optchain.YEAR_SECONDS
            iv = pricing.implied_vol(float(provided.mid), S, float(c.strike), T, optchain.RISK_FREE, c.kind)
            if iv is None:
                return None
            delta = pricing.greeks(S, float(c.strike), T, optchain.RISK_FREE, iv, c.kind)["delta"]
        return iv * 100 if metric == "iv" else delta

    def _average_volume(self, symbol: str, now: float) -> Optional[Decimal]:
        day = now // 86400 * 86400
        key = (symbol, day)
        if key not in self._avg_volume:
            if len(self._avg_volume) > 1000:
                self._avg_volume.clear()
            rows = self.ex.feed.bars_ready(symbol, "1d", 31)
            if rows is None:
                return None  # the feed is still loading the history: ask again on a later tick
            rows = [b for b in rows if b["time"] < day][-30:]
            vols = [b["volume"] for b in rows if b["volume"]]
            self._avg_volume[key] = Decimal(str(round(sum(vols) / len(vols)))) if vols else None
        return self._avg_volume[key]

    # ---- indicators --------------------------------------------------------------------------
    def _seed(self, s: Series) -> None:
        s.reset()
        self._fill(s)

    def _fill(self, s: Series) -> bool:
        """Push the history of a new or reset series. False while the feed does not have it in memory."""
        now = self.ex.now()
        start = bucket_start(s.tf, now)
        need = indicators.warmup(s.indicator, s.period) + 60
        rows = self.ex.feed.bars_ready(s.symbol, s.tf, need)
        if rows is None:
            return False
        for b in rows:
            if b["time"] < start:
                s.push(float(b["close"]))
                s.last_time = b["time"]
        s.prev_diff = s.diff  # history does not count as a crossing
        s.bucket = start
        s.seeded = True
        return True

    def _advance_series(self, s: Series, now: float) -> None:
        if not s.seeded:
            if not self._fill(s):
                for aid in s.alerts:
                    al = self.alerts.get(aid)
                    if al is not None:
                        al["last_value"] = WAITING_FOR_HISTORY
            return
        start = bucket_start(s.tf, now)
        if start != s.bucket:
            s.bucket = start
            s.waiting, s.mark = True, self.ex.feed.history_mark()  # only history read from now on has the closed bar
        if not s.waiting:
            return  # the forming bar has not closed yet: O(1)
        rows = self.ex.feed.bars_ready(s.symbol, s.tf, 4, s.mark)
        if rows is None:
            return  # the feed is loading it: come back on a later tick
        s.waiting = False
        rows = [b for b in rows if b["time"] < start and (s.last_time is None or b["time"] > s.last_time)]
        if len(rows) >= 4 and s.last_time is not None:  # a gap (the clock jumped): rebuild from history
            self._seed(s)
            rows = []
        for b in rows:
            s.push(float(b["close"]))
            s.last_time = b["time"]
            for aid in tuple(s.alerts):
                al = self.alerts.get(aid)
                if al is not None and al["status"] == "active":
                    self._eval_indicator(al, s, now)

    def _eval_indicator(self, al: dict, s: Series, now: float) -> None:
        self.evaluations += 1
        if s.diff is None:
            al["last_value"] = "waiting for enough bars"
            return
        if s.indicator == "rsi":
            v = s.diff
            self._check(al, self._cond(al["op"], v, float(al["value"])), v, now)
        else:
            cond = s.crossed_up if al["op"] == "crosses_above" else s.crossed_down
            val = s.calc.value
            shown = val if s.indicator != "macd" else val[0] - val[1]
            extra = f"Close {s.close:.2f}." if s.indicator != "macd" else ""
            self._check(al, cond, float(shown), now, extra)

    # ---- accounts -------------------------------------------------------------------------------
    def _eval_account(self, account_id: str, now: float) -> None:
        ids = self._by_account.get(account_id, ())
        if account_id not in self.ex.accounts:
            for aid in tuple(ids):
                self.delete(aid)
            return
        s = self.ex.safe_summary(account_id)  # an account that cannot be valued is skipped, not raised into the tick
        if s.get("broken") or s.get("prices_unavailable"):
            return  # no reliable value while a held symbol has no price
        values = {"equity": s["equity"], "buying_power": s["buying_power"], "margin_excess": s["excess_liquidity"],
                  "unrealized_pnl": s["unrealized_pnl"], "day_pnl": s["day_pnl"]}
        for aid in tuple(ids):
            al = self.alerts.get(aid)
            if al is None or al["status"] != "active":
                continue
            self.evaluations += 1
            v = D(values[al["params"]["metric"]])
            self._check(al, self._cond(al["op"], v, D(al["value"])), v, now)

    def _eval_calendar(self, al: dict, now: float) -> Optional[bool]:
        """False when the feed could not list corporate actions (on_tick then tries again shortly)."""
        self.evaluations += 1
        a = self.ex.accounts.get(al["account_id"])
        if a is None:
            return self.delete(al["id"])
        from datetime import timedelta
        p = al["params"]
        held = set()
        for sym in a.positions():
            c = parse_option(sym)
            held.add(c.underlying if c else sym)
        if not held:
            return
        today = self.ex.lifecycle.today()
        try:
            acts = self.ex.feed.corporate_actions(today, today + timedelta(days=p["days_before"]))
        except Exception as e:  # a provider error must not stop the tick
            al["last_value"] = f"calendar unavailable: {e}"
            return False
        if str(al.get("last_value") or "").startswith("calendar unavailable"):
            al["last_value"] = None  # it is available again
        due = []  # (date, symbol, id, kind, text)
        for act in acts:
            if act.symbol not in held or act.kind not in p["events"]:
                continue
            days = (act.ex_date - today).days
            when = "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days ({act.ex_date.isoformat()})"
            text = f"{act.describe().capitalize()}: the ex-date is {when}. You hold {act.symbol}."
            if act.kind == "dividend" and act.pay_date:
                text += f" It is paid on {act.pay_date.isoformat()}."
            due.append((act.ex_date, act.symbol, act.id, act.kind, text))
        if "earnings" in p["events"]:
            due += self._earnings_due(held, today, p["days_before"], now)
        for day, sym, eid, kind, text in sorted(due, key=lambda x: (x[0], x[1])):
            if eid in p["seen"]:
                continue
            p["seen"] = (p["seen"] + [eid])[-200:]
            al["triggered_at"], al["fire_count"] = now, al.get("fire_count", 0) + 1
            self.ex.dirty = True
            self.notices.add(al["account_id"], "alert", "info", f"Upcoming {kind}: {sym}", text,
                             symbol=sym, alert_id=al["id"])
            if not al["repeat"]:
                self._unindex(al)
                al["status"] = "triggered"
                return

    def _earnings_due(self, held: set, today, days_before: int, now: float) -> list:
        """Earnings reports of held symbols from today through days_before days ahead, from a research hub
        that schedules them (the simulated one); none from feeds without a schedule."""
        from datetime import timedelta
        from ..core import calendar as cal
        hub = getattr(self.ex.feed, "research", None)
        upcoming = getattr(hub, "upcoming_releases", None)
        if upcoming is None:
            return []
        until = cal.close_ts(today + timedelta(days=days_before)) + 6 * 3600  # covers an after-close report
        out = []
        for sym in sorted(held):
            for ts, k in upcoming(sym, now, until):
                rec = hub.earnings_record(sym, k, now)
                day = cal.ny_date(ts)
                days = (day - today).days
                when = "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days ({day.isoformat()})"
                text = (f"{sym} reports {rec['period']} results {rec['hour']} {when}. You hold {sym}. "
                        f"Analysts expect earnings of {rec['eps_estimate']} a share; the price can gap on the "
                        f"report, and a stop order then fills at the gap price. Simulated data.")
                out.append((day, sym, f"{sym}-earnings-{k}", "earnings", text))
        return out

    # ---- system monitors ---------------------------------------------------------------------------
    def _monitor(self, now: float) -> None:
        if now - self._feed_checked >= FEED_CHECK_SECONDS:
            self._feed_checked = now
            try:
                info = self.ex.feed.status_info() or {}
            except Exception:
                info = {}
            stream = info.get("stream") if isinstance(info.get("stream"), dict) else {}
            ok = not info.get("stale") and stream.get("state") != "failed"
            if ok != self._feed_ok:
                self._feed_ok = ok
                name = getattr(self.ex.feed, "name", "the data feed")
                if ok:
                    self.notices.add(None, "data", "success", "Data feed reconnected", f"Prices from {name} are arriving again.")
                else:
                    why = stream.get("message") or info.get("rest_error") or "no new prices have arrived for a while"
                    self.notices.add(None, "data", "critical", "Data feed disconnected",
                                     f"Prices from {name} have stopped: {why}. Quotes shown may be out of date.")
        if now - self._prices_checked >= PRICES_CHECK_SECONDS:
            self._prices_checked = now
            for a in self.ex.accounts.values():
                pos = a.positions()
                missing = tuple(sorted(s for s, v in self.ex._marks(pos).items() if v <= 0)) if pos else ()
                before = self._missing.get(a.id, ())
                if missing != before:
                    self._missing[a.id] = missing
                    if missing and not before:
                        self.notices.add(a.id, "data", "warning", "Prices unavailable",
                                         f"No price for {', '.join(missing)}. Account value and margin checks wait "
                                         "until a price arrives.", symbol=missing[0])
                    elif not missing:
                        self.notices.add(a.id, "data", "success", "Prices available again",
                                         f"Prices are available again for {', '.join(before)}.")

    # ---- splits ---------------------------------------------------------------------------------
    def on_split(self, act) -> None:
        for al in self.alerts.values():
            was = al["status"]
            if model.adjust_for_split(al, act):
                self.ex.dirty = True
                if was == "active" and al["status"] != "active":
                    self._unindex(al)
        for key in tuple(self._series_by_symbol.get(act.symbol, ())):
            self._seed(self._series[key])  # the bar store was adjusted: rebuild from it
        self._avg_volume = {k: v for k, v in self._avg_volume.items() if k[0] != act.symbol}

    # ---- persistence ------------------------------------------------------------------------------
    def to_plain(self) -> list:
        return [al for al in self.alerts.values()
                if al.get("account_id") is None or al["account_id"] in self.ex.accounts]

    def restore(self, rows, notifications=None) -> None:
        self.alerts, self._by_symbol, self._by_account, self._calendar = {}, {}, {}, set()
        self._series, self._series_by_symbol, self._heap = {}, {}, []
        for r in rows or []:
            al = migrate(r)
            if al is not None:
                self.alerts[al["id"]] = al
        for al in self.alerts.values():
            self._index(al)  # an expiry time that passed while the program was closed is handled on the first tick
        self.notices.restore(notifications)


def migrate(r) -> Optional[dict]:
    """Load one stored alert. The old price alerts ({id, symbol, op, price, created_at, triggered_at})
    become one-shot price alerts with the same id and state."""
    if not isinstance(r, dict) or "id" not in r:
        return None
    if "kind" in r:
        return r
    triggered = r.get("triggered_at")
    return {"id": r["id"], "kind": "price", "account_id": None, "symbol": r.get("symbol"), "op": r.get("op", "above"),
            "price": str(r.get("price")), "params": {}, "repeat": False, "cooldown": 0, "expires_at": None,
            "message": None, "note": r.get("note"), "status": "triggered" if triggered else "active",
            "created_at": r.get("created_at", 0), "triggered_at": triggered, "fire_count": 1 if triggered else 0,
            "armed": True, "rearm_at": None, "last_value": None}
