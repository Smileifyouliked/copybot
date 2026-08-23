"""Domain types.

Deliberately dumb data holders plus the two pieces of logic that must be
identical everywhere they are used: the dedup key for one of his fills, and
the entry-band tag.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable

# Bands are inclusive-low, exclusive-high, in cents.
ENTRY_BANDS: tuple[tuple[int, int, str], ...] = (
    (0, 10, "0-10c"),
    (10, 20, "10-20c"),
    (20, 30, "20-30c"),
    (30, 40, "30-40c"),
    (40, 50, "40-50c"),
)


def entry_band(price: float) -> str:
    """Tag a fill price with its entry band. Prices at or above 50c fall
    outside the copyable range and are tagged explicitly rather than clamped."""
    cents = price * 100.0
    for lo, hi, label in ENTRY_BANDS:
        if lo <= cents < hi:
            return label
    return "50c+" if cents >= 50 else "unknown"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    SETTLE = "SETTLE"


class SkipReason(str, Enum):
    """Every reason we decline to copy. A skip is data, not a failure."""

    PRICE_ABOVE_MAX_ENTRY = "his_price_above_max_entry"
    TRADE_TOO_OLD = "trade_older_than_max_age"
    ALREADY_AT_MAX_COPIES = "already_hold_max_copies_for_token"
    TOKEN_BUDGET_SPENT = "per_token_budget_already_spent"
    NOT_ENOUGH_CASH = "not_enough_cash"
    EMPTY_BOOK = "book_empty"
    BOOK_TOO_THIN = "book_too_thin_to_fill_stake"
    FILL_ABOVE_MAX = "our_fill_price_above_our_max"
    BELOW_MIN_ORDER_SIZE = "below_book_min_order_size"
    MARKET_CLOSED = "market_closed_or_not_accepting_orders"
    NO_MARKET_METADATA = "market_metadata_unavailable"
    MALFORMED_TRADE = "malformed_trade_record"


class ExitPath(str, Enum):
    RESOLUTION = "resolution"
    MIRRORED_SELL = "mirrored_sell"


class MarkSource(str, Enum):
    """Where a mark-to-market price came from. Empty books are normal for
    freshly-resolved markets, so the gamma fallback is a first-class path."""

    BOOK_MID = "book_mid"
    BOOK_BID = "book_bid"
    GAMMA_OUTCOME_PRICE = "gamma_outcome_price"
    LAST_KNOWN = "last_known_mark"
    ENTRY_PRICE = "entry_price"


@dataclass(frozen=True)
class TargetTrade:
    """One fill by the wallet we follow, parsed from /activity.

    `transactionHash` is NOT unique -- a single transaction can contain several
    fills at the same price. The dedup key composes the fields that were unique
    across a 500-trade sample.
    """

    tx_hash: str
    token_id: str
    condition_id: str
    side: Side
    price: float
    shares: float
    usd_size: float
    traded_ts: int
    title: str
    outcome: str
    outcome_index: int
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def trade_key(self) -> str:
        parts = (
            self.tx_hash,
            self.token_id,
            self.side.value,
            f"{self.shares:.8f}",
            f"{self.price:.8f}",
            str(self.traded_ts),
        )
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]

    @classmethod
    def from_activity(cls, row: dict[str, Any]) -> "TargetTrade":
        """Parse one /activity row. Raises ValueError on anything unusable so
        the caller can log a malformed-trade skip and carry on."""
        try:
            side = Side(str(row["side"]).upper())
        except (KeyError, ValueError) as exc:
            raise ValueError(f"bad or missing side: {row.get('side')!r}") from exc
        if side is Side.SETTLE:
            raise ValueError("SETTLE is not a valid side on an activity row")

        token_id = str(row.get("asset") or "").strip()
        if not token_id:
            raise ValueError("missing asset (token_id)")

        try:
            price = float(row["price"])
            shares = float(row["size"])
            traded_ts = int(row["timestamp"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"bad numeric fields: {exc}") from exc

        if not (0.0 <= price <= 1.0):
            raise ValueError(f"price out of range: {price}")
        if shares <= 0:
            raise ValueError(f"non-positive size: {shares}")
        if traded_ts <= 0:
            raise ValueError(f"bad timestamp: {traded_ts}")

        usd = row.get("usdcSize")
        usd_size = float(usd) if usd is not None else price * shares

        return cls(
            tx_hash=str(row.get("transactionHash") or ""),
            token_id=token_id,
            condition_id=str(row.get("conditionId") or ""),
            side=side,
            price=price,
            shares=shares,
            usd_size=usd_size,
            traded_ts=traded_ts,
            title=str(row.get("title") or ""),
            outcome=str(row.get("outcome") or ""),
            outcome_index=int(row.get("outcomeIndex") or 0),
            raw=row,
        )


@dataclass(frozen=True)
class BookLevel:
    price: float
    size: float


@dataclass
class OrderBook:
    """A normalised book.

    The CLOB returns asks sorted DESCENDING (asks[0] is the worst price) and
    prices as strings. Both sides are re-sorted here into "best first" so no
    caller has to remember which end is which.
    """

    token_id: str
    bids: list[BookLevel]  # best (highest) first
    asks: list[BookLevel]  # best (lowest) first
    tick_size: float
    min_order_size: float
    timestamp_ms: int = 0
    condition_id: str = ""  # the CLOB returns it as `market`

    @property
    def is_empty(self) -> bool:
        return not self.bids and not self.asks

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def mid(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2.0

    @property
    def spread(self) -> float | None:
        """Absolute spread. A mid taken across a wide spread is a fiction, so
        CLV captures record this alongside the price."""
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    @classmethod
    def from_clob(
        cls,
        payload: dict[str, Any],
        token_id: str | None = None,
        default_tick: float = 0.001,
        default_min_size: float = 5.0,
    ) -> "OrderBook":
        def levels(key: str) -> list[BookLevel]:
            out: list[BookLevel] = []
            for entry in payload.get(key) or []:
                try:
                    p = float(entry["price"])
                    s = float(entry["size"])
                except (KeyError, TypeError, ValueError):
                    continue  # one bad level must not poison the book
                if s > 0 and 0.0 <= p <= 1.0:
                    out.append(BookLevel(p, s))
            return out

        def num(key: str, default: float) -> float:
            try:
                v = payload.get(key)
                return float(v) if v is not None else default
            except (TypeError, ValueError):
                return default

        bids = sorted(levels("bids"), key=lambda l: l.price, reverse=True)
        asks = sorted(levels("asks"), key=lambda l: l.price)
        try:
            ts = int(payload.get("timestamp") or 0)
        except (TypeError, ValueError):
            ts = 0

        return cls(
            token_id=str(payload.get("asset_id") or token_id or ""),
            condition_id=str(payload.get("market") or ""),
            bids=bids,
            asks=asks,
            tick_size=num("tick_size", default_tick),
            min_order_size=num("min_order_size", default_min_size),
            timestamp_ms=ts,
        )


@dataclass(frozen=True)
class FillResult:
    """Outcome of walking a book. `filled` False means we skipped.

    A skip carries the numbers that explain it -- what the book could actually
    have absorbed, and what our price would have been -- because a skip is
    data, not a failure, and "skipped" with no numbers teaches nothing.
    """

    filled: bool
    shares: float = 0.0
    gross_usd: float = 0.0
    avg_price: float = 0.0
    fee_usd: float = 0.0
    net_usd: float = 0.0  # signed: negative = cash out, positive = cash in
    levels_consumed: int = 0
    fee_rate_used: float = 0.0
    fee_rate_was_fallback: bool = False
    skip_reason: SkipReason | None = None
    detail: str = ""
    # -- diagnostics, populated on fills and skips alike -------------------
    requested_usd: float = 0.0
    requested_shares: float = 0.0
    depth_available_usd: float = 0.0     # what the book could absorb, total
    depth_available_shares: float = 0.0
    best_price: float = 0.0              # top of book at decision time
    worst_price: float = 0.0             # deepest level we would have touched
    would_be_avg_price: float = 0.0      # VWAP we'd have got, even when skipping
    is_partial: bool = False
    book_timestamp_ms: int = 0
    book_lag_ms: float | None = None  # ms between decision and book stamp
    tick_size: float = 0.0
    min_order_size: float = 0.0


@dataclass(frozen=True)
class MarketMeta:
    condition_id: str
    question: str
    token_ids: tuple[str, ...]
    outcomes: tuple[str, ...]
    outcome_prices: tuple[float, ...]
    closed: bool
    accepting_orders: bool
    fee_rate: float | None
    fee_type: str | None
    fees_enabled: bool
    end_date: str | None = None

    def price_for_token(self, token_id: str) -> float | None:
        """gamma's outcomePrices, aligned to clobTokenIds by index."""
        try:
            idx = self.token_ids.index(token_id)
        except ValueError:
            return None
        if idx < len(self.outcome_prices):
            return self.outcome_prices[idx]
        return None

    def settlement_value(self, token_id: str) -> float | None:
        """$1.00 if this token won, $0.00 if it lost. None if unresolved."""
        if not self.closed:
            return None
        p = self.price_for_token(token_id)
        if p is None:
            return None
        if p >= 0.99:
            return 1.0
        if p <= 0.01:
            return 0.0
        return None  # closed but ambiguous -- do not guess


def parse_json_list(value: Any) -> list[Any]:
    """gamma returns clobTokenIds/outcomes/outcomePrices as JSON *strings*."""
    import json

    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def vwap(fills: Iterable[tuple[float, float]]) -> float | None:
    """Volume-weighted average price from (price, shares) pairs."""
    total_shares = 0.0
    total_usd = 0.0
    for price, shares in fills:
        if shares <= 0:
            continue
        total_usd += price * shares
        total_shares += shares
    if total_shares <= 0:
        return None
    return total_usd / total_shares
