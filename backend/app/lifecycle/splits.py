"""Applying a stock split to one account.

Stock lots
    Every lot's quantity is multiplied by new/old and its price divided by new/old; its
    cost basis (in cents) is unchanged. If the new position is not whole (a reverse
    split, or a 3-for-2 split of an odd lot) the fraction is paid out as cash in lieu.
    Positions that were already fractional keep 4 decimal places; only the part below
    0.0001 share is paid out.

    Cash in lieu is the value of the old position at the pre-split price, rounded to the
    cent, minus the value of the kept shares at the split-adjusted price, rounded to the
    cent. The fraction comes out of the newest lots first; the cost basis it carried is
    realised. Lots and ledger are exact. Account value is unchanged to the cent when
    valued at the exact adjusted price; a feed that publishes the adjusted price rounded
    to a cent (25.0025 -> 25.00) moves the value by that rounding, like any price move.

Options on the split underlying
    For a whole-number forward split (N-for-1, or any new/old that is a whole number N)
    each contract becomes N contracts with the strike divided by N, when that strike is
    representable in OCC symbology (a multiple of 0.001). Cost basis moves to the new
    symbol unchanged. The deliverable (contracts x 100 shares x strike) is unchanged.

    Every other case (reverse splits, ratios like 3-for-2, strikes that cannot be divided
    exactly) has no standard contract to map to, because the simulator only knows
    100-share deliverables. Those positions are cash settled at their pre-split model
    mark (the same mark used for account value), booked as a `split_settlement` fill.
    The cash received or paid equals the position's market value at that mark (marks are
    rounded to the cent), so account value at that mark is unchanged. Adjusted contracts
    are valued by the model after the split; rounding each new mark to the cent can move
    their value by up to half a cent per share.
"""
from decimal import ROUND_FLOOR, Decimal

from ..core.models import BUY, SELL, Fill
from ..core.money import ZERO, cents
from ..ledger.ledger import CASH, REALIZED, position_account
from ..options.symbology import build, parse

FOUR = Decimal("0.0001")
PRICE_PLACES = Decimal("0.00000001")


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def tidy(q: Decimal) -> Decimal:
    """200.0000 -> 200, 3.7500 -> 3.75 (never exponent notation)."""
    return q.quantize(Decimal(1)) if q == q.to_integral_value() else q.normalize()


def _floor(q: Decimal, whole: bool) -> Decimal:
    return q.to_integral_value(ROUND_FLOOR) if whole else q.quantize(FOUR, ROUND_FLOOR)


def split_stock(account, action, price: Decimal, ts: float) -> dict | None:
    """Adjust the account's stock lots. Returns a summary, or None when nothing is held."""
    sym = action.symbol
    lots = account.lots.get(sym)
    if not lots:
        return None
    n, o = Decimal(action.new), Decimal(action.old)
    old_pos = sum((l.qty for l in lots), ZERO)
    sign = 1 if old_pos > 0 else -1
    whole = old_pos == old_pos.to_integral_value()
    exact_total = abs(old_pos) * n / o
    kept = _floor(exact_total, whole)

    new_qty = [(abs(l.qty) * n / o).quantize(FOUR, ROUND_FLOOR) for l in lots]
    diff = kept - sum(new_qty, ZERO)
    if diff > 0:  # rounding dust from flooring each lot belongs to the kept shares
        new_qty[0] += diff
    removed_cost, i = ZERO, len(lots) - 1
    while diff < 0 and i >= 0:  # the fraction paid in lieu comes out of the newest lots
        take = min(new_qty[i], -diff)
        if take > 0:
            portion = lots[i].cost if take == new_qty[i] else cents(lots[i].cost * take / new_qty[i])
            lots[i].cost -= portion
            removed_cost += portion
            new_qty[i] -= take
            diff += take
        i -= 1
    for lot, q in zip(lots, new_qty):
        if q == 0 and lot.cost:
            removed_cost += lot.cost
            lot.cost = ZERO
        lot.qty = sign * tidy(q) if q else ZERO
        lot.price = (lot.price * o / n).quantize(PRICE_PLACES)
    account.lots[sym] = [l for l in lots if l.qty != 0]
    if not account.lots[sym]:
        del account.lots[sym]

    adj_price = price * o / n
    new_pos = sign * tidy(kept) if kept else ZERO
    fraction = exact_total - kept
    # signed (shorts pay); defined so the position's value rounded to the cent is unchanged
    cash_in_lieu = cents(old_pos * price) - cents(new_pos * adj_price) if fraction > 0 else ZERO
    pos = position_account(sym)
    pnl = (cash_in_lieu - removed_cost) if sign > 0 else (removed_cost + cash_in_lieu)
    memo = (f"{_cap(action.describe())} effective {action.ex_date.isoformat()}: "
            f"{old_pos.normalize():f} shares became {new_pos.normalize():f}")
    postings = []
    if cash_in_lieu or removed_cost:
        memo += (f"; cash in lieu of {fraction.quantize(PRICE_PLACES).normalize():f} share"
                 f" at {adj_price.quantize(PRICE_PLACES).normalize():f}")
        postings = [(CASH, cash_in_lieu), (pos, -sign * removed_cost), (REALIZED, -pnl)]
        account.closed.append({"symbol": sym, "qty": str(sign * fraction.quantize(PRICE_PLACES)),
                               "open_price": str((removed_cost / fraction).quantize(PRICE_PLACES)) if fraction else "0",
                               "close_price": str(adj_price.quantize(PRICE_PLACES)), "pnl": str(pnl),
                               "opened_at": lots[-1].ts if lots else ts, "closed_at": ts, "kind": "cash_in_lieu"})
    account.ledger.post(ts, "split", memo, postings, ref=action.id)
    return {"symbol": sym, "old_qty": old_pos, "new_qty": new_pos, "cash_in_lieu": cash_in_lieu,
            "fraction": sign * fraction, "realized": pnl if postings else ZERO, "message": memo}


def option_adjustable(action, symbol: str) -> bool:
    n = action.whole_forward_factor
    c = parse(symbol)
    if n is None or c is None:
        return False
    milli = c.strike * 1000
    return milli == milli.to_integral_value() and int(milli) % n == 0


def split_options(account, action, marks: dict, ts: float, next_fill_id) -> list:
    """Adjust or cash settle options on the split underlying. Returns notes (dicts)."""
    held = [s for s in account.lots if (c := parse(s)) is not None and c.underlying == action.symbol]
    notes = []
    # 1. Contracts with no exact adjusted equivalent are cash settled at the pre-split mark.
    for sym in [s for s in held if not option_adjustable(action, s)]:
        qty = account.position(sym)
        if qty == 0:
            continue
        mark = marks[sym]
        f = Fill(id=next_fill_id(), order_id=None, account_id=account.id, symbol=sym, side=SELL if qty > 0 else BUY,
                 qty=abs(qty), price=mark, ts=ts, kind="split_settlement")
        account.apply_fill(f, 100)
        notes.append({"symbol": sym, "kind": "settled", "qty": qty, "price": mark, "fill": f,
                      "message": f"{_cap(action.describe())}: {qty.normalize():f} {sym} cash settled at the "
                                 f"pre-split mark of {mark.normalize():f} (no standard adjusted contract exists)"})
    # 2. Whole-number forward splits: N contracts at strike / N. Two phases so a new symbol
    #    can never collide with a contract that has not been moved yet.
    n = action.whole_forward_factor
    moving = {s: account.lots.pop(s) for s in held if s in account.lots and option_adjustable(action, s)}
    postings = []
    for sym, lots in moving.items():
        c = parse(sym)
        new_sym = build(c.underlying, c.expiry, c.kind, c.strike / n)
        basis = sum((l.cost if l.qty > 0 else -l.cost for l in lots), ZERO)
        old_qty = sum((l.qty for l in lots), ZERO)
        for l in lots:
            l.qty *= n
            l.price = (l.price / n).quantize(PRICE_PLACES)
        account.lots.setdefault(new_sym, []).extend(lots)
        postings += [(position_account(sym), -basis), (position_account(new_sym), basis)]
        notes.append({"symbol": sym, "kind": "adjusted", "new_symbol": new_sym, "qty": old_qty, "new_qty": old_qty * n,
                      "message": f"{_cap(action.describe())}: {old_qty.normalize():f} {sym} became "
                                 f"{(old_qty * n).normalize():f} {new_sym}"})
    if moving:
        account.ledger.post(ts, "split", "; ".join(x["message"] for x in notes if x["kind"] == "adjusted"),
                            postings, ref=action.id)
    return notes
