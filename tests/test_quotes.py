"""Settling the tape's `side` convention from recorded quotes."""
import pytest

from copybot.quotes import Verdict, classify, nearest_quote


def seed_mark(db, token, ts, bid, ask):
    with db.tx() as conn:
        pos = db.insert_position(
            conn, token_id=token, condition_id="0xc", question="q", outcome="Yes",
            outcome_index=0, status="open", opened_ts=ts, shares=1.0,
            shares_opened=1.0, cost_basis_usd=1.0, cost_basis_opened=1.0,
            our_avg_fill=0.1,
        ) if not db.open_position_for_token(token) else db.open_position_for_token(token)["id"]
        db.record_mark(conn, pos, token, ts, bid, ask, (bid + ask) / 2, ask - bid,
                       "book_mid")


class FakeTape:
    def __init__(self, trades):
        self.trades = trades

    def get_trades(self, condition_id, limit=200, offset=0):
        return self.trades


def test_verdict_reads_taker_side():
    v = Verdict(taker_side_evidence=95, maker_side_evidence=5, classified=100, inspected=120)
    assert v.convention == "taker"
    assert "TAKER" in v.summary()


def test_verdict_reads_maker_side():
    v = Verdict(taker_side_evidence=4, maker_side_evidence=96, classified=100, inspected=120)
    assert v.convention == "maker"
    assert "MAKER" in v.summary()


def test_verdict_refuses_to_call_a_small_sample():
    """n=1 is not evidence. That is what sent the first attempt wrong."""
    v = Verdict(taker_side_evidence=1, maker_side_evidence=0, classified=1, inspected=3)
    assert v.convention == "insufficient"
    assert "not enough" in v.summary()


def test_verdict_reports_ambiguity_rather_than_picking():
    v = Verdict(taker_side_evidence=52, maker_side_evidence=48, classified=100, inspected=140)
    assert v.convention == "ambiguous"
    assert "do not rely" in v.summary()


def test_stale_quotes_are_not_used(db):
    seed_mark(db, "TOK", 1_787_000_000, 0.10, 0.12)
    assert nearest_quote(db, "TOK", 1_787_000_010) is not None
    assert nearest_quote(db, "TOK", 1_787_009_999) is None, "a 2h-old quote proves nothing"


def test_quotes_after_the_print_are_not_used(db):
    seed_mark(db, "TOK", 1_787_000_500, 0.10, 0.12)
    assert nearest_quote(db, "TOK", 1_787_000_100) is None


def test_classification_tallies_against_the_live_quote(db):
    ts = 1_787_000_000
    seed_mark(db, "TOK", ts, 0.10, 0.12)
    trades = ([{"asset": "TOK", "timestamp": ts + 5, "price": "0.12", "size": "1",
                "side": "BUY"}] * 40
              + [{"asset": "TOK", "timestamp": ts + 6, "price": "0.10", "size": "1",
                  "side": "SELL"}] * 40)
    v = classify(db, FakeTape(trades))
    assert v.taker_side_evidence == 80
    assert v.maker_side_evidence == 0
    assert v.convention == "taker"


def test_prints_inside_the_spread_are_ignored(db):
    ts = 1_787_000_000
    seed_mark(db, "TOK", ts, 0.10, 0.20)
    trades = [{"asset": "TOK", "timestamp": ts + 5, "price": "0.15", "size": "1",
               "side": "BUY"}] * 50
    v = classify(db, FakeTape(trades))
    assert v.classified == 0, "a mid-spread print identifies neither side"
