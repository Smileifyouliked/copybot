#!/usr/bin/env bash
# Keep the bot running inside a screen/tmux session.
#
# systemd already does this (Restart=always, RestartSec=10). This script exists
# for running under `screen` instead, where nothing restarts a crashed process.
# Without it, a crash at 3am means the bot is simply dead until you notice --
# and on a fixed-length experiment, days you didn't collect are days you cannot
# get back.
#
#   Usage:  ./scripts/run-forever.sh [config.yaml]
#
# Ctrl-C stops it properly rather than triggering a restart.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${1:-$HERE/config.yaml}"
PYTHON="$HERE/.venv/bin/python"
RESTART_DELAY=10

cd "$HERE" || exit 1
export PYTHONPATH="$HERE/src"

child=""

# The child runs in the background and we `wait` on it, rather than running it
# in the foreground. bash defers a trap until the current foreground command
# returns, so a foreground child would leave `kill <wrapper-pid>` hanging until
# the bot happened to exit on its own. `wait` is interruptible, so the trap
# fires immediately and can pass the signal on.
stop() {
    echo ""
    echo "[run-forever] stop requested -- shutting down, not restarting."
    if [ -n "$child" ]; then
        kill -TERM "$child" 2>/dev/null
        wait "$child" 2>/dev/null
    fi
    exit 0
}
trap stop INT TERM

if [ ! -x "$PYTHON" ]; then
    echo "[run-forever] no virtualenv at $PYTHON" >&2
    echo "[run-forever] run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi
if [ ! -f "$CONFIG" ]; then
    echo "[run-forever] config not found: $CONFIG" >&2
    exit 1
fi

restarts=0
echo "[run-forever] starting copybot with $CONFIG"
echo "[run-forever] detach with Ctrl-A then D (screen) -- the bot keeps running"

while true; do
    "$PYTHON" -m copybot.main run -c "$CONFIG" &
    child=$!
    wait "$child"
    code=$?

    # Some exits are decisions, not crashes. Restarting on those turns a clear
    # one-line refusal into a 10-second loop that fills restarts.log with
    # thousands of entries and hides the real message -- while the condition
    # that caused it (a bad config, another bot holding the database) sits
    # there unfixed, because nothing about waiting 10 seconds fixes either one.
    case $code in
        0)
            echo "[run-forever] copybot exited cleanly. Not restarting."
            exit 0
            ;;
        2)
            echo "[run-forever] config was rejected. Fix config.yaml, then start" \
                 "this again. Not restarting -- a bad config does not heal." >&2
            exit 2
            ;;
        3)
            echo "[run-forever] another copybot is already running against this" \
                 "database. Stop it first (screen -ls, ps aux | grep copybot)." \
                 "Not restarting -- waiting cannot win a lock someone else holds." >&2
            exit 3
            ;;
    esac

    restarts=$((restarts + 1))
    echo "[run-forever] copybot exited with code $code -- restart #$restarts in ${RESTART_DELAY}s"
    echo "[run-forever] $(date -u '+%Y-%m-%d %H:%M:%S UTC') restart #$restarts (exit $code)" \
        >> "$HERE/logs/restarts.log"
    sleep "$RESTART_DELAY"
done
