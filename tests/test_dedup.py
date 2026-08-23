"""Test 3: replaying the same trade feed twice produces exactly one copy."""
from conftest import make_trade

from copybot.db import Database


def _feed():
    """Includes the real-world case that broke naive dedup: one transaction
    hash carrying two different fills."""
    return [
        make_trade(token_id="TOK1", price=0.05, shares=19.21, tx="0xsame", ts=1_786_993_561),
        make_trade(token_id="TOK1", price=0.05, shares=3.16, tx="0xsame", ts=1_786_993_561),
        make_trade(token_id="TOK2", price=0.30, shares=10.0, tx="0xother", ts=1_786_993_600),
    ]


def test_replaying_feed_inserts_each_trade_once(db):
    first = sum(db.record_processed(t, "copied") for t in _feed())
    second = sum(db.record_processed(t, "copied") for t in _feed())
    assert first == 3
    assert second == 0


def test_same_tx_hash_with_different_fills_is_not_collapsed(db):
    a, b, _ = _feed()
    assert a.tx_hash == b.tx_hash
    assert a.trade_key != b.trade_key
    db.record_processed(a, "copied")
    assert not db.has_processed(b.trade_key)


def test_dedup_survives_reopening_the_database(tmp_path):
    path = tmp_path / "dedup.sqlite3"
    d1 = Database(path, 150.0)
    for t in _feed():
        d1.record_processed(t, "copied")
    keys = d1.processed_keys()
    d1.close()

    d2 = Database(path, 150.0)
    assert d2.processed_keys() == keys
    assert sum(d2.record_processed(t, "copied") for t in _feed()) == 0
    d2.close()
