"""The dashboard, as plain text in a terminal.

Reaching the web dashboard needs an SSH tunnel, which needs an SSH client and
the key file on your own machine. If you only ever work on the server itself --
through the AWS browser console, for instance -- that path is closed, and the
experiment would be collecting data nobody can read.

Same numbers, same ordering, no browser.
"""
from __future__ import annotations

import textwrap
from datetime import datetime, timezone

from .analytics import (capacity_curve, clv_at_horizons, clv_summary,
                        depth_cost_summary, exit_path_breakdown,
                        expected_vs_actual_winners, slippage_summary,
                        stopping_rules)
from .config import Config
from .dashboard import ago, build_feed, cents, held_for, money
from .db import Database, now_ts

WIDTH = 74


def rule(title: str = "") -> str:
    if not title:
        return "─" * WIDTH
    return f"── {title} " + "─" * max(0, WIDTH - len(title) - 4)


def render(cfg: Config, db: Database) -> str:
    out: list[str] = []
    now = now_ts()
    cash = db.cash()

    positions = db.open_positions()
    value = sum(
        p["shares"] * (p["last_mark_price"] if p["last_mark_price"] is not None
                       else p["our_avg_fill"])
        for p in positions
    )
    equity = cash + value
    profit = equity - cfg.starting_capital_usd

    # ---- headline --------------------------------------------------------
    out.append("")
    out.append(f"  YOUR MONEY RIGHT NOW      {money(equity)}")
    direction = "Up" if profit >= 0 else "Down"
    out.append(f"  {direction} {money(abs(profit))} since you started "
               f"(began with {money(cfg.starting_capital_usd)})")
    out.append(f"  {money(cash)} cash · {money(value)} in {len(positions)} open bet(s)")
    out.append("")

    winners = expected_vs_actual_winners(db)
    if winners["resolved"] == 0:
        out.append("  No bets have finished yet, so there is nothing to judge.")
    else:
        out.append(f"  {winners['actual']} winner(s) from {winners['resolved']} finished bets. "
                   f"Luck alone gives ~{winners['expected']:.1f} "
                   f"(normal range {winners['low']:.0f}–{winners['high']:.0f}).")
        out.append(f"  -> {winners['verdict']}")
    out.append("")

    # ---- entry quality: the whole experiment ------------------------------
    ratio = db.vwap_ratio_stats(breakeven=cfg.vwap_breakeven_ratio)
    fills = db.fill_rate_stats()
    out.append(rule("WHAT WE PAY COMPARED TO HIM"))
    out.append(f"  break-even is {cfg.vwap_breakeven_ratio:.3f}x his price. Above that, his"
               f" edge is spent")
    out.append("  entirely on our entry and there is nothing left for us.")
    if ratio["n"] == 0:
        out.append("  No bets bought yet, so there is nothing to compare.")
    else:
        verdict = ("OVER THE LINE — at these prices he can keep picking winners "
                   "and we still lose"
                   if ratio["mean"] > cfg.vwap_breakeven_ratio
                   else "inside the line — his edge still has room after our entry")
        out.append(f"  we pay on average {ratio['mean']:.3f}x   typical bet "
                   f"{ratio['median']:.3f}x   best {ratio['best']:.3f}x   "
                   f"worst {ratio['worst']:.3f}x")
        out.append(f"  {ratio['over_breakeven']} of {ratio['n']} bets are above the line "
                   f"({100 * (ratio['over_breakeven_rate'] or 0):.0f}%)")
        out.append(f"  -> {verdict}")
    out.append("")

    out.append(rule("ORDERS THAT ACTUALLY GOT FILLED"))
    out.append("  We rest at his exact price and never chase. So the question is")
    out.append("  not how much worse we paid, it is how often we got in at all.")
    if fills["orders"] == 0:
        out.append("  No orders have finished waiting yet.")
    else:
        out.append(f"  {fills['orders']} order(s) finished   "
                   f"got some: {100 * fills['fill_rate']:.0f}%   "
                   f"filled fully: {100 * fills['full_fill_rate']:.0f}%")
        if fills["share_fill_rate"] is not None:
            out.append(f"  of the shares we wanted, we got "
                       f"{100 * fills['share_fill_rate']:.0f}%")
        alt = fills["alt_fill_rate"]
        if alt is not None:
            gap = abs(alt - fills["fill_rate"])
            out.append(f"  read the other way round: {100 * alt:.0f}%"
                       + ("   <- THESE DISAGREE. One of them is counting the wrong"
                          " trades; run side-check." if gap > 0.1
                          else "   (the two readings agree)"))

    paths = db.path_breakdown(breakeven=cfg.vwap_breakeven_ratio)
    if paths:
        out.append("  by HOW we got in — resting fills land at his price by")
        out.append("  construction, so pooling them with fills the market handed")
        out.append("  us after it moved makes one number about the mix, not about")
        out.append("  execution:")
        out.append("  how               bets   we pay   over line   staked   P&L")
        label = {"rested": "waited at his px", "crossed_inside": "market came to us",
                 "market": "crossed spread", "unrecorded": "(before this was logged)"}
        for pth in paths:
            out.append(
                f"  {label.get(pth['path'], pth['path']):<18}{pth['n']:>4}   "
                f"{pth['mean_ratio']:.3f}x   {pth['over_breakeven']:>4}/{pth['n']:<4}  "
                f"{money(pth['staked_usd']):>7}  {money(pth['pnl_usd']):>7}")
        out.append("")

    variants = db.variant_breakdown(breakeven=cfg.vwap_breakeven_ratio)
    if len(variants) > 1:
        out.append("  by bet size — a resting order is held up by the queue in front")
        out.append("  of it, not by how deep the book is, so a smaller bet can get in")
        out.append("  more often. Compare these within a price band, not pooled:")
        out.append("  size    orders   got some   of shares    bets   we pay")
        for v in variants:
            out.append(
                f"  ${v['stake']:<5.2f} {v['orders']:>6}   "
                + (f"{100 * v['fill_rate']:>7.0f}%" if v["fill_rate"] is not None else "      —")
                + (f"   {100 * v['share_fill_rate']:>8.0f}%" if v["share_fill_rate"] is not None
                   else "          —")
                + f"   {v['positions']:>5}   "
                + (f"{v['mean_ratio']:.3f}x" if v["mean_ratio"] is not None else "  —")
            )
    out.append("")

    # ---- health ----------------------------------------------------------
    heartbeat = db.last_heartbeat()
    age = (now - heartbeat["ts"]) if heartbeat else None
    if age is None:
        light = "[STOPPED] bot has never run"
    elif age < 60:
        light = "[RUNNING] bot is alive"
    elif age < 300:
        light = "[STALLING] bot might be stuck"
    else:
        light = "[STOPPED] bot has stopped"
    ok, parts = db.reconcile()
    out.append(rule("HEALTH"))
    out.append(f"  {light} — last checked {ago(age)}")
    out.append(f"  ledger reconciles: {'yes' if ok else 'NO — ' + str(parts)}")
    out.append(f"  his trades seen: {len(db.processed_keys())}")
    out.append("")

    # ---- depth cost ------------------------------------------------------
    stake_label = f"${cfg.stake_per_copy_usd:g}"
    depth = depth_cost_summary(db, stake_label)
    out.append(rule("WHAT OUR OWN ORDER SIZE COSTS US"))
    if depth["n"] == 0:
        out.append("  No signals measured yet.")
    else:
        out.append(f"  typical extra cost {depth['median']:+.1f}%   "
                   f"worst 10% {depth['p90']:+.1f}%   "
                   f"free {depth['free']}/{depth['n']}   "
                   f"over 15% {depth['over_15pct']}/{depth['n']}")
        for b in depth["buckets"]:
            bar = "█" * int(round(30 * b["n"] / depth["n"]))
            out.append(f"    {b['label']:<10} {bar:<30} {b['n']}")
    out.append("")

    curve = capacity_curve(db)
    horizons = clv_at_horizons(db, cfg.clv_horizons_minutes)
    out.append(rule("WHAT WOULD HAPPEN AT OTHER BET SIZES"))
    if not curve:
        out.append("  No paired signals yet.")
    else:
        out.append(f"  {'size':>13} {'typical':>9} {'signals':>8} "
                   f"{'extra cost':>11} {'levels':>7} {'clears':>7}")
        for r in curve:
            levels = f"{r['mean_levels']:.1f}" if r["mean_levels"] is not None else "—"
            cost = (f"{r['median_depth_cost_pct']:+.1f}%"
                    if r["median_depth_cost_pct"] is not None else "—")
            out.append(f"  {r['rung']:>13} {money(r['median_usd']):>9} {r['n']:>8} "
                       f"{cost:>11} {levels:>7} {100 * r['clear_rate']:>6.0f}%")
        out.append(f"  (all rows measured on the same {curve[0]['paired_signals']} signal(s))")
    out.append("")

    # ---- CLV -------------------------------------------------------------
    clv = clv_summary(db, cfg.clv_max_spread)
    out.append(rule("DID THE PRICE MOVE OUR WAY AFTER WE BOUGHT"))
    cells = []
    if clv["mean_clv_pct"] is not None:
        cells.append(f"at the finish {clv['mean_clv_pct']:+.1f}%")
    for h in horizons:
        if h["mean_clv_pct"] is not None:
            cells.append(f"+{h['horizon_minutes']}min {h['mean_clv_pct']:+.1f}% "
                         f"(n={h['measured']})")
    out.append("  " + ("   ".join(cells) if cells else "Nothing measured yet."))
    if clv["closed"]:
        out.append(f"  final price captured for {clv['captured']}/{clv['closed']} "
                   f"finished bets ({100 * clv['capture_rate']:.0f}%)"
                   + (f" — {clv['failures']} MISSED" if clv["failures"] else ""))
    out.append("")

    # ---- stopping rule ---------------------------------------------------
    # Same three inputs the sections above already computed; `stopping_rules`
    # recomputes them otherwise.
    rules = stopping_rules(db, cfg, curve=curve, horizons=horizons, capture=clv)
    out.append(rule("YOUR STOPPING RULE"))
    if rules["breaches"]:
        out.append(f"  *** {rules['breaches']} KILL CONDITION BREACHED: "
                   f"{'; '.join(rules['breach_names'])} ***")
    for r in rules["kill"]:
        mark = {"breach": "BREACHED", "ok": "clear", "waiting": "collecting"}[r["status"]]
        out.append(f"  {r['name']:<34} {r['value']:>9}  "
                   f"kills if {r['threshold']:<12} [{mark}]")
        out.append(f"  {'':<34} {r['progress']}")
    pnl = rules["pnl_verdict"]
    out.append(f"  P&L means nothing until {pnl['required']} finished bets: "
               f"{pnl['resolved']} so far ({pnl['progress_pct']:.0f}%)")
    days = rules["go_live"]["stable_days"]
    out.append(f"  days with no crash or stall: "
               f"{days:.1f}/{rules['go_live']['required_days']}"
               if days is not None else "  days with no crash or stall: —")
    out.append("")

    # ---- positions -------------------------------------------------------
    out.append(rule("BETS YOU ARE HOLDING"))
    if not positions:
        out.append("  Nothing open right now.")
    else:
        for p in sorted(positions, key=lambda r: r["opened_ts"], reverse=True):
            mark = p["last_mark_price"] if p["last_mark_price"] is not None else p["our_avg_fill"]
            worth = p["shares"] * mark
            # paid = what went in (cost_basis_opened, which only grows).
            # cost_basis_usd is the basis of the shares still held, so a
            # mirrored sell shrinks it -- printing that as "paid" made a $3.00
            # position read as "paid $0.63" once he sold most of it back out.
            still_held = p["cost_basis_usd"]
            paid = p["cost_basis_opened"] or still_held
            part_sold = p["shares_opened"] and p["shares"] < p["shares_opened"] - 1e-9
            change = worth - still_held
            sign = "+" if change >= 0 else "-"
            out.append(f"  {(p['question'] or p['token_id'])[:66]}")
            out.append(f"    paid {money(paid)} · now {money(worth)} · "
                       f"{sign}{money(abs(change))} · bought {cents(p['our_avg_fill'])} "
                       f"now {cents(mark)} · held {held_for(now - p['opened_ts'])}"
                       + (f" · part already sold, {money(still_held)} still in"
                          if part_sold else ""))
    out.append("")

    # ---- feed ------------------------------------------------------------
    out.append(rule("WHAT THE BOT DID RECENTLY"))
    feed = build_feed(db, limit=15, max_skips=6)
    if not feed:
        out.append("  Nothing yet.")
    for item in feed:
        lines = textwrap.wrap(item["text"], width=WIDTH - 12) or [""]
        out.append(f"  {item['time']:>8}  {lines[0]}")
        for cont in lines[1:]:
            out.append(f"  {'':>8}  {cont}")
    out.append("")

    slip = slippage_summary(db)
    if slip["n"]:
        out.append(rule("SLIPPAGE"))
        out.append(f"  vs his fill {slip['mean_vs_his_entry_pct']:+.1f}%   "
                   f"vs his average entry "
                   f"{(slip['mean_vs_his_vwap_pct'] or 0):+.1f}%   "
                   f"worse than 15%: {slip['n_worse_than_15pct']}/{slip['n']}")
        for e in exit_path_breakdown(db):
            label = "held to the end" if e["bucket"] == "resolution" else "sold when he sold"
            out.append(f"  {label:<20} {e['n']:>3} bets  P&L {money(e['pnl'])}")
        out.append("")

    out.append(rule())
    out.append(f"  {datetime.fromtimestamp(now, timezone.utc):%Y-%m-%d %H:%M:%S} UTC")
    out.append("")
    return "\n".join(out)
