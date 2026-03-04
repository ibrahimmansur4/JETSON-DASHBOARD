# =============================================================
#  dashboard.py  —  Jetson Nano Live Sensor Dashboard
#
#  Reads sensor data from a remote Jetson Nano via SSH/SFTP.
#  The Jetson is never modified — all access is read-only.
#
#  Run:  streamlit run dashboard.py
# =============================================================

import streamlit as st
import pandas as pd
import time
from datetime import datetime

import config as cfg
import alarms
import sftp_reader
import audio


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


# ── Session state defaults ────────────────────────────────────

_DEFAULTS = {
    "ssh_client":           None,
    "sftp":                 None,
    "df":                   pd.DataFrame(),
    "connected":            False,
    "fetch_errors":         0,
    "file_byte_offset":     0,
    "last_file_size":       0,
    "last_row_count":       0,
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
    # Runtime threshold list — col/lo/hi editable in sidebar; exclude stays in config.py
    "threshold_alarms_runtime": [
        {"col": a["col"], "lo": a.get("lo"), "hi": a.get("hi"), "active": False}
        for a in cfg.THRESHOLD_ALARMS
    ],
    # Chart builder state
    "plot_groups":          None,
    "snd_queue":            [],
}

for k, v in _DEFAULTS.items():
    if k not in st.session_state:
        st.session_state[k] = v

ss = st.session_state   # shorthand — defined here so it is available everywhere


# ── Sidebar ───────────────────────────────────────────────────

with st.sidebar:
    st.markdown("## Jetson Dashboard")
    st.markdown("---")

    st.markdown("### SSH Connection")
    host     = st.text_input("Jetson IP Address", value=cfg.SSH_HOST)
    port     = st.number_input("SSH Port", value=cfg.SSH_PORT, min_value=1, max_value=65535)
    username = st.text_input("Username", value=cfg.SSH_USERNAME)
    password = st.text_input("Password", type="password",
                             value=cfg.SSH_PASSWORD if cfg.SSH_PASSWORD else "")

    st.markdown("### CSV File")
    remote_path  = st.text_input("Remote CSV Path", value=cfg.REMOTE_CSV_PATH)
    time_col_cfg = st.text_input("Timestamp Column", value=cfg.TIMESTAMP_COLUMN)
    max_rows     = st.number_input("Max rows in memory (0 = all)",
                                   value=cfg.MAX_ROWS, step=500)

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

    rt = ss.threshold_alarms_runtime   # shorthand

    for i, row in enumerate(rt):
        active = row.get("active", False)
        header = f"{row['col']}" + (" — ACTIVE" if active else "")
        with st.expander(header, expanded=active):
            # Column selector — list of numeric columns if data loaded, else free text
            num_cols = ss.df.select_dtypes(include="number").columns.tolist() if not ss.df.empty else []
            if num_cols:
                col_idx = num_cols.index(row["col"]) if row["col"] in num_cols else 0
                row["col"] = st.selectbox("Column", num_cols, index=col_idx, key=f"thr_col_{i}")
            else:
                row["col"] = st.text_input("Column", value=row["col"], key=f"thr_col_{i}")

            lo_on = st.checkbox("Enable low limit",  value=row["lo"]  is not None, key=f"thr_loon_{i}")
            hi_on = st.checkbox("Enable high limit", value=row["hi"] is not None, key=f"thr_hion_{i}")

            lo_val = st.number_input("Low limit",  value=float(row["lo"])  if row["lo"]  is not None else 0.0,
                                     step=0.5, key=f"thr_lo_{i}", disabled=not lo_on)
            hi_val = st.number_input("High limit", value=float(row["hi"]) if row["hi"] is not None else 100.0,
                                     step=0.5, key=f"thr_hi_{i}", disabled=not hi_on)

            row["lo"] = lo_val if lo_on else None
            row["hi"] = hi_val if hi_on else None

            if st.button("Remove", key=f"thr_del_{i}"):
                rt.pop(i)
                st.rerun()

    if st.button("Add threshold alarm", use_container_width=True):
        num_cols = ss.df.select_dtypes(include="number").columns.tolist() if not ss.df.empty else []
        default_col = num_cols[0] if num_cols else "Temperature_Outlet"
        rt.append({"col": default_col, "lo": None, "hi": 90.0, "active": False})
        st.rerun()

    ack_remaining = max(0.0, st.session_state.ack_until - time.time())
    if ack_remaining > 0:
        st.warning(f"Silenced — {int(ack_remaining // 60)}m {int(ack_remaining % 60)}s remaining")

    if st.button("Acknowledge All Alarms (5 min)", use_container_width=True):
        st.session_state.ack_until           = time.time() + cfg.ACK_SILENCE_SEC
        st.session_state.alarm_pump_active   = False
        st.session_state.alarm_stale_active  = False
        st.session_state.pump_last_change_ts = time.time()
        st.session_state.last_file_size_ts   = time.time()
        for row in st.session_state.threshold_alarms_runtime:
            row["active"] = False
        audio.queue("ack")

    st.markdown("---")
    if st.session_state.connected:
        st.success("CONNECTED")
    else:
        st.caption("DISCONNECTED")
    if st.session_state.fetch_errors > 0:
        st.warning(f"{st.session_state.fetch_errors} fetch error(s)")


# ── Connect / Disconnect ──────────────────────────────────────

def _reset_state():
    for k, v in _DEFAULTS.items():
        st.session_state[k] = v

if connect_btn:
    try:
        client = sftp_reader.connect(host, int(port), username, password or None)
        _reset_state()
        st.session_state.ssh_client = client
        st.session_state.sftp       = client.open_sftp()
        st.session_state.connected  = True
        st.success(f"Connected to {host}")
    except Exception as e:
        st.error(f"Connection failed: {e}")

if disconnect_btn:
    for obj in [st.session_state.get("sftp"), st.session_state.get("ssh_client")]:
        if obj:
            try: obj.close()
            except: pass
    st.session_state.sftp       = None
    st.session_state.ssh_client = None
    st.session_state.connected  = False
    st.info("Disconnected.")


# ── Data fetch ────────────────────────────────────────────────

if st.session_state.connected and st.session_state.ssh_client:
    try:
        sftp  = sftp_reader.open_sftp(st.session_state.ssh_client,
                                      st.session_state.sftp)
        st.session_state.sftp = sftp
        fsize = sftp_reader.file_size(sftp, remote_path)
        limit = int(max_rows) if int(max_rows) > 0 else 50_000

        # First load, or file was truncated / rotated
        if st.session_state.last_row_count == 0 \
                or fsize < st.session_state.last_file_size:
            df_new, offset, err = sftp_reader.fetch_initial(sftp, remote_path, limit)
            if err:
                st.warning(f"Read warning: {err}")
            elif not df_new.empty:
                st.session_state.df               = df_new
                st.session_state.last_row_count   = len(df_new)
                st.session_state.file_byte_offset = offset
                st.session_state.last_file_size   = fsize
        else:
            # Incremental fetch — only new bytes since last read
            df_inc, new_offset, err = sftp_reader.fetch_incremental(
                sftp, remote_path, st.session_state.file_byte_offset
            )
            if err:
                st.session_state.fetch_errors += 1
            elif df_inc is not None and not df_inc.empty:
                expected = st.session_state.df.columns.tolist()
                df_inc   = df_inc[
                    df_inc.apply(lambda r: r.notna().sum(), axis=1) == len(expected)
                ]
                if not df_inc.empty:
                    df_inc.columns = expected
                    combined = pd.concat([st.session_state.df, df_inc],
                                         ignore_index=True)
                    if limit > 0 and len(combined) > limit:
                        combined = combined.iloc[-limit:]
                    st.session_state.df = combined
                st.session_state.file_byte_offset = new_offset
                st.session_state.last_file_size   = fsize
                st.session_state.last_row_count   = len(st.session_state.df)

    except Exception as e:
        st.session_state.fetch_errors += 1
        st.error(f"Fetch error: {e} — retrying next cycle")
        try:
            if st.session_state.sftp:
                st.session_state.sftp.close()
            st.session_state.sftp = None
            new_client = sftp_reader.connect(host, int(port), username, password or None)
            st.session_state.ssh_client = new_client
            st.session_state.sftp       = new_client.open_sftp()
        except:
            pass


# ── Type conversion ───────────────────────────────────────────
# SFTP delivers all bytes as strings. Convert numeric columns here.

TEXT_COLS = {"Timestamp", "LogPhase", "Current Mode",
             "Expected MCU Version", "Actual MCU Version",
             "Bubble1_Status", "Bubble2_Status"}

_df = st.session_state.df.copy() if not st.session_state.df.empty else st.session_state.df
if not _df.empty:
    for col in _df.columns:
        if col not in TEXT_COLS and _df[col].dtype == object:
            _df[col] = pd.to_numeric(_df[col], errors="coerce")
    st.session_state.df = _df


# ── Alarm evaluation ──────────────────────────────────────────

df       = st.session_state.df
time_col = time_col_cfg.strip() if time_col_cfg.strip() in df.columns else None
alarm    = {}

if not df.empty and alarms_on:
    _acked = alarms.is_acknowledged(ss)
    _fsize = ss.last_file_size

    # Pump stall
    pump_firing = alarms.eval_pump_stall(
        df, ss, float(pump_stall_sec), cfg.PUMP_EXCLUDED_MODES
    )
    if pump_firing:
        if not ss.alarm_pump_active:
            ss.alarm_pump_active = True
            ss.alarm_log.append(
                f"[{datetime.now():%H:%M:%S}] PUMP STALL — ticks unchanged {pump_stall_sec}s"
            )
        if not _acked:
            audio.queue("alarm_pump")
    else:
        ss.alarm_pump_active = False

    # Stale data
    stale_firing = alarms.eval_stale_data(
        _fsize, ss, float(stale_sec), cfg.STALE_EXCLUDED_MODES, df
    )
    if stale_firing:
        if not ss.alarm_stale_active:
            ss.alarm_stale_active = True
            ss.alarm_log.append(
                f"[{datetime.now():%H:%M:%S}] NO DATA — file unchanged {stale_sec}s"
            )
        if not _acked:
            audio.queue("alarm_stale")
    else:
        ss.alarm_stale_active = False

    # Threshold alarms — col/lo/hi from runtime (GUI-editable),
    # exclude set from config.py (code-only).
    # Build a merged view: zip runtime rows with config exclude sets.
    # If the user has added more rows than config has entries, exclude defaults to empty.
    rt_alarms = ss.threshold_alarms_runtime
    any_threshold = False
    for i, rt_row in enumerate(rt_alarms):
        cfg_exclude = cfg.THRESHOLD_ALARMS[i].get("exclude", set()) if i < len(cfg.THRESHOLD_ALARMS) else set()
        merged = {**rt_row, "exclude": cfg_exclude}
        tripped, msg = alarms.eval_threshold(df, merged)
        if tripped:
            if not rt_row.get("active"):
                rt_row["active"] = True
                ss.alarm_log.append(f"[{datetime.now():%H:%M:%S}] THRESHOLD — {msg}")
            any_threshold = True
        else:
            rt_row["active"] = False

    if any_threshold and not _acked:
        audio.queue("alarm_threshold")

    # Mode change
    mode_changed, new_mode = alarms.eval_mode_change(df, ss)
    if mode_changed:
        ss.alarm_log.append(f"[{datetime.now():%H:%M:%S}] MODE -> {new_mode}")
        audio.queue("mode_change")
        alarm["mode_changed"] = True
        alarm["new_mode"]     = new_mode


# ── Page header ───────────────────────────────────────────────

st.markdown("# Jetson Nano — Live Sensor Dashboard")

hc = st.columns([2, 2, 2, 2, 1])
with hc[0]:
    if ss.connected:
        st.success("LIVE")
    else:
        st.caption("Offline")
with hc[1]: st.markdown(f"**Host:** `{host}:{port}`")
with hc[2]: st.markdown(f"**Rows:** `{len(df):,}`")
with hc[3]: st.markdown(f"**Updated:** `{datetime.now():%H:%M:%S}`")
with hc[4]: st.button("Refresh")

st.divider()


# ── Alarm banners ─────────────────────────────────────────────

_ack_remaining = max(0.0, ss.ack_until - time.time())
_ack_note = (f"  *(silenced — {int(_ack_remaining // 60)}m {int(_ack_remaining % 60)}s remaining)*"
             if _ack_remaining > 0
             else "  |  *Acknowledge in sidebar to silence for 5 min*")

if ss.alarm_pump_active:
    st.error(f"PUMP STALL — Pump1_Ticks unchanged for {pump_stall_sec}s{_ack_note}")

if ss.alarm_stale_active:
    st.error(f"NO NEW DATA — file unchanged for {stale_sec}s — check Jetson process{_ack_note}")

for rt_row in ss.threshold_alarms_runtime:
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

if alarm.get("mode_changed"):
    st.info(f"MODE CHANGED  ->  {alarm['new_mode']}")

if ss.alarm_log:
    with st.expander(f"Alarm Log ({len(ss.alarm_log)} events)"):
        for entry in reversed(ss.alarm_log[-50:]):
            st.text(entry)
        if st.button("Clear Log"):
            ss.alarm_log = []


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

    with st.expander("Raw Data (last 100 rows)"):
        st.dataframe(df.tail(100), use_container_width=True)

else:
    if ss.connected:
        st.info("Waiting for data — check that the CSV path is correct.")
    else:
        st.markdown("### Connect to your Jetson Nano to begin")
        st.caption("Enter SSH credentials in the sidebar and click Connect.")


# ── Audio render + auto-refresh ───────────────────────────────

audio.render()

if ss.connected:
    time.sleep(int(refresh_sec))
    st.rerun()