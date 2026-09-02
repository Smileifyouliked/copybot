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
MAX_RESTING = 120


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

    # ---- entry quality: the run-2 experiment -----------------------------
    # This section is the point of the whole run. Market orders paid 11-197%
    # worse than the wallet did against a break-even of our VWAP / his VWAP =
    # 1.395, so entries now rest at his exact price and the question became
    # "how often did we get in" rather than "how much worse did we pay".
    L.append("## Entry quality (the experiment)")
    L.append("")

    L.extend(_tsv(
        [[r["run_id"], r["entry_mode"], _ts(r["started_ts"]), r["git_commit"] or "-"]
         for r in db.runs()],
        ["run_id", "entry_mode", "started", "git_commit"]))
    L.append("")

    ratio = db.vwap_ratio_stats(breakeven=cfg.vwap_breakeven_ratio)
    L.append("### our VWAP / his VWAP  (break-even "
             f"{cfg.vwap_breakeven_ratio:.3f} -- above it his edge is spent on our entry)")
    L.append(f"n_positions\t{ratio['n']}")
    if ratio["n"]:
        L.append(f"mean\t{_num(ratio['mean'], 4)}")
        L.append(f"median\t{_num(ratio['median'], 4)}")
        L.append(f"p90\t{_num(ratio['p90'], 4)}")
        L.append(f"best\t{_num(ratio['best'], 4)}")
        L.append(f"worst\t{_num(ratio['worst'], 4)}")
        L.append(f"over_breakeven\t{ratio['over_breakeven']} of {ratio['n']}")
        L.append(f"verdict\t{'OVER THE LINE' if ratio['mean'] > cfg.vwap_breakeven_ratio else 'inside the line'}")
    L.append("")

    fills = db.fill_rate_stats()
    L.append("### Resting order fill rate")
    L.append("Both `side` conventions are reported. They are NOT two estimates of")
    L.append("one number: if the convention is inverted, the configured reading is")
    L.append("measuring a different population, and the direction of the error is")
    L.append("unknown. If they diverge, neither is usable until side-check settles it.")
    L.append(f"orders_finished\t{fills['orders']}")
    if fills["orders"]:
        L.append(f"any_fill\t{fills['any_fill']}")
        L.append(f"full_fill\t{fills['full_fill']}")
        L.append(f"fill_rate_pct\t{_num(100 * fills['fill_rate'], 1)}")
        L.append(f"full_fill_rate_pct\t{_num(100 * fills['full_fill_rate'], 1)}")
        L.append(f"share_fill_rate_pct\t{_num(100 * (fills['share_fill_rate'] or 0), 1)}")
        L.append(f"alt_fill_rate_pct\t{_num(100 * (fills['alt_fill_rate'] or 0), 1)}")
        L.append(f"alt_share_fill_rate_pct\t{_num(100 * (fills['alt_share_fill_rate'] or 0), 1)}")
        if fills["fill_rate"] is not None and fills["alt_fill_rate"] is not None:
            gap = abs(fills["fill_rate"] - fills["alt_fill_rate"])
            L.append(f"conventions_diverge\t{'YES' if gap > 0.1 else 'no'} "
                     f"(gap {_num(100 * gap, 1)} points)")
    L.append("")

    variants = db.variant_breakdown(breakeven=cfg.vwap_breakeven_ratio)
    if variants:
        L.append("### By stake variant")
        L.append("Compare WITHIN a price band, not pooled: the exchange minimum is a")
        L.append("share count, so a $1 order is unplaceable above 20c and the arms")
        L.append("therefore cover different price populations. That is physical.")
        L.extend(_tsv(
            [[_num(v["stake"], 2), v["orders"],
              _num(100 * (v["fill_rate"] or 0), 1),
              _num(100 * (v["share_fill_rate"] or 0), 1),
              v["positions"], _num(v["mean_ratio"], 4), v["over_breakeven"]]
             for v in variants],
            ["stake_usd", "orders", "fill_rate_pct", "share_fill_rate_pct",
             "positions", "mean_vwap_ratio", "over_breakeven"]))
        L.append("")

    resting = db.conn.execute(
        """SELECT trade_key, question, limit_price, usd_budget, target_shares,
                  queue_ahead_shares, filled_shares, alt_filled_shares,
                  consumed_shares, prints_observed, status, placed_ts, expires_ts
           FROM resting_orders ORDER BY placed_ts DESC LIMIT ?""",
        (MAX_RESTING,)).fetchall()
    L.append(f"### Resting orders (most recent {len(resting)})")
    L.extend(_tsv(
        [[_ts(r["placed_ts"]), r["status"], _num(r["limit_price"], 4),
          _num(r["usd_budget"], 2), _num(r["target_shares"], 2),
          _num(r["filled_shares"], 2), _num(r["alt_filled_shares"], 2),
          _num(r["queue_ahead_shares"], 0), _num(r["consumed_shares"], 0),
          r["prints_observed"], (r["question"] or "")[:44]]
         for r in resting],
        ["placed", "status", "limit_price", "usd_budget", "target_shares",
         "filled_shares", "alt_filled_shares", "queue_ahead", "tape_volume",
         "prints", "question"]))
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

    restarts = Path(cfg.log_path).parent / "restarts.log"
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
    L.append("1. FILL RATE. Of his buys we tried to match, what fraction filled at")
    L.append("   his price? If that is near zero, resting at his price does not get")
    L.append("   us in, and the answer is 'his edge is unreachable' -- not 'try")
    L.append("   harder'. Do the two `side` conventions agree? If not, nothing")
    L.append("   below this line can be trusted yet.")
    L.append(f"2. our VWAP / his VWAP against {cfg.vwap_breakeven_ratio:.3f}. Is it under the line,")
    L.append("   and is it under the line on the MEAN as well as the median -- or is")
    L.append("   an average being held up by a tail of terrible entries?")
    L.append("3. Is CLV positive, and is the closing-line capture rate high enough")
    L.append("   to trust it?")
    L.append("4. Are the skip reasons telling me the strategy is unreachable, or")
    L.append("   merely that the budget is small?")
    L.append("5. Anything in the warnings that suggests the numbers are WRONG")
    L.append("   rather than merely disappointing?")
    L.append("")
    L.append(f"kill_conditions_breached\t{rules['breaches']}")
    if rules["breaches"]:
        L.append(f"breached\t{'; '.join(rules['breach_names'])}")
    L.append("")

    text = "\n".join(L)
    approx = len(text) // 4
    return text + f"\n<!-- bundle: {len(text):,} chars, ~{approx:,} tokens -->\n"
