import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from copybot.db import Database


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.sqlite3", starting_capital_usd=150.00)
    yield d
    d.close()


def make_trade(*, token_id="TOK1", side="BUY", price=0.20, shares=10.0,
               ts=1_787_000_000, tx="0xabc", condition_id="0xcond1", title="Will X happen?"):
    from copybot.models import TargetTrade
    return TargetTrade.from_activity({
        "transactionHash": tx, "asset": token_id, "conditionId": condition_id,
        "side": side, "price": price, "size": shares, "usdcSize": price * shares,
        "timestamp": ts, "title": title, "outcome": "Yes", "outcomeIndex": 0,
    })


import dataclasses

from copybot.config import load_config
from copybot.fees import FeeModel
from copybot.fills import simulate_buy, simulate_sell, size_ladder
from copybot.models import BookLevel, MarketMeta, OrderBook


@pytest.fixture
def cfg():
    from pathlib import Path
    return load_config(Path(__file__).resolve().parents[1] / "config.yaml")


@pytest.fixture
def single_cfg(cfg):
    """One full-stake copy per token, 50c cap.

    Tests about sells, settlement and marking predate follow-him-down and are
    not about it. Pinning the entry rules here keeps them testing the thing
    they were written to test, instead of silently becoming budget tests.

    stake_variants_usd and entry_mode are pinned for the same reason: under the
    shipped config the per-token budget depends on which variant the token
    hashes to, and entries rest instead of crossing. Neither is what these
    tests are about; test_limit_path.py and test_shipped_config.py cover the
    shipped path directly.
    """
    return dataclasses.replace(
        cfg, max_entry_price=0.50, our_max_fill_price=0.50,
        shadow_band_max_price=0.50, max_copies_per_token=1, stake_schedule=[1.0],
        stake_variants_usd=[3.00], entry_mode="market",
    )


def tweak(cfg, **overrides):
    return dataclasses.replace(cfg, **overrides)


def make_book(token_id="TOK1", asks=(), bids=(), tick=0.001, min_size=5.0,
              ts=0, condition_id="0xcond1"):
    return OrderBook(
        token_id=token_id, condition_id=condition_id,
        bids=sorted([BookLevel(p, s) for p, s in bids], key=lambda l: l.price, reverse=True),
        asks=sorted([BookLevel(p, s) for p, s in asks], key=lambda l: l.price),
        tick_size=tick, min_order_size=min_size, timestamp_ms=ts,
    )


def make_meta(condition_id="0xcond1", token_ids=("TOK1", "TOK2"), prices=(0.30, 0.70),
              closed=False, question="Will X happen?", fee_rate=0.05, end_date=None):
    return MarketMeta(
        condition_id=condition_id, question=question, token_ids=tuple(token_ids),
        outcomes=("Yes", "No"), outcome_prices=tuple(prices), closed=closed,
        accepting_orders=not closed, fee_rate=fee_rate, fee_type="weather_fees",
        fees_enabled=True, end_date=end_date,
    )


class FakeExecutor:
    """Real fill simulation over books we control. Not a mock: the actual
    book-walking code runs, so these tests exercise fills.py too."""

    def __init__(self, books=None, fee_rate=0.0, tape=None, ttl=300,
                 exclude_wallet="0xhim"):
        self.books = books or {}
        self.fee = FeeModel(rate=fee_rate)
        self.calls = []
        self.tape = tape or []
        self.ttl = ttl
        self.exclude_wallet = exclude_wallet

    def get_book(self, token_id):
        return self.books.get(token_id) or make_book(token_id=token_id)

    def buy(self, token_id, usd_amount, *, book=None, decision_ts=None):
        book = book if book is not None else self.get_book(token_id)
        self.calls.append(("buy", token_id, usd_amount))
        return simulate_buy(book, usd_amount, self.fee, max_fill_price=0.50,
                            decision_ts=decision_ts, max_book_lag_seconds=3600)

    def sell(self, token_id, shares, *, book=None, decision_ts=None):
        book = book if book is not None else self.get_book(token_id)
        self.calls.append(("sell", token_id, shares))
        return simulate_sell(book, shares, self.fee, decision_ts=decision_ts,
                             max_book_lag_seconds=3600)

    def place_limit(self, token_id, usd_amount, limit_price, *, book=None, now=0, **kw):
        from copybot import limits
        book = book if book is not None else self.get_book(token_id)
        self.calls.append(("place_limit", token_id, usd_amount, limit_price))
        return limits.place(book, limit_price, usd_amount, now=now,
                            ttl_seconds=self.ttl, token_id=token_id, **kw)

    def poll_limit(self, order, now):
        from copybot import limits
        return limits.apply_tape(order, self.tape, self.fee, now,
                                 require_sell_prints=True,
                                 exclude_wallet=self.exclude_wallet)

    def shadow_ladder(self, book, rungs, *, decision_ts=None):
        self.calls.append(("ladder", book.token_id, len(rungs)))
        return size_ladder(book, rungs, self.fee, max_fill_price=0.50,
                           decision_ts=decision_ts, max_book_lag_seconds=3600)


class FakeClient:
    def __init__(self, books=None, metas=None):
        self.books = books or {}
        self.metas = metas or {}

    def get_book(self, token_id):
        return self.books.get(token_id) or make_book(token_id=token_id)

    def get_books(self, token_ids):
        return {t: self.books.get(t) or make_book(token_id=t) for t in token_ids}

    def get_markets(self, condition_ids, force=False):
        return {c: self.metas[c] for c in condition_ids if c in self.metas}

    def fee_rate_for(self, condition_id, fallback):
        meta = self.metas.get(condition_id)
        if meta and meta.fee_rate is not None:
            return meta.fee_rate, False
        return fallback, True
