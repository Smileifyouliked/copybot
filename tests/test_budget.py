"""Following him down, under a hard per-token budget.

He averages ~5 fills per token and lands well below his opener, so copying only
his first fill makes his VWAP unreachable by construction. The schedule splits
one budget across his successive fills. It must never add a second budget:
"total per token stays capped at $3, no exceptions".
"""
import pytest

from conftest import FakeClient, FakeExecutor, make_book, make_meta, make_trade, tweak

from copybot.models import SkipReason
from copybot.strategy import Strategy


def deep(price=0.20):
    return {"TOK1": make_book(asks=[(price, 100_000)], bids=[(price - 0.01, 100_000)])}


def build(db, cfg, books=None, now=1_787_000_000):
    ex = FakeExecutor(books=books or deep())
    cl = FakeClient(books=books or deep(), metas={"0xcond1": make_meta()})
    return Strategy(cfg, db, ex, cl, clock=lambda: now), ex, cl


def spend(db):
    return sum(-e["net_usd"] for e in db.recent_executions(50) if e["side"] == "BUY")


def budget(s, token="TOK1", price=0.20, book=None):
    """The cap this token was actually assigned.

    Under the shipped config the per-token budget is a stake variant, not a
    constant, so a test that hard-codes stake_per_copy_usd is testing a config
    nobody is running. Ask the strategy what it assigned instead."""
    return s.budget_for(token, book if book is not None else deep(price)[token], price)


# --- the hard cap ----------------------------------------------------------

def test_three_fills_never_exceed_one_budget(db, cfg):
    """Six of his fills at one price, one budget.

    What is left undeployed must be undeployed for a reason: less than one
    placeable order at that price. The budget is never rounded up to consume
    the remainder -- doing that would buy his opening price, which is the price
    the whole strategy is trying not to be stuck at. The remainder stays
    available for a cheaper fill, where the same 5-share floor costs less.
    """
    s, _, _ = build(db, cfg)
    cap = budget(s)
    for i in range(6):
        s.process_trades([make_trade(price=0.20, tx=f"0x{i}", ts=1_787_000_000 + i)])
    assert spend(db) <= cap + 1e-6
    floor_usd = s._min_tranche_usd(deep()["TOK1"], 0.20)
    assert cap - spend(db) < floor_usd, \
        "undeployed budget must be smaller than one placeable order"


def test_stake_schedule_splits_the_budget_at_cheap_prices(db, cfg):
    """At 2c a scheduled tranche clears the 5-share floor easily, so the split
    is exactly the schedule."""
    s, _, _ = build(db, cfg, books=deep(0.02))
    cap = budget(s, price=0.02)
    for i in range(3):
        s.process_trades([make_trade(price=0.02, tx=f"0x{i}", ts=1_787_000_000 + i)])
    buys = sorted((e["id"], -e["net_usd"]) for e in db.recent_executions(20)
                  if e["side"] == "BUY")
    amounts = [round(u, 2) for _, u in buys]
    expected = [round(cap * f, 2) for f in cfg.stake_schedule]
    assert amounts == expected
    assert sum(amounts) == pytest.approx(cap, abs=0.01)


def test_tranche_is_sized_up_to_clear_the_exchange_floor(db, cfg):
    """At 29c a scheduled $1.02 tranche buys 3.5 shares, under the 5-share
    minimum. Sizing up keeps the order placeable; the budget stays capped."""
    s, _, _ = build(db, cfg, books=deep(0.29))
    cap = budget(s, price=0.29)
    s.process_trades([make_trade(price=0.29, tx="0x1", ts=1_787_000_000)])
    buys = [e for e in db.recent_executions(20) if e["side"] == "BUY"]
    assert len(buys) == 1
    assert buys[0]["shares"] >= 5.0, "order must clear the exchange minimum"
    assert -buys[0]["net_usd"] > cap * cfg.stake_schedule[0]
    assert -buys[0]["net_usd"] <= cap + 1e-6


def test_fourth_fill_is_refused_with_a_budget_reason(db, cfg):
    s, _, _ = build(db, cfg)
    for i in range(4):
        s.process_trades([make_trade(price=0.20, tx=f"0x{i}", ts=1_787_000_000 + i)])
    reasons = {r["reason"] for r in db.recent_skips(10)}
    assert reasons & {SkipReason.ALREADY_AT_MAX_COPIES.value,
                      SkipReason.TOKEN_BUDGET_SPENT.value}, \
        f"a refused 4th fill should cite the budget, got {reasons}"
    assert spend(db) <= budget(s) + 1e-6


def test_budget_is_remembered_across_a_restart(db, cfg, tmp_path):
    """The cap is measured from the ledger, so a restart cannot forget that a
    token is already funded and hand it a second budget."""
    from copybot.db import Database
    path = tmp_path / "budget.sqlite3"
    d1 = Database(path, cfg.starting_capital_usd)
    s1, _, _ = build(d1, cfg)
    s1.process_trades([make_trade(price=0.20, tx="0xa", ts=1_787_000_000)])
    first = sum(-e["net_usd"] for e in d1.recent_executions(9) if e["side"] == "BUY")
    d1.close()

    d2 = Database(path, cfg.starting_capital_usd)
    assert d2.spent_on_token("TOK1") == pytest.approx(first)
    s2, _, _ = build(d2, cfg)
    for i in range(5):
        s2.process_trades([make_trade(price=0.20, tx=f"0xb{i}", ts=1_787_000_010 + i)])
    total = sum(-e["net_usd"] for e in d2.recent_executions(50) if e["side"] == "BUY")
    assert total <= budget(s2) + 1e-6
    d2.close()


def test_following_him_down_beats_his_opener(db, cfg):
    """He opens at 25c then averages down to 5c. Taking only his first fill
    leaves us at 25c; following him down pulls our VWAP toward his."""
    books = {"TOK1": make_book(asks=[(0.25, 100_000)], bids=[(0.24, 100_000)])}
    cfg = tweak(cfg, stake_schedule=[0.5, 0.5], max_copies_per_token=2)
    s, ex, _ = build(db, cfg, books=books)
    s.process_trades([make_trade(price=0.25, tx="0x1", ts=1_787_000_000)])
    ex.books["TOK1"] = make_book(asks=[(0.05, 100_000)], bids=[(0.04, 100_000)])
    s.process_trades([make_trade(price=0.05, tx="0x2", ts=1_787_000_001)])

    buys = [e for e in db.recent_executions(20) if e["side"] == "BUY"]
    assert len(buys) >= 2
    shares = sum(e["shares"] for e in buys)
    gross = sum(e["gross_usd"] for e in buys)
    our_vwap = gross / shares
    assert our_vwap < 0.25, "following him down must beat taking only his opener"
    assert our_vwap == pytest.approx(gross / shares)


# --- the tightened entry band ---------------------------------------------

def test_buy_at_29c_is_copied(db, cfg):
    s, _, _ = build(db, cfg, books=deep(0.29))
    c = s.process_trades([make_trade(price=0.29, ts=1_787_000_000)])
    assert c.copied == 1


def test_buy_at_31c_is_not_copied(db, cfg):
    s, _, _ = build(db, cfg, books=deep(0.31))
    c = s.process_trades([make_trade(price=0.31, ts=1_787_000_000)])
    assert c.copied == 0
    assert db.recent_skips()[0]["reason"] == SkipReason.PRICE_ABOVE_MAX_ENTRY.value


def test_config_refuses_a_schedule_that_does_not_sum_to_one(cfg):
    """The budget is a hard cap, so a schedule summing to 1.2 must not load."""
    import dataclasses
    from copybot.config import ConfigError, _validate
    bad = dataclasses.replace(cfg, stake_schedule=[0.5, 0.4, 0.3])
    with pytest.raises(ConfigError, match="sum to exactly 1.0"):
        _validate(bad)


def test_config_refuses_more_copies_than_schedule_entries(cfg):
    import dataclasses
    from copybot.config import ConfigError, _validate
    bad = dataclasses.replace(cfg, max_copies_per_token=5, stake_schedule=[0.5, 0.5])
    with pytest.raises(ConfigError, match="exceeds"):
        _validate(bad)
