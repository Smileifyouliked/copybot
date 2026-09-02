"""Three defects the first 21 hours of live running exposed.

None were found by reading the code. All three came out of a bundle exported
from a real run: a fill rate that could not be attributed, a log in which 90%
of lines were one warning, and HTTP 429s traced back to fetching the same tape
once per order instead of once per market.
"""
import dataclasses

import pytest

from conftest import FakeClient, FakeExecutor, make_book, make_meta, make_trade

from copybot.batch import assert_complete_page
from copybot.strategy import Strategy

T0 = 1_787_000_000


def print_(price, size, ts, asset="TOK1", side="SELL", wallet="0xsomeone"):
    return {"price": price, "size": size, "timestamp": ts, "side": side,
            "proxyWallet": wallet, "asset": asset}


# --- 1. how we got in must be attributable --------------------------------

def build(db, cfg, books, tape=(), now=T0):
    ex = FakeExecutor(books=books, tape=list(tape))
    cl = FakeClient(books=books, metas={"0xcond1": make_meta()})
    return Strategy(cfg, db, ex, cl, clock=lambda: now, run_id="run-B"), ex


@pytest.fixture
def limit_cfg(cfg):
    return dataclasses.replace(cfg, entry_mode="limit", stake_variants_usd=[3.00])


def test_a_resting_fill_is_recorded_as_rested(db, limit_cfg):
    """Without this the headline ratio pools two different things: prices we
    waited for and prices the market handed us after it moved."""
    books = {"TOK1": make_book(asks=[(0.22, 100_000)], bids=[])}
    s, _ = build(db, limit_cfg, books, tape=[print_(0.20, 500.0, T0 + 30)])
    s.process_trades([make_trade(price=0.20, ts=T0)])
    s.clock = lambda: T0 + 60
    s.poll_resting_orders()

    pos = db.open_position_for_token("TOK1")
    assert pos["entry_path"] == "rested"
    row = [e for e in db.recent_executions(5) if e["side"] == "BUY"][0]
    assert row["entry_path"] == "rested"


def test_a_windfall_fill_is_recorded_as_crossed_inside(db, limit_cfg):
    """The book was already below his price, so we crossed. That is a different
    event from waiting at his price and must never be counted as one."""
    books = {"TOK1": make_book(asks=[(0.05, 100_000)], bids=[(0.04, 10_000)])}
    s, _ = build(db, limit_cfg, books)
    s.process_trades([make_trade(price=0.20, ts=T0)])

    pos = db.open_position_for_token("TOK1")
    assert pos["entry_path"] == "crossed_inside"
    assert pos["our_avg_fill"] < 0.20, "crossing inside pays better than his price"


def test_a_market_mode_fill_is_recorded_as_market(db, cfg):
    market = dataclasses.replace(cfg, entry_mode="market", stake_variants_usd=[3.00])
    books = {"TOK1": make_book(asks=[(0.20, 100_000)], bids=[(0.19, 100_000)])}
    s, _ = build(db, market, books)
    s.process_trades([make_trade(price=0.20, ts=T0)])
    assert db.open_position_for_token("TOK1")["entry_path"] == "market"


def test_the_two_paths_can_be_told_apart_in_one_run(db, limit_cfg):
    """The whole point: a run holding both kinds of fill must still answer
    'what did resting get us' separately from 'what did crossing get us'."""
    resting_book = {"TOK1": make_book(token_id="TOK1", asks=[(0.22, 100_000)], bids=[])}
    s, ex = build(db, limit_cfg, resting_book, tape=[print_(0.20, 500.0, T0 + 30)])
    s.process_trades([make_trade(token_id="TOK1", price=0.20, tx="0xa", ts=T0)])
    s.clock = lambda: T0 + 60
    s.poll_resting_orders()

    ex.books["TOK2"] = make_book(token_id="TOK2", asks=[(0.05, 100_000)],
                                 bids=[(0.04, 10_000)])
    s.process_trades([make_trade(token_id="TOK2", price=0.20, tx="0xb", ts=T0 + 60)])

    paths = {r["token_id"]: r["entry_path"] for r in db.open_positions()}
    assert paths == {"TOK1": "rested", "TOK2": "crossed_inside"}


# --- 2. the tape is fetched per market, not per order ---------------------

def test_orders_in_one_market_share_a_single_tape_fetch(db, limit_cfg):
    """406 orders fetching their own tape every 15s is what produced HTTP 429,
    and a rate limit at the bottom becomes lost signals at the top: the poll
    backs off, and his trades age past max_trade_age_seconds."""
    books = {"TOK1": make_book(asks=[(0.10, 100_000)], bids=[])}
    s, ex = build(db, limit_cfg, books)
    for i in range(3):
        s.process_trades([make_trade(price=0.05, tx=f"0x{i}", ts=T0 + i)])
    orders = db.open_resting()
    assert len(orders) > 1, "several orders resting into the same market"
    assert len({o["condition_id"] for o in orders}) == 1

    s.clock = lambda: T0 + 60
    s.poll_resting_orders()
    assert len(ex.polls) == len(orders), "every order is still advanced"
    assert ex.passes[0] == "begin" and ex.passes[-1] == "end", \
        "the pass is opened and closed, so a cached tape cannot leak between passes"


def test_the_cache_does_not_outlive_one_pass(cfg):
    """A tape cached across passes would freeze the fill model: an order can
    only ever fill on prints it has not seen yet."""
    from copybot.executor import PaperExecutor

    class CountingClient:
        def __init__(self):
            self.fetches = 0

        def get_trades(self, condition_id, limit=200):
            self.fetches += 1
            return []

        def fee_rate_for(self, condition_id, fallback):
            return fallback, False

    from copybot.limits import RestingOrder
    client = CountingClient()
    ex = PaperExecutor(cfg, client)
    order = RestingOrder(token_id="TOK1", condition_id="0xc", limit_price=0.2,
                         usd_budget=1.0, target_shares=5.0, placed_ts=T0,
                         expires_ts=T0 + 300, queue_ahead_shares=0.0,
                         his_price=0.2)

    ex.begin_poll()
    ex.poll_limit(order, T0 + 10)
    ex.poll_limit(order, T0 + 10)
    assert client.fetches == 1, "one market, one fetch within a pass"
    ex.end_poll()

    ex.begin_poll()
    ex.poll_limit(order, T0 + 20)
    assert client.fetches == 2, "a new pass must see new prints"
    ex.end_poll()


def test_polling_without_a_pass_still_works(cfg):
    """The cache is an optimisation, not a requirement. Nothing may depend on
    begin_poll having been called."""
    from copybot.executor import PaperExecutor
    from copybot.limits import RestingOrder

    class Client:
        def get_trades(self, condition_id, limit=200):
            return []

        def fee_rate_for(self, condition_id, fallback):
            return fallback, False

    ex = PaperExecutor(cfg, Client())
    order = RestingOrder(token_id="TOK1", condition_id="0xc", limit_price=0.2,
                         usd_budget=1.0, target_shares=5.0, placed_ts=T0,
                         expires_ts=T0 + 300, queue_ahead_shares=0.0,
                         his_price=0.2)
    assert ex.poll_limit(order, T0 + 10) is order


# --- 3. the truncation warning must mean something ------------------------

def test_a_full_page_we_have_already_seen_past_is_not_warned_about():
    """57,604 of 66,677 log lines in one 21-hour run were this warning. A
    guardrail that fires every 15 seconds forever is a place for a real warning
    to hide, not a guardrail."""
    rows = [{"i": n} for n in range(100)]
    assert assert_complete_page(rows, 100, what="x", covered=True) is False


def test_a_full_page_reaching_past_what_we_know_still_warns():
    """The failure it exists for is real: after an outage, a full page can hide
    trades we never saw, and his VWAP silently develops holes."""
    rows = [{"i": n} for n in range(100)]
    assert assert_complete_page(rows, 100, what="x", covered=False) is True


def test_a_short_page_is_never_a_warning():
    assert assert_complete_page([{"i": 1}], 100, what="x") is False


def test_the_engine_tells_the_client_what_it_has_seen(db, cfg):
    """The client cannot decide whether a page is covered without knowing what
    the caller has processed."""
    from copybot.engine import Engine
    from test_engine_wiring import StubClient, activity_row

    market = dataclasses.replace(cfg, entry_mode="market", stake_variants_usd=[3.00])
    books = {"TOK1": make_book(asks=[(0.20, 100_000)], bids=[(0.19, 100_000)])}
    cl = StubClient(rows=[activity_row()], books=books,
                    metas={"0xcond1": make_meta()})
    strat = Strategy(market, db, FakeExecutor(books=books), cl,
                     clock=lambda: T0, run_id="r")
    Engine(market, db, cl, strat).poll_once()
    assert cl.seen_keys_passed is not None, "the client must be told what we know"


# --- the split, once it can be made ---------------------------------------

def test_the_ratio_is_reported_per_path(db, limit_cfg):
    """A mean of 1.000 from resting and 0.100 from crossing pools to 0.55,
    which describes neither. The split has to be available."""
    resting_book = {"TOK1": make_book(token_id="TOK1", asks=[(0.22, 100_000)], bids=[])}
    s, ex = build(db, limit_cfg, resting_book, tape=[print_(0.20, 500.0, T0 + 30)])
    s.process_trades([make_trade(token_id="TOK1", price=0.20, tx="0xa", ts=T0)])
    s.clock = lambda: T0 + 60
    s.poll_resting_orders()

    ex.books["TOK2"] = make_book(token_id="TOK2", asks=[(0.02, 100_000)],
                                 bids=[(0.01, 10_000)])
    s.process_trades([make_trade(token_id="TOK2", price=0.20, tx="0xb", ts=T0 + 60)])

    by_path = {p["path"]: p for p in db.path_breakdown()}
    assert set(by_path) == {"rested", "crossed_inside"}
    assert by_path["rested"]["mean_ratio"] == pytest.approx(1.0, abs=1e-6), \
        "a resting fill is at his price by construction"
    assert by_path["crossed_inside"]["mean_ratio"] < 0.5, \
        "a crossed-inside fill is wherever the book had already moved to"

    pooled = db.vwap_ratio_stats()["mean"]
    assert by_path["crossed_inside"]["mean_ratio"] < pooled < by_path["rested"]["mean_ratio"], \
        "the pooled number sits between the two and describes neither"


def test_positions_from_before_the_column_existed_are_labelled(db, limit_cfg):
    """An older database has positions with no entry_path. They must appear as
    unrecorded rather than being silently dropped from the split."""
    books = {"TOK1": make_book(asks=[(0.20, 100_000)], bids=[(0.19, 100_000)])}
    s, _ = build(db, dataclasses.replace(limit_cfg, entry_mode="market"), books)
    s.process_trades([make_trade(price=0.20, ts=T0)])
    db.conn.execute("UPDATE positions SET entry_path = NULL")
    db.conn.commit()
    assert [p["path"] for p in db.path_breakdown()] == ["unrecorded"]
