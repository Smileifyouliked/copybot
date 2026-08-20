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
