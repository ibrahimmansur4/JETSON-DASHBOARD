# =============================================================
#  config.py  —  User configuration for the Jetson Dashboard
#
#  This is the ONLY file you need to edit.
#  All changes take effect on the next dashboard restart.
# =============================================================


# ── SSH Connection ────────────────────────────────────────────

SSH_HOST     = "192.168.1.113"
SSH_PORT     = 22
SSH_USERNAME = "byonyks"
SSH_PASSWORD = ""           # Leave blank to enter at runtime in the sidebar


# ── Remote File ───────────────────────────────────────────────

REMOTE_CSV_PATH  = "/home/jetson/data/sensors.csv"
TIMESTAMP_COLUMN = "Timestamp"

# Maximum rows kept in memory at once. Older rows are dropped when the
# limit is reached. Increase for longer history; 0 = unlimited.
MAX_ROWS = 5000


# ── Display ───────────────────────────────────────────────────

REFRESH_SEC     = 5     # Seconds between data fetches
PLOT_WINDOW_MIN = 10    # Minutes of history shown in charts (0 = show all)


# ── Chart Layout ──────────────────────────────────────────────
#
# Each dict defines one subplot row in the chart builder.
# Signals listed under "columns" are overlaid on the same axis.
# These are the defaults loaded on first run; the user can adjust
# them live through the chart builder on the dashboard.

DEFAULT_CHARTS = [
    {
        "label":   "Temperatures",
        "y_label": "C",
        "columns": [
            "Temperature_Inlet",
            "Temperature_Outlet",
            "Temperature C1/T1",
            "Temperature T3",
        ],
    },
    {
        "label":   "Pressures",
        "y_label": "kPa",
        "columns": ["Pressure1", "Pressure2", "Pressure3", "Pressure4"],
    },
    {
        "label":   "Pump and Volume",
        "y_label": "",
        "columns": ["Pump1_Ticks", "Volume Calculated"],
    },
]


# ── Alarm: Pump Stall ─────────────────────────────────────────
#
# Fires when Pump1_Ticks has not increased for PUMP_STALL_SEC seconds.
#
# PUMP_EXCLUDED_MODES: set of mode names where this alarm is suppressed.
# Use this for modes where no pump is fitted or expected to run, to
# prevent false positives. The stall timer resets on mode exit, so
# the alarm will not fire immediately when transitioning back to an
# active mode.

PUMP_STALL_SEC      = 20
PUMP_EXCLUDED_MODES = {"FT", "FTP", "RFT", "CAB", "CBB", "CDB"}


# ── Alarm: Stale Data ─────────────────────────────────────────
#
# Fires when the remote CSV file size has not grown for STALE_DATA_SEC
# seconds, indicating the Jetson process may have stopped writing.
#
# STALE_EXCLUDED_MODES: set of mode names where writing is intentionally
# paused (e.g. STANDBY), so the alarm is suppressed during those modes.

STALE_DATA_SEC       = 20
STALE_EXCLUDED_MODES = {}


# ── Alarm: Sensor Thresholds ──────────────────────────────────
#
# Add one dict per sensor limit you want to monitor.
#
#   col     : exact column name from the CSV header (case-sensitive)
#   lo      : low  limit — alarm if value drops BELOW this (None = disabled)
#   hi      : high limit — alarm if value rises ABOVE this (None = disabled)
#   exclude : set of mode names where this alarm is suppressed
#
# Examples:
#   Outlet temperature must stay below 90 °C in all modes:
#       {"col": "Temperature_Outlet", "lo": None, "hi": 90.0, "exclude": set()}
#
#   Pressure1 must stay above 900 kPa, but not checked during priming:
#       {"col": "Pressure1", "lo": 900.0, "hi": None, "exclude": {"PRIMING"}}
#
#   Inlet temperature range, suppressed in FT and STANDBY:
#       {"col": "Temperature_Inlet", "lo": 20.0, "hi": 45.0,
#        "exclude": {"FT", "STANDBY"}}

THRESHOLD_ALARMS = [
    {
        "col":     "Temperature_Outlet",
        "lo":      None,
        "hi":      90.0,
        "exclude": set(),
    },
    {
        "col":     "Temperature_Inlet",
        "lo":      None,
        "hi":      90.0,
        "exclude": set(),
    },
    {
        "col":     "Pressure2",
        "lo":      40,
        "hi":      50,
        "exclude": {"FT", "FTP", "RFT", "CAB", "CBB", "CDB"},
    },
]


# ── Acknowledge Silence Duration ──────────────────────────────
#
# How long (seconds) the Acknowledge button suppresses audio output.
# Visual alarm banners remain visible during the silence window.

ACK_SILENCE_SEC = 300   # 5 minutes
