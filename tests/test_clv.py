"""Closing-line capture and the metrics built on it.

CLV is the primary metric, and it depends on capturing a price from a book that
goes empty the instant a market resolves. These tests pin the behaviour that
keeps it meaningful.
"""
import pytest

from conftest import FakeClient, FakeExecutor, make_book, make_meta, make_trade, tweak

from copybot.analytics import clv_summary, exit_path_breakdown, expected_vs_actual_winners
from copybot.models import MarkSource
from copybot.strategy import Strategy

def deep():
    """A fresh dict every call. Sharing one module-level dict let a test that
    swapped in an empty book poison every test that ran after it."""
    return {"TOK1": make_book(asks=[(0.20, 10_000)], bids=[(0.19, 10_000)])}


def build(db, single_cfg, books=None, metas=None, now=1_787_000_000):
    ex = FakeExecutor(books=books)
    cl = FakeClient(books=books, metas=metas)
    return Strategy(single_cfg, db, ex, cl, clock=lambda: now), ex, cl


def open_one(db, single_cfg, books=None, metas=None, now=1_787_000_000):
    books = deep() if books is None else books
    s, ex, cl = build(db, single_cfg, books=books, metas=metas or {"0xcond1": make_meta()}, now=now)
    s.process_trades([make_trade(price=0.20, ts=now)])
    return s, cl, db.open_positions()[0]["id"]


def test_marking_captures_bid_ask_and_spread(db, single_cfg):
    books = {"TOK1": make_book(asks=[(0.30, 500)], bids=[(0.24, 500)])}
    s, cl, pos_id = open_one(db, single_cfg, books=books)
    s.mark_positions()
    pos = db.get_position(pos_id)
    assert pos["last_mark_source"] == MarkSource.BOOK_MID.value
    assert pos["last_mark_price"] == pytest.approx(0.27)
    assert pos["last_mark_bid"] == pytest.approx(0.24)
    assert pos["last_mark_ask"] == pytest.approx(0.30)
    assert pos["last_mark_spread"] == pytest.approx(0.06)
    assert pos["closing_line_captured"] == 1
    assert pos["closing_line_spread"] == pytest.approx(0.06)


def test_empty_book_falls_back_to_gamma_never_to_zero(db, single_cfg):
    s, cl, pos_id = open_one(db, single_cfg)
    cl.books["TOK1"] = make_book(asks=[], bids=[])
    cl.metas["0xcond1"] = make_meta(prices=(0.42, 0.58))
    s.mark_positions()
    pos = db.get_position(pos_id)
    assert pos["last_mark_source"] == MarkSource.GAMMA_OUTCOME_PRICE.value
    assert pos["last_mark_price"] == pytest.approx(0.42)


def test_gamma_price_never_becomes_the_closing_line(db, single_cfg):
    """A resolved market's gamma price is 0 or 1 -- that is the outcome, not a
    line. Using it would make CLV a restatement of the result."""
    books = {"TOK1": make_book(asks=[(0.30, 500)], bids=[(0.24, 500)])}
    s, cl, pos_id = open_one(db, single_cfg, books=books)
    s.mark_positions()  # good book capture -> closing line 0.27

    cl.books["TOK1"] = make_book(asks=[], bids=[])
    cl.metas["0xcond1"] = make_meta(prices=(1.0, 0.0), closed=True)
    s.mark_positions()  # empty book, gamma says 1.0

    pos = db.get_position(pos_id)
    assert pos["last_mark_price"] == pytest.approx(1.0)
    assert pos["closing_line_price"] == pytest.approx(0.27), "closing line must not follow gamma"


def test_everything_empty_falls_back_to_last_known_then_entry(db, single_cfg):
    s, cl, pos_id = open_one(db, single_cfg)
    cl.books["TOK1"] = make_book(asks=[], bids=[])
    cl.metas = {}
    s.mark_positions()
    pos = db.get_position(pos_id)
    assert pos["last_mark_price"] is not None
    assert pos["last_mark_price"] > 0
    assert pos["last_mark_source"] in (MarkSource.LAST_KNOWN.value, MarkSource.ENTRY_PRICE.value)


def test_clv_is_frozen_at_resolution_with_its_age(db, single_cfg):
    books = {"TOK1": make_book(asks=[(0.20, 10_000)], bids=[(0.19, 10_000)])}
    s, cl, pos_id = open_one(db, single_cfg, books=books, now=1_787_000_000)
    s.mark_positions()

    later = Strategy(single_cfg, db, s.executor, cl, clock=lambda: 1_787_000_900)
    cl.books["TOK1"] = make_book(asks=[], bids=[])
    cl.metas["0xcond1"] = make_meta(prices=(1.0, 0.0), closed=True)
    later.check_resolutions()

    pos = db.get_position(pos_id)
    assert pos["status"] == "closed"
    assert pos["clv_abs"] == pytest.approx(pos["closing_line_price"] - pos["our_avg_fill"])
    assert pos["clv_pct"] == pytest.approx(pos["clv_abs"] / pos["our_avg_fill"] * 100)
    assert pos["closing_line_age_seconds"] == 900


def test_capture_failure_is_counted_not_hidden(db, single_cfg):
    """A position that resolves before any successful book mark has no closing
    line. That must show up as a failure, not as a silent zero."""
    s, cl, pos_id = open_one(db, single_cfg)
    cl.books["TOK1"] = make_book(asks=[], bids=[])
    cl.metas["0xcond1"] = make_meta(prices=(1.0, 0.0), closed=True)
    s.check_resolutions()

    pos = db.get_position(pos_id)
    assert pos["closing_line_captured"] == 0
    assert pos["clv_pct"] is None
    summary = clv_summary(db, single_cfg.clv_max_spread)
    assert summary["failures"] == 1
    assert summary["capture_rate"] == 0.0


def test_wide_spread_captures_are_separated_from_clean_ones(db, single_cfg):
    books = {"TOK1": make_book(asks=[(0.50, 500)], bids=[(0.05, 500)])}  # 45c spread
    s, cl, pos_id = open_one(db, single_cfg, books=books)
    s.mark_positions()
    cl.metas["0xcond1"] = make_meta(prices=(1.0, 0.0), closed=True)
    cl.books["TOK1"] = make_book(asks=[], bids=[])
    s.check_resolutions()

    summary = clv_summary(db, single_cfg.clv_max_spread)
    assert summary["captured"] == 1
    assert summary["wide_spread"] == 1
    assert summary["clean"] == 0


def test_near_close_tightens_the_marking_interval(db, single_cfg):
    import datetime

    now = 1_787_000_000  # 2026-08-17T20:53:20Z

    def iso(offset_seconds):
        return datetime.datetime.fromtimestamp(
            now + offset_seconds, datetime.timezone.utc
        ).isoformat().replace("+00:00", "Z")

    metas = {"0xcond1": make_meta(end_date=iso(-600))}  # already past
    s, _, cl = build(db, single_cfg, books=deep(), metas=metas, now=now)
    s.process_trades([make_trade(price=0.20, ts=now)])
    assert s.next_mark_interval() == single_cfg.mark_interval_seconds

    cl.metas["0xcond1"] = make_meta(end_date=iso(600))  # 10 min out -> tighten
    assert s.next_mark_interval() == single_cfg.mark_interval_near_close_seconds

    cl.metas["0xcond1"] = make_meta(end_date=iso(5 * 86400))  # days out
    assert s.next_mark_interval() == single_cfg.mark_interval_seconds


# --- Expected vs actual winners -------------------------------------------

def test_expected_winners_is_the_sum_of_entry_prices(db, single_cfg):
    """Under the null, a copy entered at 5c wins 5% of the time."""
    with db.tx() as conn:
        for i in range(60):
            db.insert_position(
                conn, token_id=f"T{i}", condition_id="0xc", question="Q",
                outcome="Yes", outcome_index=0, status="closed", opened_ts=1,
                closed_ts=2, shares=60.0, shares_opened=60.0, cost_basis_usd=0.0,
                cost_basis_opened=3.0, our_avg_fill=0.05, entry_band="0-10c",
                exit_path="resolution", proceeds_usd=(60.0 if i < 3 else 0.0),
                realised_pnl_usd=(57.0 if i < 3 else -3.0),
            )
    stats = expected_vs_actual_winners(db)
    assert stats["resolved"] == 60
    assert stats["expected"] == pytest.approx(3.0)          # 60 × 0.05
    assert stats["sd"] == pytest.approx((60 * 0.05 * 0.95) ** 0.5)
    assert stats["sd"] == pytest.approx(1.6882, abs=1e-4)   # the ±1.7 you cited
    assert stats["actual"] == 3
    assert abs(stats["z"]) < 1
    assert "too early" in stats["verdict"]


def test_four_winners_out_of_sixty_is_still_noise(db, single_cfg):
    """A real edge would show ~4. That must NOT read as success."""
    with db.tx() as conn:
        for i in range(60):
            db.insert_position(
                conn, token_id=f"T{i}", condition_id="0xc", question="Q",
                outcome="Yes", outcome_index=0, status="closed", opened_ts=1,
                closed_ts=2, shares=60.0, shares_opened=60.0, cost_basis_usd=0.0,
                cost_basis_opened=3.0, our_avg_fill=0.05, entry_band="0-10c",
                exit_path="resolution", proceeds_usd=(60.0 if i < 4 else 0.0),
            )
    stats = expected_vs_actual_winners(db)
    assert stats["actual"] == 4
    assert stats["z"] < 1
    assert "noise" in stats["verdict"] or "too early" in stats["verdict"]


def test_no_resolved_bets_reports_cleanly(db, single_cfg):
    stats = expected_vs_actual_winners(db)
    assert stats["resolved"] == 0
    assert stats["verdict"] == "no resolved bets yet"


def test_exit_path_breakdown_separates_the_two_products(db, single_cfg):
    with db.tx() as conn:
        db.insert_position(conn, token_id="A", condition_id="0xc", question="Q",
                           outcome="Yes", outcome_index=0, status="closed", opened_ts=1,
                           closed_ts=2, shares=0.0, shares_opened=60.0, cost_basis_usd=0.0,
                           cost_basis_opened=3.0, our_avg_fill=0.05, entry_band="0-10c",
                           exit_path="resolution", proceeds_usd=60.0, realised_pnl_usd=57.0)
        db.insert_position(conn, token_id="B", condition_id="0xc", question="Q",
                           outcome="Yes", outcome_index=0, status="closed", opened_ts=1,
                           closed_ts=2, shares=0.0, shares_opened=60.0, cost_basis_usd=0.0,
                           cost_basis_opened=3.0, our_avg_fill=0.05, entry_band="0-10c",
                           exit_path="mirrored_sell", proceeds_usd=2.0, realised_pnl_usd=-1.0)
    rows = {r["bucket"]: r for r in exit_path_breakdown(db)}
    assert set(rows) == {"resolution", "mirrored_sell"}
    assert rows["resolution"]["pnl"] == pytest.approx(57.0)
    assert rows["mirrored_sell"]["pnl"] == pytest.approx(-1.0)
