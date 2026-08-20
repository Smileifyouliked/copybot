"""Derived metrics for the dashboard.

The headline P&L number on this strategy is close to uninformative for months.
At a 5c average entry the break-even win rate is ~5.2%, so 60 resolved copies
produce ~3 expected winners and a real edge would produce ~4 -- indistinguishable
from noise. Separating skill from luck on P&L alone needs roughly 700 resolved
copies. Everything here exists to say something useful before then.

Two things carry signal early:

  * CLV, because it is continuous per trade rather than a 0/1 outcome.
  * Expected vs actual winners with a proper interval, because it makes
    "unlucky" and "broken" visually distinguishable.
"""
from __future__ import annotations

import math

from .db import Database


def expected_vs_actual_winners(db: Database) -> dict:
    """Under the null that the market was fairly priced at our entry, each copy
    wins with probability equal to its entry price.

        expected = Σ pᵢ          variance = Σ pᵢ(1 − pᵢ)

    Independent Bernoulli trials with different p, so the variance is the sum
    of per-trade variances. The interval this produces is the whole point: at
    60 copies averaging 5c it is 3 ± 1.7, which is why a red P&L number on day
    12 says nothing at all.
    """
    rows = db.conn.execute(
        """SELECT our_avg_fill, proceeds_usd FROM positions
           WHERE status='closed' AND exit_path='resolution'"""
    ).fetchall()
    if not rows:
        return {"resolved": 0, "expected": 0.0, "actual": 0, "sd": 0.0,
                "z": None, "verdict": "no resolved bets yet"}

    expected = sum(r["our_avg_fill"] for r in rows)
    variance = sum(r["our_avg_fill"] * (1 - r["our_avg_fill"]) for r in rows)
    sd = math.sqrt(variance)
    actual = sum(1 for r in rows if (r["proceeds_usd"] or 0) > 0)
    z = ((actual - expected) / sd) if sd > 0 else None

    if z is None:
        verdict = "not enough spread to judge"
    elif abs(z) < 1:
        verdict = "normal range — too early to tell"
    elif z >= 2:
        verdict = "running better than chance"
    elif z <= -2:
        verdict = "running worse than chance"
    elif z > 0:
        verdict = "slightly ahead, still within noise"
    else:
        verdict = "slightly behind, still within noise"

    return {"resolved": len(rows), "expected": expected, "actual": actual,
            "sd": sd, "z": z, "verdict": verdict,
            "low": max(0.0, expected - 2 * sd), "high": expected + 2 * sd}


def clv_summary(db: Database, max_spread: float) -> dict:
    """Closing line value, plus the health of its capture.

    If capture is failing often the primary metric is broken, so the failure
    rate is reported next to the number rather than buried.
    """
    rows = db.conn.execute(
        "SELECT clv_pct, clv_abs, closing_line_spread, closing_line_captured, "
        "closing_line_age_seconds FROM positions WHERE status='closed'"
    ).fetchall()
    if not rows:
        return {"closed": 0, "captured": 0, "capture_rate": None, "mean_clv_pct": None,
                "clean": 0, "wide_spread": 0, "mean_age_seconds": None, "failures": 0}

    captured = [r for r in rows if r["closing_line_captured"] and r["clv_pct"] is not None]
    clean = [r for r in captured
             if r["closing_line_spread"] is not None and r["closing_line_spread"] <= max_spread]
    wide = len(captured) - len(clean)
    ages = [r["closing_line_age_seconds"] for r in captured
            if r["closing_line_age_seconds"] is not None]

    def mean(values):
        return (sum(values) / len(values)) if values else None

    return {
        "closed": len(rows),
        "captured": len(captured),
        "failures": len(rows) - len(captured),
        "capture_rate": len(captured) / len(rows),
        "mean_clv_pct": mean([r["clv_pct"] for r in captured]),
        "mean_clv_pct_clean": mean([r["clv_pct"] for r in clean]),
        "clean": len(clean),
        "wide_spread": wide,
        "mean_age_seconds": mean(ages),
        "max_age_seconds": max(ages) if ages else None,
    }


def _group_breakdown(db: Database, column: str) -> list[dict]:
    rows = db.conn.execute(
        f"""SELECT {column} AS bucket,
                   COUNT(*) AS n,
                   SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END) AS closed,
                   COALESCE(SUM(realised_pnl_usd), 0) AS pnl,
                   COALESCE(SUM(cost_basis_opened), 0) AS staked,
                   AVG(slippage_vs_his_entry_pct) AS slip_pct,
                   AVG(clv_pct) AS clv_pct,
                   SUM(CASE WHEN status='closed' AND proceeds_usd > 0 THEN 1 ELSE 0 END) AS winners
            FROM positions WHERE {column} IS NOT NULL
            GROUP BY {column} ORDER BY {column}"""
    ).fetchall()
    out = []
    for r in rows:
        staked = r["staked"] or 0
        out.append({
            "bucket": r["bucket"], "n": r["n"], "closed": r["closed"],
            "pnl": r["pnl"], "staked": staked,
            "return_pct": (r["pnl"] / staked * 100) if staked else None,
            "slippage_pct": r["slip_pct"], "clv_pct": r["clv_pct"],
            "winners": r["winners"],
            "win_rate": (r["winners"] / r["closed"]) if r["closed"] else None,
        })
    return out


def band_breakdown(db: Database) -> list[dict]:
    return _group_breakdown(db, "entry_band")


def exit_path_breakdown(db: Database) -> list[dict]:
    """Held-to-resolution pays the fee once; a mirrored sell pays it twice. At
    5c entries that is ~4.75% versus ~9.5%, so these are different products and
    are reported separately."""
    return _group_breakdown(db, "exit_path")


def slippage_summary(db: Database) -> dict:
    """Percent, not cents. At a 2c entry one cent of slippage is a 50% worse
    entry, so a cent-denominated threshold means nothing here."""
    row = db.conn.execute(
        """SELECT COUNT(*) AS n,
                  AVG(slippage_vs_his_entry_pct) AS vs_entry,
                  AVG(slippage_vs_his_vwap_pct) AS vs_vwap,
                  AVG(size_ratio) AS size_ratio,
                  AVG(size_ratio_vs_total) AS size_ratio_total
           FROM positions"""
    ).fetchone()
    worse = db.conn.execute(
        "SELECT COUNT(*) AS n FROM positions WHERE slippage_vs_his_entry_pct > ?",
        (15.0,),
    ).fetchone()
    return {
        "n": row["n"], "mean_vs_his_entry_pct": row["vs_entry"],
        "mean_vs_his_vwap_pct": row["vs_vwap"],
        "mean_size_ratio_vs_first_fill": row["size_ratio"],
        "mean_size_ratio_vs_his_position": row["size_ratio_total"],
        "n_worse_than_15pct": worse["n"] if worse else 0,
    }


def fee_summary(db: Database) -> dict:
    row = db.conn.execute(
        """SELECT COALESCE(SUM(fee_usd), 0) AS fees,
                  COALESCE(SUM(ABS(gross_usd)), 0) AS gross,
                  SUM(fee_rate_was_fallback) AS fallbacks,
                  COUNT(*) AS n
           FROM copied_trades WHERE side IN ('BUY','SELL')"""
    ).fetchone()
    gross = row["gross"] or 0
    return {"total_fees_usd": row["fees"], "gross_usd": gross,
            "fee_pct_of_gross": (row["fees"] / gross * 100) if gross else None,
            "fee_rate_fallbacks": row["fallbacks"] or 0, "n_trades": row["n"]}
