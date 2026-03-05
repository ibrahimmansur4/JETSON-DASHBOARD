# =============================================================
#  alarms.py  —  Alarm evaluation logic
#
#  Each eval_* function is pure:
#    - receives the current DataFrame and session_state
#    - updates timing state on session_state as a side effect
#    - returns True if the alarm condition is currently met
#
#  Mode exclusion is handled inside each evaluator.
#  Excluded modes are defined in config.py, not in the UI.
# =============================================================

import time


# ------------------------------------------------------------------
#  Session-state helpers
#  Streamlit SessionState supports attribute access but not dict
#  subscript assignment outside the main script, so we use
#  getattr / setattr throughout.
# ------------------------------------------------------------------

def _get(ss, key, default=None):
    """Read a key from session_state (attribute) or a plain dict."""
    if isinstance(ss, dict):
        return ss.get(key, default)
    return getattr(ss, key, default)

def _set(ss, key, value):
    """Write a key to session_state (attribute) or a plain dict."""
    if isinstance(ss, dict):
        ss[key] = value
    else:
        setattr(ss, key, value)


# ------------------------------------------------------------------
#  Shared utility
# ------------------------------------------------------------------

def current_mode(df):
    """Return the latest value of the 'Current Mode' column, or empty string."""
    if df.empty or "Current Mode" not in df.columns:
        return ""
    return str(df.iloc[-1]["Current Mode"]).strip()


def is_acknowledged(ss):
    """Return True while we are inside an active acknowledge window."""
    return time.time() < _get(ss, "ack_until", 0.0)


# ------------------------------------------------------------------
#  Pump stall
# ------------------------------------------------------------------

def eval_pump_stall(df, ss, stall_sec, excluded_modes):
    """
    Returns True when Pump1_Ticks has not increased for >= stall_sec seconds.

    The check is skipped (returns False and resets the timer) whenever
    the current mode is listed in excluded_modes, which covers operating
    modes where no pump is fitted or expected to run.
    """
    now  = time.time()
    mode = current_mode(df)

    # Mode is excluded — reset timer so alarm does not fire immediately
    # when the machine transitions back to an active mode.
    if mode in excluded_modes:
        _set(ss, "pump_last_change_ts", now)
        return False

    if df.empty or "Pump1_Ticks" not in df.columns:
        return False

    try:
        ticks = float(df.iloc[-1]["Pump1_Ticks"])
    except (ValueError, TypeError):
        return False

    last_val = _get(ss, "pump_last_value")
    if last_val is None or ticks != last_val:
        _set(ss, "pump_last_value",     ticks)
        _set(ss, "pump_last_change_ts", now)
        return False

    elapsed = now - (_get(ss, "pump_last_change_ts") or now)
    return elapsed >= stall_sec


# ------------------------------------------------------------------
#  Stale data
# ------------------------------------------------------------------

def eval_stale_data(file_size_now, ss, stale_sec, excluded_modes, df):
    """
    Returns True when the remote file size has not changed for >= stale_sec
    seconds, indicating the Jetson process has stopped writing.

    Suppressed in excluded_modes (e.g. STANDBY, where writing is intentionally
    paused).
    """
    now  = time.time()
    mode = current_mode(df)

    if mode in excluded_modes:
        _set(ss, "last_file_size_ts",    now)
        _set(ss, "last_known_file_size", file_size_now)
        return False

    if file_size_now != _get(ss, "last_known_file_size", 0):
        _set(ss, "last_known_file_size", file_size_now)
        _set(ss, "last_file_size_ts",    now)
        return False

    last_change = _get(ss, "last_file_size_ts") or now
    return (now - last_change) >= stale_sec


# ------------------------------------------------------------------
#  Sensor threshold
# ------------------------------------------------------------------

def eval_threshold(df, alarm_cfg):
    """
    Evaluate one threshold entry from config.THRESHOLD_ALARMS.

    alarm_cfg keys:
        col     : CSV column name
        lo      : low limit  (None = disabled)
        hi      : high limit (None = disabled)
        exclude : set of mode names where this alarm is suppressed

    Returns (tripped: bool, message: str).
    """
    col     = alarm_cfg.get("col", "")
    lo      = alarm_cfg.get("lo")
    hi      = alarm_cfg.get("hi")
    exclude = alarm_cfg.get("exclude", set())

    if df.empty or col not in df.columns:
        return False, ""

    if current_mode(df) in exclude:
        return False, ""

    try:
        val = float(df.iloc[-1][col])
    except (ValueError, TypeError):
        return False, ""

    if hi is not None and val > hi:
        return True, f"HIGH: {col} = {val:.3f} > {hi}"
    if lo is not None and val < lo:
        return True, f"LOW:  {col} = {val:.3f} < {lo}"
    return False, ""


# ------------------------------------------------------------------
#  Mode change
# ------------------------------------------------------------------

def eval_mode_change(df, ss):
    """
    Returns (changed: bool, new_mode: str).
    Updates ss.last_mode as a side effect.
    """
    if df.empty or "Current Mode" not in df.columns:
        return False, ""

    mode = current_mode(df)
    last = _get(ss, "last_mode")
    _set(ss, "last_mode", mode)

    if last is not None and mode != last:
        return True, mode
    return False, ""
