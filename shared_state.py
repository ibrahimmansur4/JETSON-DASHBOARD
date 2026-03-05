# =============================================================
#  shared_state.py  —  File-based cross-session state manager
#
#  Provides shared state across multiple Streamlit sessions via
#  a JSON file with file locking.  DataFrames are stored as
#  Parquet for performance.
#
#  Session tracking: each browser tab registers with a unique
#  session_id and sends periodic heartbeats.  Stale sessions
#  (no heartbeat for 30 s) are reaped automatically.
#
#  Primary fetcher election: exactly one session performs SFTP
#  reads and writes the result for all others to consume.
# =============================================================

import json
import os
import time
import platform
import tempfile
import threading
from contextlib import contextmanager

import pandas as pd

import config as cfg

# ── Paths ────────────────────────────────────────────────────

_DIR        = os.path.join(os.path.dirname(__file__), cfg.STATE_DIR)
_STATE_FILE = os.path.join(_DIR, "state.json")
_LOCK_FILE  = os.path.join(_DIR, "state.lock")
_DATA_FILE  = os.path.join(_DIR, "data.parquet")

os.makedirs(_DIR, exist_ok=True)


# ── File locking ─────────────────────────────────────────────

_LOCK_LOCAL = threading.local()


@contextmanager
def _file_lock(timeout=5):
    """
    Cross-platform file lock.
    Windows: msvcrt.locking  |  POSIX: fcntl.flock
    """
    depth = getattr(_LOCK_LOCAL, "depth", 0)
    if depth > 0:
        _LOCK_LOCAL.depth = depth + 1
        try:
            yield
        finally:
            _LOCK_LOCAL.depth -= 1
        return

    fd = os.open(_LOCK_FILE, os.O_CREAT | os.O_RDWR)
    try:
        deadline = time.time() + timeout
        locked = False
        while time.time() < deadline:
            try:
                if platform.system() == "Windows":
                    import msvcrt
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except (OSError, IOError):
                time.sleep(0.05)
        if not locked:
            raise TimeoutError("Could not acquire shared-state lock")
        _LOCK_LOCAL.depth = 1
        yield
    finally:
        try:
            if platform.system() == "Windows":
                import msvcrt
                try:
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            _LOCK_LOCAL.depth = 0
            os.close(fd)


# ── State I/O ────────────────────────────────────────────────

_DEFAULT_STATE = {
    "sessions":        {},       # {session_id: heartbeat_timestamp}
    "fetcher_id":      None,     # session_id of the primary fetcher
    "fetcher_heartbeat": 0.0,
    "connected":       False,
    "ssh_host":        "",
    "ssh_port":        22,
    "ssh_username":    "",
    "ssh_password":    "",
    "remote_csv_path": cfg.REMOTE_CSV_PATH,
    "timestamp_column": cfg.TIMESTAMP_COLUMN,
    "max_rows":        cfg.MAX_ROWS,
    "file_byte_offset": 0,
    "last_file_size":  0,
    "last_row_count":  0,
    # Alarm tracking
    "alarm_pump_active":    False,
    "alarm_stale_active":   False,
    "pump_last_value":      None,
    "pump_last_change_ts":  None,
    "last_known_file_size": 0,
    "last_file_size_ts":    None,
    "ack_until":            0.0,
    "last_mode":            None,
    "alarm_log":            [],
    # Threshold alarms — runtime editable
    "threshold_alarms_runtime": [
        {"col": a["col"], "lo": a.get("lo"), "hi": a.get("hi"), "active": False}
        for a in cfg.THRESHOLD_ALARMS
    ],
    # Fetch metadata
    "fetch_errors":    0,
}


def _read_shared_unlocked():
    """Read shared state from disk. Returns defaults if file missing/corrupt."""
    if not os.path.exists(_STATE_FILE):
        return dict(_DEFAULT_STATE)
    try:
        with open(_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return dict(_DEFAULT_STATE)


def read_shared(timeout=5):
    """Read the shared state from disk under the shared-state lock."""
    with _file_lock(timeout=timeout):
        return _read_shared_unlocked()


def _replace_with_retry(src, dst, *, attempts=250, delay_sec=0.02):
    last_exc = None
    for _ in range(max(1, int(attempts))):
        try:
            os.replace(src, dst)
            return
        except PermissionError as exc:
            last_exc = exc
            time.sleep(delay_sec)
    if last_exc is not None:
        raise last_exc


def _write_shared_unlocked(state):
    """Write the entire shared state to disk (caller must hold lock)."""
    fd, tmp = tempfile.mkstemp(prefix="state.", suffix=".json.tmp", dir=_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        _replace_with_retry(tmp, _STATE_FILE)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def write_shared(state, timeout=5):
    """Write the entire shared state to disk under the shared-state lock."""
    with _file_lock(timeout=timeout):
        _write_shared_unlocked(state)


def update_shared(**kwargs):
    """Atomic read-modify-write for one or more keys."""
    with _file_lock():
        state = read_shared()
        state.update(kwargs)
        write_shared(state)
    return state


# ── Session management ───────────────────────────────────────

def cleanup_stale_sessions(state, timeout_sec=30):
    """Remove sessions with heartbeats older than timeout_sec. Mutates state."""
    now = time.time()
    sessions = state.get("sessions", {})
    stale = [sid for sid, ts in sessions.items() if now - ts > timeout_sec]
    for sid in stale:
        del sessions[sid]
        # If the stale session was the fetcher, release it
        if state.get("fetcher_id") == sid:
            state["fetcher_id"] = None
            state["fetcher_heartbeat"] = 0.0
    return state


def register_session(session_id):
    """
    Register a new session. Returns True if accepted, False if at capacity.
    """
    with _file_lock():
        state = read_shared()
        state = cleanup_stale_sessions(state)
        sessions = state.get("sessions", {})

        # Already registered — just update heartbeat
        if session_id in sessions:
            sessions[session_id] = time.time()
            write_shared(state)
            return True

        if len(sessions) >= cfg.MAX_CONNECTIONS:
            return False

        sessions[session_id] = time.time()
        state["sessions"] = sessions
        write_shared(state)
    return True


def heartbeat(session_id):
    """Update the heartbeat timestamp for this session."""
    with _file_lock():
        state = read_shared()
        sessions = state.get("sessions", {})
        sessions[session_id] = time.time()
        state["sessions"] = sessions
        state = cleanup_stale_sessions(state)
        write_shared(state)


def deregister_session(session_id):
    """Remove a session from the registry."""
    with _file_lock():
        state = read_shared()
        sessions = state.get("sessions", {})
        sessions.pop(session_id, None)
        if state.get("fetcher_id") == session_id:
            state["fetcher_id"] = None
            state["fetcher_heartbeat"] = 0.0
        state["sessions"] = sessions
        write_shared(state)


def active_session_count():
    """Return the number of active sessions."""
    with _file_lock():
        state = read_shared()
        state = cleanup_stale_sessions(state)
        write_shared(state)
    return len(state.get("sessions", {}))


# ── Shared DataFrame ─────────────────────────────────────────

def get_shared_dataframe():
    """Read the shared DataFrame from Parquet. Returns empty DataFrame if missing."""
    with _file_lock():
        if not os.path.exists(_DATA_FILE):
            return pd.DataFrame()
        try:
            return pd.read_parquet(_DATA_FILE)
        except Exception:
            return pd.DataFrame()


def set_shared_dataframe(df):
    """Write the shared DataFrame to Parquet (atomic via temp + rename)."""
    if df.empty:
        return
    with _file_lock():
        fd, tmp = tempfile.mkstemp(prefix="data.", suffix=".parquet.tmp", dir=_DIR)
        os.close(fd)
        try:
            df.to_parquet(tmp, index=False)
            _replace_with_retry(tmp, _DATA_FILE)
        finally:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass


# ── Primary fetcher election ─────────────────────────────────

def claim_fetcher(session_id):
    """
    Attempt to become the primary fetcher.
    Returns True if this session is (or just became) the fetcher.
    """
    with _file_lock():
        state = read_shared()
        current = state.get("fetcher_id")
        hb = state.get("fetcher_heartbeat", 0.0)

        # Already the fetcher — update heartbeat
        if current == session_id:
            state["fetcher_heartbeat"] = time.time()
            write_shared(state)
            return True

        # No fetcher, or fetcher is stale (>15 s)
        if current is None or (time.time() - hb) > 15:
            state["fetcher_id"] = session_id
            state["fetcher_heartbeat"] = time.time()
            write_shared(state)
            return True

    return False


def release_fetcher(session_id):
    """Release the fetcher role if this session holds it."""
    with _file_lock():
        state = read_shared()
        if state.get("fetcher_id") == session_id:
            state["fetcher_id"] = None
            state["fetcher_heartbeat"] = 0.0
            write_shared(state)


def is_fetcher_alive(timeout_sec=15):
    """Return True if the current primary fetcher's heartbeat is fresh."""
    state = read_shared()
    hb = state.get("fetcher_heartbeat", 0.0)
    return (time.time() - hb) < timeout_sec


# ── Alarm log ────────────────────────────────────────────────

def append_alarm_log(entry):
    """Append an alarm log entry. Cap at 500 entries (FIFO)."""
    with _file_lock():
        state = read_shared()
        log = state.get("alarm_log", [])
        log.append(entry)
        if len(log) > 500:
            log = log[-500:]
        state["alarm_log"] = log
        write_shared(state)


def get_alarm_log():
    """Return the alarm log."""
    state = read_shared()
    return state.get("alarm_log", [])
