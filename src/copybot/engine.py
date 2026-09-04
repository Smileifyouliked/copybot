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

import dataclasses
import errno
import json
import logging
import logging.handlers
import os
import random
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .db import Database
from .executor import build_executor
from .models import TargetTrade
from .polymarket import PolymarketClient, PolymarketError
from .strategy import Strategy

log = logging.getLogger(__name__)


class AlreadyRunning(RuntimeError):
    """Another process holds the lock on this database."""


class InstanceLock:
    """One bot per database, enforced by an OS lock rather than by discipline.

    This exists because it already happened: a manually started process ran
    alongside the systemd unit for 41 hours against the same file. Nothing was
    double-copied -- dedup held -- but two writers racing the same read-then-write
    window is not a property to leave to luck.

    flock is used rather than a PID file because the kernel releases it when the
    process dies, however it dies. A stale file cannot lock anyone out.
    """

    def __init__(self, db_path: str | Path):
        self.path = Path(str(db_path) + ".lock")
        self._fh = None

    def acquire(self) -> "InstanceLock":
        import fcntl
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # "a+" and not "w": opening for write TRUNCATES, and it happens before
        # the flock, so the loser of the race erased the winner's pid on its way
        # to being refused. The error then had no pid to name, the incumbent's
        # own record was gone for the rest of its life, and the operator was
        # left grepping ps for the process this file exists to identify.
        # Truncation belongs after the lock is ours, not before we ask for it.
        self._fh = open(self.path, "a+")
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._fh.close()
            self._fh = None
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                holder = self._holder()
                raise AlreadyRunning(
                    f"another copybot already holds {self.path}"
                    + (f" (pid {holder})" if holder else "")
                    + " -- stop it before starting this one"
                ) from exc
            raise
        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(f"{os.getpid()}\n")
        self._fh.flush()
        return self

    def _holder(self) -> str:
        try:
            return self.path.read_text().strip()
        except OSError:
            return ""

    def release(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_exc):
        self.release()


def _git_commit() -> str:
    """Which code produced these numbers. Blank if not a checkout."""
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5,
                             cwd=Path(__file__).resolve().parents[2])
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def new_run_id(cfg: Config) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{cfg.entry_mode}"


def register_run(db: Database, cfg: Config, note: str = "") -> str:
    """Stamp this run into the database with the config that produced it.

    Every row we write carries a run_id, so a database that spans a market-order
    run and a limit-order run can still answer either question separately. The
    config is snapshotted as JSON because "what were the settings then" is not
    answerable from a config file that has since been edited.
    """
    run_id = new_run_id(cfg)
    db.start_run(run_id, json.dumps(dataclasses.asdict(cfg), sort_keys=True,
                                    default=str),
                 cfg.entry_mode, _git_commit(), note)
    return run_id


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
                 strategy: Strategy, lock: InstanceLock | None = None):
        self.cfg = cfg
        self.db = db
        self.client = client
        self.strategy = strategy
        self.lock = lock
        self.limit_mode = cfg.entry_mode == "limit"
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
        # Hand the client what we have already processed, so a full page whose
        # oldest row we have seen is recognised as covering the gap rather than
        # warned about on every single poll.
        known = self.db.processed_keys()
        rows = self.client.get_activity(
            self.cfg.target_wallet, limit=self.cfg.activity_fetch_limit,
            seen_keys=known,
        )
        trades, malformed = [], 0
        for row in rows:
            try:
                trades.append(TargetTrade.from_activity(row))
            except ValueError as exc:
                malformed += 1
                # Recorded, not just logged: the row stays in the /activity
                # window for days, so warning without remembering re-warns on
                # every poll forever, and a dropped signal that appears in no
                # skip count is a hole in the taxonomy the analysis reads.
                if self.db.record_malformed_activity(row, str(exc)):
                    log.warning("malformed activity row skipped: %s", exc)

        # Cheap pre-filter: most rows on any given poll are ones we have already
        # seen, and this keeps the per-trade work proportional to what is new.
        fresh = [t for t in trades if t.trade_key not in known]

        counters = self.strategy.process_trades(fresh)

        # Resting orders are advanced on every pass, before anything else can
        # sleep: an order only fills against prints inside its TTL, so a pass we
        # skip is fills we can never see again. This runs even when no new trade
        # arrived, which is most passes.
        resting = self.strategy.poll_resting_orders() if self.limit_mode else {}

        self.db.write_heartbeat(
            ok=True, trades_seen=len(rows), copies_made=counters.copied,
            skips=counters.skipped,
            note=f"new={len(fresh)} rested={counters.rested} "
                 f"mirrored={counters.mirrored} "
                 f"ignored={counters.ignored} malformed={malformed} "
                 f"errors={counters.errors}"
                 + (f" orders={resting}" if resting else ""),
        )
        if resting.get("filled") or resting.get("partial") or resting.get("expired"):
            log.info("resting orders: %s", resting)
        if counters.copied or counters.rested or counters.mirrored or counters.skipped:
            log.info("pass: %d new of %d rows -> %d copied, %d rested, %d mirrored, "
                     "%d skipped", len(fresh), len(rows), counters.copied,
                     counters.rested, counters.mirrored, counters.skipped)

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
        log.info("copybot starting in %s mode | %s entries | wallet %s | "
                 "$%.2f capital | $%.2f per token | polling every %ds | run %s",
                 self.cfg.mode, self.cfg.entry_mode, self.cfg.target_wallet,
                 self.cfg.starting_capital_usd, self.cfg.stake_per_copy_usd,
                 self.cfg.poll_interval_seconds, self.strategy.run_id or "-")
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

        if self.lock is not None:
            self.lock.release()
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
    # The lock is taken before the database is opened, so a second instance
    # fails before it can write anything at all.
    lock = InstanceLock(cfg.db_path).acquire()
    db = Database(cfg.db_path, cfg.starting_capital_usd)
    run_id = register_run(db, cfg)
    strategy_client = PolymarketClient()
    strategy = Strategy(cfg, db, build_executor(cfg, strategy_client),
                        strategy_client, run_id=run_id)
    engine = Engine(cfg, db, strategy_client, strategy, lock=lock)
    signal.signal(signal.SIGTERM, engine.request_stop)
    signal.signal(signal.SIGINT, engine.request_stop)
    return engine
