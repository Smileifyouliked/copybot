"""Paper fill simulation by walking the real order book.

This is the file that decides whether the whole exercise is honest. A previous
bot looked profitable on paper and lost money live because paper fills assumed
infinite liquidity at the mid. Everything here exists to make that specific
failure impossible.

The rules, and what enforces each one:

  1. Never fill at the mid, at the last trade price, or at his price.
     `simulate_buy`/`simulate_sell` only ever read `book.asks`/`book.bids` and
     accumulate a true volume-weighted average across the levels consumed.
     No other price source is imported into this module.

  2. A $3 order eats levels.
     The walk takes `min(level.size, what the remaining budget affords)` at each
     level and moves to the next. Depth is finite by construction.

  3. If the book cannot fill the order, do not fill it.
     Buys are all-or-nothing: a book that cannot absorb the full stake produces
     a skip carrying the depth it *could* absorb, not a smaller fill.

  4. No look-ahead.
     This module is pure: it reads one book snapshot handed to it and has no
     network access, so it cannot consult a price from after the decision.
     `decision_ts` additionally rejects a book stamped after the decision.

  5. Fees are charged per level, not on the average.
     A marketable order crossing three levels is three matches, each charged at
     its own price. Because `p(1-p)` is concave, charging on the VWAP instead
     would systematically overstate the fee -- wrong in the safe direction, but
     still wrong.

  6. Prices are never rounded.
     Level prices come off the book already tick-aligned. The resulting VWAP is
     a weighted average and is legitimately *not* tick-aligned; rounding it to
     the tick, and especially to a whole cent, would inject a 50% error at the
     2-5c prices this wallet trades.
"""
from __future__ import annotations

import logging

from .fees import FeeModel
from .models import FillResult, OrderBook, SkipReason

log = logging.getLogger(__name__)

# Money comparisons are in dollars; float64 has ~15 significant digits, so this
# is many orders of magnitude below a cent and safely above accumulated error.
MONEY_EPS = 1e-9


class LookAheadError(Exception):
    """A book timestamped after the decision moment reached the simulator."""


def assert_no_look_ahead(book: OrderBook, decision_ts: int | None) -> None:
    """Reject a book from the future relative to our decision.

    The CLOB stamps books in milliseconds; /activity is in seconds. Mixing the
    two is the easy way to accidentally introduce look-ahead, so the comparison
    is done explicitly in seconds.
    """
    if decision_ts is None or not book.timestamp_ms:
        return
    book_ts = book.timestamp_ms / 1000.0
    if book_ts > decision_ts + 1.0:  # 1s tolerance for clock skew
        raise LookAheadError(
            f"book for {book.token_id[:16]}… is stamped {book_ts - decision_ts:.1f}s "
            f"after the decision moment; that is look-ahead"
        )


def validate_tick_alignment(book: OrderBook) -> bool:
    """Warn if book prices are not multiples of the book's own tick size.

    Not fatal -- the exchange's book is the source of truth -- but a mismatch
    means our understanding of the price grid is wrong and worth seeing.
    """
    tick = book.tick_size
    if tick <= 0:
        return True
    for level in (*book.bids, *book.asks):
        remainder = level.price / tick
        if abs(remainder - round(remainder)) > 1e-6:
            log.warning(
                "book %s has price %r off the %r tick grid",
                book.token_id[:16], level.price, tick,
            )
            return False
    return True


def _depth(levels, price_cap: float | None = None) -> tuple[float, float]:
    """(shares, usd) available across levels, optionally capped by price."""
    shares = usd = 0.0
    for lvl in levels:
        if price_cap is not None and lvl.price > price_cap:
            continue
        shares += lvl.size
        usd += lvl.size * lvl.price
    return shares, usd


def simulate_buy(
    book: OrderBook,
    budget_usd: float,
    fee: FeeModel,
    *,
    max_fill_price: float,
    respect_min_order_size: bool = True,
    min_order_size_override: float | None = None,
    decision_ts: int | None = None,
) -> FillResult:
    """Simulate spending exactly `budget_usd` of cash buying into `book`.

    `budget_usd` is the TOTAL cash outlay, fee included -- "every copy spends a
    fixed $3.00, not more, not less". So each level is charged at
    `price + fee_per_share(price)` and the walk stops when the budget is spent,
    rather than spending $3 on shares and paying the fee on top.

    All-or-nothing: if the book cannot absorb the whole budget, this returns a
    skip carrying the depth that *was* available, never a partial fill.
    """
    assert_no_look_ahead(book, decision_ts)
    validate_tick_alignment(book)

    min_size = (
        min_order_size_override
        if min_order_size_override is not None
        else book.min_order_size
    )
    depth_shares, depth_usd = _depth(book.asks)
    base = dict(
        requested_usd=budget_usd,
        depth_available_usd=depth_usd,
        depth_available_shares=depth_shares,
        best_price=book.best_ask or 0.0,
        book_timestamp_ms=book.timestamp_ms,
        tick_size=book.tick_size,
        min_order_size=min_size,
        fee_rate_used=fee.rate,
        fee_rate_was_fallback=fee.was_fallback,
    )

    if not book.asks:
        # Normal for a resolved market, and for a token nobody is offering.
        return FillResult(
            filled=False, skip_reason=SkipReason.EMPTY_BOOK,
            detail="no asks on the book", **base,
        )

    remaining = budget_usd
    shares = gross = fee_usd = 0.0
    levels = 0
    worst_price = 0.0

    for level in book.asks:  # already sorted best (lowest) first
        cost_per_share = level.price + fee.per_share(level.price)
        if cost_per_share <= 0:
            continue  # a free share is not a real level; refuse to divide by it
        affordable = remaining / cost_per_share
        take = min(level.size, affordable)
        if take <= 0:
            break

        shares += take
        gross += take * level.price
        fee_usd += take * fee.per_share(level.price)
        remaining -= take * cost_per_share
        worst_price = level.price
        levels += 1

        if remaining <= MONEY_EPS:
            break

    avg_price = (gross / shares) if shares > 0 else 0.0
    base = {**base, "worst_price": worst_price, "would_be_avg_price": avg_price}

    # --- rejection paths, ordered so the most binding constraint is reported --

    # (a) The book ran out before the budget did. This is the thin-book case the
    #     whole module exists for. Report what the book *could* have taken.
    if remaining > MONEY_EPS:
        return FillResult(
            filled=False, skip_reason=SkipReason.BOOK_TOO_THIN,
            detail=(
                f"book holds only ${depth_usd:.2f} across {len(book.asks)} ask "
                f"level(s); needed ${budget_usd:.2f} and could only place "
                f"${budget_usd - remaining:.2f}"
            ),
            **base,
        )

    # (b) We could fill, but the price moved away from us. Per spec the test is
    #     on the resulting AVERAGE, not on any single level -- so the walk above
    #     deliberately consumed expensive levels in order to learn this number.
    if avg_price > max_fill_price:
        return FillResult(
            filled=False, skip_reason=SkipReason.FILL_ABOVE_MAX,
            detail=(
                f"our average fill would be {avg_price:.4f} "
                f"(best ask {book.best_ask:.4f}, deepest level touched "
                f"{worst_price:.4f}), above our max {max_fill_price:.4f}"
            ),
            **base,
        )

    # (c) The exchange will not accept an order this small. A 1-4 share position
    #     is unsellable live and would inflate paper equity with something that
    #     could never be exited.
    if respect_min_order_size and shares < min_size:
        return FillResult(
            filled=False, skip_reason=SkipReason.BELOW_MIN_ORDER_SIZE,
            detail=(
                f"${budget_usd:.2f} buys only {shares:.4f} shares at "
                f"{avg_price:.4f}, below the book minimum of {min_size:g}"
            ),
            **base,
        )

    return FillResult(
        filled=True,
        shares=shares,
        gross_usd=gross,
        avg_price=avg_price,
        fee_usd=fee_usd,
        net_usd=-(gross + fee_usd),  # cash out
        levels_consumed=levels,
        **base,
    )


def simulate_sell(
    book: OrderBook,
    shares_to_sell: float,
    fee: FeeModel,
    *,
    respect_min_order_size: bool = True,
    min_order_size_override: float | None = None,
    decision_ts: int | None = None,
) -> FillResult:
    """Simulate selling `shares_to_sell` into the bid side.

    Unlike buys, sells may fill partially: if the book cannot absorb the whole
    position, a real market sell takes what is there. The result carries
    `is_partial` and the caller decides what to do with the remainder. Selling
    into a thin book is exactly the situation where a paper bot flatters itself,
    so the achieved VWAP is reported honestly however ugly it is.
    """
    assert_no_look_ahead(book, decision_ts)
    validate_tick_alignment(book)

    min_size = (
        min_order_size_override
        if min_order_size_override is not None
        else book.min_order_size
    )
    depth_shares, depth_usd = _depth(book.bids)
    base = dict(
        requested_shares=shares_to_sell,
        depth_available_usd=depth_usd,
        depth_available_shares=depth_shares,
        best_price=book.best_bid or 0.0,
        book_timestamp_ms=book.timestamp_ms,
        tick_size=book.tick_size,
        min_order_size=min_size,
        fee_rate_used=fee.rate,
        fee_rate_was_fallback=fee.was_fallback,
    )

    if not book.bids:
        return FillResult(
            filled=False, skip_reason=SkipReason.EMPTY_BOOK,
            detail="no bids on the book", **base,
        )
    if shares_to_sell <= 0:
        return FillResult(
            filled=False, skip_reason=SkipReason.BELOW_MIN_ORDER_SIZE,
            detail="nothing to sell", **base,
        )

    remaining = shares_to_sell
    shares = gross = fee_usd = 0.0
    levels = 0
    worst_price = 0.0

    for level in book.bids:  # already sorted best (highest) first
        take = min(level.size, remaining)
        if take <= 0:
            break
        shares += take
        gross += take * level.price
        fee_usd += take * fee.per_share(level.price)
        remaining -= take
        worst_price = level.price
        levels += 1
        if remaining <= 0:
            break

    avg_price = (gross / shares) if shares > 0 else 0.0
    is_partial = remaining > 0
    base = {**base, "worst_price": worst_price, "would_be_avg_price": avg_price}

    if respect_min_order_size and shares < min_size:
        return FillResult(
            filled=False, skip_reason=SkipReason.BELOW_MIN_ORDER_SIZE,
            detail=(
                f"bids can absorb only {shares:.4f} shares, below the book "
                f"minimum of {min_size:g}"
            ),
            is_partial=is_partial, **base,
        )

    return FillResult(
        filled=True,
        shares=shares,
        gross_usd=gross,
        avg_price=avg_price,
        fee_usd=fee_usd,
        net_usd=gross - fee_usd,  # cash in
        levels_consumed=levels,
        is_partial=is_partial,
        detail=(
            f"partial: book absorbed {shares:.4f} of {shares_to_sell:.4f} shares"
            if is_partial else ""
        ),
        **base,
    )
