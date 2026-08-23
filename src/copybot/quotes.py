"""Settling the tape's `side` convention from recorded quotes.

Whether a print at our limit fills a resting bid depends on which side was the
taker, and the tape's `side` label does not say whose perspective it takes.
Guessing is not conservative: under the inverted reading, filtering to SELL
does not under-count, it measures a disjoint population, and the direction of
the error is unknown. Since fill rate is the number this experiment turns on,
the convention has to be established rather than assumed.

The test is direct. We already record bid and ask for every position on every
marking pass. A print at the prevailing best ask means the taker was a buyer; a
print at the prevailing best bid means the taker was a seller. Tally the labels
across a few hundred such prints and the convention falls out.
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass

from .db import Database
from .polymarket import PolymarketClient, PolymarketError

log = logging.getLogger(__name__)

# A quote older than this cannot be trusted to describe the book a print hit.
MAX_QUOTE_AGE_SECONDS = 120


@dataclass
class Verdict:
    taker_side_evidence: int
    maker_side_evidence: int
    classified: int
    inspected: int

    @property
    def convention(self) -> str:
        total = self.taker_side_evidence + self.maker_side_evidence
        if total < 30:
            return "insufficient"
        share = self.taker_side_evidence / total
        if share >= 0.80:
            return "taker"
        if share <= 0.20:
            return "maker"
        return "ambiguous"

    @property
    def confidence(self) -> float:
        total = self.taker_side_evidence + self.maker_side_evidence
        if not total:
            return 0.0
        return max(self.taker_side_evidence, self.maker_side_evidence) / total

    def summary(self) -> str:
        c = self.convention
        if c == "taker":
            meaning = ("`side` is the TAKER's. SELL prints hit the bid and are "
                       "the ones that fill a resting buy.")
        elif c == "maker":
            meaning = ("`side` is the MAKER's. BUY prints hit the bid, so "
                       "limit_fill_requires_sell_prints should be false.")
        elif c == "ambiguous":
            meaning = "the labels do not separate cleanly; do not rely on either."
        else:
            meaning = ("not enough classified prints yet; keep collecting before "
                       "trusting either fill-rate series.")
        return (f"{self.classified} of {self.inspected} prints classified against a "
                f"live quote\n  taker-side evidence {self.taker_side_evidence}, "
                f"maker-side evidence {self.maker_side_evidence}\n"
                f"  verdict: {c} ({100 * self.confidence:.0f}% agreement) -- {meaning}")


def nearest_quote(db: Database, token_id: str, ts: int):
    """The most recent recorded quote at or before `ts`, if it is fresh enough."""
    return db.conn.execute(
        """SELECT bid, ask, ts FROM marks
           WHERE token_id = ? AND ts <= ? AND bid IS NOT NULL AND ask IS NOT NULL
             AND ts >= ?
           ORDER BY ts DESC LIMIT 1""",
        (token_id, ts, ts - MAX_QUOTE_AGE_SECONDS),
    ).fetchone()


def classify(db: Database, client: PolymarketClient, max_markets: int = 40) -> Verdict:
    """Tally `side` labels against the quote that was live when each print hit."""
    rows = db.conn.execute(
        """SELECT DISTINCT p.condition_id, p.token_id FROM positions p
           JOIN marks m ON m.token_id = p.token_id
           WHERE p.condition_id != '' LIMIT ?""",
        (max_markets,),
    ).fetchall()

    tally: Counter = Counter()
    inspected = 0
    for row in rows:
        try:
            trades = client.get_trades(row["condition_id"], limit=200)
        except PolymarketError as exc:
            log.warning("tape fetch failed for %s: %s", row["condition_id"][:12], exc)
            continue
        for t in trades:
            if t.get("asset") != row["token_id"]:
                continue
            inspected += 1
            try:
                ts = int(t["timestamp"])
                price = float(t["price"])
            except (KeyError, TypeError, ValueError):
                continue
            quote = nearest_quote(db, row["token_id"], ts)
            if quote is None:
                continue
            bid, ask = quote["bid"], quote["ask"]
            if bid is None or ask is None or ask <= bid:
                continue  # crossed or one-sided: tells us nothing
            side = str(t.get("side", "")).upper()
            if price >= ask - 1e-9:
                tally[(side, "at_ask")] += 1
            elif price <= bid + 1e-9:
                tally[(side, "at_bid")] += 1

    # taker-side reading: BUY lifts the ask, SELL hits the bid
    taker = tally[("BUY", "at_ask")] + tally[("SELL", "at_bid")]
    maker = tally[("BUY", "at_bid")] + tally[("SELL", "at_ask")]
    return Verdict(taker_side_evidence=taker, maker_side_evidence=maker,
                   classified=taker + maker, inspected=inspected)
