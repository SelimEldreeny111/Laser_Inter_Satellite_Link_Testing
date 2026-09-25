/*
 * Dual-motor STEP/DIR driver test (DRV8825 or TB6600)
 * Target: STM32F401RCT6 using the official STM32 Arduino core
 *
 * X driver:
 *   PA0  -> STEP
 *   PA1  -> DIR
 *
 * Z driver:
 *   PB0  -> STEP
 *   PB1  -> DIR
 *
 * Shared:
 *   PB10 -> both driver enable interfaces (LOW permits motion with the
 *           recommended TB6600 common-anode transistor interface)
 *
 * Both drivers must be configured for 6400 pulses/revolution. For TB6600
 * modules, use the switch table printed on that exact module.
 *
 * See docs/TB6600_CONNECTION_GUIDE.md before wiring a TB6600. Do not connect
 * a generic opto-isolated TB6600 input directly to a 3.3 V STM32 pin unless
 * the exact module manual explicitly permits it.
 */

#include <Arduino.h>

static const PinName X_STEP_PIN = PA_0;
static const PinName X_DIR_PIN = PA_1;
static const PinName Z_STEP_PIN = PB_0;
static const PinName Z_DIR_PIN = PB_1;
static const PinName ENABLE_PIN = PB_10;

// A 1.8-degree motor has 200 full steps/revolution. At 1/32 microstepping,
// 6400 STEP pulses produce one revolution.
static const uint32_t STEP_PULSES_PER_REVOLUTION = 6400;
static const uint32_t STEP_INTERVAL_US = 313;  // Approximately 30 RPM.
static const uint32_t STEP_HIGH_US = 10;
static const uint32_t PAUSE_MS = 1000;

static void stepBothMotors(uint32_t pulseCount) {
  for (uint32_t pulse = 0; pulse < pulseCount; ++pulse) {
    digitalWrite(X_STEP_PIN, HIGH);
    digitalWrite(Z_STEP_PIN, HIGH);
    delayMicroseconds(STEP_HIGH_US);

    digitalWrite(X_STEP_PIN, LOW);
    digitalWrite(Z_STEP_PIN, LOW);
    delayMicroseconds(STEP_INTERVAL_US - STEP_HIGH_US);
  }
}

static void setBothDirections(bool forward) {
  digitalWrite(X_DIR_PIN, forward ? HIGH : LOW);
  digitalWrite(Z_DIR_PIN, forward ? HIGH : LOW);

  // Conservative direction setup time for TB6600 opto-isolated modules.
  delayMicroseconds(5);
}

void setup() {
  pinMode(X_STEP_PIN, OUTPUT);
  pinMode(X_DIR_PIN, OUTPUT);
  pinMode(Z_STEP_PIN, OUTPUT);
  pinMode(Z_DIR_PIN, OUTPUT);
  pinMode(ENABLE_PIN, OUTPUT);

  digitalWrite(X_STEP_PIN, LOW);
  digitalWrite(Z_STEP_PIN, LOW);
  digitalWrite(X_DIR_PIN, LOW);
  digitalWrite(Z_DIR_PIN, LOW);

  // Keep both drivers disabled while their power settles, then enable them.
  digitalWrite(ENABLE_PIN, HIGH);
  delay(1000);
  digitalWrite(ENABLE_PIN, LOW);
  delay(500);
}

void loop() {
  setBothDirections(true);
  stepBothMotors(STEP_PULSES_PER_REVOLUTION);
  delay(PAUSE_MS);

  setBothDirections(false);
  stepBothMotors(STEP_PULSES_PER_REVOLUTION);
  delay(PAUSE_MS);
}
