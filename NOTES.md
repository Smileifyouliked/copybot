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

## 8b. Standing rule: Polymarket omits silently

Four separate instances now, every one returning HTTP 200 or a benign-looking
status while handing back less than was asked for:

| # | Call | Silent failure |
|---|---|---|
| 1 | gamma `/markets?condition_ids=` | unknown ids dropped from the response |
| 2 | CLOB `POST /books` | tokens with no book omitted entirely |
| 3 | CLOB `GET /book` | 404 for a resolved token, not an empty book |
| 4 | gamma `/markets?condition_ids=` | **defaults to open-only; every resolved market invisible** |

**The invariant, codebase-wide:** any call that requests a *set* must reconcile
the response by ID, treat every absence as an explicit missing value, and log it
at WARN. `batch.reconcile_batch()` is the only sanctioned way to consume one.
Never index-align a response against a request.

Paged calls have a different failure mode and their own guard. A page that
returns exactly `limit` rows means the server had at least that many and
dropped the rest without signalling. `batch.assert_complete_page()` warns on
that. It matters for `/activity` specifically: after an outage longer than the
window `limit` covers, his fill history develops holes, which corrupts his VWAP
and the sell-mirroring fraction — even though the missed trades are too old to
copy and so produce no visible symptom.

### Audit of every call site (2026-08-23)

| Call site | Kind | Status |
|---|---|---|
| `get_markets` | set | reconciled by conditionId; both resolution states requested |
| `get_books` | set | reconciled by token id; absence becomes an explicit empty book |
| `get_book` | single | 404 becomes an empty book, never retried |
| `get_activity` | paged | full-page truncation warning |
| `fee_rate_for` | via `get_markets` | inherits the guard |
| `strategy.mark_positions` | consumes both | `.get()` with an explicit fallback chain |
| `strategy.check_resolutions` | consumes `get_markets` | missing market skips rather than settles |
| `strategy.next_mark_interval` | consumes `get_markets` | missing market keeps the default interval |

No unguarded call sites remain.

### Did the settlement bug corrupt CLV?

**No.** Traced rather than assumed:

* `closing_line_*` is written only when the mark source is `book_mid` or
  `book_bid`, and those come from `get_books`, which was never broken. The
  closing-line data collected so far is sound.
* The `marks` table, which feeds fixed-horizon CLV, is likewise fed only from
  `get_books`. Horizon CLV is sound.
* What never happened is CLV *at close*: `_clv_fields` runs from `_settle`, and
  `_settle` never ran. So the closing-line prices were captured and rolled
  forward correctly but never frozen into `clv_abs`/`clv_pct`.
* One thing **was** degraded: mark-to-market. A resolved position fell through
  the gamma branch to `LAST_KNOWN`, so equity showed resolved positions at
  their last traded book price instead of $1.00 or $0.00.

Net: the collected data is usable, equity figures from before the fix are not.

### Two bugs found by running it, not by reading it

**Closing on intent instead of on outcome.** When he sold out of a token we
set out to close our whole position, but a thin bid side can absorb less than
we hold. The position was marked closed with `shares = 0` and
`cost_basis_usd = 0` regardless, discarding the shares that did not sell along
with their basis. The money left the ledger silently: reconciliation went out
by exactly the basis of the unsold remainder. Found on the deployed database,
which was $0.0587 out of balance, and reproduced at $1.40 on a deliberately
thin book. Closure now depends on what actually sold, and an unsellable
remainder stays held and is logged at WARN.

Worth noting what this says about the invariant: `reconcile()` caught it, which
is why it exists, but only as a number that did not add up. It could not say
where the money went. The reproduction did.

**Nothing stops two bots sharing one database.** A `screen` session from the
earlier deployment path kept running for 41 hours alongside the systemd unit,
both polling and both writing. No double-copy resulted -- the dedup key is a
PRIMARY KEY and `BEGIN IMMEDIATE` serialises writers -- but the guard is
checked *before* the transaction opens, so the race window is real and it
simply did not fire. Two fixes pending: a lock file so a second instance
refuses to start against a database another process holds, and moving the
dedup check inside the write transaction.

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

### The headline metric: our VWAP ÷ his VWAP

His sub-50c book returns **+41.3% net of full taker fees** (+47.5% gross; the
API's `usdcSize` is `size x price` and excludes fees). We buy the same outcome,
so the only thing that separates our result from his is the price we pay.

    break-even = our VWAP / his VWAP = 1.395

Above 1.395 we lose money on a confirmed edge. Below it we make money. This
ratio is the dashboard's headline entry-quality number, with 1.395 drawn as a
hard line, and it replaces single-fill slippage as the metric that matters.

| our entry vs his | our net return |
|---|---|
| 1.00x | +41.3% |
| 1.20x | +17.8% |
| **1.395x** | **0%** |
| 1.78x | -20.6% |
| 2.97x | -52.4% |

### Kill conditions — any one of these ends the project

* **Depth, $3:** after 50+ signals with valid ladder rows, if median depth cost
  at $3 exceeds 25%, $3 is not viable. Retest at $1 before killing outright.
* **Depth, $1:** if median depth cost at $1 also exceeds 25% across 50+
  signals, the strategy is unreachable at any size I'd trade. Kill.
* **Entry quality:** after 50 filled copies, if mean (our VWAP / his VWAP)
  exceeds **1.395**, limit orders did not solve the problem and the strategy is
  unreachable at our execution quality. Kill. *(Wired: it is the first row of
  the stopping-rule table in both the dashboard and the text report. The
  threshold reads the mean rather than the median because the mean is what the
  arithmetic behind 1.395 is about -- total spent over total he spent -- and a
  median hides a tail of terrible entries.)*
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

### Expected shape of the equity curve

Two reasons it will look worse than his, both structural rather than faults:

1. **He hedges about a quarter of the time.** 676 of his events carry both a
   sub-50c leg and a 90c+ leg on the same city-day, and **28.4% of his sub-50c
   money sits inside those hedged events**. We copy only the cheap leg. On that
   quarter he collects the favourite's payout when the tail misses and we do
   not, so our variance is strictly higher than his on the same picks.
2. **The cheap end wins rarely and large.** The 0-10c band wins 5.6% of the
   time at +62.5% gross return.

Expect long red stretches punctuated by rare large wins. **That is the
expected shape, not a malfunction.** It is also why the P&L verdict needs 700
resolved copies and why entry quality and CLV are judged first.

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

## 11b. Limit orders: what the run 2 code actually does

Run 1 crossed the spread. Its own numbers killed it: fills landed 11-197% above
his price against a break-even of 1.395x. Run 2 rests at his price instead. This
section is the assumption list for that change, because every line here is a
choice that could be wrong.

### The model

* **We rest at his exact fill price.** Never one tick better, never a chase.
  The order fills at that price or it does not fill at all, so the metric stops
  being "how much worse did we pay" and becomes "how often did we get in".
* **We join at the BACK of the queue.** Everything already bidding at our price
  or better must trade before we get a share. This is the conservative reading
  and it is almost certainly correct for a taker-driven book.
* **Only executed prints fill us.** A quote at our price is not a trade at our
  price. Fills come from `data-api /trades`, never from the book.
* **Only prints on OUR token count.** The tape is fetched per market and a
  market has two tokens; a 20c print on the complementary outcome is a
  different book with a different queue. Every live row carries `asset`, so the
  filter is exact.
* **His own prints never count.** His buy is not the seller filling our bid.
* **Prints from before we placed never count.** That is look-ahead in reverse.
* **Maker fills pay no fee.** `takerOnly: true` on every schedule sampled.
* **TTL is 300s.** After that the order is cancelled. An expired order costs
  nothing, books nothing, and frees both its budget and its slot.
* **A book already offering at or below his price is CROSSED, not rested.**
  Resting there would cross anyway. It pays the taker fee and is recorded as
  `crossed_inside`, separately from resting fills, so it cannot flatter the
  fill rate.

### What could still be wrong with it

* **The `side` convention is unresolved.** Which label means "a seller hit the
  bid" was never pinned down: classifying live prints against a simultaneous
  quote yielded n=1, and a median comparison across two assets disagreed with
  itself. Picking one is NOT conservative -- under the inverted reading we are
  not under-counting, we are measuring a disjoint population, and the direction
  of the error is unknown. So the configured convention drives the position and
  the opposite reading is computed and stored on every order. If the two
  diverge by more than 10 points the dashboard says so and neither number can
  be used until `side-check` settles it.
* **Queue position is modelled, not observed.** We cannot see our own order in
  a real queue because there is no order. Back-of-queue is an assumption.
* **No cancel/replace.** He may fill at a price we are still waiting at,
  because the market traded through him and not through us. We take the miss.

### Fill rate is the experiment

Of his buys we tried to match, what fraction filled at his price? That number
decides the project. It appears on the dashboard, in the text report, and in
`compare`, always alongside the opposite-convention reading.

## 11c. Following him down, and what is NOT deployed

The per-token budget is a hard cap. The schedule ([0.34, 0.33, 0.33]) is the
intended split across his successive fills, and the exchange minimum is a hard
floor under each tranche.

**Nothing is ever rounded up to consume the rest of the budget.** An earlier
version absorbed a remainder that could not clear the floor at the current
price. That was wrong: an unspendable remainder costs an under-deployed token,
while absorbing it costs his *opening* price -- the single worst price he pays,
and the exact thing follow-him-down exists to avoid. The remainder is not
stranded, either: the floor is a share count, so it shrinks as the price falls,
and the fills we are waiting for are the cheaper ones. Expect deployed capital
per token to run below the nominal budget; that is the design, not a leak.

**A resting order counts against the cap.** Cash has not moved, but if it fills
it fills. Before this, four of his fills placed four orders against a
three-order budget and nothing checked it until the tape did.

**The floor follows what will actually happen, not what the mode is called.**
Crossing pays the taker fee, so its floor is `5 x (p + rate x p x (1-p))`.
Resting pays nothing, so its floor is `5 x p`. At 29c that is $1.50 versus
$1.45 -- the difference between one tranche and two.

**Known gap:** the floor is computed at HIS price, but a market-mode fill
happens at the BOOK's price. When the book has moved away from him, the order
is sized for his price and refused for the book's. In limit mode this cannot
happen (we rest at his price); in market mode the refusal is the safe outcome,
so it is left alone rather than sized up to chase a price we already decided
was too far away.

## 11d. Stake as a tested variable

$1 / $2 / $3, assigned per token by hashing the token id, so a restart keeps
the assignment and a token never moves between arms mid-position.

Rationale: a resting order is punished by **queue position**, not by depth. A
smaller order can fill far more often at the same price, which is the opposite
of how crossing behaves -- crossing punishes size through the book. So the size
trade-off inverts under limit orders and has to be measured rather than
assumed.

**Variants apply in limit mode only.** In market mode, depth cost is already
measured by the shadow ladder and varying size would add noise to a run that
cannot answer the question anyway.

**The confound, stated plainly:** the exchange minimum is a share count, so its
dollar value rises with price. At the 5-share minimum a $1 order is unplaceable
above 20c. Rather than silently dropping the whole 20-30c band out of the $1
arm -- measuring a different population, again -- a token snaps up to the
smallest variant that is placeable at that price. The arms therefore still
cover different price distributions below 20c. **This is physical, not
fixable.** Compare variants WITHIN a price band; a pooled comparison of the
arms is a comparison of prices wearing a stake's clothing.

## 11e. Runs, archiving and comparison

* A run is **never deleted.** Run 1 cost real observation time and is the only
  baseline run 2 can be judged against.
* `archive` copies the database aside through SQLite's backup API (not `cp`, so
  a running bot mid-transaction cannot hand us a torn file), stamps the copy
  with a UTC timestamp, and leaves the original untouched. Refuses to overwrite
  an existing archive.
* Every run is stamped into `runs` with a JSON snapshot of the config that
  produced it. A config file that has since been edited cannot answer "what
  were the settings then".
* Every position and every resting order carries its `run_id`, so one database
  spanning a market run and a limit run still answers either question.
* `compare` reports **entry quality only** -- our VWAP over his VWAP, fill rate
  under both conventions. **P&L is deliberately excluded**: two runs see
  different markets at different times, so a P&L gap between them is mostly
  which coin flips landed. The ratio is normalised by his own price on his own
  picks, which is why it survives the comparison and P&L does not.

## 11f. One bot per database

Enforced with `flock` on `<db_path>.lock`, taken before the database is opened.
A second instance refuses to start and exits 3.

This exists because it already happened: a manually started process ran
alongside the systemd unit for 41 hours against the same file. No copy was
made twice -- dedup held -- but two writers racing the same read-then-write
window is not a property to leave to luck. `flock` rather than a PID file
because the kernel releases it however the process dies, so a stale file can
never lock the bot out.

## 11g. The $0.63 and $1.48 positions — resolved, and it was a label

Two run-1 positions displayed `paid $0.63` and `paid $1.48` on a $3.00 budget.
Both the "concurrent writers corrupted it" theory and the "fill simulator
under-filled" theory were wrong. **The column was mislabelled.**

* `cost_basis_opened` is what we put in. It only ever grows.
* `cost_basis_usd` is the basis of the shares **still held**. A mirrored sell
  releases the sold share of it (`strategy.py`, sell path).
* The dashboard and the text report both printed `cost_basis_usd` under the
  heading "you paid".

So a position we funded with $3.00 and then mirrored an 80% sell out of
correctly held $0.60 of basis, and correctly displayed it as "paid $0.60".
Reproduced exactly in a test: fund a token to $3.00, mirror a sell of 80%,
read `paid 0.60` / `cost_basis_opened 3.00`.

Fixed: "paid" now reads `cost_basis_opened`, and a partly-sold position says so
and shows what is still in. Up/down stays measured against the shares still
held, because the money from the sold shares is already realised and already
counted in cash — charging it against the position again would double-count it.

The lesson worth keeping: **an anomalous number is a claim about a field, and
the field has to be checked before the mechanism is theorised about.** Two
plausible mechanisms were proposed for this before anyone read what the column
contained, and both were wrong.

## 11h. `doctor`: proving whether two bots ever wrote a database

A manually started process once ran alongside the systemd unit. The question
"did that damage the data" cannot be answered from the code, and memory of what
was running is not evidence. The heartbeat table settles it retrospectively:

**One process polling every N seconds cannot write more than 3600/N heartbeats
in an hour.** That is a hard ceiling, not a heuristic. An hour above it is proof
that something else wrote the same file. Backoff during API trouble only lowers
the rate, so the test under-reports and can never invent a second writer.

`doctor` reports the windows that exceed the ceiling with real timestamps, and
then checks what a race could actually have damaged:

* **Not torn rows.** SQLite serialises writes, so every execution row is one
  process's complete result. Concurrency cannot make a row smaller — which is
  why it was never a candidate explanation for S11g.
* **Over-spent tokens.** Both processes read "spent so far", both see room,
  both buy. The ledger still reconciles, so only a per-token cap check finds it.
* **Duplicate open positions** on one token, which tranche-merging forbids.

The verdict separates two things that a blanket pass/fail would blur: aggregate
**rates** (fills per day, capital deployed) span both processes and cannot be
read as one bot's behaviour, while **per-fill entry quality** is each fill's own
number against a real book and survives.

## 11i. First 21 hours live: what the run actually said

Exported bundle, 2026-09-02, 0.87 days of limit-mode running, 719 of his
trades seen, 406 resting orders finished, 28 positions.

### The headline number arrived fast, and it is bad

**3% of resting orders got any fill. 0% of the shares we asked for.** Under the
opposite `side` convention it is 1% -- so the convention ambiguity that S11b
worried about is **moot at these numbers**: both readings say the same thing.
That is worth banking. The experiment does not need `side-check` to answer its
first question.

At n=406 the standard error on a 3% rate is 0.85 points, so this is not a small
sample being over-read. Resting at his exact price essentially does not fill.

### The ratio was measuring the wrong thing

The bundle showed our VWAP / his VWAP = **0.982 mean**, comfortably inside
1.395, which reads as success. It is not.

`entry_path` was passed to the recorder and never stored, so resting fills and
crossed-inside fills were pooled into one number. They are not the same event:

* a **rested** fill is at his price *by construction*, so it contributes
  exactly 1.000 and tells you nothing you did not already know;
* a **crossed-inside** fill is wherever the book had already moved to. The run
  contains fills at 0.002 against his 0.151 -- a ratio of 0.013.

A handful of those drag the pooled mean under 1.0 regardless of how the resting
orders are doing. **The number that decides the project was a number about the
mix.** Fixed: `entry_path` is now stored on every position and execution, and
both reports split by it.

### The adverse-selection hypothesis, tested and rejected

The obvious reading of the P&L was that crossed-inside fills are adverse
selection -- we only get filled when the market has already moved against him,
so our bargains are bargains for a reason. The split looked damning:

    at his price          13 bets   +$3.17
    below his price        9 bets   -$1.47
    above his price        5 bets   -$2.32

**It does not survive the win counts.** Against the null that our entry price
IS the probability, the below-his-price group expected 0.78 winners and got 1.
That is dead on. The at-his-price group expected 2.11 and got 4 -- better, but
n=13.

The P&L gap is a **price-band artifact**, not selection: 0% of the 20-30c band
came from crossing, while 83% of the 10-20c band did. Cheap tokens lose almost
always; the crossing path is concentrated in cheap tokens; so the crossing path
loses. That is arithmetic about where the two paths occur, not evidence that
crossing picks losers.

Recorded because the hypothesis was plausible, specific, and wrong, and the
thing that killed it was counting expected winners rather than staring at P&L.

### CLV is tracking toward a kill

    +15min  -22.7% (n=22)
    +60min  -37.1% mean, -48.7% median (n=20)
    +360min  +8.3% (n=9)

The kill condition is median CLV at +60min negative after 100 copies. It is at
-48.7% with 35. Not fired, and not yet firable, but there is no reading of that
number that is encouraging. Note the low-price confound: a 1c entry that
resolves to 0.0015 is -85% CLV by construction, and the 0-10c band is where
most copies are.

## 11j. Three defects the first run exposed

None were found by reading the code.

1. **`entry_path` never persisted** (above). The experiment could not be read.

2. **The tape was fetched once per ORDER.** A market has one tape however many
   orders rest into it. 406 open orders re-fetching every 15s produced HTTP 429
   from `/activity`, and the consequence is not local: a rate limit makes the
   whole poll back off, his trades then age past `max_trade_age_seconds`, and
   they are skipped. `trade_older_than_max_age` was the LARGEST skip category
   at 82 -- a rate limit at the bottom of the stack became lost signals at the
   top. Now cached per market for the duration of one pass, and only one pass:
   an order can only fill on prints it has not seen yet.

3. **The truncation guardrail fired on every poll: 57,604 of 66,677 log lines,
   90% of the log.** `/activity` returns a full page of 100 every time, because
   the wallet has more than 100 trades. A warning that fires every 15 seconds
   forever is not a guardrail, it is a place for a real warning to hide -- the
   same failure as the 103 false FEE FALLBACK warnings in S8b. The real risk it
   was written for is a gap after downtime, so it now takes the keys we have
   already processed: if the OLDEST row on a full page is one we have seen, the
   page reaches back past everything new and there is no gap to warn about.

## 11k. What a code review of run 2 found

Fifteen defects, from reading the code rather than a bundle. Four of them
changed numbers the experiment is decided on; the rest were dead knobs,
mis-filed skips and wasted work. Grouped by what was actually wrong.

**Money that was promised twice, and shares that vanished.**

`db.cash()` is derived from executed fills, so a resting order spends nothing
until the tape fills it -- and every gate that asked "can we afford this?"
therefore saw the same dollar as free once per open order. With \$6 of capital,
five \$3 orders rested on five tokens; each one passed the gate. When the tape
filled all five, `_book_limit_fill` ran out of cash and returned early, but
`poll_resting_orders` persisted every order as fully filled anyway. Those
shares had no position and no ledger row, and on the next poll
`gained = filled_shares - row["filled_shares"]` was zero, so they could never
be booked. The run reported 5/5 orders filled and a 100% share fill rate
against two actual positions, and `reconcile()` stayed green throughout,
because no cash had moved.

Both halves are fixed. `db.free_cash()` = cash minus every live resting order,
and the pre-rest gate uses it. `_book_limit_fill` returns what it managed to
book, and the caller rewinds the order to that -- so an order that could not be
paid for stays partly unfilled, and the remainder is booked on a later poll if
cash frees up. Fill rate is the one number this run exists to produce; it now
counts only fills that became positions.

**A dead order reserved its budget forever.** `resting_exposure` counted
`status='partial'`, but `partial` is terminal -- an order that expired having
filled part of its budget. Its unfilled remainder was reserved against the
token's cap with nothing left that could ever release it, and `reconcile()`
could not see the loss because no cash moved. Only `resting` counts now.

**Winners were counted from proceeds.** `expected_vs_actual_winners` called a
resolution a winner when `proceeds_usd > 0`, but proceeds also carry every
mirrored partial sell. With `mirror_partial_sells: true`, a position he half
sold before the market resolved at \$0.00 ended with proceeds above zero and
was counted as a win. The z-score -- the primary read before 700 resolved
copies -- therefore ran systematically better than reality. It now reads the
SETTLE execution's own price. Resolution-closed positions with no SETTLE row
are reported as `unmeasured` rather than dropped silently.

**The size ratio was overwritten with a constant.** `_record_buy` writes
`size_ratio_vs_total` from the real cost basis; `_refresh_his_position`, which
runs on every later fill of his that we skip -- the common case, he averages ~5
fills per token -- recomputed it from `cfg.stake_per_copy_usd`. Under
`stake_variants_usd: [1, 2, 3]` that is wrong for every token not on the \$3
arm. It uses the position's own cost basis now.

**Two promises the config made and the code did not keep.**

`shadow_band_max_price: 0.50` says the 30-50c band is "recorded as shadow rows
... so the band's behaviour at our fill prices is measured rather than left as
a gap". `_handle_buy` returned at the price gate before fetching a book, so the
setting was read only by `config._validate` and the band produced zero rows for
the whole of run 2. The test that claimed to cover it asserted no buys and
unchanged cash -- which is exactly what a plain `return` does. In-band signals
now fetch and ladder a book like any other, and only the money is withheld.

`limit_queue_model` was validated as `back`/`front` and read by nothing:
`queue_ahead_shares` always summed every bid at or better than the limit. An
operator setting `front` got a run recorded in the `runs` table as having used
a rule that was never applied. `front` is wired now -- only strictly better
prices count as ahead of us -- and remains a counterfactual for testing the
queue assumption, not a setting for a run we would trust.

**Skips that told the wrong story.** A first fill on a token whose exchange
minimum costs more than the token's entire budget was filed as
`per_token_budget_already_spent`, with a detail reading "already put \$0.00 of
the \$1.00 budget". `SkipReason.BELOW_MIN_ORDER_SIZE` exists for exactly that
and is now used when the floor exceeds the whole budget; when it only exceeds
what remains, the budget really did run down and the old reason is still right.

A malformed `/activity` row was warned about and never recorded, so it re-warned
on every 15s poll for as long as it sat in the newest-100 window: ~5,760
identical lines a day, the same log-flooding failure S11j.3 was written to stop.
`SkipReason.MALFORMED_TRADE` was declared and never raised, so unreadable rows
appeared in no skip count at all. They are now recorded once, keyed by content,
and appear in the taxonomy.

**The buy path never asked whether the market was open.** `MarketMeta.closed`
and `accepting_orders` were parsed and read nowhere; `MARKET_CLOSED` and
`NO_MARKET_METADATA` were declared and never raised. A book outlives the market
it belongs to -- the window between a halt and the book being torn down -- so a
stale quote could open a paper position that could never have been placed live,
inflating the fill counts the go-live decision rests on. The gate now runs
before the book fetch, and metadata that cannot be fetched is not read as
consent: no metadata, no copy, recorded as its own reason so an outage looks
like an outage. `acceptingOrders` is parsed as tri-state, because gamma omitting
the field is not gamma saying `false`.

**The heartbeat table was pruned by count, which is not the same as by age.**
`prune_heartbeats(keep=5000)` is 20.8 hours at a 15s poll. Both things that
table exists to answer are longer: `days_without_stall` structurally capped at
0.87 against `go_live_min_stable_days: 30`, so `go_live.ready` could never
become true however long the bot ran, and `doctor.concurrent_writer_windows`
could no longer show the 41-hour overlap it was written to audit (S11h).
Retention is 60 days now, with a row cap far above it as a floor.

**The instance lock destroyed the evidence it existed to provide.**
`acquire()` opened the lock file with mode `"w"`, which truncates -- before the
`flock`. So the loser of the race erased the incumbent's pid on its way to being
refused, the error had no pid to name, and `cat copybot.sqlite3.lock` was empty
for the rest of the incumbent's life. Opened `"a+"` now, truncated only after
the lock is ours.

**Work done twice, and work done for nothing.** `build_state` computed
`clv_summary`, `capacity_curve` and `clv_at_horizons` and then called
`stopping_rules`, which recomputed all three -- a full GROUP BY over
`shadow_fills` plus one `mark_nearest` query per position per horizon, twice per
page load, in `textreport` too. They are passed in now. `poll_limit` resolved a
`FeeModel` per order per pass for `apply_tape`, which never used it; on a cache
miss that is two gamma requests, and on a market with no fee schedule a WARN --
the exact noise `_fee_for` suppresses for empty books, on a hotter path. The
parameter is gone: a maker fill is free.

**The dashboard took a write lock on the bot's database on every page load.**
`Database.__init__` runs the schema script, the ALTER TABLE migration pass and
an `INSERT OR IGNORE`, and the dashboard builds one per HTTP request -- so a
`/healthz` poller generated sustained write traffic against the file holding the
experiment's only copy of the data. `Database(..., read_only=True)` opens
`mode=ro` and touches nothing; a missing file still falls through to the normal
path, so the dashboard can come up before the bot has ever run.

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
- Whether the tape's `side` label is the taker's or the maker's. Both readings
  are computed on every resting order; `side-check` settles it once 30+ prints
  can be classified against a simultaneous quote. Until then no fill-rate
  number is trustworthy on its own.
- Whether back-of-queue is pessimistic enough. We never see our own order in a
  real queue, so the assumption cannot be validated from paper trading alone.
- Whether run 1's own database shows a second writer. `doctor` answers it from
  the heartbeat rate (S11g); it has not been run against the deployed file yet.
