"""FastAPI dashboard.

Written for someone who does not know trading vocabulary. No "notional", no
"drawdown", no "mark-to-market" on the main screen -- those live in the
collapsed nerd section.

No authentication, so it binds 127.0.0.1 and is reached over an SSH tunnel.
`config.py` refuses any other bind address.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from .analytics import (band_breakdown, capacity_curve, clv_at_horizons,
                        clv_summary, depth_cost_summary, exit_path_breakdown,
                        expected_vs_actual_winners, fee_summary,
                        slippage_summary, stopping_rules)
from .config import Config
from .db import Database, now_ts

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


# --- formatting helpers ----------------------------------------------------

def cents(price: float | None) -> str:
    """23¢, or 0.3¢ for sub-cent prices. Never rounds a 2c token to a cent."""
    if price is None:
        return "—"
    value = price * 100
    if value >= 10:
        return f"{value:.0f}¢"
    if value >= 1:
        return f"{value:.1f}¢"
    return f"{value:.2f}¢"


def money(amount: float | None) -> str:
    if amount is None:
        return "—"
    return f"-${abs(amount):,.2f}" if amount < 0 else f"${amount:,.2f}"


def ago(seconds: float | None) -> str:
    if seconds is None:
        return "never"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''} ago"
    if seconds < 3600:
        return f"{seconds // 60} minute{'s' if seconds // 60 != 1 else ''} ago"
    if seconds < 86400:
        return f"{seconds // 3600} hour{'s' if seconds // 3600 != 1 else ''} ago"
    return f"{seconds // 86400} day{'s' if seconds // 86400 != 1 else ''} ago"


def held_for(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    seconds = int(seconds)
    if seconds < 3600:
        return f"{max(1, seconds // 60)} min"
    if seconds < 86400:
        return f"{seconds / 3600:.1f} hours"
    return f"{seconds / 86400:.1f} days"


def clock(ts: int | None) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%-I:%M %p")


# --- plain-English activity feed -------------------------------------------

def build_feed(db: Database, limit: int = 40, max_skips: int = 12) -> list[dict]:
    """One sentence per thing the bot did, newest first.

    Skips vastly outnumber trades -- he scales into a token across many fills
    and we decline all but the first -- so identical skips are collapsed into a
    single counted line and the whole skip section is capped. Otherwise the
    handful of lines that describe actual money gets buried.
    """
    items: list[dict] = []

    for row in db.recent_executions(limit):
        question = (row["question"] or "this bet").strip()
        if row["side"] == "BUY":
            text = (f'Bought {money(-row["net_usd"])} of "{question}" at '
                    f'{cents(row["avg_fill"])}, because the wallet bought it at '
                    f'{cents(row["his_price"])}')
            if row["slippage_vs_his_entry_pct"] is not None:
                text += f' ({row["slippage_vs_his_entry_pct"]:+.0f}% vs his price)'
            kind = "buy"
        elif row["side"] == "SELL":
            pnl = row["realised_pnl_usd"] or 0
            verb = "Made" if pnl >= 0 else "Lost"
            text = (f'Sold it at {cents(row["avg_fill"])} for {money(row["net_usd"])}. '
                    f'{verb} {money(abs(pnl))}. The wallet sold, so we sold.')
            kind = "sell-good" if pnl >= 0 else "sell-bad"
        else:  # SETTLE
            pnl = row["realised_pnl_usd"] or 0
            won = (row["avg_fill"] or 0) >= 1.0
            outcome = "It won" if won else "It lost"
            text = (f'"{question}" finished. {outcome}. '
                    f'{"Made" if pnl >= 0 else "Lost"} {money(abs(pnl))}.')
            kind = "settle-good" if pnl >= 0 else "settle-bad"
        items.append({"ts": row["ts"], "time": clock(row["ts"]), "text": text, "kind": kind})

    # Collapse identical (question, reason) skips into one counted line.
    grouped: dict[tuple[str, str], dict] = {}
    for row in db.recent_skips(400):
        key = ((row["question"] or "a bet").strip(), row["reason"])
        entry = grouped.get(key)
        if entry is None:
            grouped[key] = {"row": row, "count": 1}
        else:
            entry["count"] += 1
            if row["seen_ts"] > entry["row"]["seen_ts"]:
                entry["row"] = row

    skip_items: list[dict] = []
    for (question, _reason), entry in grouped.items():
        row = entry["row"]
        reason = row["reason"]
        if reason == "our_fill_price_above_our_max":
            why = (f'price had already moved to {cents(row["would_be_fill"])} '
                   f'by the time we saw it')
        elif reason == "book_too_thin_to_fill_stake":
            why = "there weren't enough shares for sale to fill $3"
        elif reason == "book_empty":
            why = "nobody was selling it"
        elif reason == "not_enough_cash":
            why = "we didn't have $3 free"
        elif reason == "already_hold_max_copies_for_token":
            why = "we already hold this one"
        elif reason == "his_price_above_max_entry":
            why = f'the wallet paid {cents(row["his_price"])}, above our 50¢ limit'
        elif reason == "trade_older_than_max_age":
            why = "the trade was too old by the time we saw it"
        elif reason == "below_book_min_order_size":
            why = "the order would have been too small to place"
        else:
            why = reason.replace("_", " ")
        times = "" if entry["count"] == 1 else f' (×{entry["count"]})'
        skip_items.append({
            "ts": row["seen_ts"], "time": clock(row["seen_ts"]),
            "text": f'Skipped "{question}" — {why}{times}', "kind": "skip",
        })

    skip_items.sort(key=lambda i: i["ts"], reverse=True)
    items.extend(skip_items[:max_skips])
    items.sort(key=lambda i: i["ts"], reverse=True)
    return items[:limit]


# --- view model ------------------------------------------------------------

def _ratio_bar(mean: float | None, breakeven: float) -> dict:
    """Where our entry price sits between "his price" and "break-even".

    Drawn on a fixed scale from 1.0 (we paid exactly what he paid) to 2.0, with
    the break-even line at its true position. A bar that scales to the data
    would move the break-even line around, which is the one thing on this page
    that must never move.
    """
    top = 2.0
    def pos(v):
        return max(0.0, min(100.0, (v - 1.0) / (top - 1.0) * 100.0))
    return {
        "breakeven_pct": pos(breakeven),
        "value_pct": pos(mean) if mean is not None else None,
        "over": (mean is not None and mean > breakeven),
    }


def _conventions_diverge(fills: dict, tolerance: float = 0.1) -> bool:
    """True when the two `side` readings disagree enough to matter.

    They are not two estimates of one number. If the convention is inverted,
    the configured reading is not conservative -- it is measuring a different
    population -- so a gap between them means neither can be trusted until
    `side-check` settles it.
    """
    a, b = fills.get("fill_rate"), fills.get("alt_fill_rate")
    if a is None or b is None:
        return False
    return abs(a - b) > tolerance


def build_state(cfg: Config, db: Database) -> dict:
    now = now_ts()
    cash = db.cash()

    holdings = []
    positions_value = 0.0
    for row in db.open_positions():
        mark = row["last_mark_price"] if row["last_mark_price"] is not None else row["our_avg_fill"]
        worth = row["shares"] * mark
        positions_value += worth
        paid = row["cost_basis_usd"]
        holdings.append({
            "question": row["question"] or row["token_id"][:20],
            "outcome": row["outcome"],
            "paid": paid,
            "worth": worth,
            "change": worth - paid,
            "bought_at": cents(row["our_avg_fill"]),
            "now_at": cents(mark),
            "held": held_for(now - row["opened_ts"]),
            "band": row["entry_band"],
        })
    # Biggest holdings first: the question a non-trader asks of this table is
    # "where is my money", and that is answered by what each bet is worth now,
    # not by which one moved the most.
    holdings.sort(key=lambda h: h["worth"], reverse=True)

    equity = cash + positions_value
    profit = equity - cfg.starting_capital_usd

    heartbeat = db.last_heartbeat()
    hb_age = (now - heartbeat["ts"]) if heartbeat else None
    if hb_age is None:
        status, status_text = "dead", "Bot has never run"
    elif hb_age < 60:
        status, status_text = "ok", "Bot is running"
    elif hb_age < 300:
        status, status_text = "warn", "Bot might be stuck"
    else:
        status, status_text = "dead", "Bot has stopped"

    series = db.equity_series()
    chart = {
        "labels": [datetime.fromtimestamp(r["ts"], timezone.utc).strftime("%m-%d %H:%M")
                   for r in series],
        "values": [round(r["total_equity_usd"], 2) for r in series],
        "start": cfg.starting_capital_usd,
    }

    winners = expected_vs_actual_winners(db)
    ratio = db.vwap_ratio_stats(breakeven=cfg.vwap_breakeven_ratio)
    fills = db.fill_rate_stats()
    clv = clv_summary(db, cfg.clv_max_spread)
    depth = depth_cost_summary(db, f"${cfg.stake_per_copy_usd:g}")
    slippage = slippage_summary(db)

    return {
        "cfg": cfg,
        "equity": equity, "cash": cash, "positions_value": positions_value,
        "profit": profit, "starting": cfg.starting_capital_usd,
        "status": status, "status_text": status_text,
        "last_checked": ago(hb_age),
        "holdings": holdings,
        "feed": build_feed(db),
        "winners": winners,
        "ratio": ratio,
        "ratio_bar": _ratio_bar(ratio.get("mean"), cfg.vwap_breakeven_ratio),
        "fills": fills,
        "fills_diverge": _conventions_diverge(fills),
        "variants": db.variant_breakdown(breakeven=cfg.vwap_breakeven_ratio),
        "clv": clv,
        "clv_horizons": clv_at_horizons(db, cfg.clv_horizons_minutes),
        "depth": depth,
        "ladder": capacity_curve(db),
        "rules": stopping_rules(db, cfg),
        "unmeasurable": db.unmeasurable_ladder_counts(),
        "slippage": slippage,
        "bands": band_breakdown(db),
        "exit_paths": exit_path_breakdown(db),
        "fees": fee_summary(db),
        "skips": sorted(db.skip_counts().items(), key=lambda kv: -kv[1]),
        "open_count": len(holdings),
        "chart_json": json.dumps(chart),
        "slippage_warn": (
            slippage["mean_vs_his_entry_pct"] is not None
            and slippage["mean_vs_his_entry_pct"] > cfg.slippage_warn_pct
        ),
        "now": datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "money": money, "cents": cents, "pct": lambda v, d="—": f"{v:.1f}%" if v is not None else d,
    }


def create_app(cfg: Config) -> FastAPI:
    app = FastAPI(title="copybot", docs_url=None, redoc_url=None)

    def db() -> Database:
        # One connection per request keeps the reader off the writer's thread.
        return Database(cfg.db_path, cfg.starting_capital_usd)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        conn = db()
        try:
            state = build_state(cfg, conn)
            return TEMPLATES.TemplateResponse(request, "index.html", state)
        finally:
            conn.close()

    @app.get("/healthz")
    def healthz():
        conn = db()
        try:
            heartbeat = conn.last_heartbeat()
            age = (now_ts() - heartbeat["ts"]) if heartbeat else None
            ok, parts = conn.reconcile()
            return {"heartbeat_age_seconds": age, "reconciles": ok, "ledger": parts}
        finally:
            conn.close()

    return app
