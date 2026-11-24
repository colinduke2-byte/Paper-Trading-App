"""Screener filters: validation and evaluation over rows from data.FeedData.row().

A filter is one of
    {"field": <numeric field>, "min": number, "max": number}   (either bound may be left out)
    {"field": "type" | "sector", "in": [values]}
    {"field": "has_options", "equals": true | false}
A row with no value for a filtered field does not match (data that is missing is never guessed).
"""
from typing import Optional

# key: (label, unit, needs daily history)
NUMERIC = {
    "last": ("Price", "$", False),
    "change": ("Change", "$", False),
    "change_pct": ("Change %", "%", False),
    "gap_pct": ("Gap from previous close %", "%", False),
    "volume": ("Volume", "shares", False),
    "dollar_volume": ("Dollar volume", "$", False),
    "rel_volume": ("Relative volume (today vs 30-day average)", "x", True),
    "avg_volume_30d": ("Average volume (30 days)", "shares", True),
    "market_cap": ("Market cap", "$", False),
    "pe": ("P/E", "x", False),
    "dividend_yield": ("Dividend yield %", "%", False),
    "pct_from_high_52w": ("Below 52-week high %", "%", True),
    "pct_from_low_52w": ("Above 52-week low %", "%", True),
    "range_52w_pos": ("Position in 52-week range %", "%", True),
    "pct_vs_sma20": ("Price vs 20-day average %", "%", True),
    "pct_vs_sma50": ("Price vs 50-day average %", "%", True),
    "pct_vs_sma200": ("Price vs 200-day average %", "%", True),
    "rsi14": ("RSI (14 days)", "", True),
    "volatility": ("Volatility (1 year, annualised)", "", True),
}
CATEGORICAL = {"type": "Asset type", "sector": "Sector", "has_options": "Has options"}
FUNDAMENTAL = {"market_cap", "pe", "dividend_yield", "sector", "has_options"}  # come from the feed's symbol data
SORTABLE = set(NUMERIC) | {"symbol", "name", "sector", "type"}
MAX_FILTERS = 20
MAX_LIMIT = 500

PRESETS = [
    {"id": "preset-gap-up", "name": "Gapping up", "preset": True,
     "filters": [{"field": "gap_pct", "min": 1}], "sort": "gap_pct", "desc": True},
    {"id": "preset-gap-down", "name": "Gapping down", "preset": True,
     "filters": [{"field": "gap_pct", "max": -1}], "sort": "gap_pct", "desc": False},
    {"id": "preset-near-high", "name": "Near 52-week high", "preset": True,
     "filters": [{"field": "pct_from_high_52w", "max": 5}], "sort": "pct_from_high_52w", "desc": False},
    {"id": "preset-uptrend", "name": "Above 50 and 200-day averages", "preset": True,
     "filters": [{"field": "pct_vs_sma50", "min": 0}, {"field": "pct_vs_sma200", "min": 0}],
     "sort": "change_pct", "desc": True},
    {"id": "preset-oversold", "name": "Oversold (RSI below 30)", "preset": True,
     "filters": [{"field": "rsi14", "max": 30}], "sort": "rsi14", "desc": False},
]


def _num(v, what: str) -> Optional[float]:
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        raise ValueError(f"{what} must be a number")
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be a number, not {v!r}")
    if x != x or x in (float("inf"), float("-inf")):
        raise ValueError(f"{what} must be a finite number")
    return x


def check_filters(filters) -> list:
    if filters is None:
        return []
    if not isinstance(filters, list):
        raise ValueError("filters must be a list")
    if len(filters) > MAX_FILTERS:
        raise ValueError(f"at most {MAX_FILTERS} filters")
    out = []
    for f in filters:
        if not isinstance(f, dict) or not isinstance(f.get("field"), str):
            raise ValueError("each filter needs a field")
        field = f["field"]
        if field in NUMERIC:
            label = NUMERIC[field][0]
            lo, hi = _num(f.get("min"), f"{label} minimum"), _num(f.get("max"), f"{label} maximum")
            if lo is None and hi is None:
                raise ValueError(f"the {label} filter needs a minimum or a maximum")
            if lo is not None and hi is not None and lo > hi:
                raise ValueError(f"the {label} minimum is above its maximum")
            out.append({"field": field, "min": lo, "max": hi})
        elif field in ("type", "sector"):
            vals = f.get("in")
            if not isinstance(vals, list) or not vals or not all(isinstance(v, str) for v in vals):
                raise ValueError(f"the {CATEGORICAL[field]} filter needs a list of values")
            out.append({"field": field, "in": [v.strip() for v in vals][:50]})
        elif field == "has_options":
            if not isinstance(f.get("equals"), bool):
                raise ValueError("the has options filter needs equals: true or false")
            out.append({"field": field, "equals": f["equals"]})
        else:
            raise ValueError(f"unknown screener field {field}")
    return out


def needs_history(filters: list) -> bool:
    return any(f["field"] in NUMERIC and NUMERIC[f["field"]][2] for f in filters)


def matches(row: dict, f: dict) -> bool:
    v = row.get(f["field"])
    if f["field"] in NUMERIC:
        if v is None:
            return False
        return (f["min"] is None or v >= f["min"]) and (f["max"] is None or v <= f["max"])
    if f["field"] in ("type", "sector"):
        return v is not None and v.lower() in {x.lower() for x in f["in"]}
    return v is f["equals"] or v == f["equals"]


def evaluate(rows: list, filters: list) -> list:
    return [r for r in rows if all(matches(r, f) for f in filters)]


def sort_rows(rows: list, key: Optional[str], desc: bool) -> list:
    if not key:
        key, desc = "market_cap", True
    if key not in SORTABLE:
        raise ValueError(f"cannot sort by {key}")
    present = [r for r in rows if r.get(key) is not None]
    missing = [r for r in rows if r.get(key) is None]  # rows without the value go last either way
    text = key in ("symbol", "name", "sector", "type")
    present.sort(key=lambda r: (str(r[key]).lower() if text else r[key], r["symbol"]), reverse=desc)
    return present + sorted(missing, key=lambda r: r["symbol"])


def check_screen(body: dict) -> dict:
    """A saved screen: name, filters, sort, desc, columns."""
    from .store import check_name
    if not isinstance(body, dict):
        raise ValueError("a screen must be an object")
    name = check_name(body.get("name"), "Screen")
    sort = body.get("sort") or None
    if sort is not None and sort not in SORTABLE:
        raise ValueError(f"cannot sort by {sort}")
    cols = body.get("columns") or []
    if not isinstance(cols, list) or any(c not in NUMERIC and c not in CATEGORICAL and c not in ("name",) for c in cols):
        raise ValueError("columns must be screener field names")
    return {"name": name, "filters": check_filters(body.get("filters") or []), "sort": sort,
            "desc": bool(body.get("desc", True)), "columns": list(dict.fromkeys(cols))[:20]}
