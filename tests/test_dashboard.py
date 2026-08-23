"""The dashboard, rendered.

It is read by someone who does not trade, so the tests check the things that
would mislead that reader: the break-even line moving, the two `side` readings
being presented as one settled number, and the money table being ordered by
something other than where the money is.
"""
import dataclasses

import pytest
from fastapi.testclient import TestClient

from conftest import FakeClient, FakeExecutor, make_book, make_meta, make_trade

from copybot.dashboard import _conventions_diverge, _ratio_bar, build_state, create_app
from copybot.db import Database
from copybot.strategy import Strategy

T0 = 1_787_000_000


@pytest.fixture
def paper_cfg(cfg, tmp_path):
    return dataclasses.replace(cfg, db_path=str(tmp_path / "dash.sqlite3"),
                               entry_mode="market", stake_variants_usd=[3.00])


def buy(db, cfg, token, price, his_price, min_size=1.0):
    books = {token: make_book(token_id=token, asks=[(price, 100_000)],
                              bids=[(price - 0.01, 100_000)], min_size=min_size)}
    s = Strategy(cfg, db, FakeExecutor(books=books),
                 FakeClient(books=books, metas={"0xcond1": make_meta()}),
                 clock=lambda: T0, run_id="run-A")
    s.process_trades([make_trade(token_id=token, price=his_price, tx=f"0x{token}",
                                 ts=T0)])


# --- the break-even line ---------------------------------------------------

def test_the_breakeven_line_does_not_move_with_the_data():
    """A bar scaled to its own data would slide the one line on the page that
    must stay put."""
    quiet = _ratio_bar(1.05, 1.395)
    loud = _ratio_bar(1.90, 1.395)
    assert quiet["breakeven_pct"] == loud["breakeven_pct"]
    assert quiet["value_pct"] < loud["value_pct"]
    assert loud["over"] and not quiet["over"]


def test_a_ratio_off_the_scale_still_renders():
    assert _ratio_bar(9.0, 1.395)["value_pct"] == 100.0
    assert _ratio_bar(0.5, 1.395)["value_pct"] == 0.0
    assert _ratio_bar(None, 1.395)["value_pct"] is None


def test_diverging_side_conventions_are_flagged():
    """They are not two estimates of one number: if the convention is inverted,
    one of them is counting a different population entirely."""
    assert _conventions_diverge({"fill_rate": 0.20, "alt_fill_rate": 0.55})
    assert not _conventions_diverge({"fill_rate": 0.20, "alt_fill_rate": 0.24})
    assert not _conventions_diverge({"fill_rate": None, "alt_fill_rate": 0.24})


# --- the rendered page -----------------------------------------------------

def test_the_page_renders_with_no_data_at_all(paper_cfg):
    """A fresh run must not show a broken page on its first minute."""
    with TestClient(create_app(paper_cfg)) as client:
        page = client.get("/")
    assert page.status_code == 200
    assert "nothing to compare" in page.text


def test_the_page_shows_the_ratio_and_the_line(paper_cfg):
    db = Database(paper_cfg.db_path, paper_cfg.starting_capital_usd)
    buy(db, paper_cfg, "TOK1", price=0.30, his_price=0.10)   # ratio 3.0
    db.close()

    with TestClient(create_app(paper_cfg)) as client:
        page = client.get("/")
    assert "3.000&times;" in page.text or "3.000×" in page.text
    assert "1.395" in page.text, "the break-even number must be on the page"
    assert "paying more than the edge is worth" in page.text


def test_holdings_are_ordered_by_what_they_are_worth(paper_cfg):
    db = Database(paper_cfg.db_path, paper_cfg.starting_capital_usd)
    buy(db, paper_cfg, "SMALL", price=0.20, his_price=0.20)
    buy(db, paper_cfg, "BIG", price=0.02, his_price=0.02)
    state = build_state(paper_cfg, db)
    worths = [h["worth"] for h in state["holdings"]]
    assert worths == sorted(worths, reverse=True), \
        "the money table answers 'where is my money', biggest first"
    db.close()


def test_healthz_reports_the_ledger(paper_cfg):
    with TestClient(create_app(paper_cfg)) as client:
        body = client.get("/healthz").json()
    assert body["reconciles"] is True
    assert "heartbeat_age_seconds" in body


# --- what "you paid" means -------------------------------------------------

def sell_most_of_it(db, cfg, token="TOK1"):
    """Fund a token fully, then mirror a sell of most of it."""
    books = {token: make_book(token_id=token, asks=[(0.05, 100_000)],
                              bids=[(0.06, 100_000)])}
    s = Strategy(cfg, db, FakeExecutor(books=books),
                 FakeClient(books=books, metas={"0xcond1": make_meta()}),
                 clock=lambda: T0, run_id="run-A")
    for i in range(3):
        s.process_trades([make_trade(token_id=token, price=0.05, shares=100.0,
                                     tx=f"0x{i}", ts=T0 + i)])
    s.process_trades([make_trade(token_id=token, side="SELL", price=0.06,
                                 shares=240.0, tx="0xs", ts=T0 + 20)])
    return s


def test_paid_shows_what_went_in_not_what_is_left(paper_cfg):
    """A $3.00 position he sold 80% back out of was reading as "paid $0.63".

    cost_basis_usd is the basis of the shares STILL HELD -- a mirrored sell
    releases the sold share of it. What we put in is cost_basis_opened, which
    only ever grows. Printing the first one under the heading "you paid" makes
    a normal partial exit look like an undersized order.
    """
    db = Database(paper_cfg.db_path, paper_cfg.starting_capital_usd)
    sell_most_of_it(db, paper_cfg)
    pos = db.open_position_for_token("TOK1")
    assert pos["cost_basis_usd"] < 1.0, "the remaining basis really is small"
    assert pos["cost_basis_opened"] == pytest.approx(3.00, abs=0.02)

    state = build_state(paper_cfg, db)
    holding = state["holdings"][0]
    assert holding["paid"] == pytest.approx(3.00, abs=0.02), \
        "'you paid' must be what we put in"
    assert holding["still_held"] == pytest.approx(pos["cost_basis_usd"])
    assert holding["part_sold"] is True
    db.close()


def test_up_down_is_measured_against_the_shares_still_held(paper_cfg):
    """The money from the sold shares is already realised and already in cash.
    Charging it against the position again would count it twice."""
    db = Database(paper_cfg.db_path, paper_cfg.starting_capital_usd)
    sell_most_of_it(db, paper_cfg)
    state = build_state(paper_cfg, db)
    h = state["holdings"][0]
    assert h["change"] == pytest.approx(h["worth"] - h["still_held"])
    assert abs(h["change"]) < 1.0, \
        "a partial exit must not read as a near-total loss"
    db.close()


def test_a_position_never_sold_reads_the_same_both_ways(paper_cfg):
    """The fix must not change the ordinary case."""
    db = Database(paper_cfg.db_path, paper_cfg.starting_capital_usd)
    buy(db, paper_cfg, "TOK1", price=0.05, his_price=0.05)
    h = build_state(paper_cfg, db)["holdings"][0]
    assert h["part_sold"] is False
    assert h["paid"] == pytest.approx(h["still_held"])
    db.close()


def test_the_page_says_when_part_of_a_bet_is_already_sold(paper_cfg):
    db = Database(paper_cfg.db_path, paper_cfg.starting_capital_usd)
    sell_most_of_it(db, paper_cfg)
    db.close()
    with TestClient(create_app(paper_cfg)) as client:
        page = client.get("/")
    assert "part already sold" in page.text
    assert "still in" in page.text
