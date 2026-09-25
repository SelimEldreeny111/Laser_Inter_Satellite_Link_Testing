"""Helpers for the newline-delimited laser-stage serial protocol.

Normal positioning remains millimetre based. Fine jogs use micrometres in the
GUI and are converted to exact integer STEP-pulse commands so no sub-pulse
rounding is hidden from the operator.
"""

from __future__ import annotations

import math
from typing import Dict


VALID_AXES = {"X", "Z"}
VALID_UNITS = {"half-steps", "mm"}
MIN_SPEED_MM_S = 1.0
MAX_SPEED_MM_S = 100.0
MIN_ACCEL_MM_S2 = 5.0
MAX_ACCEL_MM_S2 = 200.0
DEFAULT_PULSES_PER_MM = {"X": 160.0, "Z": 91.4285714286}
# Finest nominal pulse increment among the two axes. Retained for compatibility
# with integrations that imported this constant before per-axis resolution was
# introduced in firmware 2.5.
MOVE_INCREMENT_MM = min(1.0 / scale for scale in DEFAULT_PULSES_PER_MM.values())


def normalize_axis(axis: str) -> str:
    normalized = axis.strip().upper()
    if normalized not in VALID_AXES:
        raise ValueError(f"Axis must be X or Z, got {axis!r}")
    return normalized


def finite_float(value: float | str, label: str = "Value") -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    return number


def format_number(value: float | str) -> str:
    """Return a compact, non-exponential decimal for an ASCII command."""
    number = finite_float(value)
    if abs(number) < 0.0000000005:
        number = 0.0
    text = f"{number:.9f}".rstrip("0").rstrip(".")
    return text if text not in {"", "-0"} else "0"


def _round_step_pulses(value: float) -> int:
    """Match C/C++ ``lround`` behavior, including negative half values."""
    return math.floor(value + 0.5) if value >= 0.0 else math.ceil(value - 0.5)


def pulse_resolution_um(pulses_per_mm: float | str) -> float:
    """Return the physical coordinate increment represented by one STEP pulse."""
    scale = finite_float(pulses_per_mm, "STEP pulses per millimetre")
    if scale <= 0.0:
        raise ValueError("STEP pulses per millimetre must be positive")
    return 1000.0 / scale


def micrometres_to_pulses(
    value_um: float | str,
    pulses_per_mm: float | str,
    label: str = "Micrometre movement",
) -> int:
    """Convert an exact micrometre jog to an integer number of STEP pulses."""
    value = finite_float(value_um, label)
    scale = finite_float(pulses_per_mm, "STEP pulses per millimetre")
    if scale <= 0.0:
        raise ValueError("STEP pulses per millimetre must be positive")
    raw_pulses = value * scale / 1000.0
    pulses = _round_step_pulses(raw_pulses)
    if value != 0.0 and pulses == 0:
        raise ValueError(
            f"{label} is smaller than one STEP pulse "
            f"({pulse_resolution_um(scale):g} µm)"
        )
    if not math.isclose(raw_pulses, pulses, rel_tol=0.0, abs_tol=5e-5):
        raise ValueError(
            f"{label} must use whole STEP-pulse increments of "
            f"{pulse_resolution_um(scale):g} µm"
        )
    return pulses


def motion_grid_value(
    value: float | str,
    label: str = "Movement value",
    pulses_per_mm: float | str | None = None,
) -> float:
    """Validate a millimetre value against the controller's pulse resolution.

    Firmware 2.5 rounds millimetre values to the nearest STEP pulse. The GUI
    rejects only nonzero values that would round to zero; exact fine jogging is
    handled by :func:`build_jog_um`.
    """
    number = finite_float(value, label)
    if pulses_per_mm is not None:
        scale = finite_float(pulses_per_mm, "STEP pulses per millimetre")
        if scale <= 0.0:
            raise ValueError("STEP pulses per millimetre must be positive")
        if number != 0.0 and _round_step_pulses(number * scale) == 0:
            raise ValueError(
                f"{label} is smaller than one STEP pulse "
                f"({pulse_resolution_um(scale):g} µm)"
            )
    return number


def to_half_steps(value: float, unit: str, half_steps_per_mm: float) -> int:
    """Convert a distance/position into legacy integer pulse units."""
    number = finite_float(value)
    if unit not in VALID_UNITS:
        raise ValueError(f"Unsupported unit: {unit}")
    if unit == "mm":
        scale = finite_float(half_steps_per_mm, "Half-steps per mm")
        if scale <= 0:
            raise ValueError("Half-steps per mm must be positive")
        number *= scale
    return _round_step_pulses(number)


def steps_to_mm(half_steps: int | str, half_steps_per_mm: float | str) -> float:
    scale = finite_float(half_steps_per_mm, "Half-steps per mm")
    if scale <= 0:
        raise ValueError("Half-steps per mm must be positive")
    return int(half_steps) / scale


def build_move(axis: str, half_steps: int) -> str:
    return f"MOVE {normalize_axis(axis)} {int(half_steps)}"


def build_goto(axis: str, half_steps: int) -> str:
    return f"GOTO {normalize_axis(axis)} {int(half_steps)}"


def build_jog_um(
    axis: str,
    distance_um: float | str,
    pulses_per_mm: float | str | None = None,
) -> str:
    """Build an exact relative fine jog expressed in micrometres."""
    normalized = normalize_axis(axis)
    scale = DEFAULT_PULSES_PER_MM[normalized] if pulses_per_mm is None else pulses_per_mm
    pulses = micrometres_to_pulses(
        distance_um,
        scale,
        f"{normalized} jog distance",
    )
    return build_move(normalized, pulses)


def build_jog_mm(axis: str, distance_mm: float | str) -> str:
    normalized = normalize_axis(axis)
    distance = motion_grid_value(
        distance_mm,
        f"{normalized} distance",
        DEFAULT_PULSES_PER_MM[normalized],
    )
    return f"MOVE_MM {normalized} {format_number(distance)}"


def _build_axes_mm(command: str, x_mm: float | str | None, z_mm: float | str | None) -> str:
    fields: list[str] = [command]
    if x_mm is not None:
        fields.extend(
            (
                "X",
                format_number(
                    motion_grid_value(x_mm, "X value", DEFAULT_PULSES_PER_MM["X"])
                ),
            )
        )
    if z_mm is not None:
        fields.extend(
            (
                "Z",
                format_number(
                    motion_grid_value(z_mm, "Z value", DEFAULT_PULSES_PER_MM["Z"])
                ),
            )
        )
    if len(fields) == 1:
        raise ValueError("At least one X or Z value is required")
    return " ".join(fields)


def build_relative_mm(
    x_mm: float | str | None = None,
    z_mm: float | str | None = None,
) -> str:
    return _build_axes_mm("MOVE_MM", x_mm, z_mm)


def build_absolute_mm(
    x_mm: float | str | None = None,
    z_mm: float | str | None = None,
) -> str:
    return _build_axes_mm("GOTO_MM", x_mm, z_mm)


def build_speed_mm(axis: str, speed_mm_s: float | str) -> str:
    speed = finite_float(speed_mm_s, "Speed")
    if not MIN_SPEED_MM_S <= speed <= MAX_SPEED_MM_S:
        raise ValueError(
            f"Speed must be {MIN_SPEED_MM_S:g} to {MAX_SPEED_MM_S:g} mm/s"
        )
    return f"SPEED_MM {normalize_axis(axis)} {format_number(speed)}"


def build_accel_mm(axis: str, acceleration_mm_s2: float | str) -> str:
    acceleration = finite_float(acceleration_mm_s2, "Acceleration")
    if not MIN_ACCEL_MM_S2 <= acceleration <= MAX_ACCEL_MM_S2:
        raise ValueError(
            f"Acceleration must be {MIN_ACCEL_MM_S2:g} to {MAX_ACCEL_MM_S2:g} mm/s^2"
        )
    return f"ACCEL_MM {normalize_axis(axis)} {format_number(acceleration)}"


def sanitize_raw_command(command: str) -> str:
    """Reject embedded line breaks so one GUI action sends one command."""
    cleaned = command.strip()
    if not cleaned:
        raise ValueError("Command is empty")
    if "\n" in cleaned or "\r" in cleaned:
        raise ValueError("Command must be a single line")
    try:
        cleaned.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("Command must contain ASCII characters only") from exc
    return cleaned


def parse_status(line: str) -> Dict[str, str]:
    """Parse ``STATUS KEY=VALUE ...`` into a dictionary."""
    parts = line.strip().split()
    if not parts or parts[0] != "STATUS":
        raise ValueError("Not a STATUS line")

    status: Dict[str, str] = {}
    for part in parts[1:]:
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        if key:
            status[key] = value
    return status


def parse_idle_event(line: str) -> Dict[str, int]:
    """Parse the pulse coordinates in ``EVENT IDLE X=<steps> Z=<steps>``."""
    parts = line.strip().split()
    if len(parts) < 4 or parts[0:2] != ["EVENT", "IDLE"]:
        raise ValueError("Not an EVENT IDLE line")
    positions: Dict[str, int] = {}
    for part in parts[2:]:
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        if key in VALID_AXES:
            try:
                positions[key] = int(value)
            except ValueError as exc:
                raise ValueError(f"Invalid {key} pulse position") from exc
    if set(positions) != VALID_AXES:
        raise ValueError("EVENT IDLE must contain X and Z pulse positions")
    return positions


def axis_position_mm(status: Dict[str, str], axis: str) -> float:
    """Read a firmware 2.x mm position, with a firmware 1.x fallback."""
    normalized = normalize_axis(axis)
    direct_key = f"{normalized}MM"
    if direct_key in status:
        return finite_float(status[direct_key], f"{normalized} position")
    return steps_to_mm(status[normalized], status[f"{normalized}SPMM"])
