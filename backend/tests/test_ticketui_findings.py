"""Backend problems found while building the order ticket UI (reported, not fixed in that stream).

Marked xfail(strict=True): each test fails today and will start passing (and so be flagged by pytest)
once the problem is fixed, at which point the marker should be removed.
"""
import pytest

from helpers import D, make_exchange


@pytest.mark.xfail(strict=True, reason="preview builds 'closeing' from f\"{which}ing\" with which='close'")
def test_on_close_preview_text_spells_closing_correctly():
    ex, _, _ = make_exchange()
    a = ex.create_account("t", "margin", D("100000")).id
    text = " ".join(ex.preview(a, {"symbol": "ABC", "side": "buy", "qty": 1, "type": "market_on_close"})["warnings"])
    assert "closeing" not in text and "at the closing price" in text
