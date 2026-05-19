"""
AM4096 Encoder – Real-Time Visualizer
======================================
Reads the serial output produced by encoder_arduino.ino and displays:
  • A live gauge (dial) showing the current angle
  • A scrolling angle history plot (last N seconds)
  • Live readouts for angle, raw count, RPM, and rotation direction

Usage
-----
  python encoder_visualizer.py                   # auto-detects the first Arduino port
  python encoder_visualizer.py --port COM3       # Windows
  python encoder_visualizer.py --port /dev/ttyACM0   # Linux
  python encoder_visualizer.py --port /dev/cu.usbmodem1401  # macOS
  python encoder_visualizer.py --demo            # run without hardware (demo mode)

Requirements
------------
  pip install pyserial matplotlib
"""

import argparse
import collections
import math
import re
import sys
import threading
import time

import matplotlib
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.gridspec as gridspec
import numpy as np
from matplotlib.patches import FancyArrowPatch
from matplotlib.animation import FuncAnimation

# ── Serial line parser ──────────────────────────────────────────────────────
# Matches: "Raw: 2048 / 4095   |   Angle: 180.00 °"
_LINE_RE = re.compile(r"Raw:\s*(\d+).*?Angle:\s*([\d.]+)")

# ── Configuration ───────────────────────────────────────────────────────────
BAUD_RATE    = 115200
HISTORY_SEC  = 10          # seconds of scrolling history to show
UPDATE_MS    = 80          # animation interval in ms (~12 fps)
MAX_PTS      = 500         # max points kept in the rolling buffer


# ════════════════════════════════════════════════════════════════════════════
# Data acquisition (runs in a background thread)
# ════════════════════════════════════════════════════════════════════════════

class EncoderReader:
    """Reads lines from the serial port and exposes the latest values."""

    def __init__(self, port, baud=BAUD_RATE, demo=False):
        self.demo       = demo
        self.port       = port
        self.baud       = baud
        self.lock       = threading.Lock()
        self._angle     = 0.0
        self._raw       = 0
        self._times     = collections.deque(maxlen=MAX_PTS)
        self._angles    = collections.deque(maxlen=MAX_PTS)
        self._running   = False
        self._thread    = None
        self._t0        = time.monotonic()

    # ── public interface (called from main thread) ──────────────────────────

    @property
    def angle(self):
        with self.lock:
            return self._angle

    @property
    def raw(self):
        with self.lock:
            return self._raw

    def history(self):
        """Return (times, angles) arrays relative to now."""
        with self.lock:
            t = np.array(self._times)
            a = np.array(self._angles)
        if len(t):
            t = t - t[-1]          # make the latest sample t=0
        return t, a

    def start(self):
        self._running = True
        self._thread  = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    # ── background thread ───────────────────────────────────────────────────

    def _push(self, raw, angle):
        with self.lock:
            self._raw   = raw
            self._angle = angle
            self._times.append(time.monotonic() - self._t0)
            self._angles.append(angle)

    def _run(self):
        if self.demo:
            self._run_demo()
        else:
            self._run_serial()

    def _run_serial(self):
        try:
            import serial as _serial
            with _serial.Serial(self.port, self.baud, timeout=1) as ser:
                print(f"[encoder] connected to {self.port} @ {self.baud} baud")
                while self._running:
                    try:
                        line = ser.readline().decode("utf-8", errors="ignore").strip()
                    except Exception as exc:
                        print(f"[encoder] read error: {exc}")
                        time.sleep(0.1)
                        continue
                    m = _LINE_RE.search(line)
                    if m:
                        self._push(int(m.group(1)), float(m.group(2)))
        except Exception as exc:
            print(f"[encoder] serial error: {exc}")
            print("[encoder] switching to demo mode")
            self._run_demo()

    def _run_demo(self):
        """Simulates a smooth spin + wobble so the GUI can be tested without hardware."""
        print("[encoder] running in DEMO mode – no hardware needed")
        t = 0.0
        while self._running:
            # slow rotation with a slight oscillation
            angle = (t * 30 + 20 * math.sin(t * 0.7)) % 360
            raw   = int(angle * 4096 / 360)
            self._push(raw, angle)
            t += 0.1
            time.sleep(0.1)


# ════════════════════════════════════════════════════════════════════════════
# Helper – auto-detect Arduino serial port
# ════════════════════════════════════════════════════════════════════════════

def auto_detect_port():
    """Return the first plausible Arduino/USB-serial port, or None."""
    try:
        from serial.tools import list_ports
        candidates = []
        for p in list_ports.comports():
            desc = (p.description or "").lower()
            hwid = (p.hwid or "").lower()
            if any(k in desc or k in hwid for k in
                   ("arduino", "ch340", "cp210", "ftdi", "acm", "usbmodem")):
                candidates.append(p.device)
        if candidates:
            return candidates[0]
    except Exception:
        pass
    return None


# ════════════════════════════════════════════════════════════════════════════
# Gauge drawing helpers
# ════════════════════════════════════════════════════════════════════════════

def _polar_xy(cx, cy, r, angle_deg):
    """Convert (centre, radius, angle in degrees from top-CW) → (x, y)."""
    rad = math.radians(angle_deg - 90)   # 0° = top, increases clockwise
    return cx + r * math.cos(rad), cy + r * math.sin(rad)


def draw_gauge_background(ax):
    """Draw the static parts of the dial (tick marks, arc, labels)."""
    ax.set_xlim(-1.25, 1.25)
    ax.set_ylim(-1.25, 1.25)
    ax.set_aspect("equal")
    ax.axis("off")

    # Outer ring
    ring = plt.Circle((0, 0), 1.05, color="#2c2c2c", fill=False, linewidth=3, zorder=1)
    ax.add_patch(ring)

    # Coloured arc background (light grey)
    theta = np.linspace(-np.pi / 2, 3 * np.pi / 2, 360)
    ax.fill_between(np.cos(theta) * 0.88, np.sin(theta) * 0.88,
                    np.cos(theta) * 1.0,  np.sin(theta) * 1.0,
                    color="#1a1a2e", alpha=0.9, zorder=2)

    # Degree ticks and labels
    for deg in range(0, 360, 10):
        is_major = (deg % 30 == 0)
        r_outer = 1.0
        r_inner = 0.88 if is_major else 0.91
        x0, y0 = _polar_xy(0, 0, r_inner, deg)
        x1, y1 = _polar_xy(0, 0, r_outer, deg)
        ax.plot([x0, x1], [y0, y1],
                color="white" if is_major else "#888888",
                linewidth=1.5 if is_major else 0.7, zorder=3)
        if is_major:
            lx, ly = _polar_xy(0, 0, 0.77, deg)
            ax.text(lx, ly, f"{deg}°",
                    ha="center", va="center", fontsize=7.5,
                    color="#cccccc", fontweight="bold", zorder=4)

    # Centre boss
    boss = plt.Circle((0, 0), 0.06, color="#e0e0e0", zorder=10)
    ax.add_patch(boss)
    boss2 = plt.Circle((0, 0), 0.04, color="#555555", zorder=11)
    ax.add_patch(boss2)


# ════════════════════════════════════════════════════════════════════════════
# Main visualizer
# ════════════════════════════════════════════════════════════════════════════

class EncoderVisualizer:

    def __init__(self, reader: EncoderReader):
        self.reader = reader

        # ── figure layout ──────────────────────────────────────────────────
        self.fig = plt.figure(figsize=(13, 7), facecolor="#0d0d1a")
        self.fig.canvas.manager.set_window_title("AM4096 Encoder – Live View")

        gs = gridspec.GridSpec(
            2, 2,
            figure=self.fig,
            left=0.04, right=0.97,
            top=0.93,  bottom=0.10,
            wspace=0.35, hspace=0.45,
        )

        self.ax_gauge   = self.fig.add_subplot(gs[:, 0])   # full left column
        self.ax_history = self.fig.add_subplot(gs[0, 1])   # top-right
        self.ax_info    = self.fig.add_subplot(gs[1, 1])   # bottom-right

        for ax in (self.ax_gauge, self.ax_history, self.ax_info):
            ax.set_facecolor("#0d0d1a")

        # ── title ──────────────────────────────────────────────────────────
        self.fig.text(0.5, 0.97, "AM4096  ·  12-bit Angular Encoder  ·  Live View",
                      ha="center", va="top",
                      fontsize=13, color="#e0e0e0", fontweight="bold")

        # ── gauge static background ────────────────────────────────────────
        draw_gauge_background(self.ax_gauge)

        # dynamic needle (will be redrawn each frame)
        self._needle_line, = self.ax_gauge.plot(
            [], [], color="#ff4444", linewidth=3, solid_capstyle="round", zorder=8)
        self._needle_shadow, = self.ax_gauge.plot(
            [], [], color="#880000", linewidth=5, solid_capstyle="round",
            alpha=0.4, zorder=7)

        # coloured arc showing current angle
        self._arc_patch = None

        # centre readout text
        self._angle_text = self.ax_gauge.text(
            0, -0.35, "0.00°",
            ha="center", va="center", fontsize=22,
            color="#ff4444", fontweight="bold", zorder=12)
        self._raw_text = self.ax_gauge.text(
            0, -0.55, "raw: 0",
            ha="center", va="center", fontsize=10,
            color="#888888", zorder=12)

        # ── history plot ────────────────────────────────────────────────────
        self.ax_history.set_facecolor("#111128")
        self.ax_history.set_ylim(-5, 365)
        self.ax_history.set_xlim(-HISTORY_SEC, 0)
        self.ax_history.set_ylabel("Angle (°)", color="#aaaaaa", fontsize=9)
        self.ax_history.set_xlabel("Time (s)", color="#aaaaaa", fontsize=9)
        self.ax_history.set_title("Angle history", color="#cccccc",
                                   fontsize=10, pad=6)
        self.ax_history.tick_params(colors="#888888", labelsize=8)
        for spine in self.ax_history.spines.values():
            spine.set_edgecolor("#333355")
        self.ax_history.yaxis.set_major_locator(
            matplotlib.ticker.MultipleLocator(90))
        self.ax_history.yaxis.set_minor_locator(
            matplotlib.ticker.MultipleLocator(30))
        self.ax_history.grid(which="major", color="#222244", linewidth=0.6)
        self.ax_history.grid(which="minor", color="#1a1a33", linewidth=0.3)
        self._hist_line, = self.ax_history.plot(
            [], [], color="#4488ff", linewidth=1.4, zorder=3)

        # ── info panel ──────────────────────────────────────────────────────
        self.ax_info.axis("off")
        self._build_info_panel()

        # state for RPM calculation
        self._prev_angle = 0.0
        self._prev_time  = time.monotonic()
        self._rpm        = 0.0

        # animation
        self._anim = FuncAnimation(
            self.fig, self._update,
            interval=UPDATE_MS, blit=False, cache_frame_data=False)

    # ── info panel ───────────────────────────────────────────────────────────

    def _build_info_panel(self):
        ax = self.ax_info
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)

        style = dict(ha="left", va="center", fontsize=10,
                     transform=ax.transAxes)
        label_style = dict(**style, color="#888888")
        value_style = dict(**style, color="#e0e0e0", fontweight="bold")

        labels = ["Angle", "Raw count", "Speed (RPM)", "Direction"]
        self._info_labels = []
        self._info_values = []

        for i, lbl in enumerate(labels):
            y = 0.82 - i * 0.20
            ax.text(0.04, y, lbl, **label_style)
            t = ax.text(0.52, y, "—", **value_style)
            self._info_values.append(t)

        ax.set_title("Live readout", color="#cccccc", fontsize=10, pad=6)

        # divider lines (in axes-fraction coords via transAxes + plot)
        for i in range(len(labels) + 1):
            y = 0.96 - i * 0.20
            ax.plot([0, 1], [y, y], color="#222244", linewidth=0.8,
                    transform=ax.transAxes, clip_on=False)

    # ── animation update ─────────────────────────────────────────────────────

    def _update(self, frame):
        angle = self.reader.angle
        raw   = self.reader.raw

        # ── 1. Gauge needle ─────────────────────────────────────────────────
        nx, ny = _polar_xy(0, 0, 0.84, angle)
        self._needle_line.set_data([0, nx], [0, ny])
        self._needle_shadow.set_data([0, nx * 0.98], [0, ny * 0.98])

        # Coloured arc from 0° to current angle
        if self._arc_patch:
            self._arc_patch.remove()
        if angle > 0.5:
            a_start = 90                     # matplotlib angles: CCW from East
            a_end   = 90 - angle
            self._arc_patch = mpatches.Wedge(
                (0, 0), 0.94, a_end, a_start,
                width=0.06,
                facecolor="#4488ff", alpha=0.35, zorder=5)
            self.ax_gauge.add_patch(self._arc_patch)
        else:
            self._arc_patch = None

        self._angle_text.set_text(f"{angle:.2f}°")
        self._raw_text.set_text(f"raw: {raw}  /  4095")

        # ── 2. History plot ─────────────────────────────────────────────────
        t_arr, a_arr = self.reader.history()
        if len(t_arr) >= 2:
            self._hist_line.set_data(t_arr, a_arr)
            # keep x-axis anchored so latest sample is at t=0
            self.ax_history.set_xlim(t_arr[0], 0)

        # ── 3. RPM / direction calculation ─────────────────────────────────
        now = time.monotonic()
        dt  = now - self._prev_time
        if dt > 0.15:                        # update every ~150 ms
            da = angle - self._prev_angle
            # handle wrap-around
            if da >  180: da -= 360
            if da < -180: da += 360
            self._rpm       = abs(da / 360.0 / dt * 60.0)
            direction       = ("CW ↻" if da >= 0 else "CCW ↺") if abs(da) > 0.5 else "stopped"
            self._prev_angle = angle
            self._prev_time  = now

            self._info_values[0].set_text(f"{angle:.2f}°")
            self._info_values[1].set_text(f"{raw}  /  4095")
            self._info_values[2].set_text(f"{self._rpm:.1f}")
            self._info_values[3].set_text(direction)
            # colour the direction text
            self._info_values[3].set_color(
                "#ff6666" if "CW" in direction and "CCW" not in direction
                else "#66aaff" if "CCW" in direction
                else "#888888")

    def show(self):
        plt.show()


# ════════════════════════════════════════════════════════════════════════════
# Entry point
# ════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Real-time visualizer for the AM4096 encoder via Arduino SSI sketch")
    parser.add_argument("--port",  default=None,
                        help="Serial port (e.g. COM3, /dev/ttyACM0). "
                             "Auto-detected if omitted.")
    parser.add_argument("--baud",  type=int, default=BAUD_RATE,
                        help=f"Baud rate (default {BAUD_RATE})")
    parser.add_argument("--demo",  action="store_true",
                        help="Run in demo mode without hardware")
    args = parser.parse_args()

    demo = args.demo
    port = args.port

    if not demo:
        if port is None:
            port = auto_detect_port()
        if port is None:
            print("[encoder] Could not auto-detect a serial port.")
            print("          Connect your Arduino and retry, or use --port, or --demo.")
            sys.exit(1)
        print(f"[encoder] using port: {port}")

    reader = EncoderReader(port=port, baud=args.baud, demo=demo)
    reader.start()

    try:
        viz = EncoderVisualizer(reader)
        viz.show()
    finally:
        reader.stop()
        print("[encoder] stopped.")


if __name__ == "__main__":
    main()
