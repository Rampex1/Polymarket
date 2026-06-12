#!/usr/bin/env bash
# Zero-to-running deployment. Run from anywhere inside the repo:
#
#     bash scripts/setup_vm.sh
#
# Idempotent — safe to re-run after every git push; it restarts the tmux
# sessions cleanly. Goes from a bare checkout + .env to all three
# processes running:
#
#   paper   — PROFILE=experimental python main.py
#   prod    — PROFILE=prod         python main.py   (REAL MONEY)
#   archive — python -m discovery.archive --loop --every 3600
set -euo pipefail

cd "$(dirname "$0")/.."

echo "==> Code"
git pull --ff-only

echo "==> Python env"
[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate
pip install -q -r requirements.txt

echo "==> Data layout"
mkdir -p data
if [ -f positions.db ] && [ ! -f data/positions.db ]; then
    mv positions.db* data/
fi

echo "==> Env hygiene"
# Obsolete since webhooks moved to config/webhooks.toml — a leftover
# .env.experimental shadows .env and its stale webhook overrides the registry.
rm -f .env.experimental
if [ ! -f .env ]; then
    echo "ERROR: .env missing — create it with the five POLY_* secrets (see .env.example)." >&2
    exit 1
fi
for key in POLY_PRIVATE_KEY POLY_FUNDER_ADDRESS POLY_API_KEY POLY_API_SECRET POLY_API_PASSPHRASE; do
    grep -q "^${key}=" .env || { echo "ERROR: ${key} missing from .env." >&2; exit 1; }
done

echo "==> Validating profiles (fail fast before touching tmux)"
PROFILE=experimental python -m bot.params --effective > /dev/null
PROFILE=prod         python -m bot.params --effective > /dev/null

echo "==> Restarting tmux sessions"
start_session() {
    local name=$1 cmd=$2
    tmux kill-session -t "$name" 2>/dev/null || true
    tmux new-session -d -s "$name" \
        "cd $PWD && source .venv/bin/activate && $cmd"
    # Keep the pane (and its scrollback) around if the process crashes,
    # so `tmux attach -t <name>` shows the traceback instead of nothing.
    tmux set-option -t "$name" remain-on-exit on
}
start_session paper   "PROFILE=experimental python main.py"
start_session prod    "PROFILE=prod python main.py"
start_session archive "python -m discovery.archive --loop --every 3600"

echo "==> Verifying (give workers a moment to boot)"
sleep 8
fail=0
for s in paper prod archive; do
    dead=$(tmux list-panes -t "$s" -F '#{pane_dead}' 2>/dev/null || echo 1)
    if [ "$dead" = "0" ]; then
        echo "  [$s] running"
    else
        fail=1
        echo "  [$s] CRASHED — last output:"
        tmux capture-pane -p -t "$s" 2>/dev/null | grep -v '^$' | tail -5 | sed 's/^/      /'
    fi
done

echo
tmux ls
[ "$fail" = "0" ] && echo "All sessions up. Attach with: tmux attach -t paper|prod|archive (Ctrl-b d to detach)."
exit "$fail"
