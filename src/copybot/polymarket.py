"""Polymarket public API client.

Three hosts, verified live before this was written:
  data-api.polymarket.com/activity   -- the wallet's fills
  clob.polymarket.com/book, /books   -- order books (batch POST supported)
  gamma-api.polymarket.com/markets   -- metadata, fee schedule, resolution

None of the three return rate-limit headers, so backoff here is blind and
defensive rather than header-driven. Excess traffic is throttled by Cloudflare
rather than rejected, so slow responses are expected under load, not errors.
"""
from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

import httpx

from .batch import reconcile_batch
from .models import MarketMeta, OrderBook, parse_json_list

log = logging.getLogger(__name__)

DATA_API = "https://data-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

# gamma silently drops unknown condition_ids and caps result sets, so ask in
# modest chunks and reconcile by id rather than trusting the count.
GAMMA_CHUNK = 20
CLOB_BOOKS_CHUNK = 50


class PolymarketError(Exception):
    """A request failed after exhausting retries."""


class NotFound(PolymarketError):
    """The resource does not exist. Permanent -- never retried.

    CLOB /book returns 404 for a token with no book (a resolved market), which
    is the single-fetch counterpart of POST /books omitting the same token. It
    is a normal state, not an error, and retrying it burns the whole backoff
    ladder on something that can never succeed.
    """


@dataclass
class _Cached:
    value: Any
    expires_at: float


class PolymarketClient:
    def __init__(
        self,
        *,
        timeout: float = 20.0,
        max_retries: int = 5,
        backoff_base: float = 1.0,
        backoff_cap: float = 60.0,
        market_ttl: float = 300.0,
        resolved_market_ttl: float = 86400.0,
        fee_rate_ttl: float = 3600.0,
        client: httpx.Client | None = None,
    ):
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.market_ttl = market_ttl
        self.resolved_market_ttl = resolved_market_ttl
        self.fee_rate_ttl = fee_rate_ttl
        self._client = client or httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "copybot/0.1 (paper trading research)"},
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )
        self._market_cache: dict[str, _Cached] = {}
        self._fee_cache: dict[str, _Cached] = {}

    def close(self) -> None:
        self._client.close()

    # -- transport ---------------------------------------------------------
    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        last: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                resp = self._client.request(method, url, **kwargs)
                if resp.status_code == 404:
                    raise NotFound(f"404 {url.split('?')[0]}")
                if 400 <= resp.status_code < 500 and resp.status_code != 429:
                    raise PolymarketError(
                        f"HTTP {resp.status_code} {url.split('?')[0]}: {resp.text[:200]}"
                    )
                if resp.status_code == 429 or resp.status_code >= 500:
                    retry_after = resp.headers.get("retry-after")
                    delay = (
                        float(retry_after)
                        if retry_after and retry_after.replace(".", "", 1).isdigit()
                        else self._backoff(attempt)
                    )
                    log.warning(
                        "%s %s -> HTTP %s, retrying in %.1fs (attempt %d/%d)",
                        method, url.split("?")[0], resp.status_code, delay,
                        attempt + 1, self.max_retries,
                    )
                    time.sleep(delay)
                    last = PolymarketError(f"HTTP {resp.status_code}")
                    continue
                resp.raise_for_status()
                return resp.json()
            except (httpx.TransportError, httpx.HTTPStatusError, json.JSONDecodeError) as exc:
                last = exc
                if attempt == self.max_retries - 1:
                    break
                delay = self._backoff(attempt)
                log.warning(
                    "%s %s failed (%s), retrying in %.1fs (attempt %d/%d)",
                    method, url.split("?")[0], exc.__class__.__name__, delay,
                    attempt + 1, self.max_retries,
                )
                time.sleep(delay)
        raise PolymarketError(f"{method} {url.split('?')[0]} failed after "
                              f"{self.max_retries} attempts: {last}") from last

    def _backoff(self, attempt: int) -> float:
        raw = self.backoff_base * (2 ** attempt)
        return min(self.backoff_cap, raw) * (0.5 + random.random())  # full-ish jitter

    # -- activity ----------------------------------------------------------
    def get_activity(self, wallet: str, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        """Newest-first list of the wallet's TRADE rows."""
        data = self._request(
            "GET",
            f"{DATA_API}/activity",
            params={"user": wallet, "type": "TRADE", "limit": limit, "offset": offset},
        )
        if not isinstance(data, list):
            raise PolymarketError(f"/activity returned {type(data).__name__}, expected list")
        return data

    # -- books -------------------------------------------------------------
    def get_book(self, token_id: str) -> OrderBook:
        """A token with no book yields an EMPTY book, not an exception.

        Resolved markets 404 here. That is a normal, expected state -- the same
        one POST /books signals by omission -- so it is represented as an empty
        book carrying its own token_id, which every caller already handles.
        """
        try:
            data = self._request("GET", f"{CLOB_API}/book", params={"token_id": token_id})
        except NotFound:
            log.info("no book for token %s (resolved or never listed)", token_id[:16])
            return self._empty_book(token_id)
        if not isinstance(data, dict):
            raise PolymarketError("/book returned a non-object")
        return OrderBook.from_clob(data, token_id=token_id)

    @staticmethod
    def _empty_book(token_id: str) -> OrderBook:
        return OrderBook(token_id=token_id, bids=[], asks=[], tick_size=0.001,
                         min_order_size=5.0, timestamp_ms=0)

    def get_books(self, token_ids: Sequence[str]) -> dict[str, OrderBook]:
        """Batch book fetch. Used to mark up to ~50 open positions in one or
        two calls rather than one call per position."""
        out: dict[str, OrderBook] = {}
        ids = [t for t in dict.fromkeys(token_ids) if t]
        for i in range(0, len(ids), CLOB_BOOKS_CHUNK):
            chunk = ids[i:i + CLOB_BOOKS_CHUNK]
            try:
                data = self._request(
                    "POST",
                    f"{CLOB_API}/books",
                    json=[{"token_id": t} for t in chunk],
                )
            except PolymarketError as exc:
                log.warning("batch /books failed for %d tokens (%s); falling back to singles",
                            len(chunk), exc)
                for t in chunk:
                    try:
                        out[t] = self.get_book(t)
                    except PolymarketError as inner:
                        log.warning("book fetch failed for %s: %s", t[:16], inner)
                        out[t] = self._empty_book(t)
                continue
            books = [
                OrderBook.from_clob(e) for e in (data if isinstance(data, list) else [])
                if isinstance(e, dict)
            ]
            # POST /books omits tokens with no book (resolved markets) rather
            # than returning an empty one, so the response is shorter than the
            # request and index-alignment would mismark positions.
            by_id, missing, _ = reconcile_batch(
                chunk, books, key_of=lambda b: b.token_id or None, what="CLOB POST /books"
            )
            out.update(by_id)
            for token_id in missing:
                # Absent means "no book", which is a real state, not an error.
                # Recorded as an explicitly empty book so callers cannot
                # mistake it for a book they simply failed to look up.
                out[token_id] = self._empty_book(token_id)
        return out

    # -- markets -----------------------------------------------------------
    def get_markets(self, condition_ids: Iterable[str],
                    force: bool = False) -> dict[str, MarketMeta]:
        """Metadata by conditionId, cached. Resolved markets cache far longer
        since they cannot change again."""
        wanted = [c for c in dict.fromkeys(condition_ids) if c]
        out: dict[str, MarketMeta] = {}
        missing: list[str] = []
        now = time.monotonic()

        for cid in wanted:
            hit = self._market_cache.get(cid)
            if hit and not force and hit.expires_at > now:
                out[cid] = hit.value
            else:
                missing.append(cid)

        for i in range(0, len(missing), GAMMA_CHUNK):
            chunk = missing[i:i + GAMMA_CHUNK]
            params = [("condition_ids", c) for c in chunk]
            try:
                data = self._request("GET", f"{GAMMA_API}/markets", params=params)
            except PolymarketError as exc:
                log.warning("gamma /markets failed for %d ids: %s", len(chunk), exc)
                continue
            metas = [
                m for m in (self._parse_market(r) for r in
                            (data if isinstance(data, list) else []))
                if m is not None
            ]
            # gamma silently drops unknown condition_ids: ask for 2, get 1, 200 OK.
            by_id, _missing, _unexpected = reconcile_batch(
                chunk, metas, key_of=lambda m: m.condition_id or None,
                what="gamma /markets",
            )
            for cid, meta in by_id.items():
                ttl = self.resolved_market_ttl if meta.closed else self.market_ttl
                self._market_cache[cid] = _Cached(meta, time.monotonic() + ttl)
                out[cid] = meta
        return out

    @staticmethod
    def _parse_market(row: dict[str, Any]) -> MarketMeta | None:
        cid = str(row.get("conditionId") or "")
        if not cid:
            return None
        token_ids = tuple(str(t) for t in parse_json_list(row.get("clobTokenIds")))
        outcomes = tuple(str(o) for o in parse_json_list(row.get("outcomes")))
        prices: list[float] = []
        for p in parse_json_list(row.get("outcomePrices")):
            try:
                prices.append(float(p))
            except (TypeError, ValueError):
                prices.append(float("nan"))

        fee_rate: float | None = None
        schedule = row.get("feeSchedule")
        if isinstance(schedule, dict):
            try:
                fee_rate = float(schedule.get("rate"))
            except (TypeError, ValueError):
                fee_rate = None

        return MarketMeta(
            condition_id=cid,
            question=str(row.get("question") or ""),
            token_ids=token_ids,
            outcomes=outcomes,
            outcome_prices=tuple(prices),
            closed=bool(row.get("closed")),
            accepting_orders=bool(row.get("acceptingOrders")),
            fee_rate=fee_rate,
            fee_type=row.get("feeType"),
            fees_enabled=bool(row.get("feesEnabled")),
            end_date=row.get("endDate"),
        )

    def market_for_condition(self, condition_id: str) -> MarketMeta | None:
        return self.get_markets([condition_id]).get(condition_id)

    # -- fees --------------------------------------------------------------
    def fee_rate_for(self, condition_id: str, fallback: float) -> tuple[float, bool]:
        """Per-market taker fee rate from gamma's feeSchedule.

        Returns (rate, was_fallback). The fallback is logged at WARN every
        time it is used and can never be zero -- config rejects that -- because
        a missing fee field silently becoming free trading is precisely the bug
        that makes paper look better than live at cheap prices.
        """
        now = time.monotonic()
        hit = self._fee_cache.get(condition_id)
        if hit and hit.expires_at > now:
            return hit.value

        result: tuple[float, bool]
        meta = self.market_for_condition(condition_id)
        if meta is None:
            log.warning(
                "FEE FALLBACK: no gamma metadata for conditionId %s -- using fallback rate %.4f",
                condition_id, fallback,
            )
            result = (fallback, True)
        elif not meta.fees_enabled:
            result = (0.0, False)  # genuinely fee-free, confirmed by the API
        elif meta.fee_rate is None or meta.fee_rate <= 0:
            log.warning(
                "FEE FALLBACK: conditionId %s has feesEnabled=%s but rate=%r "
                "-- using fallback rate %.4f",
                condition_id, meta.fees_enabled, meta.fee_rate, fallback,
            )
            result = (fallback, True)
        else:
            result = (meta.fee_rate, False)

        self._fee_cache[condition_id] = _Cached(result, now + self.fee_rate_ttl)
        return result
