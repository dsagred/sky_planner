"""Desktop-browser session bookkeeping shared by the server and launchers.

A browser refresh sends ``close`` followed by a fresh ``ping``.  The launcher
only stops the server if the close survives a short reconnection grace period,
but missing heartbeats alone must never terminate a working server.
The session files are private to the launcher.
"""

import math
from pathlib import Path
import sys
import time
from threading import Lock

_session_lock = Lock()


def record_activity(folder: Path, action: str, now: float | None = None,
                    page_id: str | None = None) -> None:
    """Record a ping or close; ignore closes from superseded browser pages.

    Refresh creates a new browser page, with a new page ID. An old page's
    delayed close beacon must not stop a reconnected session. The lock also
    prevents two request threads from interleaving writes and unlinking.
    """
    stamp = str(time.time() if now is None else now)
    with _session_lock:
        active_file = folder / "active-page"
        if action == "ping":
            # An in-flight heartbeat from a closed page can arrive after its
            # close beacon. Do not let that stale heartbeat revoke the close.
            if (page_id and (folder / "closed").exists()
                    and active_file.exists() and active_file.read_text() == page_id):
                return
            if page_id:
                active_file.write_text(page_id)
            (folder / "heartbeat").write_text(stamp)
            (folder / "closed").unlink(missing_ok=True)
        elif action == "close":
            # An old page can deliver its beacon after the new page is ready.
            if page_id and active_file.exists() and active_file.read_text() != page_id:
                return
            (folder / "closed").write_text(stamp)
        else:
            raise ValueError(f"Unknown session action: {action}")


def _timestamp(path: Path) -> float:
    # A missing/empty legacy 'closed' file must not break the launcher loop.
    try:
        value = float(path.read_text())
        return value if math.isfinite(value) else 0.0
    except (OSError, ValueError):
        return 0.0


def should_stop(folder: Path, grace: float = 12.0,
                heartbeat_timeout: float | None = None,
                now: float | None = None) -> bool:
    """Stop after an explicit tab-close beacon that survives the grace period.

    Delayed pings are normal for inactive/background browser tabs and sleeping
    computers, and do not prove that the tab is closed. The optional timeout
    is retained only for explicit opt-in use, not used by the launchers.
    """
    moment = time.time() if now is None else now
    last_ping = _timestamp(folder / "heartbeat")
    last_close = _timestamp(folder / "closed")
    closed = last_close > 0 and moment - last_close >= grace and last_ping <= last_close
    timed_out = (heartbeat_timeout is not None and last_ping > 0
                 and moment - last_ping > heartbeat_timeout)
    return closed or timed_out


if __name__ == "__main__":
    # Exit status is consumed by both POSIX launchers (0 means stop server).
    if len(sys.argv) != 3:
        sys.exit("Usage: python -m desktop_session SESSION_DIR CLOSE_GRACE_SECONDS")
    sys.exit(0 if should_stop(Path(sys.argv[1]), float(sys.argv[2])) else 1)
