# =============================================================
#  dashboard.py  —  Jetson Nano Live Sensor Dashboard
#
#  Reads sensor data from a remote Jetson Nano via SSH/SFTP.
#  The Jetson is never modified — all access is read-only.
#
#  Run:  python launcher.py        (recommended — includes mDNS)
#  Or:   streamlit run dashboard.py
# =============================================================

import streamlit as st
import pandas as pd
import time
from uuid import uuid4
from datetime import datetime
from streamlit_autorefresh import st_autorefresh

import config as cfg
import alarms
import sftp_reader
import audio
import shared_state
import auth
import sftp_browser
from fetcher import DataFetcher


# ── Page setup ────────────────────────────────────────────────

st.set_page_config(
    page_title="Jetson Dashboard",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
  @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Rajdhani:wght@400;600;700&display=swap');

  html, body, [class*="css"] {
      font-family: 'Rajdhani', sans-serif;
      background: #0d1117;
      color: #c9d1d9;
  }
  .stApp { background: #0d1117; }

  .metric-card {
      background: #161b22;
      border: 1px solid #30363d;
      border-radius: 8px;
      padding: 12px 16px;
      text-align: center;
  }
  .metric-label {
      font-size: 11px;
      color: #8b949e;
      letter-spacing: 1px;
      text-transform: uppercase;
  }
  .metric-value {
      font-family: 'JetBrains Mono', monospace;
      font-size: 22px;
      color: #58a6ff;
      font-weight: 700;
  }
  .metric-unit { font-size: 11px; color: #6e7681; }

  .stSidebar { background: #161b22 !important; border-right: 1px solid #30363d; }

  .stButton>button {
      background: #21262d;
      border: 1px solid #30363d;
      color: #c9d1d9;
      border-radius: 6px;
      font-family: 'Rajdhani', sans-serif;
      font-weight: 600;
  }
  .stButton>button:hover { border-color: #58a6ff; color: #58a6ff; }
</style>
""", unsafe_allow_html=True)


# ── Authentication gate ──────────────────────────────────────

if not auth.render_login_gate():
    st.stop()


# ── Session registration ─────────────────────────────────────

if "session_id" not in st.session_state:
    st.session_state.session_id = str(uuid4())

session_id = st.session_state.session_id

if not shared_state.register_session(session_id):
    st.error(f"Server at capacity (max {cfg.MAX_CONNECTIONS} connections). Please try again later.")
    st.stop()

shared_state.heartbeat(session_id)


# ── Session state defaults (local per-tab state) ─────────────

_LOCAL_DEFAULTS = {
    "ssh_client":   None,
    "sftp":         None,
    # Chart builder — per-session so each user can customise their view
    "plot_groups":  None,
    "snd_queue":    [],
}

for k, v in _LOCAL_DEFAULTS.items():
    if k not in st.session_state:
        st.session_state[k] = v

ss = st.session_state

# Create the fetcher coordinator for this session
if "fetcher" not in ss:
    ss.fetcher = DataFetcher(session_id)

# Read current shared state
_shared = shared_state.read_shared()


# ── Sidebar ───────────────────────────────────────────────────

with st.sidebar:
    st.markdown("## Jetson Dashboard")
    st.markdown("---")

    st.markdown("### SSH Connection")
    host     = st.text_input("Jetson IP Address", value=_shared.get("ssh_host", cfg.SSH_HOST) or cfg.SSH_HOST)
    port     = st.number_input("SSH Port", value=_shared.get("ssh_port", cfg.SSH_PORT), min_value=1, max_value=65535)
    username = st.text_input("Username", value=_shared.get("ssh_username", cfg.SSH_USERNAME) or cfg.SSH_USERNAME)
    password = st.text_input("Password", type="password",
                             value=_shared.get("ssh_password", cfg.SSH_PASSWORD) or "")

    st.markdown("### CSV File")
    current_csv_path = _shared.get("remote_csv_path", cfg.REMOTE_CSV_PATH)
    remote_path  = st.text_input("Remote CSV Path", value=current_csv_path)
    time_col_cfg = st.text_input("Timestamp Column", value=_shared.get("timestamp_column", cfg.TIMESTAMP_COLUMN))
    max_rows     = st.number_input("Max rows in memory (0 = all)",
                                   value=_shared.get("max_rows", cfg.MAX_ROWS), step=500)

    # SFTP file browser
    if _shared.get("connected") and ss.sftp:
        selected_file = sftp_browser.render_browser(ss.sftp, remote_path)
        if selected_file:
            remote_path = selected_file
            shared_state.update_shared(
                remote_csv_path=selected_file,
                file_byte_offset=0,
                last_row_count=0,
                last_file_size=0,
            )
            st.rerun()

    st.markdown("### Refresh")
    refresh_sec = st.slider("Interval (seconds)", 2, 60, cfg.REFRESH_SEC)
    plot_window = st.slider("Plot window (minutes, 0 = all)", 0, 120, cfg.PLOT_WINDOW_MIN)

    c1, c2 = st.columns(2)
    with c1: connect_btn    = st.button("Connect",    use_container_width=True)
    with c2: disconnect_btn = st.button("Disconnect", use_container_width=True)

    st.markdown("---")
    st.markdown("### Alarms")
    alarms_on      = st.checkbox("Enable audible alarms", value=True)
    pump_stall_sec = st.number_input("Pump stall timeout (s)",
                                     value=cfg.PUMP_STALL_SEC, min_value=5, step=5)
    stale_sec      = st.number_input("No-data alarm (s)",
                                     value=cfg.STALE_DATA_SEC, min_value=10, step=5)

    st.markdown("**Threshold Alarms**")
    st.caption("Excluded modes are set in config.py.")

    # Read thresholds from shared state
    rt = _shared.get("threshold_alarms_runtime", [
        {"col": a["col"], "lo": a.get("lo"), "hi": a.get("hi"), "active": False}
        for a in cfg.THRESHOLD_ALARMS
    ])

    # Load shared DataFrame for column detection
    df_for_cols = shared_state.get_shared_dataframe()

    threshold_changed = False
    for i, row in enumerate(rt):
        active = row.get("active", False)
        header = f"{row['col']}" + (" — ACTIVE" if active else "")
        with st.expander(header, expanded=active):
            num_cols = df_for_cols.select_dtypes(include="number").columns.tolist() if not df_for_cols.empty else []
            if num_cols:
                col_idx = num_cols.index(row["col"]) if row["col"] in num_cols else 0
                new_col = st.selectbox("Column", num_cols, index=col_idx, key=f"thr_col_{i}")
            else:
                new_col = st.text_input("Column", value=row["col"], key=f"thr_col_{i}")

            lo_on = st.checkbox("Enable low limit",  value=row["lo"]  is not None, key=f"thr_loon_{i}")
            hi_on = st.checkbox("Enable high limit", value=row["hi"] is not None, key=f"thr_hion_{i}")

            lo_val = st.number_input("Low limit",  value=float(row["lo"])  if row["lo"]  is not None else 0.0,
                                     step=0.5, key=f"thr_lo_{i}", disabled=not lo_on)
            hi_val = st.number_input("High limit", value=float(row["hi"]) if row["hi"] is not None else 100.0,
                                     step=0.5, key=f"thr_hi_{i}", disabled=not hi_on)

            new_lo = lo_val if lo_on else None
            new_hi = hi_val if hi_on else None

            if new_col != row["col"] or new_lo != row.get("lo") or new_hi != row.get("hi"):
                threshold_changed = True
            row["col"] = new_col
            row["lo"] = new_lo
            row["hi"] = new_hi

            if st.button("Remove", key=f"thr_del_{i}"):
                rt.pop(i)
                shared_state.update_shared(threshold_alarms_runtime=rt)
                st.rerun()

    if st.button("Add threshold alarm", use_container_width=True):
        num_cols = df_for_cols.select_dtypes(include="number").columns.tolist() if not df_for_cols.empty else []
        default_col = num_cols[0] if num_cols else "Temperature_Outlet"
        rt.append({"col": default_col, "lo": None, "hi": 90.0, "active": False})
        shared_state.update_shared(threshold_alarms_runtime=rt)
        st.rerun()

    # Sync threshold changes to shared state
    if threshold_changed:
        shared_state.update_shared(threshold_alarms_runtime=rt)

    ack_until = _shared.get("ack_until", 0.0)
    ack_remaining = max(0.0, ack_until - time.time())
    if ack_remaining > 0:
        st.warning(f"Silenced — {int(ack_remaining // 60)}m {int(ack_remaining % 60)}s remaining")

    if st.button("Acknowledge All Alarms (5 min)", use_container_width=True):
        now = time.time()
        shared_state.update_shared(
            ack_until=now + cfg.ACK_SILENCE_SEC,
            alarm_pump_active=False,
            alarm_stale_active=False,
            pump_last_change_ts=now,
            last_file_size_ts=now,
        )
        # Reset threshold active flags
        for row in rt:
            row["active"] = False
        shared_state.update_shared(threshold_alarms_runtime=rt)
        audio.queue("ack")

    st.markdown("---")

    # Connection status + health indicator
    is_connected = _shared.get("connected", False)
    fetch_errs = _shared.get("fetch_errors", 0)

    if is_connected:
        if fetch_errs == 0:
            st.success("CONNECTED")
        elif fetch_errs <= 2:
            st.warning(f"CONNECTED — {fetch_errs} fetch error(s)")
        else:
            st.error(f"CONNECTED — {fetch_errs} errors — reconnecting...")
    else:
        st.caption("DISCONNECTED")

    # Active sessions count
    session_count = shared_state.active_session_count()
    st.caption(f"Active sessions: {session_count}/{cfg.MAX_CONNECTIONS}")


# ── Connect / Disconnect ──────────────────────────────────────

if connect_btn:
    try:
        client = sftp_reader.connect(host, int(port), username, password or None)
        ss.ssh_client = client
        ss.sftp       = client.open_sftp()

        # Write connection params to shared state so all sessions see them
        shared_state.update_shared(
            connected=True,
            ssh_host=host,
            ssh_port=int(port),
            ssh_username=username,
            ssh_password=password or "",
            remote_csv_path=remote_path,
            timestamp_column=time_col_cfg,
            max_rows=int(max_rows),
            file_byte_offset=0,
            last_file_size=0,
            last_row_count=0,
            fetch_errors=0,
            alarm_pump_active=False,
            alarm_stale_active=False,
        )
        # Claim fetcher role
        ss.fetcher.try_become_fetcher()
        st.success(f"Connected to {host}")
    except Exception as e:
        st.error(f"Connection failed: {e}")

if disconnect_btn:
    for obj in [ss.get("sftp"), ss.get("ssh_client")]:
        if obj:
            try: obj.close()
            except: pass
    ss.sftp       = None
    ss.ssh_client = None
    ss.fetcher.release()
    shared_state.update_shared(connected=False, fetch_errors=0)
    st.info("Disconnected.")

# Re-read shared state after potential connect/disconnect
_shared = shared_state.read_shared()


# ── Data fetch (primary fetcher) or read (other sessions) ────

is_connected = _shared.get("connected", False)
df = pd.DataFrame()

if is_connected:
    is_primary = ss.fetcher.is_primary()

    # If no fetcher is alive, try to become one
    if not is_primary and not shared_state.is_fetcher_alive():
        # Need an SSH connection to become the fetcher
        if ss.ssh_client is None:
            try:
                h = _shared.get("ssh_host", host)
                p = _shared.get("ssh_port", int(port))
                u = _shared.get("ssh_username", username)
                pw = _shared.get("ssh_password", password) or None
                client = sftp_reader.connect(h, p, u, pw)
                ss.ssh_client = client
                ss.sftp = client.open_sftp()
            except Exception:
                pass
        if ss.ssh_client:
            is_primary = ss.fetcher.try_become_fetcher()

    if is_primary and ss.ssh_client:
        # This session performs the SFTP fetch
        sftp_session, df, err = ss.fetcher.tick(ss.ssh_client, ss.sftp)
        ss.sftp = sftp_session

        if err:
            st.warning(f"Fetch: {err}")
            # Attempt reconnect on repeated errors
            fetch_errs = _shared.get("fetch_errors", 0)
            if fetch_errs >= 3:
                try:
                    if ss.sftp:
                        ss.sftp.close()
                    ss.sftp = None
                    h = _shared.get("ssh_host", host)
                    p = _shared.get("ssh_port", int(port))
                    u = _shared.get("ssh_username", username)
                    pw = _shared.get("ssh_password", password) or None
                    new_client = sftp_reader.connect(h, p, u, pw)
                    ss.ssh_client = new_client
                    ss.sftp = new_client.open_sftp()
                    shared_state.update_shared(fetch_errors=0)
                except Exception:
                    pass
    else:
        # Non-primary: read shared data
        df = shared_state.get_shared_dataframe()

# Fallback: if df is still empty, try shared data
if df.empty:
    df = shared_state.get_shared_dataframe()


# ── Type conversion ───────────────────────────────────────────

TEXT_COLS = {"Timestamp", "LogPhase", "Current Mode",
             "Expected MCU Version", "Actual MCU Version",
             "Bubble1_Status", "Bubble2_Status"}

if not df.empty:
    for col in df.columns:
        if col not in TEXT_COLS and df[col].dtype == object:
            df[col] = pd.to_numeric(df[col], errors="coerce")


# ── Alarm evaluation ──────────────────────────────────────────

# Re-read shared state for latest alarm info
_shared = shared_state.read_shared()
time_col = time_col_cfg.strip() if (not df.empty and time_col_cfg.strip() in df.columns) else None
alarm_events = {}

if not df.empty and alarms_on:
    is_primary = ss.fetcher.is_primary()

    if is_primary:
        # Primary fetcher evaluates alarms and writes to shared state
        alarm_state = {
            "alarm_pump_active":    _shared.get("alarm_pump_active", False),
            "alarm_stale_active":   _shared.get("alarm_stale_active", False),
            "pump_last_value":      _shared.get("pump_last_value"),
            "pump_last_change_ts":  _shared.get("pump_last_change_ts"),
            "last_known_file_size": _shared.get("last_known_file_size", 0),
            "last_file_size":       _shared.get("last_file_size", 0),
            "last_file_size_ts":    _shared.get("last_file_size_ts"),
            "ack_until":            _shared.get("ack_until", 0.0),
            "last_mode":            _shared.get("last_mode"),
            "threshold_alarms_runtime": _shared.get("threshold_alarms_runtime", rt),
        }

        alarm_state, alarm_events = ss.fetcher.evaluate_alarms(
            df, alarm_state, alarms_on, pump_stall_sec, stale_sec
        )

        # Write alarm results back to shared state
        shared_state.update_shared(
            alarm_pump_active=alarm_state.get("alarm_pump_active", False),
            alarm_stale_active=alarm_state.get("alarm_stale_active", False),
            pump_last_value=alarm_state.get("pump_last_value"),
            pump_last_change_ts=alarm_state.get("pump_last_change_ts"),
            last_known_file_size=alarm_state.get("last_known_file_size", 0),
            last_file_size_ts=alarm_state.get("last_file_size_ts"),
            last_mode=alarm_state.get("last_mode"),
            threshold_alarms_runtime=alarm_state.get("threshold_alarms_runtime", rt),
        )

        # Send webhook for critical alarms
        if cfg.ALARM_WEBHOOK_URL and (alarm_events.get("alarm_pump") or alarm_events.get("alarm_threshold") or alarm_events.get("alarm_stale")):
            _send_webhook(alarm_events, df)

        _shared = shared_state.read_shared()

    # Queue audio for this session based on shared alarm state
    _acked = time.time() < _shared.get("ack_until", 0.0)
    if not _acked:
        if _shared.get("alarm_pump_active"):
            audio.queue("alarm_pump")
        if _shared.get("alarm_stale_active"):
            audio.queue("alarm_stale")
        # Check threshold active flags
        for rt_row in _shared.get("threshold_alarms_runtime", []):
            if rt_row.get("active"):
                audio.queue("alarm_threshold")
                break

    if alarm_events.get("mode_changed"):
        audio.queue("mode_change")


# ── Webhook helper ───────────────────────────────────────────

def _send_webhook(events, df):
    """POST alarm notification to configured webhook URL."""
    if not cfg.ALARM_WEBHOOK_URL:
        return
    try:
        import requests
        payload = {
            "timestamp": datetime.now().isoformat(),
            "host": _shared.get("ssh_host", ""),
            "events": list(events.keys()),
        }
        if not df.empty and "Current Mode" in df.columns:
            payload["mode"] = str(df.iloc[-1]["Current Mode"])
        requests.post(cfg.ALARM_WEBHOOK_URL, json=payload, timeout=5)
    except Exception:
        pass


# ── Page header ───────────────────────────────────────────────

st.markdown("# Jetson Nano — Live Sensor Dashboard")

hc = st.columns([2, 2, 2, 2, 1])
with hc[0]:
    if is_connected:
        fetch_errs = _shared.get("fetch_errors", 0)
        if fetch_errs == 0:
            st.success("LIVE")
        elif fetch_errs <= 2:
            st.warning(f"LIVE ({fetch_errs} errors)")
        else:
            st.error(f"RECONNECTING ({fetch_errs} errors)")
    else:
        st.caption("Offline")
with hc[1]: st.markdown(f"**Host:** `{_shared.get('ssh_host', host)}:{_shared.get('ssh_port', port)}`")
with hc[2]: st.markdown(f"**Rows:** `{len(df):,}`")
with hc[3]: st.markdown(f"**Updated:** `{datetime.now():%H:%M:%S}`")
with hc[4]: st.button("Refresh")

st.divider()


# ── Alarm banners ─────────────────────────────────────────────

_ack_remaining = max(0.0, _shared.get("ack_until", 0.0) - time.time())
_ack_note = (f"  *(silenced — {int(_ack_remaining // 60)}m {int(_ack_remaining % 60)}s remaining)*"
             if _ack_remaining > 0
             else "  |  *Acknowledge in sidebar to silence for 5 min*")

if _shared.get("alarm_pump_active"):
    st.error(f"PUMP STALL — Pump1_Ticks unchanged for {pump_stall_sec}s{_ack_note}")

if _shared.get("alarm_stale_active"):
    st.error(f"NO NEW DATA — file unchanged for {stale_sec}s — check Jetson process{_ack_note}")

for rt_row in _shared.get("threshold_alarms_runtime", []):
    if rt_row.get("active"):
        col = rt_row["col"]
        val = "N/A"
        if col in df.columns and not df.empty:
            try:
                val = f"{float(df.iloc[-1][col]):.3f}"
            except (ValueError, TypeError):
                pass
        lo, hi = rt_row.get("lo"), rt_row.get("hi")
        limit  = f"hi={hi}" if hi is not None else f"lo={lo}"
        st.error(f"THRESHOLD — {col} = {val}  ({limit}){_ack_note}")

if alarm_events.get("mode_changed"):
    st.info(f"MODE CHANGED  ->  {alarm_events['mode_changed']}")

# Persistent alarm log from shared state
alarm_log = shared_state.get_alarm_log()
if alarm_log:
    with st.expander(f"Alarm Log ({len(alarm_log)} events)"):
        for entry in reversed(alarm_log[-50:]):
            st.text(entry)
        if st.button("Clear Log"):
            shared_state.update_shared(alarm_log=[])


# ── Live status cards ─────────────────────────────────────────

if not df.empty:
    st.markdown("### Live Status")

    def lv(col, fmt=".3f"):
        if col not in df.columns: return "N/A"
        try: return f"{float(df.iloc[-1][col]):{fmt}}"
        except: return str(df.iloc[-1][col])

    mode_val  = str(df.iloc[-1].get("Current Mode", "—")) if "Current Mode" in df.columns else "—"
    phase_val = str(df.iloc[-1].get("LogPhase",     "—")) if "LogPhase"     in df.columns else "—"

    cards = [
        ("Current Mode",   mode_val,                ""),
        ("Log Phase",      phase_val,               ""),
        ("Heater PWM",     lv("Heater_PWM", ".1f"), "%"),
        ("Outlet Temp",    lv("Temperature_Outlet"), "C"),
        ("Inlet Temp",     lv("Temperature_Inlet"), "C"),
        ("Volume Total",         lv("Load Cell Raw"),  "mL"),
        ("Pump Ticks",     lv("Pump1_Ticks", ".0f"), ""),
        ("Volume Relative",         lv("Load Cell Relative"),  "mL"),
    ]

    card_html = '<div style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:16px;">'
    for label, value, unit in cards:
        card_html += (
            f'<div class="metric-card" style="min-width:130px;">'
            f'<div class="metric-label">{label}</div>'
            f'<div class="metric-value">{value}</div>'
            + (f'<div class="metric-unit">{unit}</div>' if unit else "")
            + "</div>"
        )
    card_html += "</div>"
    st.markdown(card_html, unsafe_allow_html=True)

    st.divider()


# ── Chart builder ─────────────────────────────────────────────

    st.markdown("### Charts")
    st.caption("Each row can overlay multiple signals on one axis.")

    numeric_cols = df.select_dtypes(include="number").columns.tolist()

    PALETTE = [
        "#58a6ff", "#3fb950", "#d29922", "#f85149",
        "#bc8cff", "#79c0ff", "#56d364", "#ffa657",
        "#ff7b72", "#e3b341", "#a5d6ff", "#7ee787",
    ]

    # Initialise from config on first load
    if ss.plot_groups is None:
        ss.plot_groups = [
            {**g, "colors": [PALETTE[j % len(PALETTE)] for j in range(len(g["columns"]))]}
            for g in cfg.DEFAULT_CHARTS
        ]

    n_rows = st.slider("Number of chart rows", 1, 6, max(1, len(ss.plot_groups)))

    while len(ss.plot_groups) < n_rows:
        ss.plot_groups.append({
            "label":   f"Chart {len(ss.plot_groups) + 1}",
            "y_label": "",
            "columns": [],
            "colors":  [],
        })

    groups_to_plot = []
    cols_ui = st.columns(min(n_rows, 3))

    for i in range(n_rows):
        g     = ss.plot_groups[i] if i < len(ss.plot_groups) else {}
        saved = [c for c in g.get("columns", []) if c in numeric_cols]

        with cols_ui[i % 3]:
            st.markdown(f"**Row {i + 1}**")
            lbl  = st.text_input("Label",        value=g.get("label",   f"Chart {i+1}"), key=f"lbl_{i}")
            ylab = st.text_input("Y-axis label",  value=g.get("y_label", ""),            key=f"ylab_{i}")
            sel  = st.multiselect("Signals",      options=numeric_cols, default=saved,    key=f"sel_{i}")

        if sel:
            groups_to_plot.append({
                "label":   lbl,
                "y_label": ylab,
                "columns": sel,
                "colors":  [PALETTE[j % len(PALETTE)] for j in range(len(sel))],
            })

    if groups_to_plot:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots

        plot_df = df.copy()
        tw = int(plot_window) if int(plot_window) > 0 else None
        if tw and time_col and time_col in plot_df.columns:
            try:
                plot_df[time_col] = pd.to_datetime(plot_df[time_col])
                cutoff = plot_df[time_col].max() - pd.Timedelta(minutes=tw)
                plot_df = plot_df[plot_df[time_col] >= cutoff]
            except:
                pass

        x     = plot_df[time_col] if (time_col and time_col in plot_df.columns) else plot_df.index
        valid = [g for g in groups_to_plot if any(c in plot_df.columns for c in g["columns"])]

        if valid:
            fig = make_subplots(
                rows=len(valid), cols=1,
                shared_xaxes=True,
                vertical_spacing=0.03,
                subplot_titles=[g["label"] for g in valid],
            )
            for row_i, g in enumerate(valid, start=1):
                for col_i, col in enumerate(g["columns"]):
                    if col not in plot_df.columns:
                        continue
                    color = g["colors"][col_i % len(g["colors"])]
                    fig.add_trace(
                        go.Scatter(
                            x=x,
                            y=pd.to_numeric(plot_df[col], errors="coerce"),
                            mode="lines",
                            name=col,
                            line=dict(color=color, width=1.8),
                            hovertemplate=f"<b>{col}</b>: %{{y:.3f}}<br>%{{x}}<extra></extra>",
                            legendgroup=g["label"],
                        ),
                        row=row_i, col=1,
                    )
                fig.update_yaxes(
                    row=row_i, col=1,
                    title_text=g.get("y_label", ""),
                    gridcolor="#21262d",
                    zerolinecolor="#30363d",
                    tickfont=dict(family="JetBrains Mono", size=9),
                )
                fig.update_xaxes(
                    row=row_i, col=1,
                    gridcolor="#21262d",
                    zerolinecolor="#30363d",
                )
            fig.update_layout(
                height=max(200 * len(valid) + 60, 300),
                paper_bgcolor="#0d1117",
                plot_bgcolor="#161b22",
                font=dict(color="#c9d1d9", family="Rajdhani", size=12),
                margin=dict(l=70, r=20, t=30, b=40),
                hovermode="x unified",
                legend=dict(
                    x=1.01, y=1,
                    font=dict(size=10, family="JetBrains Mono"),
                    bgcolor="rgba(0,0,0,0)",
                ),
            )
            st.plotly_chart(fig, use_container_width=True, key="main_chart")
        else:
            st.info("No matching columns in data for selected signals.")
    else:
        st.info("Select at least one signal above to plot.")

    st.divider()

    # Raw data + export
    with st.expander("Raw Data (last 100 rows)"):
        st.dataframe(df.tail(100), use_container_width=True)
        csv_bytes = df.to_csv(index=False).encode("utf-8")
        st.download_button(
            label=f"Export all {len(df):,} rows as CSV",
            data=csv_bytes,
            file_name=f"jetson_export_{datetime.now():%Y%m%d_%H%M%S}.csv",
            mime="text/csv",
        )

else:
    if is_connected:
        st.info("Waiting for data — check that the CSV path is correct.")
    else:
        st.markdown("### Connect to your Jetson Nano to begin")
        st.caption("Enter SSH credentials in the sidebar and click Connect.")


# ── Audio render + auto-refresh ───────────────────────────────

audio.render()

if is_connected:
    st_autorefresh(interval=int(refresh_sec) * 1000, key="data_refresh")
