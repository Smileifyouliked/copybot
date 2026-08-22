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


# --- 404 on a single book is a state, not an error -------------------------

class _Resp:
    def __init__(self, status, text="", payload=None):
        self.status_code = status
        self.text = text
        self.headers: dict = {}
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


def test_404_on_get_book_returns_an_empty_book(client, monkeypatch):
    """CLOB /book 404s for a resolved token -- the single-fetch counterpart of
    POST /books omitting it. Normal state, not an error."""
    calls = []
    monkeypatch.setattr(client._client, "request",
                        lambda m, u, **k: (calls.append(u), _Resp(404, "not found"))[1])
    monkeypatch.setattr("time.sleep", lambda s: None)

    book = client.get_book("TOK_RESOLVED")
    assert book.is_empty
    assert book.token_id == "TOK_RESOLVED"
    assert len(calls) == 1, f"a permanent 404 was retried {len(calls)} times"


def test_500_is_still_retried(client, monkeypatch):
    calls = []
    monkeypatch.setattr(client._client, "request",
                        lambda m, u, **k: (calls.append(u), _Resp(503, "down"))[1])
    monkeypatch.setattr("time.sleep", lambda s: None)
    with pytest.raises(Exception):
        client.get_activity("0xabc")
    assert len(calls) == client.max_retries


def test_other_4xx_is_not_retried(client, monkeypatch):
    calls = []
    monkeypatch.setattr(client._client, "request",
                        lambda m, u, **k: (calls.append(u), _Resp(400, "bad request"))[1])
    monkeypatch.setattr("time.sleep", lambda s: None)
    with pytest.raises(Exception):
        client.get_activity("0xabc")
    assert len(calls) == 1


# --- gamma hides resolved markets unless asked ----------------------------

def test_get_markets_asks_for_closed_markets_too(client, monkeypatch):
    """gamma defaults to open-only. Asking once makes every resolution
    invisible, so nothing ever settles and positions accumulate forever."""
    calls = []

    def fake(method, url, **kwargs):
        params = dict(kwargs.get("params") or [])
        calls.append(params.get("closed"))
        if params.get("closed") == "true":
            return [{"conditionId": "0xclosed", "question": "done",
                     "clobTokenIds": '["T1","T2"]', "outcomes": '["Yes","No"]',
                     "outcomePrices": '["1","0"]', "closed": True,
                     "acceptingOrders": False, "feesEnabled": False}]
        return [{"conditionId": "0xopen", "question": "live",
                 "clobTokenIds": '["T3","T4"]', "outcomes": '["Yes","No"]',
                 "outcomePrices": '["0.4","0.6"]', "closed": False,
                 "acceptingOrders": True, "feesEnabled": False}]

    monkeypatch.setattr(client, "_request", fake)
    metas = client.get_markets(["0xopen", "0xclosed"])

    assert None in calls and "true" in calls, "both resolution states must be requested"
    assert set(metas) == {"0xopen", "0xclosed"}
    assert metas["0xclosed"].closed is True
    assert metas["0xclosed"].settlement_value("T1") == 1.0
    assert metas["0xopen"].closed is False


def test_resolved_market_is_settleable_after_the_fix(client, monkeypatch):
    monkeypatch.setattr(client, "_request", lambda m, u, **k: (
        [{"conditionId": "0xc", "question": "q", "clobTokenIds": '["WIN","LOSE"]',
          "outcomes": '["Yes","No"]', "outcomePrices": '["1","0"]', "closed": True,
          "acceptingOrders": False, "feesEnabled": False}]
        if dict(k.get("params") or []).get("closed") == "true" else []))
    meta = client.get_markets(["0xc"])["0xc"]
    assert meta.settlement_value("WIN") == 1.0
    assert meta.settlement_value("LOSE") == 0.0
