"""Alpaca corporate actions for the lifecycle: fetched on the feed's thread, read by the tick.

The lifecycle asks `AlpacaFeed.corporate_actions(start, end)` from the synchronous tick. That call
never touches the network: it answers from what `ActionBook.poll()` (run by the poller thread)
has already fetched, or raises `ActionsPending` so the lifecycle waits and asks again.

What is fetched (docs/DATA_PROVIDERS.md, "Corporate actions"):
- Only symbols the accounts hold (stocks, and the underlyings of options) or have open orders in,
  in batches of `BATCH` symbols, through the feed's shared rate limiter.
- Once per New York day per symbol, over [earliest date asked for, today + FORWARD days]. A symbol
  that starts being held is fetched on the next poll, without waiting for the next day.
- Alpaca's start/end filter is documented as an interval of corporate actions "sorted by their
  process_date"; whether it filters on process_date or ex_date is UNVERIFIED, so the query is
  widened (PROCESS_MARGIN days after the end, a week before the start) and records are then
  matched on ex-date here.
- Data for a day counts as ready only when it was fetched on that New York date or later, so
  an action announced the evening before its ex-date is still seen.

Mapping to app.lifecycle.actions.CorporateAction:
- cash_dividend -> dividend (rate per share, ex_date, payable_date; a missing or earlier pay date
  becomes the ex-date). Dividends in a currency other than USD become notices.
- capital_gains_distribution -> dividend of long_term_rate + short_term_rate (cash per share).
- forward_split, reverse_split -> split, new_rate for every old_rate (decimal rates become an
  exact whole-number ratio, 1.5-for-1 -> 3-for-2).
- every other kind (stock dividends, spin-offs, mergers, name changes, redemptions, unit splits,
  worthless removals, rights, partial calls, reorganisations) -> a notice: PaperDesk cannot apply
  it, and says so on the affected position instead of dropping it silently.
Ids are "alpaca:<Alpaca id>". Each action also carries a revision key (symbol, kind, ex-date,
regular or special) so a record Alpaca re-issues under a new id is not applied twice.
"""
import logging
import threading
from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import Callable, Optional

from ..core import calendar as cal
from ..lifecycle.actions import DIVIDEND, SPLIT, ActionsPending, ActionsUnavailable, CorporateAction
from .http import ProviderError
from .research import corporate_action_records

log = logging.getLogger("paperdesk.alpaca")

BATCH = 50  # symbols per request: keeps the URL short
LOOKBACK = 10  # days before today always covered (late announcements, restarts)
FORWARD = 120  # days after today covered (the upcoming list)
PROCESS_MARGIN = 120  # a dividend's process (pay) date can be weeks after its ex-date
PERMANENT = (400, 401, 403, 404, 422)  # errors retrying soon will not fix
RETRY = (30.0, 60.0, 120.0, 300.0, 900.0)
HOLD_DAYS = 5  # liquidation waits this many days after an action PaperDesk cannot apply

LABELS = {
    "stock_dividend": "stock dividend", "spin_off": "spin-off", "cash_merger": "cash merger",
    "stock_merger": "stock merger", "stock_and_cash_merger": "stock and cash merger", "redemption": "redemption",
    "name_change": "name change", "worthless_removal": "removal as worthless", "rights_distribution": "rights distribution",
    "partial_call": "partial call", "reorganization": "reorganization", "unit_split": "unit split",
    "cash_dividend": "cash dividend", "forward_split": "split", "reverse_split": "reverse split",
}


def _day(v) -> Optional[date]:
    try:
        return date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


def _ratio(new, old) -> Optional[tuple]:
    try:
        f = Fraction(Decimal(str(new))) / Fraction(Decimal(str(old)))
    except (ArithmeticError, ValueError, ZeroDivisionError):
        return None
    if f <= 0 or f == 1 or f.numerator > 100000 or f.denominator > 100000:
        return None
    return f.numerator, f.denominator


def notice(rec: dict, why: Optional[str] = None) -> dict:
    kind = rec["type"]
    sym = rec["symbol"]
    when = rec.get("ex_date") or rec.get("effective_date") or rec.get("process_date")
    label = LABELS.get(kind, kind.replace("_", " "))
    detail = ""
    if kind == "name_change" and rec.get("new_symbol"):
        detail = f" (new symbol {rec['new_symbol']})"
    elif kind in ("cash_merger", "stock_merger", "stock_and_cash_merger") and rec.get("acquirer_symbol"):
        detail = f" (acquirer {rec['acquirer_symbol']})"
    elif kind == "spin_off" and rec.get("new_symbol"):
        detail = f" (new company {rec['new_symbol']})"
    reason = why or "PaperDesk cannot apply this kind of corporate action"
    msg = (f"{label[:1].upper() + label[1:]} of {sym}{detail} effective {when or 'on an unknown date'}: {reason}, so "
           f"positions in {sym} were not changed. Check the real terms; the symbol may stop trading, and then its "
           f"last price is kept.")
    return {"id": f"alpaca:{rec.get('id')}", "symbol": sym, "type": kind, "date": when, "message": msg}


def to_lifecycle(records: list) -> tuple:
    """Flat Alpaca records (research.corporate_action_records) -> (CorporateActions, notices)."""
    actions, notices = [], []
    for r in records:
        kind, sym, rid = r.get("type"), r.get("symbol"), r.get("id")
        if not sym or not rid:
            continue
        ex = _day(r.get("ex_date"))
        try:
            if kind in ("cash_dividend", "capital_gains_distribution"):
                if ex is None:
                    continue  # incomplete record: Alpaca completes it later (data_quality=complete hides it)
                cur = (r.get("currency") or "USD").upper()
                if cur != "USD":
                    notices.append(notice(r, f"the dividend is paid in {cur}, which PaperDesk does not convert"))
                    continue
                if kind == "cash_dividend":
                    amount = r.get("rate")
                else:
                    amount = (r.get("long_term_rate") or Decimal(0)) + (r.get("short_term_rate") or Decimal(0))
                if amount is None or amount <= 0:
                    continue
                pay = _day(r.get("payable_date")) or ex
                special = "special" if r.get("special") else "regular"
                actions.append(CorporateAction(f"alpaca:{rid}", sym, DIVIDEND, ex, max(pay, ex), Decimal(amount),
                                               revision_key=f"alpaca|{sym}|{kind}|{ex.isoformat()}|{special}"))
            elif kind in ("forward_split", "reverse_split"):
                ratio = _ratio(r.get("new_rate"), r.get("old_rate"))
                if ex is None:
                    continue
                if ratio is None:
                    notices.append(notice(r, f"the ratio {r.get('new_rate')} for {r.get('old_rate')} cannot be applied"))
                    continue
                actions.append(CorporateAction(f"alpaca:{rid}", sym, SPLIT, ex, new=ratio[0], old=ratio[1],
                                               revision_key=f"alpaca|{sym}|split|{ex.isoformat()}"))
            else:
                notices.append(notice(r))
        except ValueError as e:  # a record CorporateAction refuses: show it rather than lose it
            notices.append(notice(r, f"the record could not be used ({e})"))
    return actions, notices


def _when(x) -> Optional[date]:
    if isinstance(x, CorporateAction):
        return x.ex_date
    return _day(x.get("date"))


class ActionBook:
    """Per-symbol corporate action cache filled by `poll()` on a background thread."""

    def __init__(self, rest, wall_clock: Callable[[], float], symbols: Callable[[], list]):
        self.rest, self.wall_clock, self.symbols = rest, wall_clock, symbols
        self.lock = threading.Lock()
        self.cover: dict = {}  # symbol -> (fetched on NY date, start, end)
        self.actions: dict = {}  # id -> CorporateAction
        self.notices: dict = {}  # id -> notice
        self.want_start: Optional[date] = None
        self.error: Optional[str] = None
        self.error_status: Optional[int] = None
        self.failures = 0
        self.next_try = 0.0
        self.last_ok: Optional[float] = None

    def today(self) -> date:
        return cal.ny_date(self.wall_clock())

    def _holdings(self) -> list:
        try:
            return sorted({s for s in self.symbols() if s})
        except Exception:  # the exchange changed under us; the next call sees it
            return []

    @property
    def permanent(self) -> bool:
        return self.error_status in PERMANENT

    def _missing(self, start: date, end: date, syms: list) -> list:
        need = min(end, self.today())
        out = []
        for s in syms:
            c = self.cover.get(s)
            if c is None or c[0] < need or c[1] > start or c[2] < end:
                out.append(s)
        return out

    @staticmethod
    def _select(start: date, end: date, syms: set, rows) -> list:
        out = []
        for x in rows:
            sym = x.symbol if isinstance(x, CorporateAction) else x["symbol"]
            w = _when(x)
            if sym in syms and w is not None and start <= w <= end:
                out.append(x)
        return out

    def actions_for(self, start: date, end: date) -> list:
        """Actions with an ex-date in [start, end] for the symbols held. Raises ActionsPending until ready."""
        syms = self._holdings()
        with self.lock:
            if self.want_start is None or start < self.want_start:
                self.want_start = start
            missing = self._missing(start, end, syms)
            known = self._select(start, end, set(syms), self.actions.values())
            if not missing:
                return sorted(known, key=lambda x: (x.ex_date, x.symbol, x.id))
            if self.permanent:
                raise ActionsUnavailable(self.error)
            cause = f"the last request failed ({self.error}); retrying" if self.error else "the request is in progress"
            raise ActionsPending(f"Alpaca corporate actions for {', '.join(missing[:5])}"
                                 f"{' and others' if len(missing) > 5 else ''} are not loaded yet ({cause}).", known)

    def notices_for(self, start: date, end: date) -> list:
        syms = set(self._holdings())
        with self.lock:
            return sorted(self._select(start, end, syms, self.notices.values()), key=lambda n: (n.get("date") or "", n["id"]))

    def poll(self) -> int:
        """Fetch what is missing (run by the poller thread). Returns the number of batches fetched."""
        now = self.wall_clock()
        if now < self.next_try:
            return 0
        today = self.today()
        syms = self._holdings()
        with self.lock:
            start = min(self.want_start or today, today - timedelta(days=LOOKBACK))
            end = today + timedelta(days=FORWARD)
            missing = self._missing(start, end, syms)
        done = 0
        for i in range(0, len(missing), BATCH):
            batch = missing[i:i + BATCH]
            qstart, qend = start - timedelta(days=7), end + timedelta(days=PROCESS_MARGIN)
            try:
                resp = self.rest.corporate_actions(",".join(batch), qstart.isoformat(), qend.isoformat(), None,
                                                   max_pages=20, strict=True, max_wait=30)
            except ProviderError as e:
                with self.lock:
                    self.error, self.error_status = e.message, e.status
                    self.failures += 1
                    wait = 3600.0 if self.permanent else RETRY[min(self.failures - 1, len(RETRY) - 1)]
                    self.next_try = now + wait
                log.warning("Alpaca corporate actions: %s", e.message)
                return done
            acts, notes = to_lifecycle(corporate_action_records(resp))
            wanted = set(batch)
            with self.lock:
                for a in acts:
                    if a.symbol in wanted:
                        self.actions[a.id] = a
                for n in notes:
                    if n["symbol"] in wanted:
                        self.notices[n["id"]] = n
                for s in batch:
                    self.cover[s] = (today, start, end)
                self.error = self.error_status = None
                self.failures, self.next_try, self.last_ok = 0, 0.0, now
            done += 1
        return done

    def holds(self, today: date, applied: set, since: Optional[date]) -> dict:
        """{symbol: [{"kind", "reason"}]}: known splits due but not applied (kind "split_pending"), and
        recent actions PaperDesk cannot apply (kind "unsupported"). Read by the margin checks; in memory only."""
        out: dict = {}
        lo = today - timedelta(days=LOOKBACK)
        with self.lock:
            for a in self.actions.values():
                if a.kind != SPLIT or not lo <= a.ex_date <= today or (since is not None and a.ex_date <= since):
                    continue
                if a.id in applied or (a.revision_key and a.revision_key in applied):
                    continue
                out.setdefault(a.symbol, []).append({
                    "kind": "split_pending",
                    "reason": f"the {a.describe()} effective {a.ex_date.isoformat()} is not applied to the accounts yet"})
            for n in self.notices.values():
                d = _day(n.get("date"))
                if d is not None and today - timedelta(days=HOLD_DAYS) <= d <= today:
                    label = LABELS.get(n.get("type"), str(n.get("type")).replace("_", " "))
                    out.setdefault(n["symbol"], []).append({
                        "kind": "unsupported",
                        "reason": f"{n['symbol']} had a {label} on {d.isoformat()} that PaperDesk cannot apply, so its "
                                  f"price change may not be a real loss"})
        return out

    def status(self) -> dict:
        with self.lock:
            state = "error" if self.error else "ok" if self.last_ok else "idle"
            return {"state": state, "message": self.error, "symbols": len(self.cover), "last_ok": self.last_ok}
