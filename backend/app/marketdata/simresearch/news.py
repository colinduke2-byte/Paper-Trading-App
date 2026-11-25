"""Simulated news: short items generated from templates, each tied to an event that has happened.

Sources of items (every item carries `event` naming what it refers to):
- earnings: a preview two days before a report, and the result on the release time
- dividend declarations (three weeks before the ex-date) and split announcements (a month before)
- analyst rating changes (seeded)
- economic releases (market-wide)
- price action from the stored daily bars: large daily moves, first new 52-week high or low, sector moves

No real company, person or organisation is named: companies are the fictional listings, analysts are
"a sell-side analyst", the central bank and statistics office have no names.
"""
import math
from datetime import date, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from ...core import calendar as cal
from . import macro
from .profiles import SOURCE, ny_ts, quarter_label, quarter_of, release_time, rng

MARKET_DAYS = 10
SYMBOL_DAYS = 120
CATEGORIES = ("earnings", "dividend", "split", "rating", "macro", "price_move", "52_week", "sector")


def _money(x) -> str:
    x = Decimal(x)
    sign = "-" if x < 0 else ""
    return f"{sign}${abs(x):,.2f}"


def _big(x) -> str:
    v = float(abs(x))
    unit = (1e12, "trillion") if v >= 1e12 else (1e9, "billion") if v >= 1e9 else (1e6, "million")
    return f"{'-' if x < 0 else ''}${v / unit[0]:.2f} {unit[1]}"


def _pct(x: float, places: int = 1) -> str:
    return f"{abs(x):.{places}f}%"


def _day(d: date) -> str:
    return d.strftime("%a %d %b").replace(" 0", " ")


def _item(id_, ts, category, headline, summary, symbols, event) -> dict:
    return {"id": id_, "headline": headline, "summary": summary, "source": SOURCE, "url": "", "author": "",
            "created_at": ts, "updated_at": None, "symbols": symbols, "image": None, "provider": "simulated",
            "category": category, "simulated": True, "event": event}


def _pick(key, options):
    return options[rng(key).randrange(len(options))]


def _bar_date(row) -> date:
    return date(1970, 1, 1) + timedelta(days=int(row[0] // 86400))


def _bar_ts(d: date) -> float:
    """News time for a daily bar: ten minutes after that day's close."""
    return (cal.close_ts(d) if cal.is_trading_day(d) else ny_ts(d, time(16))) + 600


# ---- per-symbol generators --------------------------------------------------------------------------
def _earnings(h, sym: str, start: float, now: float) -> list:
    out = []
    p = h.profile(sym)
    name = p.name
    k0 = quarter_of(cal.ny_date(start)) - 2
    for k in range(k0, quarter_of(cal.ny_date(now)) + 1):
        d, ts = release_time(h.seed, p, k)
        preview_ts = ny_ts(d - timedelta(days=2), time(7, 0))
        rec = None
        if start < preview_ts <= now:
            rec = h.earnings_record(sym, k, now)
            past = [h.gap(sym, j) for j in range(k - 4, k)]
            avg = sum(abs(math.exp(g) - 1) for g in past) / 4 * 100
            when = "before the open" if p.hour == "before open" else "after the close"
            out.append(_item(
                f"sim-preview-{sym}-{k}", preview_ts, "earnings",
                f"{name} reports {quarter_label(k)} results {when} on {_day(d)}",
                f"Analysts expect {name} ({sym}) to report earnings of {_money(rec['eps_estimate'])} a share on revenue "
                f"of {_big(rec['revenue_estimate'])}. Option prices imply a move of about {rec['expected_move_pct']}% "
                f"either way, against an average move of {avg:.1f}% after the last four reports.",
                [sym], {"kind": "earnings_preview", "id": f"{sym}-{k}", "ts": preview_ts}))
        if start < ts <= now:
            rec = rec if rec and rec["status"] == "reported" else h.earnings_record(sym, k, now)
            a, e = rec["eps_actual"], rec["eps_estimate"]
            res = rec["result"]
            if a < 0:
                verb = "loss narrower than expected" if res == "beat" else "wider loss than expected" if res == "miss" \
                    else "loss in line with estimates"
                head = f"{name} posts {verb}"
            else:
                head = {"beat": _pick(f"{sym}{k}h", (f"{name} beats profit estimates", f"{name} tops earnings forecasts",
                                                       f"{name} earnings come in ahead of expectations")),
                        "miss": _pick(f"{sym}{k}h", (f"{name} misses profit estimates", f"{name} falls short of earnings forecasts",
                                                       f"{name} earnings disappoint")),
                        "in line": f"{name} earnings match estimates"}[res]
            q = h.model(sym).quarter(k, h.shares(sym, now))
            prev = h.model(sym).quarter(k - 4, h.shares(sym, now))
            growth = float(q["income"]["revenue"] / prev["income"]["revenue"] - 1) * 100
            s1 = (f"{name} ({sym}) reported {quarter_label(k)} earnings of {_money(a)} a share against an average "
                  f"estimate of {_money(e)}, on revenue of {_big(rec['revenue_actual'])}, "
                  f"{'up' if growth >= 0 else 'down'} {_pct(growth)} from a year earlier.")
            guide = {"raised": "Management raised its full-year outlook", "cut": "Management cut its full-year outlook",
                     "reaffirmed": "Management reaffirmed its full-year outlook"}[rec["guidance"]]
            r = rec["price_reaction_pct"]
            s2 = (f"{guide}, and the shares gapped {'up' if r >= 0 else 'down'} {_pct(float(r))} on the release."
                  if r is not None else f"{guide}.")
            out.append(_item(f"sim-earnings-{sym}-{k}", ts, "earnings", head, f"{s1} {s2}", [sym],
                             {"kind": "earnings", "id": f"{sym}-{k}", "ts": ts}))
    return out


def _actions(h, sym: str, start: float, now: float) -> list:
    out = []
    name = h.profile(sym).name
    d0, d1 = cal.ny_date(start), cal.ny_date(now) + timedelta(days=35)
    for a in h.feed.corporate_actions(d0, d1):
        if a.symbol != sym:
            continue
        if a.kind == "dividend":
            ts = ny_ts(a.ex_date - timedelta(days=21), time(16, 30))
            if not (start < ts <= now):
                continue
            ref = h.reference_price(sym, ts)
            yld = f" At the recent price of {_money(ref)} that is an annual yield of about {float(a.amount * 4 / ref * 100):.2f}%." \
                if ref else ""
            out.append(_item(
                f"sim-dividend-{a.id}", ts, "dividend", f"{name} declares quarterly dividend of {_money(a.amount)} a share",
                f"The board of {name} ({sym}) declared a quarterly cash dividend of {_money(a.amount)} a share, payable on "
                f"{_day(a.pay_date)} to holders of record before the ex-dividend date of {_day(a.ex_date)}.{yld or ' The payment matches the previous quarter.'}",
                [sym], {"kind": "dividend", "id": a.id, "ts": ts}))
        else:
            ts = ny_ts(a.ex_date - timedelta(days=30), time(16, 30))
            if start < ts <= now:
                out.append(_item(
                    f"sim-split-{a.id}", ts, "split", f"{name} announces {a.new}-for-{a.old} stock split",
                    f"{name} ({sym}) will split its shares {a.new}-for-{a.old}, with trading on a split-adjusted basis from "
                    f"{_day(a.ex_date)}. Holders receive {a.new} shares for every {a.old} they own; the company's value "
                    f"does not change.", [sym], {"kind": "split_announcement", "id": a.id, "ts": ts}))
            eff = ny_ts(a.ex_date, time(9, 0))
            if start < eff <= now and a.id in h.feed.applied_actions:
                out.append(_item(
                    f"sim-split-done-{a.id}", eff, "split", f"{name} begins trading on a split-adjusted basis",
                    f"{name} ({sym}) shares now reflect the {a.new}-for-{a.old} split. Prices, open orders and "
                    f"per-share figures are adjusted; positions hold {a.new} shares for every {a.old}.",
                    [sym], {"kind": "split", "id": a.id, "ts": eff}))
    return out


def _ratings(h, sym: str, start: float, now: float) -> list:
    out = []
    p = h.profile(sym)
    words = {"buy": "buy", "hold": "hold", "sell": "sell"}
    up = {("hold", "buy"), ("sell", "hold"), ("sell", "buy")}
    reasons = ("improving margins", "a stronger product pipeline", "a stretched valuation", "slowing demand",
               "steady cash generation", "rising competition", "a better balance sheet", "cost pressures")
    for c in h.rating_changes(sym, cal.ny_date(now)):
        if not (start < c["ts"] <= now):
            continue
        tgt = h.target(sym, c)
        upgrade = (c["from"], c["to"]) in up
        reason = _pick(c["id"], reasons[0::2] if upgrade else reasons[1::2] if c["to"] == "sell" else reasons)
        verb = "upgrades" if upgrade else "downgrades"
        out.append(_item(
            f"sim-{c['id']}", c["ts"], "rating", f"Analyst {verb} {p.name} to {words[c['to']]}",
            f"A sell-side analyst covering {p.sector.lower()} moved {p.name} ({sym}) to {words[c['to']]} from "
            f"{words[c['from']]}{f', with a price target of {_money(tgt)}' if tgt else ''}. The note cited {reason}.",
            [sym], {"kind": "rating", "id": c["id"], "ts": c["ts"]}))
    return out


def _bars(h, sym: str, start: float, now: float) -> list:
    rows = h.feed.store.daily.get(sym, [])
    if len(rows) < 2:
        return []
    name = h.feed.info[sym].get("name", sym)
    sigma = float(h.feed.info[sym].get("vol", 0.3)) / math.sqrt(252)
    threshold = max(0.035, 2.5 * sigma)
    out = []
    first = max(1, len(rows) - SYMBOL_DAYS - 2)
    for i in range(first, len(rows)):
        d = _bar_date(rows[i])
        ts = _bar_ts(d)
        if not (start < ts <= now):
            continue
        prev_c, c = rows[i - 1][4], rows[i][4]
        if prev_c <= 0:
            continue
        ret = c / prev_c - 1
        if abs(ret) >= threshold:
            day_start = ts - 86400
            cause = ""
            for rts, k in h.upcoming_releases(sym, day_start - 86400, ts):
                cause = f" after its {quarter_label(k)} report"
            verb = _pick(f"{sym}{d}", ("jumps", "surges", "climbs")) if ret > 0 else \
                _pick(f"{sym}{d}", ("slides", "drops", "tumbles"))
            out.append(_item(
                f"sim-move-{sym}-{d.isoformat()}", ts, "price_move", f"{name} {verb} {_pct(ret * 100)}{cause}",
                f"{name} ({sym}) closed at {_money(Decimal(repr(c)).quantize(Decimal('0.01')))}, "
                f"{'up' if ret > 0 else 'down'} {_pct(ret * 100)} on the day. That is about {abs(ret) / sigma:.1f} times "
                f"its typical daily move in simulated trading.", [sym],
                {"kind": "price_move", "id": f"{sym}-{d.isoformat()}", "ts": ts, "return": ret}))
        if i >= 200:
            window = rows[max(0, i - 252):i]
            hi, lo = max(r[2] for r in window), min(r[3] for r in window)
            prev_window = rows[max(0, i - 253):i - 1]
            prev_new_high = rows[i - 1][4] > max(r[2] for r in prev_window)
            prev_new_low = rows[i - 1][4] < min(r[3] for r in prev_window)
            for kind, cond, prev_cond, word, level in (("high", c > hi, prev_new_high, "high", hi),
                                                       ("low", c < lo, prev_new_low, "low", lo)):
                if cond and not prev_cond:
                    out.append(_item(
                        f"sim-52w-{kind}-{sym}-{d.isoformat()}", ts, "52_week", f"{name} closes at a 52-week {word}",
                        f"{name} ({sym}) closed at {_money(Decimal(repr(c)).quantize(Decimal('0.01')))}, past its previous "
                        f"52-week {word} of {_money(Decimal(repr(level)).quantize(Decimal('0.01')))}. "
                        f"{'Momentum traders often watch for follow-through' if kind == 'high' else 'Some traders wait for a base to form'} "
                        f"after a break like this.", [sym],
                        {"kind": "52_week_" + kind, "id": f"{sym}-{d.isoformat()}", "ts": ts}))
    return out


def _sectors(h, start: float, now: float) -> list:
    by_sector: dict = {}
    for s in h.stocks():
        by_sector.setdefault(h.feed.info[s].get("sector"), []).append(s)
    out = []
    for sector, syms in by_sector.items():
        if len(syms) < 2:
            continue
        # The synthetic walk has a common market factor (0.55) and a stock factor (0.83): a sector average
        # moving more than about 2.4 standard deviations is unusual enough to report.
        sig = sum(float(h.feed.info[s].get("vol", 0.3)) for s in syms) / len(syms) / math.sqrt(252)
        threshold = max(0.02, 2.4 * sig * math.sqrt(0.55 ** 2 + 0.83 ** 2 / len(syms)))
        by_day: dict = {}
        for s in syms:
            rows = h.feed.store.daily.get(s, [])
            for i in range(max(1, len(rows) - MARKET_DAYS - 3), len(rows)):
                if rows[i - 1][4] > 0:
                    by_day.setdefault(_bar_date(rows[i]), []).append((s, rows[i][4] / rows[i - 1][4] - 1))
        for d, rets in by_day.items():
            ts = _bar_ts(d)
            if len(rets) < len(syms) or not (start < ts <= now):
                continue
            avg = sum(r for _, r in rets) / len(rets)
            if abs(avg) < threshold:
                continue
            best = max(rets, key=lambda r: r[1] if avg > 0 else -r[1])
            out.append(_item(
                f"sim-sector-{sector}-{d.isoformat()}", ts, "sector",
                f"{sector} stocks {'rally' if avg > 0 else 'sell off'}, {_pct(avg * 100)} on average",
                f"The {len(syms)} {sector.lower()} names in the simulated market moved {_pct(avg * 100)} on average "
                f"{'higher' if avg > 0 else 'lower'}. {h.feed.info[best[0]].get('name')} ({best[0]}) led with a move of "
                f"{'+' if best[1] > 0 else '-'}{_pct(best[1] * 100)}.", sorted(s for s, _ in rets),
                {"kind": "sector_move", "id": f"{sector}-{d.isoformat()}", "ts": ts, "return": avg}))
    return out


def _macro(h, start: float, now: float) -> list:
    out = []
    for e in macro.releases(h.seed, start, now):
        row = macro.public(e, now, h.moves_applied(e["ts"]))
        a, f, prev = row["actual"], row["forecast"], row["previous"]
        mv = row["market_move_pct"]
        tail = (f" Simulated stocks moved {'+' if mv >= 0 else ''}{mv}% on average (scaled by each stock's beta) as the "
                f"figure came out." if mv is not None else "")
        kind = e["kind"]
        if kind == "jobs":
            diff = "more than expected" if a > f else "fewer than expected" if a < f else "in line with forecasts"
            change = f"added {a:,} thousand jobs" if a >= 0 else f"lost {-a:,} thousand jobs"
            head = f"Jobs report: the economy {change}, {diff}"
            body = (f"The simulated economy {change} in {e['period']} against a forecast of {f:,} thousand, and the "
                    f"unemployment rate was {row['unemployment_rate']}%. The previous month's figure was {prev:,} thousand.{tail}")
        elif kind == "cpi":
            word = "hotter" if a > f else "cooler" if a < f else "in line with forecasts"
            head = f"Inflation {word if word.endswith('forecasts') else 'runs ' + word + ' than expected'} at {a}%"
            body = (f"Consumer prices rose {a}% from a year earlier in {e['period']}, against a forecast of {f}% and {prev}% "
                    f"the month before. Faster inflation makes rate rises from the central bank more likely.{tail}")
        elif kind == "retail":
            head = f"Retail sales {'rise' if a >= 0 else 'fall'} {abs(a)}% in {e['period']}"
            body = (f"Retail sales changed {a}% from the month before, against a forecast of {f}%. Spending is a large part "
                    f"of the simulated economy, so consumer stocks often react.{tail}")
        elif kind == "gdp":
            head = f"Economy grew at a {a}% annual rate in {e['period']}, first estimate shows"
            body = (f"The first estimate of growth was {a}% at an annualised rate, against a forecast of {f}% and {prev}% in "
                    f"the quarter before. Later estimates may revise the figure.{tail}")
        else:
            ch = row["change"]
            act = "raises" if ch > 0 else "cuts" if ch < 0 else "holds"
            expected = "as expected" if a == f else f"against expectations of {f}%"
            head = f"Central bank {act} its policy rate{f' by {abs(ch) * 100:.0f} basis points' if ch else ''} to {a}%"
            body = (f"The central bank set its policy rate at {a}%, {expected}, from {prev}%. Higher rates raise borrowing "
                    f"costs and tend to weigh on richly valued stocks.{tail}")
        out.append(_item(f"sim-macro-{e['id']}", e["ts"], "macro", head, body, [],
                         {"kind": "economic_release", "id": e["id"], "ts": e["ts"]}))
    return out


def symbol_news(h, sym: str, now: float, limit: int) -> list:
    start = now - SYMBOL_DAYS * 86400
    items = _bars(h, sym, start, now)
    if sym in h.stocks():
        items += _earnings(h, sym, start, now) + _actions(h, sym, start, now) + _ratings(h, sym, start, now)
    else:
        items += _actions(h, sym, start, now)
    items += [i for i in _sectors(h, now - MARKET_DAYS * 86400, now) if sym in i["symbols"]]
    items.sort(key=lambda i: (-i["created_at"], i["id"]))
    return items[:limit]


def market_news(h, now: float, limit: int, category: Optional[str] = None) -> list:
    start = now - MARKET_DAYS * 86400
    items = _macro(h, start, now) + _sectors(h, start, now)
    for s in h.feed.info:
        items += _bars(h, s, start, now)
        if s in h.stocks():
            items += _earnings(h, s, start, now) + _actions(h, s, start, now) + _ratings(h, s, start, now)
        else:
            items += _actions(h, s, start, now)
    if category:
        items = [i for i in items if i["category"] == category]
    items.sort(key=lambda i: (-i["created_at"], i["id"]))
    return items[:limit]
