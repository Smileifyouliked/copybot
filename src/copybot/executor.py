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
from .fills import LadderRung, simulate_buy, simulate_sell, size_ladder
from .models import FillResult, OrderBook
from .polymarket import PolymarketClient

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


class PaperExecutor(Executor):
    """Fake money, real prices.

    Every fill walks the live order book fetched at the moment of the decision.
    The book is fetched *here*, inside the execution call, so there is no window
    in which a later price could be substituted for the one we decided on.
    """

    def __init__(self, cfg: Config, client: PolymarketClient):
        self.cfg = cfg
        self.client = client

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


def build_executor(cfg: Config, client: PolymarketClient) -> Executor:
    """The single place that decides paper vs live."""
    if cfg.mode == "live":
        return LiveExecutor(cfg, client)
    return PaperExecutor(cfg, client)
