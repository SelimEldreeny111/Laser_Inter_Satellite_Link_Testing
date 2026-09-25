"""Professional desktop controller for the STM32F401 X-Z laser stage."""

from __future__ import annotations

import os
import math
import queue
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

import serial
from serial.tools import list_ports

try:
    from .protocol import (
        build_absolute_mm,
        build_accel_mm,
        build_jog_um,
        build_jog_mm,
        build_relative_mm,
        build_speed_mm,
        DEFAULT_PULSES_PER_MM,
        finite_float,
        format_number,
        MAX_ACCEL_MM_S2,
        MAX_SPEED_MM_S,
        MIN_ACCEL_MM_S2,
        MIN_SPEED_MM_S,
        motion_grid_value,
        parse_idle_event,
        parse_status,
        pulse_resolution_um,
        sanitize_raw_command,
        steps_to_mm,
    )
    from .gpib_visa import (
        E5574AVisa,
        MEASUREMENT_APPLICATIONS,
        PowerReading,
        VisaUnavailableError,
        list_visa_resources,
    )
    from .session_excel import (
        BEAM_POWER_BANDS,
        WEATHER_COLOR_STOPS,
        SessionEventRecord,
        SessionExcelReport,
        SessionMeasurementRecord,
        radial_average_profile,
    )
    from .excel_beam_plotter import (
        BeamPlotExportResult,
        BeamWorkbookDataset,
        export_plotted_workbook,
        load_beam_workbook,
    )
except ImportError:  # Allows ``python laser_stage_gui.py`` from this directory.
    from protocol import (  # type: ignore
        build_absolute_mm,
        build_accel_mm,
        build_jog_um,
        build_jog_mm,
        build_relative_mm,
        build_speed_mm,
        DEFAULT_PULSES_PER_MM,
        finite_float,
        format_number,
        MAX_ACCEL_MM_S2,
        MAX_SPEED_MM_S,
        MIN_ACCEL_MM_S2,
        MIN_SPEED_MM_S,
        motion_grid_value,
        parse_idle_event,
        parse_status,
        pulse_resolution_um,
        sanitize_raw_command,
        steps_to_mm,
    )
    from gpib_visa import (  # type: ignore
        E5574AVisa,
        MEASUREMENT_APPLICATIONS,
        PowerReading,
        VisaUnavailableError,
        list_visa_resources,
    )
    from session_excel import (  # type: ignore
        BEAM_POWER_BANDS,
        WEATHER_COLOR_STOPS,
        SessionEventRecord,
        SessionExcelReport,
        SessionMeasurementRecord,
        radial_average_profile,
    )
    from excel_beam_plotter import (  # type: ignore
        BeamPlotExportResult,
        BeamWorkbookDataset,
        export_plotted_workbook,
        load_beam_workbook,
    )


APP_TITLE = "Laser X-Z Motion Controller"
BAUD_RATE = 115200
MINIMUM_FIRMWARE_VERSION = (2, 5)
STATUS_PERIOD_MS = 400
QUEUE_PERIOD_MS = 30
HANDSHAKE_TIMEOUT_MS = 2500
SESSION_TICK_MS = 100
SESSION_POWER_CHANNEL = 1
SESSION_MEASUREMENT_SETTLE_MAX_S = 0.5
DEFAULT_SCALE = dict(DEFAULT_PULSES_PER_MM)
X_TRAVEL_MM = 200.0
Z_TRAVEL_MM = 200.0
JOG_PULSE_MULTIPLIERS = (1, 2, 4, 8, 16, 80, 160, 800, 1600, 8000)
PLOTTER_VIEW_OPTIONS = (
    "Weather heatmap",
    "Measured point map",
    "X centerline profile",
    "Z centerline profile",
    "Radial average profile",
)
Z_SERPENTINE_RECIPE = "Stepped Z serpentine scan"
X_SERPENTINE_RECIPE = "Stepped X serpentine scan"

APP_BACKGROUND = "#eef2f7"
EGSA_NAVY = "#0b1f3a"
EGSA_BLUE = "#123c69"
EGSA_RED = "#c62828"
TEXT_PRIMARY = "#10213a"
TEXT_MUTED = "#607089"


def resource_path(relative_path: str) -> Path:
    """Resolve a source-tree or PyInstaller-bundled application resource."""
    base_path = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base_path / relative_path


def format_micrometres(value: float) -> str:
    """Format fine-motion values to 0.0001 µm without visual noise."""
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text if text not in {"", "-0"} else "0"


@dataclass(frozen=True)
class SerpentineSessionConfig:
    """Validated parameters for a stepped X- or Z-axis serpentine scan."""

    loops: int
    z_span_mm: float
    z_step_mm: float
    x_step_mm: float
    step_period_s: float
    speed_mm_s: float
    acceleration_mm_s2: float
    scan_axis: str = "Z"
    target_diameter_mm: float = 100.0

    def validate(self) -> None:
        scan_axis = self.scan_axis.upper()
        if scan_axis not in ("X", "Z"):
            raise ValueError("Scan axis must be X or Z")
        if not 1 <= self.loops <= 1000:
            raise ValueError("Loop count must be 1 to 1000")
        if not 1.0 <= self.target_diameter_mm <= min(X_TRAVEL_MM, Z_TRAVEL_MM):
            raise ValueError(
                f"Target beam diameter must be 1 to {min(X_TRAVEL_MM, Z_TRAVEL_MM):g} mm"
            )
        for value, label, axis in (
            (self.scan_span_mm, f"{scan_axis} span", scan_axis),
            (self.scan_step_mm, f"{scan_axis} increment", scan_axis),
            (self.cross_step_mm, f"{self.cross_axis} shift", self.cross_axis),
        ):
            if value <= 0:
                raise ValueError(f"{label} must be greater than zero")
            motion_grid_value(value, label, DEFAULT_SCALE[axis])
        axis_travel = {"X": X_TRAVEL_MM, "Z": Z_TRAVEL_MM}
        if self.scan_span_mm > axis_travel[scan_axis]:
            raise ValueError(
                f"{scan_axis} span cannot exceed {axis_travel[scan_axis]:g} mm"
            )
        if self.cross_step_mm > axis_travel[self.cross_axis]:
            raise ValueError(
                f"{self.cross_axis} shift cannot exceed "
                f"{axis_travel[self.cross_axis]:g} mm"
            )
        steps = self.scan_span_mm / self.scan_step_mm
        if abs(steps - round(steps)) > 1e-6:
            raise ValueError(
                f"{scan_axis} span must be an exact multiple of the "
                f"{scan_axis} increment"
            )
        if not 0.2 <= self.step_period_s <= 3600.0:
            raise ValueError("Post-move measurement dwell must be 0.2 to 3600 seconds")
        if not MIN_SPEED_MM_S <= self.speed_mm_s <= MAX_SPEED_MM_S:
            raise ValueError(
                f"Session speed must be {MIN_SPEED_MM_S:g} to {MAX_SPEED_MM_S:g} mm/s"
            )
        if not MIN_ACCEL_MM_S2 <= self.acceleration_mm_s2 <= MAX_ACCEL_MM_S2:
            raise ValueError(
                "Session acceleration must be "
                f"{MIN_ACCEL_MM_S2:g} to {MAX_ACCEL_MM_S2:g} mm/s²"
            )

    @property
    def cross_axis(self) -> str:
        return "X" if self.scan_axis.upper() == "Z" else "Z"

    @property
    def scan_span_mm(self) -> float:
        return self.z_span_mm

    @property
    def scan_step_mm(self) -> float:
        return self.z_step_mm

    @property
    def cross_step_mm(self) -> float:
        return self.x_step_mm

    @property
    def steps_per_pass(self) -> int:
        return int(round(self.scan_span_mm / self.scan_step_mm))

    @property
    def z_steps_per_pass(self) -> int:
        """Backward-compatible alias retained for existing integrations/tests."""
        return self.steps_per_pass

    @property
    def total_actions(self) -> int:
        return self.loops * (2 * self.steps_per_pass + 2)

    @property
    def total_cross_advance_mm(self) -> float:
        return self.loops * 2.0 * self.cross_step_mm

    @property
    def total_x_advance_mm(self) -> float:
        return self.total_cross_advance_mm if self.cross_axis == "X" else 0.0

    @property
    def estimated_duration_s(self) -> float:
        scan_motion_time = self._ideal_move_time(self.scan_step_mm)
        cross_motion_time = self._ideal_move_time(self.cross_step_mm)
        scheduler_margin_s = 0.02
        # The configured period is a true post-move dwell.  Every motion is
        # confirmed idle, measured through GPIB during this dwell, and only
        # then may the next action begin.
        scan_action_time = scan_motion_time + self.step_period_s + scheduler_margin_s
        cross_action_time = cross_motion_time + self.step_period_s + scheduler_margin_s
        return self.loops * (
            2.0 * self.steps_per_pass * scan_action_time
            + 2.0 * cross_action_time
        )

    def _ideal_move_time(self, distance_mm: float) -> float:
        """Return ideal trapezoidal/triangular motion time for one relative move."""
        distance = abs(distance_mm)
        speed = self.speed_mm_s
        acceleration = self.acceleration_mm_s2
        distance_to_accelerate_and_decelerate = speed * speed / acceleration
        if distance <= distance_to_accelerate_and_decelerate:
            return 2.0 * (distance / acceleration) ** 0.5
        return (
            2.0 * speed / acceleration
            + (distance - distance_to_accelerate_and_decelerate) / speed
        )


@dataclass(frozen=True)
class SessionAction:
    command: str
    label: str
    loop_number: int
    phase: str
    phase_step: int
    phase_steps: int
    pace_seconds: float
    axis: str
    delta_mm: float
    distance_mm: float


def build_serpentine_actions(config: SerpentineSessionConfig) -> list[SessionAction]:
    """Build one deterministic action sequence for the session scheduler."""
    config.validate()
    actions: list[SessionAction] = []
    scan_axis = config.scan_axis.upper()
    cross_axis = config.cross_axis
    scan_steps = config.steps_per_pass
    positive_direction = "up" if scan_axis == "Z" else "right"
    negative_direction = "down" if scan_axis == "Z" else "left"
    for loop_number in range(1, config.loops + 1):
        for step_number in range(1, scan_steps + 1):
            actions.append(
                SessionAction(
                    command=build_jog_mm(scan_axis, config.scan_step_mm),
                    label=(
                        f"Loop {loop_number}: {scan_axis} {positive_direction} "
                        f"{step_number}/{scan_steps}"
                    ),
                    loop_number=loop_number,
                    phase=f"{scan_axis}+ pass",
                    phase_step=step_number,
                    phase_steps=scan_steps,
                    pace_seconds=config.step_period_s,
                    axis=scan_axis,
                    delta_mm=config.scan_step_mm,
                    distance_mm=config.scan_step_mm,
                )
            )
        actions.append(
            SessionAction(
                command=build_jog_mm(cross_axis, config.cross_step_mm),
                label=(
                    f"Loop {loop_number}: {cross_axis}+ shift after "
                    f"{positive_direction} pass"
                ),
                loop_number=loop_number,
                phase=f"{cross_axis}+ shift",
                phase_step=1,
                phase_steps=1,
                pace_seconds=config.step_period_s,
                axis=cross_axis,
                delta_mm=config.cross_step_mm,
                distance_mm=config.cross_step_mm,
            )
        )
        for step_number in range(1, scan_steps + 1):
            actions.append(
                SessionAction(
                    command=build_jog_mm(scan_axis, -config.scan_step_mm),
                    label=(
                        f"Loop {loop_number}: {scan_axis} {negative_direction} "
                        f"{step_number}/{scan_steps}"
                    ),
                    loop_number=loop_number,
                    phase=f"{scan_axis}- pass",
                    phase_step=step_number,
                    phase_steps=scan_steps,
                    pace_seconds=config.step_period_s,
                    axis=scan_axis,
                    delta_mm=-config.scan_step_mm,
                    distance_mm=config.scan_step_mm,
                )
            )
        actions.append(
            SessionAction(
                command=build_jog_mm(cross_axis, config.cross_step_mm),
                label=(
                    f"Loop {loop_number}: {cross_axis}+ shift after "
                    f"{negative_direction} pass"
                ),
                loop_number=loop_number,
                phase=f"{cross_axis}+ shift",
                phase_step=1,
                phase_steps=1,
                pace_seconds=config.step_period_s,
                axis=cross_axis,
                delta_mm=config.cross_step_mm,
                distance_mm=config.cross_step_mm,
            )
        )
    return actions


class StatusBadge(tk.Frame):
    """Small status tile with an explicit colour and text value."""

    COLOURS = {
        "neutral": ("#eef2f6", "#334155"),
        "good": ("#dcfce7", "#166534"),
        "warn": ("#fef3c7", "#92400e"),
        "bad": ("#fee2e2", "#991b1b"),
        "active": ("#dbeafe", "#1d4ed8"),
    }

    def __init__(self, master: tk.Misc, title: str, value: str = "--") -> None:
        super().__init__(master, background="#eef2f6", bd=0, highlightthickness=0)
        self.title_label = tk.Label(
            self,
            text=title,
            font=("Segoe UI", 8),
            background="#eef2f6",
            foreground="#64748b",
        )
        self.title_label.pack(padx=12, pady=(5, 0))
        self.value_label = tk.Label(
            self,
            text=value,
            font=("Segoe UI Semibold", 10),
            background="#eef2f6",
            foreground="#334155",
        )
        self.value_label.pack(padx=12, pady=(0, 6))

    def set(self, value: str, state: str = "neutral") -> None:
        background, foreground = self.COLOURS[state]
        self.configure(background=background)
        self.title_label.configure(background=background)
        self.value_label.configure(text=value, background=background, foreground=foreground)


class VerticalScrolledFrame(ttk.Frame):
    """A width-responsive tab viewport with a visible vertical scrollbar."""

    def __init__(self, master: tk.Misc, *, padding: int = 10) -> None:
        super().__init__(master)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self.canvas = tk.Canvas(
            self,
            background=APP_BACKGROUND,
            highlightthickness=0,
            borderwidth=0,
        )
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.scrollbar = ttk.Scrollbar(
            self, orient="vertical", command=self.canvas.yview
        )
        self.scrollbar.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.content = ttk.Frame(self.canvas, padding=padding)
        self.content_window = self.canvas.create_window(
            (0, 0), window=self.content, anchor="nw"
        )
        self.content.bind("<Configure>", self._content_resized)
        self.canvas.bind("<Configure>", self._viewport_resized)
        self.bind_all("<MouseWheel>", self._mousewheel, add="+")

    def _content_resized(self, _event: tk.Event[tk.Misc]) -> None:
        bounds = self.canvas.bbox("all")
        if bounds is not None:
            self.canvas.configure(scrollregion=bounds)

    def _viewport_resized(self, event: tk.Event[tk.Misc]) -> None:
        self.canvas.itemconfigure(self.content_window, width=max(event.width, 1))

    def _pointer_is_inside(self) -> bool:
        widget = self.winfo_containing(*self.winfo_pointerxy())
        while widget is not None:
            if widget is self:
                return True
            widget = widget.master
        return False

    def _mousewheel(self, event: tk.Event[tk.Misc]) -> str | None:
        if not self._pointer_is_inside() or not event.delta:
            return None
        units = int(-event.delta / 120)
        self.canvas.yview_scroll(units if units else (-1 if event.delta > 0 else 1), "units")
        return "break"

    def scroll_to_top(self) -> None:
        self.canvas.yview_moveto(0.0)


class StageSimulation(ttk.LabelFrame):
    """Animated status-driven schematic of the physical X-Z mechanism."""

    FRAME_MS = 25

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, text="Live stage simulation", padding=10)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        self.summary_var = tk.StringVar(value="X 0.000 mm   |   Z 0.000 mm")
        ttk.Label(self, textvariable=self.summary_var, style="Position.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 6)
        )
        self.canvas = tk.Canvas(
            self,
            width=430,
            height=455,
            background="#f8fafc",
            highlightbackground="#cbd5e1",
            highlightthickness=1,
        )
        self.canvas.grid(row=1, column=0, sticky="nsew")
        self.canvas.bind("<Configure>", lambda _event: self._draw())
        ttk.Label(
            self,
            text=(
                "Controller status drives this schematic. Displayed envelope: "
                "X 0-200 mm, Z 0-200 mm; positive Z is upward."
            ),
            style="Muted.TLabel",
            wraplength=430,
        ).grid(row=2, column=0, sticky="ew", pady=(7, 0))

        self.x_mm = 0.0
        self.z_mm = 0.0
        self.target_x_mm = 0.0
        self.target_z_mm = 0.0
        self.x_speed_mm_s = 20.0
        self.z_speed_mm_s = 20.0
        self.moving = False
        self.have_status = False
        self.last_frame_time = time.monotonic()
        self.after(self.FRAME_MS, self._animate)

    @staticmethod
    def _approach(current: float, target: float, distance: float) -> float:
        if current < target:
            return min(current + distance, target)
        if current > target:
            return max(current - distance, target)
        return current

    def update_status(self, values: dict[str, str]) -> None:
        x = finite_float(values.get("XMM", "0"))
        z = finite_float(values.get("ZMM", "0"))
        target_x = finite_float(values.get("XTMM", str(x)))
        target_z = finite_float(values.get("ZTMM", str(z)))
        busy = values.get("BUSY") == "1"

        # A BUSY status contains the authoritative starting point and target.
        # Between reports, animate locally at the configured controller speed.
        if not self.have_status or not busy:
            self.x_mm = x
            self.z_mm = z
        elif not self.moving:
            self.x_mm = x
            self.z_mm = z

        self.target_x_mm = target_x
        self.target_z_mm = target_z
        self.x_speed_mm_s = max(abs(finite_float(values.get("XVMM", "20"))), 0.001)
        self.z_speed_mm_s = max(abs(finite_float(values.get("ZVMM", "20"))), 0.001)
        self.moving = busy
        self.have_status = True
        self.last_frame_time = time.monotonic()
        self._draw()

    def _animate(self) -> None:
        now = time.monotonic()
        elapsed = min(max(now - self.last_frame_time, 0.0), 0.1)
        self.last_frame_time = now
        if self.moving:
            self.x_mm = self._approach(
                self.x_mm, self.target_x_mm, self.x_speed_mm_s * elapsed
            )
            self.z_mm = self._approach(
                self.z_mm, self.target_z_mm, self.z_speed_mm_s * elapsed
            )
            self._draw()
        self.after(self.FRAME_MS, self._animate)

    @staticmethod
    def _clamp(value: float, lower: float, upper: float) -> float:
        return max(lower, min(value, upper))

    def _draw(self) -> None:
        canvas = self.canvas
        canvas.delete("all")
        width = max(canvas.winfo_width(), 380)
        height = max(canvas.winfo_height(), 360)
        left, right, top, bottom = 48.0, width - 34.0, 64.0, height - 66.0
        rail_y = bottom

        x_ratio = self._clamp(self.x_mm / X_TRAVEL_MM, 0.0, 1.0)
        z_ratio = self._clamp(self.z_mm / Z_TRAVEL_MM, 0.0, 1.0)
        target_x_ratio = self._clamp(self.target_x_mm / X_TRAVEL_MM, 0.0, 1.0)
        target_z_ratio = self._clamp(self.target_z_mm / Z_TRAVEL_MM, 0.0, 1.0)
        x_pixel = left + x_ratio * (right - left)
        z_pixel = rail_y - z_ratio * (rail_y - top)
        target_x_pixel = left + target_x_ratio * (right - left)
        target_z_pixel = rail_y - target_z_ratio * (rail_y - top)

        # Work envelope and measurement grid.
        canvas.create_rectangle(left, top, right, rail_y, outline="#cbd5e1", dash=(4, 4))
        for index in range(1, 4):
            gx = left + index * (right - left) / 4
            gy = top + index * (rail_y - top) / 4
            canvas.create_line(gx, top, gx, rail_y, fill="#e2e8f0")
            canvas.create_line(left, gy, right, gy, fill="#e2e8f0")

        # Target crosshair remains visible while the carriage approaches it.
        canvas.create_line(
            target_x_pixel, top, target_x_pixel, rail_y, fill="#60a5fa", dash=(3, 4)
        )
        canvas.create_line(
            left, target_z_pixel, right, target_z_pixel, fill="#60a5fa", dash=(3, 4)
        )

        # Base, horizontal rail, belt and pulleys.
        canvas.create_rectangle(left - 16, rail_y + 20, right + 16, rail_y + 36, fill="#1e293b", outline="")
        canvas.create_rectangle(left, rail_y - 10, right, rail_y + 10, fill="#334155", outline="#0f172a")
        canvas.create_line(left + 7, rail_y - 4, right - 7, rail_y - 4, fill="#d1d5db", width=3)
        canvas.create_oval(left - 7, rail_y - 13, left + 13, rail_y + 7, fill="#94a3b8", outline="#0f172a")
        canvas.create_oval(right - 13, rail_y - 13, right + 7, rail_y + 7, fill="#94a3b8", outline="#0f172a")

        # Moving X carriage and its Z extrusion.
        canvas.create_rectangle(x_pixel - 25, rail_y - 20, x_pixel + 25, rail_y + 18, fill="#0f172a", outline="#020617")
        canvas.create_rectangle(x_pixel - 12, top - 8, x_pixel + 12, rail_y - 19, fill="#334155", outline="#0f172a")
        canvas.create_line(x_pixel - 5, top, x_pixel - 5, rail_y - 22, fill="#94a3b8", width=2)
        canvas.create_line(x_pixel + 5, top, x_pixel + 5, rail_y - 22, fill="#94a3b8", width=2)
        canvas.create_rectangle(x_pixel - 21, z_pixel - 15, x_pixel + 21, z_pixel + 15, fill="#1d4ed8", outline="#1e3a8a")
        canvas.create_rectangle(x_pixel - 35, z_pixel - 10, x_pixel - 21, z_pixel + 18, fill="#475569", outline="#0f172a")
        canvas.create_oval(x_pixel - 32, z_pixel + 9, x_pixel - 24, z_pixel + 17, fill="#fde047", outline="#a16207")

        motion_text = "MOVING" if self.moving else "IDLE"
        motion_colour = "#2563eb" if self.moving else "#166534"
        canvas.create_text(left, 23, anchor="w", text=motion_text, fill=motion_colour, font=("Segoe UI Semibold", 10))
        canvas.create_text(
            right,
            23,
            anchor="e",
            text=f"Target X {self.target_x_mm:.3f}  |  Z {self.target_z_mm:.3f} mm",
            fill="#475569",
            font=("Segoe UI", 9),
        )
        canvas.create_text(left, rail_y + 50, anchor="w", text="X 0", fill="#475569", font=("Segoe UI", 8))
        canvas.create_text(right, rail_y + 50, anchor="e", text="+X 200 mm  →", fill="#475569", font=("Segoe UI", 8))
        canvas.create_text(left - 8, rail_y, anchor="e", text="Z 0", fill="#475569", font=("Segoe UI", 8))
        canvas.create_text(left - 8, top, anchor="e", text="+Z\n200", fill="#475569", font=("Segoe UI", 8), justify="center")

        if not (0.0 <= self.x_mm <= X_TRAVEL_MM and 0.0 <= self.z_mm <= Z_TRAVEL_MM):
            canvas.create_text(
                (left + right) / 2,
                43,
                text="Position is outside the displayed work envelope",
                fill="#b45309",
                font=("Segoe UI Semibold", 9),
            )
        self.summary_var.set(f"X {self.x_mm:,.3f} mm   |   Z {self.z_mm:,.3f} mm")


class AxisPanel(ttk.LabelFrame):
    """All controls and live feedback for one physical axis."""

    def __init__(self, master: tk.Misc, app: "LaserStageApp", axis: str) -> None:
        super().__init__(master, text=f"{axis} axis", padding=14)
        self.app = app
        self.axis = axis
        self.settings_loaded = False

        default_speed = "20"
        default_acceleration = "40"
        self.pulses_per_mm = DEFAULT_SCALE[axis]
        initial_resolution_um = pulse_resolution_um(self.pulses_per_mm)
        self.position_var = tk.StringVar(value="-- mm")
        self.target_var = tk.StringVar(value="-- mm")
        self.steps_var = tk.StringVar(value="-- STEP pulses")
        self.reference_var = tk.StringVar(value="Coordinate reference not set")
        self.scale_var = tk.StringVar(value=format_number(self.pulses_per_mm))
        self.resolution_var = tk.StringVar(
            value=f" · {format_micrometres(initial_resolution_um)} µm/pulse"
        )
        self.jog_var = tk.StringVar(value=format_micrometres(initial_resolution_um))
        self.jog_slider_var = tk.DoubleVar(value=0.0)
        self.goto_var = tk.StringVar(value="0.0")
        self.speed_var = tk.StringVar(value=default_speed)
        self.accel_var = tk.StringVar(value=default_acceleration)

        self.columnconfigure(1, weight=1)
        self.columnconfigure(3, weight=1)

        ttk.Label(self, text="CURRENT POSITION", style="Section.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w"
        )
        ttk.Label(self, text="TARGET", style="Section.TLabel").grid(
            row=0, column=2, columnspan=2, sticky="w"
        )
        ttk.Label(self, textvariable=self.position_var, style="Position.TLabel").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(0, 2)
        )
        ttk.Label(self, textvariable=self.target_var, style="Position.TLabel").grid(
            row=1, column=2, columnspan=2, sticky="w", pady=(0, 2)
        )
        ttk.Label(self, textvariable=self.steps_var, style="Muted.TLabel").grid(
            row=2, column=0, columnspan=4, sticky="w", pady=(0, 10)
        )

        state_row = ttk.Frame(self)
        state_row.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(0, 10))
        state_row.columnconfigure(0, weight=1)
        ttk.Label(state_row, textvariable=self.reference_var).grid(row=0, column=0, sticky="w")
        calibration = ttk.Frame(state_row)
        calibration.grid(row=0, column=1, padx=(8, 10))
        ttk.Label(calibration, text="Scale:", style="Muted.TLabel").pack(side="left")
        ttk.Label(calibration, textvariable=self.scale_var, style="Muted.TLabel").pack(
            side="left", padx=(4, 2)
        )
        ttk.Label(calibration, text="pulses/mm", style="Muted.TLabel").pack(side="left")
        ttk.Label(
            calibration,
            textvariable=self.resolution_var,
            style="Muted.TLabel",
        ).pack(side="left")
        zero_button = ttk.Button(state_row, text="Set zero", command=self.zero)
        zero_button.grid(row=0, column=2, sticky="e")
        app.register_online_widgets(zero_button)

        ttk.Separator(self).grid(row=4, column=0, columnspan=4, sticky="ew", pady=(0, 10))

        ttk.Label(self, text="Fine jog step").grid(row=5, column=0, sticky="w")
        self.jog_slider = ttk.Scale(
            self,
            from_=0,
            to=len(JOG_PULSE_MULTIPLIERS) - 1,
            variable=self.jog_slider_var,
            command=self._jog_slider_changed,
            orient="horizontal",
            style="Jog.Horizontal.TScale",
        )
        self.jog_slider.grid(
            row=5, column=1, columnspan=2, sticky="ew", padx=(8, 8)
        )
        self.jog_slider.bind("<ButtonRelease-1>", self._snap_jog_slider)
        jog_value = ttk.Frame(self)
        jog_value.grid(row=5, column=3, sticky="e")
        self.jog_spinbox = ttk.Spinbox(
            jog_value,
            from_=initial_resolution_um,
            to=200000.0,
            increment=initial_resolution_um,
            textvariable=self.jog_var,
            width=10,
            command=self._sync_slider_from_jog_entry,
        )
        self.jog_spinbox.pack(side="left")
        self.jog_spinbox.bind("<Return>", self._sync_slider_from_jog_entry)
        self.jog_spinbox.bind("<FocusOut>", self._sync_slider_from_jog_entry)
        ttk.Label(jog_value, text="µm").pack(side="left", padx=(4, 0))

        jog_buttons = ttk.Frame(self)
        jog_buttons.grid(row=6, column=0, columnspan=4, sticky="ew", pady=(8, 12))
        jog_buttons.columnconfigure(0, weight=1)
        jog_buttons.columnconfigure(1, weight=1)
        negative_button = ttk.Button(
            jog_buttons,
            text=f"{axis}  -",
            command=lambda: self.jog(-1),
            style="Jog.TButton",
        )
        negative_button.grid(row=0, column=0, sticky="ew", padx=(0, 5))
        positive_button = ttk.Button(
            jog_buttons,
            text=f"{axis}  +",
            command=lambda: self.jog(1),
            style="Jog.TButton",
        )
        positive_button.grid(row=0, column=1, sticky="ew", padx=(5, 0))
        app.register_motion_widgets(negative_button, positive_button)

        ttk.Label(self, text="Absolute target").grid(row=7, column=0, sticky="w", pady=4)
        goto_entry = ttk.Entry(self, textvariable=self.goto_var, width=12)
        goto_entry.grid(row=7, column=1, sticky="ew", padx=(8, 6), pady=4)
        ttk.Label(self, text="mm").grid(row=7, column=2, sticky="w")
        goto_button = ttk.Button(self, text="Go to", command=self.go_to)
        goto_button.grid(row=7, column=3, sticky="ew", pady=4)
        goto_entry.bind("<Return>", lambda _event: self.go_to())
        app.register_motion_widgets(goto_button)

    def _jog_slider_changed(self, raw_index: str) -> None:
        index = max(0, min(round(float(raw_index)), len(JOG_PULSE_MULTIPLIERS) - 1))
        self.jog_var.set(format_micrometres(self._jog_presets_um()[index]))

    def _snap_jog_slider(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        index = max(
            0,
            min(round(self.jog_slider_var.get()), len(JOG_PULSE_MULTIPLIERS) - 1),
        )
        self.jog_slider_var.set(float(index))
        self.jog_var.set(format_micrometres(self._jog_presets_um()[index]))

    def _jog_presets_um(self) -> tuple[float, ...]:
        resolution_um = pulse_resolution_um(self.pulses_per_mm)
        return tuple(multiplier * resolution_um for multiplier in JOG_PULSE_MULTIPLIERS)

    def _sync_slider_from_jog_entry(
        self, _event: tk.Event[tk.Misc] | None = None
    ) -> None:
        try:
            value = abs(finite_float(self.jog_var.get(), f"{self.axis} jog step"))
        except ValueError:
            return
        nearest_index = min(
            range(len(JOG_PULSE_MULTIPLIERS)),
            key=lambda index: abs(self._jog_presets_um()[index] - value),
        )
        self.jog_slider_var.set(float(nearest_index))

    def jog(self, direction: int) -> None:
        try:
            amount = abs(
                finite_float(self.jog_var.get(), f"{self.axis} jog distance in µm")
            )
            if amount <= 0:
                raise ValueError("Jog distance must be greater than zero")
            self.app.send_command(
                build_jog_um(self.axis, direction * amount, self.pulses_per_mm)
            )
        except ValueError as exc:
            messagebox.showerror("Invalid jog", str(exc), parent=self)

    def go_to(self) -> None:
        try:
            target = finite_float(self.goto_var.get(), f"{self.axis} target")
            self.app.send_command(build_absolute_mm(**{f"{self.axis.lower()}_mm": target}))
        except ValueError as exc:
            messagebox.showerror("Invalid target", str(exc), parent=self)

    def apply_dynamics(self) -> None:
        try:
            speed_command = build_speed_mm(self.axis, self.speed_var.get())
            acceleration_command = build_accel_mm(self.axis, self.accel_var.get())
        except ValueError as exc:
            messagebox.showerror("Invalid motion setting", str(exc), parent=self)
            return
        self.app.send_command(speed_command)
        self.app.send_command(acceleration_command)

    def zero(self) -> None:
        if messagebox.askokcancel(
            "Set coordinate zero",
            f"Set the current {self.axis} position to 0.000 mm without moving?",
            parent=self,
        ):
            self.app.send_command(f"ZERO {self.axis}")

    def update_status(self, values: dict[str, str]) -> None:
        scale = finite_float(values.get(f"{self.axis}SPMM", DEFAULT_SCALE[self.axis]))
        previous_resolution_um = pulse_resolution_um(self.pulses_per_mm)
        try:
            current_jog_um = finite_float(
                self.jog_var.get(), f"{self.axis} jog distance"
            )
        except ValueError:
            current_jog_um = previous_resolution_um
        pulse_multiplier = max(1, round(current_jog_um / previous_resolution_um))
        scale_changed = not math.isclose(scale, self.pulses_per_mm, rel_tol=0.0, abs_tol=1e-6)
        self.pulses_per_mm = scale
        resolution_um = pulse_resolution_um(scale)
        if scale_changed:
            self.jog_var.set(format_micrometres(pulse_multiplier * resolution_um))
            self.jog_spinbox.configure(from_=resolution_um, increment=resolution_um)
        steps = int(values.get(self.axis, "0"))
        target_steps = int(values.get(f"{self.axis}T", str(steps)))
        position_mm = finite_float(values.get(f"{self.axis}MM", steps / scale))
        target_mm = finite_float(values.get(f"{self.axis}TMM", target_steps / scale))

        self.position_var.set(f"{position_mm:,.6f} mm")
        self.target_var.set(f"{target_mm:,.6f} mm")
        self.steps_var.set(
            f"{steps:,} STEP pulses · one pulse = {format_micrometres(resolution_um)} µm"
        )
        self.scale_var.set(format_number(scale))
        self.resolution_var.set(f" · {format_micrometres(resolution_um)} µm/pulse")
        self.reference_var.set(
            "Coordinate reference set" if values.get(f"{self.axis}H") == "1"
            else "Coordinate reference not set"
        )

        if not self.settings_loaded:
            if f"{self.axis}VMM" in values:
                self.speed_var.set(f"{finite_float(values[f'{self.axis}VMM']):g}")
            if f"{self.axis}AMM" in values:
                self.accel_var.set(f"{finite_float(values[f'{self.axis}AMM']):g}")
            self.settings_loaded = True


class LaserStageApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1440x900")
        self.minsize(1000, 680)
        self.resizable(True, True)
        self.configure(background=APP_BACKGROUND)

        self.serial_port: serial.Serial | None = None
        self.serial_lock = threading.Lock()
        self.visa_instrument: E5574AVisa | None = None
        self.visa_lock = threading.Lock()
        self.visa_busy = False
        self.reader_thread: threading.Thread | None = None
        self.reader_stop = threading.Event()
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.controller_verified = False
        self.firmware_version = "--"
        self.connection_generation = 0
        self.last_status_time = 0.0
        self.current_status: dict[str, str] = {}
        self.online_widgets: list[ttk.Widget] = []
        self.motion_widgets: list[ttk.Widget] = []
        self.serial_log_path: Path | None = self._prepare_serial_log()
        self.session_log_path: Path | None = self._prepare_session_log()
        self.session_report_directory: Path | None = self._prepare_session_report_directory()
        self.session_report_path: Path | None = None
        self.session_report: SessionExcelReport | None = None
        self.session_running = False
        self.session_paused = False
        self.session_preparing_visa = False
        self.session_actions: list[SessionAction] = []
        self.session_action_index = 0
        self.session_current_action: SessionAction | None = None
        self.session_waiting_for_idle = False
        self.session_waiting_for_measurement = False
        self.session_measurement_in_progress = False
        self.session_action_started_at = 0.0
        self.session_action_started_wallclock = datetime.now()
        self.session_motion_completed_at = 0.0
        self.session_motion_completed_wallclock = datetime.now()
        self.session_measurement_started_at = 0.0
        self.session_measurement_started_wallclock = datetime.now()
        self.session_measurement_due_at = 0.0
        self.session_dwell_until = 0.0
        self.session_idle_event = ""
        self.session_next_action_at = 0.0
        self.session_started_at = 0.0
        self.session_started_wallclock = datetime.now()
        self.session_pause_started_at = 0.0
        self.session_paused_duration = 0.0
        self.session_config: SerpentineSessionConfig | None = None
        self.session_expected_positions = {"X": 0.0, "Z": 0.0}
        self.plotter_busy = False
        self.plotter_dataset: BeamWorkbookDataset | None = None
        self.plotter_output_path: Path | None = None

        self.port_var = tk.StringVar()
        self.visa_resource_var = tk.StringVar(value="GPIB0::24::INSTR")
        self.visa_application_var = tk.StringVar(value="Power meter")
        self.visa_idn_var = tk.StringVar(value="Not identified")
        self.visa_reading_var = tk.StringVar(value="--")
        self.visa_status_var = tk.StringVar(
            value="Connect a VISA GPIB resource to the HP E5574A."
        )
        self.xy_mode_var = tk.StringVar(value="relative")
        self.xy_x_var = tk.StringVar(value="0.0")
        self.xy_z_var = tk.StringVar(value="0.0")
        self.raw_command_var = tk.StringVar()
        self.show_status_traffic_var = tk.BooleanVar(value=False)
        self.message_var = tk.StringVar(value="Connect the USB-UART adapter to begin.")
        self.workspace_state_var = tk.StringVar(value="Motion control · Normal view")
        self.workspace_mode = "normal"
        self.session_recipe_var = tk.StringVar(value=Z_SERPENTINE_RECIPE)
        self.session_loops_var = tk.StringVar(value="1")
        self.session_z_span_var = tk.StringVar(value="200.0")
        self.session_z_step_var = tk.StringVar(value="1.0")
        self.session_x_step_var = tk.StringVar(value="1.0")
        self.session_target_diameter_var = tk.StringVar(value="100.0")
        self.session_period_var = tk.StringVar(value="1.0")
        self.session_speed_var = tk.StringVar(value="5.0")
        self.session_accel_var = tk.StringVar(value="20.0")
        self.session_state_var = tk.StringVar(value="Ready")
        self.session_phase_var = tk.StringVar(value="No active session")
        self.session_progress_text_var = tk.StringVar(value="0 / 0 actions")
        self.session_time_var = tk.StringVar(value="Estimated duration: 00:09:50")
        self.session_sequence_var = tk.StringVar()
        self.session_requirement_var = tk.StringVar()
        self.session_progress_var = tk.DoubleVar(value=0.0)
        self.plotter_file_var = tk.StringVar()
        self.plotter_target_diameter_var = tk.StringVar(value="100.0")
        self.plotter_x_scale_var = tk.StringVar(value=format_number(DEFAULT_SCALE["X"]))
        self.plotter_z_scale_var = tk.StringVar(value=format_number(DEFAULT_SCALE["Z"]))
        self.plotter_use_event_positions_var = tk.BooleanVar(value=True)
        self.plotter_auto_center_var = tk.BooleanVar(value=True)
        self.plotter_center_x_var = tk.StringVar(value="50.0")
        self.plotter_center_z_var = tk.StringVar(value="50.0")
        self.plotter_view_var = tk.StringVar(value=PLOTTER_VIEW_OPTIONS[0])
        self.plotter_status_var = tk.StringVar(
            value="Choose an existing session .xlsx workbook. No controller or GPIB connection is required."
        )
        self.plotter_metrics_var = tk.StringVar(value="No workbook loaded")
        self.plotter_source_var = tk.StringVar(value="Position source: --")
        report_location = (
            str(self.session_report_directory)
            if self.session_report_directory is not None
            else "Excel report folder unavailable"
        )
        self.session_report_var = tk.StringVar(value=f"Excel reports: {report_location}")

        self._configure_style()
        self._load_brand_assets()
        self._build_ui()
        self.refresh_ports()
        self._update_control_states()

        self.bind("<Escape>", self._keyboard_stop)
        self.bind("<Control-m>", lambda _event: self.minimize_selected_tab())
        self.bind("<Control-0>", lambda _event: self.restore_workspace())
        self.bind("<F11>", lambda _event: self.maximize_selected_tab())
        self.after(QUEUE_PERIOD_MS, self._process_events)
        self.after(STATUS_PERIOD_MS, self._poll_status)
        self.after(SESSION_TICK_MS, self._session_tick)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    @staticmethod
    def _prepare_serial_log() -> Path | None:
        try:
            base = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
            log_directory = base / "LaserStageController"
            log_directory.mkdir(parents=True, exist_ok=True)
            return log_directory / "serial.log"
        except OSError:
            return None

    @staticmethod
    def _prepare_session_log() -> Path | None:
        try:
            base = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
            log_directory = base / "LaserStageController"
            log_directory.mkdir(parents=True, exist_ok=True)
            return log_directory / "sessions.log"
        except OSError:
            return None

    @staticmethod
    def _prepare_session_report_directory() -> Path | None:
        """Create the Excel-report folder beside the project contents."""
        try:
            if getattr(sys, "frozen", False):
                executable_directory = Path(sys.executable).resolve().parent
                project_directory = (
                    executable_directory.parent
                    if executable_directory.name.lower() == "dist"
                    else executable_directory
                )
            else:
                project_directory = Path(__file__).resolve().parent.parent
            report_directory = project_directory / "Session Reports"
            report_directory.mkdir(parents=True, exist_ok=True)
            return report_directory
        except OSError:
            return None

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("TFrame", background=APP_BACKGROUND)
        style.configure("TLabel", background=APP_BACKGROUND, foreground=TEXT_PRIMARY)
        style.configure("TLabelframe", background=APP_BACKGROUND, bordercolor="#cbd5e1")
        style.configure(
            "TLabelframe.Label",
            background=APP_BACKGROUND,
            foreground=TEXT_PRIMARY,
            font=("Segoe UI Semibold", 10),
        )
        style.configure("TButton", font=("Segoe UI", 9), padding=(9, 7))
        style.configure("TEntry", padding=6, fieldbackground="#ffffff")
        style.configure("TCombobox", padding=5, fieldbackground="#ffffff")
        style.configure("Title.TLabel", font=("Segoe UI Semibold", 20), foreground=TEXT_PRIMARY)
        style.configure("Subtitle.TLabel", font=("Segoe UI", 9), foreground=TEXT_MUTED)
        style.configure("Section.TLabel", font=("Segoe UI Semibold", 8), foreground=TEXT_MUTED)
        style.configure("Position.TLabel", font=("Consolas", 18, "bold"), foreground=TEXT_PRIMARY)
        style.configure("Muted.TLabel", foreground=TEXT_MUTED)
        style.configure("Jog.TButton", font=("Segoe UI Semibold", 11), padding=9)
        style.configure(
            "Jog.Horizontal.TScale",
            background=APP_BACKGROUND,
            troughcolor="#cbd5e1",
            bordercolor="#94a3b8",
            lightcolor=EGSA_BLUE,
            darkcolor=EGSA_BLUE,
        )
        style.configure("Toolbar.TButton", padding=(8, 7))
        style.configure(
            "Primary.TButton",
            font=("Segoe UI Semibold", 9),
            padding=(10, 7),
            foreground="#ffffff",
            background=EGSA_BLUE,
            bordercolor=EGSA_BLUE,
        )
        style.map(
            "Primary.TButton",
            background=[("active", "#1a568f"), ("disabled", "#9aa8b7")],
            foreground=[("disabled", "#e8edf2")],
        )
        style.configure(
            "Stop.TButton",
            font=("Segoe UI Semibold", 10),
            padding=(8, 7),
            foreground="#7f1d1d",
        )
        style.configure(
            "Emergency.TButton",
            font=("Segoe UI", 10, "bold"),
            padding=(8, 7),
            foreground="#ffffff",
            background=EGSA_RED,
            bordercolor="#9f1f1f",
        )
        style.map(
            "Emergency.TButton",
            background=[("active", "#a91f1f"), ("disabled", "#c9a3a3")],
            foreground=[("disabled", "#f6e8e8")],
        )
        style.configure("Workspace.TFrame", background="#dde5ef")
        style.configure(
            "Workspace.TLabel",
            background="#dde5ef",
            foreground="#31445f",
            font=("Segoe UI Semibold", 9),
        )
        style.configure("Workspace.TButton", font=("Segoe UI", 8), padding=(8, 4))
        style.configure(
            "TNotebook",
            background=APP_BACKGROUND,
            borderwidth=0,
            tabmargins=(0, 3, 0, 0),
        )
        style.configure(
            "TNotebook.Tab",
            font=("Segoe UI Semibold", 10),
            padding=(18, 9),
            background="#d9e1eb",
            foreground="#41516a",
            borderwidth=0,
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", "#ffffff"), ("active", "#e7edf4")],
            foreground=[("selected", EGSA_BLUE), ("active", TEXT_PRIMARY)],
        )

    def _load_brand_assets(self) -> None:
        """Load official EgSA artwork and prepare a compact header crop."""
        self.egsa_logo_image: tk.PhotoImage | None = None
        self.egsa_logo_source: tk.PhotoImage | None = None
        try:
            self.egsa_logo_source = tk.PhotoImage(
                file=str(resource_path("assets/egsa_icon.png"))
            )
            self.egsa_logo_image = tk.PhotoImage(width=132, height=72)
            self.egsa_logo_image.tk.call(
                str(self.egsa_logo_image),
                "copy",
                str(self.egsa_logo_source),
                "-from",
                4,
                58,
                136,
                130,
                "-to",
                0,
                0,
            )
            self.iconphoto(True, self.egsa_logo_image)
        except tk.TclError:
            self.egsa_logo_image = None

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=(16, 12, 16, 10))
        root.pack(fill="both", expand=True)
        self.root_frame = root
        root.columnconfigure(0, weight=1)
        root.rowconfigure(4, weight=1)

        header = tk.Frame(
            root,
            background=EGSA_NAVY,
            highlightbackground="#07152a",
            highlightthickness=1,
            padx=14,
            pady=8,
        )
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)
        logo = tk.Label(
            header,
            image=self.egsa_logo_image,
            text="EgSA" if self.egsa_logo_image is None else "",
            font=("Segoe UI Semibold", 18),
            background="#ffffff",
            foreground=EGSA_NAVY,
            width=10 if self.egsa_logo_image is None else 132,
            height=3 if self.egsa_logo_image is None else 72,
            padx=6,
            pady=2,
        )
        logo.grid(row=0, column=0, rowspan=3, sticky="nsw", padx=(0, 14))
        tk.Label(
            header,
            text="EGYPTIAN SPACE AGENCY · COMMUNICATION SUBSYSTEM",
            font=("Segoe UI Semibold", 9),
            background=EGSA_NAVY,
            foreground="#ff8a80",
        ).grid(row=0, column=1, sticky="w")
        tk.Label(
            header,
            text="Laser X-Z Motion Controller",
            font=("Segoe UI Semibold", 20),
            background=EGSA_NAVY,
            foreground="#ffffff",
        ).grid(row=1, column=1, sticky="w")
        tk.Label(
            header,
            text=(
                "STM32F401RCT6  |  1/32 microstep high resolution  |  "
                "200 × 200 mm stage  |  UART 115200"
            ),
            font=("Segoe UI", 9),
            background=EGSA_NAVY,
            foreground="#b8c8da",
        ).grid(row=2, column=1, sticky="w")
        tk.Label(
            header,
            text="OPERATOR CONSOLE\nEsc = immediate STOP",
            justify="right",
            font=("Segoe UI Semibold", 9),
            background=EGSA_NAVY,
            foreground="#dce7f3",
        ).grid(row=0, column=2, rowspan=3, sticky="e", padx=(16, 0))

        connection = ttk.LabelFrame(root, text="Controller connection", padding=10)
        self.connection_frame = connection
        connection.grid(row=1, column=0, sticky="ew", pady=(10, 8))
        connection.columnconfigure(0, weight=3)
        connection.columnconfigure(1, weight=5)
        connection_controls = ttk.Frame(connection)
        connection_controls.grid(row=0, column=0, sticky="ew", padx=(0, 12))
        connection_controls.columnconfigure(1, weight=1)
        ttk.Label(connection_controls, text="Serial port").grid(row=0, column=0, sticky="w")
        self.port_box = ttk.Combobox(
            connection_controls, textvariable=self.port_var, state="readonly"
        )
        self.port_box.grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Button(
            connection_controls, text="Refresh ports", command=self.refresh_ports
        ).grid(row=0, column=2)
        self.connect_button = ttk.Button(
            connection_controls,
            text="Connect",
            command=self.toggle_connection,
            style="Primary.TButton",
        )
        self.connect_button.grid(row=0, column=3, padx=(8, 0))

        badges = ttk.Frame(connection)
        badges.grid(row=0, column=1, sticky="ew")
        for column in range(5):
            badges.columnconfigure(column, weight=1, uniform="badge")
        self.link_badge = StatusBadge(badges, "LINK", "Disconnected")
        self.driver_badge = StatusBadge(badges, "DRIVERS", "Disabled")
        self.motion_badge = StatusBadge(badges, "MOTION", "Idle")
        self.estop_badge = StatusBadge(badges, "E-STOP", "Released")
        self.fault_badge = StatusBadge(badges, "FAULT", "None")
        for column, badge in enumerate(
            (self.link_badge, self.driver_badge, self.motion_badge, self.estop_badge, self.fault_badge)
        ):
            badge.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 4, 0))

        toolbar = ttk.Frame(root)
        self.command_toolbar = toolbar
        toolbar.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        for column in range(6):
            toolbar.columnconfigure(column, weight=1)
        self.enable_button = ttk.Button(
            toolbar,
            text="Enable drivers",
            command=lambda: self.send_command("ENABLE"),
            style="Primary.TButton",
        )
        self.enable_button.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.stop_button = ttk.Button(
            toolbar, text="STOP motion", command=self.stop_motion, style="Stop.TButton"
        )
        self.stop_button.grid(row=0, column=1, sticky="ew", padx=4)
        self.disable_button = ttk.Button(
            toolbar, text="Disable drivers", command=self.disable_drivers, style="Toolbar.TButton"
        )
        self.disable_button.grid(row=0, column=2, sticky="ew", padx=4)
        self.clear_button = ttk.Button(
            toolbar, text="Clear fault", command=lambda: self.send_command("CLEAR"), style="Toolbar.TButton"
        )
        self.clear_button.grid(row=0, column=3, sticky="ew", padx=4)
        self.zero_all_button = ttk.Button(
            toolbar, text="Set X/Z zero", command=self.zero_all, style="Toolbar.TButton"
        )
        self.zero_all_button.grid(row=0, column=4, sticky="ew", padx=4)
        self.estop_button = ttk.Button(
            toolbar, text="E-STOP", command=self.emergency_stop, style="Emergency.TButton"
        )
        self.estop_button.grid(row=0, column=5, sticky="ew", padx=(4, 0))
        self.register_online_widgets(
            self.enable_button,
            self.stop_button,
            self.disable_button,
            self.clear_button,
            self.zero_all_button,
            self.estop_button,
        )

        workspace_bar = ttk.Frame(root, style="Workspace.TFrame", padding=(8, 5))
        self.workspace_bar = workspace_bar
        workspace_bar.grid(row=3, column=0, sticky="ew", pady=(0, 6))
        workspace_bar.columnconfigure(0, weight=1)
        ttk.Label(
            workspace_bar,
            textvariable=self.workspace_state_var,
            style="Workspace.TLabel",
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            workspace_bar,
            text="Ctrl+M minimize  ·  F11 maximize  ·  Ctrl+0 restore",
            style="Workspace.TLabel",
        ).grid(row=0, column=1, sticky="e", padx=(12, 10))
        self.minimize_tab_button = ttk.Button(
            workspace_bar,
            text="Minimize tab",
            command=self.minimize_selected_tab,
            style="Workspace.TButton",
        )
        self.minimize_tab_button.grid(row=0, column=2, padx=(0, 4))
        self.maximize_tab_button = ttk.Button(
            workspace_bar,
            text="Maximize tab",
            command=self.maximize_selected_tab,
            style="Workspace.TButton",
        )
        self.maximize_tab_button.grid(row=0, column=3, padx=4)
        self.restore_tab_button = ttk.Button(
            workspace_bar,
            text="Restore layout",
            command=self.restore_workspace,
            style="Workspace.TButton",
        )
        self.restore_tab_button.grid(row=0, column=4, padx=(4, 0))

        self.notebook = ttk.Notebook(root)
        self.notebook.grid(row=4, column=0, sticky="nsew")
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self.notebook.bind(
            "<Double-Button-1>", lambda _event: self.maximize_selected_tab()
        )

        motion_tab = ttk.Frame(self.notebook)
        motion_tab.columnconfigure(0, weight=1)
        motion_tab.rowconfigure(0, weight=1)
        self.notebook.add(motion_tab, text="Motion control")

        self.motion_scroll = VerticalScrolledFrame(motion_tab, padding=10)
        self.motion_scroll.grid(row=0, column=0, sticky="nsew")
        motion_content = self.motion_scroll.content
        motion_content.columnconfigure(0, weight=3)
        motion_content.columnconfigure(1, weight=2)
        motion_content.rowconfigure(0, weight=1)

        axes = ttk.Frame(motion_content)
        axes.grid(row=0, column=0, sticky="nsew")
        axes.columnconfigure(0, weight=1, uniform="axis")
        axes.columnconfigure(1, weight=1, uniform="axis")
        axes.rowconfigure(0, weight=1)
        self.x_panel = AxisPanel(axes, self, "X")
        self.z_panel = AxisPanel(axes, self, "Z")
        self.x_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        self.z_panel.grid(row=0, column=1, sticky="nsew", padx=(5, 0))

        coordinated = ttk.LabelFrame(motion_content, text="Two-axis move", padding=10)
        coordinated.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        coordinated.columnconfigure(6, weight=1)
        mode_controls = ttk.Frame(coordinated)
        mode_controls.grid(row=0, column=0, sticky="w", padx=(0, 12))
        ttk.Radiobutton(
            mode_controls, text="Relative", variable=self.xy_mode_var, value="relative"
        ).pack(side="left")
        ttk.Radiobutton(
            mode_controls, text="Absolute", variable=self.xy_mode_var, value="absolute"
        ).pack(side="left", padx=(8, 0))
        ttk.Label(coordinated, text="X (mm)").grid(row=0, column=1, padx=(0, 4))
        ttk.Entry(coordinated, textvariable=self.xy_x_var, width=13).grid(
            row=0, column=2, sticky="ew", padx=(0, 12)
        )
        ttk.Label(coordinated, text="Z (mm)").grid(row=0, column=3, padx=(0, 4))
        ttk.Entry(coordinated, textvariable=self.xy_z_var, width=13).grid(
            row=0, column=4, sticky="ew", padx=(0, 12)
        )
        execute_button = ttk.Button(
            coordinated, text="Execute X/Z move", command=self.execute_xy_move, style="Jog.TButton"
        )
        execute_button.grid(row=0, column=6, sticky="nsew")
        self.register_motion_widgets(execute_button)

        ttk.Label(
            motion_content,
            text=(
                "Fine jog values are micrometres and always map to whole STEP pulses. "
                "Position remains open-loop and must be referenced with Set zero after power-up."
            ),
            style="Muted.TLabel",
            wraplength=1000,
        ).grid(row=2, column=0, sticky="ew", pady=(8, 0))

        self.stage_simulation = StageSimulation(motion_content)
        self.stage_simulation.grid(
            row=0, column=1, rowspan=3, sticky="nsew", padx=(12, 0)
        )

        settings_tab = ttk.Frame(self.notebook, padding=18)
        self.settings_tab = settings_tab
        settings_tab.columnconfigure(0, weight=1, uniform="setting")
        settings_tab.columnconfigure(1, weight=1, uniform="setting")
        self.notebook.add(settings_tab, text="Motion settings")

        for column, panel in enumerate((self.x_panel, self.z_panel)):
            axis_settings = ttk.LabelFrame(
                settings_tab, text=f"{panel.axis} axis dynamics", padding=18
            )
            axis_settings.grid(
                row=0,
                column=column,
                sticky="nsew",
                padx=(0, 8) if column == 0 else (8, 0),
            )
            axis_settings.columnconfigure(1, weight=1)
            ttk.Label(axis_settings, text="Maximum speed").grid(
                row=0, column=0, sticky="w", pady=5
            )
            ttk.Entry(axis_settings, textvariable=panel.speed_var, width=14).grid(
                row=0, column=1, sticky="ew", padx=(12, 6), pady=5
            )
            ttk.Label(axis_settings, text="mm/s").grid(row=0, column=2, sticky="w")
            ttk.Label(axis_settings, text="Acceleration").grid(
                row=1, column=0, sticky="w", pady=5
            )
            ttk.Entry(axis_settings, textvariable=panel.accel_var, width=14).grid(
                row=1, column=1, sticky="ew", padx=(12, 6), pady=5
            )
            ttk.Label(axis_settings, text="mm/s²").grid(row=1, column=2, sticky="w")
            apply_button = ttk.Button(
                axis_settings,
                text=f"Apply {panel.axis} settings",
                command=panel.apply_dynamics,
                style="Primary.TButton",
            )
            apply_button.grid(
                row=2, column=0, columnspan=3, sticky="ew", pady=(14, 8)
            )
            self.register_online_widgets(apply_button)
            ttk.Separator(axis_settings).grid(
                row=3, column=0, columnspan=3, sticky="ew", pady=8
            )
            ttk.Label(axis_settings, text="Controller calibration", style="Section.TLabel").grid(
                row=4, column=0, sticky="w", pady=(5, 0)
            )
            ttk.Label(
                axis_settings,
                textvariable=panel.scale_var,
                style="Position.TLabel",
            ).grid(row=5, column=0, sticky="w")
            ttk.Label(axis_settings, text="STEP pulses per millimetre").grid(
                row=5, column=1, columnspan=2, sticky="w", padx=(10, 0)
            )
            ttk.Label(
                axis_settings,
                text=(
                    f"Allowed speed: {MIN_SPEED_MM_S:g}-{MAX_SPEED_MM_S:g} mm/s\n"
                    f"Allowed acceleration: {MIN_ACCEL_MM_S2:g}-{MAX_ACCEL_MM_S2:g} mm/s²"
                ),
                style="Muted.TLabel",
                justify="left",
            ).grid(row=6, column=0, columnspan=3, sticky="w", pady=(12, 0))

        ttk.Label(
            settings_tab,
            text=(
                "Settings are sent independently to each axis and remain active until the "
                "controller restarts. Begin commissioning at 5 mm/s and 20 mm/s²."
            ),
            style="Muted.TLabel",
            wraplength=1000,
        ).grid(row=1, column=0, columnspan=2, sticky="ew", pady=(14, 0))

        gpib_tab = ttk.Frame(self.notebook, padding=18)
        self.gpib_tab = gpib_tab
        gpib_tab.columnconfigure(0, weight=1)
        gpib_tab.rowconfigure(2, weight=1)
        self.notebook.add(gpib_tab, text="E5574A / GPIB")
        self._build_gpib_tab(gpib_tab)

        sessions = ttk.Frame(self.notebook)
        self.sessions_tab = sessions
        sessions.columnconfigure(0, weight=1)
        sessions.rowconfigure(0, weight=1)
        self.notebook.add(sessions, text="Sessions")

        self.sessions_scroll = VerticalScrolledFrame(sessions, padding=14)
        self.sessions_scroll.grid(row=0, column=0, sticky="nsew")
        sessions_content = self.sessions_scroll.content
        sessions_content.columnconfigure(0, weight=3)
        sessions_content.columnconfigure(1, weight=2)
        sessions_content.rowconfigure(1, weight=1)
        self._build_sessions_tab(sessions_content)

        plotter_tab = ttk.Frame(self.notebook, padding=14)
        self.plotter_tab = plotter_tab
        plotter_tab.columnconfigure(1, weight=1)
        plotter_tab.rowconfigure(0, weight=1)
        self.notebook.add(plotter_tab, text="Excel Beam Plotter")
        self._build_excel_plotter_tab(plotter_tab)

        diagnostics = ttk.Frame(self.notebook, padding=10)
        self.diagnostics_tab = diagnostics
        diagnostics.columnconfigure(0, weight=1)
        diagnostics.rowconfigure(1, weight=1)
        self.notebook.add(diagnostics, text="UART bus log")

        diagnostic_header = ttk.Frame(diagnostics)
        diagnostic_header.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        diagnostic_header.columnconfigure(0, weight=1)
        ttk.Label(
            diagnostic_header,
            text="Live UART traffic:  TX > GUI to STM32     RX < STM32 to GUI",
            style="Muted.TLabel",
        ).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(
            diagnostic_header,
            text="Show periodic STATUS traffic",
            variable=self.show_status_traffic_var,
        ).grid(row=0, column=1, padx=(8, 4))
        ttk.Button(diagnostic_header, text="Clear log", command=self.clear_log).grid(
            row=0, column=2, padx=(4, 0)
        )
        ttk.Button(diagnostic_header, text="Save log", command=self.save_log).grid(
            row=0, column=3, padx=(4, 0)
        )
        ttk.Button(diagnostic_header, text="Open log folder", command=self.open_log_folder).grid(
            row=0, column=4, padx=(4, 0)
        )

        self.log = scrolledtext.ScrolledText(
            diagnostics,
            height=16,
            wrap="word",
            state="disabled",
            font=("Consolas", 9),
            background="#0f172a",
            foreground="#e2e8f0",
            insertbackground="#e2e8f0",
        )
        self.log.grid(row=1, column=0, sticky="nsew")
        raw = ttk.Frame(diagnostics)
        raw.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        raw.columnconfigure(0, weight=1)
        raw_entry = ttk.Entry(raw, textvariable=self.raw_command_var)
        raw_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        raw_entry.bind("<Return>", lambda _event: self.send_raw_command())
        self.raw_send_button = ttk.Button(raw, text="Send command", command=self.send_raw_command)
        self.raw_send_button.grid(row=0, column=1)
        self.register_online_widgets(self.raw_send_button)
        log_path_text = str(self.serial_log_path) if self.serial_log_path else "Persistent log unavailable"
        ttk.Label(diagnostics, text=f"Complete automatic bus log: {log_path_text}", style="Muted.TLabel").grid(
            row=3, column=0, sticky="w", pady=(6, 0)
        )

        status_bar = ttk.Frame(root)
        self.status_bar = status_bar
        status_bar.grid(row=5, column=0, sticky="ew", pady=(8, 0))
        status_bar.columnconfigure(0, weight=1)
        ttk.Label(status_bar, textvariable=self.message_var, style="Muted.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self.firmware_label = ttk.Label(status_bar, text="Firmware: --", style="Muted.TLabel")
        self.firmware_label.grid(row=0, column=1, sticky="e")
        self._update_workspace_controls()

    def _build_gpib_tab(self, parent: ttk.Frame) -> None:
        """Build the independent host-side VISA controls for the E5574A."""
        connection = ttk.LabelFrame(parent, text="VISA / GPIB connection", padding=14)
        connection.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        connection.columnconfigure(1, weight=1)

        ttk.Label(connection, text="VISA resource").grid(
            row=0, column=0, sticky="w", padx=(0, 8), pady=4
        )
        self.visa_resource_box = ttk.Combobox(
            connection,
            textvariable=self.visa_resource_var,
            state="normal",
            width=28,
        )
        self.visa_resource_box.grid(row=0, column=1, sticky="ew", pady=4)
        self.visa_refresh_button = ttk.Button(
            connection, text="Refresh VISA", command=self.refresh_visa_resources
        )
        self.visa_refresh_button.grid(row=0, column=2, padx=(8, 0), pady=4)
        self.visa_connect_button = ttk.Button(
            connection,
            text="Connect E5574A",
            command=self.toggle_visa_connection,
            style="Primary.TButton",
        )
        self.visa_connect_button.grid(row=0, column=3, padx=(8, 0), pady=4)

        ttk.Label(
            connection,
            textvariable=self.visa_status_var,
            style="Muted.TLabel",
            wraplength=900,
        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(8, 0))

        identity = ttk.LabelFrame(parent, text="Instrument", padding=14)
        identity.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        identity.columnconfigure(1, weight=1)
        ttk.Label(identity, text="Identification").grid(row=0, column=0, sticky="w")
        ttk.Label(
            identity,
            textvariable=self.visa_idn_var,
            style="Position.TLabel",
        ).grid(row=0, column=1, sticky="w", padx=(12, 0))

        measurement = ttk.LabelFrame(parent, text="Measurement", padding=14)
        measurement.grid(row=2, column=0, sticky="nsew")
        measurement.columnconfigure(1, weight=1)
        ttk.Label(measurement, text="E5574A application").grid(
            row=0, column=0, sticky="w", pady=4
        )
        self.visa_application_box = ttk.Combobox(
            measurement,
            textvariable=self.visa_application_var,
            values=tuple(MEASUREMENT_APPLICATIONS.keys()),
            state="readonly",
            width=24,
        )
        self.visa_application_box.grid(row=0, column=1, sticky="w", padx=(12, 8), pady=4)
        self.visa_select_application_button = ttk.Button(
            measurement,
            text="Select application",
            command=self.select_visa_application,
        )
        self.visa_select_application_button.grid(row=0, column=2, padx=4, pady=4)
        self.visa_read_button = ttk.Button(
            measurement,
            text="Read measurement",
            command=self.read_visa_measurement,
            style="Primary.TButton",
        )
        self.visa_read_button.grid(row=0, column=3, padx=(4, 0), pady=4)

        ttk.Label(measurement, text="Latest value").grid(
            row=1, column=0, sticky="w", pady=(18, 4)
        )
        ttk.Label(
            measurement,
            textvariable=self.visa_reading_var,
            style="Position.TLabel",
        ).grid(row=1, column=1, columnspan=3, sticky="w", padx=(12, 0), pady=(18, 4))
        ttk.Label(
            measurement,
            text=(
                "The PC controls the E5574A through VISA/GPIB. It is independent of the "
                "STM32 UART connection. Default E5574A address: GPIB0::24::INSTR."
            ),
            style="Muted.TLabel",
            wraplength=1000,
        ).grid(row=2, column=0, columnspan=4, sticky="w", pady=(18, 0))
        self._update_visa_control_states()

    def _build_sessions_tab(self, parent: ttk.Frame) -> None:
        configuration = ttk.LabelFrame(parent, text="Session recipe", padding=16)
        configuration.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        configuration.columnconfigure(1, weight=1)

        ttk.Label(configuration, text="Recipe").grid(row=0, column=0, sticky="w", pady=4)
        recipe_box = ttk.Combobox(
            configuration,
            textvariable=self.session_recipe_var,
            values=(Z_SERPENTINE_RECIPE, X_SERPENTINE_RECIPE),
            state="readonly",
        )
        recipe_box.grid(row=0, column=1, columnspan=2, sticky="ew", padx=(12, 0), pady=4)

        parameter_rows = (
            ("Number of loops", self.session_loops_var, "cycles"),
            ("Z scan span", self.session_z_span_var, "mm"),
            ("Z increment", self.session_z_step_var, "mm per step"),
            ("X shift after each pass", self.session_x_step_var, "mm"),
            ("Beam target diameter", self.session_target_diameter_var, "mm"),
            ("Post-move measurement dwell", self.session_period_var, "seconds"),
            ("Session motion speed", self.session_speed_var, "mm/s"),
            ("Session acceleration", self.session_accel_var, "mm/s²"),
        )
        self.session_parameter_widgets: list[ttk.Widget] = [recipe_box]
        for row, (label, variable, unit) in enumerate(parameter_rows, start=1):
            parameter_label = ttk.Label(configuration, text=label)
            parameter_label.grid(row=row, column=0, sticky="w", pady=4)
            if row == 2:
                self.session_scan_span_label = parameter_label
            elif row == 3:
                self.session_scan_increment_label = parameter_label
            elif row == 4:
                self.session_cross_shift_label = parameter_label
            elif row == 6:
                self.session_step_period_label = parameter_label
            if variable is self.session_loops_var:
                entry: ttk.Widget = ttk.Spinbox(
                    configuration,
                    from_=1,
                    to=1000,
                    increment=1,
                    textvariable=variable,
                    width=12,
                )
            else:
                entry = ttk.Entry(configuration, textvariable=variable, width=14)
            entry.grid(row=row, column=1, sticky="ew", padx=(12, 8), pady=4)
            ttk.Label(configuration, text=unit, style="Muted.TLabel").grid(
                row=row, column=2, sticky="w", pady=4
            )
            self.session_parameter_widgets.append(entry)

        ttk.Separator(configuration).grid(
            row=9, column=0, columnspan=3, sticky="ew", pady=(10, 8)
        )
        ttk.Label(
            configuration,
            textvariable=self.session_sequence_var,
            wraplength=700,
            justify="left",
        ).grid(row=10, column=0, columnspan=3, sticky="ew")
        ttk.Label(
            configuration,
            textvariable=self.session_time_var,
            style="Muted.TLabel",
        ).grid(row=11, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Label(
            configuration,
            textvariable=self.session_requirement_var,
            style="Muted.TLabel",
            wraplength=700,
            justify="left",
        ).grid(row=12, column=0, columnspan=3, sticky="ew", pady=(8, 0))

        status = ttk.LabelFrame(parent, text="Session status", padding=16)
        status.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        status.columnconfigure(0, weight=1)
        ttk.Label(status, textvariable=self.session_state_var, style="Position.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(status, textvariable=self.session_phase_var).grid(
            row=1, column=0, sticky="w", pady=(4, 10)
        )
        ttk.Progressbar(
            status,
            variable=self.session_progress_var,
            maximum=100.0,
            mode="determinate",
        ).grid(row=2, column=0, sticky="ew")
        ttk.Label(status, textvariable=self.session_progress_text_var, style="Muted.TLabel").grid(
            row=3, column=0, sticky="w", pady=(5, 0)
        )
        self.session_runtime_var = tk.StringVar(value="Elapsed 00:00:00  ·  Remaining --")
        ttk.Label(status, textvariable=self.session_runtime_var, style="Muted.TLabel").grid(
            row=4, column=0, sticky="w", pady=(3, 12)
        )

        session_buttons = ttk.Frame(status)
        session_buttons.grid(row=5, column=0, sticky="ew")
        for column in range(3):
            session_buttons.columnconfigure(column, weight=1)
        self.session_start_button = ttk.Button(
            session_buttons,
            text="Start session",
            command=self.start_session,
            style="Primary.TButton",
        )
        self.session_start_button.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.session_pause_button = ttk.Button(
            session_buttons, text="Pause", command=self.toggle_session_pause
        )
        self.session_pause_button.grid(row=0, column=1, sticky="ew", padx=4)
        self.session_stop_button = ttk.Button(
            session_buttons,
            text="Stop session",
            command=self.stop_session,
            style="Stop.TButton",
        )
        self.session_stop_button.grid(row=0, column=2, sticky="ew", padx=(4, 0))

        session_log_frame = ttk.LabelFrame(parent, text="Session event log", padding=10)
        session_log_frame.grid(
            row=1, column=0, columnspan=2, sticky="nsew", pady=(12, 0)
        )
        session_log_frame.columnconfigure(0, weight=1)
        session_log_frame.rowconfigure(0, weight=1)
        self.session_log_widget = scrolledtext.ScrolledText(
            session_log_frame,
            height=9,
            wrap="word",
            state="disabled",
            font=("Consolas", 9),
            background="#0f172a",
            foreground="#e2e8f0",
            insertbackground="#e2e8f0",
        )
        self.session_log_widget.grid(row=0, column=0, sticky="nsew")
        session_log_footer = ttk.Frame(session_log_frame)
        session_log_footer.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        session_log_footer.columnconfigure(0, weight=1)
        session_log_path = (
            str(self.session_log_path)
            if self.session_log_path is not None
            else "Persistent session log unavailable"
        )
        ttk.Label(
            session_log_footer,
            text=f"Persistent log: {session_log_path}",
            style="Muted.TLabel",
        ).grid(row=0, column=0, sticky="w")
        ttk.Button(
            session_log_footer,
            text="Clear session log",
            command=self.clear_session_log,
            style="Workspace.TButton",
        ).grid(row=0, column=1, sticky="e")
        ttk.Label(
            session_log_footer,
            textvariable=self.session_report_var,
            style="Muted.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(5, 0))
        ttk.Button(
            session_log_footer,
            text="Open Excel reports",
            command=self.open_session_report_folder,
            style="Workspace.TButton",
        ).grid(row=1, column=1, sticky="e", pady=(5, 0))

        for variable in (
            self.session_recipe_var,
            self.session_loops_var,
            self.session_z_span_var,
            self.session_z_step_var,
            self.session_x_step_var,
            self.session_target_diameter_var,
            self.session_period_var,
            self.session_speed_var,
            self.session_accel_var,
        ):
            variable.trace_add("write", self._session_parameters_changed)
        self._session_parameters_changed()
        self._update_session_controls()

    def _build_excel_plotter_tab(self, parent: ttk.Frame) -> None:
        controls = ttk.LabelFrame(parent, text="Workbook input and calibration", padding=16)
        controls.grid(row=0, column=0, sticky="nsw", padx=(0, 12))
        controls.columnconfigure(1, weight=1)

        ttk.Label(controls, text="Session workbook").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(controls, textvariable=self.plotter_file_var, width=42).grid(
            row=1, column=0, columnspan=2, sticky="ew", pady=(0, 5)
        )
        ttk.Button(
            controls,
            text="Browse .xlsx",
            command=self.select_excel_plotter_file,
        ).grid(row=1, column=2, sticky="ew", padx=(7, 0), pady=(0, 5))

        parameter_rows = (
            ("Beam target diameter", self.plotter_target_diameter_var, "mm"),
            ("X controller calibration", self.plotter_x_scale_var, "pulses/mm"),
            ("Z controller calibration", self.plotter_z_scale_var, "pulses/mm"),
        )
        for row, (label, variable, unit) in enumerate(parameter_rows, start=2):
            ttk.Label(controls, text=label).grid(row=row, column=0, sticky="w", pady=4)
            ttk.Entry(controls, textvariable=variable, width=14).grid(
                row=row, column=1, sticky="ew", padx=(10, 7), pady=4
            )
            ttk.Label(controls, text=unit, style="Muted.TLabel").grid(
                row=row, column=2, sticky="w", pady=4
            )

        ttk.Checkbutton(
            controls,
            text="Use raw EVENT IDLE pulse positions when available",
            variable=self.plotter_use_event_positions_var,
            command=self._invalidate_excel_plotter,
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(9, 4))
        ttk.Label(
            controls,
            text=(
                "Recommended for legacy reports: this includes STEP-pulse rounding instead "
                "of trusting accumulated requested millimetres."
            ),
            style="Muted.TLabel",
            wraplength=390,
        ).grid(row=6, column=0, columnspan=3, sticky="ew", pady=(0, 8))

        ttk.Checkbutton(
            controls,
            text="Infer target center from measured X/Z extents",
            variable=self.plotter_auto_center_var,
            command=self._plotter_center_mode_changed,
        ).grid(row=7, column=0, columnspan=3, sticky="w", pady=4)
        ttk.Label(controls, text="Manual center X").grid(row=8, column=0, sticky="w", pady=4)
        self.plotter_center_x_entry = ttk.Entry(
            controls, textvariable=self.plotter_center_x_var, width=14
        )
        self.plotter_center_x_entry.grid(row=8, column=1, sticky="ew", padx=(10, 7), pady=4)
        ttk.Label(controls, text="mm", style="Muted.TLabel").grid(row=8, column=2, sticky="w")
        ttk.Label(controls, text="Manual center Z").grid(row=9, column=0, sticky="w", pady=4)
        self.plotter_center_z_entry = ttk.Entry(
            controls, textvariable=self.plotter_center_z_var, width=14
        )
        self.plotter_center_z_entry.grid(row=9, column=1, sticky="ew", padx=(10, 7), pady=4)
        ttk.Label(controls, text="mm", style="Muted.TLabel").grid(row=9, column=2, sticky="w")

        ttk.Separator(controls).grid(row=10, column=0, columnspan=3, sticky="ew", pady=12)
        self.plotter_load_button = ttk.Button(
            controls,
            text="Load workbook and preview",
            command=self.load_excel_beam_preview,
            style="Primary.TButton",
        )
        self.plotter_load_button.grid(row=11, column=0, columnspan=3, sticky="ew", pady=4)
        self.plotter_export_button = ttk.Button(
            controls,
            text="Create plotted Excel copy",
            command=self.export_excel_beam_plots,
        )
        self.plotter_export_button.grid(row=12, column=0, columnspan=3, sticky="ew", pady=4)
        self.plotter_open_button = ttk.Button(
            controls,
            text="Open plotted workbook",
            command=self.open_excel_beam_output,
        )
        self.plotter_open_button.grid(row=13, column=0, columnspan=3, sticky="ew", pady=4)
        self.plotter_export_button.state(["disabled"])
        self.plotter_open_button.state(["disabled"])

        ttk.Label(
            controls,
            textvariable=self.plotter_status_var,
            style="Muted.TLabel",
            wraplength=400,
            justify="left",
        ).grid(row=14, column=0, columnspan=3, sticky="ew", pady=(12, 3))
        ttk.Label(
            controls,
            textvariable=self.plotter_source_var,
            style="Muted.TLabel",
            wraplength=400,
            justify="left",
        ).grid(row=15, column=0, columnspan=3, sticky="ew", pady=3)

        preview = ttk.LabelFrame(parent, text="Independent beam-graph preview", padding=10)
        preview.grid(row=0, column=1, sticky="nsew")
        preview.columnconfigure(0, weight=1)
        preview.rowconfigure(1, weight=1)
        preview_toolbar = ttk.Frame(preview)
        preview_toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 7))
        preview_toolbar.columnconfigure(0, weight=1)
        ttk.Label(
            preview_toolbar,
            textvariable=self.plotter_metrics_var,
            style="Workspace.TLabel",
            wraplength=760,
            justify="left",
        ).grid(row=0, column=0, sticky="ew", padx=(0, 10))
        ttk.Label(preview_toolbar, text="Preview graph").grid(row=0, column=1, sticky="e", padx=(0, 6))
        self.plotter_view_combo = ttk.Combobox(
            preview_toolbar,
            textvariable=self.plotter_view_var,
            values=PLOTTER_VIEW_OPTIONS,
            state="readonly",
            width=24,
        )
        self.plotter_view_combo.grid(row=0, column=2, sticky="e")
        self.plotter_view_combo.bind(
            "<<ComboboxSelected>>",
            lambda _event: self._redraw_excel_beam_preview(),
        )
        self.plotter_canvas = tk.Canvas(
            preview,
            background="#ffffff",
            highlightbackground="#94a3b8",
            highlightthickness=1,
        )
        self.plotter_canvas.grid(row=1, column=0, sticky="nsew")
        self.plotter_canvas.bind("<Configure>", lambda _event: self._redraw_excel_beam_preview())
        ttk.Label(
            preview,
            text=(
                "The weather map is interpolated for visualization; measured values remain on "
                "Beam Heatmap and Beam Data. Analysis is offline and never overwrites the source."
            ),
            style="Muted.TLabel",
            wraplength=1000,
        ).grid(row=2, column=0, sticky="ew", pady=(7, 0))

        for variable in (
            self.plotter_file_var,
            self.plotter_target_diameter_var,
            self.plotter_x_scale_var,
            self.plotter_z_scale_var,
            self.plotter_center_x_var,
            self.plotter_center_z_var,
        ):
            variable.trace_add("write", self._invalidate_excel_plotter)
        self._update_plotter_center_controls()
        self.after_idle(self._redraw_excel_beam_preview)

    def select_excel_plotter_file(self) -> None:
        initial_directory = (
            str(self.session_report_directory)
            if self.session_report_directory is not None
            else str(Path.cwd())
        )
        filename = filedialog.askopenfilename(
            parent=self,
            title="Select a laser-stage session workbook",
            initialdir=initial_directory,
            filetypes=(("Excel workbooks", "*.xlsx"), ("All files", "*.*")),
        )
        if not filename:
            return
        self.plotter_file_var.set(filename)
        self.load_excel_beam_preview()

    def _plotter_center_mode_changed(self) -> None:
        self._update_plotter_center_controls()
        self._invalidate_excel_plotter()

    def _update_plotter_center_controls(self) -> None:
        state = ["disabled"] if self.plotter_auto_center_var.get() else ["!disabled"]
        if hasattr(self, "plotter_center_x_entry"):
            self.plotter_center_x_entry.state(state)
            self.plotter_center_z_entry.state(state)

    def _invalidate_excel_plotter(self, *_args: object) -> None:
        if not hasattr(self, "plotter_export_button") or self.plotter_busy:
            return
        self.plotter_dataset = None
        self.plotter_export_button.state(["disabled"])
        self.plotter_source_var.set("Position source: reload the workbook after changing parameters")
        self.plotter_status_var.set("Parameters changed. Click Load workbook and preview.")

    def _plotter_parameters(self) -> dict[str, object]:
        source_text = self.plotter_file_var.get().strip()
        if not source_text:
            raise ValueError("Choose an Excel .xlsx workbook first")
        source = Path(source_text)
        target = finite_float(self.plotter_target_diameter_var.get(), "Target diameter")
        x_scale = finite_float(self.plotter_x_scale_var.get(), "X pulses/mm")
        z_scale = finite_float(self.plotter_z_scale_var.get(), "Z pulses/mm")
        if target <= 0 or x_scale <= 0 or z_scale <= 0:
            raise ValueError("Target diameter and pulse calibrations must be greater than zero")
        auto_center = self.plotter_auto_center_var.get()
        center_x = None if auto_center else finite_float(self.plotter_center_x_var.get(), "Center X")
        center_z = None if auto_center else finite_float(self.plotter_center_z_var.get(), "Center Z")
        return {
            "source_path": source,
            "target_diameter_mm": target,
            "x_pulses_per_mm": x_scale,
            "z_pulses_per_mm": z_scale,
            "use_event_positions": self.plotter_use_event_positions_var.get(),
            "center_x_mm": center_x,
            "center_z_mm": center_z,
        }

    def load_excel_beam_preview(self) -> None:
        try:
            parameters = self._plotter_parameters()
        except (OSError, ValueError) as exc:
            messagebox.showerror("Cannot load workbook", str(exc), parent=self)
            return
        self.plotter_status_var.set("Reading and validating X, Z and dBm rows...")
        self._start_plotter_operation("load", lambda: load_beam_workbook(**parameters))

    def export_excel_beam_plots(self) -> None:
        dataset = self.plotter_dataset
        if dataset is None:
            messagebox.showwarning(
                "Load workbook first",
                "Load and preview the workbook before creating the plotted copy.",
                parent=self,
            )
            return
        self.plotter_status_var.set(
            "Creating weather map, 3D surface, profiles and measured-data sheets..."
        )
        self._start_plotter_operation("export", lambda: export_plotted_workbook(dataset))

    def open_excel_beam_output(self) -> None:
        path = self.plotter_output_path
        if path is None or not path.exists():
            messagebox.showwarning(
                "No plotted workbook",
                "Create a plotted Excel copy first.",
                parent=self,
            )
            return
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except OSError as exc:
            messagebox.showerror("Open workbook failed", str(exc), parent=self)

    def _start_plotter_operation(self, operation: str, action: object) -> None:
        if self.plotter_busy:
            return
        self.plotter_busy = True
        self.plotter_load_button.state(["disabled"])
        self.plotter_export_button.state(["disabled"])
        self.plotter_open_button.state(["disabled"])

        def worker() -> None:
            try:
                result = action()  # type: ignore[operator]
            except Exception as exc:
                self.events.put(("plotter", ("error", operation, str(exc))))
            else:
                self.events.put(("plotter", ("success", operation, result)))

        threading.Thread(target=worker, name=f"excel-plotter-{operation}", daemon=True).start()

    def _handle_plotter_event(self, payload: object) -> None:
        self.plotter_busy = False
        self.plotter_load_button.state(["!disabled"])
        try:
            result_kind, operation, result = payload  # type: ignore[misc]
        except (TypeError, ValueError):
            self.plotter_status_var.set("Malformed Excel plotter event ignored.")
            return
        if result_kind == "error":
            self.plotter_status_var.set(f"Excel {operation} failed: {result}")
            if self.plotter_dataset is not None:
                self.plotter_export_button.state(["!disabled"])
            if self.plotter_output_path is not None:
                self.plotter_open_button.state(["!disabled"])
            messagebox.showerror("Excel beam plotter failed", str(result), parent=self)
            return

        if operation == "load" and isinstance(result, BeamWorkbookDataset):
            self.plotter_dataset = result
            self.plotter_output_path = None
            analysis = result.analysis
            self.plotter_status_var.set(
                f"Loaded {result.valid_rows:,} valid readings from {result.sheet_name!r}. "
                "Choose any preview graph, then create the plotted Excel copy."
            )
            self.plotter_source_var.set(
                f"Position source: {result.position_source_summary}; "
                f"X={result.x_pulses_per_mm:g}, Z={result.z_pulses_per_mm:g} pulses/mm"
            )
            peak = "--" if analysis.peak_power_dbm is None else f"{analysis.peak_power_dbm:.6f} dBm"
            offset = (
                "--"
                if analysis.centroid_offset_mm is None
                else f"{analysis.centroid_offset_mm:.3f} mm"
            )
            self.plotter_metrics_var.set(
                f"Valid readings {result.valid_rows:,}  ·  Inside target {len(analysis.inside_points):,}  ·  "
                f"Peak {peak}  ·  Centroid offset {offset}  ·  "
                f"Target {analysis.target_diameter_mm:g} mm"
            )
            self.plotter_export_button.state(["!disabled"])
            self._redraw_excel_beam_preview()
        elif operation == "export" and isinstance(result, BeamPlotExportResult):
            self.plotter_output_path = result.output_path
            self.plotter_status_var.set(f"Plotted Excel workbook created: {result.output_path}")
            self.plotter_export_button.state(["!disabled"])
            self.plotter_open_button.state(["!disabled"])

    def _redraw_excel_beam_preview(self) -> None:
        if not hasattr(self, "plotter_canvas"):
            return
        canvas = self.plotter_canvas
        canvas.delete("all")
        width = max(canvas.winfo_width(), 500)
        height = max(canvas.winfo_height(), 420)
        dataset = self.plotter_dataset
        if dataset is None:
            canvas.create_text(
                width / 2,
                height / 2,
                text="Select a session workbook\nthen click Load workbook and preview",
                fill=TEXT_MUTED,
                font=("Segoe UI Semibold", 14),
                justify="center",
            )
            return

        analysis = dataset.analysis
        if analysis.center_x_mm is None or analysis.center_z_mm is None:
            return
        selected_view = self.plotter_view_var.get()
        if selected_view == "Weather heatmap":
            self._draw_weather_heatmap_preview(canvas, width, height, dataset)
            return
        if selected_view in (
            "X centerline profile",
            "Z centerline profile",
            "Radial average profile",
        ):
            self._draw_beam_profile_preview(canvas, width, height, dataset, selected_view)
            return
        plot_size = max(220.0, min(width - 150.0, height - 145.0))
        left = (width - plot_size) / 2.0
        top = 55.0
        x_low = min(analysis.x_min_mm, analysis.center_x_mm - analysis.target_radius_mm)
        x_high = max(analysis.x_max_mm, analysis.center_x_mm + analysis.target_radius_mm)
        z_low = min(analysis.z_min_mm, analysis.center_z_mm - analysis.target_radius_mm)
        z_high = max(analysis.z_max_mm, analysis.center_z_mm + analysis.target_radius_mm)
        half_span = max(x_high - x_low, z_high - z_low) * 0.52
        plot_center_x = (x_low + x_high) / 2.0
        plot_center_z = (z_low + z_high) / 2.0
        x_min = plot_center_x - half_span
        z_min = plot_center_z - half_span

        def screen_x(value: float) -> float:
            return left + (value - x_min) * plot_size / (2.0 * half_span)

        def screen_y(value: float) -> float:
            return top + plot_size - (value - z_min) * plot_size / (2.0 * half_span)

        canvas.create_text(
            width / 2,
            22,
            text="Complete measured X-Z scan - all valid readings",
            fill=TEXT_PRIMARY,
            font=("Segoe UI Semibold", 13),
        )
        for tick in range(11):
            value_x = x_min + tick * 2.0 * half_span / 10.0
            value_z = z_min + tick * 2.0 * half_span / 10.0
            x = screen_x(value_x)
            y = screen_y(value_z)
            canvas.create_line(x, top, x, top + plot_size, fill="#e2e8f0")
            canvas.create_line(left, y, left + plot_size, y, fill="#e2e8f0")
            if tick % 2 == 0:
                canvas.create_text(x, top + plot_size + 16, text=f"{value_x:g}", fill=TEXT_MUTED, font=("Segoe UI", 8))
                canvas.create_text(left - 28, y, text=f"{value_z:g}", fill=TEXT_MUTED, font=("Segoe UI", 8))
        canvas.create_rectangle(left, top, left + plot_size, top + plot_size, outline="#64748b")

        for point in analysis.points:
            x = screen_x(point.x_mm)
            y = screen_y(point.z_mm)
            if not (left <= x <= left + plot_size and top <= y <= top + plot_size):
                continue
            color = self._beam_preview_color(point.relative_db)
            radius = 3.2 if point.inside_target else 2.7
            canvas.create_oval(x - radius, y - radius, x + radius, y + radius, fill=color, outline="")

        circle_left = screen_x(analysis.center_x_mm - analysis.target_radius_mm)
        circle_right = screen_x(analysis.center_x_mm + analysis.target_radius_mm)
        circle_top = screen_y(analysis.center_z_mm + analysis.target_radius_mm)
        circle_bottom = screen_y(analysis.center_z_mm - analysis.target_radius_mm)
        canvas.create_oval(
            circle_left,
            circle_top,
            circle_right,
            circle_bottom,
            outline="#334155",
            width=2,
        )
        center_x = screen_x(analysis.center_x_mm)
        center_y = screen_y(analysis.center_z_mm)
        canvas.create_line(center_x - 7, center_y, center_x + 7, center_y, fill="#0f172a", width=2)
        canvas.create_line(center_x, center_y - 7, center_x, center_y + 7, fill="#0f172a", width=2)
        if analysis.centroid_x_mm is not None and analysis.centroid_z_mm is not None:
            x = screen_x(analysis.centroid_x_mm)
            y = screen_y(analysis.centroid_z_mm)
            canvas.create_oval(x - 6, y - 6, x + 6, y + 6, fill="#7030A0", outline="#ffffff", width=1)
        if analysis.peak_x_mm is not None and analysis.peak_z_mm is not None:
            x = screen_x(analysis.peak_x_mm)
            y = screen_y(analysis.peak_z_mm)
            points: list[float] = []
            for index in range(10):
                angle = -math.pi / 2.0 + index * math.pi / 5.0
                radius = 8.0 if index % 2 == 0 else 3.5
                points.extend((x + radius * math.cos(angle), y + radius * math.sin(angle)))
            canvas.create_polygon(points, fill="#C00000", outline="#ffffff")

        canvas.create_text(width / 2, top + plot_size + 38, text="X position (mm)", fill=TEXT_PRIMARY, font=("Segoe UI", 9))
        canvas.create_text(
            left - 62,
            top + plot_size / 2,
            text="Z position (mm)",
            fill=TEXT_PRIMARY,
            font=("Segoe UI", 9),
            angle=90,
        )
        legend_y = height - 25
        legend_x = 20.0
        for label, _lower, _upper, color in BEAM_POWER_BANDS:
            canvas.create_oval(legend_x, legend_y - 4, legend_x + 8, legend_y + 4, fill=f"#{color}", outline="")
            canvas.create_text(legend_x + 13, legend_y, text=label, anchor="w", fill=TEXT_MUTED, font=("Segoe UI", 8))
            legend_x += 105.0

    def _draw_weather_heatmap_preview(
        self,
        canvas: tk.Canvas,
        width: int,
        height: int,
        dataset: BeamWorkbookDataset,
    ) -> None:
        analysis = dataset.analysis
        grid = dataset.weather_grid
        if (
            not grid.relative_db
            or analysis.center_x_mm is None
            or analysis.center_z_mm is None
        ):
            canvas.create_text(
                width / 2,
                height / 2,
                text="Not enough valid X/Z power points for a weather map",
                fill=TEXT_MUTED,
                font=("Segoe UI Semibold", 13),
            )
            return

        plot_size = max(220.0, min(width - 150.0, height - 150.0))
        left = (width - plot_size) / 2.0
        top = 52.0
        x_low = min(min(grid.x_values_mm), analysis.center_x_mm - analysis.target_radius_mm)
        x_high = max(max(grid.x_values_mm), analysis.center_x_mm + analysis.target_radius_mm)
        z_low = min(min(grid.z_values_mm), analysis.center_z_mm - analysis.target_radius_mm)
        z_high = max(max(grid.z_values_mm), analysis.center_z_mm + analysis.target_radius_mm)
        half_span = max(x_high - x_low, z_high - z_low) * 0.52
        plot_center_x = (x_low + x_high) / 2.0
        plot_center_z = (z_low + z_high) / 2.0
        plot_x_min = plot_center_x - half_span
        plot_z_min = plot_center_z - half_span
        x_grid_step = (
            abs(grid.x_values_mm[1] - grid.x_values_mm[0]) if grid.size > 1 else 0.0
        )
        z_grid_step = (
            abs(grid.z_values_mm[1] - grid.z_values_mm[0]) if grid.size > 1 else 0.0
        )
        half_cell_x = x_grid_step * plot_size / (4.0 * half_span)
        half_cell_y = z_grid_step * plot_size / (4.0 * half_span)

        def screen_x(value: float) -> float:
            return left + (value - plot_x_min) * plot_size / (2.0 * half_span)

        def screen_y(value: float) -> float:
            return top + plot_size - (value - plot_z_min) * plot_size / (2.0 * half_span)

        canvas.create_text(
            width / 2,
            20,
            text="Weather heatmap - complete measured scan",
            fill=TEXT_PRIMARY,
            font=("Segoe UI Semibold", 13),
        )
        for row_index, (z_mm, row) in enumerate(zip(grid.z_values_mm, grid.relative_db)):
            y = screen_y(z_mm)
            for column_index, value in enumerate(row):
                if value is None:
                    continue
                x = screen_x(grid.x_values_mm[column_index])
                canvas.create_rectangle(
                    max(left, x - half_cell_x),
                    max(top, y - half_cell_y),
                    min(left + plot_size, x + half_cell_x),
                    min(top + plot_size, y + half_cell_y),
                    fill=self._weather_preview_color(value),
                    outline="",
                )

        for tick in range(11):
            x_value = plot_x_min + tick * 2.0 * half_span / 10.0
            z_value = plot_z_min + tick * 2.0 * half_span / 10.0
            x = screen_x(x_value)
            y = screen_y(z_value)
            canvas.create_line(x, top, x, top + plot_size, fill="#ffffff", stipple="gray50")
            canvas.create_line(left, y, left + plot_size, y, fill="#ffffff", stipple="gray50")
            if tick % 2 == 0:
                canvas.create_text(
                    x,
                    top + plot_size + 16,
                    text=f"{x_value:g}",
                    fill=TEXT_MUTED,
                    font=("Segoe UI", 8),
                )
                canvas.create_text(
                    left - 28,
                    y,
                    text=f"{z_value:g}",
                    fill=TEXT_MUTED,
                    font=("Segoe UI", 8),
                )

        canvas.create_oval(
            screen_x(analysis.center_x_mm - analysis.target_radius_mm),
            screen_y(analysis.center_z_mm + analysis.target_radius_mm),
            screen_x(analysis.center_x_mm + analysis.target_radius_mm),
            screen_y(analysis.center_z_mm - analysis.target_radius_mm),
            outline="#0f172a",
            width=2,
        )
        marker_specs = (
            (analysis.center_x_mm, analysis.center_z_mm, "#0f172a", "C", 5),
            (analysis.centroid_x_mm, analysis.centroid_z_mm, "#7030A0", "P", 6),
            (analysis.peak_x_mm, analysis.peak_z_mm, "#C00000", "M", 6),
        )
        for x_mm, z_mm, color, label, radius in marker_specs:
            if x_mm is None or z_mm is None:
                continue
            x = screen_x(x_mm)
            y = screen_y(z_mm)
            canvas.create_oval(
                x - radius,
                y - radius,
                x + radius,
                y + radius,
                fill=color,
                outline="#ffffff",
                width=1,
            )
            canvas.create_text(x, y, text=label, fill="#ffffff", font=("Segoe UI Bold", 7))

        canvas.create_text(
            width / 2,
            top + plot_size + 39,
            text="X position (mm)",
            fill=TEXT_PRIMARY,
            font=("Segoe UI", 9),
        )
        canvas.create_text(
            left - 62,
            top + plot_size / 2,
            text="Z position (mm)",
            fill=TEXT_PRIMARY,
            font=("Segoe UI", 9),
            angle=90,
        )

        legend_y = height - 25
        legend_left = max(20.0, width / 2.0 - 190.0)
        legend_width = 270.0
        legend_min = min(
            -15.0,
            min(value for row in grid.relative_db for value in row if value is not None),
        )
        for index in range(135):
            value = legend_min + (0.0 - legend_min) * index / 134.0
            x0 = legend_left + legend_width * index / 135.0
            x1 = legend_left + legend_width * (index + 1) / 135.0
            canvas.create_rectangle(
                x0,
                legend_y - 6,
                x1 + 1,
                legend_y + 6,
                fill=self._weather_preview_color(value),
                outline="",
            )
        canvas.create_text(
            legend_left - 8,
            legend_y,
            text=f"{legend_min:g}",
            anchor="e",
            fill=TEXT_MUTED,
            font=("Segoe UI", 8),
        )
        canvas.create_text(
            legend_left + legend_width + 8,
            legend_y,
            text="0 dB",
            anchor="w",
            fill=TEXT_MUTED,
            font=("Segoe UI", 8),
        )
        canvas.create_text(
            legend_left + legend_width + 75,
            legend_y,
            text="C center  P centroid  M peak",
            anchor="w",
            fill=TEXT_MUTED,
            font=("Segoe UI", 8),
        )

    def _draw_beam_profile_preview(
        self,
        canvas: tk.Canvas,
        width: int,
        height: int,
        dataset: BeamWorkbookDataset,
        selected_view: str,
    ) -> None:
        analysis = dataset.analysis
        grid = dataset.weather_grid
        if analysis.center_x_mm is None or analysis.center_z_mm is None:
            return
        if selected_view == "X centerline profile":
            points = tuple(
                (x_mm - analysis.center_x_mm, value)
                for x_mm, value in grid.x_profile
            )
            title = "X centerline power profile"
            x_title = "X offset from target center (mm)"
            x_min, x_max = -analysis.target_radius_mm, analysis.target_radius_mm
            color = "#2F75B5"
        elif selected_view == "Z centerline profile":
            points = tuple(
                (z_mm - analysis.center_z_mm, value)
                for z_mm, value in grid.z_profile
            )
            title = "Z centerline power profile"
            x_title = "Z offset from target center (mm)"
            x_min, x_max = -analysis.target_radius_mm, analysis.target_radius_mm
            color = "#70AD47"
        else:
            radial = radial_average_profile(analysis)
            points = tuple((radius_mm, value) for radius_mm, value, _count in radial)
            title = "Measured radial-average power profile"
            x_title = "Radius from target center (mm)"
            x_min, x_max = 0.0, analysis.target_radius_mm
            color = "#7030A0"

        if not points:
            canvas.create_text(
                width / 2,
                height / 2,
                text="Not enough valid data for this profile",
                fill=TEXT_MUTED,
                font=("Segoe UI Semibold", 13),
            )
            return
        y_min = min(-5.0, math.floor(min(value for _position, value in points) / 5.0) * 5.0)
        y_max = 0.5
        left, right, top, bottom = 82.0, width - 34.0, 58.0, height - 70.0
        plot_width = max(200.0, right - left)
        plot_height = max(180.0, bottom - top)

        def screen_x(value: float) -> float:
            return left + (value - x_min) * plot_width / max(x_max - x_min, 1e-12)

        def screen_y(value: float) -> float:
            return top + (y_max - value) * plot_height / max(y_max - y_min, 1e-12)

        canvas.create_text(
            width / 2,
            23,
            text=title,
            fill=TEXT_PRIMARY,
            font=("Segoe UI Semibold", 13),
        )
        for tick in range(11):
            value = x_min + (x_max - x_min) * tick / 10.0
            x = screen_x(value)
            canvas.create_line(x, top, x, bottom, fill="#e2e8f0")
            if tick % 2 == 0:
                canvas.create_text(
                    x,
                    bottom + 16,
                    text=f"{value:g}",
                    fill=TEXT_MUTED,
                    font=("Segoe UI", 8),
                )
        y_steps = max(1, int(abs(y_min) / 5.0))
        for tick in range(y_steps + 1):
            value = y_min + (0.0 - y_min) * tick / y_steps
            y = screen_y(value)
            canvas.create_line(left, y, right, y, fill="#e2e8f0")
            canvas.create_text(
                left - 10,
                y,
                text=f"{value:g}",
                anchor="e",
                fill=TEXT_MUTED,
                font=("Segoe UI", 8),
            )
        if y_min <= -3.0 <= y_max:
            threshold_y = screen_y(-3.0)
            canvas.create_line(left, threshold_y, right, threshold_y, fill="#C00000", width=2, dash=(6, 4))
            canvas.create_text(
                right - 3,
                threshold_y - 8,
                text="-3 dB",
                anchor="e",
                fill="#C00000",
                font=("Segoe UI Semibold", 8),
            )
        canvas.create_rectangle(left, top, right, bottom, outline="#64748b")
        coordinates: list[float] = []
        for position, value in points:
            coordinates.extend((screen_x(position), screen_y(value)))
        if len(coordinates) >= 4:
            canvas.create_line(*coordinates, fill=color, width=3, smooth=True)
        for position, value in points:
            x, y = screen_x(position), screen_y(value)
            canvas.create_oval(x - 2.5, y - 2.5, x + 2.5, y + 2.5, fill=color, outline="#ffffff")
        canvas.create_text(
            width / 2,
            height - 30,
            text=x_title,
            fill=TEXT_PRIMARY,
            font=("Segoe UI", 9),
        )
        canvas.create_text(
            22,
            top + plot_height / 2,
            text="Power relative to peak (dB)",
            fill=TEXT_PRIMARY,
            font=("Segoe UI", 9),
            angle=90,
        )

    @staticmethod
    def _weather_preview_color(relative_db: float) -> str:
        stops = WEATHER_COLOR_STOPS
        if relative_db <= stops[0][0]:
            return f"#{stops[0][1]}"
        if relative_db >= stops[-1][0]:
            return f"#{stops[-1][1]}"
        for (lower_value, lower_color), (upper_value, upper_color) in zip(stops, stops[1:]):
            if lower_value <= relative_db <= upper_value:
                fraction = (relative_db - lower_value) / (upper_value - lower_value)
                lower_rgb = tuple(int(lower_color[index : index + 2], 16) for index in (0, 2, 4))
                upper_rgb = tuple(int(upper_color[index : index + 2], 16) for index in (0, 2, 4))
                blended = tuple(
                    round(start + (end - start) * fraction)
                    for start, end in zip(lower_rgb, upper_rgb)
                )
                return "#" + "".join(f"{component:02X}" for component in blended)
        return "#64748b"

    @staticmethod
    def _beam_preview_color(relative_db: float) -> str:
        for _label, lower, upper, color in BEAM_POWER_BANDS:
            if lower is not None and relative_db < lower:
                continue
            if upper is not None and relative_db >= upper:
                continue
            return f"#{color}"
        return "#64748b"

    @staticmethod
    def _format_duration(seconds: float) -> str:
        total_seconds = max(0, int(round(seconds)))
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds_part = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds_part:02d}"

    def _selected_session_axis(self) -> str:
        recipe = self.session_recipe_var.get()
        if recipe == Z_SERPENTINE_RECIPE:
            return "Z"
        if recipe == X_SERPENTINE_RECIPE:
            return "X"
        raise ValueError("Select a supported session recipe")

    def _update_session_recipe_text(self) -> None:
        try:
            scan_axis = self._selected_session_axis()
        except ValueError:
            return
        cross_axis = "X" if scan_axis == "Z" else "Z"
        scan_scale = finite_float(
            self.current_status.get(f"{scan_axis}SPMM", DEFAULT_SCALE[scan_axis]),
            f"{scan_axis} calibration",
        )
        cross_scale = finite_float(
            self.current_status.get(f"{cross_axis}SPMM", DEFAULT_SCALE[cross_axis]),
            f"{cross_axis} calibration",
        )
        self.session_scan_span_label.configure(text=f"{scan_axis} scan span")
        self.session_scan_increment_label.configure(text=f"{scan_axis} increment")
        self.session_cross_shift_label.configure(
            text=f"{cross_axis} shift after each pass"
        )
        self.session_step_period_label.configure(text="Post-move measurement dwell")
        self.session_sequence_var.set(
            f"Sequence per loop: +{scan_axis} span in stepped increments → "
            f"{cross_axis}+ shift → −{scan_axis} span → {cross_axis}+ shift. "
            f"The next loop repeats from {scan_axis} = 0 mm."
        )
        self.session_requirement_var.set(
            "Start requirement: controller online and idle, drivers enabled, X/Z zero "
            f"references set, {scan_axis} at 0 mm, and enough remaining "
            f"+{cross_axis} travel. E5574A VISA must be connected; every completed "
            "move is logged with a Head A absolute-power reading in dBm. "
            f"Pulse resolution: {scan_axis} {format_micrometres(pulse_resolution_um(scan_scale))} µm, "
            f"{cross_axis} {format_micrometres(pulse_resolution_um(cross_scale))} µm."
        )

    def _parse_session_config(self) -> SerpentineSessionConfig:
        scan_axis = self._selected_session_axis()
        cross_axis = "X" if scan_axis == "Z" else "Z"
        loops_text = self.session_loops_var.get().strip()
        try:
            loops = int(loops_text)
        except ValueError as exc:
            raise ValueError("Loop count must be a whole number") from exc
        config = SerpentineSessionConfig(
            loops=loops,
            z_span_mm=finite_float(
                self.session_z_span_var.get(), f"{scan_axis} span"
            ),
            z_step_mm=finite_float(
                self.session_z_step_var.get(), f"{scan_axis} increment"
            ),
            x_step_mm=finite_float(
                self.session_x_step_var.get(), f"{cross_axis} shift"
            ),
            step_period_s=finite_float(
                self.session_period_var.get(), "Post-move measurement dwell"
            ),
            speed_mm_s=finite_float(self.session_speed_var.get(), "Session speed"),
            acceleration_mm_s2=finite_float(
                self.session_accel_var.get(), "Session acceleration"
            ),
            scan_axis=scan_axis,
            target_diameter_mm=finite_float(
                self.session_target_diameter_var.get(), "Target beam diameter"
            ),
        )
        config.validate()
        return config

    def _session_parameters_changed(self, *_args: object) -> None:
        self._update_session_recipe_text()
        try:
            config = self._parse_session_config()
            self.session_time_var.set(
                f"Estimated duration: {self._format_duration(config.estimated_duration_s)}  ·  "
                f"{config.total_actions:,} actions  ·  total {config.cross_axis} advance "
                f"{config.total_cross_advance_mm:g} mm"
            )
        except ValueError as exc:
            self.session_time_var.set(f"Parameter check: {exc}")

    def _update_session_controls(self) -> None:
        if not hasattr(self, "session_start_button"):
            return
        status = self.current_status
        ready = (
            self.controller_verified
            and status.get("ENABLED") == "1"
            and status.get("BUSY") != "1"
            and status.get("ESTOP") != "1"
            and status.get("FAULT", "NONE") == "NONE"
            and self.visa_instrument is not None
            and self.visa_instrument.is_open
            and not self.visa_busy
            and self.session_report_directory is not None
        )
        self.session_start_button.state(
            ["!disabled"] if ready and not self.session_running else ["disabled"]
        )
        self.session_pause_button.state(
            ["!disabled"] if self.session_running else ["disabled"]
        )
        self.session_stop_button.state(
            ["!disabled"] if self.session_running else ["disabled"]
        )
        self.session_pause_button.configure(
            text="Resume" if self.session_paused else "Pause"
        )
        for widget in self.session_parameter_widgets:
            widget.state(["disabled"] if self.session_running else ["!disabled"])

    def start_session(self) -> None:
        try:
            config = self._parse_session_config()
        except ValueError as exc:
            messagebox.showerror("Invalid session", str(exc), parent=self)
            return

        status = self.current_status
        if not self.controller_verified or self.serial_port is None:
            messagebox.showerror("Session unavailable", "Connect to the controller first.", parent=self)
            return
        if self.visa_instrument is None or not self.visa_instrument.is_open:
            messagebox.showerror(
                "E5574A connection required",
                "Connect the HP E5574A in the E5574A / GPIB tab before starting. "
                "The session requires one Head A power reading for every completed move.",
                parent=self,
            )
            return
        if self.visa_busy:
            messagebox.showerror(
                "E5574A is busy",
                "Wait for the current VISA operation to finish before starting the session.",
                parent=self,
            )
            return
        if self.session_report_directory is None:
            messagebox.showerror(
                "Excel report unavailable",
                "The session cannot start because the Excel report folder could not be created.",
                parent=self,
            )
            return
        if status.get("ENABLED") != "1":
            messagebox.showerror("Session unavailable", "Enable the motor drivers first.", parent=self)
            return
        if status.get("BUSY") == "1":
            messagebox.showerror("Session unavailable", "Wait for the current move to finish.", parent=self)
            return
        if status.get("ESTOP") == "1" or status.get("FAULT", "NONE") != "NONE":
            messagebox.showerror(
                "Session unavailable", "Clear the E-stop/fault before starting.", parent=self
            )
            return
        if status.get("XH") != "1" or status.get("ZH") != "1":
            messagebox.showerror(
                "Coordinate reference required",
                "Set X/Z zero at the safe lower-left starting point before starting a session.",
                parent=self,
            )
            return

        try:
            start_x = finite_float(status.get("XMM", "nan"), "Current X")
            start_z = finite_float(status.get("ZMM", "nan"), "Current Z")
        except ValueError as exc:
            messagebox.showerror("Invalid controller position", str(exc), parent=self)
            return
        scan_axis = config.scan_axis.upper()
        cross_axis = config.cross_axis
        scan_scale = finite_float(
            status.get(f"{scan_axis}SPMM", DEFAULT_SCALE[scan_axis]),
            f"{scan_axis} calibration",
        )
        tolerance = 0.5 / scan_scale + 1e-6
        positions = {"X": start_x, "Z": start_z}
        travel = {"X": X_TRAVEL_MM, "Z": Z_TRAVEL_MM}
        start_scan = positions[scan_axis]
        start_cross = positions[cross_axis]
        if abs(start_scan) > tolerance:
            messagebox.showerror(
                f"{scan_axis} must start at zero",
                f"This recipe starts at {scan_axis} = 0 mm. Current {scan_axis} is "
                f"{start_scan:.3f} mm. Move safely to {scan_axis} = 0 before starting.",
                parent=self,
            )
            return
        final_cross = start_cross + config.total_cross_advance_mm
        if start_cross < -tolerance or final_cross > travel[cross_axis] + tolerance:
            maximum_loops = max(
                0,
                int(
                    (travel[cross_axis] - max(start_cross, 0.0))
                    // (2.0 * config.cross_step_mm)
                ),
            )
            messagebox.showerror(
                f"Insufficient {cross_axis} travel",
                f"This session would finish at {cross_axis} = {final_cross:.3f} mm, "
                f"beyond the {travel[cross_axis]:g} mm envelope. From the current "
                f"{cross_axis} position, use at most {maximum_loops} loop(s) with this "
                f"{cross_axis} shift.",
                parent=self,
            )
            return

        actions = build_serpentine_actions(config)
        final_positions = dict(positions)
        final_positions[scan_axis] = 0.0
        final_positions[cross_axis] = final_cross
        confirmation = (
            f"Start stepped {scan_axis} serpentine session?\n\n"
            f"Loops: {config.loops}\n"
            f"{scan_axis} travel per pass: {config.scan_span_mm:g} mm in "
            f"{config.scan_step_mm:g} mm steps\n"
            f"{cross_axis} shift after each pass: +{config.cross_step_mm:g} mm\n"
            f"After every completed move: wait {config.step_period_s:g} s and record "
            "E5574A Head A absolute power in dBm\n"
            f"Excel beam-map target: {config.target_diameter_mm:g} mm diameter\n"
            f"Expected final position: X {final_positions['X']:.3f} mm, "
            f"Z {final_positions['Z']:.3f} mm\n"
            f"Estimated duration: {self._format_duration(config.estimated_duration_s)}\n\n"
            "Confirm that the calculated X-Z path is mechanically clear and the laser "
            "is configured safely."
        )
        if not messagebox.askokcancel("Start long-running session", confirmation, parent=self):
            return

        started_wallclock = datetime.now()
        session_id = started_wallclock.strftime("%Y%m%d-%H%M%S-%f")[:-3]
        report_path = self.session_report_directory / f"LaserStageSession_{session_id}.xlsx"
        report = SessionExcelReport(
            report_path,
            session_id=session_id,
            started_at=started_wallclock,
            metadata={
                "Recipe": self.session_recipe_var.get(),
                "Controller firmware": self.firmware_version,
                "VISA resource": self.visa_resource_var.get().strip(),
                "Instrument": self.visa_idn_var.get(),
                "Scan axis": scan_axis,
                "Cross axis": cross_axis,
                "Loops": config.loops,
                "Scan span (mm)": config.scan_span_mm,
                "Scan increment (mm)": config.scan_step_mm,
                "Cross shift (mm)": config.cross_step_mm,
                "Target diameter (mm)": config.target_diameter_mm,
                "Post-move dwell (s)": config.step_period_s,
                "Motion speed (mm/s)": config.speed_mm_s,
                "Acceleration (mm/s^2)": config.acceleration_mm_s2,
                "X pulses/mm": status.get("XSPMM", ""),
                "Z pulses/mm": status.get("ZSPMM", ""),
                "Expected actions": len(actions),
            },
        )

        self.session_config = config
        self.session_actions = actions
        self.session_action_index = 0
        self.session_current_action = None
        self.session_waiting_for_idle = False
        self.session_waiting_for_measurement = False
        self.session_measurement_in_progress = False
        self.session_preparing_visa = True
        self.session_running = True
        self.session_paused = False
        self.session_started_at = time.monotonic()
        self.session_started_wallclock = started_wallclock
        self.session_paused_duration = 0.0
        self.session_next_action_at = float("inf")
        self.session_expected_positions = {"X": start_x, "Z": start_z}
        self.session_report_path = report_path
        self.session_report = report
        self.session_report_var.set(f"Current Excel report: {report_path}")
        self.session_progress_var.set(0.0)
        self.session_progress_text_var.set(f"0 / {len(actions):,} actions")
        self.session_state_var.set("Running")
        self.session_phase_var.set("Preparing E5574A Head A for absolute dBm readings")
        self.session_runtime_var.set(
            f"Elapsed 00:00:00  ·  Remaining {self._format_duration(config.estimated_duration_s)}"
        )
        self._session_log(
            f"START stepped {scan_axis} serpentine: "
            f"loops={config.loops}, {scan_axis} span={config.scan_span_mm:g} mm, "
            f"{scan_axis} step={config.scan_step_mm:g} mm, "
            f"post-move dwell={config.step_period_s:g} s, "
            f"{cross_axis} shift={config.cross_step_mm:g} mm, report={report_path}",
            event_type="SESSION_START",
        )
        try:
            report.save(
                status="Running",
                reason="Preparing E5574A and controller",
                ended_at=started_wallclock,
                duration_s=0.0,
                completed_actions=0,
            )
        except Exception as exc:
            self.session_report = None
            self._abort_session(f"Unable to create Excel report: {exc}", send_stop=False)
            messagebox.showerror(
                "Excel report unavailable",
                f"The session was not started because its Excel workbook could not be created:\n\n{exc}",
                parent=self,
            )
            return
        self._prepare_session_power_acquisition()
        self.message_var.set(
            "Session started. E5574A power and every X/Z move will be recorded in Excel."
        )
        self._update_control_states()
        self._update_session_controls()

    def _prepare_session_power_acquisition(self) -> None:
        instrument = self.visa_instrument
        if instrument is None:
            self._abort_session("E5574A connection was lost before preparation", send_stop=False)
            return

        def action() -> str:
            with self.visa_lock:
                return instrument.configure_power_dbm(SESSION_POWER_CHANNEL)

        self.visa_status_var.set(
            "Preparing E5574A Head A: Powermeter, absolute mode, dBm..."
        )
        self._start_visa_operation("session_prepare", action)

    def _start_session_motion(self) -> None:
        if not self.session_running or self.session_config is None:
            return
        config = self.session_config
        speed_ok = self.send_command(f"SPEED_MM ALL {config.speed_mm_s:g}")
        accel_ok = self.send_command(f"ACCEL_MM ALL {config.acceleration_mm_s2:g}")
        if not speed_ok or not accel_ok:
            self._abort_session(
                "Unable to configure controller motion settings", send_stop=False
            )
            return
        self.session_preparing_visa = False
        self.session_next_action_at = time.monotonic() + 0.3
        self.session_phase_var.set("E5574A ready; preparing controller motion settings")
        self._session_log(
            "E5574A configured: Head A, absolute power, dBm",
            event_type="GPIB_CONFIGURED",
        )

    def _session_tick(self) -> None:
        try:
            if self.session_running:
                now = time.monotonic()
                self._update_session_runtime(now)
                if self.serial_port is None or not self.controller_verified:
                    self._abort_session("Controller connection lost", send_stop=False)
                elif self.current_status.get("ESTOP") == "1":
                    self._abort_session("Software E-stop is active", send_stop=False)
                elif self.current_status.get("FAULT", "NONE") != "NONE":
                    self._abort_session(
                        f"Controller fault: {self.current_status.get('FAULT')}",
                        send_stop=False,
                    )
                elif self.current_status.get("ENABLED") != "1":
                    self._abort_session("Motor drivers became disabled", send_stop=False)
                elif self.session_waiting_for_idle:
                    action = self.session_current_action
                    if action is not None and self.session_config is not None:
                        ideal_time = self.session_config._ideal_move_time(
                            action.distance_mm
                        )
                        completion_timeout = max(
                            10.0,
                            ideal_time * 5.0 + 2.0,
                        )
                        if now - self.session_action_started_at > completion_timeout:
                            self._abort_session(
                                "Timed out waiting for the controller EVENT IDLE reply"
                            )
                elif self.session_waiting_for_measurement:
                    if (
                        not self.session_measurement_in_progress
                        and now >= self.session_measurement_due_at
                    ):
                        self._start_session_power_measurement()
                elif (
                    not self.session_paused
                    and not self.session_preparing_visa
                    and self.current_status.get("BUSY") != "1"
                    and now >= self.session_next_action_at
                ):
                    self._dispatch_session_action()
        finally:
            self.after(SESSION_TICK_MS, self._session_tick)

    def _dispatch_session_action(self) -> None:
        if not self.session_running or self.session_action_index >= len(self.session_actions):
            self._finish_session()
            return
        action = self.session_actions[self.session_action_index]
        now = time.monotonic()
        self.session_current_action = action
        self.session_action_started_at = now
        self.session_action_started_wallclock = datetime.now()
        self.session_waiting_for_idle = True
        self.session_phase_var.set(
            f"Loop {action.loop_number}/{self.session_config.loops if self.session_config else '?'}  ·  "
            f"{action.phase}  ·  {action.phase_step}/{action.phase_steps}"
        )
        if (
            "shift" in action.phase
            or action.phase_step == 1
            or action.phase_step == action.phase_steps
            or action.phase_step % 10 == 0
        ):
            self._session_log(action.label)
        self._add_session_report_event(
            "MOVE_SENT",
            f"Action {self.session_action_index + 1}/{len(self.session_actions)}: "
            f"{action.label}; command={action.command}",
            timestamp=self.session_action_started_wallclock,
            elapsed_s=max(0.0, now - self.session_started_at),
        )
        if not self.send_command(action.command):
            self._abort_session(f"Failed to send {action.command}", send_stop=False)

    def _complete_session_action(self, idle_event: str = "EVENT IDLE") -> None:
        if not self.session_running or not self.session_waiting_for_idle:
            return
        action = self.session_current_action
        if action is None:
            self._abort_session("Session action state was lost", send_stop=False)
            return
        now = time.monotonic()
        completed_wallclock = datetime.now()
        self.session_waiting_for_idle = False
        self.session_motion_completed_at = now
        self.session_motion_completed_wallclock = completed_wallclock
        self.session_idle_event = idle_event
        self.session_expected_positions[action.axis] += action.delta_mm
        try:
            pulse_positions = parse_idle_event(idle_event)
            reported_positions = {
                axis: steps_to_mm(
                    pulse_positions[axis],
                    self.current_status[f"{axis}SPMM"],
                )
                for axis in ("X", "Z")
            }
            for axis, position_mm in reported_positions.items():
                self.session_expected_positions[axis] = position_mm
        except (KeyError, TypeError, ValueError):
            # Older/third-party firmware may omit pulse coordinates. Retain the
            # deterministic commanded-position fallback in that case.
            pass
        dwell = action.pace_seconds
        settle_delay = min(
            SESSION_MEASUREMENT_SETTLE_MAX_S,
            max(0.05, dwell / 2.0),
        )
        self.session_measurement_due_at = now + settle_delay
        self.session_dwell_until = now + dwell
        self.session_waiting_for_measurement = True
        self.session_measurement_in_progress = False
        self.session_phase_var.set(
            f"{action.phase} {action.phase_step}/{action.phase_steps} complete  Â·  "
            f"settling {settle_delay:.2f} s before E5574A power read"
        )
        self._add_session_report_event(
            "MOTION_IDLE",
            f"Action {self.session_action_index + 1}: {idle_event}; expected "
            f"X={self.session_expected_positions['X']:.6f} mm, "
            f"Z={self.session_expected_positions['Z']:.6f} mm",
            timestamp=completed_wallclock,
            elapsed_s=max(0.0, now - self.session_started_at),
        )

    def _start_session_power_measurement(self) -> None:
        if (
            not self.session_running
            or not self.session_waiting_for_measurement
            or self.session_measurement_in_progress
        ):
            return
        instrument = self.visa_instrument
        if instrument is None or not instrument.is_open:
            self._record_failed_session_measurement(
                "E5574A connection lost before power acquisition"
            )
            self._abort_session("E5574A connection lost during session", send_stop=True)
            return
        if self.visa_busy:
            return

        self.session_measurement_in_progress = True
        self.session_measurement_started_at = time.monotonic()
        self.session_measurement_started_wallclock = datetime.now()
        action = self.session_current_action
        action_number = self.session_action_index + 1
        self.session_phase_var.set(
            f"Reading E5574A Head A power for action {action_number}/{len(self.session_actions)}"
        )
        self._add_session_report_event(
            "GPIB_READ_START",
            f"Action {action_number}: :SENS{SESSION_POWER_CHANNEL}:DATA? POW",
            timestamp=self.session_measurement_started_wallclock,
            elapsed_s=max(0.0, self.session_measurement_started_at - self.session_started_at),
        )

        def visa_action() -> PowerReading:
            with self.visa_lock:
                return instrument.read_power_dbm(SESSION_POWER_CHANNEL)

        self.visa_status_var.set(f"Reading session power for action {action_number}...")
        self._start_visa_operation("session_power", visa_action)

    def _complete_session_power_measurement(self, reading: PowerReading) -> None:
        if not self.session_running or not self.session_waiting_for_measurement:
            return
        action = self.session_current_action
        if action is None:
            self._abort_session("Session measurement state was lost", send_stop=False)
            return
        completed_at = time.monotonic()
        completed_wallclock = datetime.now()
        elapsed_s = max(0.0, completed_at - self.session_started_at)
        wait_s = max(0.0, completed_at - self.session_motion_completed_at)
        record = SessionMeasurementRecord(
            action_number=self.session_action_index + 1,
            measurement_timestamp=completed_wallclock,
            elapsed_s=elapsed_s,
            loop_number=action.loop_number,
            phase=action.phase,
            phase_step=action.phase_step,
            phase_steps=action.phase_steps,
            command=action.command,
            axis=action.axis,
            delta_mm=action.delta_mm,
            expected_x_mm=self.session_expected_positions["X"],
            expected_z_mm=self.session_expected_positions["Z"],
            move_started_at=self.session_action_started_wallclock,
            motion_completed_at=self.session_motion_completed_wallclock,
            measurement_started_at=self.session_measurement_started_wallclock,
            measurement_completed_at=completed_wallclock,
            dwell_target_s=action.pace_seconds,
            actual_post_move_wait_s=wait_s,
            power_dbm=reading.value_dbm,
            raw_response=reading.raw_response,
            result="OK",
            notes=self.session_idle_event,
        )
        if self.session_report is not None:
            self.session_report.add_measurement(record)
        self._add_session_report_event(
            "POWER_READING",
            f"Action {record.action_number}: {reading.value_dbm:.6f} dBm; "
            f"raw={reading.raw_response!r}",
            timestamp=completed_wallclock,
            elapsed_s=elapsed_s,
        )
        self.visa_reading_var.set(f"{reading.value_dbm:.6f} dBm")
        self.visa_status_var.set(
            f"Session action {record.action_number} power received from E5574A Head A."
        )
        self._session_log(
            f"MEASURE action={record.action_number}/{len(self.session_actions)} "
            f"loop={action.loop_number} phase={action.phase} "
            f"X={record.expected_x_mm:.6f} mm Z={record.expected_z_mm:.6f} mm "
            f"t={record.elapsed_s:.3f} s power={record.power_dbm:.6f} dBm",
            event_type="MEASUREMENT",
            add_to_report=False,
        )

        self.session_waiting_for_measurement = False
        self.session_measurement_in_progress = False
        self.session_action_index += 1
        completed = self.session_action_index
        total = len(self.session_actions)
        self.session_progress_var.set(100.0 * completed / total if total else 0.0)
        self.session_progress_text_var.set(f"{completed:,} / {total:,} actions")
        self.session_next_action_at = max(self.session_dwell_until, completed_at + 0.02)
        self.session_current_action = None

    def _update_session_runtime(self, now: float | None = None) -> None:
        if not self.session_running or self.session_config is None:
            return
        current_time = time.monotonic() if now is None else now
        paused_now = (
            current_time - self.session_pause_started_at if self.session_paused else 0.0
        )
        elapsed = max(
            0.0,
            current_time
            - self.session_started_at
            - self.session_paused_duration
            - paused_now,
        )
        total = max(len(self.session_actions), 1)
        remaining_fraction = max(0.0, 1.0 - self.session_action_index / total)
        remaining = self.session_config.estimated_duration_s * remaining_fraction
        self.session_runtime_var.set(
            f"Elapsed {self._format_duration(elapsed)}  ·  "
            f"Remaining approximately {self._format_duration(remaining)}"
        )

    def toggle_session_pause(self) -> None:
        if not self.session_running:
            return
        now = time.monotonic()
        if self.session_paused:
            paused_for = max(0.0, now - self.session_pause_started_at)
            self.session_paused_duration += paused_for
            self.session_paused = False
            self.session_next_action_at = max(self.session_next_action_at, now)
            self.session_state_var.set("Running")
            self._session_log(
                f"RESUME after {self._format_duration(paused_for)} pause",
                event_type="RESUME",
            )
        else:
            self.session_paused = True
            self.session_pause_started_at = now
            self.session_state_var.set("Paused")
            self.session_phase_var.set(
                "Paused after current move and power reading"
                if self.session_waiting_for_idle or self.session_waiting_for_measurement
                else "Paused"
            )
            self._session_log("PAUSE requested", event_type="PAUSE")
        self._update_session_controls()

    def stop_session(self) -> None:
        if not self.session_running:
            return
        self.send_command("STOP", quiet=True)
        self._end_session("Stopped", "Stopped by operator")

    def _finish_session(self) -> None:
        if not self.session_running:
            return
        self.session_progress_var.set(100.0)
        self.session_progress_text_var.set(
            f"{len(self.session_actions):,} / {len(self.session_actions):,} actions"
        )
        self._end_session("Completed", "All requested loops completed")

    def _abort_session(self, reason: str, *, send_stop: bool = True) -> None:
        if not self.session_running:
            return
        if send_stop and self.serial_port is not None and self.controller_verified:
            self.send_command("STOP", quiet=True)
        self._end_session("Aborted", reason)

    def _end_session(self, state: str, reason: str) -> None:
        if self.session_waiting_for_measurement:
            self._record_failed_session_measurement(
                f"{state}: {reason}; power acquisition did not complete",
                result="NOT ACQUIRED",
            )
        if self.session_paused:
            self.session_paused_duration += max(
                0.0, time.monotonic() - self.session_pause_started_at
            )
        self.session_running = False
        self.session_paused = False
        self.session_preparing_visa = False
        self.session_waiting_for_idle = False
        self.session_waiting_for_measurement = False
        self.session_measurement_in_progress = False
        self.session_current_action = None
        self.session_state_var.set(state)
        self.session_phase_var.set(reason)
        self._session_log(f"{state.upper()}: {reason}", event_type=state.upper())
        self._save_session_excel_report(state, reason)
        self.message_var.set(f"Session {state.lower()}: {reason}")
        self._update_control_states()
        self._update_session_controls()

    def _record_failed_session_measurement(
        self, notes: str, *, result: str = "ERROR"
    ) -> None:
        action = self.session_current_action
        report = self.session_report
        if action is None or report is None:
            return
        completed_wallclock = datetime.now()
        completed_at = time.monotonic()
        motion_completed = self.session_motion_completed_wallclock
        measurement_started = (
            self.session_measurement_started_wallclock
            if self.session_measurement_in_progress
            else completed_wallclock
        )
        report.add_measurement(
            SessionMeasurementRecord(
                action_number=self.session_action_index + 1,
                measurement_timestamp=completed_wallclock,
                elapsed_s=max(0.0, completed_at - self.session_started_at),
                loop_number=action.loop_number,
                phase=action.phase,
                phase_step=action.phase_step,
                phase_steps=action.phase_steps,
                command=action.command,
                axis=action.axis,
                delta_mm=action.delta_mm,
                expected_x_mm=self.session_expected_positions["X"],
                expected_z_mm=self.session_expected_positions["Z"],
                move_started_at=self.session_action_started_wallclock,
                motion_completed_at=motion_completed,
                measurement_started_at=measurement_started,
                measurement_completed_at=completed_wallclock,
                dwell_target_s=action.pace_seconds,
                actual_post_move_wait_s=max(
                    0.0, completed_at - self.session_motion_completed_at
                ),
                power_dbm=None,
                raw_response="",
                result=result,
                notes=notes,
            )
        )
        self._add_session_report_event(
            "POWER_ERROR",
            f"Action {self.session_action_index + 1}: {notes}",
            timestamp=completed_wallclock,
            elapsed_s=max(0.0, completed_at - self.session_started_at),
        )
        self.session_waiting_for_measurement = False
        self.session_measurement_in_progress = False

    def _add_session_report_event(
        self,
        event_type: str,
        details: str,
        *,
        timestamp: datetime | None = None,
        elapsed_s: float | None = None,
    ) -> None:
        report = self.session_report
        if report is None:
            return
        event_time = datetime.now() if timestamp is None else timestamp
        event_elapsed = (
            max(0.0, time.monotonic() - self.session_started_at)
            if elapsed_s is None
            else max(0.0, elapsed_s)
        )
        report.add_event(
            SessionEventRecord(
                timestamp=event_time,
                elapsed_s=event_elapsed,
                event_type=event_type,
                details=details,
            )
        )

    def _save_session_excel_report(self, state: str, reason: str) -> None:
        report = self.session_report
        if report is None:
            return
        ended_at = datetime.now()
        paused_now = (
            time.monotonic() - self.session_pause_started_at if self.session_paused else 0.0
        )
        duration_s = max(
            0.0,
            time.monotonic()
            - self.session_started_at
            - self.session_paused_duration
            - paused_now,
        )
        try:
            path = report.save(
                status=state,
                reason=reason,
                ended_at=ended_at,
                duration_s=duration_s,
                completed_actions=self.session_action_index,
            )
        except Exception as exc:
            self.session_report_var.set(f"Excel report save failed: {exc}")
            messagebox.showerror(
                "Excel report save failed",
                f"The session ended, but the Excel workbook could not be saved:\n\n{exc}",
                parent=self,
            )
        else:
            self.session_report_path = path
            self.session_report_var.set(f"Latest Excel report: {path}")
            self._append_session_log_display(f"Excel report saved: {path}")
        finally:
            self.session_report = None

    def _session_log(
        self,
        message: str,
        *,
        event_type: str = "SESSION_EVENT",
        add_to_report: bool = True,
    ) -> None:
        timestamp_value = datetime.now()
        timestamp = timestamp_value.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        record = f"[{timestamp}] {message}\n"
        self._append_session_log_display(record, already_formatted=True)
        if add_to_report:
            self._add_session_report_event(event_type, message, timestamp=timestamp_value)

    def _append_session_log_display(
        self, message: str, *, already_formatted: bool = False
    ) -> None:
        record = message if already_formatted else f"[{datetime.now():%Y-%m-%d %H:%M:%S.%f}] {message}\n"
        if hasattr(self, "session_log_widget"):
            self.session_log_widget.configure(state="normal")
            self.session_log_widget.insert("end", record)
            line_count = int(self.session_log_widget.index("end-1c").split(".")[0])
            if line_count > 2000:
                self.session_log_widget.delete("1.0", "250.0")
            self.session_log_widget.see("end")
            self.session_log_widget.configure(state="disabled")
        if self.session_log_path is not None:
            try:
                with self.session_log_path.open("a", encoding="utf-8") as log_file:
                    log_file.write(record)
            except OSError:
                self.session_log_path = None

    def clear_session_log(self) -> None:
        self.session_log_widget.configure(state="normal")
        self.session_log_widget.delete("1.0", "end")
        self.session_log_widget.configure(state="disabled")

    def open_session_report_folder(self) -> None:
        directory = self.session_report_directory
        if directory is None:
            messagebox.showwarning(
                "Excel reports unavailable",
                "The Excel report folder is unavailable.",
                parent=self,
            )
            return
        try:
            os.startfile(directory)  # type: ignore[attr-defined]
        except OSError as exc:
            messagebox.showerror("Open report folder failed", str(exc), parent=self)

    def _selected_tab_name(self) -> str:
        selected = self.notebook.select()
        return str(self.notebook.tab(selected, "text")) if selected else "Workspace"

    def _on_tab_changed(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        self._update_workspace_controls()

    def _update_workspace_controls(self) -> None:
        tab_name = self._selected_tab_name()
        mode_label = {
            "normal": "Normal view",
            "minimized": "Minimized",
            "maximized": "Maximized workspace",
        }[self.workspace_mode]
        self.workspace_state_var.set(f"{tab_name} · {mode_label}")
        self.minimize_tab_button.state(
            ["disabled"] if self.workspace_mode == "minimized" else ["!disabled"]
        )
        self.maximize_tab_button.state(
            ["disabled"] if self.workspace_mode == "maximized" else ["!disabled"]
        )
        self.restore_tab_button.state(
            ["disabled"] if self.workspace_mode == "normal" else ["!disabled"]
        )

    def minimize_selected_tab(self) -> None:
        """Collapse tabs while keeping connection and safety controls visible."""
        self.connection_frame.grid()
        self.command_toolbar.grid()
        self.notebook.grid_remove()
        self.root_frame.rowconfigure(4, weight=0)
        self.workspace_mode = "minimized"
        self._update_workspace_controls()

    def maximize_selected_tab(self) -> None:
        """Expand the selected tab while keeping all safety controls visible."""
        self.notebook.grid()
        self.connection_frame.grid_remove()
        self.command_toolbar.grid()
        self.root_frame.rowconfigure(4, weight=1)
        self.workspace_mode = "maximized"
        self._update_workspace_controls()

    def restore_workspace(self) -> None:
        """Restore connection, safety-toolbar and tab panels."""
        self.connection_frame.grid()
        self.command_toolbar.grid()
        self.notebook.grid()
        self.root_frame.rowconfigure(4, weight=1)
        self.workspace_mode = "normal"
        self._update_workspace_controls()

    def register_online_widgets(self, *widgets: ttk.Widget) -> None:
        self.online_widgets.extend(widgets)

    def register_motion_widgets(self, *widgets: ttk.Widget) -> None:
        self.motion_widgets.extend(widgets)

    @staticmethod
    def _set_widgets_enabled(
        widgets: list[ttk.Widget] | tuple[ttk.Widget, ...], enabled: bool
    ) -> None:
        for widget in widgets:
            if enabled:
                widget.state(["!disabled"])
            else:
                widget.state(["disabled"])

    def _update_control_states(self, status: dict[str, str] | None = None) -> None:
        online = self.controller_verified
        effective_status = status if status is not None else self.current_status
        motion_ready = (
            online
            and effective_status.get("ENABLED") == "1"
            and effective_status.get("BUSY") != "1"
            and effective_status.get("ESTOP") != "1"
            and effective_status.get("FAULT", "NONE") == "NONE"
        )
        self._set_widgets_enabled(self.online_widgets, online)
        self._set_widgets_enabled(
            self.motion_widgets, motion_ready and not self.session_running
        )
        if self.session_running:
            self._set_widgets_enabled(self.online_widgets, False)
            self._set_widgets_enabled(
                (self.stop_button, self.disable_button, self.estop_button), online
            )
        self._update_session_controls()

    def _update_visa_control_states(self) -> None:
        connected = self.visa_instrument is not None and self.visa_instrument.is_open
        busy = self.visa_busy or self.session_running
        self.visa_connect_button.configure(
            text="Disconnect E5574A" if connected else "Connect E5574A"
        )
        self._set_widgets_enabled(
            (self.visa_refresh_button, self.visa_connect_button), not busy
        )
        self._set_widgets_enabled(
            (self.visa_select_application_button, self.visa_read_button),
            connected and not busy,
        )
        self._update_session_controls()

    def _start_visa_operation(self, operation: str, action: object) -> None:
        """Run a potentially blocking VISA call away from Tk's main thread."""
        if self.visa_busy:
            return
        self.visa_busy = True
        self._update_visa_control_states()

        def worker() -> None:
            try:
                result = action()  # type: ignore[operator]
            except Exception as exc:
                self.events.put(("visa", ("error", operation, str(exc))))
            else:
                self.events.put(("visa", ("success", operation, result)))

        threading.Thread(target=worker, name=f"visa-{operation}", daemon=True).start()

    def refresh_visa_resources(self) -> None:
        try:
            resources = list_visa_resources()
        except VisaUnavailableError as exc:
            messagebox.showerror("VISA unavailable", str(exc), parent=self)
            self.visa_status_var.set(str(exc))
            return
        except Exception as exc:
            messagebox.showerror("VISA discovery failed", str(exc), parent=self)
            self.visa_status_var.set(f"VISA discovery failed: {exc}")
            return

        self.visa_resource_box["values"] = resources
        if resources and not self.visa_resource_var.get().strip():
            self.visa_resource_var.set(resources[0])
        self.visa_status_var.set(
            f"Found {len(resources)} VISA resource(s). Select or enter the E5574A resource."
        )

    def toggle_visa_connection(self) -> None:
        if self.visa_instrument is not None:
            self.disconnect_visa()
        else:
            self.connect_visa()

    def connect_visa(self) -> None:
        resource_name = self.visa_resource_var.get().strip()
        if not resource_name:
            messagebox.showerror(
                "No VISA resource",
                "Enter a resource such as GPIB0::24::INSTR or refresh VISA resources.",
                parent=self,
            )
            return

        self.visa_status_var.set(f"Opening {resource_name}...")

        def action() -> tuple[E5574AVisa, str]:
            client = E5574AVisa(resource_name)
            client.open()
            try:
                identification = client.identify()
            except Exception:
                client.close()
                raise
            return client, identification

        self._start_visa_operation("connect", action)

    def disconnect_visa(self) -> None:
        if self.visa_busy:
            return
        instrument = self.visa_instrument
        self.visa_instrument = None
        if instrument is not None:
            with self.visa_lock:
                instrument.close()
        self.visa_idn_var.set("Not identified")
        self.visa_reading_var.set("--")
        self.visa_status_var.set("E5574A VISA connection closed.")
        self._update_visa_control_states()

    def select_visa_application(self) -> None:
        instrument = self.visa_instrument
        if instrument is None:
            messagebox.showwarning(
                "E5574A not connected",
                "Connect the E5574A through VISA before selecting an application.",
                parent=self,
            )
            return
        application = MEASUREMENT_APPLICATIONS[self.visa_application_var.get()]

        def action() -> str:
            with self.visa_lock:
                if application == "POW":
                    return instrument.configure_power_dbm(SESSION_POWER_CHANNEL)
                return instrument.select_application(application)

        self.visa_status_var.set(f"Selecting E5574A application {application}...")
        self._start_visa_operation("select", action)

    def read_visa_measurement(self) -> None:
        instrument = self.visa_instrument
        if instrument is None:
            messagebox.showwarning(
                "E5574A not connected",
                "Connect the E5574A through VISA before reading a measurement.",
                parent=self,
            )
            return
        application = MEASUREMENT_APPLICATIONS[self.visa_application_var.get()]

        def action() -> str:
            with self.visa_lock:
                if application == "POW":
                    reading = instrument.read_power_dbm(SESSION_POWER_CHANNEL)
                    return f"{reading.value_dbm:.6f} dBm"
                return instrument.read_measurement(application)

        self.visa_status_var.set(f"Reading E5574A measurement {application}...")
        self._start_visa_operation("read", action)

    def _handle_visa_event(self, payload: object) -> None:
        self.visa_busy = False
        try:
            result_kind, operation, result = payload  # type: ignore[misc]
        except (TypeError, ValueError):
            self.visa_status_var.set("Malformed VISA event ignored.")
            self._update_visa_control_states()
            return

        if result_kind == "error":
            self.visa_status_var.set(f"VISA {operation} failed: {result}")
            if operation == "session_prepare":
                if self.session_running:
                    self._abort_session(
                        f"Unable to prepare E5574A for dBm acquisition: {result}",
                        send_stop=False,
                    )
                    messagebox.showerror(
                        "Session GPIB preparation failed", str(result), parent=self
                    )
            elif operation == "session_power":
                if self.session_running:
                    self._record_failed_session_measurement(str(result))
                    self._abort_session(
                        f"E5574A power acquisition failed: {result}", send_stop=True
                    )
                    messagebox.showerror(
                        "Session power acquisition failed", str(result), parent=self
                    )
            else:
                messagebox.showerror("VISA operation failed", str(result), parent=self)
            self._update_visa_control_states()
            return

        if operation == "connect":
            instrument, identification = result  # type: ignore[misc]
            self.visa_instrument = instrument
            self.visa_idn_var.set(str(identification))
            self.visa_status_var.set("E5574A connected through VISA/GPIB.")
        elif operation == "select":
            self.visa_status_var.set(f"E5574A application active: {result}")
        elif operation == "read":
            self.visa_reading_var.set(str(result))
            self.visa_status_var.set(
                f"{self.visa_application_var.get()} reading received from E5574A."
            )
        elif operation == "session_prepare":
            if self.session_running:
                self.visa_application_var.set("Power meter")
                self.visa_status_var.set(f"E5574A session acquisition ready: {result}")
                self._start_session_motion()
        elif operation == "session_power":
            if self.session_running:
                if not isinstance(result, PowerReading):
                    self._record_failed_session_measurement(
                        f"Unexpected E5574A result type: {type(result).__name__}"
                    )
                    self._abort_session(
                        "Unexpected E5574A session measurement response", send_stop=True
                    )
                else:
                    self._complete_session_power_measurement(result)
        self._update_visa_control_states()

    def refresh_ports(self) -> None:
        ports = sorted(list_ports.comports(), key=lambda item: item.device)
        labels = [f"{item.device} | {item.description}" for item in ports]
        current_device = self.port_var.get().split(" | ", 1)[0]
        self.port_box["values"] = labels
        matching = next((label for label in labels if label.startswith(f"{current_device} |")), None)
        if matching:
            self.port_var.set(matching)
        elif labels:
            self.port_var.set(labels[0])
        else:
            self.port_var.set("")
            self.message_var.set("No serial ports detected. Connect the USB-UART adapter and refresh.")

    def toggle_connection(self) -> None:
        if self.serial_port is not None:
            self.disconnect()
        else:
            self.connect()

    def connect(self) -> None:
        selection = self.port_var.get()
        if not selection:
            messagebox.showerror(
                "No serial port",
                "Connect a 3.3 V USB-UART adapter and refresh the port list.",
                parent=self,
            )
            return
        port_name = selection.split(" | ", 1)[0]

        try:
            port = serial.Serial(
                port=port_name,
                baudrate=BAUD_RATE,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.1,
                write_timeout=1.0,
            )
            port.reset_input_buffer()
        except (serial.SerialException, OSError) as exc:
            messagebox.showerror("Connection failed", str(exc), parent=self)
            return

        self.serial_port = port
        self.controller_verified = False
        self.current_status = {}
        self.connection_generation += 1
        generation = self.connection_generation
        self.reader_stop.clear()
        self.reader_thread = threading.Thread(target=self._reader_loop, args=(port,), daemon=True)
        self.reader_thread.start()
        self.connect_button.configure(text="Disconnect")
        self.link_badge.set("Checking...", "warn")
        self.message_var.set(f"Checking controller on {port_name}...")
        self._append_log(f"SYSTEM Connected to {port_name} at {BAUD_RATE} baud")
        self.send_command("PING", require_verified=False)
        self.send_command("STATUS", quiet=True, require_verified=False)
        self.after(HANDSHAKE_TIMEOUT_MS, lambda: self._check_handshake(generation, port_name))

    def _check_handshake(self, generation: int, port_name: str) -> None:
        if generation != self.connection_generation or self.serial_port is None:
            return
        if not self.controller_verified:
            self.link_badge.set("No reply", "warn")
            self.message_var.set(
                f"{port_name} is open, but compatible firmware did not answer PING. Check TX/RX crossing."
            )

    def disconnect(self, log_message: bool = True) -> None:
        if self.session_running:
            self._abort_session("Serial connection closed", send_stop=False)
        self.connection_generation += 1
        self.reader_stop.set()
        port = self.serial_port
        self.serial_port = None
        self.controller_verified = False
        self.current_status = {}
        if port is not None:
            try:
                port.close()
            except (serial.SerialException, OSError):
                pass
        self.connect_button.configure(text="Connect")
        self.link_badge.set("Disconnected", "neutral")
        self.driver_badge.set("Unknown", "neutral")
        self.motion_badge.set("Unknown", "neutral")
        self._update_control_states()
        self.message_var.set("Disconnected. Motor motion commands are locked.")
        if log_message:
            self._append_log("SYSTEM Disconnected")

    def _reader_loop(self, port: serial.Serial) -> None:
        while not self.reader_stop.is_set():
            try:
                raw = port.readline()
                if raw:
                    self.events.put(("line", raw.decode("ascii", errors="replace").strip()))
            except (serial.SerialException, OSError) as exc:
                if not self.reader_stop.is_set():
                    self.events.put(("error", str(exc)))
                return

    def send_command(
        self,
        command: str,
        *,
        quiet: bool = False,
        require_verified: bool = True,
    ) -> bool:
        try:
            command = sanitize_raw_command(command)
        except ValueError as exc:
            messagebox.showerror("Invalid command", str(exc), parent=self)
            return False

        port = self.serial_port
        if port is None or not port.is_open:
            if not quiet:
                messagebox.showwarning("Not connected", "Connect to the STM32 UART first.", parent=self)
            return False
        if require_verified and not self.controller_verified:
            if not quiet:
                messagebox.showwarning(
                    "Controller not ready",
                    "The serial port is open, but Laser X-Z firmware 2.x has not completed its handshake.",
                    parent=self,
                )
            return False
        try:
            with self.serial_lock:
                port.write((command + "\n").encode("ascii"))
            self._append_log(
                f"TX > {command}",
                display=not quiet or self.show_status_traffic_var.get(),
            )
            return True
        except (UnicodeEncodeError, serial.SerialException, OSError) as exc:
            if not quiet:
                messagebox.showerror("Send failed", str(exc), parent=self)
            return False

    def send_raw_command(self) -> None:
        command = self.raw_command_var.get()
        if self.send_command(command):
            self.raw_command_var.set("")

    def execute_xy_move(self) -> None:
        try:
            x_text = self.xy_x_var.get().strip()
            z_text = self.xy_z_var.get().strip()
            x_value = None if not x_text else finite_float(x_text, "X value")
            z_value = None if not z_text else finite_float(z_text, "Z value")
            if x_value is None and z_value is None:
                raise ValueError("Enter an X value, a Z value, or both")
            if self.xy_mode_var.get() == "relative":
                x_value = None if x_value == 0 else x_value
                z_value = None if z_value == 0 else z_value
                if x_value is None and z_value is None:
                    raise ValueError("A relative move must contain a non-zero distance")
                command = build_relative_mm(x_value, z_value)
            else:
                command = build_absolute_mm(x_value, z_value)
            self.send_command(command)
        except ValueError as exc:
            messagebox.showerror("Invalid X/Z move", str(exc), parent=self)

    def disable_drivers(self) -> None:
        if messagebox.askokcancel(
            "Disable drivers",
            "Disabling removes holding torque. The Z stage may fall unless it is self-locking, "
            "braked or counterbalanced.\n\nDisable both drivers now?",
            parent=self,
        ):
            if self.session_running:
                self._end_session("Aborted", "Drivers disabled by operator")
            self.send_command("DISABLE")

    def stop_motion(self) -> None:
        if self.session_running:
            self.stop_session()
        else:
            self.send_command("STOP")

    def emergency_stop(self) -> None:
        if self.session_running:
            self._end_session("Aborted", "E-stop requested by operator")
        self.send_command("ESTOP")

    def zero_all(self) -> None:
        if messagebox.askokcancel(
            "Set X/Z coordinate zero",
            "Set both current positions to 0.000 mm without moving?",
            parent=self,
        ):
            self.send_command("ZERO ALL")

    def _keyboard_stop(self, _event: tk.Event[tk.Misc]) -> None:
        if self.controller_verified:
            if self.session_running:
                self.stop_session()
            else:
                self.send_command("STOP")
            self.message_var.set("STOP requested from the Escape key.")

    def _process_events(self) -> None:
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "line":
                self._handle_line(payload)
            elif kind == "error":
                self._append_log(f"SYSTEM Serial error: {payload}")
                self.disconnect(log_message=False)
            elif kind == "visa":
                self._handle_visa_event(payload)
            elif kind == "plotter":
                self._handle_plotter_event(payload)
        self.after(QUEUE_PERIOD_MS, self._process_events)

    def _handle_line(self, line: str) -> None:
        if not line:
            return

        self._append_log(
            f"RX < {line}",
            display=not line.startswith("STATUS ") or self.show_status_traffic_var.get(),
        )

        if line.startswith("OK PONG LASER_XZ "):
            version = line.rsplit(" ", 1)[-1]
            self.firmware_version = version
            self.firmware_label.configure(text=f"Firmware: {version}")
            try:
                parts = tuple(int(part) for part in version.split(".")[:2])
                compatible = len(parts) == 2 and parts >= MINIMUM_FIRMWARE_VERSION
            except ValueError:
                compatible = False
            if compatible:
                self.controller_verified = True
                port_name = self.serial_port.port if self.serial_port else "UART"
                self.link_badge.set(f"Online {port_name}", "good")
                self.message_var.set("Controller online. Enable the drivers, then begin with a small jog.")
                self._update_control_states()
                self._send_status_quietly()
            else:
                self.controller_verified = False
                self.link_badge.set("Update firmware", "bad")
                self.message_var.set(
                    f"Firmware {version} answered, but this high-resolution GUI requires "
                    f"firmware {MINIMUM_FIRMWARE_VERSION[0]}.{MINIMUM_FIRMWARE_VERSION[1]} or newer."
                )
                self._update_control_states()
            return

        if line.startswith("STATUS "):
            try:
                status = parse_status(line)
                self._apply_status(status)
            except (KeyError, TypeError, ValueError) as exc:
                self._append_log(f"SYSTEM Malformed status ignored: {exc}")
            return

        if line.startswith("ERR "):
            if self.session_running:
                self._abort_session(f"Controller rejected a session command: {line}")
            self.message_var.set(f"Controller rejected command: {line[4:]}")
            self.notebook.select(self.diagnostics_tab)
            self.after(60, self._send_status_quietly)
        elif line.startswith("EVENT "):
            if line.startswith("EVENT IDLE") and self.session_waiting_for_idle:
                self._complete_session_action(line)
            self.message_var.set(line)
            self.after(60, self._send_status_quietly)
        elif line.startswith("OK "):
            self.message_var.set(line)
            self.after(60, self._send_status_quietly)

    def _apply_status(self, status: dict[str, str]) -> None:
        self.last_status_time = time.monotonic()
        first_status = not self.current_status
        self.current_status = status
        enabled = status.get("ENABLED") == "1"
        busy = status.get("BUSY") == "1"
        estop = status.get("ESTOP") == "1"
        fault = status.get("FAULT", "UNKNOWN")

        self.driver_badge.set("Enabled" if enabled else "Disabled", "good" if enabled else "neutral")
        self.motion_badge.set("Moving" if busy else "Idle", "active" if busy else "neutral")
        if estop:
            self.estop_badge.set("ACTIVE", "bad")
        else:
            self.estop_badge.set("Software only", "warn")
        self.fault_badge.set(fault.title() if fault != "NONE" else "None", "bad" if fault != "NONE" else "good")
        self.x_panel.update_status(status)
        self.z_panel.update_status(status)
        self.stage_simulation.update_status(status)
        self._update_control_states(status)
        if first_status:
            self.message_var.set(
                "Five-pin motor interface active: PA0/PA1, PB0/PB1 and PB10. "
                "Use Set zero here before absolute positioning."
            )

    def _send_status_quietly(self) -> None:
        self.send_command("STATUS", quiet=True, require_verified=False)

    def _poll_status(self) -> None:
        # Long status replies can briefly fill a small MCU UART TX buffer. Once
        # BUSY is observed, rely on the asynchronous EVENT IDLE reply instead
        # of polling during every motion; this keeps STEP timing smooth.
        if self.serial_port is not None and self.current_status.get("BUSY") != "1":
            self._send_status_quietly()
        self.after(STATUS_PERIOD_MS, self._poll_status)

    def _append_log(self, text: str, *, display: bool = True) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        record = f"[{timestamp}] {text}\n"
        if display:
            self.log.configure(state="normal")
            self.log.insert("end", record)
            line_count = int(self.log.index("end-1c").split(".")[0])
            if line_count > 1500:
                self.log.delete("1.0", "200.0")
            self.log.see("end")
            self.log.configure(state="disabled")
        if self.serial_log_path is not None:
            try:
                with self.serial_log_path.open("a", encoding="utf-8") as log_file:
                    log_file.write(record)
            except OSError:
                self.serial_log_path = None

    def clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def save_log(self) -> None:
        filename = filedialog.asksaveasfilename(
            parent=self,
            title="Save controller log",
            defaultextension=".txt",
            filetypes=(("Text files", "*.txt"), ("All files", "*.*")),
            initialfile=f"laser-stage-{datetime.now():%Y%m%d-%H%M%S}.txt",
        )
        if not filename:
            return
        try:
            Path(filename).write_text(self.log.get("1.0", "end-1c"), encoding="utf-8")
        except OSError as exc:
            messagebox.showerror("Save failed", str(exc), parent=self)

    def open_log_folder(self) -> None:
        if self.serial_log_path is None:
            messagebox.showwarning("Log unavailable", "The automatic log folder is unavailable.", parent=self)
            return
        try:
            os.startfile(self.serial_log_path.parent)  # type: ignore[attr-defined]
        except OSError as exc:
            messagebox.showerror("Open folder failed", str(exc), parent=self)

    def _on_close(self) -> None:
        self.disconnect(log_message=False)
        self.disconnect_visa()
        self.destroy()


def main() -> None:
    app = LaserStageApp()
    app.mainloop()


if __name__ == "__main__":
    main()
