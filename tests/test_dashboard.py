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
