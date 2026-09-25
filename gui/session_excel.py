"""Professional Excel reports for automated laser-stage sessions."""

from __future__ import annotations

import os
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from openpyxl import Workbook
from openpyxl.chart import LineChart, Reference, ScatterChart, Series
from openpyxl.chart.series import SeriesLabel
from openpyxl.formatting.rule import ColorScaleRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.worksheet.table import Table, TableStyleInfo


NAVY = "0B1F3A"
BLUE = "123C69"
LIGHT_BLUE = "DCE6F1"
LIGHT_GREY = "E7E6E6"
WHITE = "FFFFFF"
GREEN = "E2F0D9"
AMBER = "FFF2CC"
RED = "F4CCCC"
TEAL = "0F6B78"
LIGHT_TEAL = "DDEBF7"
DARK_GREY = "44546A"

BEAM_POWER_BANDS = (
    ("0 to -1 dB", -1.0, None, "C00000"),
    ("-1 to -3 dB", -3.0, -1.0, "F46D43"),
    ("-3 to -6 dB", -6.0, -3.0, "FDAE61"),
    ("-6 to -9 dB", -9.0, -6.0, "FEE08B"),
    ("-9 to -12 dB", -12.0, -9.0, "66C2A5"),
    ("Below -12 dB", None, -12.0, "3288BD"),
)

WEATHER_COLOR_STOPS = (
    (-15.0, "313695"),
    (-12.0, "3288BD"),
    (-9.0, "66C2A5"),
    (-6.0, "FEE08B"),
    (-3.0, "FDAE61"),
    (-1.0, "F46D43"),
    (0.0, "A50026"),
)

WEATHER_GRID_SIZE = 41


@dataclass(frozen=True)
class SessionEventRecord:
    timestamp: datetime
    elapsed_s: float
    event_type: str
    details: str


@dataclass(frozen=True)
class SessionMeasurementRecord:
    action_number: int
    measurement_timestamp: datetime
    elapsed_s: float
    loop_number: int
    phase: str
    phase_step: int
    phase_steps: int
    command: str
    axis: str
    delta_mm: float
    expected_x_mm: float
    expected_z_mm: float
    move_started_at: datetime
    motion_completed_at: datetime
    measurement_started_at: datetime
    measurement_completed_at: datetime
    dwell_target_s: float
    actual_post_move_wait_s: float
    power_dbm: float | None
    raw_response: str
    result: str
    notes: str = ""


@dataclass(frozen=True)
class BeamAnalysisPoint:
    action_number: int
    x_mm: float
    z_mm: float
    power_dbm: float
    relative_db: float
    radial_mm: float
    inside_target: bool


@dataclass(frozen=True)
class BeamAnalysis:
    target_diameter_mm: float
    target_radius_mm: float
    center_x_mm: float | None
    center_z_mm: float | None
    x_min_mm: float | None
    x_max_mm: float | None
    z_min_mm: float | None
    z_max_mm: float | None
    peak_power_dbm: float | None
    peak_x_mm: float | None
    peak_z_mm: float | None
    centroid_x_mm: float | None
    centroid_z_mm: float | None
    centroid_dx_mm: float | None
    centroid_dz_mm: float | None
    centroid_offset_mm: float | None
    mean_power_dbm: float | None
    power_std_db: float | None
    power_spread_db: float | None
    points: tuple[BeamAnalysisPoint, ...]

    @property
    def inside_points(self) -> tuple[BeamAnalysisPoint, ...]:
        return tuple(point for point in self.points if point.inside_target)


@dataclass(frozen=True)
class BeamInterpolationGrid:
    """Regular X-Z grid produced from measured points for weather-style views."""

    x_values_mm: tuple[float, ...]
    z_values_mm: tuple[float, ...]
    relative_db: tuple[tuple[float | None, ...], ...]
    center_x_mm: float | None
    center_z_mm: float | None

    @property
    def size(self) -> int:
        return len(self.x_values_mm)

    @property
    def center_index(self) -> int:
        return self.size // 2

    @property
    def x_profile(self) -> tuple[tuple[float, float], ...]:
        if not self.relative_db:
            return ()
        row_index = min(
            range(len(self.z_values_mm)),
            key=lambda index: abs(self.z_values_mm[index] - float(self.center_z_mm)),
        )
        row = self.relative_db[row_index]
        return tuple(
            (x_mm, value)
            for x_mm, value in zip(self.x_values_mm, row)
            if value is not None
        )

    @property
    def z_profile(self) -> tuple[tuple[float, float], ...]:
        if not self.relative_db:
            return ()
        column = min(
            range(len(self.x_values_mm)),
            key=lambda index: abs(self.x_values_mm[index] - float(self.center_x_mm)),
        )
        return tuple(
            (z_mm, row[column])
            for z_mm, row in reversed(tuple(zip(self.z_values_mm, self.relative_db)))
            if row[column] is not None
        )


def interpolate_beam_grid(
    analysis: BeamAnalysis,
    *,
    grid_size: int = WEATHER_GRID_SIZE,
    nearest_points: int = 12,
) -> BeamInterpolationGrid:
    """Interpolate measured power on a circular grid using linear-power IDW."""
    if grid_size < 11 or grid_size % 2 == 0:
        raise ValueError("Weather grid size must be an odd integer of at least 11")
    if nearest_points < 1:
        raise ValueError("Nearest-point count must be at least one")
    if (
        not analysis.points
        or analysis.center_x_mm is None
        or analysis.center_z_mm is None
        or analysis.peak_power_dbm is None
        or analysis.x_min_mm is None
        or analysis.x_max_mm is None
        or analysis.z_min_mm is None
        or analysis.z_max_mm is None
    ):
        return BeamInterpolationGrid((), (), (), None, None)

    source_points = analysis.points
    # Collapse repeated coordinates in linear power before spatial interpolation.
    grouped: dict[tuple[float, float], list[float]] = defaultdict(list)
    for point in source_points:
        grouped[(point.x_mm, point.z_mm)].append(10.0 ** (point.relative_db / 10.0))
    sources = tuple(
        (x_mm, z_mm, sum(values) / len(values))
        for (x_mm, z_mm), values in grouped.items()
    )

    x_spacing = (analysis.x_max_mm - analysis.x_min_mm) / (grid_size - 1)
    z_spacing = (analysis.z_max_mm - analysis.z_min_mm) / (grid_size - 1)
    x_values = tuple(
        analysis.x_min_mm + index * x_spacing for index in range(grid_size)
    )
    # Descending Z keeps the worksheet/canvas map oriented with positive Z upward.
    z_values = tuple(
        analysis.z_max_mm - index * z_spacing for index in range(grid_size)
    )
    rows: list[tuple[float | None, ...]] = []
    exact_tolerance_sq = max(x_spacing, z_spacing, 1e-12) ** 2 * 1e-18
    for z_mm in z_values:
        row: list[float | None] = []
        for x_mm in x_values:
            distances = sorted(
                (
                    ((x_mm - source_x) ** 2 + (z_mm - source_z) ** 2, linear_power)
                    for source_x, source_z, linear_power in sources
                ),
                key=lambda item: item[0],
            )
            exact = [power for distance_sq, power in distances if distance_sq <= exact_tolerance_sq]
            if exact:
                linear_power = sum(exact) / len(exact)
            else:
                neighbors = distances[: min(nearest_points, len(distances))]
                weighted_sum = sum(power / distance_sq for distance_sq, power in neighbors)
                weight_sum = sum(1.0 / distance_sq for distance_sq, _power in neighbors)
                linear_power = weighted_sum / weight_sum
            row.append(10.0 * math.log10(max(linear_power, 1e-300)))
        rows.append(tuple(row))
    return BeamInterpolationGrid(
        x_values,
        z_values,
        tuple(rows),
        analysis.center_x_mm,
        analysis.center_z_mm,
    )


def radial_average_profile(
    analysis: BeamAnalysis,
    *,
    bin_count: int = 20,
) -> tuple[tuple[float, float, int], ...]:
    """Return linear-power radial averages using only measured in-target points."""
    if bin_count < 1:
        raise ValueError("Radial profile bin count must be at least one")
    if not analysis.inside_points or analysis.target_radius_mm <= 0:
        return ()
    bins: list[list[float]] = [[] for _ in range(bin_count)]
    for point in analysis.inside_points:
        index = min(
            bin_count - 1,
            int(point.radial_mm / analysis.target_radius_mm * bin_count),
        )
        bins[index].append(10.0 ** (point.relative_db / 10.0))
    width = analysis.target_radius_mm / bin_count
    return tuple(
        (
            (index + 0.5) * width,
            10.0 * math.log10(sum(values) / len(values)),
            len(values),
        )
        for index, values in enumerate(bins)
        if values
    )


class SessionExcelReport:
    """Collect session records and save a formatted, auditable XLSX workbook."""

    MEASUREMENT_HEADERS = (
        "Action #",
        "Measurement timestamp",
        "Elapsed (s)",
        "Elapsed second",
        "Loop",
        "Phase",
        "Phase step",
        "Phase total",
        "Command",
        "Axis",
        "Move delta (mm)",
        "Controller X position (mm)",
        "Controller Z position (mm)",
        "Move started",
        "Motion completed",
        "Measurement started",
        "Measurement completed",
        "Dwell target (s)",
        "Actual post-move wait (s)",
        "Power (dBm)",
        "Raw GPIB response",
        "Result",
        "Notes",
    )
    EVENT_HEADERS = ("Event #", "Timestamp", "Elapsed (s)", "Event type", "Details")

    def __init__(
        self,
        path: Path,
        *,
        session_id: str,
        started_at: datetime,
        metadata: Mapping[str, Any],
    ) -> None:
        self.path = Path(path)
        self.session_id = session_id
        self.started_at = started_at
        self.metadata = dict(metadata)
        self.events: list[SessionEventRecord] = []
        self.measurements: list[SessionMeasurementRecord] = []

    def add_event(self, record: SessionEventRecord) -> None:
        self.events.append(record)

    def add_measurement(self, record: SessionMeasurementRecord) -> None:
        self.measurements.append(record)

    def analyze_beam(self) -> BeamAnalysis:
        """Return the scientific X-Z beam analysis for the collected readings."""
        return self._analyze_beam()

    def add_beam_analysis_sheets(self, workbook: Workbook) -> BeamAnalysis:
        """Add or replace beam-analysis sheets in an existing session workbook."""
        for sheet_name in (
            "Beam Map",
            "Weather Map",
            "3D Power Surface",
            "Beam Profiles",
            "Beam Heatmap",
            "Beam Data",
            "Chart Data",
        ):
            if sheet_name in workbook.sheetnames:
                workbook.remove(workbook[sheet_name])

        insert_index = 1 if "Summary" in workbook.sheetnames else 0
        beam_map = workbook.create_sheet("Beam Map", insert_index)
        weather_map = workbook.create_sheet("Weather Map", insert_index + 1)
        beam_profiles = workbook.create_sheet("Beam Profiles", insert_index + 2)
        beam_heatmap = workbook.create_sheet("Beam Heatmap", insert_index + 3)
        beam_data = workbook.create_sheet("Beam Data", insert_index + 4)
        chart_data = workbook.create_sheet("Chart Data")
        analysis = self._analyze_beam()
        weather_grid = interpolate_beam_grid(analysis)
        self._write_chart_data(chart_data, analysis, weather_grid)
        self._build_beam_map(beam_map, chart_data, analysis)
        self._build_weather_map(weather_map, analysis, weather_grid)
        self._build_beam_profiles(beam_profiles, chart_data, analysis, weather_grid)
        self._build_beam_heatmap(beam_heatmap, analysis)
        self._build_beam_data(beam_data, analysis)
        chart_data.sheet_state = "hidden"

        calculation = getattr(workbook, "calculation", None)
        if calculation is not None:
            calculation.fullCalcOnLoad = True
            calculation.forceFullCalc = True
        return analysis

    def save(
        self,
        *,
        status: str,
        reason: str,
        ended_at: datetime,
        duration_s: float,
        completed_actions: int,
    ) -> Path:
        """Atomically write the current report and return its final path."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        workbook = self._build_workbook(
            status=status,
            reason=reason,
            ended_at=ended_at,
            duration_s=duration_s,
            completed_actions=completed_actions,
        )
        temporary_path = self.path.with_name(f"{self.path.stem}.tmp{self.path.suffix}")
        try:
            workbook.save(temporary_path)
            os.replace(temporary_path, self.path)
        finally:
            workbook.close()
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        return self.path

    def _build_workbook(
        self,
        *,
        status: str,
        reason: str,
        ended_at: datetime,
        duration_s: float,
        completed_actions: int,
    ) -> Workbook:
        workbook = Workbook()
        summary = workbook.active
        summary.title = "Summary"
        beam_map = workbook.create_sheet("Beam Map")
        weather_map = workbook.create_sheet("Weather Map")
        beam_profiles = workbook.create_sheet("Beam Profiles")
        beam_heatmap = workbook.create_sheet("Beam Heatmap")
        beam_data = workbook.create_sheet("Beam Data")
        measurements = workbook.create_sheet("Measurements")
        events = workbook.create_sheet("Events")
        chart_data = workbook.create_sheet("Chart Data")

        analysis = self._analyze_beam()
        weather_grid = interpolate_beam_grid(analysis)

        self._build_summary(
            summary,
            analysis=analysis,
            status=status,
            reason=reason,
            ended_at=ended_at,
            duration_s=duration_s,
            completed_actions=completed_actions,
        )
        self._write_chart_data(chart_data, analysis, weather_grid)
        self._build_beam_map(beam_map, chart_data, analysis)
        self._build_weather_map(weather_map, analysis, weather_grid)
        self._build_beam_profiles(beam_profiles, chart_data, analysis, weather_grid)
        self._build_beam_heatmap(beam_heatmap, analysis)
        self._build_beam_data(beam_data, analysis)
        self._build_measurements(measurements)
        self._build_events(events)
        chart_data.sheet_state = "hidden"

        workbook.properties.title = "Laser Stage Measurement Session Report"
        workbook.properties.subject = "Motion and E5574A optical-power session log"
        workbook.properties.creator = "Laser X-Z Motion Controller"
        workbook.properties.description = (
            "Time-ordered movement, timing, HP E5574A power readings, and a "
            "position-calibrated two-dimensional beam profile with measured, weather-style "
            "and profile views."
        )
        calculation = getattr(workbook, "calculation", None)
        if calculation is not None:
            calculation.fullCalcOnLoad = True
            calculation.forceFullCalc = True
        return workbook

    def _analyze_beam(self) -> BeamAnalysis:
        target_diameter_mm = self._metadata_float("Target diameter (mm)")
        if target_diameter_mm is None or target_diameter_mm <= 0:
            target_diameter_mm = 100.0
        target_radius_mm = target_diameter_mm / 2.0

        valid_rows: list[tuple[SessionMeasurementRecord, float, float, float]] = []
        for record in self.measurements:
            if record.result != "OK" or record.power_dbm is None:
                continue
            x_mm = float(record.expected_x_mm)
            z_mm = float(record.expected_z_mm)
            power_dbm = float(record.power_dbm)
            if all(math.isfinite(value) for value in (x_mm, z_mm, power_dbm)):
                valid_rows.append((record, x_mm, z_mm, power_dbm))

        if not valid_rows:
            return BeamAnalysis(
                target_diameter_mm=target_diameter_mm,
                target_radius_mm=target_radius_mm,
                center_x_mm=None,
                center_z_mm=None,
                x_min_mm=None,
                x_max_mm=None,
                z_min_mm=None,
                z_max_mm=None,
                peak_power_dbm=None,
                peak_x_mm=None,
                peak_z_mm=None,
                centroid_x_mm=None,
                centroid_z_mm=None,
                centroid_dx_mm=None,
                centroid_dz_mm=None,
                centroid_offset_mm=None,
                mean_power_dbm=None,
                power_std_db=None,
                power_spread_db=None,
                points=(),
            )

        x_values = [row[1] for row in valid_rows]
        z_values = [row[2] for row in valid_rows]
        x_min, x_max = min(x_values), max(x_values)
        z_min, z_max = min(z_values), max(z_values)
        center_x = self._metadata_float("Target center X (mm)")
        center_z = self._metadata_float("Target center Z (mm)")
        if center_x is None:
            center_x = (x_min + x_max) / 2.0
        if center_z is None:
            center_z = (z_min + z_max) / 2.0

        staged_rows: list[tuple[SessionMeasurementRecord, float, float, float, float, bool]] = []
        for record, x_mm, z_mm, power_dbm in valid_rows:
            radial_mm = math.hypot(x_mm - center_x, z_mm - center_z)
            staged_rows.append(
                (
                    record,
                    x_mm,
                    z_mm,
                    power_dbm,
                    radial_mm,
                    radial_mm <= target_radius_mm + 1e-9,
                )
            )

        target_rows = [row for row in staged_rows if row[5]]
        peak_source = target_rows or staged_rows
        peak_row = max(peak_source, key=lambda row: row[3])
        peak_power = peak_row[3]
        points = tuple(
            BeamAnalysisPoint(
                action_number=row[0].action_number,
                x_mm=row[1],
                z_mm=row[2],
                power_dbm=row[3],
                relative_db=row[3] - peak_power,
                radial_mm=row[4],
                inside_target=row[5],
            )
            for row in staged_rows
        )

        if target_rows:
            target_powers = [row[3] for row in target_rows]
            logarithmic_mean = sum(target_powers) / len(target_powers)
            weights = [10.0 ** ((row[3] - peak_power) / 10.0) for row in target_rows]
            mean_power = peak_power + 10.0 * math.log10(sum(weights) / len(weights))
            power_std = math.sqrt(
                sum((value - logarithmic_mean) ** 2 for value in target_powers)
                / len(target_powers)
            )
            power_spread = max(target_powers) - min(target_powers)

            # Relative linear power is numerically stable and physically meaningful;
            # subtracting the peak before conversion keeps every weight <= 1.
            weight_sum = sum(weights)
            centroid_x = sum(row[1] * weight for row, weight in zip(target_rows, weights)) / weight_sum
            centroid_z = sum(row[2] * weight for row, weight in zip(target_rows, weights)) / weight_sum
            centroid_dx = centroid_x - center_x
            centroid_dz = centroid_z - center_z
            centroid_offset = math.hypot(centroid_dx, centroid_dz)
        else:
            mean_power = None
            power_std = None
            power_spread = None
            centroid_x = None
            centroid_z = None
            centroid_dx = None
            centroid_dz = None
            centroid_offset = None

        return BeamAnalysis(
            target_diameter_mm=target_diameter_mm,
            target_radius_mm=target_radius_mm,
            center_x_mm=center_x,
            center_z_mm=center_z,
            x_min_mm=x_min,
            x_max_mm=x_max,
            z_min_mm=z_min,
            z_max_mm=z_max,
            peak_power_dbm=peak_power,
            peak_x_mm=peak_row[1],
            peak_z_mm=peak_row[2],
            centroid_x_mm=centroid_x,
            centroid_z_mm=centroid_z,
            centroid_dx_mm=centroid_dx,
            centroid_dz_mm=centroid_dz,
            centroid_offset_mm=centroid_offset,
            mean_power_dbm=mean_power,
            power_std_db=power_std,
            power_spread_db=power_spread,
            points=points,
        )

    def _build_beam_map(
        self,
        sheet: Any,
        chart_data: Any,
        analysis: BeamAnalysis,
    ) -> None:
        sheet.sheet_view.showGridLines = False
        sheet.freeze_panes = "A4"
        sheet.merge_cells("A1:R1")
        sheet["A1"] = f"{analysis.target_diameter_mm:g} mm Laser Beam Spatial Analysis"
        sheet["A1"].font = Font(name="Segoe UI", size=18, bold=True, color=WHITE)
        sheet["A1"].fill = PatternFill("solid", fgColor=NAVY)
        sheet["A1"].alignment = Alignment(horizontal="left", vertical="center")
        sheet.row_dimensions[1].height = 34
        sheet.merge_cells("A2:R2")
        sheet["A2"] = (
            "Two-dimensional X-Z power profile from synchronized stage positions and "
            "HP E5574A Head A readings"
        )
        sheet["A2"].font = Font(name="Segoe UI", size=10, italic=True, color=DARK_GREY)

        inside_count = len(analysis.inside_points)
        cards = (
            (1, 3, "Peak power", analysis.peak_power_dbm, '0.000000 "dBm"'),
            (4, 6, "Centroid offset", analysis.centroid_offset_mm, '0.000 "mm"'),
            (7, 9, "Inside target", f"{inside_count:,} / {len(analysis.points):,}", "General"),
            (10, 12, "Power spread", analysis.power_spread_db, '0.000 "dB"'),
            (13, 15, "Target diameter", analysis.target_diameter_mm, '0.000 "mm"'),
            (16, 18, "Valid positions", len(analysis.points), '0'),
        )
        for start_column, end_column, label, value, number_format in cards:
            self._write_metric_card(
                sheet,
                start_column,
                end_column,
                label,
                value,
                number_format,
            )

        sheet.merge_cells("A8:H8")
        sheet["A8"] = "Position and power metrics"
        sheet["A8"].fill = PatternFill("solid", fgColor=BLUE)
        sheet["A8"].font = Font(name="Segoe UI", bold=True, color=WHITE)
        details = (
            ("Target center X", analysis.center_x_mm, "Target center Z", analysis.center_z_mm, '0.000 "mm"'),
            ("Measured X range", self._format_range(analysis.x_min_mm, analysis.x_max_mm), "Measured Z range", self._format_range(analysis.z_min_mm, analysis.z_max_mm), "General"),
            ("Peak X", analysis.peak_x_mm, "Peak Z", analysis.peak_z_mm, '0.000 "mm"'),
            ("Power centroid X", analysis.centroid_x_mm, "Power centroid Z", analysis.centroid_z_mm, '0.000 "mm"'),
            ("Centroid X offset", analysis.centroid_dx_mm, "Centroid Z offset", analysis.centroid_dz_mm, '0.000 "mm"'),
            ("Mean linear power (dBm)", analysis.mean_power_dbm, "Power std. deviation (dB)", analysis.power_std_db, '0.000'),
        )
        for row, (left_label, left_value, right_label, right_value, number_format) in enumerate(details, start=9):
            self._write_detail_pair(
                sheet,
                row,
                left_label,
                left_value,
                right_label,
                right_value,
                number_format,
            )

        sheet.merge_cells("J8:R8")
        sheet["J8"] = "Interpretation and traceability"
        sheet["J8"].fill = PatternFill("solid", fgColor=TEAL)
        sheet["J8"].font = Font(name="Segoe UI", bold=True, color=WHITE)
        notes = (
            "Position basis: EVENT IDLE pulse coordinates converted with controller pulses/mm; commanded fallback for legacy events. This remains open-loop, not encoder feedback.",
            f"Plot scope: all {len(analysis.points):,} valid readings across the measured X/Z envelope. The {analysis.target_diameter_mm:g} mm circle is the ROI for target metrics.",
            "Power centroid: calculated with linear-power weighting, then reported in millimetres relative to the target center.",
            "Metrics are descriptive. No pass/fail claim is made until beam-uniformity and pointing tolerances are defined.",
        )
        for row, note in zip((9, 11, 13, 15), notes):
            sheet.merge_cells(start_row=row, start_column=10, end_row=row + 1, end_column=18)
            cell = sheet.cell(row, 10, note)
            cell.alignment = Alignment(wrap_text=True, vertical="center")
            cell.font = Font(name="Segoe UI", size=9, color=DARK_GREY)
            cell.fill = PatternFill("solid", fgColor="F3F6F9")

        for column in range(1, 19):
            sheet.column_dimensions[self._column_letter(column)].width = 11.5

        if analysis.points and analysis.center_x_mm is not None and analysis.center_z_mm is not None:
            self._add_spatial_chart(sheet, chart_data, analysis)
            self._add_radial_chart(sheet, chart_data, analysis)
        else:
            sheet.merge_cells("A18:R22")
            sheet["A18"] = (
                "No valid completed power measurements are available yet. The spatial "
                "charts will be generated automatically when the session records X, Z, and dBm data."
            )
            sheet["A18"].alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            sheet["A18"].font = Font(name="Segoe UI", size=12, italic=True, color=DARK_GREY)
            sheet["A18"].fill = PatternFill("solid", fgColor=LIGHT_GREY)

        sheet.page_setup.orientation = "landscape"
        sheet.page_setup.fitToWidth = 1
        sheet.sheet_properties.pageSetUpPr.fitToPage = True

    def _write_chart_data(
        self,
        sheet: Any,
        analysis: BeamAnalysis,
        weather_grid: BeamInterpolationGrid,
    ) -> None:
        sheet.sheet_view.showGridLines = False
        base_headers = ("Action #", "X (mm)", "Z (mm)", "Power (dBm)", "Relative (dB)", "Radius (mm)")
        for column, header in enumerate(base_headers, start=1):
            sheet.cell(1, column, header)
        for row, point in enumerate(analysis.points, start=2):
            values = (
                point.action_number,
                point.x_mm,
                point.z_mm,
                point.power_dbm,
                point.relative_db,
                point.radial_mm,
            )
            for column, value in enumerate(values, start=1):
                sheet.cell(row, column, value)

        band_start_column = 8
        for band_index, (label, lower, upper, _color) in enumerate(BEAM_POWER_BANDS):
            x_column = band_start_column + band_index * 2
            z_column = x_column + 1
            sheet.cell(1, x_column, f"{label} X")
            sheet.cell(1, z_column, f"{label} Z")
            for row, point in enumerate(analysis.points, start=2):
                if self._relative_in_band(point.relative_db, lower, upper):
                    sheet.cell(row, x_column, point.x_mm)
                    sheet.cell(row, z_column, point.z_mm)

        circle_x_column, circle_z_column = 21, 22
        sheet.cell(1, circle_x_column, "Target circle X")
        sheet.cell(1, circle_z_column, "Target circle Z")
        if analysis.center_x_mm is not None and analysis.center_z_mm is not None:
            for index in range(73):
                angle = 2.0 * math.pi * index / 72.0
                sheet.cell(index + 2, circle_x_column, analysis.center_x_mm + analysis.target_radius_mm * math.cos(angle))
                sheet.cell(index + 2, circle_z_column, analysis.center_z_mm + analysis.target_radius_mm * math.sin(angle))

        marker_sets = (
            (24, 25, "Target center", analysis.center_x_mm, analysis.center_z_mm),
            (27, 28, "Power centroid", analysis.centroid_x_mm, analysis.centroid_z_mm),
            (30, 31, "Peak power", analysis.peak_x_mm, analysis.peak_z_mm),
        )
        for x_column, z_column, label, x_value, z_value in marker_sets:
            sheet.cell(1, x_column, f"{label} X")
            sheet.cell(1, z_column, f"{label} Z")
            if x_value is not None and z_value is not None:
                sheet.cell(2, x_column, x_value)
                sheet.cell(2, z_column, z_value)

        radial_x_column, radial_y_column = 33, 34
        sheet.cell(1, radial_x_column, "Radial distance (mm)")
        sheet.cell(1, radial_y_column, "Relative power (dB)")
        for row, point in enumerate(sorted(analysis.points, key=lambda value: value.radial_mm), start=2):
            sheet.cell(row, radial_x_column, point.radial_mm)
            sheet.cell(row, radial_y_column, point.relative_db)

        boundary_x_column, boundary_y_column = 36, 37
        sheet.cell(1, boundary_x_column, "Target radius X")
        sheet.cell(1, boundary_y_column, "Target radius Y")
        minimum_relative = min((point.relative_db for point in analysis.points), default=-1.0)
        boundary_minimum = min(-1.0, math.floor(minimum_relative / 5.0) * 5.0)
        for index in range(41):
            sheet.cell(index + 2, boundary_x_column, analysis.target_radius_mm)
            sheet.cell(
                index + 2,
                boundary_y_column,
                boundary_minimum + (0.0 - boundary_minimum) * index / 40.0,
            )

        weather_start_column = 40
        sheet.cell(1, weather_start_column, "Weather Z \\ X")
        for index, x_mm in enumerate(weather_grid.x_values_mm, start=1):
            sheet.cell(1, weather_start_column + index, x_mm)
        for row_offset, (z_mm, values) in enumerate(
            zip(weather_grid.z_values_mm, weather_grid.relative_db),
            start=2,
        ):
            sheet.cell(row_offset, weather_start_column, z_mm)
            for column_offset, value in enumerate(values, start=1):
                sheet.cell(row_offset, weather_start_column + column_offset, value)

        profile_start_column = weather_start_column + max(weather_grid.size, 1) + 3
        profile_headers = (
            "Offset (mm)",
            "X centerline (dB)",
            "Z centerline (dB)",
            "Cross-section -3 dB",
            "Radius (mm)",
            "Radial average (dB)",
            "Radial -3 dB",
            "Radial samples",
        )
        for offset, header in enumerate(profile_headers):
            sheet.cell(1, profile_start_column + offset, header)

        x_profile = weather_grid.x_profile
        z_profile = weather_grid.z_profile
        if analysis.center_x_mm is not None and analysis.center_z_mm is not None:
            for row, ((x_mm, x_value), (_z_mm, z_value)) in enumerate(
                zip(x_profile, z_profile),
                start=2,
            ):
                sheet.cell(row, profile_start_column, x_mm - analysis.center_x_mm)
                sheet.cell(row, profile_start_column + 1, x_value)
                sheet.cell(row, profile_start_column + 2, z_value)
                sheet.cell(row, profile_start_column + 3, -3.0)
        for row, (radius_mm, relative_db, sample_count) in enumerate(
            radial_average_profile(analysis),
            start=2,
        ):
            sheet.cell(row, profile_start_column + 4, radius_mm)
            sheet.cell(row, profile_start_column + 5, relative_db)
            sheet.cell(row, profile_start_column + 6, -3.0)
            sheet.cell(row, profile_start_column + 7, sample_count)

    def _add_spatial_chart(self, sheet: Any, chart_data: Any, analysis: BeamAnalysis) -> None:
        chart = ScatterChart()
        chart.title = "Complete X-Z scan - all valid power readings"
        chart.style = 13
        chart.scatterStyle = "marker"
        chart.height = 15.5
        chart.width = 15.5
        chart.legend.position = "b"
        chart.x_axis.title = "X position (mm)"
        chart.y_axis.title = "Z position (mm)"
        chart.x_axis.axPos = "b"
        chart.y_axis.axPos = "l"
        x_min = min(analysis.x_min_mm, analysis.center_x_mm - analysis.target_radius_mm)
        x_max = max(analysis.x_max_mm, analysis.center_x_mm + analysis.target_radius_mm)
        z_min = min(analysis.z_min_mm, analysis.center_z_mm - analysis.target_radius_mm)
        z_max = max(analysis.z_max_mm, analysis.center_z_mm + analysis.target_radius_mm)
        margin = max(max(x_max - x_min, z_max - z_min) * 0.025, 1.0)
        chart.x_axis.scaling.min = x_min - margin
        chart.x_axis.scaling.max = x_max + margin
        chart.y_axis.scaling.min = z_min - margin
        chart.y_axis.scaling.max = z_max + margin
        chart.x_axis.numFmt = '0.0'
        chart.y_axis.numFmt = '0.0'

        point_last_row = len(analysis.points) + 1
        for band_index, (label, _lower, _upper, color) in enumerate(BEAM_POWER_BANDS):
            x_column = 8 + band_index * 2
            z_column = x_column + 1
            series = Series(
                Reference(chart_data, min_col=z_column, min_row=2, max_row=point_last_row),
                Reference(chart_data, min_col=x_column, min_row=2, max_row=point_last_row),
                title=label,
            )
            self._style_marker_series(series, color, "circle", 5)
            chart.series.append(series)

        circle_series = Series(
            Reference(chart_data, min_col=22, min_row=2, max_row=74),
            Reference(chart_data, min_col=21, min_row=2, max_row=74),
            title=f"{analysis.target_diameter_mm:g} mm target boundary",
        )
        circle_series.marker.symbol = "circle"
        circle_series.marker.size = 2
        circle_series.graphicalProperties.line.solidFill = DARK_GREY
        circle_series.graphicalProperties.line.width = 19050
        chart.series.append(circle_series)

        markers = (
            (24, 25, "Target center", DARK_GREY, "plus", 9),
            (27, 28, "Power centroid", "7030A0", "diamond", 8),
            (30, 31, "Peak power", "C00000", "star", 9),
        )
        for x_column, z_column, label, color, symbol, size in markers:
            if chart_data.cell(2, x_column).value is None:
                continue
            series = Series(
                Reference(chart_data, min_col=z_column, min_row=2, max_row=2),
                Reference(chart_data, min_col=x_column, min_row=2, max_row=2),
                title=label,
            )
            self._style_marker_series(series, color, symbol, size)
            chart.series.append(series)

        sheet.add_chart(chart, "A17")

    def _add_radial_chart(self, sheet: Any, chart_data: Any, analysis: BeamAnalysis) -> None:
        chart = ScatterChart()
        chart.title = "Radial power profile — distance from target center"
        chart.style = 13
        chart.scatterStyle = "marker"
        chart.height = 15.5
        chart.width = 15.5
        chart.legend.position = "b"
        chart.x_axis.title = "Radial distance (mm)"
        chart.y_axis.title = "Power relative to target peak (dB)"
        chart.x_axis.axPos = "b"
        chart.y_axis.axPos = "l"
        chart.x_axis.scaling.min = 0.0
        maximum_radius = max((point.radial_mm for point in analysis.points), default=analysis.target_radius_mm)
        chart.x_axis.scaling.max = max(maximum_radius, analysis.target_radius_mm) * 1.05
        minimum_relative = min((point.relative_db for point in analysis.points), default=-1.0)
        chart.y_axis.scaling.min = min(-1.0, math.floor(minimum_relative / 5.0) * 5.0)
        chart.y_axis.scaling.max = 0.5
        chart.x_axis.numFmt = '0.0'
        chart.y_axis.numFmt = '0.0'

        point_last_row = len(analysis.points) + 1
        measurements = Series(
            Reference(chart_data, min_col=34, min_row=2, max_row=point_last_row),
            Reference(chart_data, min_col=33, min_row=2, max_row=point_last_row),
            title="Measured positions",
        )
        self._style_marker_series(measurements, "2F75B5", "circle", 5)
        chart.series.append(measurements)

        boundary = Series(
            Reference(chart_data, min_col=37, min_row=2, max_row=42),
            Reference(chart_data, min_col=36, min_row=2, max_row=42),
            title=f"{analysis.target_radius_mm:g} mm target radius",
        )
        boundary.marker.symbol = "circle"
        boundary.marker.size = 3
        boundary.marker.graphicalProperties.solidFill = "C00000"
        boundary.marker.graphicalProperties.line.solidFill = "C00000"
        boundary.graphicalProperties.line.solidFill = "C00000"
        boundary.graphicalProperties.line.width = 19050
        chart.series.append(boundary)
        sheet.add_chart(chart, "J17")

    def _build_weather_map(
        self,
        sheet: Any,
        analysis: BeamAnalysis,
        weather_grid: BeamInterpolationGrid,
    ) -> None:
        sheet.sheet_view.showGridLines = False
        sheet.freeze_panes = "B6"
        title_end_column = max(18, weather_grid.size + 10)
        sheet.merge_cells(start_row=1, start_column=1, end_row=1, end_column=title_end_column)
        sheet["A1"] = "Weather-Style Beam Power Map"
        sheet["A1"].font = Font(name="Segoe UI", size=18, bold=True, color=WHITE)
        sheet["A1"].fill = PatternFill("solid", fgColor=NAVY)
        sheet["A1"].alignment = Alignment(horizontal="left", vertical="center")
        sheet.row_dimensions[1].height = 34
        sheet.merge_cells(start_row=2, start_column=1, end_row=2, end_column=title_end_column)
        sheet["A2"] = (
            "Continuous X-Z power estimate across the complete measured scan. Values are "
            "inverse-distance interpolated in linear power and shown relative to the in-target peak."
        )
        sheet["A2"].font = Font(name="Segoe UI", size=10, italic=True, color=DARK_GREY)
        sheet.merge_cells(start_row=3, start_column=1, end_row=3, end_column=title_end_column)
        sheet["A3"] = (
            "All valid readings contribute to this map; the outlined target circle remains the "
            "region used for target metrics. Use Beam Heatmap and Beam Data for raw values."
        )
        sheet["A3"].font = Font(name="Segoe UI", size=9, color=DARK_GREY)

        if not weather_grid.relative_db:
            sheet.merge_cells("A6:R12")
            sheet["A6"] = "No valid X/Z power samples are available for interpolation."
            sheet["A6"].alignment = Alignment(horizontal="center", vertical="center")
            sheet["A6"].fill = PatternFill("solid", fgColor=LIGHT_GREY)
            return

        grid_start_row = 6
        grid_start_column = 2
        sheet.cell(grid_start_row - 1, 1, "Z \\ X")
        sheet.cell(grid_start_row - 1, 1).fill = PatternFill("solid", fgColor=BLUE)
        sheet.cell(grid_start_row - 1, 1).font = Font(name="Segoe UI", bold=True, color=WHITE)
        for index, x_mm in enumerate(weather_grid.x_values_mm):
            cell = sheet.cell(grid_start_row - 1, grid_start_column + index)
            if index % 4 == 0 or index == weather_grid.size - 1:
                cell.value = x_mm
                cell.number_format = "0.0"
            cell.fill = PatternFill("solid", fgColor=BLUE)
            cell.font = Font(name="Segoe UI", size=7, bold=True, color=WHITE)
            cell.alignment = Alignment(horizontal="center", vertical="center", text_rotation=90)

        for row_index, (z_mm, values) in enumerate(
            zip(weather_grid.z_values_mm, weather_grid.relative_db)
        ):
            label_cell = sheet.cell(grid_start_row + row_index, 1)
            if row_index % 4 == 0 or row_index == weather_grid.size - 1:
                label_cell.value = z_mm
                label_cell.number_format = "0.0"
            label_cell.fill = PatternFill("solid", fgColor=BLUE)
            label_cell.font = Font(name="Segoe UI", size=7, bold=True, color=WHITE)
            label_cell.alignment = Alignment(horizontal="center", vertical="center")
            for column_index, value in enumerate(values):
                if value is None:
                    continue
                cell = sheet.cell(
                    grid_start_row + row_index,
                    grid_start_column + column_index,
                    value,
                )
                cell.number_format = ";;;"

        data_end_row = grid_start_row + weather_grid.size - 1
        data_end_column = grid_start_column + weather_grid.size - 1
        minimum_relative = min(
            value
            for row in weather_grid.relative_db
            for value in row
            if value is not None
        )
        sheet.conditional_formatting.add(
            f"B{grid_start_row}:{self._column_letter(data_end_column)}{data_end_row}",
            ColorScaleRule(
                start_type="num",
                start_value=min(-15.0, minimum_relative),
                start_color="313695",
                mid_type="num",
                mid_value=-6.0,
                mid_color="FFFFBF",
                end_type="num",
                end_value=0.0,
                end_color="A50026",
            ),
        )

        def marker_cell(x_mm: float | None, z_mm: float | None) -> Any | None:
            if x_mm is None or z_mm is None:
                return None
            column_index = min(
                range(weather_grid.size),
                key=lambda index: abs(weather_grid.x_values_mm[index] - x_mm),
            )
            row_index = min(
                range(weather_grid.size),
                key=lambda index: abs(weather_grid.z_values_mm[index] - z_mm),
            )
            return sheet.cell(grid_start_row + row_index, grid_start_column + column_index)

        marker_specs = (
            (analysis.center_x_mm, analysis.center_z_mm, "000000"),
            (analysis.centroid_x_mm, analysis.centroid_z_mm, "7030A0"),
            (analysis.peak_x_mm, analysis.peak_z_mm, "C00000"),
        )
        for x_mm, z_mm, color in marker_specs:
            cell = marker_cell(x_mm, z_mm)
            if cell is not None:
                side = Side(style="thick", color=color)
                cell.border = Border(left=side, right=side, top=side, bottom=side)

        sheet.column_dimensions["A"].width = 10
        for column in range(grid_start_column, data_end_column + 1):
            sheet.column_dimensions[self._column_letter(column)].width = 2.7
        sheet.row_dimensions[grid_start_row - 1].height = 48
        for row in range(grid_start_row, data_end_row + 1):
            sheet.row_dimensions[row].height = 15

        legend_column = data_end_column + 3
        legend_letter = self._column_letter(legend_column)
        sheet.cell(5, legend_column, "Relative power")
        sheet.cell(5, legend_column).font = Font(name="Segoe UI", bold=True, color=WHITE)
        sheet.cell(5, legend_column).fill = PatternFill("solid", fgColor=TEAL)
        sheet.merge_cells(start_row=5, start_column=legend_column, end_row=5, end_column=legend_column + 2)
        for row_offset, (level, color) in enumerate(reversed(WEATHER_COLOR_STOPS), start=6):
            sheet.cell(row_offset, legend_column).fill = PatternFill("solid", fgColor=color)
            sheet.cell(row_offset, legend_column + 1, f"{level:g} dB")
            sheet.cell(row_offset, legend_column + 1).font = Font(name="Segoe UI", size=9)
        marker_legend = (
            ("000000", "Target center"),
            ("7030A0", "Power centroid"),
            ("C00000", "Measured peak"),
        )
        marker_row = 14
        for color, label in marker_legend:
            side = Side(style="thick", color=color)
            sheet.cell(marker_row, legend_column).border = Border(
                left=side, right=side, top=side, bottom=side
            )
            sheet.cell(marker_row, legend_column + 1, label)
            marker_row += 2
        sheet.column_dimensions[legend_letter].width = 5
        sheet.column_dimensions[self._column_letter(legend_column + 1)].width = 19
        sheet.column_dimensions[self._column_letter(legend_column + 2)].width = 3

        note_row = data_end_row + 2
        sheet.merge_cells(
            start_row=note_row,
            start_column=1,
            end_row=note_row + 1,
            end_column=min(title_end_column, data_end_column),
        )
        sheet.cell(note_row, 1).value = (
            "Method: 12-nearest-point inverse-distance weighting applied to linear power, "
            "then converted to dB relative to the measured in-target peak. The grid spans the "
            "full measured X/Z envelope; the target boundary is retained as the analysis ROI."
        )
        sheet.cell(note_row, 1).alignment = Alignment(wrap_text=True, vertical="center")
        sheet.cell(note_row, 1).font = Font(name="Segoe UI", size=9, italic=True, color=DARK_GREY)
        sheet.sheet_view.zoomScale = 60
        sheet.page_setup.orientation = "landscape"
        sheet.page_setup.fitToWidth = 1
        sheet.sheet_properties.pageSetUpPr.fitToPage = True

    def _build_beam_profiles(
        self,
        sheet: Any,
        chart_data: Any,
        analysis: BeamAnalysis,
        weather_grid: BeamInterpolationGrid,
    ) -> None:
        sheet.sheet_view.showGridLines = False
        sheet.merge_cells("A1:R1")
        sheet["A1"] = "Beam Cross-Section and Radial Profiles"
        sheet["A1"].font = Font(name="Segoe UI", size=18, bold=True, color=WHITE)
        sheet["A1"].fill = PatternFill("solid", fgColor=NAVY)
        sheet.row_dimensions[1].height = 34
        sheet.merge_cells("A2:R2")
        sheet["A2"] = (
            "Centerline X/Z cuts from the weather interpolation and radial averages from "
            "measured in-target samples. The red line marks -3 dB."
        )
        sheet["A2"].font = Font(name="Segoe UI", size=10, italic=True, color=DARK_GREY)
        if not weather_grid.relative_db:
            sheet.merge_cells("A5:R12")
            sheet["A5"] = "No valid samples are available for beam profiles."
            sheet["A5"].alignment = Alignment(horizontal="center", vertical="center")
            sheet["A5"].fill = PatternFill("solid", fgColor=LIGHT_GREY)
            return

        profile_start_column = 40 + max(weather_grid.size, 1) + 3
        profile_last_row = weather_grid.size + 1
        cross_chart = LineChart()
        cross_chart.title = "Centerline X and Z power profiles"
        cross_chart.style = 13
        cross_chart.height = 15
        cross_chart.width = 15.5
        cross_chart.legend.position = "b"
        cross_chart.x_axis.title = "Offset from target center (mm)"
        cross_chart.y_axis.title = "Power relative to peak (dB)"
        cross_chart.y_axis.scaling.max = 0.5
        minimum_relative = min(
            value
            for row in weather_grid.relative_db
            for value in row
            if value is not None
        )
        cross_chart.y_axis.scaling.min = min(-5.0, math.floor(minimum_relative / 5.0) * 5.0)
        cross_chart.y_axis.numFmt = "0.0"
        cross_chart.x_axis.axPos = "b"
        cross_chart.x_axis.tickLblPos = "low"
        cross_chart.y_axis.axPos = "l"
        cross_chart.y_axis.crosses = "min"
        cross_chart.add_data(
            Reference(
                chart_data,
                min_col=profile_start_column + 1,
                max_col=profile_start_column + 3,
                min_row=1,
                max_row=profile_last_row,
            ),
            titles_from_data=True,
        )
        cross_chart.set_categories(
            Reference(
                chart_data,
                min_col=profile_start_column,
                min_row=2,
                max_row=profile_last_row,
            )
        )
        cross_chart.x_axis.tickLblSkip = 4
        for series, label, color, marker in zip(
            cross_chart.series,
            ("X centerline", "Z centerline", "-3 dB reference"),
            ("2F75B5", "70AD47", "C00000"),
            (True, True, False),
        ):
            series.tx = SeriesLabel(v=label)
            self._style_line_series(series, color, marker=marker)
        sheet.add_chart(cross_chart, "A5")

        radial_profile = radial_average_profile(analysis)
        radial_chart = LineChart()
        radial_chart.title = "Measured radial-average profile"
        radial_chart.style = 13
        radial_chart.height = 15
        radial_chart.width = 15.5
        radial_chart.legend.position = "b"
        radial_chart.x_axis.title = "Radius from target center (mm)"
        radial_chart.y_axis.title = "Average power relative to peak (dB)"
        radial_chart.y_axis.scaling.min = cross_chart.y_axis.scaling.min
        radial_chart.y_axis.scaling.max = 0.5
        radial_chart.y_axis.numFmt = "0.0"
        radial_chart.x_axis.axPos = "b"
        radial_chart.x_axis.tickLblPos = "low"
        radial_chart.y_axis.axPos = "l"
        radial_chart.y_axis.crosses = "min"
        if radial_profile:
            radial_last_row = len(radial_profile) + 1
            radial_chart.add_data(
                Reference(
                    chart_data,
                    min_col=profile_start_column + 5,
                    max_col=profile_start_column + 6,
                    min_row=1,
                    max_row=radial_last_row,
                ),
                titles_from_data=True,
            )
            radial_chart.set_categories(
                Reference(
                    chart_data,
                    min_col=profile_start_column + 4,
                    min_row=2,
                    max_row=radial_last_row,
                ),
            )
            radial_chart.x_axis.tickLblSkip = 2
            radial_chart.series[0].tx = SeriesLabel(v="Measured radial average")
            radial_chart.series[1].tx = SeriesLabel(v="-3 dB reference")
            self._style_line_series(radial_chart.series[0], "7030A0")
            self._style_line_series(radial_chart.series[1], "C00000", marker=False)
        sheet.add_chart(radial_chart, "J5")

        sheet.merge_cells("A34:R36")
        sheet["A34"] = (
            "Centerline curves come from the interpolated map at the target center. The radial "
            "curve averages measured linear power in 20 equal radial bins; empty bins are omitted."
        )
        sheet["A34"].alignment = Alignment(wrap_text=True, vertical="center")
        sheet["A34"].font = Font(name="Segoe UI", size=9, italic=True, color=DARK_GREY)
        for column in range(1, 19):
            sheet.column_dimensions[self._column_letter(column)].width = 11
        sheet.page_setup.orientation = "landscape"
        sheet.page_setup.fitToWidth = 1
        sheet.sheet_properties.pageSetUpPr.fitToPage = True

    def _build_beam_heatmap(self, sheet: Any, analysis: BeamAnalysis) -> None:
        sheet.sheet_view.showGridLines = False
        sheet.freeze_panes = "B5"
        sheet["A1"] = "Complete Measured Power Heatmap (dBm)"
        sheet["A1"].font = Font(name="Segoe UI", size=18, bold=True, color=WHITE)
        sheet["A1"].fill = PatternFill("solid", fgColor=NAVY)
        sheet.row_dimensions[1].height = 34
        sheet["A2"] = (
            "Average HP E5574A power at every measured X/Z coordinate across the complete "
            "scan; blank cells are unmeasured. Target-circle classification remains in Beam Data."
        )
        sheet["A2"].font = Font(name="Segoe UI", size=10, italic=True, color=DARK_GREY)

        if not analysis.points or analysis.center_x_mm is None or analysis.center_z_mm is None:
            sheet.merge_cells("A1:H1")
            sheet.merge_cells("A2:H2")
            sheet.merge_cells("A4:H8")
            sheet["A4"] = "No valid X/Z power samples are available for the heatmap."
            sheet["A4"].alignment = Alignment(horizontal="center", vertical="center")
            sheet["A4"].fill = PatternFill("solid", fgColor=LIGHT_GREY)
            return

        x_values = sorted({round(point.x_mm, 6) for point in analysis.points})
        z_values = sorted({round(point.z_mm, 6) for point in analysis.points}, reverse=True)
        title_end_column = max(8, len(x_values) + 1)
        sheet.merge_cells(start_row=1, start_column=1, end_row=1, end_column=title_end_column)
        sheet.merge_cells(start_row=2, start_column=1, end_row=2, end_column=title_end_column)
        cell_count = len(x_values) * len(z_values)
        if cell_count > 250_000:
            sheet.merge_cells("A4:H9")
            sheet["A4"] = (
                f"Heatmap omitted because the measured coordinate grid would contain "
                f"{cell_count:,} cells. The native X-Z scatter map remains available on Beam Map."
            )
            sheet["A4"].alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            sheet["A4"].fill = PatternFill("solid", fgColor=AMBER)
            return

        aggregates: dict[tuple[float, float], list[float]] = defaultdict(list)
        for point in analysis.points:
            aggregates[(round(point.x_mm, 6), round(point.z_mm, 6))].append(point.power_dbm)

        sheet.cell(4, 1, "Z \\ X (mm)")
        for column, x_mm in enumerate(x_values, start=2):
            sheet.cell(4, column, x_mm)
            sheet.cell(4, column).number_format = "0.###"
        for row, z_mm in enumerate(z_values, start=5):
            sheet.cell(row, 1, z_mm)
            sheet.cell(row, 1).number_format = "0.###"
            for column, x_mm in enumerate(x_values, start=2):
                values = aggregates.get((x_mm, z_mm))
                if values:
                    sheet.cell(row, column, self._mean_dbm(values))
                    sheet.cell(row, column).number_format = "0.00"
                    sheet.cell(row, column).font = Font(name="Segoe UI", size=8)
                    sheet.cell(row, column).alignment = Alignment(horizontal="center", vertical="center")

        header_fill = PatternFill("solid", fgColor=BLUE)
        for cell in sheet[4]:
            cell.fill = header_fill
            cell.font = Font(name="Segoe UI", size=8, bold=True, color=WHITE)
            cell.alignment = Alignment(horizontal="center", vertical="center", text_rotation=90 if cell.column > 1 else 0)
        for row in range(5, 5 + len(z_values)):
            sheet.cell(row, 1).fill = header_fill
            sheet.cell(row, 1).font = Font(name="Segoe UI", size=8, bold=True, color=WHITE)
            sheet.cell(row, 1).alignment = Alignment(horizontal="center", vertical="center")

        if x_values and z_values:
            start = f"B5"
            end = f"{self._column_letter(len(x_values) + 1)}{len(z_values) + 4}"
            sheet.conditional_formatting.add(
                f"{start}:{end}",
                ColorScaleRule(
                    start_type="min",
                    start_color="313695",
                    mid_type="percentile",
                    mid_value=50,
                    mid_color="FFFFBF",
                    end_type="max",
                    end_color="A50026",
                ),
            )
        sheet.column_dimensions["A"].width = 12
        for column in range(2, len(x_values) + 2):
            sheet.column_dimensions[self._column_letter(column)].width = 5.4
        sheet.row_dimensions[4].height = 48
        for row in range(5, len(z_values) + 5):
            sheet.row_dimensions[row].height = 16
        sheet.sheet_view.zoomScale = 70
        sheet.page_setup.orientation = "landscape"
        sheet.page_setup.fitToWidth = 1
        sheet.sheet_properties.pageSetUpPr.fitToPage = True

    def _build_beam_data(self, sheet: Any, analysis: BeamAnalysis) -> None:
        headers = (
            "Point #",
            "Action #",
            "X position (mm)",
            "Z position (mm)",
            "Power (dBm)",
            "Relative to target peak (dB)",
            "Radial distance (mm)",
            "Inside target circle",
            "Position source",
        )
        sheet.sheet_view.showGridLines = False
        sheet.freeze_panes = "C2"
        sheet.append(headers)
        for index, point in enumerate(analysis.points, start=1):
            sheet.append(
                (
                    index,
                    point.action_number,
                    point.x_mm,
                    point.z_mm,
                    point.power_dbm,
                    point.relative_db,
                    point.radial_mm,
                    "Yes" if point.inside_target else "No",
                    "EVENT IDLE pulse position converted to mm; commanded fallback for legacy events",
                )
            )
        self._style_table_sheet(sheet, len(headers), "BeamAnalysisData")
        widths = (11, 11, 18, 18, 17, 29, 22, 20, 39)
        for index, width in enumerate(widths, start=1):
            sheet.column_dimensions[self._column_letter(index)].width = width
        for row in range(2, sheet.max_row + 1):
            for column in (3, 4, 7):
                sheet.cell(row, column).number_format = '0.000'
            for column in (5, 6):
                sheet.cell(row, column).number_format = '0.000000'
        if sheet.max_row >= 2:
            sheet.conditional_formatting.add(
                f"E2:E{sheet.max_row}",
                ColorScaleRule(
                    start_type="min",
                    start_color="313695",
                    mid_type="percentile",
                    mid_value=50,
                    mid_color="FFFFBF",
                    end_type="max",
                    end_color="A50026",
                ),
            )

    @staticmethod
    def _write_metric_card(
        sheet: Any,
        start_column: int,
        end_column: int,
        label: str,
        value: Any,
        number_format: str,
    ) -> None:
        sheet.merge_cells(start_row=4, start_column=start_column, end_row=4, end_column=end_column)
        sheet.merge_cells(start_row=5, start_column=start_column, end_row=6, end_column=end_column)
        label_cell = sheet.cell(4, start_column, label)
        value_cell = sheet.cell(5, start_column, "No data" if value is None else value)
        label_cell.fill = PatternFill("solid", fgColor=BLUE)
        label_cell.font = Font(name="Segoe UI", bold=True, color=WHITE)
        label_cell.alignment = Alignment(horizontal="center", vertical="center")
        value_cell.fill = PatternFill("solid", fgColor=LIGHT_BLUE)
        value_cell.font = Font(name="Segoe UI", size=15, bold=True, color=NAVY)
        value_cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        value_cell.number_format = number_format

    @staticmethod
    def _write_detail_pair(
        sheet: Any,
        row: int,
        left_label: str,
        left_value: Any,
        right_label: str,
        right_value: Any,
        number_format: str,
    ) -> None:
        sheet.merge_cells(start_row=row, start_column=1, end_row=row, end_column=2)
        sheet.merge_cells(start_row=row, start_column=3, end_row=row, end_column=4)
        sheet.merge_cells(start_row=row, start_column=5, end_row=row, end_column=6)
        sheet.merge_cells(start_row=row, start_column=7, end_row=row, end_column=8)
        for label_column, value_column, label, value in (
            (1, 3, left_label, left_value),
            (5, 7, right_label, right_value),
        ):
            label_cell = sheet.cell(row, label_column, label)
            value_cell = sheet.cell(row, value_column, "No data" if value is None else value)
            label_cell.fill = PatternFill("solid", fgColor=LIGHT_GREY)
            label_cell.font = Font(name="Segoe UI", size=9, bold=True, color=DARK_GREY)
            value_cell.font = Font(name="Segoe UI", size=9, color="1F1F1F")
            value_cell.alignment = Alignment(horizontal="right", vertical="center")
            value_cell.number_format = number_format

    @staticmethod
    def _style_marker_series(series: Any, color: str, symbol: str, size: int) -> None:
        series.marker.symbol = symbol
        series.marker.size = size
        series.marker.graphicalProperties.solidFill = color
        series.marker.graphicalProperties.line.solidFill = color
        series.graphicalProperties.line.noFill = True

    @staticmethod
    def _style_line_series(series: Any, color: str, *, marker: bool = True) -> None:
        series.graphicalProperties.line.solidFill = color
        series.graphicalProperties.line.width = 28575
        if marker:
            series.marker.symbol = "circle"
            series.marker.size = 4
            series.marker.graphicalProperties.solidFill = color
            series.marker.graphicalProperties.line.solidFill = color
        else:
            series.marker.symbol = "none"

    @staticmethod
    def _relative_in_band(value: float, lower: float | None, upper: float | None) -> bool:
        if lower is not None and value < lower:
            return False
        if upper is not None and value >= upper:
            return False
        return True

    def _metadata_float(self, key: str) -> float | None:
        try:
            value = float(self.metadata.get(key, ""))
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) else None

    @staticmethod
    def _mean_dbm(values: list[float]) -> float:
        """Average powers in linear units and return the result in dBm."""
        reference = max(values)
        return reference + 10.0 * math.log10(
            sum(10.0 ** ((value - reference) / 10.0) for value in values)
            / len(values)
        )

    @staticmethod
    def _format_range(minimum: float | None, maximum: float | None) -> str:
        if minimum is None or maximum is None:
            return "No data"
        return f"{minimum:.3f} to {maximum:.3f} mm"

    def _build_summary(
        self,
        sheet: Any,
        *,
        analysis: BeamAnalysis,
        status: str,
        reason: str,
        ended_at: datetime,
        duration_s: float,
        completed_actions: int,
    ) -> None:
        sheet.sheet_view.showGridLines = False
        sheet.freeze_panes = "A4"
        sheet.merge_cells("A1:F1")
        title = sheet["A1"]
        title.value = "Laser Stage Measurement Session Report"
        title.font = Font(name="Segoe UI", size=18, bold=True, color=WHITE)
        title.fill = PatternFill("solid", fgColor=NAVY)
        title.alignment = Alignment(horizontal="left", vertical="center")
        sheet.row_dimensions[1].height = 34

        sheet.merge_cells("A2:F2")
        sheet["A2"] = (
            "Synchronized X/Z motion and HP E5574A Head A absolute-power readings"
        )
        sheet["A2"].font = Font(name="Segoe UI", size=10, italic=True, color="44546A")

        metadata_rows = [
            ("Session ID", self.session_id),
            ("Status", status),
            ("Status detail", reason),
            ("Started", self._excel_datetime(self.started_at)),
            ("Ended", self._excel_datetime(ended_at)),
            ("Duration (s)", float(duration_s)),
            ("Recipe", self.metadata.get("Recipe", "")),
            ("Controller firmware", self.metadata.get("Controller firmware", "")),
            ("VISA resource", self.metadata.get("VISA resource", "")),
            ("Instrument", self.metadata.get("Instrument", "")),
            ("Power acquisition", "Head A (SENS1), absolute power, dBm"),
        ]
        settings_rows = [
            ("Scan axis", self.metadata.get("Scan axis", "")),
            ("Cross axis", self.metadata.get("Cross axis", "")),
            ("Loops", self.metadata.get("Loops", "")),
            ("Scan span (mm)", self.metadata.get("Scan span (mm)", "")),
            ("Scan increment (mm)", self.metadata.get("Scan increment (mm)", "")),
            ("Cross shift (mm)", self.metadata.get("Cross shift (mm)", "")),
            ("Target diameter (mm)", analysis.target_diameter_mm),
            ("Post-move dwell (s)", self.metadata.get("Post-move dwell (s)", "")),
            ("Motion speed (mm/s)", self.metadata.get("Motion speed (mm/s)", "")),
            ("Acceleration (mm/s^2)", self.metadata.get("Acceleration (mm/s^2)", "")),
            ("X pulses/mm", self.metadata.get("X pulses/mm", "")),
            ("Z pulses/mm", self.metadata.get("Z pulses/mm", "")),
            ("Expected actions", self.metadata.get("Expected actions", "")),
            ("Completed actions", completed_actions),
        ]
        self._write_summary_block(sheet, 4, 1, "Session", metadata_rows)
        self._write_summary_block(sheet, 4, 4, "Configuration", settings_rows)

        statistics_row = 20
        sheet.cell(statistics_row, 1, "Measurement statistics")
        sheet.cell(statistics_row, 1).font = Font(name="Segoe UI", bold=True, color=WHITE)
        sheet.cell(statistics_row, 1).fill = PatternFill("solid", fgColor=BLUE)
        sheet.merge_cells(start_row=statistics_row, start_column=1, end_row=statistics_row, end_column=2)
        successful = sum(record.result == "OK" for record in self.measurements)
        failed = len(self.measurements) - successful
        power_last_row = max(2, len(self.measurements) + 1)
        power_range = f"'Measurements'!$T$2:$T${power_last_row}"
        statistics = [
            ("Measurement rows", len(self.measurements)),
            ("Successful readings", successful),
            ("Failed readings", failed),
            ("Minimum power (dBm)", f'=IF(COUNT({power_range})=0,"",MIN({power_range}))'),
            ("Average power (dBm)", f'=IF(COUNT({power_range})=0,"",AVERAGE({power_range}))'),
            ("Maximum power (dBm)", f'=IF(COUNT({power_range})=0,"",MAX({power_range}))'),
        ]
        for offset, (label, value) in enumerate(statistics, start=1):
            row = statistics_row + offset
            sheet.cell(row, 1, label)
            sheet.cell(row, 2, value)
            sheet.cell(row, 1).font = Font(name="Segoe UI", bold=True, color="44546A")
            sheet.cell(row, 1).fill = PatternFill("solid", fgColor=LIGHT_GREY)
            if "power" in label.lower():
                sheet.cell(row, 2).number_format = '0.000000 "dBm"'

        sheet.merge_cells("D20:E20")
        sheet["D20"] = "Beam accuracy overview"
        sheet["D20"].font = Font(name="Segoe UI", bold=True, color=WHITE)
        sheet["D20"].fill = PatternFill("solid", fgColor=TEAL)
        beam_statistics = (
            ("Target diameter", analysis.target_diameter_mm, '0.000 "mm"'),
            ("In-target positions", len(analysis.inside_points), '0'),
            ("Power centroid offset", analysis.centroid_offset_mm, '0.000 "mm"'),
            ("Peak power", analysis.peak_power_dbm, '0.000000 "dBm"'),
            ("Power spread", analysis.power_spread_db, '0.000 "dB"'),
            ("Detailed analysis", "See Weather Map and Beam Profiles", "General"),
        )
        for row, (label, value, number_format) in enumerate(beam_statistics, start=21):
            sheet.cell(row, 4, label)
            sheet.cell(row, 5, "No data" if value is None else value)
            sheet.cell(row, 4).font = Font(name="Segoe UI", bold=True, color=DARK_GREY)
            sheet.cell(row, 4).fill = PatternFill("solid", fgColor=LIGHT_GREY)
            sheet.cell(row, 5).number_format = number_format

        status_cell = sheet["B6"]
        status_fill = GREEN if status == "Completed" else AMBER if status == "Stopped" else RED
        status_cell.fill = PatternFill("solid", fgColor=status_fill)
        status_cell.font = Font(name="Segoe UI", bold=True, color="1F1F1F")

        for column, width in {"A": 25, "B": 39, "C": 3, "D": 25, "E": 22, "F": 3}.items():
            sheet.column_dimensions[column].width = width
        sheet.page_setup.orientation = "landscape"
        sheet.page_setup.fitToWidth = 1
        sheet.sheet_properties.pageSetUpPr.fitToPage = True

    @staticmethod
    def _write_summary_block(
        sheet: Any,
        start_row: int,
        start_column: int,
        title: str,
        rows: list[tuple[str, Any]],
    ) -> None:
        title_cell = sheet.cell(start_row, start_column, title)
        title_cell.font = Font(name="Segoe UI", bold=True, color=WHITE)
        title_cell.fill = PatternFill("solid", fgColor=BLUE)
        sheet.merge_cells(
            start_row=start_row,
            start_column=start_column,
            end_row=start_row,
            end_column=start_column + 1,
        )
        for offset, (label, value) in enumerate(rows, start=1):
            row = start_row + offset
            label_cell = sheet.cell(row, start_column, label)
            value_cell = sheet.cell(row, start_column + 1, value)
            label_cell.font = Font(name="Segoe UI", bold=True, color="44546A")
            label_cell.fill = PatternFill("solid", fgColor=LIGHT_GREY)
            value_cell.alignment = Alignment(wrap_text=True, vertical="top")
            if isinstance(value, datetime):
                value_cell.number_format = "yyyy-mm-dd hh:mm:ss.000"
            elif isinstance(value, float):
                value_cell.number_format = "0.000"

    def _build_measurements(self, sheet: Any) -> None:
        sheet.sheet_view.showGridLines = False
        sheet.freeze_panes = "F2"
        sheet.append(self.MEASUREMENT_HEADERS)
        for record in self.measurements:
            sheet.append(
                (
                    record.action_number,
                    self._excel_datetime(record.measurement_timestamp),
                    record.elapsed_s,
                    int(record.elapsed_s),
                    record.loop_number,
                    record.phase,
                    record.phase_step,
                    record.phase_steps,
                    record.command,
                    record.axis,
                    record.delta_mm,
                    record.expected_x_mm,
                    record.expected_z_mm,
                    self._excel_datetime(record.move_started_at),
                    self._excel_datetime(record.motion_completed_at),
                    self._excel_datetime(record.measurement_started_at),
                    self._excel_datetime(record.measurement_completed_at),
                    record.dwell_target_s,
                    record.actual_post_move_wait_s,
                    record.power_dbm,
                    record.raw_response,
                    record.result,
                    record.notes,
                )
            )
        self._style_table_sheet(sheet, len(self.MEASUREMENT_HEADERS), "SessionMeasurements")
        widths = (11, 24, 13, 15, 9, 18, 12, 12, 22, 8, 17, 18, 18, 24, 24, 24, 24, 17, 24, 17, 25, 12, 32)
        for index, width in enumerate(widths, start=1):
            sheet.column_dimensions[self._column_letter(index)].width = width
        for row in range(2, sheet.max_row + 1):
            for column in (2, 14, 15, 16, 17):
                sheet.cell(row, column).number_format = "yyyy-mm-dd hh:mm:ss.000"
            for column in (3, 11, 12, 13, 18, 19):
                sheet.cell(row, column).number_format = "0.000"
            sheet.cell(row, 20).number_format = '0.000000 "dBm"'
        if sheet.max_row >= 2:
            sheet.conditional_formatting.add(
                f"T2:T{sheet.max_row}",
                ColorScaleRule(
                    start_type="min",
                    start_color="F8696B",
                    mid_type="percentile",
                    mid_value=50,
                    mid_color="FFEB84",
                    end_type="max",
                    end_color="63BE7B",
                ),
            )

    def _build_events(self, sheet: Any) -> None:
        sheet.sheet_view.showGridLines = False
        sheet.freeze_panes = "A2"
        sheet.append(self.EVENT_HEADERS)
        for index, record in enumerate(self.events, start=1):
            sheet.append(
                (
                    index,
                    self._excel_datetime(record.timestamp),
                    record.elapsed_s,
                    record.event_type,
                    record.details,
                )
            )
        self._style_table_sheet(sheet, len(self.EVENT_HEADERS), "SessionEvents")
        for column, width in zip(("A", "B", "C", "D", "E"), (11, 24, 13, 22, 95)):
            sheet.column_dimensions[column].width = width
        for row in range(2, sheet.max_row + 1):
            sheet.cell(row, 2).number_format = "yyyy-mm-dd hh:mm:ss.000"
            sheet.cell(row, 3).number_format = "0.000"
            sheet.cell(row, 5).alignment = Alignment(wrap_text=True, vertical="top")

    @staticmethod
    def _style_table_sheet(sheet: Any, column_count: int, table_name: str) -> None:
        header_fill = PatternFill("solid", fgColor=NAVY)
        bottom_border = Border(bottom=Side(style="thin", color="8497B0"))
        for cell in sheet[1]:
            cell.fill = header_fill
            cell.font = Font(name="Segoe UI", bold=True, color=WHITE)
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            cell.border = bottom_border
        sheet.row_dimensions[1].height = 34
        if sheet.max_row >= 2:
            reference = f"A1:{SessionExcelReport._column_letter(column_count)}{sheet.max_row}"
            table = Table(displayName=table_name, ref=reference)
            table.tableStyleInfo = TableStyleInfo(
                name="TableStyleMedium2",
                showFirstColumn=False,
                showLastColumn=False,
                showRowStripes=True,
                showColumnStripes=False,
            )
            sheet.add_table(table)
        else:
            sheet.auto_filter.ref = f"A1:{SessionExcelReport._column_letter(column_count)}1"
        sheet.page_setup.orientation = "landscape"
        sheet.page_setup.fitToWidth = 1
        sheet.sheet_properties.pageSetUpPr.fitToPage = True
        sheet.auto_filter.ref = f"A1:{SessionExcelReport._column_letter(column_count)}{sheet.max_row}"

    @staticmethod
    def _excel_datetime(value: datetime) -> datetime:
        return value.replace(tzinfo=None)

    @staticmethod
    def _column_letter(index: int) -> str:
        result = ""
        while index:
            index, remainder = divmod(index - 1, 26)
            result = chr(65 + remainder) + result
        return result
