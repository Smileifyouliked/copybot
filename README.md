# copybot — Polymarket copy-trading paper bot

Watches one Polymarket wallet and copies its buys with **fake money at real
prices**. Nothing here can place a real order: `LiveExecutor` raises on
construction and `mode: live` refuses to start.

Target wallet: `0x6297b93ea37ff92a57fd636410f3b71ebf74517e`

---

## What it does

1. Polls the wallet's trades every 15 seconds.
2. Copies each **new buy under 50¢** with a fixed **$3.00**, filled by walking
   the real order book — never at the mid, never at his price.
3. Mirrors his sells proportionally. Anything he never sells is held to
   resolution and settled at $1.00 or $0.00.
4. Records why it skipped every trade it didn't copy. **A skip is data.**
5. Serves a dashboard on localhost.

Starting capital is $150, so about 50 concurrent $3 positions.

---

## Install on Ubuntu (EC2)

```bash
sudo apt update && sudo apt install -y python3-venv git
git clone <your-repo-url> ~/copybot
cd ~/copybot

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

mkdir -p data logs
.venv/bin/python -m pytest tests/ -q          # 115 tests, all should pass
```

Sanity-check the config and the API before running for real:

```bash
PYTHONPATH=src .venv/bin/python -m copybot.main status
```

### Run it under systemd

```bash
sudo cp copybot.service copybot-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now copybot
sudo systemctl enable --now copybot-dashboard

systemctl status copybot
journalctl -u copybot -f
```

Both units use `Restart=always` and `RestartSec=10`. The bot recovers its full
state from SQLite on start, so a restart never double-copies anything.

If you did not install to `/home/ubuntu/copybot`, edit `User=`,
`WorkingDirectory=`, `ExecStart=` and `ReadWritePaths=` in both unit files.

### Run it by hand instead

```bash
cd ~/copybot
PYTHONPATH=src .venv/bin/python -m copybot.main run          # the bot
PYTHONPATH=src .venv/bin/python -m copybot.main dashboard    # the dashboard
```

---

## Viewing the dashboard

The dashboard has **no login**, so it binds `127.0.0.1` only. `config.py`
refuses to start on any other bind address.

**Do not open port 8080 in your EC2 security group.** Tunnel instead. From your
laptop:

```bash
ssh -N -L 8080:127.0.0.1:8080 ubuntu@YOUR_EC2_IP
```

Leave that running and open <http://localhost:8080> in your browser.

With a key file and a specific user:

```bash
ssh -i ~/.ssh/your-key.pem -N -L 8080:127.0.0.1:8080 ubuntu@YOUR_EC2_IP
```

If port 8080 is busy locally, map a different one — `-L 9000:127.0.0.1:8080`,
then browse to `http://localhost:9000`.

---

## How to read the numbers

### The top of the page

**"Your money right now"** is cash plus what the open bets are currently worth
if you sold them now. **"Up/Down $X since you started"** compares that to your
$150.

**The line under it — expected vs actual winners — matters more than the P&L.**
These are cheap long-shot bets: most lose, a few pay 20×. At a 5¢ average entry
you break even at about a 5.2% win rate, so 60 finished bets produce roughly
**3 expected winners ± 1.7**. Four winners is not success and two is not
failure — both are noise. The page tells you the normal range so you can tell
"unlucky" from "broken" instead of guessing. **Expect the money line to be red
and jagged for weeks. That is what this strategy looks like when it is working.**

### The status light

🟢 running (checked in the last minute) · 🟡 might be stuck (1–5 min) ·
🔴 stopped (over 5 min). It shows the actual time since the last check.

### "What our own order size costs us" — read this one

When we buy, we take the cheapest shares on offer and reach for pricier ones.
This panel is how much worse our average price ends up than the best price
showing, measured on the same prices at the same instant.

The ladder underneath simulates $1, $3, $10 and **his own size** against the
same prices, without betting. Three readings, all actionable:

* every size is cheap → size isn't the problem
* $1 is clean but $3 isn't → the edge is real but only in small size
* nothing clears at any size → stop now rather than in month three

The rung at **his** size is the sharpest one: if his size costs nothing and
ours costs a lot, his edge may just be that he's small enough not to move the
market.

### "Did the price move our way after we bought?"

This is the most useful early number. Wins are too rare to judge for months,
but every single bet tells you whether you got a good price. Positive means the
market moved toward our side after we bought. It's shown at 15 / 60 / 360
minutes and at the finish line — the fixed horizons are always measurable, the
finish line sometimes isn't.

Watch the capture rate underneath. **If it's low, this number can't be
trusted** and you should say so rather than read into it.

### "Where you stand against your own stopping rule"

The thresholds were fixed before starting, on purpose, so they cannot be argued
with later. The panel shows the current value beside each one so the decision is
a reading rather than an argument. **Any single breach ends the project.**

| Kill condition | Ends it when |
|---|---|
| Depth cost at $3 | median over 25% across 50+ paired signals (retest $1 first) |
| Depth cost at $1 | median also over 25% — unreachable at any size worth trading |
| Price moved our way (+60 min) | median negative after 100 copies, regardless of P&L |
| Final-price capture failures | over 30% — the primary metric is broken, fix before collecting more |

**P&L says nothing before 700 resolved copies** (~10 weeks at ~10/day). Going
live additionally needs positive median CLV and 30 consecutive days with no
crash or stall.

The full text, and why the depth number carries the whole project, is in
NOTES.md §10. Don't loosen these mid-experiment — if one turns out to be wrong,
write down why rather than editing it quietly.

### The warning banner

Appears when we're paying more than 15% worse than he is, on average. It is a
heads-up, not a stop — the bot keeps collecting data, because a truncated
sample answers nothing. (If you ever go live, that threshold should actually
halt trading. See NOTES.md.)

---

## Configuration

Everything tunable is in `config.yaml`; there are no magic numbers in the code.
The ones worth knowing:

| Key | Default | What it does |
|---|---|---|
| `stake_per_copy_usd` | 3.00 | Total cash per copy, **fee included** |
| `max_entry_price` | 0.50 | Only copy his buys below this |
| `our_max_fill_price` | 0.50 | Our own average fill must also clear this |
| `max_copies_per_token` | 1 | Don't stack $3 into one market |
| `max_trade_age_seconds` | 300 | Don't copy stale trades after downtime |
| `fee_rate_fallback` | 0.05 | Used only if the live fee lookup fails. **Cannot be zero** |
| `mode` | paper | `live` refuses to start |
| `shadow_ladder_usd` | [1, 3, 10] | Sizes measured on every signal |
| `slippage_warn_pct` | 15.0 | Banner threshold, in percent not cents |

Fees are **not** a flat rate. Polymarket charges
`shares × rate × price × (1 − price)`, which at a 5¢ entry is about 4.75% of
your stake each way. Settlement is not a trade and pays no fee, so holding to
the end costs roughly half what selling early costs.

---

## Files

```
config.yaml            every tunable
src/copybot/
  fills.py             order-book walking      ← the file that decides if this is honest
  fees.py              the real fee formula
  strategy.py          buy / sell / resolution paths
  executor.py          PaperExecutor + the live stub
  db.py                SQLite, cash derived from the ledger
  polymarket.py        API client with backoff
  analytics.py         CLV, expected winners, capacity curve
  engine.py            the poll loop and heartbeat
  dashboard.py         FastAPI
tests/                 115 tests
NOTES.md               every API assumption, tagged VERIFIED or UNVERIFIED
```

Data lives in `data/copybot.sqlite3`. Logs rotate in `logs/copybot.log` (10 MB
× 5, UTC timestamps). To start the paper run over, stop the service and delete
the database — that resets cash, positions, and the seen-trade ledger together.

---

## Going live later

Not built, on purpose. When you want it:

1. Implement `LiveExecutor` in `src/copybot/executor.py` against the same three
   methods `PaperExecutor` already implements.
2. Change nothing in `strategy.py`. If you find yourself needing to, the seam
   has leaked.
3. Set `mode: live` — `build_executor()` is the only place that switches.

Credentials come from **environment variables only** — never `config.yaml`,
never source. The list lives in one place, `config.LIVE_ENV_VARS`:

```
POLYMARKET_PRIVATE_KEY
POLYMARKET_API_KEY
POLYMARKET_API_SECRET
POLYMARKET_API_PASSPHRASE
POLYMARKET_PROXY_ADDRESS
```

Before risking money, work through the pre-live checklist in NOTES.md — the
first item is verifying the fee formula against one real trade rather than
trusting the docs.
