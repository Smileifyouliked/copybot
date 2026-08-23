"""Test 6: cash never goes negative and the ledger always reconciles.

Invariant: cash + open cost basis - realised P&L == starting capital.
Cash already contains realised gains, so realised P&L is subtracted rather
than added; adding it would double-count every closed position.
"""
import pytest

from copybot.db import Database


def _buy(db, conn, token, shares, avg, fee):
    gross = shares * avg
    net = -(gross + fee)
    pos_id = db.insert_position(
        conn, token_id=token, condition_id="0xc", question="Q", outcome="Yes",
        outcome_index=0, status="open", opened_ts=1, shares=shares,
        shares_opened=shares, cost_basis_usd=gross + fee,
        cost_basis_opened=gross + fee, our_avg_fill=avg, entry_fee_usd=fee,
        entry_band="20-30c",
    )
    db.insert_execution(conn, position_id=pos_id, token_id=token, side="BUY", ts=1,
                        shares=shares, avg_fill=avg, gross_usd=gross, fee_usd=fee,
                        net_usd=net)
    return pos_id


def test_starting_state_reconciles(db):
    assert db.cash() == pytest.approx(150.0)
    ok, _ = db.reconcile()
    assert ok


def test_buy_moves_cash_into_cost_basis(db):
    with db.tx() as conn:
        _buy(db, conn, "TOK1", shares=12.0, avg=0.25, fee=0.045)
    assert db.cash() == pytest.approx(150.0 - 3.045)
    assert db.open_cost_basis() == pytest.approx(3.045)
    ok, parts = db.reconcile()
    assert ok, parts


def test_full_round_trip_reconciles_with_realised_pnl(db):
    with db.tx() as conn:
        pos_id = _buy(db, conn, "TOK1", shares=12.0, avg=0.25, fee=0.045)
    with db.tx() as conn:
        proceeds, fee = 12.0 * 0.34, 0.05
        net = proceeds - fee
        realised = net - 3.045
        db.insert_execution(conn, position_id=pos_id, token_id="TOK1", side="SELL", ts=2,
                            shares=12.0, avg_fill=0.34, gross_usd=proceeds, fee_usd=fee,
                            net_usd=net, realised_pnl_usd=realised)
        db.update_position(conn, pos_id, status="closed", closed_ts=2, shares=0.0,
                           cost_basis_usd=0.0, realised_pnl_usd=realised,
                           proceeds_usd=net, exit_fee_usd=fee, exit_path="mirrored_sell")
    ok, parts = db.reconcile()
    assert ok, parts
    assert db.realised_pnl() == pytest.approx(12.0 * 0.34 - 0.05 - 3.045)
    assert db.cash() == pytest.approx(150.0 + db.realised_pnl())


def test_settlement_at_one_dollar_reconciles(db):
    with db.tx() as conn:
        pos_id = _buy(db, conn, "TOK1", shares=60.0, avg=0.05, fee=0.1425)
    with db.tx() as conn:
        proceeds = 60.0 * 1.00  # settlement is not a trade: no fee
        realised = proceeds - 3.1425
        db.insert_execution(conn, position_id=pos_id, token_id="TOK1", side="SETTLE", ts=3,
                            shares=60.0, avg_fill=1.0, gross_usd=proceeds, fee_usd=0.0,
                            net_usd=proceeds, realised_pnl_usd=realised)
        db.update_position(conn, pos_id, status="closed", closed_ts=3, shares=0.0,
                           cost_basis_usd=0.0, realised_pnl_usd=realised,
                           proceeds_usd=proceeds, exit_path="resolution")
    ok, parts = db.reconcile()
    assert ok, parts
    assert db.cash() == pytest.approx(150.0 - 3.1425 + 60.0)


def test_cash_never_goes_negative_across_fifty_buys(db):
    """$150 at $3 a copy is 50 slots minus fees, so the 50th must be refused."""
    refused = 0
    for i in range(60):
        if db.cash() < 3.0:
            refused += 1
            continue
        with db.tx() as conn:
            _buy(db, conn, f"TOK{i}", shares=12.0, avg=0.25, fee=0.045)
        assert db.cash() >= 0.0
    assert refused > 0
    ok, parts = db.reconcile()
    assert ok, parts


def test_reconcile_detects_a_tampered_ledger(db):
    """The invariant must actually be able to fail, or it proves nothing."""
    with db.tx() as conn:
        _buy(db, conn, "TOK1", shares=12.0, avg=0.25, fee=0.045)
    db.conn.execute("UPDATE positions SET cost_basis_usd = cost_basis_usd + 1.0")
    ok, parts = db.reconcile()
    assert not ok
    assert parts["diff"] == pytest.approx(1.0)


# --- Where the fee lands -----------------------------------------------------
# Decision on record: the entry fee is capitalised into cost basis. It is NOT
# also booked as an immediate realised loss. Accounting for it in both places,
# or in neither, breaks the reconciliation identity by exactly the fee.

def test_entry_fee_is_capitalised_into_cost_basis(db):
    fee = 0.1425
    with db.tx() as conn:
        _buy(db, conn, "TOK1", shares=60.0, avg=0.05, fee=fee)
    pos = db.open_positions()[0]
    assert pos["cost_basis_usd"] == pytest.approx(60.0 * 0.05 + fee)
    assert pos["entry_fee_usd"] == pytest.approx(fee)
    assert db.realised_pnl() == pytest.approx(0.0), "entry fee must not also be realised"
    ok, parts = db.reconcile()
    assert ok, parts


def test_identity_holds_with_non_zero_fees_on_both_legs(db):
    entry_fee, exit_fee = 0.1425, 0.1197
    with db.tx() as conn:
        pos_id = _buy(db, conn, "TOK1", shares=60.0, avg=0.05, fee=entry_fee)
    basis = 60.0 * 0.05 + entry_fee
    with db.tx() as conn:
        proceeds = 60.0 * 0.09
        net = proceeds - exit_fee
        realised = net - basis
        db.insert_execution(conn, position_id=pos_id, token_id="TOK1", side="SELL", ts=2,
                            shares=60.0, avg_fill=0.09, gross_usd=proceeds,
                            fee_usd=exit_fee, net_usd=net, realised_pnl_usd=realised)
        db.update_position(conn, pos_id, status="closed", closed_ts=2, shares=0.0,
                           cost_basis_usd=0.0, realised_pnl_usd=realised,
                           proceeds_usd=net, exit_fee_usd=exit_fee,
                           exit_path="mirrored_sell")
    ok, parts = db.reconcile()
    assert ok, parts
    # Both fees are really in the P&L: gross move was +$2.40, fees ate $0.2622.
    assert db.realised_pnl() == pytest.approx(2.40 - entry_fee - exit_fee)


def test_double_counting_the_entry_fee_breaks_the_identity(db):
    """Proves the identity is sensitive to exactly this mistake."""
    fee = 0.1425
    with db.tx() as conn:
        _buy(db, conn, "TOK1", shares=60.0, avg=0.05, fee=fee)
    # Also book the entry fee as a realised loss -- the error we are excluding.
    db.conn.execute("UPDATE positions SET realised_pnl_usd = ?", (-fee,))
    ok, parts = db.reconcile()
    assert not ok
    assert parts["diff"] == pytest.approx(fee)


def test_omitting_the_fee_from_basis_also_breaks_the_identity(db):
    fee = 0.1425
    with db.tx() as conn:
        _buy(db, conn, "TOK1", shares=60.0, avg=0.05, fee=fee)
    db.conn.execute("UPDATE positions SET cost_basis_usd = cost_basis_usd - ?", (fee,))
    ok, parts = db.reconcile()
    assert not ok
    assert parts["diff"] == pytest.approx(-fee)
