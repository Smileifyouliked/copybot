"""Resting limit orders, simulated against the real tape.

Market orders cross the spread ~15s behind him and pay 11-197% worse on his own
picks. Break-even is our VWAP / his VWAP = 1.395, so that is fatal. A resting
order at his exact price either fills at his price or does not fill at all.

The temptation here is to assume a fill whenever the best ask touches our limit.
That is the infinite-liquidity mistake in a new costume: the ask *quoting* at
our price is not the market *trading* at our price, and it says nothing about
the queue in front of us. So fills are driven by the executed-trade tape only.

The model, deliberately conservative at every choice:

  * We join at the BACK of the queue. Everything already resting at our price or
    better has priority, and that whole amount must trade before we get a share.
  * Only executed prints at or below our limit count. A print at or below our
    bid means a seller transacted at a price we were bidding at or above, so it
    consumed queue. Prints above our limit are irrelevant to us.
  * His own prints never count. His buy cannot be the seller that fills our
    bid, and letting it would mean trading against the wallet we copy.
  * Only prints on OUR token count. The tape is fetched per market, and a
    market has two tokens: a print at 0.20 on the complementary outcome is a
    different book with a different queue. Verified against the live endpoint,
    which stamps every row with `asset`.
  * **Both `side` conventions are computed, every time.** A print at or below
    our limit fills us only if a *seller* hit the bid; a buyer lifting an ask
    at the same price consumes no bid queue. Which label means "seller" is not
    established -- classifying live prints against a simultaneous quote yielded
    n=1, and a median comparison across two assets disagreed with itself.
    Picking one is not conservative: if the convention is inverted, requiring
    SELL does not under-count, it measures a disjoint population, and the
    direction of the error is unknown. So the configured convention drives the
    position while the opposite one is computed alongside it and stored. If the
    two track each other the ambiguity is moot; if they diverge we know it
    matters before trusting either. `quotes.py` settles it from recorded
    book snapshots.
  * Partial fills are real. An order that half-fills before its TTL expires
    leaves us with half a position, and that is recorded as such.
  * A maker fill pays NO fee. Confirmed against the live schedule, which
    carries `takerOnly: true`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

from .models import OrderBook

log = logging.getLogger(__name__)

SHARE_EPS = 1e-9


@dataclass
class RestingOrder:
    """A limit buy sitting on the book, waiting for the market to come to us."""

    token_id: str
    condition_id: str
    limit_price: float
    usd_budget: float
    target_shares: float
    placed_ts: int
    expires_ts: int
    queue_ahead_shares: float
    his_price: float
    his_trade_key: str = ""
    question: str = ""
    # progress
    filled_shares: float = 0.0
    filled_usd: float = 0.0
    fee_usd: float = 0.0
    consumed_shares: float = 0.0   # tape volume seen at or below our price
    last_seen_ts: int = 0
    prints_observed: int = 0
    # Measurement only -- the same order evaluated under the opposite `side`
    # convention. Never drives the position; exists so the ambiguity is visible
    # rather than assumed away.
    alt_consumed_shares: float = 0.0
    alt_filled_shares: float = 0.0
    alt_prints_observed: int = 0

    @property
    def remaining_shares(self) -> float:
        return max(0.0, self.target_shares - self.filled_shares)

    @property
    def is_complete(self) -> bool:
        return self.remaining_shares <= SHARE_EPS

    def expired(self, now: int) -> bool:
        return now >= self.expires_ts

    @property
    def fill_fraction(self) -> float:
        if self.target_shares <= 0:
            return 0.0
        return self.filled_shares / self.target_shares

    @property
    def alt_fill_fraction(self) -> float:
        """Same order under the opposite `side` convention. Measurement only."""
        if self.target_shares <= 0:
            return 0.0
        return self.alt_filled_shares / self.target_shares


def queue_ahead_shares(book: OrderBook, limit_price: float,
                       model: str = "back") -> float:
    """Shares that must trade before ours.

    "back" (the default, and what the live model assumes): every bid at our
    price or better has priority -- a better price is strictly preferred by any
    seller, and equal prices were there first.

    "front" is the optimistic counterfactual: only strictly better prices are
    ahead of us, as if we were first in line at our own price. It exists so the
    queue assumption can be tested rather than believed, and it is a knob, not
    a default -- a run configured with it is measuring a different market than
    the one we would trade.
    """
    if model == "front":
        return sum(l.size for l in book.bids if l.price > limit_price + 1e-12)
    return sum(l.size for l in book.bids if l.price >= limit_price - 1e-12)


def place(
    book: OrderBook,
    his_price: float,
    usd_budget: float,
    *,
    now: int,
    ttl_seconds: int,
    condition_id: str = "",
    token_id: str = "",
    his_trade_key: str = "",
    question: str = "",
    queue_model: str = "back",
) -> RestingOrder:
    """Create a resting buy at his exact fill price."""
    target = usd_budget / his_price if his_price > 0 else 0.0
    return RestingOrder(
        token_id=token_id or book.token_id,
        condition_id=condition_id or book.condition_id,
        limit_price=his_price,
        usd_budget=usd_budget,
        target_shares=target,
        placed_ts=now,
        expires_ts=now + ttl_seconds,
        queue_ahead_shares=queue_ahead_shares(book, his_price, queue_model),
        his_price=his_price,
        his_trade_key=his_trade_key,
        question=question,
    )


def marketable(book: OrderBook, limit_price: float) -> bool:
    """True when the book is already offering at or below our limit.

    Then we are not resting at all -- we would cross and fill immediately, as a
    taker, at a price no worse than his. That is a windfall rather than the
    slippage we are trying to avoid, but it pays taker fees, so it is executed
    and recorded as a distinct path.
    """
    return book.best_ask is not None and book.best_ask <= limit_price + 1e-12


def apply_tape(
    order: RestingOrder,
    trades: Iterable[dict[str, Any]],
    now: int,
    *,
    require_sell_prints: bool = True,
    exclude_wallet: str | None = None,
) -> RestingOrder:
    """Advance an order's fill using executed prints since it was placed.

    Only prints strictly inside the order's lifetime count -- a trade from
    before we placed cannot have filled us, and using one would be look-ahead
    in reverse.

    No fee model is taken, deliberately: a maker fill pays nothing (every live
    schedule sampled carries `takerOnly: true`), so there is nothing for one to
    do here. Accepting one anyway cost a `fee_rate_for` lookup per order per
    pass, which on a cache miss is two gamma requests and, on a market with no
    fee schedule, a FEE FALLBACK warning -- the exact noise the empty-book case
    in `executor._fee_for` goes out of its way to avoid, on a hotter path.
    """
    excluded = (exclude_wallet or "").lower()
    primary_label = "SELL" if require_sell_prints else "BUY"
    volume = alt_volume = 0.0
    prints = alt_prints = 0

    for t in trades:
        try:
            ts = int(t["timestamp"])
            price = float(t["price"])
            size = float(t["size"])
        except (KeyError, TypeError, ValueError):
            continue
        if size <= 0:
            continue
        asset = str(t.get("asset", "") or "")
        if asset and order.token_id and asset != order.token_id:
            # Same market, other outcome. Its 0.20 prints belong to a different
            # book and a different queue. (A row without `asset` is counted:
            # the caller fetched this tape for one market, and dropping
            # unlabelled rows would zero a fill rate for a shape reason rather
            # than a market one. Every live row sampled carries the field.)
            continue
        if ts < order.placed_ts or ts > min(now, order.expires_ts):
            continue
        if price > order.limit_price + 1e-12:
            continue  # traded above us; our bid was never touched
        if excluded and str(t.get("proxyWallet", "")).lower() == excluded:
            continue  # his own fill cannot be the seller that fills us

        side = str(t.get("side", "")).upper()
        if side == primary_label:
            volume += size
            prints += 1
        else:
            alt_volume += size
            alt_prints += 1

    order.consumed_shares = max(order.consumed_shares, volume)
    order.prints_observed = max(order.prints_observed, prints)
    order.alt_consumed_shares = max(order.alt_consumed_shares, alt_volume)
    order.alt_prints_observed = max(order.alt_prints_observed, alt_prints)
    order.last_seen_ts = now

    reachable = max(0.0, order.consumed_shares - order.queue_ahead_shares)
    filled = min(order.target_shares, reachable)
    if filled > order.filled_shares:
        gained = filled - order.filled_shares
        order.filled_shares = filled
        order.filled_usd += gained * order.limit_price
        # `fee_usd` stays at zero: takerOnly is true on every live schedule
        # sampled, so a maker fill is free.

    alt_reachable = max(0.0, order.alt_consumed_shares - order.queue_ahead_shares)
    order.alt_filled_shares = max(
        order.alt_filled_shares, min(order.target_shares, alt_reachable)
    )
    return order


def describe(order: RestingOrder) -> str:
    if order.is_complete:
        return f"filled {order.filled_shares:.2f} shares at {order.limit_price:.4f}"
    if order.filled_shares > 0:
        return (f"partial: {order.filled_shares:.2f}/{order.target_shares:.2f} shares "
                f"at {order.limit_price:.4f}")
    return (f"unfilled at {order.limit_price:.4f}; {order.queue_ahead_shares:.0f} shares "
            f"ahead in queue, {order.consumed_shares:.0f} traded through")
