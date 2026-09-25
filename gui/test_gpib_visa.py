"""Tests for the documented HP E5574A GPIB power workflow."""

from __future__ import annotations

import unittest

from .gpib_visa import E5574AVisa, parse_numeric_response


class FakeInstrument:
    def __init__(self) -> None:
        self.writes: list[str] = []
        self.queries: list[str] = []

    def write(self, command: str) -> None:
        self.writes.append(command)

    def query(self, command: str) -> str:
        self.queries.append(command)
        responses = {
            ":SENS:FUNC?": "POW\n",
            ":SENS1:POW:MEAS:MOD?": "0\n",
            ":SENS1:POW:UNIT?": "0\n",
            ":SENS1:DATA? POW": "-12.3456\n",
        }
        return responses[command]


class E5574APowerTests(unittest.TestCase):
    def test_configures_documented_head_a_absolute_dbm_sequence(self) -> None:
        client = E5574AVisa("GPIB0::24::INSTR")
        instrument = FakeInstrument()
        client.instrument = instrument

        result = client.configure_power_dbm(1)

        self.assertEqual(result, "POW Head 1, absolute, dBm")
        self.assertEqual(
            instrument.writes,
            [
                ":SENS:FUNC MAIN",
                ":SENS:FUNC POW",
                ":SENS1:POW:MEAS:MOD ABS",
                ":SENS1:POW:UNIT DBM",
            ],
        )
        self.assertEqual(
            instrument.queries,
            [
                ":SENS:FUNC?",
                ":SENS1:POW:MEAS:MOD?",
                ":SENS1:POW:UNIT?",
            ],
        )

    def test_reads_numeric_power_and_preserves_raw_response(self) -> None:
        client = E5574AVisa("GPIB0::24::INSTR")
        instrument = FakeInstrument()
        client.instrument = instrument

        reading = client.read_power_dbm(1)

        self.assertAlmostEqual(reading.value_dbm, -12.3456)
        self.assertEqual(reading.raw_response, "-12.3456")
        self.assertEqual(reading.channel, 1)

    def test_rejects_non_numeric_measurement(self) -> None:
        with self.assertRaisesRegex(ValueError, "non-numeric"):
            parse_numeric_response("No valid result possible")


if __name__ == "__main__":
    unittest.main()
