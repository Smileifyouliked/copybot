"""Entry point.

    python -m copybot.main run          # the polling loop
    python -m copybot.main dashboard    # the web dashboard
    python -m copybot.main report       # the dashboard as text, no browser
    python -m copybot.main status       # one-line state dump
"""
from __future__ import annotations

import argparse
import sys

from .config import ConfigError, load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="copybot")
    parser.add_argument("command",
                        choices=["run", "dashboard", "report", "status"])
    parser.add_argument("-c", "--config", default="config.yaml")
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if args.command == "run":
        from .engine import build_engine, setup_logging
        setup_logging(cfg)
        build_engine(cfg).run()
        return 0

    if args.command == "dashboard":
        import uvicorn

        from .dashboard import create_app
        # config.py refuses any bind address that is not loopback: the
        # dashboard has no authentication and must stay behind an SSH tunnel.
        uvicorn.run(create_app(cfg), host=cfg.dashboard_host,
                    port=cfg.dashboard_port, log_level="warning")
        return 0

    from .db import Database
    db = Database(cfg.db_path, cfg.starting_capital_usd)

    if args.command == "report":
        from .textreport import render
        try:
            print(render(cfg, db))
            return 0
        finally:
            db.close()

    try:
        value = sum(
            r["shares"] * (r["last_mark_price"] if r["last_mark_price"] is not None
                           else r["our_avg_fill"])
            for r in db.open_positions()
        )
        cash = db.cash()
        ok, parts = db.reconcile()
        heartbeat = db.last_heartbeat()
        print(f"mode            : {cfg.mode}")
        print(f"cash            : ${cash:,.2f}")
        print(f"open positions  : {len(db.open_positions())} worth ${value:,.2f}")
        print(f"total equity    : ${cash + value:,.2f}  "
              f"(started ${cfg.starting_capital_usd:,.2f})")
        print(f"realised P&L    : ${db.realised_pnl():+,.2f}")
        print(f"his trades seen : {len(db.processed_keys())}")
        print(f"reconciles      : {ok}  {parts if not ok else ''}")
        print(f"last heartbeat  : {heartbeat['ts'] if heartbeat else 'never'}")
        return 0 if ok else 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
