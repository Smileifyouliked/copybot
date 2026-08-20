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

---

## 6. Secrets

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

## 7. Still open

- Whether a disputed UMA resolution can reverse a settlement we already booked.
- Whether `feeSchedule.exponent` is ever ≠ 1 (never observed; unhandled).
- Whether `/activity` back-fills late rows (bot is insensitive either way).
- The real rate limit on `/activity` specifically.
