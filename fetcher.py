# =============================================================
#  fetcher.py  —  Primary-fetcher SFTP polling coordinator
#
#  In a multi-session deployment, exactly ONE session performs
#  SFTP reads and writes the result to shared state.  Other
#  sessions read from the shared Parquet file.
#
#  The primary fetcher is elected via shared_state.claim_fetcher().
#  If the current fetcher's heartbeat goes stale (>15 s), another
#  session auto-promotes.
# =============================================================

import time
from datetime import datetime

import pandas as pd

import sftp_reader
import shared_state
import config as cfg
import alarms


class DataFetcher:
    """Coordinates SFTP polling for the primary fetcher session."""

    def __init__(self, session_id):
        self.session_id = session_id

    def try_become_fetcher(self):
        """Attempt to become the primary fetcher."""
        return shared_state.claim_fetcher(self.session_id)

    def release(self):
        """Release the fetcher role."""
        shared_state.release_fetcher(self.session_id)

    def is_primary(self):
        """Check if this session is the current primary fetcher."""
        state = shared_state.read_shared()
        return state.get("fetcher_id") == self.session_id

    def tick(self, ssh_client, sftp_session):
        """
        Perform one fetch cycle. Called by the primary fetcher's Streamlit
        re-run loop.

        Returns (sftp_session, df, fetch_error_str | None).
        The caller should update its session_state.sftp with the returned
        sftp_session in case it was re-opened.
        """
        state = shared_state.read_shared()

        # Refresh our fetcher heartbeat
        shared_state.claim_fetcher(self.session_id)

        remote_path = state.get("remote_csv_path", cfg.REMOTE_CSV_PATH)
        max_rows = state.get("max_rows", cfg.MAX_ROWS)
        limit = int(max_rows) if int(max_rows) > 0 else 50_000

        try:
            sftp = sftp_reader.open_sftp(ssh_client, sftp_session)
            fsize = sftp_reader.file_size(sftp, remote_path)

            last_row_count = state.get("last_row_count", 0)
            last_file_size = state.get("last_file_size", 0)
            byte_offset = state.get("file_byte_offset", 0)

            df = shared_state.get_shared_dataframe()

            # First load, or file was truncated / rotated
            if last_row_count == 0 or fsize < last_file_size:
                df_new, offset, err = sftp_reader.fetch_initial(sftp, remote_path, limit)
                if err:
                    return sftp, df, f"Read warning: {err}"
                if not df_new.empty:
                    df = df_new
                    shared_state.update_shared(
                        last_row_count=len(df_new),
                        file_byte_offset=offset,
                        last_file_size=fsize,
                        fetch_errors=0,
                    )
                    shared_state.set_shared_dataframe(df)
            else:
                # Incremental fetch — only new bytes since last read
                df_inc, new_offset, err = sftp_reader.fetch_incremental(
                    sftp, remote_path, byte_offset
                )
                if err:
                    errs = state.get("fetch_errors", 0) + 1
                    shared_state.update_shared(fetch_errors=errs)
                    return sftp, df, err
                if df_inc is not None and not df_inc.empty:
                    if not df.empty:
                        expected = df.columns.tolist()
                        df_inc = df_inc[
                            df_inc.apply(lambda r: r.notna().sum(), axis=1) == len(expected)
                        ]
                        if not df_inc.empty:
                            df_inc.columns = expected
                            combined = pd.concat([df, df_inc], ignore_index=True)
                            if limit > 0 and len(combined) > limit:
                                combined = combined.iloc[-limit:]
                            df = combined

                    shared_state.update_shared(
                        file_byte_offset=new_offset,
                        last_file_size=fsize,
                        last_row_count=len(df),
                        fetch_errors=0,
                    )
                    shared_state.set_shared_dataframe(df)

            return sftp, df, None

        except Exception as e:
            errs = state.get("fetch_errors", 0) + 1
            shared_state.update_shared(fetch_errors=errs)
            return sftp_session, shared_state.get_shared_dataframe(), str(e)

    def evaluate_alarms(self, df, alarm_state, alarms_on, pump_stall_sec, stale_sec):
        """
        Evaluate alarms using the shared alarm state dict.
        Returns (alarm_state, alarm_events) where alarm_events is a dict of
        events that occurred this cycle.

        alarm_state is a dict-like object (not st.session_state).
        """
        events = {}
        fsize = alarm_state.get("last_file_size", 0)

        if df.empty or not alarms_on:
            return alarm_state, events

        acked = time.time() < alarm_state.get("ack_until", 0.0)

        # Pump stall
        pump_firing = alarms.eval_pump_stall(
            df, alarm_state, float(pump_stall_sec), cfg.PUMP_EXCLUDED_MODES
        )
        if pump_firing:
            if not alarm_state.get("alarm_pump_active"):
                alarm_state["alarm_pump_active"] = True
                entry = f"[{datetime.now():%H:%M:%S}] PUMP STALL — ticks unchanged {pump_stall_sec}s"
                shared_state.append_alarm_log(entry)
            if not acked:
                events["alarm_pump"] = True
        else:
            alarm_state["alarm_pump_active"] = False

        # Stale data
        stale_firing = alarms.eval_stale_data(
            fsize, alarm_state, float(stale_sec), cfg.STALE_EXCLUDED_MODES, df
        )
        if stale_firing:
            if not alarm_state.get("alarm_stale_active"):
                alarm_state["alarm_stale_active"] = True
                entry = f"[{datetime.now():%H:%M:%S}] NO DATA — file unchanged {stale_sec}s"
                shared_state.append_alarm_log(entry)
            if not acked:
                events["alarm_stale"] = True
        else:
            alarm_state["alarm_stale_active"] = False

        # Threshold alarms
        rt_alarms = alarm_state.get("threshold_alarms_runtime", [])
        any_threshold = False
        for i, rt_row in enumerate(rt_alarms):
            cfg_exclude = (
                cfg.THRESHOLD_ALARMS[i].get("exclude", set())
                if i < len(cfg.THRESHOLD_ALARMS) else set()
            )
            merged = {**rt_row, "exclude": cfg_exclude}
            tripped, msg = alarms.eval_threshold(df, merged)
            if tripped:
                if not rt_row.get("active"):
                    rt_row["active"] = True
                    entry = f"[{datetime.now():%H:%M:%S}] THRESHOLD — {msg}"
                    shared_state.append_alarm_log(entry)
                any_threshold = True
            else:
                rt_row["active"] = False

        if any_threshold and not acked:
            events["alarm_threshold"] = True

        # Mode change
        mode_changed, new_mode = alarms.eval_mode_change(df, alarm_state)
        if mode_changed:
            entry = f"[{datetime.now():%H:%M:%S}] MODE -> {new_mode}"
            shared_state.append_alarm_log(entry)
            events["mode_changed"] = new_mode

        return alarm_state, events
