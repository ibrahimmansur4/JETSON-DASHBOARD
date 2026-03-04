"""
=============================================================
  Fake Jetson Nano — CSV Data Generator for Local Testing
  Simulates all 41 sensor columns from the real device.
  Run this in Terminal 1, then run the dashboard in Terminal 2.
=============================================================

WHAT THIS SIMULATES:
  - Realistic sensor noise on all channels
  - Mode switching every ~40 seconds  → triggers mode-change alert (1s tone + voice)
  - Mode "FT" at t=80s                → pump ticks FREEZE but NO pump alarm
                                         (FT is in excluded-modes list)
  - Pump stall at t=160s (in RUNNING) → triggers pump stall alarm after 15s
  - Outlet temperature spike at t=220s→ triggers over-temp alarm
  - STALE DATA at t=290s              → script pauses writing for 30s
                                         → triggers no-data alarm after 25s
  - Normal recovery after each event

HOW TO USE:
  Terminal 1:  python fake_jetson.py
  Terminal 2:  streamlit run dashboard.py

  Dashboard sidebar settings:
    Host:        127.0.0.1
    Username:    <your Windows username>  (run 'whoami' in cmd)
    Password:    <your Windows password>
    CSV Path:    C:/path/shown/below/sensors.csv
    Timestamp:   Timestamp
"""

import csv
import time
import math
import random
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_PATH   = os.path.join(SCRIPT_DIR, "sensors.csv")
INTERVAL   = 2.0

HEADER = [
    "Timestamp", "LogPhase", "Current Mode", "Expected MCU Version",
    "Actual MCU Version", "Temperature_Inlet", "Temperature_Outlet",
    "Heater_PWM", "Pressure1", "Pressure2", "Pressure3", "Pressure4",
    "Pressure5", "Pressure6", "Pressure7", "Conductivity1", "Conductivity2",
    "Conductivity3", "Bubble1_Status", "Bubble2_Status",
    "Temperature C1/T1", "Temperature T2", "Temperature T3", "Temperature T4",
    "Temperature C2/T5", "Skin Temperature 1", "Skin Temperature 2",
    "Skin Temperature 3", "Skin Temperature 4", "Flow Sensor",
    "Pump1_Ticks", "NTC_1", "NTC_2", "NTC_3", "NTC_4", "NTC_5", "NTC_6",
    "Load Cell Raw", "Load Cell Ref", "Load Cell Relative", "Volume Calculated"
]

# Mode timeline — each entry is (start_seconds, mode_name)
# Dashboard's excluded list is: FT, STANDBY, PRIMING
# So during FT the pump ticks can freeze without triggering alarm.
MODE_TIMELINE = [
    (  0, "STANDBY"),
    ( 40, "PRIMING"),
    ( 80, "FT"),        # pump ticks freeze here — should NOT alarm
    (120, "RUNNING"),   # back to running — stall alarm should reset
    (160, "RUNNING"),   # pump stall injected here (same mode, ticks freeze)
    (220, "HEATING"),   # outlet temp spike starts here
    (290, "RUNNING"),   # stale-data pause starts here
    (320, "FLUSHING"),  # recovery
]

def get_mode(elapsed):
    mode = "STANDBY"
    for start, m in MODE_TIMELINE:
        if elapsed >= start:
            mode = m
    return mode

def noise(scale=1.0):
    return random.gauss(0, scale)

def clamp(val, lo, hi):
    return max(lo, min(hi, val))


class ScenarioController:
    def __init__(self):
        self.start = time.time()

    def elapsed(self):
        return time.time() - self.start

    def log_phase(self):
        e = self.elapsed()
        if   e < 80:  return "INIT"
        elif e < 160: return "PHASE_1"
        elif e < 290: return "PHASE_2"
        else:          return "PHASE_3"

    def pump_ticks(self, base):
        e    = self.elapsed()
        mode = get_mode(e)

        # In FT mode: freeze ticks (no pump), but alarm should be suppressed by dashboard
        if mode == "FT":
            return base  # frozen — dashboard ignores because FT is excluded

        # Pump stall test window in RUNNING mode: t=160–185
        if 160 <= e <= 185:
            return base  # frozen — THIS should trigger alarm (RUNNING not excluded)

        increment = 5 + noise(0.5)
        return base + max(0, increment)

    def outlet_temp(self, base):
        e = self.elapsed()
        if 220 <= e <= 245:
            progress = (e - 220) / 25.0
            return clamp(base + progress * 58 + noise(0.5), 36, 96)
        elif 245 < e <= 265:
            progress = (e - 245) / 20.0
            return clamp(96 - progress * 59 + noise(0.8), 36, 97)
        return base + noise(0.3)

    def heater_pwm(self):
        e = self.elapsed()
        if 220 <= e <= 265:
            return clamp(85 + noise(3), 75, 100)
        return clamp(35 + 10 * math.sin(e / 30) + noise(2), 0, 100)

    def should_pause_writing(self):
        """Return True during the stale-data test window (t=290–322s)."""
        e = self.elapsed()
        return 290 <= e <= 322


def generate_row(row_count, scenario, pump_ticks_state):
    e   = scenario.elapsed()
    now = time.strftime("%Y-%m-%d %H:%M:%S")

    t_base   = 36.5 + noise(0.2)
    t_inlet  = clamp(t_base + 0.5 * math.sin(e / 20) + noise(0.3), 34, 40)
    t_outlet = scenario.outlet_temp(t_base + 0.8)

    p_base   = [1013.5, 1012.8, 1014.1, 1011.9, 1013.2, 1012.5, 1014.8]
    pressures = [clamp(p + 2 * math.sin(e / 15 + i) + noise(0.4), 1005, 1025)
                 for i, p in enumerate(p_base)]

    cond = [
        clamp(1.42 + 0.05 * math.sin(e / 25) + noise(0.01), 1.30, 1.60),
        clamp(1.38 + 0.04 * math.cos(e / 22) + noise(0.01), 1.25, 1.55),
        clamp(1.45 + 0.06 * math.sin(e / 20) + noise(0.01), 1.30, 1.65),
    ]

    bub1 = 1 if random.random() < 0.05 else 0
    bub2 = 1 if random.random() < 0.03 else 0

    temps = [clamp(t_base + i * 0.3 + noise(0.25), 34, 42) for i in range(5)]
    skin  = [clamp(34.0 + i * 0.2 + noise(0.3), 32, 38) for i in range(4)]
    flow  = clamp(120 + 10 * math.sin(e / 18) + noise(2), 90, 160)

    new_ticks = scenario.pump_ticks(pump_ticks_state)
    ntc = [clamp(37.0 + i * 0.4 + 0.5 * math.sin(e / (12 + i)) + noise(0.2), 34, 42)
           for i in range(6)]

    lc_raw      = clamp(2048 + 50 * math.sin(e / 40) + noise(5), 1900, 2200)
    lc_ref      = 2048.0
    lc_relative = lc_raw - lc_ref
    volume      = clamp(new_ticks * 0.0025 + noise(0.001), 0, 500)

    row = [
        now, scenario.log_phase(), get_mode(e), "v2.4.1", "v2.4.1",
        round(t_inlet,  4), round(t_outlet, 4),
        round(scenario.heater_pwm(), 2),
        *[round(p, 4) for p in pressures],
        *[round(c, 5) for c in cond],
        bub1, bub2,
        *[round(t, 4) for t in temps],
        *[round(s, 4) for s in skin],
        round(flow, 3), round(new_ticks, 1),
        *[round(n, 4) for n in ntc],
        round(lc_raw, 2), round(lc_ref, 2), round(lc_relative, 4),
        round(volume, 4),
    ]
    return row, new_ticks


def main():
    print("=" * 65)
    print("  Fake Jetson Nano — Sensor Data Generator")
    print("=" * 65)
    print(f"\n  CSV path:  {CSV_PATH}")
    print(f"  Interval:  {INTERVAL}s per row")
    print()
    print("  ALARM / EVENT SCHEDULE:")
    print("    t=40s   — Mode: STANDBY → PRIMING (mode alert, 1s tone)")
    print("    t=80s   — Mode: PRIMING → FT  (pump ticks FREEZE — no alarm)")
    print("    t=120s  — Mode: FT → RUNNING   (pump timer resets on mode exit)")
    print("    t=160s  — Pump STALL begins in RUNNING  → alarm after 15s")
    print("    t=185s  — Pump recovers")
    print("    t=220s  — Mode: HEATING + outlet temp spike → over-temp alarm")
    print("    t=265s  — Temperature recovers")
    print("    t=290s  — STALE DATA: writing paused for 32s → alarm after 25s")
    print("    t=322s  — Writing resumes, stale alarm clears")
    print()
    print("  Dashboard sidebar:")
    print(f"    Host:        127.0.0.1")
    print(f"    CSV Path:    {CSV_PATH.replace(os.sep, '/')}")
    print(f"    Timestamp:   Timestamp")
    print(f"    Excluded pump modes: FT, STANDBY, PRIMING")
    print()
    print("  Press Ctrl+C to stop.\n")
    print("-" * 65)

    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(HEADER)
    print(f"  Created: {CSV_PATH}\n")

    scenario   = ScenarioController()
    pump_ticks = 0.0
    row_count  = 0

    try:
        while True:
            e    = scenario.elapsed()
            mode = get_mode(e)

            # ── Stale data test: hold writing for 32 seconds ──────────────
            if scenario.should_pause_writing():
                elapsed_int = int(e)
                secs_left   = int(322 - e)
                print(
                    f"  ⏸  WRITING PAUSED (stale-data test) — {secs_left}s remaining | "
                    f"t={e:.1f}s | Mode: {mode}",
                    flush=True
                )
                time.sleep(INTERVAL)
                continue

            row, pump_ticks = generate_row(row_count, scenario, pump_ticks)

            with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(row)

            row_count += 1
            t_out      = row[6]
            stalled    = "⚠ STALL" if (160 <= e <= 185) else "      "
            ft_freeze  = "❄ FT (no alarm)" if mode == "FT" else "               "
            over_temp  = "🔥 OVER TEMP" if float(t_out) > 90 else "           "

            print(
                f"  Row {row_count:>4} | t={e:>6.1f}s | Mode: {mode:<10} | "
                f"Outlet: {float(t_out):>6.2f}°C {over_temp} | "
                f"Ticks: {pump_ticks:>8.1f} {stalled}{ft_freeze}",
                flush=True
            )
            time.sleep(INTERVAL)

    except KeyboardInterrupt:
        print(f"\n\n  Stopped. {row_count} rows written to:\n  {CSV_PATH}")


if __name__ == "__main__":
    main()

