"""The bundled sample sessions. They come from the synthetic feed with fixed seeds, so they are
simulated data (fictional companies) and are labelled that way. Regenerate with:

    python -m app.marketdata.replay samples
"""
import os
from datetime import date, datetime

from ...core import calendar as cal
from ..synthetic import SyntheticFeed
from .format import SUFFIX
from .library import BUNDLED_DIR
from .recorder import record, session_times

SYMBOLS = ["MKTX", "TECX", "NVLX", "ORBT", "AUTO", "FNBK", "ENRG"]
STEP_SECONDS = 5.0

SAMPLES = [
    # id, date, seed, note
    ("sample-2026-03-13", date(2026, 3, 13), 11,
     "A Friday with weekly options expiring at the close. Simulated data from the synthetic feed (seed 11)."),
    ("sample-2025-11-28", date(2025, 11, 28), 12,
     "The day after Thanksgiving: the market closes early at 1:00 pm. Simulated data (seed 12)."),
    ("sample-2026-06-17", date(2026, 6, 17), 13,
     "An ordinary Wednesday. Simulated data from the synthetic feed (seed 13)."),
]


def build_sample(sid: str, day: date, seed: int, note: str, out_dir: str = BUNDLED_DIR,
                 symbols=None, step: float = STEP_SECONDS) -> dict:
    open_ts = datetime.combine(day, cal.OPEN, tzinfo=cal.NY).timestamp()
    feed = SyntheticFeed(seed=seed, now=open_ts, history_days=260, always_open=False)
    times = session_times(day, step)
    return record(feed, os.path.join(out_dir, sid + SUFFIX), times, end=cal.close_ts(day),
                  symbols=list(symbols or SYMBOLS), label=f"Simulated session, {day.strftime('%a %d %b %Y')}",
                  description=note, source=f"synthetic feed, seed {seed}", simulated=True, fresh_day=True)


def build_all(out_dir: str = BUNDLED_DIR) -> list:
    os.makedirs(out_dir, exist_ok=True)
    return [build_sample(sid, d, seed, note, out_dir) for sid, d, seed, note in SAMPLES]
