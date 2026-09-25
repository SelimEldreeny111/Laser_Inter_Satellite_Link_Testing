/*
 * Continuous single-motor test
 * Target MCU: STM32F401RCT6 using the official STM32 Arduino core
 * Driver: DRV8825 configured for 1/32 microstep mode
 *
 * Connections:
 *   PA0  -> DRV8825 STEP
 *   PA1  -> DRV8825 DIR
 *   PB10 -> DRV8825 nENBL (optional if nENBL is already tied to GND)
 *   GND  -> DRV8825 GND and power-supply negative
 *
 * DRV8825 static connections:
 *   MODE0 -> 3.3 V
 *   MODE1 -> GND
 *   MODE2 -> 3.3 V
 *   nRESET and nSLEEP -> 3.3 V
 *
 * MODE2/MODE1/MODE0 = HIGH/LOW/HIGH is one of the DRV8825's valid
 * 1/32-microstep combinations and matches the commissioned stage wiring.
 */

#include <Arduino.h>

static const PinName STEP_PIN = PA_0;
static const PinName DIR_PIN = PA_1;
static const PinName ENABLE_PIN = PB_10;

// At 1/32 microstepping, 6400 pulses equal one complete revolution for a
// 1.8-degree motor. A 313 us interval is approximately 30 rpm.
static const uint32_t STEP_INTERVAL_US = 313;
static const uint32_t STEP_HIGH_US = 5;
static const bool FORWARD_DIRECTION = true;

void setup() {
  pinMode(STEP_PIN, OUTPUT);
  pinMode(DIR_PIN, OUTPUT);
  pinMode(ENABLE_PIN, OUTPUT);

  digitalWrite(STEP_PIN, LOW);
  digitalWrite(DIR_PIN, FORWARD_DIRECTION ? HIGH : LOW);

  // DRV8825 nENBL is active low. Begin disabled, wait for power to settle,
  // then enable the motor outputs.
  digitalWrite(ENABLE_PIN, HIGH);
  delay(1000);
  digitalWrite(ENABLE_PIN, LOW);
  delay(500);
}

void loop() {
  // Every rising edge advances the DRV8825 by one 1/32 microstep. Keep producing
  // pulses forever, so the motor rotates continuously in one direction.
  digitalWrite(STEP_PIN, HIGH);
  delayMicroseconds(STEP_HIGH_US);
  digitalWrite(STEP_PIN, LOW);
  delayMicroseconds(STEP_INTERVAL_US - STEP_HIGH_US);
}
