import math
import unittest

try:
    from .protocol import (
        axis_position_mm,
        build_absolute_mm,
        build_accel_mm,
        build_goto,
        build_jog_um,
        build_jog_mm,
        build_move,
        build_relative_mm,
        build_speed_mm,
        format_number,
        micrometres_to_pulses,
        parse_idle_event,
        parse_status,
        pulse_resolution_um,
        sanitize_raw_command,
        steps_to_mm,
        to_half_steps,
    )
except ImportError:
    from protocol import (  # type: ignore
        axis_position_mm,
        build_absolute_mm,
        build_accel_mm,
        build_goto,
        build_jog_um,
        build_jog_mm,
        build_move,
        build_relative_mm,
        build_speed_mm,
        format_number,
        micrometres_to_pulses,
        parse_idle_event,
        parse_status,
        pulse_resolution_um,
        sanitize_raw_command,
        steps_to_mm,
        to_half_steps,
    )


class ProtocolTests(unittest.TestCase):
    def test_legacy_half_step_conversion(self) -> None:
        self.assertEqual(to_half_steps(12.6, "half-steps", 50), 13)
        self.assertEqual(to_half_steps(2.5, "mm", 50), 125)
        self.assertEqual(to_half_steps(-1.0, "mm", 200), -200)
        self.assertAlmostEqual(steps_to_mm(125, 50), 2.5)

    def test_legacy_command_builders(self) -> None:
        self.assertEqual(build_move("x", -40), "MOVE X -40")
        self.assertEqual(build_goto("z", 125), "GOTO Z 125")
        with self.assertRaises(ValueError):
            build_move("Y", 1)

    def test_mm_command_builders(self) -> None:
        self.assertEqual(build_jog_mm("x", -0.5), "MOVE_MM X -0.5")
        self.assertEqual(build_relative_mm(12.2, -3), "MOVE_MM X 12.2 Z -3")
        self.assertEqual(build_absolute_mm(z_mm=8), "GOTO_MM Z 8")
        self.assertEqual(build_speed_mm("X", 20), "SPEED_MM X 20")
        self.assertEqual(build_accel_mm("z", "45.5"), "ACCEL_MM Z 45.5")
        with self.assertRaises(ValueError):
            build_relative_mm()
        self.assertEqual(build_jog_mm("X", 0.05), "MOVE_MM X 0.05")
        self.assertEqual(build_absolute_mm(x_mm=0.15), "GOTO_MM X 0.15")
        with self.assertRaises(ValueError):
            build_jog_mm("X", 0.001)
        with self.assertRaises(ValueError):
            build_speed_mm("X", 0)
        with self.assertRaises(ValueError):
            build_speed_mm("X", 100.1)
        with self.assertRaises(ValueError):
            build_accel_mm("Z", 4.9)
        with self.assertRaises(ValueError):
            build_accel_mm("Z", 200.1)
        self.assertEqual(build_speed_mm("X", 1), "SPEED_MM X 1")
        self.assertEqual(build_speed_mm("Z", 100), "SPEED_MM Z 100")
        self.assertEqual(build_accel_mm("X", 5), "ACCEL_MM X 5")
        self.assertEqual(build_accel_mm("Z", 200), "ACCEL_MM Z 200")

    def test_decimal_format_is_finite_and_ascii(self) -> None:
        self.assertEqual(format_number(-0.0), "0")
        self.assertEqual(format_number(1.23456749), "1.23456749")
        for invalid in (math.inf, -math.inf, math.nan):
            with self.assertRaises(ValueError):
                format_number(invalid)

    def test_exact_micrometre_jogs_map_to_one_step_pulse(self) -> None:
        self.assertAlmostEqual(pulse_resolution_um(160.0), 6.25)
        self.assertAlmostEqual(pulse_resolution_um(91.4285714286), 10.9375)
        self.assertEqual(build_jog_um("X", 6.25, 160.0), "MOVE X 1")
        self.assertEqual(
            build_jog_um("Z", -10.9375, 91.4285714286),
            "MOVE Z -1",
        )
        self.assertEqual(micrometres_to_pulses(50.0, 160.0), 8)
        with self.assertRaisesRegex(ValueError, "whole STEP-pulse"):
            build_jog_um("Z", 10.0, 91.4285714286)

    def test_status_parser_and_mm_position(self) -> None:
        status = parse_status(
            "STATUS ENABLED=1 ESTOP=0 FAULT=NONE BUSY=1 "
            "X=10 XT=20 XH=1 XMM=1.0000 XSPMM=10.0 "
            "Z=-3 ZT=-3 ZH=0 ZMM=-0.3000 ZSPMM=10.0"
        )
        self.assertEqual(status["ENABLED"], "1")
        self.assertEqual(status["FAULT"], "NONE")
        self.assertAlmostEqual(axis_position_mm(status, "X"), 1.0)
        self.assertAlmostEqual(axis_position_mm(status, "Z"), -0.3)
        self.assertEqual(status["XSPMM"], status["ZSPMM"])

    def test_idle_event_parser_returns_controller_pulse_coordinates(self) -> None:
        self.assertEqual(
            parse_idle_event("EVENT IDLE X=10 Z=-6"),
            {"X": 10, "Z": -6},
        )
        with self.assertRaises(ValueError):
            parse_idle_event("EVENT IDLE X=10")

    def test_raw_command_is_one_ascii_line(self) -> None:
        self.assertEqual(sanitize_raw_command("  STATUS  "), "STATUS")
        with self.assertRaises(ValueError):
            sanitize_raw_command("MOVE_MM X 1\nDISABLE")
        with self.assertRaises(ValueError):
            sanitize_raw_command("MOVE_MM X 1 µm")


if __name__ == "__main__":
    unittest.main()
