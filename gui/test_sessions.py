"""Tests for the reusable long-running session scheduler."""

from __future__ import annotations

import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from .gpib_visa import PowerReading
from .laser_stage_gui import (
    LaserStageApp,
    SerpentineSessionConfig,
    X_SERPENTINE_RECIPE,
    build_serpentine_actions,
)


class FakeSerial:
    def __init__(self) -> None:
        self.is_open = True
        self.port = "TEST"
        self.writes: list[str] = []

    def write(self, payload: bytes) -> int:
        self.writes.append(payload.decode("ascii"))
        return len(payload)

    def close(self) -> None:
        self.is_open = False


class FakeVisa:
    def __init__(self) -> None:
        self.is_open = True

    def configure_power_dbm(self, channel: int = 1) -> str:
        return f"POW Head {channel}, absolute, dBm"

    def read_power_dbm(self, channel: int = 1) -> PowerReading:
        return PowerReading(-12.5, "-12.5", channel)


class SessionRecipeTests(unittest.TestCase):
    def setUp(self) -> None:
        showerror_patcher = patch("gui.laser_stage_gui.messagebox.showerror")
        showwarning_patcher = patch("gui.laser_stage_gui.messagebox.showwarning")
        self.showerror = showerror_patcher.start()
        showwarning_patcher.start()
        self.addCleanup(showerror_patcher.stop)
        self.addCleanup(showwarning_patcher.stop)

    def test_default_two_loop_serpentine_sequence(self) -> None:
        config = SerpentineSessionConfig(
            loops=2,
            z_span_mm=200.0,
            z_step_mm=1.0,
            x_step_mm=1.0,
            step_period_s=1.0,
            speed_mm_s=5.0,
            acceleration_mm_s2=20.0,
        )
        actions = build_serpentine_actions(config)

        self.assertEqual(len(actions), 804)
        self.assertEqual(actions[0].command, "MOVE_MM Z 1")
        self.assertEqual(actions[199].command, "MOVE_MM Z 1")
        self.assertEqual(actions[200].command, "MOVE_MM X 1")
        self.assertEqual(actions[201].command, "MOVE_MM Z -1")
        self.assertEqual(actions[400].command, "MOVE_MM Z -1")
        self.assertEqual(actions[401].command, "MOVE_MM X 1")
        self.assertEqual(actions[402].loop_number, 2)
        self.assertEqual(config.total_x_advance_mm, 4.0)
        self.assertAlmostEqual(config.estimated_duration_s, 1179.64, places=2)

    def test_gui_one_pulse_fine_jogs_use_exact_raw_commands(self) -> None:
        app = LaserStageApp()
        app.withdraw()
        fake_serial = FakeSerial()
        app.serial_port = fake_serial  # type: ignore[assignment]
        app.controller_verified = True

        self.assertEqual(app.x_panel.jog_var.get(), "6.25")
        self.assertEqual(app.z_panel.jog_var.get(), "10.9375")
        app.x_panel.jog(1)
        app.z_panel.jog(-1)

        self.assertEqual(fake_serial.writes, ["MOVE X 1\n", "MOVE Z -1\n"])
        app.serial_port = None
        app.destroy()

    def test_duration_uses_real_motion_time_when_period_is_too_short(self) -> None:
        config = SerpentineSessionConfig(
            loops=1,
            z_span_mm=2.0,
            z_step_mm=1.0,
            x_step_mm=1.0,
            step_period_s=0.2,
            speed_mm_s=5.0,
            acceleration_mm_s2=20.0,
        )

        # A 1 mm triangular move at 20 mm/s^2 takes about 0.447 seconds,
        # so a requested 0.2 second cadence cannot physically be achieved.
        self.assertGreater(config.estimated_duration_s, 2.7)

    def test_high_resolution_session_accepts_one_pulse_axis_increments(self) -> None:
        config = SerpentineSessionConfig(
            loops=1,
            z_span_mm=0.04375,
            z_step_mm=0.0109375,
            x_step_mm=0.00625,
            step_period_s=0.2,
            speed_mm_s=5.0,
            acceleration_mm_s2=20.0,
        )

        actions = build_serpentine_actions(config)

        self.assertEqual(config.steps_per_pass, 4)
        self.assertEqual(actions[0].command, "MOVE_MM Z 0.0109375")
        self.assertEqual(actions[4].command, "MOVE_MM X 0.00625")
        self.assertEqual(actions[5].command, "MOVE_MM Z -0.0109375")

    def test_x_serpentine_is_mirrored_with_z_cross_shifts(self) -> None:
        config = SerpentineSessionConfig(
            loops=1,
            z_span_mm=2.0,
            z_step_mm=1.0,
            x_step_mm=1.0,
            step_period_s=1.0,
            speed_mm_s=5.0,
            acceleration_mm_s2=20.0,
            scan_axis="X",
        )

        actions = build_serpentine_actions(config)

        self.assertEqual(
            [action.command for action in actions],
            [
                "MOVE_MM X 1",
                "MOVE_MM X 1",
                "MOVE_MM Z 1",
                "MOVE_MM X -1",
                "MOVE_MM X -1",
                "MOVE_MM Z 1",
            ],
        )
        self.assertEqual(config.cross_axis, "Z")
        self.assertEqual(config.total_cross_advance_mm, 2.0)
        self.assertEqual(config.total_x_advance_mm, 0.0)

    def test_recipe_rejects_non_divisible_span(self) -> None:
        config = SerpentineSessionConfig(
            loops=1,
            z_span_mm=200.0,
            z_step_mm=3.0,
            x_step_mm=1.0,
            step_period_s=1.0,
            speed_mm_s=5.0,
            acceleration_mm_s2=20.0,
        )
        with self.assertRaisesRegex(ValueError, "exact multiple"):
            config.validate()

    def test_app_starts_and_advances_small_session_without_blocking(self) -> None:
        app = LaserStageApp()
        app.withdraw()
        app.session_report_directory = Path.cwd() / "tmp"
        app.visa_instrument = FakeVisa()  # type: ignore[assignment]
        app.visa_idn_var.set("HEWLETT-PACKARD,E5574A,0,0")
        fake_serial = FakeSerial()
        app.serial_port = fake_serial  # type: ignore[assignment]
        app.controller_verified = True
        app.current_status = {
            "ENABLED": "1",
            "BUSY": "0",
            "ESTOP": "0",
            "FAULT": "NONE",
            "XH": "1",
            "ZH": "1",
            "XMM": "0.0000",
            "ZMM": "0.0000",
            "XSPMM": "10.0",
            "ZSPMM": "5.7143",
        }
        app.session_loops_var.set("1")
        app.session_z_span_var.set("2")
        app.session_z_step_var.set("1")
        app.session_x_step_var.set("1")
        app.session_period_var.set("1")

        with (
            patch("gui.laser_stage_gui.messagebox.askokcancel", return_value=True),
            patch.object(
                app,
                "_prepare_session_power_acquisition",
                side_effect=app._start_session_motion,
            ),
        ):
            app.start_session()

        self.assertTrue(app.session_running)
        self.assertEqual(len(app.session_actions), 6)
        self.assertEqual(fake_serial.writes[0], "SPEED_MM ALL 5\n")
        self.assertEqual(fake_serial.writes[1], "ACCEL_MM ALL 20\n")

        app._dispatch_session_action()
        self.assertTrue(app.session_waiting_for_idle)
        self.assertEqual(fake_serial.writes[-1], "MOVE_MM Z 1\n")
        app._complete_session_action("EVENT IDLE X=0 Z=6")
        self.assertTrue(app.session_waiting_for_measurement)
        app.session_measurement_started_at = time.monotonic()
        app.session_measurement_started_wallclock = datetime.now()
        app._complete_session_power_measurement(PowerReading(-12.5, "-12.5", 1))
        self.assertEqual(app.session_action_index, 1)
        self.assertAlmostEqual(
            app.session_report.measurements[0].expected_z_mm,
            6.0 / 5.7143,
            places=6,
        )

        app.toggle_session_pause()
        self.assertTrue(app.session_paused)
        app.toggle_session_pause()
        self.assertFalse(app.session_paused)
        app.stop_session()
        self.assertFalse(app.session_running)
        self.assertEqual(app.session_state_var.get(), "Stopped")
        self.assertEqual(fake_serial.writes[-1], "STOP\n")

        app.serial_port = None
        app.destroy()

    def test_app_selects_x_recipe_and_dispatches_x_first(self) -> None:
        app = LaserStageApp()
        app.withdraw()
        app.session_report_directory = Path.cwd() / "tmp"
        app.visa_instrument = FakeVisa()  # type: ignore[assignment]
        app.visa_idn_var.set("HEWLETT-PACKARD,E5574A,0,0")
        fake_serial = FakeSerial()
        app.serial_port = fake_serial  # type: ignore[assignment]
        app.controller_verified = True
        app.current_status = {
            "ENABLED": "1",
            "BUSY": "0",
            "ESTOP": "0",
            "FAULT": "NONE",
            "XH": "1",
            "ZH": "1",
            "XMM": "0.0000",
            "ZMM": "0.0000",
        }
        app.session_recipe_var.set(X_SERPENTINE_RECIPE)
        app.session_loops_var.set("1")
        app.session_z_span_var.set("2")
        app.session_z_step_var.set("1")
        app.session_x_step_var.set("1")

        with (
            patch("gui.laser_stage_gui.messagebox.askokcancel", return_value=True),
            patch.object(
                app,
                "_prepare_session_power_acquisition",
                side_effect=app._start_session_motion,
            ),
        ):
            app.start_session()

        self.assertTrue(app.session_running)
        self.assertEqual(app.session_config.scan_axis, "X")
        self.assertIn("total Z advance", app.session_time_var.get())
        app._dispatch_session_action()
        self.assertEqual(fake_serial.writes[-1], "MOVE_MM X 1\n")

        app.stop_session()
        app.serial_port = None
        app.destroy()

    def test_x_recipe_rejects_insufficient_z_shift_travel(self) -> None:
        app = LaserStageApp()
        app.withdraw()
        app.session_report_directory = Path.cwd() / "tmp"
        app.visa_instrument = FakeVisa()  # type: ignore[assignment]
        fake_serial = FakeSerial()
        app.serial_port = fake_serial  # type: ignore[assignment]
        app.controller_verified = True
        app.current_status = {
            "ENABLED": "1",
            "BUSY": "0",
            "ESTOP": "0",
            "FAULT": "NONE",
            "XH": "1",
            "ZH": "1",
            "XMM": "0.0000",
            "ZMM": "199.0000",
        }
        app.session_recipe_var.set(X_SERPENTINE_RECIPE)
        app.session_z_span_var.set("2")
        app.session_z_step_var.set("1")
        app.session_x_step_var.set("1")

        with patch("gui.laser_stage_gui.messagebox.showerror") as showerror:
            app.start_session()

        self.assertFalse(app.session_running)
        self.assertEqual(showerror.call_args.args[0], "Insufficient Z travel")
        self.assertEqual(fake_serial.writes, [])

        app.serial_port = None
        app.destroy()


if __name__ == "__main__":
    unittest.main()
