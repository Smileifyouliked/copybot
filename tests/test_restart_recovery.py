"""Test 4: kill the bot and reload; state must match exactly.

The design rule under test is that cash is derived from the executions ledger
rather than held as a mutable number, so there is nothing in memory that can
survive a crash unpersisted and nothing to double-count on reboot.
"""
import pytest

from copybot.db import Database
from conftest import make_trade


def _populate(db):
    """Two open positions, one closed round trip, and a feed of his fills."""
    for t in [
        make_trade(token_id="TOK1", price=0.05, shares=20.0, tx="0x1", ts=1000),
        make_trade(token_id="TOK1", price=0.04, shares=30.0, tx="0x2", ts=1010),
        make_trade(token_id="TOK2", price=0.25, shares=8.0, tx="0x3", ts=1020),
        make_trade(token_id="TOK3", price=0.30, shares=5.0, tx="0x4", ts=1030),
    ]:
        db.record_processed(t, "copied")

    with db.tx() as conn:
        for tok, shares, avg, fee, band in [
            ("TOK1", 60.0, 0.05, 0.1425, "0-10c"),
            ("TOK2", 12.0, 0.25, 0.045, "20-30c"),
        ]:
            pid = db.insert_position(
                conn, token_id=tok, condition_id=f"0xc{tok}", question=f"Q {tok}",
                outcome="Yes", outcome_index=0, status="open", opened_ts=1000,
                shares=shares, shares_opened=shares, cost_basis_usd=shares * avg + fee,
                cost_basis_opened=shares * avg + fee, our_avg_fill=avg,
                entry_fee_usd=fee, entry_band=band, his_first_price=avg,
                his_vwap_entry=0.044, last_mark_price=avg,
            )
            db.insert_execution(conn, position_id=pid, token_id=tok, side="BUY", ts=1000,
                                shares=shares, avg_fill=avg, gross_usd=shares * avg,
                                fee_usd=fee, net_usd=-(shares * avg + fee), entry_band=band)

    with db.tx() as conn:
        pid = db.insert_position(
            conn, token_id="TOK3", condition_id="0xcTOK3", question="Q TOK3",
            outcome="Yes", outcome_index=0, status="closed", opened_ts=900, closed_ts=950,
            shares=0.0, shares_opened=10.0, cost_basis_usd=0.0, cost_basis_opened=3.0,
            our_avg_fill=0.30, entry_fee_usd=0.0, entry_band="30-40c",
            realised_pnl_usd=1.0, proceeds_usd=4.0, exit_path="mirrored_sell",
        )
        db.insert_execution(conn, position_id=pid, token_id="TOK3", side="BUY", ts=900,
                            shares=10.0, avg_fill=0.30, gross_usd=3.0, fee_usd=0.0,
                            net_usd=-3.0)
        db.insert_execution(conn, position_id=pid, token_id="TOK3", side="SELL", ts=950,
                            shares=10.0, avg_fill=0.40, gross_usd=4.0, fee_usd=0.0,
                            net_usd=4.0, realised_pnl_usd=1.0)


def _snapshot(db):
    return {
        "cash": round(db.cash(), 8),
        "realised": round(db.realised_pnl(), 8),
        "open_basis": round(db.open_cost_basis(), 8),
        "processed": sorted(db.processed_keys()),
        "open_tokens": sorted(r["token_id"] for r in db.open_positions()),
        "open_shares": {r["token_id"]: round(r["shares"], 8) for r in db.open_positions()},
        "copies_per_token": {t: db.count_copies_for_token(t) for t in ("TOK1", "TOK2", "TOK3")},
        "his_vwap_TOK1": db.his_vwap_entry("TOK1")[0],
        "his_open_shares_TOK1": round(db.his_open_shares("TOK1"), 8),
    }


def test_state_is_identical_after_restart(tmp_path):
    path = tmp_path / "restart.sqlite3"
    d1 = Database(path, 150.0)
    _populate(d1)
    before = _snapshot(d1)
    d1.close()  # simulate the process being killed

    d2 = Database(path, 150.0)
    after = _snapshot(d2)
    assert after == before
    ok, parts = d2.reconcile()
    assert ok, parts
    d2.close()


def test_restart_does_not_recopy_already_seen_trades(tmp_path):
    path = tmp_path / "restart2.sqlite3"
    d1 = Database(path, 150.0)
    _populate(d1)
    cash_before = d1.cash()
    d1.close()

    d2 = Database(path, 150.0)
    feed = [
        make_trade(token_id="TOK1", price=0.05, shares=20.0, tx="0x1", ts=1000),
        make_trade(token_id="TOK1", price=0.04, shares=30.0, tx="0x2", ts=1010),
        make_trade(token_id="TOK2", price=0.25, shares=8.0, tx="0x3", ts=1020),
    ]
    assert sum(d2.record_processed(t, "copied") for t in feed) == 0
    assert d2.cash() == pytest.approx(cash_before)
    d2.close()


def test_closed_position_does_not_free_a_copy_slot(tmp_path):
    """TOK3 round-tripped. max_copies_per_token counts the ledger, so a closed
    position must still block a second copy rather than looking untouched."""
    path = tmp_path / "restart3.sqlite3"
    d1 = Database(path, 150.0)
    _populate(d1)
    d1.close()

    d2 = Database(path, 150.0)
    assert d2.open_position_for_token("TOK3") is None
    assert d2.count_copies_for_token("TOK3") == 1
    d2.close()


def test_uncommitted_work_is_not_half_persisted(tmp_path):
    """A crash mid-copy must leave no position and no cash movement."""
    path = tmp_path / "restart4.sqlite3"
    d1 = Database(path, 150.0)
    try:
        with d1.tx() as conn:
            pid = d1.insert_position(
                conn, token_id="TOKX", condition_id="0xcx", question="Q", outcome="Yes",
                outcome_index=0, status="open", opened_ts=1, shares=12.0,
                shares_opened=12.0, cost_basis_usd=3.0, cost_basis_opened=3.0,
                our_avg_fill=0.25,
            )
            d1.insert_execution(conn, position_id=pid, token_id="TOKX", side="BUY", ts=1,
                                shares=12.0, avg_fill=0.25, gross_usd=3.0, fee_usd=0.0,
                                net_usd=-3.0)
            raise RuntimeError("crash mid-copy")
    except RuntimeError:
        pass
    d1.close()

    d2 = Database(path, 150.0)
    assert d2.open_positions() == []
    assert d2.cash() == pytest.approx(150.0)
    ok, _ = d2.reconcile()
    assert ok
    d2.close()
