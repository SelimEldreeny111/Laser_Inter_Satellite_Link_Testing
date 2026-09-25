"""Optional VISA control for an HP/Agilent E5574A optical loss analyzer.

The E5574A is controlled from the host PC through a VISA GPIB resource.  This
module deliberately has no dependency on the STM32 UART connection so that a
lost instrument link cannot interfere with motion control.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

try:
    import pyvisa
except ImportError:  # pragma: no cover - exercised only without VISA installed
    pyvisa = None  # type: ignore[assignment]


MEASUREMENT_APPLICATIONS: dict[str, str] = {
    "Power meter": "POW",
    "Insertion loss": "IL",
    "PDL": "PDL",
    "Return loss": "RL",
}


class VisaUnavailableError(RuntimeError):
    """Raised when PyVISA or a system VISA backend is not available."""


@dataclass(frozen=True)
class PowerReading:
    """One documented E5574A absolute-power reading."""

    value_dbm: float
    raw_response: str
    channel: int


_NUMERIC_RESPONSE = re.compile(
    r"^[\s]*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?)"
)


def parse_numeric_response(response: str) -> float:
    """Parse the unit-less numeric response returned by E5574A DATA queries."""
    match = _NUMERIC_RESPONSE.match(str(response))
    if match is None:
        raise ValueError(f"E5574A returned a non-numeric measurement: {response!r}")
    value = float(match.group(1))
    if not math.isfinite(value):
        raise ValueError(f"E5574A returned a non-finite measurement: {response!r}")
    return value


class E5574AVisa:
    """Small, serialized VISA client for the E5574A."""

    def __init__(self, resource_name: str, timeout_ms: int = 5000) -> None:
        self.resource_name = resource_name.strip()
        self.timeout_ms = timeout_ms
        self.manager: Any | None = None
        self.instrument: Any | None = None

    @property
    def is_open(self) -> bool:
        return self.instrument is not None

    def open(self) -> None:
        if not self.resource_name:
            raise ValueError("A VISA resource name is required")
        if pyvisa is None:
            raise VisaUnavailableError(
                "PyVISA is not installed. Install pyvisa and a vendor VISA runtime."
            )

        try:
            self.manager = pyvisa.ResourceManager()
            self.instrument = self.manager.open_resource(self.resource_name)
            self.instrument.timeout = self.timeout_ms
            # The E5574A terminates responses with LF and uses GPIB EOI for
            # command completion.  An empty write terminator preserves EOI.
            self.instrument.read_termination = "\n"
            self.instrument.write_termination = ""
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        instrument = self.instrument
        manager = self.manager
        self.instrument = None
        self.manager = None
        if instrument is not None:
            try:
                instrument.close()
            except Exception:
                pass
        if manager is not None:
            try:
                manager.close()
            except Exception:
                pass

    def _require_open(self) -> Any:
        if self.instrument is None:
            raise RuntimeError("The E5574A VISA connection is not open")
        return self.instrument

    def write(self, command: str) -> None:
        self._require_open().write(command.strip())

    def query(self, command: str) -> str:
        response = self._require_open().query(command.strip())
        return str(response).strip()

    def identify(self) -> str:
        identification = self.query("*IDN?")
        if "E5574A" not in identification.upper():
            raise RuntimeError(
                f"VISA resource did not identify as an E5574A: {identification}"
            )
        return identification

    def select_application(self, application: str) -> str:
        code = application.strip().upper()
        valid_codes = set(MEASUREMENT_APPLICATIONS.values())
        if code not in valid_codes:
            raise ValueError(f"Unsupported E5574A application: {application}")
        # MAIN exits nested applications before selecting a top-level mode, as
        # shown in the E5574A programming guide examples.
        self.write(":SENS:FUNC MAIN")
        self.write(f":SENS:FUNC {code}")
        return self.query(":SENS:FUNC?")

    def read_measurement(self, application: str) -> str:
        code = application.strip().upper()
        valid_codes = set(MEASUREMENT_APPLICATIONS.values())
        if code not in valid_codes:
            raise ValueError(f"Unsupported E5574A measurement: {application}")
        return self.query(f":SENS:DATA? {code}")

    def configure_power_dbm(self, channel: int = 1) -> str:
        """Select Powermeter, absolute mode and dBm for optical head A or B."""
        if channel not in (1, 2):
            raise ValueError("E5574A power channel must be 1 (Head A) or 2 (Head B)")
        active = self.select_application("POW").strip().upper()
        if "POW" not in active:
            raise RuntimeError(f"E5574A did not activate Powermeter: {active}")
        self.write(f":SENS{channel}:POW:MEAS:MOD ABS")
        self.write(f":SENS{channel}:POW:UNIT DBM")
        mode = self.query(f":SENS{channel}:POW:MEAS:MOD?").strip()
        unit = self.query(f":SENS{channel}:POW:UNIT?").strip()
        if int(parse_numeric_response(mode)) != 0:
            raise RuntimeError(
                f"E5574A Head {channel} is not in absolute-power mode (mode={mode})"
            )
        if int(parse_numeric_response(unit)) != 0:
            raise RuntimeError(f"E5574A power unit is not dBm (unit={unit})")
        return f"POW Head {channel}, absolute, dBm"

    def read_power_dbm(self, channel: int = 1) -> PowerReading:
        """Read absolute optical power in dBm from the selected optical head."""
        if channel not in (1, 2):
            raise ValueError("E5574A power channel must be 1 (Head A) or 2 (Head B)")
        raw_response = self.query(f":SENS{channel}:DATA? POW")
        return PowerReading(
            value_dbm=parse_numeric_response(raw_response),
            raw_response=raw_response,
            channel=channel,
        )


def list_visa_resources() -> tuple[str, ...]:
    """Return VISA resources visible through the installed backend."""

    if pyvisa is None:
        raise VisaUnavailableError(
            "PyVISA is not installed. Install pyvisa and a vendor VISA runtime."
        )
    manager = pyvisa.ResourceManager()
    try:
        return tuple(str(resource) for resource in manager.list_resources())
    finally:
        manager.close()
