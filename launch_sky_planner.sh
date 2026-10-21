#!/usr/bin/env bash
# Launch the local server and a separate browser instance; close both together.
set -Eeuo pipefail
APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP_HOST="${SKY_PLANNER_HOST:-127.0.0.1}"
APP_PORT="${SKY_PLANNER_PORT:-5511}"
export SKY_PLANNER_HOST APP_PORT SKY_PLANNER_PORT="$APP_PORT"
URL="http://${APP_HOST}:${APP_PORT}/"
LOG_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/sky_planner"
LOG_FILE="$LOG_DIR/server.log"
PID_FILE="$LOG_DIR/server.pid"
mkdir -p "$LOG_DIR"
APP_PID=""
BROWSER_PROFILE=""
SESSION_DIR=""
BROWSER_PID=""

error() {
  local message="$1"
  printf '%b\nLog: %s\n' "$message" "$LOG_FILE" >&2
  if command -v zenity >/dev/null 2>&1; then
    zenity --error --title='Sky Observation Planner' --text="$message\n\nLog: $LOG_FILE" --width=520 >/dev/null 2>&1 || true
  elif command -v notify-send >/dev/null 2>&1; then
    notify-send 'Sky Planner' "$message" || true
  fi
}

cleanup() {
  trap - EXIT HUP INT TERM
  if [[ -n "$APP_PID" ]]; then
    kill "$APP_PID" 2>/dev/null || true
    wait "$APP_PID" 2>/dev/null || true
    if [[ -f "$PID_FILE" && "$(cat "$PID_FILE")" == "$APP_PID" ]]; then
      rm -f "$PID_FILE"
    fi
  fi
  [[ -z "$BROWSER_PROFILE" ]] || rm -rf -- "$BROWSER_PROFILE"
  [[ -z "$SESSION_DIR" ]] || rm -rf -- "$SESSION_DIR"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP

load_conda() {
  local root conda_exe
  if [[ -n "${CONDA_EXE:-}" && -x "$CONDA_EXE" ]]; then
    root="$(dirname "$(dirname "$CONDA_EXE")")"
    if [[ -f "$root/etc/profile.d/conda.sh" ]]; then
      source "$root/etc/profile.d/conda.sh"
      return
    fi
  fi
  for root in "$HOME/miniforge3" "$HOME/miniconda3" "$HOME/anaconda3" "$HOME/mambaforge" "$HOME/opt/miniconda3" "$HOME/opt/anaconda3" "/opt/conda"; do
    if [[ -f "$root/etc/profile.d/conda.sh" ]]; then
      source "$root/etc/profile.d/conda.sh"
      return
    fi
  done
  if command -v conda >/dev/null 2>&1; then
    eval "$(conda shell.bash hook)"
    return
  fi
  conda_exe="$(bash -lc 'command -v conda 2>/dev/null || true' | tail -n 1)"
  if [[ -n "$conda_exe" && -x "$conda_exe" ]]; then
    eval "$("$conda_exe" shell.bash hook)"
    return
  fi
  return 1
}

if ! load_conda || ! conda activate base; then
  error 'Could not activate Conda base. Check your Conda installation.'
  exit 1
fi
cd "$APP_DIR"

server_ready() {
  python - "$URL" <<'PYREADY' >/dev/null 2>&1
import json, sys, urllib.request
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
with opener.open(sys.argv[1] + 'api/locations', timeout=1) as r:
    assert isinstance(json.load(r), list)
PYREADY
}

# Already-running instance? Open its tab, but never terminate a server we do not own.
if server_ready; then
  xdg-open "$URL" >/dev/null 2>&1 || true
  exit 0
fi
# Do not preflight the port with a temporary bind(). A recently closed
# TCP connection can make that check fail even with no LISTEN socket,
# especially when the probe itself does not enable SO_REUSEADDR.
# Let Waitress bind the actual listening socket instead.

# Private per-launch files are used by the browser to signal a closed window.
SESSION_DIR="$(mktemp -d "$LOG_DIR/session.XXXXXXXX")"
export SKY_PLANNER_SESSION_DIR="$SESSION_DIR"
export SKY_PLANNER_SESSION_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(24))')"
SESSION_URL="${URL}?sky_desktop=${SKY_PLANNER_SESSION_TOKEN}"
printf '\n=== Sky Planner desktop session: %s ===\n' "$(date -Is)" >> "$LOG_FILE"
python -u app.py >> "$LOG_FILE" 2>&1 </dev/null &
APP_PID=$!
printf '%s\n' "$APP_PID" > "$PID_FILE"

ready=0
for ((i=0; i<60; i++)); do
  if server_ready; then ready=1; break; fi
  if ! kill -0 "$APP_PID" 2>/dev/null; then break; fi
  sleep 0.5
done
if [[ "$ready" != 1 ]]; then
  error "Sky Planner could not start (possibly a real port conflict or missing dependency). Last log entries:\n$(tail -n 20 "$LOG_FILE")"
  exit 1
fi

# Open with GNOME's configured default browser, not a hard-coded Firefox instance.
# The browser may reuse an existing process; webpage session heartbeat/close tracks it.
if ! xdg-open "$SESSION_URL" >>"$LOG_FILE" 2>&1; then
  error "Could not open the default browser. Check 'xdg-settings get default-web-browser'."
  exit 1
fi

# Unlike waiting for the browser executable, the page heartbeat works even if
# a Snap wrapper hands off the browser process. pagehide sends an immediate
# close event; if that is missed, a heartbeat timeout is the fallback.
seen=0
missing_since=0
for ((i=0; i<90; i++)); do
  if [[ -f "$SESSION_DIR/closed" ]]; then exit 0; fi
  if [[ -f "$SESSION_DIR/heartbeat" ]]; then seen=1; break; fi
  if ! kill -0 "$APP_PID" 2>/dev/null; then
    error 'The Sky Planner server exited unexpectedly.'; exit 1
  fi
  sleep 1
done
if [[ "$seen" != 1 ]]; then
  error "The browser did not connect to the desktop session. See $LOG_FILE"
  exit 1
fi
while kill -0 "$APP_PID" 2>/dev/null; do
  if [[ -f "$SESSION_DIR/closed" ]]; then break; fi
  # To avoid stopping a minimized/background-throttled tab, allow 90 seconds.
  last="$(cat "$SESSION_DIR/heartbeat" 2>/dev/null || echo 0)"
  if python - "$last" <<'PYSTALE'
import sys, time
sys.exit(0 if time.time() - float(sys.argv[1]) > 90 else 1)
PYSTALE
  then break; fi
  sleep 2
done
