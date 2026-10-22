#!/usr/bin/env bash
# macOS launcher: start Sky Planner and open its browser session.
# A refresh reconnects; tab/window close stops the owned server after a grace period.
set -Eeuo pipefail
umask 077
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP_HOST="${SKY_PLANNER_HOST:-127.0.0.1}"
APP_PORT="${SKY_PLANNER_PORT:-5511}"
export SKY_PLANNER_HOST APP_PORT SKY_PLANNER_PORT="$APP_PORT"
BROWSE_HOST="$APP_HOST"
if [[ "$BROWSE_HOST" == "0.0.0.0" || "$BROWSE_HOST" == "::" ]]; then BROWSE_HOST="127.0.0.1"; fi
URL="http://${BROWSE_HOST}:${APP_PORT}/"
LOG_DIR="$HOME/Library/Logs/SkyPlanner"
LOG_FILE="$LOG_DIR/server.log"
PID_FILE="$LOG_DIR/server.pid"
ACTIVE_SESSION_FILE="$LOG_DIR/active-session"
mkdir -p "$LOG_DIR"
APP_PID=""
BROWSER_PROFILE=""
SESSION_DIR=""
BROWSER_PID=""

error() {
  local message="$1"
  printf '%b\nLog: %s\n' "$message" "$LOG_FILE" >&2
  # Works when launched from Finder; a notification is optional.
  if command -v osascript >/dev/null 2>&1; then
    /usr/bin/osascript -e 'on run argv' -e 'display alert "Sky Observation Planner" message (item 1 of argv) as critical' -e 'end run' "$message" >/dev/null 2>&1 || true
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
    # Only remove the published session when it belongs to this launcher.
    if [[ -f "$ACTIVE_SESSION_FILE" ]]; then
      read -r active_pid active_token < "$ACTIVE_SESSION_FILE" || true
      if [[ "${active_pid:-}" == "$APP_PID" ]]; then rm -f "$ACTIVE_SESSION_FILE"; fi
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
  for root in "$HOME/miniforge3" "$HOME/miniconda3" "$HOME/anaconda3" "$HOME/mambaforge" "$HOME/opt/miniconda3" "$HOME/opt/anaconda3" "/opt/homebrew/Caskroom/miniconda/base" "/opt/homebrew/Caskroom/miniforge/base" "/opt/miniconda3" "$HOME/miniconda3" "$HOME/anaconda3" "$HOME/mambaforge" "$HOME/opt/miniconda3" "$HOME/opt/anaconda3" "/opt/conda"; do
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

# Keep the proven Conda startup path unchanged when Conda is installed.
# Only fall back when Conda itself cannot be found; do not silently switch
# interpreters when an installed Conda environment is broken.
if load_conda; then
  if ! conda activate base; then
    error 'Conda was found, but its base environment could not be activated.'
    exit 1
  fi
else
  # No Conda: use a project virtual environment, then installed Python.
  # Validate candidates before using them so a Python without dependencies
  # does not result in an unexplained failed browser/server launch.
  selected_python=''
  for candidate in "$APP_DIR/.venv/bin/python" "$APP_DIR/venv/bin/python" python3 python; do
    if [[ "$candidate" == */* && ! -x "$candidate" ]]; then continue; fi
    if ! command -v "$candidate" >/dev/null 2>&1; then continue; fi
    if "$candidate" - <<'PYCHECK' >/dev/null 2>&1
import importlib.util
import sys
assert sys.version_info >= (3, 10)
for name in ('flask', 'astropy', 'astroquery', 'numpy', 'pandas',
             'matplotlib', 'plotly', 'markdown', 'waitress'):
    assert importlib.util.find_spec(name) is not None, name
PYCHECK
    then
      selected_python="$candidate"
      break
    fi
  done
  if [[ -z "$selected_python" ]]; then
    error "No usable Python 3.10+ environment found. Install dependencies in a project virtual environment:
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt"
    exit 1
  fi
  # Preserve every existing 'python' invocation in the working launcher.
  # A Bash function affects this launcher only, including its subprocesses.
  python() { "$selected_python" "$@"; }
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
  # A quick relaunch must reconnect the tracked browser session, rather than
  # opening an untracked tab which the original launcher may soon shut down.
  reopen_url="$URL"
  if [[ -f "$ACTIVE_SESSION_FILE" && -f "$PID_FILE" ]]; then
    read -r active_pid active_token < "$ACTIVE_SESSION_FILE" || true
    if [[ "${active_pid:-}" == "$(cat "$PID_FILE")" && -n "${active_token:-}" ]] && kill -0 "$active_pid" 2>/dev/null; then
      reopen_url="${URL}?sky_desktop=${active_token}"
    fi
  fi
  /usr/bin/open "$reopen_url" >/dev/null 2>&1 || true
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
printf '\n=== Sky Planner desktop session: %s ===\n' "$(date "+%Y-%m-%dT%H:%M:%S%z")" >> "$LOG_FILE"
python -u app.py >> "$LOG_FILE" 2>&1 </dev/null &
APP_PID=$!
printf '%s\n' "$APP_PID" > "$PID_FILE"
printf '%s %s\n' "$APP_PID" "$SKY_PLANNER_SESSION_TOKEN" > "$ACTIVE_SESSION_FILE"

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

# Open with macOS's configured default browser.
# The browser may reuse an existing process; webpage session heartbeat/close tracks it.
if ! /usr/bin/open "$SESSION_URL" >>"$LOG_FILE" 2>&1; then
  error "Could not open the default browser. Check macOS default browser settings."
  exit 1
fi

# A browser pagehide event fires both on refresh and on actual tab closure.
# Allow the reloaded page to reconnect before honoring a close notification.
# A missing beacon still falls back to the heartbeat timeout.
CLOSE_GRACE_SECONDS=2
seen=0
missing_since=0
for ((i=0; i<90; i++)); do
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
  # The close beacon alone is ambiguous: refresh also triggers pagehide.
  # A heartbeat from the reloaded page clears the pending close in app.py.
  # Keep a grace period for slow refreshes and browser startup.
  last="$(cat "$SESSION_DIR/heartbeat" 2>/dev/null || echo 0)"
  closed="$(cat "$SESSION_DIR/closed" 2>/dev/null || echo 0)"
  if python - "$last" "$closed" "$CLOSE_GRACE_SECONDS" <<'PYSESSION'
import sys, time
last, closed, grace = map(float, sys.argv[1:])
now = time.time()
# Respect a close only after the reconnection grace period; if a heartbeat
# arrived after the close request, it represents a still-open page.
closed_expired = closed > 0 and now - closed >= grace and last <= closed
heartbeat_expired = now - last > 90
sys.exit(0 if closed_expired or heartbeat_expired else 1)
PYSESSION
  then break; fi
  sleep 0.25
done
