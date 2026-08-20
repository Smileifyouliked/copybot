# NOTES — assumptions, verified facts, and open questions

Every claim below is tagged **VERIFIED** (hit the live API and observed it, with
the date) or **UNVERIFIED** (taken from docs or reasoning, not measured).

Last verified: 2026-08-20.

---

## 1. Target wallet

**VERIFIED** `0x6297b93ea37ff92a57fd636410f3b71ebf74517e` returns trade activity.
The address *is* the proxy wallet — it comes back in the `proxyWallet` field of
every row — so there is no EOA/proxy indirection to resolve.

**VERIFIED** 500 trades over 8 days (2026-08-12 → 2026-08-20): 452 BUY, 48 SELL,
84 distinct markets, 86 distinct tokens. Almost entirely daily weather markets.

Consequences for this bot, all **VERIFIED** from that sample:

| Observation | Value | Why it matters |
|---|---|---|
| Copyable buys (< $0.50) | 444 of 452 (98%) | The price filter barely filters |
| Distinct tokens / day | ~10 | ≈ $31/day deployed at $3 a copy |
| His median *first* buy per token | $1.59 | Our $3 is ~2× his opening size |
| His fills per token | ~5 (he scales in) | We take only his first → VWAP gap |
| Sells | 48 / 500 (~10%) | ~90% of positions ride to resolution |
| Entry band spread | 70% in 0-10c | The band breakdown is near single-bucket |

**Correction on record:** an earlier figure of "$0.26 median, so $3 is 10–15× his
size" was the median across *all* fills, which double-counts his scaling-in. The
figure that matters is the median first fill per token, $1.59.

---

## 2. Endpoints

### `data-api.polymarket.com/activity`
**VERIFIED** `?user=<addr>&type=TRADE&limit=N&offset=M` returns a newest-first
JSON array. `offset` paginates and returns disjoint pages.

Field mapping used (**VERIFIED** against 200 live rows, 0 parse failures):

| Our field | Their field | Note |
|---|---|---|
| `token_id` | `asset` | The thing we copy verbatim. Never inferred from outcome name |
| `condition_id` | `conditionId` | Market key for gamma lookups |
| `shares` | `size` | Shares, not dollars |
| `usd_size` | `usdcSize` | Dollars |
| `price` | `price` | Float, 0–1 |
| `traded_ts` | `timestamp` | Unix **seconds** (not ms — the CLOB uses ms, these differ) |

**VERIFIED** `transactionHash` is **not** a unique trade id: 4 collisions in 500
rows, one transaction carrying multiple fills at the same price. Dedup key is
therefore `sha256(tx_hash | token_id | side | shares | price | traded_ts)`, which
was unique across all 500 rows.

**ASSUMPTION** the feed only ever contains the fills of the queried wallet, and
`side` is from that wallet's perspective. Every row in the sample carried the
target's own `proxyWallet`, consistent with this.

**UNVERIFIED** whether `/activity` can return rows out of timestamp order, or
back-fill older rows late. The bot does not depend on ordering — it dedups by key
and filters by age — so either behaviour is safe.

### `clob.polymarket.com/book` and `/books`
**VERIFIED** `GET /book?token_id=…` returns
`{market, asset_id, timestamp, hash, bids, asks, min_order_size, tick_size, neg_risk, last_trade_price}`.

**VERIFIED — the sharp edge:** `asks` arrives sorted **descending** (`asks[0]` is
the *worst* price, e.g. 0.99; the best ask is `asks[-1]`). `bids` arrives
ascending. Walking either from index 0 without sorting fills at catastrophic
prices. `OrderBook.from_clob` re-sorts both sides to best-first and asserts
nothing about the input order.

**VERIFIED** prices and sizes are **strings**, not numbers. `timestamp` is
**milliseconds** here, unlike `/activity`'s seconds.

**VERIFIED** `POST /books` accepts `[{"token_id": …}, …]` and batches. Used to
mark up to ~50 open positions in one or two calls.

**VERIFIED — the second sharp edge:** `POST /books` **omits** tokens with no book
entirely rather than returning an empty one. Requesting 2 tokens where one had
resolved returned 1 entry, no error. Mark-to-market must treat "absent from the
batch response" as "no book" and fall back to gamma, not as an error and never
as a zero.

**VERIFIED** resolved markets return `bids: [], asks: []` from `GET /book`.

**VERIFIED** `min_order_size` is `5` (shares) on every market sampled.

**VERIFIED** `tick_size` is `0.001` on 99 of 100 active markets sampled; `0.01`
on 1. Prices as low as `0.0005` occur. The fill simulator reads `tick_size` per
book and never assumes whole cents — at a 2¢ price, rounding to a cent is a 50%
error.

### `gamma-api.polymarket.com/markets`
**VERIFIED** `?condition_ids=<id>&condition_ids=<id>` filters by market.

**VERIFIED** `clobTokenIds`, `outcomes`, and `outcomePrices` are JSON **strings
that need a second parse**, not arrays. `clobTokenIds` is index-aligned with
`outcomes` and `outcomePrices` — that alignment is how we price a specific token.

**VERIFIED** unknown `condition_ids` are **silently dropped** — ask for 2, get 1,
HTTP 200, no error. The client reconciles by id and logs each miss.

**VERIFIED** resolution is signalled by `closed: true`. `active` stays `true` on
resolved markets, so `active` is *not* a resolution flag. Resolved markets show
`outcomePrices` of `["0","1"]` / `["1","0"]`.

**ASSUMPTION** `outcomePrices` ≥ 0.99 means that token settles at $1.00 and
≤ 0.01 means $0.00. A market that is `closed` but shows an intermediate price is
treated as *not settleable* and left open rather than guessed at.

**UNVERIFIED** how `umaResolutionStatuses` (`[]`, `["proposed"]`, presumably
`["disputed"]`) interacts with `closed`. A market observed as `closed: true` with
status `"proposed"` could in principle be disputed and revised. We settle on
`closed` + an unambiguous `outcomePrices`. If a dispute reverses a settlement
after we booked it, we will not currently notice.

---

## 3. Fees — the biggest single modelling decision

**VERIFIED** fees are **on** for this wallet's markets: `feesEnabled: true`,
`feeType: "weather_fees"`, `feeSchedule: {exponent: 1, rate: 0.05, takerOnly: true, rebateRate: 0.25}`.

**VERIFIED** rates by category, sampled across 40 top-volume live markets:

| feeType | rate |
|---|---|
| `crypto_fees_v2` | 0.07 |
| `sports_fees_v3`, `economics_fees`, `culture_fees`, `weather_fees` | 0.05 |
| `politics_fees` | 0.04 |
| `sports_fees_v2` | 0.03 |
| (7 of 40 markets) | fees disabled |

**Formula used:** `fee_usd = shares × rate × p × (1 − p)`

**UNVERIFIED but strongly corroborated:** the docs' own worked example — 100
crypto shares at 50¢ costing $1.75 — reconciles only with `p × (1−p)`
(100 × 0.07 × 0.25 = 1.75), not with the older `min(p, 1−p)` form
(which gives 3.50). `feeSchedule.exponent: 1` is read as the exponent on that
term and is 1 everywhere sampled; a non-1 exponent has never been observed and is
**not** currently handled.

**ASSUMPTION** the fee is charged in USDC and deducted from proceeds / added to
cost. Polymarket may in practice charge in the output asset (shares on a buy).
For paper accounting the dollar effect is what matters.

**ASSUMPTION** we are always the taker. We simulate marketable orders that cross
the book, so this is true by construction. `rebateRate` applies to makers and is
therefore ignored.

**VERIFIED** settlement is not a trade, so it carries **no fee**. This makes the
two exit paths structurally different products:

| Exit path | Fees paid | Cost at his 5¢ median |
|---|---|---|
| Held to resolution | entry only | ~4.75% of stake |
| Mirrored sell | entry + exit | ~9.5% of stake |

The dashboard therefore splits P&L by exit path as well as by entry band.

### Fee guardrails (per your instruction)
- Per-market rate is read from gamma and **cached** (1h TTL) — not fetched per fill.
- Any fallback logs at **WARN** with the conditionId, every time.
- `fee_rate_fallback` **cannot be zero**: `config.py` refuses to start otherwise.
  A missing fee field silently becoming free trading is the same bug class as a
  probability function returning a hard 1.0, and it biases paper *upward*
  precisely in the cheap band this wallet trades.
- `feesEnabled: false` from the API is the *only* path to a zero rate, and it is
  a positive assertion from the API rather than an absence of data.

### Pre-live checklist
- [ ] **Verify the fee formula empirically.** Place one small real trade and
      compare the actual charge against `fee_usd = shares × rate × p × (1−p)`.
      Do not trust the docs into live money.

---

## 4. Rate limits — what was measured vs what was read

**VERIFIED (measured):** none of the three APIs return any rate-limit headers.
No `x-ratelimit-*`, no `retry-after`. All three sit behind Cloudflare. Backoff in
this client is therefore blind and defensive, not header-driven.

**UNVERIFIED (from docs, not measured):** gamma general 4,000/10s and `/markets`
300/10s; data-api general 1,000/10s and `/trades` 200/10s; CLOB market data
1,500/10s. Docs also state that exceeding a limit **throttles** rather than
returning 429 — so slow responses under load are expected, not failures.

Note on an earlier inconsistency: two different data-api figures were quoted
(1,000/10s and 200/10s). Neither was measured. The 1,000 is the documented
general data-api limit; the 200 is specific to `/trades`. We use `/activity`,
which the docs do not list, so **the applicable limit for our actual endpoint is
unknown**. At a 15s poll we issue ~4 requests/minute, which is far below any of
these numbers, so this is not currently a live risk.

The client handles 429 and 5xx with exponential backoff plus jitter and honours
`Retry-After` when present, despite never having observed one.

---

## 5. Deviations from the spec, and why

1. **Sell floor is the book's `min_order_size` (5 shares), not 1 share.**
   Per your correction. Applied on *both* sides: a buy that cannot clear the
   minimum is a skip, and a partial sell whose remainder would fall below the
   minimum closes the whole position. A 1–4 share residual is unsellable live and
   would inflate paper equity with positions that could never be exited.

2. **`fee_bps` became `fee_rate_fallback` + `fee_bps_override`.** A flat bps of
   notional is the wrong *shape* for Polymarket's fee, understating cost roughly
   2× at cheap prices. `fee_bps_override` preserves a flat-bps mode for testing.

3. **The reconciliation identity is `cash + open cost basis − realised P&L =
   starting capital`,** not `cash + cost basis + realised P&L`. Cash already
   contains realised gains; adding them again double-counts every closed
   position. Worked example: buy $3, sell for $4 net → cash 151, basis 0,
   realised 1. `151 + 0 − 1 = 150` ✓; `151 + 0 + 1 = 152` ✗.

4. **Cash is derived, never stored.** `cash = starting_capital + Σ(net_usd)` over
   the executions ledger, where settlements are ledger rows with `side='SETTLE'`.
   There is no mutable cash number that can drift from the ledger or survive a
   crash unpersisted.

5. **`processed_trade_ids` stores the full trade, not just the key.** It doubles
   as the source for his volume-weighted average entry per token, which needs his
   whole fill history and not only the fill we copied.

6. **Slippage is measured in percent, not cents.** At a 2¢ entry, one cent of
   slippage is a 50% worse entry; a cent-denominated threshold is meaningless
   here. Both absolute and percent are stored; the dashboard leads with percent.

7. **Slippage breach warns, never halts — in paper mode.** Halting truncates the
   sample. **In live mode it should actually halt** at `slippage_warn_pct`:
   different mode, different purpose. Not yet implemented, since live is not
   implemented.

8. **The $3 stake is the TOTAL cash outlay, fee included.** "Every copy spends
   a fixed $3.00. Not more, not less." So the book walk charges each level at
   `price + fee_per_share(price)` and stops when $3 of cash is committed, rather
   than buying $3 of shares and paying the fee on top (which would spend ~$3.15).
   At a 5c entry this is the difference between 60.0 and 57.1 shares.

9. **The entry fee is capitalised into cost basis**, not booked as an immediate
   realised loss. It has to land in exactly one place or the reconciliation
   identity breaks by exactly the fee -- `test_double_counting_the_entry_fee_
   breaks_the_identity` and `test_omitting_the_fee_from_basis_also_breaks_the_
   identity` pin both failure directions.

10. **Batch responses are never zipped by index.** `batch.reconcile_batch()`
    keys every batch response by its own identifier, logs each omission at WARN,
    and flags any id we never requested. This is a codebase-wide rule, not a
    local fix: Polymarket has now been observed silently dropping items from a
    batch twice (gamma on unknown condition_ids, POST /books on resolved
    tokens), both with HTTP 200. Index-alignment would attribute one token's
    book to another token and mark a position at the wrong price -- silently,
    with a plausible-looking number.

11. **Only a book-derived mark can become the closing line.** gamma's price for
    a resolved market is 0 or 1, which is the outcome, not a line; using it
    would make CLV a restatement of the result. When the book is empty the mark
    falls back to gamma so equity stays correct, but `closing_line_*` is left
    alone.

12. **His position stats keep updating after the copy.** Our entry is frozen at
    one fill; his keeps improving as he scales in (median ~5 fills per token).
    `his_position_usd_at_copy` is frozen for the record, while
    `his_vwap_entry`, `his_position_usd_total` and `size_ratio_vs_total` are
    refreshed on every later fill of his in a token we hold. Without this the
    averaging-down gap would be invisible by construction.

13. **Two size ratios, because they answer different questions.**
    `size_ratio` = our $3 / the fill we copied. `size_ratio_vs_total` = our $3 /
    his entire position in that token. The first says how much bigger our order
    was than his; the second says how much bigger our bet is than his
    conviction. Observed example: he opened a token with $0.50 and scaled to
    $0.88 across 9 fills, so the two ratios are 6.0x and 3.4x.

---

## 6. Metrics

### CLV is the primary metric
At a 5c average entry the break-even win rate is ~5.2%. Sixty resolved copies
give ~3 expected winners; a real edge gives ~4. Distinguishing those on P&L
needs roughly 700 resolved copies -- two months or more. CLV is continuous per
trade rather than 0/1, so it says something long before then.

Because CLV depends on capturing a price from a book that empties the instant a
market resolves, the capture is instrumented rather than assumed:

* the marking interval tightens from 300s to 60s within 30 minutes of `endDate`,
  so a market resolving between two marks does not lose its closing line;
* `closing_line_bid`, `closing_line_ask` and `closing_line_spread` are stored
  alongside the price, because a mid taken across a wide spread is a fiction --
  `clv_max_spread` (0.05) splits clean captures from wide ones;
* `closing_line_age_seconds` records how stale the capture was at resolution;
* the capture **failure rate** is a first-class dashboard number. If it is high,
  the primary metric is broken and that needs to be visible immediately.

### Expected vs actual winners
Under the null that each market was fairly priced at our entry:

    expected winners = Σ pᵢ        variance = Σ pᵢ(1 − pᵢ)

over resolved copies, where `pᵢ` is our entry price. Independent Bernoulli
trials with differing p, so variances add. At 60 copies averaging 5c that is
**3 ± 1.7 (1 s.d.)**, which is exactly why a red P&L number in week two means
nothing. This sits under the headline number so "unlucky" and "broken" are
visually distinguishable.

**ASSUMPTION** our entry price is a fair estimate of the true probability under
the null. That is the null hypothesis itself, not a claim about reality -- if
the wallet has edge, actual should exceed expected, which is the test.

---

## 7. Measured: what a \$3 order does to these books

Probed on 2026-08-20 against every token this wallet traded in 8 days. 83 of 86
had already resolved and returned empty books, leaving 3 live -- too few to be a
statistic, but enough to confirm the mechanism:

| best ask | our $3 VWAP | depth cost | levels eaten |
|---|---|---|---|
| 0.0300 | 0.0534 | **+78.0%** | 4 |
| 0.0700 | 0.0736 | +5.1% | 2 |
| 0.0600 | 0.0600 | −0.0% | 1 |

This is pure depth cost measured on a single snapshot -- zero latency, zero
drift. One book in three moved 78% against a $3 order. Any model that filled at
the mid or at the best ask would have been wrong by that much on that trade.

**Not measured, and not measurable retrospectively:** the real latency slippage
(his fill vs our fill ~15s later). It needs the book as it stood seconds after
his trade, and no historical book endpoint is available. Comparing his old fills
against today's books measures hours of drift, not our latency, and was
discarded rather than reported. The bot collects this going forward; it is what
`slippage_vs_his_entry_pct` is for.

14. **A 404 from CLOB `/book` is a state, not an error.** **VERIFIED
    2026-08-20:** `GET /book?token_id=…` returns **HTTP 404** for a token with
    no book (resolved market), rather than an empty book. This is the
    single-fetch counterpart of `POST /books` omitting the same token. It is
    now mapped to an empty `OrderBook` carrying its own token id, which every
    caller already handles, and **404 and other 4xx are never retried** — the
    original client burned all five backoff attempts (~15s) per resolved token,
    which made a single pass over 100 trades take ~30 minutes instead of ~5
    seconds. Only 429, 5xx and transport errors retry.

15. **The look-ahead guard bounds lag; it does not require the book to precede
    the decision.** A live fill legitimately uses a book fetched milliseconds
    after we decide — we cannot fill on a book we have not fetched, and "we
    fill at the book as it exists when we see it" is the honest model. Requiring
    the book to be strictly earlier would reject every real fill. So the guard
    compares in **milliseconds** (a second-resolution decision is treated as
    occurring at `.000` of that second, the earliest instant it could have been)
    and rejects anything later than `max_book_lag_seconds`. Live uses 5s, which
    covers a fetch round trip and still catches a book from minutes later.
    **Replay and tests use 0**, which requires the book to be provably earlier
    and closes the truncation hole where a book stamped 400ms after the decision
    fell into the same whole second and passed.

---

## 8. The shadow size ladder

On every in-universe, fresh signal — copies and skips alike — the same walk is
run against the **same book snapshot** at several sizes: `$1`, `$3`, `$10`, his
own fill size, and his whole position in that token. It costs no extra API
calls, touches no cash, and opens no position.

Three questions are kept separate per rung, because collapsing them destroys the
measurement:

* `filled` — could the book absorb this size at all?
* `cleared_max_fill` — was the resulting price acceptable?
* `below_min_order_size` — could the order legally be placed?

Both the price cap and the 5-share minimum are lifted during the walk and judged
afterwards. **His own trade sizes are frequently under the 5-share floor** once
the fee comes out of the same budget (his median fill is ~$1.26), so enforcing
the minimum during the walk would have thrown away the depth measurement from
the single most informative rung.

### First real measurement (2026-08-20, 3 live books)

| rung | avg size | median depth cost | levels eaten |
|---|---|---|---|
| `$1` | $1.00 | +24.0% | 1.7 |
| `his_fill` | $1.26 | **+0.0%** | 1.2 |
| `$3` | $3.00 | **+70.0%** | 3.4 |
| `his_position` | $3.41 | +10.5% | 1.8 |
| `$10` | $10.00 | +183.7% | 6.2 |

n is far too small to conclude anything, and this is a cold-start sample where
most books were empty. But the shape is the thing to watch: **his size moves
these books ~0%, ours moves them ~70%.** If that holds up over a real sample,
size is the binding constraint, not selection — and his edge may partly be an
artifact of being small enough not to move the market.

### Integrity guarantees

Three things protect the ladder from being quietly wrong, because if it is
wrong every capacity conclusion drawn from it is wrong too:

1. **One row per (signal, rung), always.** A rung that cannot be measured is
   stored with a NULL depth cost and an `unmeasurable_reason`, never dropped.
   Dropping it would let a median silently span different subsets of signals.
2. **Aggregation is paired-only.** `capacity_curve()` includes a signal only
   when *every* rung produced a depth cost on that snapshot, so each rung's
   median is taken over exactly the same set of books. The dashboard shows n
   per rung and the count of excluded signals.
3. **Monotonicity is asserted, not hoped for.** Within a single snapshot, depth
   cost must be non-decreasing across ascending rung sizes.
   `assert_monotonic_depth_cost()` **raises** `LadderInversionError` on
   violation. It is arithmetically impossible for a correct walk — levels are
   consumed cheapest-first, so a larger order can only reach equal or worse
   prices — so a violation means the walk is broken and must stop the trade,
   not log a warning.

### The reported inversion, diagnosed

An early dashboard showed `his_fill` at "avg $1.26" costing +0.0% while `$1.00`
cost +24%, and `his_position` at "avg $3.41" costing +10.5% while `$3.00` cost
+70%. Both inversions ran the same direction: making his sizes look free and
ours look expensive.

**It was not a walk bug.** Checked per-signal across 28 fully-measured signals:
**zero monotonicity violations**, and all 99 signals had all five rungs (no
partial measurement — an empty book makes every rung unmeasurable at once, so
pairing was already intact).

The fault was in the aggregate table, which printed each rung's **mean** size
beside its **median** depth cost. His fill sizes are heavily skewed:

| rung | min | median | mean | max |
|---|---|---|---|---|
| `his_fill` | $0.00 | **$0.20** | $1.47 | $12.25 |
| `his_position` | $0.04 | **$2.23** | $3.43 | $13.49 |

So the row labelled "$1.26" was reporting the median cost of a rung that is
typically **20 cents**. Comparing a median cost against a mean size, in a
column that invited exactly that inference. The curve now reports **median**
size with the min-max range beside it.

The underlying reading is unchanged and still worth taking seriously: at his
typical fill size the book barely moves, at ours it does. But it is a
20c-vs-$3 comparison, not $1.26-vs-$3.

---

## 9. The mark path

Marks are stored as **rows** in a `marks` table, not as a single overwritten
field. A single overwritten mark makes CLV all-or-nothing on one capture landing
in the window before the book empties.

With the full path, CLV is also computed at fixed horizons after entry (15, 60,
360 minutes by default). Fixed horizons are **always** capturable because the
book is still live at those points, so a failed closing-line capture no longer
costs the entire measurement for that trade. It also distinguishes a price that
drifts our way at +1h but gives it back by close from one that never moves at
all.

Only book-derived marks (`book_mid`, `book_bid`) qualify for either the closing
line or a horizon measurement. A gamma price for a resolved market is 0 or 1 —
the outcome, not a market price — and using it would turn CLV into a restatement
of the result.

**Growth:** ~10 new positions/day × ~288 marks each (5-minute cadence over a
~24h weather market) is roughly 3k rows/day, ~1M/year. Trivial for SQLite on a
t3.small with indexes on **both** `(position_id, ts)` (horizon lookups) and
`(token_id, ts)` (per-token history). Both are created on startup, including on
databases made before the second index existed.

### Book timestamps, measured

**VERIFIED 2026-08-20:** the CLOB `timestamp` is the book's **last-update**
time, not the time the response was produced. Repeated fetches of an unchanged
book return an identical timestamp, and most fetches yield a *negative* lag
against the decision moment. Across 12 live fetches: min −7984ms, median
−4005ms, max **+769ms**. The 2s `max_book_lag_seconds` bound admits all of them
with 1231ms of headroom, so the tightening from 5s to 2s is safe.

---

## 10. The stopping rule

Fixed while neutral, on purpose, so it cannot be renegotiated at week three
while down money and hopeful. Recorded here verbatim as agreed. The dashboard
renders current standing against each threshold; the numbers live in
`config.yaml` under the `kill_*` / `pnl_verdict_*` / `go_live_*` keys.

**Do not loosen these mid-experiment.** If a threshold turns out to be wrong,
that is a finding to write down here, not an edit to make quietly.

### Kill conditions — any one of these ends the project

* **Depth, $3:** after 50+ signals with valid ladder rows, if median depth cost
  at $3 exceeds 25%, $3 is not viable. Retest at $1 before killing outright.
* **Depth, $1:** if median depth cost at $1 also exceeds 25% across 50+
  signals, the strategy is unreachable at any size I'd trade. Kill.
* **CLV:** after 100 copies, if median CLV at the +60min horizon is negative,
  kill regardless of P&L.
* **Metric integrity:** if closing-line capture failure exceeds 30%, stop and
  fix before collecting further — a broken primary metric means I'm
  accumulating unusable data.

### Continue conditions

* P&L verdict requires 700 resolved copies, not fewer. At ~10/day that's ~10
  weeks. Anything before that is noise at these prices and I will not read it
  as a result.
* Go-live requires: all kill conditions clear, positive median CLV, and 30
  consecutive days without a crash or stall.

### Why the depth number carries the project

If the depth cost at $3 survives proper measurement at anything like the early
reading, it is the whole story. At a 5c ask a 70% depth cost means an 8.5c
fill. After fees that needs roughly an **8.9% win rate** to break even where
the market's fair rate is **5%** — his picks would have to be about **78%
better than fair** just to cover the slippage, before any profit at all.
Meanwhile at his own size he pays near zero.

If that holds, his edge is not copyable at our size and no amount of tuning
fixes it. That is why the ladder has an assertion rather than a warning.

### How "no crash or stall" is measured

`analytics.days_without_stall()` walks the heartbeat table and finds the last
gap longer than 300 seconds. API failures do **not** count as stalls — those
still write a heartbeat, which is exactly why the heartbeat is written on every
pass rather than only successful ones. Only the process actually not running
breaks the streak.

---

## 11. Secrets

No API keys, private keys, or secrets appear in the code or in `config.yaml`, and
none are needed for paper mode — all three endpoints used are public and
unauthenticated.

When live execution is built it will read from environment variables only. The
names are declared in one place, `config.LIVE_ENV_VARS`:

`POLYMARKET_PRIVATE_KEY`, `POLYMARKET_API_KEY`, `POLYMARKET_API_SECRET`,
`POLYMARKET_API_PASSPHRASE`, `POLYMARKET_PROXY_ADDRESS`.

`COPYBOT_ALLOW_PUBLIC_DASHBOARD=1` is the only escape hatch for binding the
dashboard off localhost. Don't set it.

---

## 12. Still open

- Whether a disputed UMA resolution can reverse a settlement we already booked.
- Whether `feeSchedule.exponent` is ever ≠ 1 (never observed; unhandled).
- Whether `/activity` back-fills late rows (bot is insensitive either way).
- The real rate limit on `/activity` specifically.
- Whether `POST /books` and `GET /book` ever disagree about a token having a
  book. Both are treated as "no book" so a disagreement is harmless, but it has
  not been checked.
- The strategy's real latency slippage. Still unmeasurable retrospectively; the
  cold-start run produced only 3 fills and those carried a +124% average, which
  is a sample of three against a book that had moved, not a slippage estimate.
