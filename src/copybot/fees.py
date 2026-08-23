"""Polymarket taker fees.

    fee_usd = shares × rate × p × (1 − p)

Verified against Polymarket's own worked example: 100 crypto shares at 50¢ costs
$1.75 taker fee, and 100 × 0.07 × 0.5 × 0.5 = 1.75. The older `min(p, 1−p)` form
would give 3.50, so that form is wrong for the current schedule.

Two properties of this shape matter for this strategy:

1. As a fraction of the money moving (shares × p), the fee is `rate × (1 − p)`.
   At a 5¢ entry that is ~4.75%, not ~0.25%. A flat bps-of-notional model
   understates the true cost by roughly 20× at these prices, in the direction
   that makes paper look better than live.
2. Settlement is not a trade and carries no fee. Holding to resolution pays the
   fee once; mirroring his sell pays it twice.
"""
from __future__ import annotations

from dataclasses import dataclass


def fee_per_share(price: float, rate: float) -> float:
    """Fee in dollars for one share at `price`. Zero at p=0 and p=1, peaks at p=0.5."""
    if rate <= 0:
        return 0.0
    p = min(max(price, 0.0), 1.0)
    return rate * p * (1.0 - p)


def taker_fee_usd(shares: float, price: float, rate: float) -> float:
    if shares <= 0:
        return 0.0
    return shares * fee_per_share(price, rate)


def effective_rate_of_notional(price: float, rate: float) -> float:
    """The fee as a fraction of dollars moved -- the number worth reasoning in.

    At p=0.05, rate=0.05 -> 0.0475, i.e. 4.75% each way.
    """
    if price <= 0:
        return 0.0
    return fee_per_share(price, rate) / price


@dataclass(frozen=True)
class FeeModel:
    """Resolved fee treatment for one market.

    `rate` comes from gamma's feeSchedule for that market, or from
    config.fee_rate_fallback when the lookup failed (`was_fallback` True, and
    the client has already logged it at WARN). The fallback can never be zero:
    config.py refuses to start otherwise, because a missing fee field silently
    becoming free trading is the bug that makes paper beat live.

    `bps_override`, when set, replaces the real formula with a flat
    bps-of-notional charge. Testing only -- it is the wrong shape for real fees.
    """

    rate: float
    was_fallback: bool = False
    bps_override: float | None = None

    def per_share(self, price: float) -> float:
        if self.bps_override is not None:
            return price * (self.bps_override / 10_000.0)
        return fee_per_share(price, self.rate)

    def on(self, shares: float, price: float) -> float:
        if shares <= 0:
            return 0.0
        return shares * self.per_share(price)

    @property
    def is_free(self) -> bool:
        """True only when the API positively asserted feesEnabled: false."""
        return (self.bps_override or 0) == 0 and self.rate == 0 and not self.was_fallback
