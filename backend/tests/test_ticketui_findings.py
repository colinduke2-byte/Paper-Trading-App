"""Backend problems found while building the order ticket UI (reported, not fixed in that stream).

Both fixed in integration/wave2 (the first one was an xfail(strict=True) marker, now removed).
"""
import pytest

from helpers import D, make_exchange


def test_on_close_preview_text_spells_closing_correctly():
    ex, _, _ = make_exchange()
    a = ex.create_account("t", "margin", D("100000")).id
    text = " ".join(ex.preview(a, {"symbol": "ABC", "side": "buy", "qty": 1, "type": "market_on_close"})["warnings"])
    assert "closeing" not in text and "at the closing price" in text


def test_one_triggers_other_error_names_the_ticket_choice_not_only_the_api_field():
    from app.exchange import OrderRejected
    ex, _, _ = make_exchange()
    a = ex.create_account("t", "margin", D("100000")).id
    exits = [{"side": "sell", "type": "limit", "limit_price": "200"}, {"side": "sell", "type": "stop", "stop_price": "50"}]
    with pytest.raises(OrderRejected) as e:
        ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 1, "type": "limit", "limit_price": "100", "triggers": exits})
    assert "Cancel the others when one fills" in str(e.value)
    assert not str(e.value).endswith("set triggers_oco")
