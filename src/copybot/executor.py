"""Execution interface.

The point of this seam is that flipping to live trading changes which class is
constructed and nothing else. No strategy logic imports anything from here
except the abstract `Executor`.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from .config import LIVE_ENV_VARS, Config
from .fees import FeeModel
from . import limits
from .fills import LadderRung, simulate_buy, simulate_sell, size_ladder
from .models import FillResult, OrderBook
from .polymarket import PolymarketClient, PolymarketError

log = logging.getLogger(__name__)


class Executor(ABC):
    @abstractmethod
    def get_book(self, token_id: str) -> OrderBook: ...

    @abstractmethod
    def buy(self, token_id: str, usd_amount: float, *,
            book: OrderBook | None = None,
            decision_ts: int | None = None) -> FillResult: ...

    @abstractmethod
    def sell(self, token_id: str, shares: float, *,
             book: OrderBook | None = None,
             decision_ts: int | None = None) -> FillResult: ...

    @abstractmethod
    def shadow_ladder(self, book: OrderBook, rungs: list[tuple[str, float]], *,
                      decision_ts: int | None = None) -> list[LadderRung]:
        """Measurement only: never moves cash, never opens a position."""

    @abstractmethod
    def place_limit(self, token_id: str, usd_amount: float, limit_price: float, *,
                    book: OrderBook | None = None, now: int = 0,
                    **kw) -> "limits.RestingOrder":
        """Rest a buy at `limit_price`. Never crosses the spread."""

    @abstractmethod
    def poll_limit(self, order: "limits.RestingOrder", now: int) -> "limits.RestingOrder":
        """Advance a resting order against executed prints since it was placed."""

    def begin_poll(self) -> None:
        """Start a polling pass. Tapes fetched during it may be shared."""

    def end_poll(self) -> None:
        """End the pass and drop anything cached for it."""


class PaperExecutor(Executor):
    """Fake money, real prices.

    Every fill walks the live order book fetched at the moment of the decision.
    The book is fetched *here*, inside the execution call, so there is no window
    in which a later price could be substituted for the one we decided on.
    """

    def __init__(self, cfg: Config, client: PolymarketClient):
        self.cfg = cfg
        self.client = client
        # Tapes fetched during one polling pass, keyed by conditionId. A market
        # has one tape no matter how many orders we rest into it.
        self._tape_cache: dict[str, list] | None = None

    def begin_poll(self) -> None:
        self._tape_cache = {}

    def end_poll(self) -> None:
        self._tape_cache = None

    def get_book(self, token_id: str) -> OrderBook:
        return self.client.get_book(token_id)

    def _fee_for(self, book: OrderBook) -> FeeModel:
        """The book carries its own conditionId, so fee resolution needs no
        extra plumbing from the caller."""
        if book.is_empty:
            # No fill can happen against an empty book, so this rate is never
            # used. Resolving it anyway would emit a fee-fallback warning for
            # every resolved market the wallet ever touched -- and that warning
            # exists to make a REAL missing fee rate impossible to miss. Drowning
            # it in hundreds of false positives defeats the guardrail.
            return FeeModel(rate=self.cfg.fee_rate_fallback, was_fallback=False,
                            bps_override=self.cfg.fee_bps_override)
        if not book.condition_id:
            log.warning("FEE FALLBACK: book for %s carries no conditionId -- "
                        "using fallback rate %.4f", book.token_id[:16],
                        self.cfg.fee_rate_fallback)
            return FeeModel(rate=self.cfg.fee_rate_fallback, was_fallback=True,
                            bps_override=self.cfg.fee_bps_override)
        rate, was_fallback = self.client.fee_rate_for(
            book.condition_id, self.cfg.fee_rate_fallback
        )
        return FeeModel(rate=rate, was_fallback=was_fallback,
                        bps_override=self.cfg.fee_bps_override)

    def buy(self, token_id: str, usd_amount: float, *,
            book: OrderBook | None = None,
            decision_ts: int | None = None) -> FillResult:
        book = book if book is not None else self.get_book(token_id)
        return simulate_buy(
            book, usd_amount, self._fee_for(book),
            max_fill_price=self.cfg.our_max_fill_price,
            respect_min_order_size=self.cfg.respect_min_order_size,
            decision_ts=decision_ts,
            max_book_lag_seconds=self.cfg.max_book_lag_seconds,
        )

    def sell(self, token_id: str, shares: float, *,
             book: OrderBook | None = None,
             decision_ts: int | None = None) -> FillResult:
        book = book if book is not None else self.get_book(token_id)
        return simulate_sell(
            book, shares, self._fee_for(book),
            respect_min_order_size=self.cfg.respect_min_order_size,
            decision_ts=decision_ts,
            max_book_lag_seconds=self.cfg.max_book_lag_seconds,
        )

    def shadow_ladder(self, book: OrderBook, rungs: list[tuple[str, float]], *,
                      decision_ts: int | None = None) -> list[LadderRung]:
        return size_ladder(
            book, rungs, self._fee_for(book),
            max_fill_price=self.cfg.our_max_fill_price,
            respect_min_order_size=self.cfg.respect_min_order_size,
            decision_ts=decision_ts,
            max_book_lag_seconds=self.cfg.max_book_lag_seconds,
        )


    # -- limit orders ------------------------------------------------------
    def place_limit(self, token_id: str, usd_amount: float, limit_price: float, *,
                    book: OrderBook | None = None, now: int = 0,
                    **kw) -> limits.RestingOrder:
        book = book if book is not None else self.get_book(token_id)
        return limits.place(book, limit_price, usd_amount, now=now,
                            ttl_seconds=self.cfg.limit_order_ttl_seconds,
                            token_id=token_id, **kw)

    def poll_limit(self, order: limits.RestingOrder, now: int) -> limits.RestingOrder:
        """Advance the order using the market's own executed trades.

        The tape is the only honest source here. A quote at our price says
        nothing about whether anyone traded, or about the queue ahead of us.
        """
        if not order.condition_id:
            log.warning("resting order on %s carries no conditionId; cannot "
                        "check the tape", order.token_id[:16])
            return order
        cache = self._tape_cache
        if cache is not None and order.condition_id in cache:
            trades = cache[order.condition_id]
        else:
            try:
                trades = self.client.get_trades(order.condition_id, limit=200)
            except PolymarketError as exc:
                log.warning("tape fetch failed for %s: %s",
                            order.condition_id[:12], exc)
                return order
            if cache is not None:
                cache[order.condition_id] = trades
        return limits.apply_tape(
            order, trades, self._fee_for_condition(order.condition_id), now,
            require_sell_prints=self.cfg.limit_fill_requires_sell_prints,
            exclude_wallet=self.cfg.target_wallet,
        )

    def _fee_for_condition(self, condition_id: str) -> FeeModel:
        rate, was_fallback = self.client.fee_rate_for(
            condition_id, self.cfg.fee_rate_fallback)
        return FeeModel(rate=rate, was_fallback=was_fallback,
                        bps_override=self.cfg.fee_bps_override)


class LiveExecutor(Executor):
    """Not implemented. Deliberately.

    When this is built, the only change elsewhere should be which executor
    main.py constructs. Strategy code must not need touching.

    It will need signing credentials, read from the environment and never from
    config.yaml or source. The names are declared in `config.LIVE_ENV_VARS`.
    """

    def __init__(self, *args, **kwargs):
        raise NotImplementedError(
            "LiveExecutor is not implemented -- this bot is paper-only.\n"
            "Paper results should decide whether live is worth building, and "
            "the fill realism in fills.py is what makes that decision "
            "meaningful.\n"
            "When implementing, these environment variables will be required "
            "(never put them in config.yaml):\n  "
            + "\n  ".join(LIVE_ENV_VARS)
        )

    def get_book(self, token_id: str) -> OrderBook:  # pragma: no cover
        raise NotImplementedError

    def buy(self, token_id: str, usd_amount: float, *, book: OrderBook | None = None,
            decision_ts: int | None = None) -> FillResult:  # pragma: no cover
        raise NotImplementedError

    def sell(self, token_id: str, shares: float, *, book: OrderBook | None = None,
             decision_ts: int | None = None) -> FillResult:  # pragma: no cover
        raise NotImplementedError

    def shadow_ladder(self, book: OrderBook, rungs, *,
                      decision_ts: int | None = None):  # pragma: no cover
        raise NotImplementedError

    def place_limit(self, token_id, usd_amount, limit_price, *, book=None,
                    now=0, **kw):  # pragma: no cover
        raise NotImplementedError

    def poll_limit(self, order, now):  # pragma: no cover
        raise NotImplementedError


def build_executor(cfg: Config, client: PolymarketClient) -> Executor:
    """The single place that decides paper vs live."""
    if cfg.mode == "live":
        return LiveExecutor(cfg, client)
    return PaperExecutor(cfg, client)
