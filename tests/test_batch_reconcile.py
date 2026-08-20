"""Regression: batch responses must be keyed by ID, never zipped by index.

Polymarket returns HTTP 200 and a SHORTER list when it drops items -- gamma on
unknown condition_ids, POST /books on resolved tokens. Index-zipping a short
response attributes one token's book to a different token, which marks a
position at the wrong price and makes equity silently wrong.
"""
import pytest

from copybot.batch import BatchMismatch, reconcile_batch
from copybot.polymarket import PolymarketClient


def _book(token_id, best_ask, best_bid):
    return {
        "asset_id": token_id,
        "bids": [{"price": str(best_bid), "size": "500"}],
        "asks": [{"price": str(best_ask), "size": "500"}],
        "tick_size": "0.001", "min_order_size": "5", "timestamp": "1787000000000",
    }


@pytest.fixture
def client(monkeypatch):
    c = PolymarketClient()
    yield c
    c.close()


def test_omitted_token_does_not_shift_the_others(client, monkeypatch):
    """TOK_B is resolved and omitted. TOK_C must keep its own book, not B's."""
    requested = ["TOK_A", "TOK_B", "TOK_C"]
    response = [_book("TOK_A", 0.10, 0.09), _book("TOK_C", 0.80, 0.79)]
    monkeypatch.setattr(client, "_request", lambda *a, **k: response)

    books = client.get_books(requested)

    assert books["TOK_A"].best_ask == pytest.approx(0.10)
    assert books["TOK_C"].best_ask == pytest.approx(0.80)
    assert books["TOK_B"].is_empty

    # Teeth: index-zipping would have handed TOK_C's 0.80 book to TOK_B.
    naive = dict(zip(requested, response))
    assert float(naive["TOK_B"]["asks"][0]["price"]) == pytest.approx(0.80)
    assert books["TOK_B"].best_ask != 0.80


def test_response_order_is_never_trusted(client, monkeypatch):
    requested = ["TOK_A", "TOK_B", "TOK_C"]
    monkeypatch.setattr(client, "_request", lambda *a, **k: [
        _book("TOK_C", 0.80, 0.79), _book("TOK_A", 0.10, 0.09), _book("TOK_B", 0.45, 0.44),
    ])
    books = client.get_books(requested)
    assert books["TOK_A"].best_ask == pytest.approx(0.10)
    assert books["TOK_B"].best_ask == pytest.approx(0.45)
    assert books["TOK_C"].best_ask == pytest.approx(0.80)


def test_unrequested_token_is_not_absorbed(client, monkeypatch):
    monkeypatch.setattr(client, "_request", lambda *a, **k: [
        _book("TOK_A", 0.10, 0.09), _book("TOK_INTRUDER", 0.99, 0.98),
    ])
    books = client.get_books(["TOK_A", "TOK_B"])
    assert "TOK_INTRUDER" not in books
    assert books["TOK_A"].best_ask == pytest.approx(0.10)
    assert books["TOK_B"].is_empty


def test_empty_response_yields_empty_books_not_wrong_ones(client, monkeypatch):
    monkeypatch.setattr(client, "_request", lambda *a, **k: [])
    books = client.get_books(["TOK_A", "TOK_B"])
    assert set(books) == {"TOK_A", "TOK_B"}
    assert all(b.is_empty for b in books.values())


def test_missing_book_carries_its_own_token_id(client, monkeypatch):
    """A placeholder must never be mistakable for another token's book."""
    monkeypatch.setattr(client, "_request", lambda *a, **k: [])
    books = client.get_books(["TOK_A"])
    assert books["TOK_A"].token_id == "TOK_A"


def test_gamma_drops_unknown_condition_ids(client, monkeypatch):
    monkeypatch.setattr(client, "_request", lambda *a, **k: [{
        "conditionId": "0xreal", "question": "Q",
        "clobTokenIds": '["T1", "T2"]', "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.30", "0.70"]', "closed": False, "acceptingOrders": True,
        "feesEnabled": True, "feeType": "weather_fees", "feeSchedule": {"rate": 0.05},
    }])
    metas = client.get_markets(["0xreal", "0xbogus"])
    assert set(metas) == {"0xreal"}
    assert metas["0xreal"].price_for_token("T1") == pytest.approx(0.30)


def test_reconcile_batch_reports_missing_and_unexpected():
    by_id, missing, unexpected = reconcile_batch(
        ["a", "b", "c"],
        [{"id": "a"}, {"id": "c"}, {"id": "zzz"}],
        key_of=lambda x: x["id"],
        what="test",
    )
    assert set(by_id) == {"a", "c"}
    assert missing == ["b"]
    assert unexpected == ["zzz"]


def test_reconcile_batch_strict_raises_on_unrequested_ids():
    with pytest.raises(BatchMismatch):
        reconcile_batch(["a"], [{"id": "zzz"}], key_of=lambda x: x["id"],
                        what="test", strict=True)
