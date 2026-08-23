"""Copy logic: the buy path, the sell path, the resolution path, and marking.

No network calls happen here directly -- everything goes through `Executor` and
`PolymarketClient`, so the whole file is testable against fake books.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import hashlib

from . import limits
from .config import Config
from .db import Database, now_ts
from .executor import Executor
from .models import (ExitPath, MarkSource, Side, SkipReason, TargetTrade,
                     entry_band)
from .polymarket import PolymarketClient

log = logging.getLogger(__name__)


@dataclass
class PollCounters:
    seen: int = 0
    copied: int = 0
    rested: int = 0
    mirrored: int = 0
    skipped: int = 0
    ignored: int = 0
    errors: int = 0
    reasons: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: SkipReason) -> None:
        self.skipped += 1
        self.reasons[reason.value] = self.reasons.get(reason.value, 0) + 1


def _parse_end_date(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(
            value.replace("Z", "+00:00")
        ).astimezone(timezone.utc).timestamp())
    except (ValueError, TypeError):
        return None


class Strategy:
    def __init__(self, cfg: Config, db: Database, executor: Executor,
                 client: PolymarketClient, clock=time.time, run_id: str = ""):
        self.cfg = cfg
        self.db = db
        self.executor = executor
        self.client = client
        self.clock = clock
        self.run_id = run_id

    def stake_variant(self, token_id: str) -> float:
        """Which stake this token is assigned to, before the exchange floor.

        Resting orders are punished by queue position rather than depth, so a
        smaller order may fill far more often. Assigning variants per token --
        deterministically, so a restart keeps the assignment -- turns stake into
        a tested variable instead of a constant nobody questioned.
        """
        variants = self.cfg.stake_variants_usd
        if len(variants) <= 1:
            return float(variants[0]) if variants else self.cfg.stake_per_copy_usd
        digest = hashlib.sha256(token_id.encode()).digest()
        return float(variants[digest[0] % len(variants)])

    def budget_for(self, token_id: str, book=None, price: float | None = None) -> float:
        """The hard per-token cap actually used, floor included.

        Two things force this to be more than the hash:

        1. The exchange minimum is a share count, so its dollar value rises with
           price. At the 5-share minimum a $1 stake is unplaceable above 20c --
           which would silently drop the entire 20-30c band out of the $1 arm
           rather than measuring it. Snapping up to the smallest variant that
           IS placeable keeps the arms at declared sizes and keeps the signal.
           The arms still cover different price populations below 20c; that
           confound is physical, not fixable here, and is why variants are
           compared within price bands rather than pooled.
        2. It must be assigned once. Recomputing per fill would let a cheaper
           later price hand a token a smaller cap than it has already spent.
        """
        assigned = self.db.assigned_budget(token_id)
        if assigned:
            return assigned
        if self.cfg.entry_mode != "limit":
            # Variants exist because a resting order is punished by queue
            # position, not by depth. Crossing has no such trade-off -- depth
            # cost is already measured by the shadow ladder -- so varying size
            # in market mode would add noise to a run that cannot answer the
            # question anyway.
            return self.cfg.stake_per_copy_usd
        nominal = self.stake_variant(token_id)
        if book is None or price is None:
            return nominal
        floor_usd = self._min_tranche_usd(book, price)
        if floor_usd <= nominal + 1e-9:
            return nominal
        placeable = [float(v) for v in sorted(self.cfg.stake_variants_usd)
                     if v >= floor_usd - 1e-9]
        return placeable[0] if placeable else nominal

    def _now(self) -> int:
        return int(self.clock())

    # =====================================================================
    # Trade dispatch
    # =====================================================================
    def process_trades(self, trades: list[TargetTrade]) -> PollCounters:
        """Oldest first, so his scale-ins accumulate in the order he made them.

        Each trade is wrapped individually: one malformed trade must never kill
        the loop or block the trades behind it.
        """
        counters = PollCounters()
        for trade in sorted(trades, key=lambda t: t.traded_ts):
            counters.seen += 1
            try:
                if self.db.has_processed(trade.trade_key):
                    continue
                if trade.side is Side.BUY:
                    self._handle_buy(trade, counters)
                elif trade.side is Side.SELL:
                    self._handle_sell(trade, counters)
                else:
                    self._record_only(trade, "ignored", counters)
            except Exception:
                counters.errors += 1
                log.exception("trade %s (%s %s @ %s) failed; continuing",
                              trade.trade_key[:12], trade.side.value,
                              trade.token_id[:16], trade.price)
        return counters

    def _record_only(self, trade: TargetTrade, action: str,
                     counters: PollCounters) -> None:
        with self.db.tx() as conn:
            self.db.record_processed(trade, action, conn=conn)
        if action == "ignored":
            counters.ignored += 1

    def _skip(self, trade: TargetTrade, reason: SkipReason, counters: PollCounters,
              detail: str = "", best_price=None, would_be_fill=None,
              book=None, rungs=None) -> None:
        with self.db.tx() as conn:
            self.db.record_processed(trade, "skipped", conn=conn)
            self.db.record_skip(reason, trade=trade, question=trade.title,
                                detail=detail, best_price=best_price,
                                would_be_fill=would_be_fill, conn=conn)
            if rungs:
                self.db.record_shadow_ladder(
                    conn, rungs, trade=trade, book=book, question=trade.title,
                    outcome=f"skipped:{reason.value}",
                    his_usd_size=trade.usd_size,
                )
        counters.skip(reason)
        log.info("SKIP %s %s @ %.4f -- %s: %s", trade.side.value,
                 trade.title[:44] or trade.token_id[:16], trade.price,
                 reason.value, detail)

    # =====================================================================
    # Buy path
    # =====================================================================
    def _handle_buy(self, trade: TargetTrade, counters: PollCounters) -> None:
        # Cheap local gates first; the network call is the last thing we do.
        if trade.price >= self.cfg.max_entry_price:
            return self._skip(
                trade, SkipReason.PRICE_ABOVE_MAX_ENTRY, counters,
                f"he bought at {trade.price:.4f}, our max entry is "
                f"{self.cfg.max_entry_price:.4f}",
            )

        age = self._now() - trade.traded_ts
        if age > self.cfg.max_trade_age_seconds:
            return self._skip(
                trade, SkipReason.TRADE_TOO_OLD, counters,
                f"trade is {age}s old, max age is "
                f"{self.cfg.max_trade_age_seconds}s (downtime catch-up)",
            )

        # From here a book is worth fetching: the signal is in-universe and
        # fresh, so even if we decline to act, the capacity curve is real data.
        decision_ts = self._now()
        book = self.executor.get_book(trade.token_id)
        rungs = self.executor.shadow_ladder(
            book, self._ladder_rungs(trade), decision_ts=decision_ts
        )

        # Commitments, not fills: an order resting at his price is money the
        # cap has to know about before the tape decides whether it fills.
        copies = self.db.count_commitments_for_token(trade.token_id)
        spent = self.db.committed_on_token(trade.token_id)
        budget = self.budget_for(trade.token_id, book, trade.price)
        remaining = budget - spent

        # Follow him down. He averages ~5 fills per token and lands well below
        # his opener, so taking only his first fill makes his VWAP unreachable
        # by construction. The schedule splits one budget across his successive
        # fills; it never adds a second budget.
        #
        # The schedule is the intended split and the exchange minimum is a hard
        # floor under each tranche. Nothing is ever rounded UP to consume the
        # rest of the budget: an unspendable remainder costs us an
        # under-deployed token, while absorbing it costs us his opening price
        # -- the single worst price he pays -- and the whole thesis is that his
        # opener is the price we must not be stuck at. The remainder is not
        # stranded either: the floor is a share count, so it shrinks with price,
        # and the fills we are waiting for are cheaper ones.
        floor_usd = self._min_tranche_usd(book, trade.price)
        stake = 0.0
        if copies < self.cfg.max_copies_per_token and remaining > 0.01:
            schedule = self.cfg.stake_schedule
            shaped = budget * schedule[min(copies, len(schedule) - 1)]
            stake = min(max(shaped, floor_usd), remaining)
            if stake < floor_usd - 1e-9:
                stake = 0.0  # what is left cannot be placed at this price

        if stake <= 0.01:
            reason = (SkipReason.ALREADY_AT_MAX_COPIES
                      if copies >= self.cfg.max_copies_per_token
                      else SkipReason.TOKEN_BUDGET_SPENT)
            log.debug("no tranche for %s: %d copies, $%.4f of $%.2f left, "
                      "floor $%.4f", trade.token_id[:16], copies, remaining,
                      budget, floor_usd)
            # We are not copying this fill, but he is still scaling in. His
            # average entry keeps improving while ours is frozen, and that gap
            # is the whole point of tracking it -- so record the fill and
            # refresh the position's view of his side before skipping.
            self._skip(
                trade, reason, counters,
                f"already put ${spent:.2f} of the ${budget:.2f} per-token budget "
                f"into this token across {copies} fill(s)",
                book=book, rungs=rungs,
            )
            self._refresh_his_position(trade.token_id)
            return

        # The cap is absolute. If this ever trips, the ledger and the gate
        # disagree and no further money should go in on a guess.
        if spent + stake > budget + 1e-6:
            raise AssertionError(
                f"per-token budget breach on {trade.token_id[:16]}: already "
                f"${spent:.4f} + ${stake:.4f} exceeds ${budget:.4f}"
            )

        cash = self.db.cash()
        if cash < stake:
            return self._skip(
                trade, SkipReason.NOT_ENOUGH_CASH, counters,
                f"free cash ${cash:.2f} is below the ${stake:.2f} needed for this fill",
                book=book, rungs=rungs,
            )

        if self.cfg.entry_mode == "limit":
            return self._rest_at_his_price(trade, stake, budget, book,
                                           decision_ts, counters, rungs)

        # The real decision uses the SAME snapshot the ladder was measured on,
        # so the two are directly comparable and no second fetch can slip a
        # different (possibly better) book into the fill.
        fill = self.executor.buy(trade.token_id, stake,
                                 book=book, decision_ts=decision_ts)
        if not fill.filled:
            return self._skip(
                trade, fill.skip_reason or SkipReason.BOOK_TOO_THIN, counters,
                fill.detail, best_price=fill.best_price,
                would_be_fill=fill.would_be_avg_price,
                book=book, rungs=rungs,
            )

        return self._record_buy(trade, fill, stake, budget, decision_ts, counters,
                                book, rungs, path="market")

    def _record_buy(self, trade: TargetTrade, fill, stake: float, budget: float,
                    decision_ts: int, counters: PollCounters, book, rungs,
                    *, path: str = "market") -> None:
        """Commit a taker fill: ledger, position, ladder and mark, in one
        transaction. Shared by the market path and by the limit path's
        crossed-inside case, so both record the same fields and the only
        difference between them is the recorded `path`."""
        band = entry_band(fill.avg_price)
        latency = decision_ts - trade.traded_ts
        slip_abs = fill.avg_price - trade.price
        slip_pct = (slip_abs / trade.price * 100.0) if trade.price > 0 else 0.0

        with self.db.tx() as conn:
            # Record his fill first so the VWAP below includes it.
            self.db.record_processed(trade, "copied", conn=conn)
            his_vwap, his_n, his_shares, his_usd = self.db.his_vwap_entry(trade.token_id)
            his_vwap = his_vwap if his_vwap is not None else trade.price

            slip_vwap = fill.avg_price - his_vwap
            slip_vwap_pct = (slip_vwap / his_vwap * 100.0) if his_vwap > 0 else 0.0
            existing = self.db.open_position_for_token(trade.token_id)
            if existing is not None:
                # Following him down adds to the SAME position rather than
                # opening another. Three tranches into one token is one holding
                # with an averaged entry -- anything else makes "our VWAP vs
                # his VWAP" per token meaningless and settles the token three
                # times over.
                position_id = existing["id"]
                shares_total = existing["shares"] + fill.shares
                gross_total = (existing["our_avg_fill"] * existing["shares_opened"]
                               + fill.gross_usd)
                shares_opened = existing["shares_opened"] + fill.shares
                our_vwap = gross_total / shares_opened if shares_opened else fill.avg_price
                self.db.update_position(
                    conn, position_id,
                    shares=shares_total,
                    shares_opened=shares_opened,
                    cost_basis_usd=existing["cost_basis_usd"] + fill.gross_usd + fill.fee_usd,
                    cost_basis_opened=existing["cost_basis_opened"] + fill.gross_usd + fill.fee_usd,
                    our_avg_fill=our_vwap,
                    entry_fee_usd=existing["entry_fee_usd"] + fill.fee_usd,
                    entry_band=entry_band(our_vwap),
                    his_vwap_entry=his_vwap,
                    his_fill_count=his_n,
                    his_total_shares=his_shares,
                    his_total_usd=his_usd,
                    his_position_usd_total=his_usd,
                    size_ratio_vs_total=((existing["cost_basis_opened"] + fill.gross_usd
                                          + fill.fee_usd) / his_usd) if his_usd > 0 else None,
                    slippage_vs_his_entry=our_vwap - existing["his_first_price"],
                    slippage_vs_his_entry_pct=(
                        (our_vwap - existing["his_first_price"]) / existing["his_first_price"] * 100.0
                        if existing["his_first_price"] else None),
                    slippage_vs_his_vwap=our_vwap - his_vwap,
                    slippage_vs_his_vwap_pct=(
                        (our_vwap - his_vwap) / his_vwap * 100.0 if his_vwap > 0 else None),
                    last_mark_price=fill.avg_price, last_mark_ts=decision_ts,
                )
            else:
                position_id = self.db.insert_position(
                    conn, run_id=self.run_id, stake_variant_usd=budget,
                    token_id=trade.token_id, condition_id=trade.condition_id,
                    question=trade.title, outcome=trade.outcome,
                    outcome_index=trade.outcome_index,
                    status="open", opened_ts=decision_ts,
                    shares=fill.shares, shares_opened=fill.shares,
                    cost_basis_usd=fill.gross_usd + fill.fee_usd,
                    cost_basis_opened=fill.gross_usd + fill.fee_usd,
                    our_avg_fill=fill.avg_price, entry_fee_usd=fill.fee_usd,
                    entry_band=band,
                    his_first_price=trade.price, his_first_ts=trade.traded_ts,
                    his_vwap_entry=his_vwap, his_fill_count=his_n,
                    his_total_shares=his_shares, his_total_usd=his_usd,
                    # Two size ratios: against the fill we copied, and against his
                    # whole position, which is the one that says how much bigger
                    # our bet is than his conviction.
                    size_ratio=(stake / trade.usd_size) if trade.usd_size > 0 else None,
                    his_position_usd_at_copy=his_usd,
                    his_position_usd_total=his_usd,
                    size_ratio_vs_total=(stake / his_usd) if his_usd > 0 else None,
                    slippage_vs_his_entry=slip_abs,
                    slippage_vs_his_entry_pct=slip_pct,
                    slippage_vs_his_vwap=slip_vwap,
                    slippage_vs_his_vwap_pct=slip_vwap_pct,
                    last_mark_price=fill.avg_price, last_mark_ts=decision_ts,
                    last_mark_source=MarkSource.ENTRY_PRICE.value,
                )
            self.db.insert_execution(
                conn, position_id=position_id, trade_key=trade.trade_key,
                token_id=trade.token_id, condition_id=trade.condition_id,
                question=trade.title, side=Side.BUY.value, ts=decision_ts,
                shares=fill.shares, avg_fill=fill.avg_price,
                gross_usd=fill.gross_usd, fee_usd=fill.fee_usd,
                net_usd=fill.net_usd, levels_consumed=fill.levels_consumed,
                fee_rate_used=fill.fee_rate_used,
                fee_rate_was_fallback=1 if fill.fee_rate_was_fallback else 0,
                entry_band=band, his_price=trade.price, his_ts=trade.traded_ts,
                his_vwap_at_copy=his_vwap, latency_seconds=latency,
                size_ratio=(stake / trade.usd_size) if trade.usd_size > 0 else None,
                slippage_vs_his_entry=slip_abs,
                slippage_vs_his_entry_pct=slip_pct,
            )
            self.db.record_shadow_ladder(
                conn, rungs, trade=trade, book=book, question=trade.title,
                outcome="copied", his_usd_size=trade.usd_size,
            )
            self.db.record_mark(
                conn, position_id, trade.token_id, decision_ts,
                book.best_bid, book.best_ask, fill.avg_price, book.spread,
                MarkSource.ENTRY_PRICE.value,
            )

        counters.copied += 1
        log.info("BUY  $%.2f -> %.4f shares @ %.4f (he paid %.4f, %+.1f%%, "
                 "%d level(s), %ds late) %s",
                 stake, fill.shares, fill.avg_price,
                 trade.price, slip_pct, fill.levels_consumed, latency,
                 trade.title[:44])
        if slip_pct > self.cfg.slippage_warn_pct:
            log.warning("SLIPPAGE %+.1f%% on %s -- above the %.0f%% warning level",
                        slip_pct, trade.title[:44], self.cfg.slippage_warn_pct)

    def _min_tranche_usd(self, book, price: float) -> float:
        """Smallest placeable order at this price, in dollars.

        Mode-dependent, because the stake includes fees. A maker fill pays
        nothing (`takerOnly: true`), so the floor is just `min_shares x price`.
        A taker fill pays `rate x p x (1-p)` per share, which at 29c lifts the
        5-share floor from $1.45 to $1.50 -- and two of those no longer fit in
        a $3 budget. So the same price supports two tranches resting and only
        one crossing.
        """
        if not self.cfg.respect_min_order_size or price <= 0:
            return 0.0
        per_share = price
        crossing = (self.cfg.entry_mode == "market"
                    or limits.marketable(book, price))
        if crossing:
            # The stake is total outlay, fee included, so clearing a 5-share
            # minimum as a taker costs 5 x (p + fee_per_share). A resting order
            # that never crosses pays no fee at all, so its floor is just the
            # shares. Limit mode still crosses when the book is already offering
            # at or below his price, and that case pays the taker fee like any
            # other -- so the floor follows what will actually happen, not what
            # the mode is called.
            per_share += self.cfg.fee_rate_fallback * price * (1.0 - price)
        return book.min_order_size * per_share

    # =====================================================================
    # Limit path
    # =====================================================================
    def _rest_at_his_price(self, trade: TargetTrade, stake: float, budget: float,
                           book, decision_ts: int, counters: PollCounters,
                           rungs) -> None:
        """Place a resting buy at his exact fill price. Never cross.

        Nothing is committed here: no position, no cash movement. The order
        either fills from the tape on a later poll or expires unfilled, and
        which of those happens is the measurement the experiment turns on.
        """
        if limits.marketable(book, trade.price):
            # The book is already offering at or below his price, so resting
            # would cross. That is a windfall, not slippage -- take it as a
            # taker, at a price no worse than his, and record it as such.
            fill = self.executor.buy(trade.token_id, stake, book=book,
                                     decision_ts=decision_ts)
            if not fill.filled:
                return self._skip(trade, fill.skip_reason or SkipReason.BOOK_TOO_THIN,
                                  counters, fill.detail, book=book, rungs=rungs)
            return self._record_buy(trade, fill, stake, budget, decision_ts,
                                    counters, book, rungs, path="crossed_inside")

        order = self.executor.place_limit(
            trade.token_id, stake, trade.price, book=book, now=decision_ts,
            condition_id=trade.condition_id, his_trade_key=trade.trade_key,
            question=trade.title,
        )
        with self.db.tx() as conn:
            self.db.record_processed(trade, "rested", conn=conn)
            self.db.upsert_resting(conn, order, run_id=self.run_id,
                                   status="resting", stake_variant=budget)
            self.db.record_shadow_ladder(conn, rungs, trade=trade, book=book,
                                         question=trade.title, outcome="rested",
                                         his_usd_size=trade.usd_size)
        counters.rested += 1
        log.info("REST $%.2f at %.4f (his price) on %s -- %.0f shares ahead in queue",
                 stake, trade.price, trade.title[:44], order.queue_ahead_shares)

    def poll_resting_orders(self) -> dict[str, int]:
        """Advance every resting order against the tape, and settle expiries."""
        stats = {"resting": 0, "filled": 0, "partial": 0, "expired": 0}
        now = self._now()
        for row in self.db.open_resting():
            try:
                order = self._rebuild_order(row)
                order = self.executor.poll_limit(order, now)
                gained = order.filled_shares - (row["filled_shares"] or 0.0)
                if gained > 1e-9:
                    self._book_limit_fill(order, gained, row)

                if order.is_complete:
                    status = "filled"
                elif order.expired(now):
                    status = "partial" if order.filled_shares > 0 else "expired"
                else:
                    status = "resting"
                stats[status] = stats.get(status, 0) + 1
                with self.db.tx() as conn:
                    self.db.upsert_resting(conn, order, run_id=self.run_id,
                                           status=status,
                                           stake_variant=row["stake_variant_usd"])
                    if status != "resting":
                        self.db.close_resting(conn, order.his_trade_key, status)
                if status in ("expired", "partial"):
                    log.info("ORDER %s at %.4f: %s", status, order.limit_price,
                             limits.describe(order))
            except Exception:
                log.exception("resting order %s failed to advance; continuing",
                              row["trade_key"])
        return stats

    def _rebuild_order(self, row) -> "limits.RestingOrder":
        return limits.RestingOrder(
            token_id=row["token_id"], condition_id=row["condition_id"] or "",
            limit_price=row["limit_price"], usd_budget=row["usd_budget"],
            target_shares=row["target_shares"], placed_ts=row["placed_ts"],
            expires_ts=row["expires_ts"],
            queue_ahead_shares=row["queue_ahead_shares"],
            his_price=row["his_price"], his_trade_key=row["trade_key"],
            question=row["question"] or "",
            filled_shares=row["filled_shares"] or 0.0,
            filled_usd=row["filled_usd"] or 0.0,
            fee_usd=row["fee_usd"] or 0.0,
            consumed_shares=row["consumed_shares"] or 0.0,
            prints_observed=row["prints_observed"] or 0,
            alt_filled_shares=row["alt_filled_shares"] or 0.0,
            alt_consumed_shares=row["alt_consumed_shares"] or 0.0,
            alt_prints_observed=row["alt_prints_observed"] or 0,
        )

    def _book_limit_fill(self, order, gained_shares: float, row) -> None:
        """Turn an incremental resting fill into position and ledger rows."""
        ts = self._now()
        gross = gained_shares * order.limit_price
        fee = 0.0  # maker: takerOnly is true on every live schedule sampled
        cash = self.db.cash()
        if cash < gross:
            log.warning("resting fill on %s needs $%.2f but only $%.2f is free; "
                        "booking what we can afford", order.token_id[:16], gross, cash)
            if cash <= 0:
                return
            gained_shares = cash / order.limit_price
            gross = cash

        band = entry_band(order.limit_price)
        with self.db.tx() as conn:
            existing = self.db.open_position_for_token(order.token_id)
            his_vwap, his_n, his_shares, his_usd = self.db.his_vwap_entry(order.token_id)
            his_vwap = his_vwap or order.his_price
            if existing is not None:
                shares_opened = existing["shares_opened"] + gained_shares
                gross_total = existing["our_avg_fill"] * existing["shares_opened"] + gross
                vwap = gross_total / shares_opened if shares_opened else order.limit_price
                position_id = existing["id"]
                self.db.update_position(
                    conn, position_id,
                    shares=existing["shares"] + gained_shares,
                    shares_opened=shares_opened,
                    cost_basis_usd=existing["cost_basis_usd"] + gross,
                    cost_basis_opened=existing["cost_basis_opened"] + gross,
                    our_avg_fill=vwap, entry_band=entry_band(vwap),
                    his_vwap_entry=his_vwap, his_fill_count=his_n,
                    his_total_shares=his_shares, his_total_usd=his_usd,
                    his_position_usd_total=his_usd,
                    slippage_vs_his_vwap=vwap - his_vwap,
                    slippage_vs_his_vwap_pct=((vwap - his_vwap) / his_vwap * 100.0
                                              if his_vwap > 0 else None),
                    last_mark_price=order.limit_price, last_mark_ts=ts,
                )
            else:
                position_id = self.db.insert_position(
                    conn, run_id=self.run_id,
                    stake_variant_usd=row["stake_variant_usd"],
                    token_id=order.token_id, condition_id=order.condition_id,
                    question=order.question, outcome="", outcome_index=0,
                    status="open", opened_ts=ts,
                    shares=gained_shares, shares_opened=gained_shares,
                    cost_basis_usd=gross, cost_basis_opened=gross,
                    our_avg_fill=order.limit_price, entry_fee_usd=0.0,
                    entry_band=band,
                    his_first_price=order.his_price, his_first_ts=order.placed_ts,
                    his_vwap_entry=his_vwap, his_fill_count=his_n,
                    his_total_shares=his_shares, his_total_usd=his_usd,
                    his_position_usd_at_copy=his_usd,
                    his_position_usd_total=his_usd,
                    slippage_vs_his_entry=order.limit_price - order.his_price,
                    slippage_vs_his_entry_pct=0.0,
                    slippage_vs_his_vwap=order.limit_price - his_vwap,
                    slippage_vs_his_vwap_pct=((order.limit_price - his_vwap)
                                              / his_vwap * 100.0 if his_vwap > 0 else None),
                    last_mark_price=order.limit_price, last_mark_ts=ts,
                    last_mark_source=MarkSource.ENTRY_PRICE.value,
                )
            self.db.insert_execution(
                conn, position_id=position_id, trade_key=order.his_trade_key,
                token_id=order.token_id, condition_id=order.condition_id,
                question=order.question, side=Side.BUY.value, ts=ts,
                shares=gained_shares, avg_fill=order.limit_price,
                gross_usd=gross, fee_usd=fee, net_usd=-(gross + fee),
                levels_consumed=0, fee_rate_used=0.0, fee_rate_was_fallback=0,
                entry_band=band, his_price=order.his_price,
                his_ts=order.placed_ts, his_vwap_at_copy=his_vwap,
                latency_seconds=ts - order.placed_ts,
                slippage_vs_his_entry=0.0, slippage_vs_his_entry_pct=0.0,
                note="resting fill at his price",
            )
        log.info("FILL %.4f shares at %.4f (his price) on %s", gained_shares,
                 order.limit_price, (order.question or order.token_id)[:44])

    def _ladder_rungs(self, trade: TargetTrade) -> list[tuple[str, float]]:
        """Fixed rungs plus his own size.

        The rung at his size is the sharpest one: it says whether his edge is
        genuine or an artifact of trading small enough not to move the book.
        """
        rungs = [(f"${usd:g}", float(usd)) for usd in self.cfg.shadow_ladder_usd]
        if self.cfg.shadow_ladder_include_his_sizes:
            if trade.usd_size > 0:
                rungs.append(("his_fill", trade.usd_size))
            _, _, _, his_usd = self.db.his_vwap_entry(trade.token_id)
            his_position = (his_usd or 0.0) + trade.usd_size
            if his_position > 0:
                rungs.append(("his_position", his_position))
        return rungs

    def _refresh_his_position(self, token_id: str) -> None:
        """Recompute his side of an open position after he adds a fill.

        Our entry is fixed at one copy; his keeps moving as he scales in. If he
        averages down, his effective entry beats ours by construction, and that
        gap only becomes visible if these fields keep updating after the copy.
        """
        position = self.db.open_position_for_token(token_id)
        if position is None:
            return
        his_vwap, his_n, his_shares, his_usd = self.db.his_vwap_entry(token_id)
        if his_vwap is None:
            return
        our_fill = position["our_avg_fill"]
        stake = self.cfg.stake_per_copy_usd
        with self.db.tx() as conn:
            self.db.update_position(
                conn, position["id"],
                his_vwap_entry=his_vwap,
                his_fill_count=his_n,
                his_total_shares=his_shares,
                his_total_usd=his_usd,
                his_position_usd_total=his_usd,
                size_ratio_vs_total=(stake / his_usd) if his_usd > 0 else None,
                slippage_vs_his_vwap=our_fill - his_vwap,
                slippage_vs_his_vwap_pct=(
                    (our_fill - his_vwap) / his_vwap * 100.0 if his_vwap > 0 else None
                ),
            )

    # =====================================================================
    # Sell path
    # =====================================================================
    def _handle_sell(self, trade: TargetTrade, counters: PollCounters) -> None:
        position = self.db.open_position_for_token(trade.token_id)
        if position is None:
            # He sold something we never held. Recorded so his position maths
            # stays right, but there is nothing to mirror.
            return self._record_only(trade, "ignored", counters)

        # His holding BEFORE this sell, which is what the fraction is against.
        his_before = self.db.his_open_shares(trade.token_id)
        if his_before <= 0:
            fraction = 1.0
        else:
            fraction = min(1.0, trade.shares / his_before)
        if not self.cfg.mirror_partial_sells:
            fraction = 1.0

        our_shares = position["shares"]
        to_sell = our_shares * fraction

        decision_ts = self._now()
        book = self.executor.get_book(trade.token_id)

        # A residual below the exchange minimum could never be sold live, so it
        # would sit in paper equity as value we could not realise. Close it all.
        floor = book.min_order_size if self.cfg.respect_min_order_size else 0.0
        remainder = our_shares - to_sell
        closing_all = False
        if fraction >= 1.0 or (0 < remainder < floor) or remainder <= 0:
            to_sell = our_shares
            closing_all = True

        fill = self.executor.sell(trade.token_id, to_sell, book=book,
                                  decision_ts=decision_ts)
        if not fill.filled:
            return self._skip(
                trade, fill.skip_reason or SkipReason.BOOK_TOO_THIN, counters,
                f"wanted to mirror {fraction:.1%} of our position "
                f"({to_sell:.4f} shares): {fill.detail}",
                best_price=fill.best_price, would_be_fill=fill.would_be_avg_price,
            )

        sold = fill.shares
        share_of_position = sold / our_shares if our_shares > 0 else 1.0
        basis_released = position["cost_basis_usd"] * share_of_position
        realised = fill.net_usd - basis_released
        remaining_shares = max(0.0, our_shares - sold)
        remaining_basis = max(0.0, position["cost_basis_usd"] - basis_released)
        # Whether the position is closed depends on what actually SOLD, not on
        # what we intended to sell. A thin bid side can absorb less than the
        # whole position, and closing on intent zeroed the basis of shares we
        # still held -- money that simply vanished from the ledger.
        fully_closed = remaining_shares <= 1e-9

        with self.db.tx() as conn:
            self.db.record_processed(trade, "mirrored", conn=conn)
            self.db.insert_execution(
                conn, position_id=position["id"], trade_key=trade.trade_key,
                token_id=trade.token_id, condition_id=trade.condition_id,
                question=position["question"], side=Side.SELL.value, ts=decision_ts,
                shares=sold, avg_fill=fill.avg_price, gross_usd=fill.gross_usd,
                fee_usd=fill.fee_usd, net_usd=fill.net_usd,
                levels_consumed=fill.levels_consumed,
                fee_rate_used=fill.fee_rate_used,
                fee_rate_was_fallback=1 if fill.fee_rate_was_fallback else 0,
                entry_band=position["entry_band"], his_price=trade.price,
                his_ts=trade.traded_ts, latency_seconds=decision_ts - trade.traded_ts,
                realised_pnl_usd=realised,
                note=f"mirrored {fraction:.1%} of his position"
                     + (" (closed remainder below min order size)"
                        if closing_all and fraction < 1.0 else ""),
            )
            updates = dict(
                shares=remaining_shares,
                cost_basis_usd=0.0 if fully_closed else remaining_basis,
                realised_pnl_usd=position["realised_pnl_usd"] + realised,
                proceeds_usd=position["proceeds_usd"] + fill.net_usd,
                exit_fee_usd=position["exit_fee_usd"] + fill.fee_usd,
            )
            if fully_closed:
                updates.update(status="closed", closed_ts=decision_ts,
                               shares=0.0, cost_basis_usd=0.0,
                               exit_path=ExitPath.MIRRORED_SELL.value)
                updates.update(self._clv_fields(position, decision_ts))
            self.db.update_position(conn, position["id"], **updates)

        counters.mirrored += 1
        log.info("SELL %.4f shares @ %.4f -> $%.2f (P&L %+.2f) %s%s",
                 sold, fill.avg_price, fill.net_usd, realised,
                 "closed" if fully_closed else f"{remaining_shares:.2f} left",
                 " [partial book]" if fill.is_partial else "")
        if closing_all and not fully_closed:
            log.warning(
                "wanted to close %s entirely but the bids only absorbed %.4f of "
                "%.4f shares; %.4f still held and will ride to resolution",
                (position["question"] or position["token_id"])[:44], sold,
                our_shares, remaining_shares,
            )

    # =====================================================================
    # Resolution path
    # =====================================================================
    def check_resolutions(self) -> int:
        positions = self.db.open_positions()
        if not positions:
            return 0
        metas = self.client.get_markets(
            {p["condition_id"] for p in positions}, force=True
        )
        settled = 0
        for position in positions:
            try:
                meta = metas.get(position["condition_id"])
                if meta is None or not meta.closed:
                    continue
                value = meta.settlement_value(position["token_id"])
                if value is None:
                    # Closed but the outcome price is ambiguous. Do not guess a
                    # settlement -- leave it open and look again next cycle.
                    log.warning("market %s is closed but its outcome price is "
                                "ambiguous; not settling", position["condition_id"][:20])
                    continue
                self._settle(position, value)
                settled += 1
            except Exception:
                log.exception("settlement failed for position %s; continuing",
                              position["id"])
        return settled

    def _settle(self, position, value_per_share: float) -> None:
        ts = self._now()
        # Settlement is not a trade: no fee, no book, no slippage.
        proceeds = position["shares"] * value_per_share
        realised = proceeds - position["cost_basis_usd"]
        with self.db.tx() as conn:
            self.db.insert_execution(
                conn, position_id=position["id"], token_id=position["token_id"],
                condition_id=position["condition_id"], question=position["question"],
                side=Side.SETTLE.value, ts=ts, shares=position["shares"],
                avg_fill=value_per_share, gross_usd=proceeds, fee_usd=0.0,
                net_usd=proceeds, entry_band=position["entry_band"],
                realised_pnl_usd=realised,
                note="settled at $1.00 (won)" if value_per_share >= 1.0
                     else "settled at $0.00 (lost)",
            )
            updates = dict(
                status="closed", closed_ts=ts, shares=0.0, cost_basis_usd=0.0,
                realised_pnl_usd=position["realised_pnl_usd"] + realised,
                proceeds_usd=position["proceeds_usd"] + proceeds,
                exit_path=ExitPath.RESOLUTION.value,
                last_mark_price=value_per_share, last_mark_ts=ts,
                last_mark_source=MarkSource.GAMMA_OUTCOME_PRICE.value,
            )
            updates.update(self._clv_fields(position, ts))
            self.db.update_position(conn, position["id"], **updates)
        log.info("SETTLE %s at $%.2f -> $%.2f (P&L %+.2f) %s",
                 position["token_id"][:14], value_per_share, proceeds, realised,
                 (position["question"] or "")[:44])

    @staticmethod
    def _clv_fields(position, closed_ts: int) -> dict:
        """Freeze the closing line at close.

        The closing line is the last BOOK-derived mark before resolution. A
        gamma price for a resolved market is 0 or 1, which is the outcome, not
        a line -- using it would make CLV a restatement of the result and
        therefore worthless.
        """
        line = position["closing_line_price"]
        if line is None or not position["closing_line_captured"]:
            return {"closing_line_age_seconds": None}
        entry = position["our_avg_fill"]
        return {
            "clv_abs": line - entry,
            "clv_pct": ((line - entry) / entry * 100.0) if entry > 0 else None,
            "closing_line_age_seconds": (
                closed_ts - position["closing_line_ts"]
                if position["closing_line_ts"] else None
            ),
        }

    # =====================================================================
    # Marking / closing-line capture
    # =====================================================================
    def mark_positions(self) -> dict[str, int]:
        """Mark every open position and roll the closing line forward.

        CLV is the primary metric and it depends on capturing a price from a
        book that empties the instant the market resolves, so the closing line
        is refreshed on every mark rather than sampled once at the end.
        """
        positions = self.db.open_positions()
        if not positions:
            return {}
        books = self.client.get_books([p["token_id"] for p in positions])
        metas = self.client.get_markets({p["condition_id"] for p in positions})
        stats: dict[str, int] = {}
        ts = self._now()

        for position in positions:
            try:
                book = books.get(position["token_id"])
                price = bid = ask = spread = None
                source = MarkSource.LAST_KNOWN

                if book is not None and book.best_bid is not None and book.best_ask is not None:
                    price, bid, ask = book.mid, book.best_bid, book.best_ask
                    spread = book.spread
                    source = MarkSource.BOOK_MID
                elif book is not None and book.best_bid is not None:
                    price = bid = book.best_bid
                    source = MarkSource.BOOK_BID
                else:
                    # Empty book: normal the moment a market resolves. Fall back
                    # to gamma's outcome price -- never to zero, never crash.
                    meta = metas.get(position["condition_id"])
                    gamma_price = meta.price_for_token(position["token_id"]) if meta else None
                    if gamma_price is not None:
                        price, source = gamma_price, MarkSource.GAMMA_OUTCOME_PRICE
                    elif position["last_mark_price"] is not None:
                        price, source = position["last_mark_price"], MarkSource.LAST_KNOWN
                    else:
                        price, source = position["our_avg_fill"], MarkSource.ENTRY_PRICE

                updates = dict(last_mark_price=price, last_mark_bid=bid,
                               last_mark_ask=ask, last_mark_spread=spread,
                               last_mark_ts=ts, last_mark_source=source.value)

                # Only a real book mark can serve as a closing line.
                if source in (MarkSource.BOOK_MID, MarkSource.BOOK_BID):
                    updates.update(closing_line_price=price, closing_line_bid=bid,
                                   closing_line_ask=ask, closing_line_spread=spread,
                                   closing_line_ts=ts, closing_line_captured=1)

                with self.db.tx() as conn:
                    self.db.update_position(conn, position["id"], **updates)
                    # The full path, not just the latest value: fixed-horizon
                    # CLV needs history, and horizons are always capturable
                    # because the book is still live at those points.
                    self.db.record_mark(
                        conn, position["id"], position["token_id"], ts,
                        bid, ask, price, spread, source.value,
                    )
                stats[source.value] = stats.get(source.value, 0) + 1
            except Exception:
                log.exception("marking failed for position %s; continuing",
                              position["id"])
        return stats

    def next_mark_interval(self) -> int:
        """Tighten the marking cadence near a market's endDate.

        A market that resolves between two five-minute marks yields no closing
        line at all, which silently destroys the primary metric for that trade.
        """
        positions = self.db.open_positions()
        if not positions:
            return self.cfg.mark_interval_seconds
        metas = self.client.get_markets({p["condition_id"] for p in positions})
        now = self._now()
        for position in positions:
            meta = metas.get(position["condition_id"])
            end = _parse_end_date(meta.end_date) if meta else None
            if end is not None and 0 <= (end - now) <= self.cfg.near_close_window_seconds:
                return self.cfg.mark_interval_near_close_seconds
        return self.cfg.mark_interval_seconds

    # =====================================================================
    def portfolio_value(self) -> tuple[float, int]:
        """Mark-to-market value of open positions, and how many there are."""
        total = 0.0
        positions = self.db.open_positions()
        for position in positions:
            mark = position["last_mark_price"]
            if mark is None:
                mark = position["our_avg_fill"]
            total += position["shares"] * mark
        return total, len(positions)
