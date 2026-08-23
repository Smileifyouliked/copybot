"""Ladder integrity: the walk, the pairing, and the aggregate that misled.

The reported inversion (his_fill "$1.26" at +0.0% beside $1.00 at +24%) was not
a walk bug. Per-signal monotonicity held on all 28 fully-measured signals. The
aggregate table printed each rung's MEAN size beside its MEDIAN depth cost, and
his_fill's mean ($1.47) is 7x its median ($0.20) because a few of his fills are
large. So a row labelled $1.26 was reporting the cost of a rung that is
typically 20c.
"""
import pytest

from conftest import FakeClient, FakeExecutor, make_book, make_meta, make_trade

from copybot.analytics import capacity_curve, stopping_rules
from copybot.fees import FeeModel
from copybot.fills import (LadderInversionError, LadderRung,
                           assert_monotonic_depth_cost, size_ladder)
from copybot.models import BookLevel, OrderBook
from copybot.strategy import Strategy

NO_FEE = FeeModel(rate=0.0)


def book(asks=(), bids=(), min_size=5.0):
    return OrderBook(
        token_id="TOK1", condition_id="0xcond1",
        bids=sorted([BookLevel(p, s) for p, s in bids], key=lambda l: l.price, reverse=True),
        asks=sorted([BookLevel(p, s) for p, s in asks], key=lambda l: l.price),
        tick_size=0.001, min_order_size=min_size, timestamp_ms=0,
    )


def rung(label, usd, cost):
    return LadderRung(label=label, usd=usd, filled=True, shares=1.0, vwap=0.1,
                      depth_cost_pct=cost, levels_consumed=1, cleared_max_fill=True,
                      below_min_order_size=False, fee_usd=0.0, skip_reason=None)


# --- the assertion ---------------------------------------------------------

def test_inversion_raises_rather_than_warns():
    with pytest.raises(LadderInversionError) as exc:
        assert_monotonic_depth_cost([rung("$1", 1.0, 24.0), rung("$3", 3.0, 5.0)])
    assert "walk is wrong" in str(exc.value)


def test_monotonic_ladder_passes():
    assert_monotonic_depth_cost([
        rung("his_fill", 0.20, 0.0), rung("$1", 1.0, 5.0),
        rung("$3", 3.0, 20.0), rung("$10", 10.0, 60.0),
    ])


def test_equal_costs_at_different_sizes_are_fine():
    """A single deep level absorbs every rung at the same price."""
    assert_monotonic_depth_cost([rung("$1", 1.0, 0.0), rung("$10", 10.0, 0.0)])


def test_unmeasurable_rungs_do_not_reorder_the_check():
    unmeasured = LadderRung(label="$3", usd=3.0, filled=False, shares=0.0, vwap=0.0,
                            depth_cost_pct=None, levels_consumed=0,
                            cleared_max_fill=False, below_min_order_size=False,
                            fee_usd=0.0, skip_reason="book_empty",
                            unmeasurable_reason="no_asks_on_book")
    assert_monotonic_depth_cost([rung("$1", 1.0, 10.0), unmeasured, rung("$10", 10.0, 20.0)])


def test_real_walk_is_always_monotonic_across_many_shapes():
    """The property the assertion guards, exercised on the real walk."""
    shapes = [
        [(0.10, 20), (0.20, 20), (0.40, 200)],
        [(0.02, 5), (0.03, 10), (0.05, 40), (0.30, 1000)],
        [(0.05, 3)],                       # too thin for most rungs
        [(0.50, 10_000)],                  # one deep level
        [(0.01, 1), (0.99, 10_000)],       # pathological
    ]
    rungs = [("his_fill", 0.15), ("$1", 1.0), ("his_position", 2.4),
             ("$3", 3.0), ("$10", 10.0)]
    for shape in shapes:
        result = size_ladder(book(asks=shape), rungs, FeeModel(rate=0.05),
                             max_fill_price=1.0)
        assert_monotonic_depth_cost(result)  # raises if violated


def test_null_depth_cost_carries_a_reason():
    result = size_ladder(book(asks=[]), [("$3", 3.0)], NO_FEE, max_fill_price=0.50)
    assert result[0].depth_cost_pct is None
    assert result[0].unmeasurable_reason == "no_asks_on_book"


# --- the aggregate that misled --------------------------------------------

def _shadow_row(db, conn, trade_key, label, usd, cost):
    conn.execute(
        """INSERT INTO shadow_fills
           (trade_key, token_id, seen_ts, rung_label, rung_usd, filled, shares,
            vwap, depth_cost_pct, levels_consumed, cleared_max_fill,
            below_min_order_size, fee_usd)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (trade_key, "TOK1", 1, label, usd, 1, 10.0, 0.1, cost, 1, 1, 0, 0.0),
    )


def test_representative_size_is_the_median_not_the_mean(db):
    """The exact regression. his_fill is 0.20 nine times and 12.00 once:
    mean 1.38, median 0.20. Reporting the mean beside a median cost is what
    made a 20c rung look like a $1.26 rung."""
    sizes = [0.20] * 9 + [12.00]
    with db.tx() as conn:
        for i, size in enumerate(sizes):
            _shadow_row(db, conn, f"sig{i}", "his_fill", size, 0.0)
            _shadow_row(db, conn, f"sig{i}", "$1", 1.0, 24.0)
            _shadow_row(db, conn, f"sig{i}", "$3", 3.0, 70.0)

    rows = {r["rung"]: r for r in capacity_curve(db)}
    assert rows["his_fill"]["median_usd"] == pytest.approx(0.20)
    assert sum(sizes) / len(sizes) == pytest.approx(1.38)   # the misleading number
    assert rows["his_fill"]["median_usd"] != pytest.approx(1.38)
    assert rows["his_fill"]["min_usd"] == pytest.approx(0.20)
    assert rows["his_fill"]["max_usd"] == pytest.approx(12.00)


def test_curve_is_ordered_by_median_size(db):
    sizes = [0.20] * 9 + [12.00]
    with db.tx() as conn:
        for i, size in enumerate(sizes):
            _shadow_row(db, conn, f"sig{i}", "his_fill", size, 0.0)
            _shadow_row(db, conn, f"sig{i}", "$1", 1.0, 24.0)
            _shadow_row(db, conn, f"sig{i}", "$3", 3.0, 70.0)
    curve = capacity_curve(db)
    assert [r["rung"] for r in curve] == ["his_fill", "$1", "$3"]
    costs = [r["median_depth_cost_pct"] for r in curve]
    assert costs == sorted(costs), "aggregate reads as an inversion"


def test_aggregation_uses_paired_signals_only(db):
    """A signal where one rung could not be measured is excluded entirely,
    rather than dropping that rung and letting medians span different sets."""
    with db.tx() as conn:
        _shadow_row(db, conn, "good", "$1", 1.0, 10.0)
        _shadow_row(db, conn, "good", "$3", 3.0, 20.0)
        # 'partial' has a $1 measurement but the $3 rung was unmeasurable.
        _shadow_row(db, conn, "partial", "$1", 1.0, 99.0)
        conn.execute(
            """INSERT INTO shadow_fills
               (trade_key, token_id, seen_ts, rung_label, rung_usd, filled, shares,
                vwap, depth_cost_pct, levels_consumed, cleared_max_fill,
                below_min_order_size, fee_usd, unmeasurable_reason)
               VALUES ('partial','TOK1',1,'$3',3.0,0,0,0,NULL,0,0,0,0,'no_asks_on_book')"""
        )
    rows = {r["rung"]: r for r in capacity_curve(db)}
    assert rows["$1"]["n"] == 1, "the unpaired signal leaked into the $1 median"
    assert rows["$1"]["median_depth_cost_pct"] == pytest.approx(10.0)
    assert rows["$1"]["paired_signals"] == 1


def test_every_rung_reports_the_same_signal_count(db):
    with db.tx() as conn:
        for i in range(5):
            for label, usd, cost in (("$1", 1.0, 5.0), ("$3", 3.0, 15.0), ("$10", 10.0, 40.0)):
                _shadow_row(db, conn, f"s{i}", label, usd, cost)
    counts = {r["n"] for r in capacity_curve(db)}
    assert len(counts) == 1, "rung medians span different numbers of signals"


def test_end_to_end_ladder_is_monotonic_on_a_real_signal(db, cfg):
    books = {"TOK1": make_book(asks=[(0.05, 40), (0.10, 40), (0.40, 10_000)],
                               bids=[(0.04, 500)])}
    ex = FakeExecutor(books=books)
    cl = FakeClient(books=books, metas={"0xcond1": make_meta()})
    s = Strategy(cfg, db, ex, cl, clock=lambda: 1_787_000_000)
    s.process_trades([make_trade(price=0.05, shares=4.0, ts=1_787_000_000)])

    rows = db._all("SELECT rung_usd, depth_cost_pct FROM shadow_fills "
                   "WHERE depth_cost_pct IS NOT NULL ORDER BY rung_usd")
    costs = [r["depth_cost_pct"] for r in rows]
    assert costs == sorted(costs)


# --- stopping rule ---------------------------------------------------------

def test_stopping_rules_wait_for_enough_data(db, cfg):
    rules = stopping_rules(db, cfg)
    assert all(r["status"] == "waiting" for r in rules["kill"])
    assert rules["breaches"] == 0
    assert rules["go_live"]["ready"] is False


def test_depth_breach_fires_once_there_are_enough_signals(db, cfg):
    with db.tx() as conn:
        for i in range(cfg.kill_depth_min_signals):
            _shadow_row(db, conn, f"s{i}", "$1", 1.0, 30.0)
            _shadow_row(db, conn, f"s{i}", "$3", 3.0, 80.0)
    rules = stopping_rules(db, cfg)
    statuses = {r["name"]: r["status"] for r in rules["kill"]}
    assert statuses["Depth cost at $3"] == "breach"
    assert statuses["Depth cost at $1"] == "breach"
    assert rules["breaches"] == 2


def test_depth_below_threshold_reads_clear(db, cfg):
    with db.tx() as conn:
        for i in range(cfg.kill_depth_min_signals):
            _shadow_row(db, conn, f"s{i}", "$1", 1.0, 2.0)
            _shadow_row(db, conn, f"s{i}", "$3", 3.0, 8.0)
    rules = stopping_rules(db, cfg)
    statuses = {r["name"]: r["status"] for r in rules["kill"]}
    assert statuses["Depth cost at $3"] == "ok"
    assert rules["breaches"] == 0


def test_one_signal_short_of_the_minimum_still_waits(db, cfg):
    with db.tx() as conn:
        for i in range(cfg.kill_depth_min_signals - 1):
            _shadow_row(db, conn, f"s{i}", "$3", 3.0, 80.0)
    rules = stopping_rules(db, cfg)
    statuses = {r["name"]: r["status"] for r in rules["kill"]}
    assert statuses["Depth cost at $3"] == "waiting"


def test_pnl_verdict_needs_seven_hundred_resolved(db, cfg):
    rules = stopping_rules(db, cfg)
    assert rules["pnl_verdict"]["required"] == 700
    assert rules["pnl_verdict"]["ready"] is False


def test_go_live_gate_is_not_green_before_anything_is_measured(db, cfg):
    """'No breaches' is not 'cleared'. A fresh database has zero breaches and
    must still not show a green tick."""
    rules = stopping_rules(db, cfg)
    assert rules["breaches"] == 0
    assert rules["go_live"]["kills_clear"] is False
    assert rules["go_live"]["still_collecting"] == len(rules["kill"])
    assert rules["go_live"]["ready"] is False


# --- the fee-fallback warning must stay meaningful -------------------------

def test_empty_book_does_not_trip_the_fee_fallback_warning(cfg, caplog):
    """Every resolved market returns an empty book. Warning on each one buries
    the real fallbacks this warning exists to surface -- 103 false positives in
    one short run, before this was fixed."""
    import logging

    from copybot.executor import PaperExecutor

    class Client:
        def get_book(self, token_id):
            return OrderBook(token_id=token_id, bids=[], asks=[],
                             tick_size=0.001, min_order_size=5.0)

        def fee_rate_for(self, condition_id, fallback):
            raise AssertionError("must not look up a fee for an empty book")

    ex = PaperExecutor(cfg, Client())
    with caplog.at_level(logging.WARNING):
        result = ex.buy("TOK", 3.00)
    assert not result.filled
    assert "FEE FALLBACK" not in caplog.text


def test_real_missing_condition_id_still_warns(cfg, caplog):
    """A book with depth but no conditionId is a genuine problem and must
    still be loud."""
    import logging

    from copybot.executor import PaperExecutor

    class Client:
        def get_book(self, token_id):
            return OrderBook(token_id=token_id, condition_id="",
                             bids=[BookLevel(0.19, 500)], asks=[BookLevel(0.20, 500)],
                             tick_size=0.001, min_order_size=5.0)

        def fee_rate_for(self, condition_id, fallback):
            return fallback, True

    ex = PaperExecutor(cfg, Client())
    with caplog.at_level(logging.WARNING):
        result = ex.buy("TOK", 3.00)
    assert result.filled
    assert "FEE FALLBACK" in caplog.text
    assert result.fee_rate_was_fallback
