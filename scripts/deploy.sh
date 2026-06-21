#!/usr/bin/env bash
#
# Deploy the drydown app to the AppDaemon add-on on Home Assistant.
#
# It validates (ruff + pytest), copies the Python modules over SSH, restarts
# the AppDaemon add-on for a clean reload, and tails the log to confirm the
# startup run succeeded. The live drydown.yaml is NEVER overwritten — it holds
# your real sensors/credentials and is the source of truth (use --with-config
# to push the repo's drydown.yaml on purpose).
#
# No backups are left on the host: git is the history. Roll back with
# `git checkout <ref> -- apps/drydown && scripts/deploy.sh`.
#
# Config via env vars (defaults match the `ha` skill):
#   HA_HOST     homeassistant.local
#   HA_USER     root
#   HA_SSH_KEY  ~/.ssh/ha
#   HA_ADDON    a0d7b954_appdaemon
#   HA_APPS_DIR /addon_configs/a0d7b954_appdaemon/apps/drydown
#
# Usage:
#   scripts/deploy.sh                 # validate, deploy, restart, verify
#   scripts/deploy.sh --skip-tests    # skip ruff/pytest (faster, riskier)
#   scripts/deploy.sh --with-config   # also push drydown.yaml (overwrites live!)
#   scripts/deploy.sh --no-restart    # rely on AppDaemon's file-watch reload

set -euo pipefail

HA_HOST="${HA_HOST:-homeassistant.local}"
HA_USER="${HA_USER:-root}"
HA_SSH_KEY="${HA_SSH_KEY:-$HOME/.ssh/ha}"
HA_ADDON="${HA_ADDON:-a0d7b954_appdaemon}"
HA_APPS_DIR="${HA_APPS_DIR:-/addon_configs/a0d7b954_appdaemon/apps/drydown}"

SKIP_TESTS=0
WITH_CONFIG=0
RESTART=1
for arg in "$@"; do
  case "$arg" in
    --skip-tests)  SKIP_TESTS=1 ;;
    --with-config) WITH_CONFIG=1 ;;
    --no-restart)  RESTART=0 ;;
    -h|--help)     sed -n '2,33p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

# Resolve repo root (this script lives in scripts/).
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

SRC_DIR="apps/drydown"
MODULES=(drydown.py calibration.py influx.py publish.py)

SSH=(ssh -i "$HA_SSH_KEY" "$HA_USER@$HA_HOST")
SCP=(scp -i "$HA_SSH_KEY")

say() { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }

# ---- 1. Validate -----------------------------------------------------------
if [[ "$SKIP_TESTS" -eq 0 ]]; then
  PY=".venv/bin/python"; RUFF=".venv/bin/ruff"
  [[ -x "$PY" ]]   || PY="python3"
  [[ -x "$RUFF" ]] || RUFF="ruff"
  say "Linting (ruff)"
  "$RUFF" check .
  say "Testing (pytest)"
  "$PY" -m pytest -q
else
  say "Skipping validation (--skip-tests)"
fi

# ---- 2. Deploy -------------------------------------------------------------
say "Deploying to $HA_USER@$HA_HOST:$HA_APPS_DIR"
"${SSH[@]}" "mkdir -p '$HA_APPS_DIR'"

files=()
for m in "${MODULES[@]}"; do files+=("$SRC_DIR/$m"); done
if [[ "$WITH_CONFIG" -eq 1 ]]; then
  echo "  (also pushing drydown.yaml — overwriting live config)"
  files+=("$SRC_DIR/drydown.yaml")
fi
"${SCP[@]}" "${files[@]}" "$HA_USER@$HA_HOST:$HA_APPS_DIR/"
echo "  copied: ${MODULES[*]}$([[ $WITH_CONFIG -eq 1 ]] && echo ' drydown.yaml')"

# ---- 3. Reload -------------------------------------------------------------
if [[ "$RESTART" -eq 1 ]]; then
  say "Restarting AppDaemon add-on ($HA_ADDON)"
  "${SSH[@]}" "ha apps restart '$HA_ADDON'"
else
  say "Skipping restart (--no-restart); AppDaemon will reload on file change"
fi

# ---- 4. Verify -------------------------------------------------------------
# Poll for a NEW "run complete" line rather than sleeping a fixed time: a
# restart (~40s) + AppDaemon's file-watch reload + the 30s startup delay make a
# fixed wait unreliable. Bail on a traceback.
fetch_log() { "${SSH[@]}" "ha apps logs '$HA_ADDON' --lines 400" 2>/dev/null; }

say "Waiting for a fresh drydown run to complete (up to ~150s)…"
baseline_completes=$(fetch_log | grep -c 'drydown run complete' || true)
deadline=$(( $(date +%s) + 150 ))
ok=0
while [[ $(date +%s) -lt $deadline ]]; do
  sleep 10
  log=$(fetch_log)
  if printf '%s\n' "$log" | grep -iqE 'traceback|error computing|run failed'; then
    echo "  detected an error in the log:"
    printf '%s\n' "$log" | grep -iE 'drydown|traceback|error|exception' | tail -15
    echo "  deploy completed but the app reported errors — investigate above." >&2
    exit 1
  fi
  now=$(printf '%s\n' "$log" | grep -c 'drydown run complete' || true)
  if [[ "$now" -gt "$baseline_completes" ]]; then ok=1; break; fi
  printf '  …still waiting (%ds left)\n' "$(( deadline - $(date +%s) ))"
done

say "Latest drydown run"
fetch_log | grep -iE 'drydown (plant_|run )' | tail -12
if [[ "$ok" -eq 1 ]]; then
  echo "  ✅ fresh run completed cleanly."
else
  echo "  ⚠️  no fresh 'run complete' seen within the timeout — check manually:" >&2
  echo "     ssh -i $HA_SSH_KEY $HA_USER@$HA_HOST \"ha apps logs $HA_ADDON --lines 100\"" >&2
fi

say "Done. Reminder: the live drydown.yaml was left untouched$([[ $WITH_CONFIG -eq 1 ]] && echo ' (except --with-config)')."
