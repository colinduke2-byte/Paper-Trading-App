"""Global invariants checked by the soak simulator.

Each check raises InvariantError(name, message). Names are stable so a minimised trace can be
matched to the same failure. The checks only read state.

Cash identity, derived from the ledger accounts rather than guessed: every ledger account an
account uses must be one of KNOWN_ACCOUNTS (or a position account), and then
    cash = starting cash + realised - fees + dividends received - dividends charged on shorts
           - margin interest - borrow fees - (signed cost basis of every open position)
where realised equals the sum of the closed-lot records, fees equal the sum of the fills' fees and
the cost basis equals the open lots' remaining cost. Cash in lieu is realised (split postings) and
option split settlements are fills, so they are covered by the same terms.
"""
import json
import math
import re
from decimal import Decimal

from app.core import models as m
from app.core.money import ZERO, D, cents
from app.ledger.ledger import CAPITAL, CASH, FEES, REALIZED
from app.lifecycle.manager import BORROW_EXPENSE, DIVIDEND_INCOME, INTEREST_EXPENSE, SHORT_DIVIDEND_EXPENSE
from app.options.symbology import multiplier, parse as parse_option

KNOWN_ACCOUNTS = {CASH, CAPITAL, REALIZED, FEES, DIVIDEND_INCOME, SHORT_DIVIDEND_EXPENSE, INTEREST_EXPENSE, BORROW_EXPENSE}
STATUSES = set(m.ACTIVE) | set(m.TERMINAL)
BAD_NUMBER = re.compile(r"^[+-]?(nan|snan|inf|infinity)$", re.I)
# Float values allowed in state.json: times (seconds since the epoch). Paths use [] for list indices.
TIME_PATHS = re.compile(r"(\.(ts|created_at|updated_at|expire_at|auction_at|opened_at|closed_at|triggered_at|"
                        r"margin_call_since|margin_call_ok_since|expires_at|rearm_at|at)$)|(\.lots\.[A-Z0-9]+\[\]\[\]$)|"
                        r"(\.log\[\]\[\]$)|(\.equity_history\[\]\[\]$)")


class InvariantError(AssertionError):
    def __init__(self, name: str, message: str):
        super().__init__(f"{name}: {message}")
        self.name = name
        self.message = message


# Failures already confirmed and reported (each has an xfail test in tests/soak/test_findings.py).
# A soak run counts them in SOFT_SEEN instead of stopping, so it can go on to find new ones;
# `python -m tools.soak --strict` (or clearing this set) makes them stop the run again.
CONFIRMED = {"statement_reconciliation", "statement_positions", "cash_account_negative_cash", "combo_without_bid"}
KNOWN = set()
SOFT_SEEN: dict = {}


def fail(name: str, message: str):
    if name in KNOWN:
        if name not in SOFT_SEEN:
            SOFT_SEEN[name] = [0, message]
        SOFT_SEEN[name][0] += 1
        return
    raise InvariantError(name, message)


# ---- ledger and lots -----------------------------------------------------------------------------
def check_ledger_incremental(account, start: int) -> int:
    """Every transaction from index `start` balances. Returns the new index."""
    txns = account.ledger.transactions
    for t in txns[start:]:
        total = sum((v for _, v in t.postings), ZERO)
        if total != 0:
            fail("ledger_unbalanced", f"{account.id} transaction {t.id} ({t.kind}) sums to {total}")
        for _, v in t.postings:
            if not isinstance(v, Decimal) or not v.is_finite():
                fail("ledger_bad_number", f"{account.id} transaction {t.id} posting {v!r}")
    return len(txns)


def check_lots(account) -> None:
    for sym, lots in account.lots.items():
        if not lots:
            fail("lots_empty_list", f"{account.id} keeps an empty lot list for {sym}")
        signs = {l.qty > 0 for l in lots}
        if len(signs) > 1:
            fail("lots_mixed_sign", f"{account.id} {sym} has long and short lots at once")
        for l in lots:
            if l.qty == 0 or l.cost < 0:
                fail("lot_bad", f"{account.id} {sym} lot qty {l.qty} cost {l.cost}")
        led = account.ledger.balance(f"pos:{sym}")
        basis = account.cost_basis(sym)
        if led != basis:
            fail("position_account_vs_lots", f"{account.id} {sym}: ledger pos {led} != lots basis {basis}")
    for acct, v in account.ledger.balances.items():
        if acct.startswith("pos:") and v != 0 and acct[4:] not in account.lots:
            fail("orphan_position_account", f"{account.id} {acct} holds {v} with no open lots")


def check_ledger_full(account) -> None:
    led = account.ledger
    re_ = {k: v for k, v in led.recompute().items() if v}
    bal = {k: v for k, v in led.balances.items() if v}
    if re_ != bal:
        diff = {k: (re_.get(k), bal.get(k)) for k in set(re_) | set(bal) if re_.get(k) != bal.get(k)}
        fail("ledger_replay_mismatch", f"{account.id} replay differs: {diff}")
    for i, t in enumerate(led.transactions):
        if t.id != i + 1:
            fail("ledger_ids", f"{account.id} transaction #{i} has id {t.id}")
    unknown = {k for k in bal if k not in KNOWN_ACCOUNTS and not k.startswith("pos:")}
    if unknown:
        fail("ledger_unknown_account", f"{account.id} uses ledger accounts {sorted(unknown)}")
    if -led.balance(CAPITAL) != account.starting_cash:
        fail("capital", f"{account.id} capital {-led.balance(CAPITAL)} != starting cash {account.starting_cash}")
    closed = sum((D(c["pnl"]) for c in account.closed), ZERO)
    if closed != account.realized:
        fail("realized_vs_closed_lots", f"{account.id} closed lots sum {closed} != ledger realised {account.realized}")
    fees = sum((f.fee for f in account.fills), ZERO)
    if fees != account.fees_paid:
        fail("fees_vs_fills", f"{account.id} fills' fees {fees} != ledger fees {account.fees_paid}")
    basis = sum((account.cost_basis(s) for s in account.lots), ZERO)
    expected = (account.starting_cash + account.realized - account.fees_paid - led.balance(DIVIDEND_INCOME)
                - led.balance(SHORT_DIVIDEND_EXPENSE) - led.balance(INTEREST_EXPENSE) - led.balance(BORROW_EXPENSE) - basis)
    if expected != account.cash:
        fail("cash_identity", f"{account.id} cash {account.cash} != derived {expected}")


def check_positions_vs_fills(account, split_symbols: set) -> None:
    """Open quantity equals the signed sum of the account's fills, for symbols never split."""
    qty: dict = {}
    for f in account.fills:
        qty[f.symbol] = qty.get(f.symbol, ZERO) + (f.qty if f.side == m.BUY else -f.qty)
    pos = account.positions()
    for sym in set(qty) | set(pos):
        c = parse_option(sym)
        if (c.underlying if c else sym) in split_symbols:
            continue
        if qty.get(sym, ZERO) != pos.get(sym, ZERO):
            fail("position_vs_fills", f"{account.id} {sym}: fills sum {qty.get(sym, ZERO)} != position {pos.get(sym, ZERO)}")


def check_cash_account(account) -> None:
    if account.type != "cash":
        return
    if account.cash < 0:
        fail("cash_account_negative_cash", f"{account.id} cash {account.cash}")
    for sym, q in account.positions().items():
        if q < 0 and parse_option(sym) is None:
            fail("cash_account_short_stock", f"{account.id} is short {q} {sym}")


def check_no_expired_options(ex, now: float) -> None:
    for a in ex.accounts.values():
        for sym in a.positions():
            c = parse_option(sym)
            if c is not None and c.expiry_ts <= now:
                fail("expired_option_held", f"{a.id} still holds {sym} after its expiry")


# ---- orders ----------------------------------------------------------------------------------------
class OrderBook:
    """What the checker has seen of each order: first quantity and terminal status."""

    def __init__(self):
        self.first_qty: dict = {}
        self.terminal: dict = {}

    def see(self, o) -> None:
        if o.status not in STATUSES:
            fail("order_bad_status", f"{o.id} status {o.status!r}")
        if o.filled_qty < 0 or o.filled_qty > o.qty:
            fail("order_overfilled", f"{o.id} filled {o.filled_qty} of {o.qty}")
        if o.status == m.FILLED and o.filled_qty != o.qty:
            fail("order_filled_short", f"{o.id} is filled with {o.filled_qty} of {o.qty}")
        self.first_qty.setdefault(o.id, o.qty)
        prev = self.terminal.get(o.id)
        if prev is not None and prev[0] != o.status:
            fail("order_two_terminal_states", f"{o.id} went from {prev[0]} to {o.status}")
        if prev is not None and prev[1] != o.filled_qty:
            fail("order_fill_after_terminal", f"{o.id} filled {prev[1]} -> {o.filled_qty} after it ended")
        if o.status in m.TERMINAL:
            self.terminal[o.id] = (o.status, o.filled_qty)

    def forget(self, order_ids) -> None:
        for i in order_ids:
            self.first_qty.pop(i, None)
            self.terminal.pop(i, None)


def check_orders_full(ex, book: OrderBook) -> None:
    orders = ex.engine.orders
    for o in orders.values():
        book.see(o)
    fills_by_order: dict = {}
    for a in ex.accounts.values():
        for f in a.fills:
            if f.order_id:
                fills_by_order.setdefault(f.order_id, {}).setdefault(f.symbol, ZERO)
                fills_by_order[f.order_id][f.symbol] += f.qty
    groups: dict = {}
    for o in orders.values():
        got = fills_by_order.get(o.id, {})
        if o.legs:
            want = {l["symbol"]: o.filled_qty * l["ratio"] for l in o.legs if o.filled_qty}
            if {k: v for k, v in got.items() if v} != want:
                fail("multileg_fills", f"{o.id} legs filled {got}, order filled {o.filled_qty} x {o.legs}")
            if o.filled_qty not in (ZERO, o.qty):
                fail("multileg_partial", f"{o.id} filled {o.filled_qty} of {o.qty}")
        elif sum(got.values(), ZERO) != o.filled_qty:
            fail("order_fills_mismatch", f"{o.id} fills sum {sum(got.values(), ZERO)} != filled {o.filled_qty}")
        if o.oco_group:
            groups.setdefault(o.oco_group, []).append(o)
        if o.role == m.TRIGGERED and o.status not in (m.HELD, m.CANCELED, m.EXPIRED) and o.parent_id in orders:
            p = orders[o.parent_id]
            if p.status != m.FILLED:
                fail("triggered_without_parent_fill", f"{o.id} is {o.status} but its parent {p.id} is {p.status}")
    for g, members in groups.items():
        if all(x.parent_id is None for x in members):  # standalone one-cancels-other
            bound = max(book.first_qty.get(x.id, x.qty) for x in members)
            total = sum((x.filled_qty for x in members), ZERO)
            if total > bound:
                fail("oco_overfill", f"group {g} filled {total} > largest order {bound}")
        elif all(x.role in (m.TAKE_PROFIT, m.STOP_LOSS, m.EXIT) for x in members) and g in orders:
            parent = orders[g]
            total = sum((x.filled_qty for x in members), ZERO)
            if total > parent.filled_qty:
                fail("bracket_overfill", f"children of {g} filled {total} > entry filled {parent.filled_qty}")


# ---- summary and reports ---------------------------------------------------------------------------
def check_summary(ex, a) -> dict:
    s = ex.summary(a.id)
    pos = a.positions()
    mv = sum((q * ex.mark(sym) * multiplier(sym) for sym, q in pos.items()), ZERO)
    pending = ex.lifecycle.pending_dividends(a)
    expected = cents(a.cash + mv) + pending
    if s["equity"] != expected:
        fail("equity_identity", f"{a.id} summary equity {s['equity']} != cash + marked positions + pending {expected}")
    for k, v in s.items():
        if isinstance(v, float) and not math.isfinite(v):
            fail("summary_not_finite", f"{a.id} {k} = {v}")
        if isinstance(v, Decimal) and not v.is_finite():
            fail("summary_not_finite", f"{a.id} {k} = {v}")
    if s["cash"] != a.cash or s["realized_pnl"] != a.realized:
        fail("summary_fields", f"{a.id} summary cash/realised disagree with the ledger")
    return s


def scan_state_json(text: str, label: str = "state.json") -> dict:
    """No NaN or Infinity anywhere, and floats only at time paths. Returns {path: count} of floats seen."""
    def bad_constant(c):
        fail("state_nan", f"{label} contains {c}")
    data = json.loads(text, parse_constant=bad_constant)
    floats: dict = {}

    def walk(x, path):
        if isinstance(x, float):
            if not math.isfinite(x):
                fail("state_nan", f"{label} {path} = {x}")
            floats[path] = floats.get(path, 0) + 1
            if label == "state.json" and not TIME_PATHS.search(path):
                fail("state_float_money", f"{label} has a float at {path}: {x!r}")
        elif isinstance(x, str):
            if BAD_NUMBER.match(x.strip()):
                fail("state_nan", f"{label} {path} = {x!r}")
        elif isinstance(x, dict):
            for k, v in x.items():
                walk(v, f"{path}.{k}")
        elif isinstance(x, list):
            for v in x:
                walk(v, f"{path}[]")
    walk(data, "")
    return floats
