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
.venv/bin/python -m pytest tests/ -q          # all should pass, none skipped
```

Sanity-check the config and the API before running for real:

```bash
PYTHONPATH=src .venv/bin/python -m copybot.main status
grep '^entry_mode' config.yaml               # should say "limit"
```

On a fresh install `status` reports $150.00 cash, no open positions and
`last heartbeat: never`. That is correct: the database is created empty on
first use and the starting capital is a config number, not a deposit.

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

### Or run it in a `screen` session instead

Useful if you'd rather watch the bot work than read log files. Two things
systemd gives you that `screen` does not: surviving a reboot, and restarting
after a crash. `scripts/run-forever.sh` restores the second one.

```bash
sudo apt install -y screen
cd ~/copybot

screen -S copybot -dm ./scripts/run-forever.sh          # the bot
screen -S dash -dm bash -c 'PYTHONPATH=src .venv/bin/python -m copybot.main dashboard'
```

| Task | Command |
|---|---|
| List sessions | `screen -ls` |
| Watch the bot | `screen -r copybot` |
| Leave it running | press `Ctrl-A` then `D` |
| Stop it | `screen -S copybot -X quit` |
| Peek without attaching | `tail -f ~/copybot/logs/copybot.log` |

`run-forever.sh` restarts a crashed bot, but not a rejected config (exit 2) or
a database another bot already holds (exit 3). Neither heals by waiting.

To survive reboots, add a crontab entry with `crontab -e`:

```
@reboot cd /home/ubuntu/copybot && /usr/bin/screen -S copybot -dm ./scripts/run-forever.sh
```

Restarts are recorded in `logs/restarts.log`. That file should stay empty — if
it's filling up, something is crashing repeatedly and the "30 consecutive days
without a crash or stall" go-live condition is not being met.

### Run it by hand instead

```bash
cd ~/copybot
PYTHONPATH=src .venv/bin/python -m copybot.main run          # the bot
PYTHONPATH=src .venv/bin/python -m copybot.main dashboard    # the dashboard
```

---

## Wiping the server and starting over

For when you want nothing of the old install left. Every destructive step is
preceded by a step that *shows you what it will destroy* — read that output
before running the next command.

### 1. Stop everything, and make sure nothing restarts it

The systemd units matter even if you plan to use `screen`: left enabled, they
start a second bot at the next reboot, and two bots on one database is the
failure this project already had once.

```bash
sudo systemctl stop copybot copybot-dashboard 2>/dev/null
sudo systemctl disable copybot copybot-dashboard 2>/dev/null
sudo rm -f /etc/systemd/system/copybot.service /etc/systemd/system/copybot-dashboard.service
sudo systemctl daemon-reload

screen -ls                                    # see what is running
screen -S copybot -X quit 2>/dev/null
screen -S dash -X quit 2>/dev/null
pkill -f "copybot.main" 2>/dev/null

crontab -l 2>/dev/null | grep copybot         # any @reboot line?
crontab -l 2>/dev/null | grep -v copybot | crontab -   # removes only those lines
```

Confirm the server is quiet. **Both of these must print `0`:**

```bash
ps aux | grep -c "[c]opybot.main"
screen -ls 2>/dev/null | grep -c copybot
```

### 2. Look at what you are about to delete

```bash
ls -la ~/copybot
sudo find / -name "copybot*.sqlite3*" -not -path "*/proc/*" 2>/dev/null
```

The second command finds databases anywhere on the box, including ones outside
`~/copybot`. **If it lists a file you want to keep, copy it somewhere else
now** — the next step does not ask twice.

### 3. Delete it

**`cd ~` first.** If your shell is sitting inside `~/copybot` when you delete
it, the shell is left with a working directory that no longer exists, and
everything after it fails with `getcwd() failed: No such file or directory` --
including the `git clone`, which cannot run from a directory that is gone. The
recovery is just `cd ~`, but it is easier to not step in it.

```bash
cd ~
rm -rf ~/copybot
ls ~/copybot 2>&1                             # "No such file or directory"
pwd                                           # /home/ubuntu -- a real directory
```

### 4. Install clean

```bash
cd ~                                          # never clone from a deleted cwd
sudo apt update && sudo apt install -y python3-venv git screen
git clone https://github.com/Smileifyouliked/copybot.git ~/copybot
cd ~/copybot

python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
mkdir -p data logs
chmod +x scripts/run-forever.sh

.venv/bin/python -m pytest -q                 # must pass, nothing skipped
grep '^entry_mode' config.yaml                # must say "limit"
grep '^mode' config.yaml                      # must say "paper"
PYTHONPATH=src .venv/bin/python -m copybot.main status
```

`status` on a clean install reports `$150.00` cash, no open positions and
`last heartbeat: never`. That is what an empty ledger looks like, not a fault.

### 5. Start it in a detached `screen`

```bash
cd ~/copybot
screen -S copybot -dm ./scripts/run-forever.sh
screen -S dash -dm bash -c 'PYTHONPATH=src .venv/bin/python -m copybot.main dashboard'
screen -ls                                    # both should be listed as Detached
```

Give it a minute, then check it is actually working:

```bash
sleep 60
PYTHONPATH=src .venv/bin/python -m copybot.main status     # heartbeat seconds ago
tail -20 logs/copybot.log
```

`last heartbeat: never` after a minute means it is not running — attach with
`screen -r copybot` and read the error.

### 6. Survive a reboot

`screen` does not. Add one crontab line with `crontab -e`:

```
@reboot cd /home/ubuntu/copybot && /usr/bin/screen -S copybot -dm ./scripts/run-forever.sh
```

Use your real path if you did not clone to `/home/ubuntu/copybot`.

### Living with it

| Task | Command |
|---|---|
| Read the numbers | `PYTHONPATH=src .venv/bin/python -m copybot.main report` |
| Watch them update | `PYTHONPATH=src .venv/bin/python -m copybot.main report --watch` |
| Attach to the bot | `screen -r copybot` |
| Detach again | `Ctrl-A` then `D` — **not** `Ctrl-C`, which stops the bot |
| Tail the log instead | `tail -f ~/copybot/logs/copybot.log` |
| Stop it | `screen -S copybot -X quit` |
| Did it crash? | `cat logs/restarts.log` — should stay empty |

`run-forever.sh` restarts the bot if it crashes, but deliberately does **not**
restart it when the config is rejected or when another bot already holds the
database. Neither of those heals by waiting, and looping on them would bury the
one line telling you what to fix.

---

## Starting a fresh run without losing the old one

Run 1 (market orders) is the only baseline run 2 (limit orders) can be judged
against, so **nothing here deletes it.** `archive` copies; moving the live file
aside is a `mv`, never an `rm`.

**First check there is anything to preserve.** If the previous install was
removed, there is no run 1 to archive and this whole section is skippable —
go to [Install on Ubuntu (EC2)](#install-on-ubuntu-ec2) instead:

```bash
sudo find / -name "copybot*.sqlite3*" -not -path "*/proc/*" 2>/dev/null
```

Nothing printed means run 1 is gone. That costs the market-vs-limit
comparison, and nothing else: the 1.395 break-even comes from the wallet's own
public history, not from our database, so the kill condition still works on run
2's own numbers. The absolute question — *is our entry price under 1.395x
his* — never needed run 1 to answer.

If it did print a database, copy-paste this whole block on the server, in this
order:

```bash
cd ~/copybot

# 1. STOP the bot first. Archiving a database a bot is still writing to is
#    safe (the backup API handles it), but starting run 2 while run 1 is still
#    polling would put two bots on one file -- the exact thing we just fixed.
sudo systemctl stop copybot copybot-dashboard
screen -ls                      # anything still listed? kill it before step 5
ps aux | grep -c "[c]opybot.main run"    # must print 0

# 2. CHECK run 1 before you touch it. Prints whether two bots ever wrote this
#    file, when, and whether anything was actually damaged.
PYTHONPATH=src .venv/bin/python -m copybot.main doctor | tee run1-health.txt

# 3. ARCHIVE. Copies to data/copybot-<timestamp>.sqlite3 and leaves the
#    original exactly where it is. Refuses to overwrite an existing archive.
PYTHONPATH=src .venv/bin/python -m copybot.main archive

# 4. MOVE the live file aside so run 2 starts on an empty ledger. This is the
#    only step that touches run 1, and it is a move, not a delete. The -n flag
#    makes mv refuse rather than clobber if the name is somehow taken.
ls -la data/                                   # confirm the archive exists FIRST
mv -n data/copybot.sqlite3 data/run1-market.sqlite3
mv -n data/copybot.sqlite3-wal data/run1-market.sqlite3-wal 2>/dev/null || true
mv -n data/copybot.sqlite3-shm data/run1-market.sqlite3-shm 2>/dev/null || true
ls -la data/                                   # copybot.sqlite3 should be GONE

# 5. UPDATE the code.
git fetch origin main && git checkout main && git pull
.venv/bin/pip install -q -r requirements.txt
.venv/bin/python -m pytest -q                  # must say "passed", no failures
grep '^entry_mode' config.yaml                 # must say "limit"

# 6. START. The instance lock means this refuses to run if anything from
#    step 1 is somehow still alive, rather than quietly sharing the file.
sudo systemctl start copybot copybot-dashboard
sleep 30
PYTHONPATH=src .venv/bin/python -m copybot.main status
```

Then watch it:

```bash
PYTHONPATH=src .venv/bin/python -m copybot.main report --watch
```

And once run 2 has a few days of data, compare the two runs on entry quality:

```bash
PYTHONPATH=src .venv/bin/python -m copybot.main compare \
    data/run1-market.sqlite3 data/copybot.sqlite3
```

### If something goes wrong

Run 1 is in three places after this: the archive, `run1-market.sqlite3`, and
whatever backups you keep. To go back to it, stop the bot and
`mv data/run1-market.sqlite3 data/copybot.sqlite3`.

`systemctl start` failing with `refusing to start: another copybot already
holds ...` means step 1 did not finish — something is still running. Find it
with `ps aux | grep [c]opybot` and stop it; do not delete the `.lock` file,
which does nothing (the kernel owns the lock, not the file).

---

## Reading it without a browser

If you only work on the server itself — through the AWS browser console, say —
the SSH tunnel below is not available to you, since it needs an SSH client and
the key file on your own machine. Same numbers, straight into the terminal:

```bash
cd ~/copybot && PYTHONPATH=src .venv/bin/python -m copybot.main report
```

Everything the web dashboard shows, in the same order, as text. Add `--watch`
to have it redraw in place like `top`:

```bash
PYTHONPATH=src .venv/bin/python -m copybot.main report --watch
```

`Ctrl-C` stops watching; it does not touch the bot.

## Sending it to someone (or an AI) for analysis

```bash
cd ~/copybot && PYTHONPATH=src .venv/bin/python -m copybot.main export
```

Prints a single self-contained bundle and saves it to `analysis-bundle.md`.
Roughly 20 KB — small enough to paste into a chat.

Do **not** send the raw log. It is mostly repeated `SKIP` lines, grows to
megabytes, and the numbers that matter are aggregates the log never states.
The bundle contains the config in force, the full dashboard view, every
aggregate (capacity ladder, depth cost distribution, entry bands, exit paths,
CLV by horizon, fees, slippage, every skip reason), per-position and
per-execution tables, and from the log only what carries signal: level counts,
warnings and errors with repeats collapsed, and the last few lines.

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
