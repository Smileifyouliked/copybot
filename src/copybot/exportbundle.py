"""One self-contained text bundle for analysis.

The raw log is the wrong thing to hand an analyst. It is mostly repeated SKIP
lines, it grows to megabytes, and the numbers that matter are aggregates the
log never states. This assembles what an analyst actually needs -- the same
metrics the dashboard shows, plus the per-trade tables behind them, plus only
the log lines that carry signal -- into something small enough to paste.

Tab-separated tables so they parse cleanly. Bounded so a month of running still
produces a readable document.
"""
from __future__ import annotations

import re
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .analytics import (band_breakdown, capacity_curve, clv_at_horizons,
                        clv_summary, depth_cost_summary, exit_path_breakdown,
                        expected_vs_actual_winners, fee_summary,
                        slippage_summary, stopping_rules)
from .config import Config
from .db import Database, now_ts
from .textreport import render

MAX_EXECUTIONS = 300
MAX_POSITIONS = 200
MAX_LOG_ISSUES = 80
MAX_LOG_TAIL = 25


def _ts(value) -> str:
    if not value:
        return "-"
    return datetime.fromtimestamp(int(value), timezone.utc).strftime("%Y-%m-%d %H:%M")


def _tsv(rows: list[list], header: list[str]) -> list[str]:
    out = ["\t".join(header)]
    for r in rows:
        out.append("\t".join("" if v is None else str(v) for v in r))
    return out


def _num(value, places: int = 4) -> str:
    if value is None:
        return ""
    try:
        return f"{float(value):.{places}f}"
    except (TypeError, ValueError):
        return str(value)


def build(cfg: Config, db: Database) -> str:
    L: list[str] = []
    now = now_ts()

    L.append("# copybot analysis bundle")
    L.append("")
    L.append("Paper-trading bot copying one Polymarket wallet. Fake money, real prices.")
    L.append("Everything below is generated from the bot's own database and log.")
    L.append("")
    L.append(f"generated_utc\t{datetime.fromtimestamp(now, timezone.utc):%Y-%m-%d %H:%M:%S}")
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                                capture_output=True, text=True, timeout=5,
                                cwd=Path(__file__).resolve().parents[2])
        L.append(f"code_commit\t{commit.stdout.strip() or 'unknown'}")
    except Exception:
        L.append("code_commit\tunknown")

    first = db.conn.execute("SELECT MIN(ts) AS t FROM heartbeat").fetchone()
    if first and first["t"]:
        L.append(f"running_since_utc\t{_ts(first['t'])}")
        L.append(f"running_days\t{(now - first['t']) / 86400:.2f}")
    L.append("")

    # ---- what the numbers mean -------------------------------------------
    L.append("## How to read this")
    L.append("")
    L.append("- The wallet trades cheap long-shots; most bets lose, a few pay ~20x.")
    L.append("- P&L is uninformative until ~700 resolved copies. Before that, judge on")
    L.append("  CLV (did the price move our way after entry) and depth cost (what our")
    L.append("  own order size costs us against the book).")
    L.append("- `depth_cost_pct` is measured on one book snapshot at several sizes, so")
    L.append("  it isolates our size impact from any price drift.")
    L.append("- A skip is data, not a failure. Skip reasons are listed in full below.")
    L.append("")

    # ---- config ----------------------------------------------------------
    L.append("## Config in force")
    L.append("")
    for key in ("mode", "target_wallet", "starting_capital_usd", "stake_per_copy_usd",
                "max_entry_price", "our_max_fill_price", "max_copies_per_token",
                "max_trade_age_seconds", "poll_interval_seconds", "mirror_partial_sells",
                "fee_rate_fallback", "fee_bps_override", "respect_min_order_size",
                "shadow_ladder_usd", "clv_horizons_minutes", "slippage_warn_pct",
                "kill_depth_cost_pct", "kill_depth_min_signals", "kill_clv_min_copies",
                "kill_capture_failure_pct", "pnl_verdict_min_resolved"):
        L.append(f"{key}\t{getattr(cfg, key, '')}")
    L.append("")

    # ---- the dashboard ---------------------------------------------------
    L.append("## Current state (same view as the dashboard)")
    L.append("")
    L.append("```")
    L.append(render(cfg, db))
    L.append("```")
    L.append("")

    # ---- aggregates ------------------------------------------------------
    L.append("## Aggregates")
    L.append("")

    L.append("### Capacity ladder (paired signals, same book snapshot per row)")
    curve = capacity_curve(db)
    L.extend(_tsv(
        [[r["rung"], _num(r["median_usd"], 2), r["n"], _num(r["median_depth_cost_pct"], 2),
          _num(r["p90_depth_cost_pct"], 2), _num(r["mean_levels"], 2),
          _num(100 * r["clear_rate"], 1), _num(100 * r["fill_rate"], 1)]
         for r in curve],
        ["rung", "median_usd", "n_signals", "median_depth_cost_pct",
         "p90_depth_cost_pct", "mean_levels_eaten", "clear_rate_pct", "fill_rate_pct"]))
    L.append("")

    depth = depth_cost_summary(db, f"${cfg.stake_per_copy_usd:g}")
    L.append(f"### Depth cost at our stake (n={depth['n']})")
    if depth["n"]:
        L.append(f"median_pct\t{_num(depth['median'], 2)}")
        L.append(f"mean_pct\t{_num(depth['mean'], 2)}")
        L.append(f"p90_pct\t{_num(depth['p90'], 2)}")
        L.extend(_tsv([[b["label"], b["n"]] for b in depth["buckets"]], ["bucket", "n"]))
    L.append("")

    L.append("### Entry band breakdown")
    L.extend(_tsv(
        [[b["bucket"], b["n"], b["closed"], b["winners"], _num(b["staked"], 2),
          _num(b["pnl"], 2), _num(b["return_pct"], 2), _num(b["slippage_pct"], 2),
          _num(b["clv_pct"], 2)] for b in band_breakdown(db)],
        ["band", "n", "closed", "winners", "staked_usd", "pnl_usd",
         "return_pct", "avg_slippage_pct", "avg_clv_pct"]))
    L.append("")

    L.append("### Exit path breakdown (resolution pays fees once, mirrored sell twice)")
    L.extend(_tsv(
        [[e["bucket"], e["n"], e["closed"], _num(e["staked"], 2), _num(e["pnl"], 2),
          _num(e["return_pct"], 2)] for e in exit_path_breakdown(db)],
        ["exit_path", "n", "closed", "staked_usd", "pnl_usd", "return_pct"]))
    L.append("")

    L.append("### CLV by horizon")
    L.extend(_tsv(
        [[h["horizon_minutes"], h["eligible"], h["measured"],
          _num(h["mean_clv_pct"], 2), _num(h["median_clv_pct"], 2), h["positive"]]
         for h in clv_at_horizons(db, cfg.clv_horizons_minutes)],
        ["horizon_min", "eligible", "measured", "mean_clv_pct",
         "median_clv_pct", "n_positive"]))
    clv = clv_summary(db, cfg.clv_max_spread)
    L.append("")
    L.append(f"closing_line_capture_rate\t{_num(clv['capture_rate'], 3)}")
    L.append(f"closing_line_failures\t{clv['failures']}")
    L.append(f"closing_line_wide_spread\t{clv['wide_spread']}")
    L.append(f"closing_line_mean_age_s\t{_num(clv['mean_age_seconds'], 0)}")
    L.append("")

    winners = expected_vs_actual_winners(db)
    L.append("### Expected vs actual winners (null: entry price is the true probability)")
    for k in ("resolved", "expected", "actual", "sd", "z", "verdict"):
        L.append(f"{k}\t{winners.get(k) if not isinstance(winners.get(k), float) else _num(winners[k], 3)}")
    L.append("")

    slip = slippage_summary(db)
    L.append("### Slippage and size")
    for k, v in slip.items():
        L.append(f"{k}\t{_num(v, 3) if isinstance(v, float) else v}")
    L.append("")

    L.append("### Fees")
    for k, v in fee_summary(db).items():
        L.append(f"{k}\t{_num(v, 4) if isinstance(v, float) else v}")
    L.append("")

    L.append("### Skip reasons (every skip, full counts)")
    L.extend(_tsv(sorted(db.skip_counts().items(), key=lambda kv: -kv[1]),
                  ["reason", "n"]))
    L.append("")

    unmeasurable = db.unmeasurable_ladder_counts()
    if unmeasurable:
        L.append("### Ladder rows that could not be measured")
        L.extend(_tsv(sorted(unmeasurable.items(), key=lambda kv: -kv[1]), ["reason", "n"]))
        L.append("")

    # ---- per-position ----------------------------------------------------
    L.append("## Positions")
    L.append("")
    rows = db.conn.execute(
        f"""SELECT * FROM positions ORDER BY opened_ts DESC LIMIT {MAX_POSITIONS}"""
    ).fetchall()
    L.append(f"(most recent {len(rows)})")
    L.extend(_tsv(
        [[p["id"], p["status"], _ts(p["opened_ts"]), _ts(p["closed_ts"]),
          (p["question"] or "")[:70].replace("\t", " "), p["outcome"], p["entry_band"],
          _num(p["our_avg_fill"]), _num(p["his_first_price"]), _num(p["his_vwap_entry"]),
          _num(p["shares_opened"], 2), _num(p["cost_basis_opened"], 4),
          _num(p["slippage_vs_his_entry_pct"], 2), _num(p["slippage_vs_his_vwap_pct"], 2),
          _num(p["size_ratio"], 2), _num(p["size_ratio_vs_total"], 2),
          _num(p["closing_line_price"]), _num(p["clv_pct"], 2),
          p["closing_line_captured"], p["exit_path"],
          _num(p["realised_pnl_usd"], 4), _num(p["last_mark_price"])]
         for p in rows],
        ["id", "status", "opened", "closed", "question", "outcome", "band",
         "our_fill", "his_first", "his_vwap", "shares", "cost_basis",
         "slip_vs_his_pct", "slip_vs_vwap_pct", "size_ratio_first",
         "size_ratio_total", "closing_line", "clv_pct", "clv_captured",
         "exit_path", "realised_pnl", "last_mark"]))
    L.append("")

    # ---- executions ------------------------------------------------------
    L.append("## Executions")
    L.append("")
    execs = db.recent_executions(MAX_EXECUTIONS)
    L.append(f"(most recent {len(execs)})")
    L.extend(_tsv(
        [[e["id"], _ts(e["ts"]), e["side"], (e["question"] or "")[:60].replace("\t", " "),
          _num(e["shares"], 2), _num(e["avg_fill"]), _num(e["his_price"]),
          _num(e["gross_usd"], 4), _num(e["fee_usd"], 4), _num(e["net_usd"], 4),
          e["levels_consumed"], e["latency_seconds"],
          _num(e["slippage_vs_his_entry_pct"], 2), e["entry_band"],
          e["fee_rate_was_fallback"], _num(e["realised_pnl_usd"], 4)]
         for e in execs],
        ["id", "ts", "side", "question", "shares", "our_fill", "his_price",
         "gross", "fee", "net", "levels", "latency_s", "slip_pct", "band",
         "fee_fallback", "realised_pnl"]))
    L.append("")

    # ---- log -------------------------------------------------------------
    L.append("## Log")
    L.append("")
    log_path = Path(cfg.log_path)
    if not log_path.exists():
        L.append("(no log file found)")
    else:
        levels: Counter = Counter()
        issues: list[str] = []
        tail: list[str] = []
        pattern = re.compile(r"\b(DEBUG|INFO|WARNING|ERROR|CRITICAL)\b")
        with log_path.open(errors="replace") as fh:
            for line in fh:
                m = pattern.search(line)
                level = m.group(1) if m else "OTHER"
                levels[level] += 1
                if level in ("WARNING", "ERROR", "CRITICAL"):
                    issues.append(line.rstrip())
                tail.append(line.rstrip())
                if len(tail) > MAX_LOG_TAIL:
                    tail.pop(0)

        L.append(f"log_file\t{log_path}")
        L.append(f"log_lines\t{sum(levels.values())}")
        L.extend(_tsv(sorted(levels.items(), key=lambda kv: -kv[1]), ["level", "n"]))
        L.append("")

        L.append(f"### Warnings and errors ({len(issues)} total)")
        if not issues:
            L.append("(none — this is what you want)")
        else:
            # Collapse repeats: the same warning 400 times is one fact.
            grouped = Counter(re.sub(r"^\S+ \S+ \S+ ", "", i)[:160] for i in issues)
            L.extend(_tsv(
                [[n, msg] for msg, n in grouped.most_common(MAX_LOG_ISSUES)],
                ["count", "message"]))
        L.append("")

        L.append(f"### Last {len(tail)} log lines")
        L.append("```")
        L.extend(tail)
        L.append("```")
        L.append("")

    restarts = Path("logs/restarts.log")
    if restarts.exists():
        lines = restarts.read_text(errors="replace").splitlines()
        L.append(f"### Crash restarts ({len(lines)})")
        L.append("```")
        L.extend(lines[-20:])
        L.append("```")
        L.append("")

    # ---- questions -------------------------------------------------------
    rules = stopping_rules(db, cfg)
    L.append("## What I want to know")
    L.append("")
    L.append("1. Is the depth cost at our stake size acceptable, or is our order")
    L.append("   size the binding constraint rather than the wallet's selection?")
    L.append("2. Is CLV positive, and is the closing-line capture rate high enough")
    L.append("   to trust it?")
    L.append("3. Are the skip reasons telling me the strategy is unreachable?")
    L.append("4. Anything in the warnings that suggests the numbers are wrong")
    L.append("   rather than merely disappointing?")
    L.append("")
    L.append(f"kill_conditions_breached\t{rules['breaches']}")
    if rules["breaches"]:
        L.append(f"breached\t{'; '.join(rules['breach_names'])}")
    L.append("")

    text = "\n".join(L)
    approx = len(text) // 4
    return text + f"\n<!-- bundle: {len(text):,} chars, ~{approx:,} tokens -->\n"
