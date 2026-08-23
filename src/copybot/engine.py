"""The polling loop.

Reliability rules this file exists to enforce:

  * A heartbeat is written on EVERY pass, successful or not. A loop that is
    alive but failing must look different from a loop that has died, and both
    must look different from a healthy one.
  * One bad trade never kills the loop (handled per-trade in `Strategy`), and
    one bad poll never kills the process.
  * API failures back off exponentially and recover. The bot survives the API
    being down for an hour without dying or hot-spinning.
"""
from __future__ import annotations

import logging
import logging.handlers
import random
import signal
import time
from pathlib import Path

from .config import Config
from .db import Database
from .executor import build_executor
from .models import TargetTrade
from .polymarket import PolymarketClient, PolymarketError
from .strategy import Strategy

log = logging.getLogger(__name__)


def setup_logging(cfg: Config) -> None:
    """Rotating file log at INFO, timestamps in UTC."""
    path = Path(cfg.log_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s UTC %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    formatter.converter = time.gmtime  # UTC, not local

    file_handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=cfg.log_max_bytes, backupCount=cfg.log_backup_count
    )
    file_handler.setFormatter(formatter)

    console = logging.StreamHandler()
    console.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(console)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


class Engine:
    def __init__(self, cfg: Config, db: Database, client: PolymarketClient,
                 strategy: Strategy):
        self.cfg = cfg
        self.db = db
        self.client = client
        self.strategy = strategy
        self.running = True

        now = time.monotonic()
        self._next_mark = now
        self._next_resolution = now
        self._next_equity = now
        self._consecutive_failures = 0

    def request_stop(self, *_args) -> None:
        log.info("shutdown requested; finishing the current pass")
        self.running = False

    # -- one pass ----------------------------------------------------------
    def poll_once(self) -> None:
        rows = self.client.get_activity(
            self.cfg.target_wallet, limit=self.cfg.activity_fetch_limit
        )
        trades, malformed = [], 0
        for row in rows:
            try:
                trades.append(TargetTrade.from_activity(row))
            except ValueError as exc:
                malformed += 1
                log.warning("malformed activity row skipped: %s", exc)

        # Cheap pre-filter: most rows on any given poll are ones we have already
        # seen, and this keeps the per-trade work proportional to what is new.
        known = self.db.processed_keys()
        fresh = [t for t in trades if t.trade_key not in known]

        counters = self.strategy.process_trades(fresh)
        self.db.write_heartbeat(
            ok=True, trades_seen=len(rows), copies_made=counters.copied,
            skips=counters.skipped,
            note=f"new={len(fresh)} mirrored={counters.mirrored} "
                 f"ignored={counters.ignored} malformed={malformed} "
                 f"errors={counters.errors}",
        )
        if counters.copied or counters.mirrored or counters.skipped:
            log.info("pass: %d new of %d rows -> %d copied, %d mirrored, %d skipped",
                     len(fresh), len(rows), counters.copied, counters.mirrored,
                     counters.skipped)

    def periodic_work(self) -> None:
        now = time.monotonic()

        if now >= self._next_mark:
            stats = self.strategy.mark_positions()
            interval = self.strategy.next_mark_interval()
            self._next_mark = now + interval
            if stats:
                log.info("marked %d position(s) %s (next in %ds)",
                         sum(stats.values()), stats, interval)

        if now >= self._next_resolution:
            settled = self.strategy.check_resolutions()
            self._next_resolution = now + self.cfg.resolution_check_interval_seconds
            if settled:
                log.info("settled %d position(s)", settled)

        if now >= self._next_equity:
            value, count = self.strategy.portfolio_value()
            cash = self.db.cash()
            self.db.write_equity_snapshot(cash, value, count, self.db.realised_pnl())
            self._next_equity = now + self.cfg.equity_snapshot_interval_seconds
            ok, parts = self.db.reconcile()
            if not ok:
                # The ledger disagreeing with itself is a correctness failure,
                # not a warning. Loud, but not fatal: the data is still intact.
                log.error("LEDGER DOES NOT RECONCILE: %s", parts)
            log.info("equity $%.2f (cash $%.2f + %d position(s) worth $%.2f)",
                     cash + value, cash, count, value)
            self.db.prune_heartbeats()

    # -- the loop ----------------------------------------------------------
    def run(self) -> None:
        log.info("copybot starting in %s mode | wallet %s | $%.2f capital | "
                 "$%.2f per copy | polling every %ds",
                 self.cfg.mode, self.cfg.target_wallet, self.cfg.starting_capital_usd,
                 self.cfg.stake_per_copy_usd, self.cfg.poll_interval_seconds)
        ok, parts = self.db.reconcile()
        log.info("recovered state: cash $%.2f, %d open position(s), "
                 "realised P&L $%+.2f, %d of his trades already seen (reconciles: %s)",
                 self.db.cash(), len(self.db.open_positions()), self.db.realised_pnl(),
                 len(self.db.processed_keys()), ok)
        if not ok:
            log.error("LEDGER DOES NOT RECONCILE AT STARTUP: %s", parts)

        while self.running:
            started = time.monotonic()
            try:
                self.poll_once()
                self.periodic_work()
                self._consecutive_failures = 0
                delay = self.cfg.poll_interval_seconds
            except PolymarketError as exc:
                self._consecutive_failures += 1
                delay = self._backoff()
                self.db.write_heartbeat(
                    ok=False, error=str(exc)[:400],
                    note=f"api failure #{self._consecutive_failures}, "
                         f"retrying in {delay:.0f}s",
                )
                log.warning("poll failed (%s); backing off %.0fs (failure #%d)",
                            exc, delay, self._consecutive_failures)
            except Exception as exc:  # never let the loop die
                self._consecutive_failures += 1
                delay = self._backoff()
                self.db.write_heartbeat(
                    ok=False, error=f"{type(exc).__name__}: {exc}"[:400],
                    note=f"unexpected failure #{self._consecutive_failures}",
                )
                log.exception("unexpected error in poll loop; continuing")

            elapsed = time.monotonic() - started
            self._sleep(max(0.0, delay - elapsed))

        log.info("copybot stopped cleanly")

    def _backoff(self) -> float:
        """Exponential with jitter, capped. Survives an hour-long outage without
        hot-spinning or giving up."""
        base = min(300.0, self.cfg.poll_interval_seconds * (2 ** min(self._consecutive_failures, 6)))
        return base * (0.5 + random.random())

    def _sleep(self, seconds: float) -> None:
        """Sleep in slices so a shutdown signal is honoured promptly."""
        deadline = time.monotonic() + seconds
        while self.running and time.monotonic() < deadline:
            time.sleep(min(1.0, deadline - time.monotonic()))


def build_engine(cfg: Config) -> Engine:
    db = Database(cfg.db_path, cfg.starting_capital_usd)
    client = PolymarketClient()
    strategy = Strategy(cfg, db, build_executor(cfg, client), client)
    engine = Engine(cfg, db, client, strategy)
    signal.signal(signal.SIGTERM, engine.request_stop)
    signal.signal(signal.SIGINT, engine.request_stop)
    return engine
