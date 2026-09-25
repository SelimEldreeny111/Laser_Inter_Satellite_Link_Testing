/*
 * Laser X-Z positioning stage controller
 * Target: STM32F401RCT6 with the official STM32 Arduino core
 *
 * The public motion interface uses millimetres. Internally, positions remain
 * integer driver pulses for deterministic motion. This high-resolution build
 * requires the DRV8825 and TB6600 to be configured for 6400 STEP pulses per
 * motor revolution (1/32 microstepping) before motion is enabled.
 *
 * UART protocol: 115200 baud, 8-N-1, newline-terminated ASCII commands.
 * See docs/SERIAL_PROTOCOL.md for the complete command set.
 */

#include <Arduino.h>
#include <ctype.h>
#include <limits.h>
#include <math.h>
#include <stdlib.h>
#include <string.h>

// ---------------------------------------------------------------------------
// Pin configuration - edit this block if the PCB uses different pins.
// ---------------------------------------------------------------------------

static const PinName X_STEP_PIN = PA_0;
static const PinName X_DIR_PIN = PA_1;
static const PinName Z_STEP_PIN = PB_0;
static const PinName Z_DIR_PIN = PB_1;

// Both drivers' enable interfaces are controlled by this one output.
// With the recommended TB6600 common-anode transistor interface, LOW permits
// motion and HIGH asserts the modules' disable/offline optocouplers.
static const PinName DRIVERS_ENABLE_PIN = PB_10;

// No limit, hardware E-stop, or nFAULT pins are used in this hardware version.
// The only additional MCU pins used are USART1 PA9/PA10 for the desktop GUI.

// USART1: constructor order is RX, TX.
HardwareSerial MotionSerial(PA_10, PA_9);

// STEP/DIR driver logic. Change either value if the driver wiring is inverted.
static const bool X_POSITIVE_DIR_LEVEL = LOW;
static const bool Z_POSITIVE_DIR_LEVEL = LOW;
static const bool DRIVER_ENABLE_ACTIVE_LEVEL = LOW;

// Conservative timing for opto-isolated TB6600 modules. It also remains
// compatible with the original DRV8825 carriers and adds negligible blocking
// time at this stage's maximum configured linear speed.
static const uint32_t STEP_PULSE_WIDTH_US = 10;
static const uint32_t DIR_SETUP_TIME_US = 5;

// High-resolution mechanical calibration. A 1.8-degree motor at 1/32
// microstepping needs 6400 STEP pulses/revolution. X uses a 20-tooth GT2,
// 2 mm-pitch pulley: 6400 / 40 = 160 pulses/mm = 6.25 um/pulse.
//
// Z preserves the proven empirical correction from the previous 400-pulse
// configuration. The measured scale was 10 * 20 / 35 = 5.7142857 pulses/mm;
// increasing 400 -> 6400 pulses/revolution multiplies it by 16:
// 91.428571 pulses/mm = 10.9375 um/pulse.
// Recheck Z with a long, slow measurement after changing the TB6600 switches.
static const float X_STEP_PULSES_PER_MM = 160.0f;
static const float Z_STEP_PULSES_PER_MM = 91.428571f;

// Conservative commissioning defaults in physical units.
static const float X_DEFAULT_SPEED_MM_S = 20.0f;
static const float Z_DEFAULT_SPEED_MM_S = 20.0f;
static const float X_DEFAULT_ACCEL_MM_S2 = 40.0f;
static const float Z_DEFAULT_ACCEL_MM_S2 = 40.0f;

// Conservative operating envelope for this 12 V GT2 stage. The supplied
// motor curves show the expected high-speed torque reduction, so the normal
// millimetre interface is intentionally limited well below the driver's
// electrical STEP ceiling. These are commissioning limits, not a guarantee
// against stalls with every payload/current setting.
static const float MIN_OPERATING_SPEED_MM_S = 1.0f;
static const float MAX_OPERATING_SPEED_MM_S = 100.0f;
static const float MIN_OPERATING_ACCEL_MM_S2 = 5.0f;
static const float MAX_OPERATING_ACCEL_MM_S2 = 200.0f;

// The StepperAxis constructor needs safe non-zero values; ControlledAxis
// replaces them with the calibrated per-axis defaults during setup.
static const float DEFAULT_MAX_SPEED = 200.0f;      // STEP pulses/s
static const float DEFAULT_ACCELERATION = 400.0f;   // STEP pulses/s^2
// At 160 pulses/mm, the 100 mm/s operating limit requires 16000 pulses/s.
// Keep margin below the 10 us HIGH + 10 us LOW pulse-timing ceiling.
static const float MAX_CONFIGURED_SPEED = 20000.0f;
static const float MAX_CONFIGURED_ACCELERATION = 100000.0f;

static const size_t RX_BUFFER_SIZE = 160;
static char rxBuffer[RX_BUFFER_SIZE];
static size_t rxLength = 0;

// ---------------------------------------------------------------------------
// Minimal acceleration-controlled step generator.
// The recurrence is the standard real-time stepper ramp used by many motion
// controllers. It avoids delays except for the short STEP pulse itself, so the
// two axes can move at the same time while UART commands remain responsive.
// ---------------------------------------------------------------------------

class StepperAxis {
 public:
  StepperAxis(PinName stepPin, PinName dirPin, bool positiveDirLevel)
      : stepPin_(stepPin),
        dirPin_(dirPin),
        positiveDirLevel_(positiveDirLevel),
        currentPosition_(0),
        targetPosition_(0),
        maxSpeed_(DEFAULT_MAX_SPEED),
        acceleration_(DEFAULT_ACCELERATION),
        speed_(0.0f),
        stepIntervalUs_(0),
        lastStepTimeUs_(0),
        n_(0),
        c0_(0.0f),
        cn_(0.0f),
        cmin_(0.0f),
        directionPositive_(true) {
    recalculateRampConstants();
  }

  void begin() {
    pinMode(stepPin_, OUTPUT);
    pinMode(dirPin_, OUTPUT);
    digitalWrite(stepPin_, LOW);
    writeDirection(true);
  }

  long currentPosition() const { return currentPosition_; }
  long targetPosition() const { return targetPosition_; }
  long distanceToGo() const { return targetPosition_ - currentPosition_; }
  float maxSpeed() const { return maxSpeed_; }
  float acceleration() const { return acceleration_; }
  float speed() const { return speed_; }
  bool directionPositive() const { return directionPositive_; }
  bool isRunning() const { return stepIntervalUs_ != 0; }

  void setCurrentPosition(long position) {
    currentPosition_ = position;
    targetPosition_ = position;
    speed_ = 0.0f;
    stepIntervalUs_ = 0;
    n_ = 0;
  }

  void setMaxSpeed(float speed) {
    if (speed < 1.0f) {
      speed = 1.0f;
    }
    maxSpeed_ = speed;
    cmin_ = 1000000.0f / maxSpeed_;
    if (n_ > 0) {
      n_ = static_cast<long>((speed_ * speed_) / (2.0f * acceleration_));
      computeNewSpeed();
    }
  }

  void setAcceleration(float acceleration) {
    if (acceleration < 1.0f) {
      acceleration = 1.0f;
    }
    if (acceleration_ != acceleration) {
      n_ = static_cast<long>(n_ * (acceleration_ / acceleration));
      acceleration_ = acceleration;
      recalculateRampConstants();
      computeNewSpeed();
    }
  }

  void moveTo(long absolutePosition) {
    if (targetPosition_ != absolutePosition) {
      targetPosition_ = absolutePosition;
      computeNewSpeed();
    }
  }

  void move(long relativeDistance) { moveTo(currentPosition_ + relativeDistance); }

  void stopImmediate() {
    targetPosition_ = currentPosition_;
    speed_ = 0.0f;
    stepIntervalUs_ = 0;
    n_ = 0;
    digitalWrite(stepPin_, LOW);
  }

  bool stepDue(uint32_t nowUs) const {
    return stepIntervalUs_ != 0 &&
           static_cast<uint32_t>(nowUs - lastStepTimeUs_) >= stepIntervalUs_;
  }

  void performDueStep(uint32_t nowUs) {
    writeDirection(directionPositive_);
    delayMicroseconds(DIR_SETUP_TIME_US);
    digitalWrite(stepPin_, HIGH);
    delayMicroseconds(STEP_PULSE_WIDTH_US);
    digitalWrite(stepPin_, LOW);

    currentPosition_ += directionPositive_ ? 1L : -1L;
    lastStepTimeUs_ = nowUs;
    computeNewSpeed();
  }

 private:
  PinName stepPin_;
  PinName dirPin_;
  bool positiveDirLevel_;
  long currentPosition_;
  long targetPosition_;
  float maxSpeed_;
  float acceleration_;
  float speed_;
  uint32_t stepIntervalUs_;
  uint32_t lastStepTimeUs_;
  long n_;
  float c0_;
  float cn_;
  float cmin_;
  bool directionPositive_;

  void writeDirection(bool positive) {
    const bool outputLevel = positive ? positiveDirLevel_ : !positiveDirLevel_;
    digitalWrite(dirPin_, outputLevel ? HIGH : LOW);
  }

  void recalculateRampConstants() {
    c0_ = 0.676f * sqrtf(2.0f / acceleration_) * 1000000.0f;
    cmin_ = 1000000.0f / maxSpeed_;
  }

  uint32_t computeNewSpeed() {
    const long distance = distanceToGo();
    const long stepsToStop =
        static_cast<long>((speed_ * speed_) / (2.0f * acceleration_));

    if (distance == 0 && stepsToStop <= 1) {
      stepIntervalUs_ = 0;
      speed_ = 0.0f;
      n_ = 0;
      return stepIntervalUs_;
    }

    if (distance > 0) {
      if (n_ > 0) {
        if (stepsToStop >= distance || !directionPositive_) {
          n_ = -stepsToStop;
        }
      } else if (n_ < 0) {
        if (stepsToStop < distance && directionPositive_) {
          n_ = -n_;
        }
      }
    } else if (distance < 0) {
      if (n_ > 0) {
        if (stepsToStop >= -distance || directionPositive_) {
          n_ = -stepsToStop;
        }
      } else if (n_ < 0) {
        if (stepsToStop < -distance && !directionPositive_) {
          n_ = -n_;
        }
      }
    }

    if (n_ == 0) {
      cn_ = c0_;
      directionPositive_ = distance > 0;
    } else {
      cn_ = cn_ - ((2.0f * cn_) / ((4.0f * n_) + 1.0f));
      if (cn_ < cmin_) {
        cn_ = cmin_;
      }
    }

    ++n_;
    stepIntervalUs_ = static_cast<uint32_t>(cn_);
    if (stepIntervalUs_ < STEP_PULSE_WIDTH_US + 2U) {
      stepIntervalUs_ = STEP_PULSE_WIDTH_US + 2U;
    }

    speed_ = 1000000.0f / cn_;
    if (!directionPositive_) {
      speed_ = -speed_;
    }
    return stepIntervalUs_;
  }
};

struct ControlledAxis {
  StepperAxis motor;
  float pulsesPerMm;
  float requestedMaxSpeed;
  float requestedAcceleration;
  bool referenceSet;

  ControlledAxis(PinName stepPin,
                 PinName dirPin,
                 bool positiveDirLevel,
                  float calibratedPulsesPerMm,
                 float defaultSpeedMmS,
                 float defaultAccelerationMmS2)
      : motor(stepPin, dirPin, positiveDirLevel),
        pulsesPerMm(calibratedPulsesPerMm),
        requestedMaxSpeed(defaultSpeedMmS * calibratedPulsesPerMm),
        requestedAcceleration(defaultAccelerationMmS2 * calibratedPulsesPerMm),
        referenceSet(false) {}
};

ControlledAxis xAxis(X_STEP_PIN,
                     X_DIR_PIN,
                     X_POSITIVE_DIR_LEVEL,
                     X_STEP_PULSES_PER_MM,
                     X_DEFAULT_SPEED_MM_S,
                     X_DEFAULT_ACCEL_MM_S2);
ControlledAxis zAxis(Z_STEP_PIN,
                     Z_DIR_PIN,
                     Z_POSITIVE_DIR_LEVEL,
                     Z_STEP_PULSES_PER_MM,
                     Z_DEFAULT_SPEED_MM_S,
                     Z_DEFAULT_ACCEL_MM_S2);

static bool driversEnabled = false;
static bool estopLatched = false;
static const char *faultCode = "NONE";
static bool wasBusy = false;

static bool axisBusy(const ControlledAxis &axis) {
  return axis.motor.isRunning();
}

static bool systemBusy() { return axisBusy(xAxis) || axisBusy(zAxis); }

static void setDriversEnabled(bool enabled) {
  driversEnabled = enabled;
  const bool pinLevel = enabled ? DRIVER_ENABLE_ACTIVE_LEVEL
                                : !DRIVER_ENABLE_ACTIVE_LEVEL;
  digitalWrite(DRIVERS_ENABLE_PIN, pinLevel ? HIGH : LOW);
}

static void restoreAxisDynamics(ControlledAxis &axis) {
  axis.motor.setMaxSpeed(axis.requestedMaxSpeed);
  axis.motor.setAcceleration(axis.requestedAcceleration);
}

static void stopAxis(ControlledAxis &axis) {
  axis.motor.stopImmediate();
  restoreAxisDynamics(axis);
}

static void stopAllMotion() {
  stopAxis(xAxis);
  stopAxis(zAxis);
}

static float pulsesToMm(const ControlledAxis &axis, long pulses) {
  return static_cast<float>(pulses) / axis.pulsesPerMm;
}

static bool mmToPulses(const ControlledAxis &axis,
                       float millimetres,
                       long &pulses) {
  const double converted =
      static_cast<double>(millimetres) * axis.pulsesPerMm;
  if (!isfinite(converted) || converted > static_cast<double>(LONG_MAX) ||
      converted < static_cast<double>(LONG_MIN)) {
    return false;
  }
  pulses = static_cast<long>(lround(converted));
  return true;
}

static void latchEstop(const char *source) {
  if (!estopLatched) {
    estopLatched = true;
    stopAllMotion();
    setDriversEnabled(false);
    faultCode = "ESTOP";
    MotionSerial.print("EVENT ESTOP SOURCE=");
    MotionSerial.println(source);
  }
}

static void serviceAxis(ControlledAxis &axis, uint32_t nowUs) {
  if (axis.motor.stepDue(nowUs)) {
    axis.motor.performDueStep(nowUs);
  }
}

static ControlledAxis *findAxis(const char *name) {
  if (strcmp(name, "X") == 0) {
    return &xAxis;
  }
  if (strcmp(name, "Z") == 0) {
    return &zAxis;
  }
  return nullptr;
}

static bool parseLongStrict(const char *text, long &value) {
  if (text == nullptr || *text == '\0') {
    return false;
  }
  char *end = nullptr;
  value = strtol(text, &end, 10);
  return end != text && *end == '\0';
}

static bool parseFloatStrict(const char *text, float &value) {
  if (text == nullptr || *text == '\0') {
    return false;
  }
  char *end = nullptr;
  value = strtof(text, &end);
  return end != text && *end == '\0' && isfinite(value);
}

static void uppercaseInPlace(char *text) {
  while (*text != '\0') {
    *text = static_cast<char>(toupper(static_cast<unsigned char>(*text)));
    ++text;
  }
}

static void replyOk(const char *detail) {
  MotionSerial.print("OK");
  if (detail != nullptr && *detail != '\0') {
    MotionSerial.print(' ');
    MotionSerial.print(detail);
  }
  MotionSerial.println();
}

static void replyError(const char *code, const char *detail) {
  MotionSerial.print("ERR ");
  MotionSerial.print(code);
  if (detail != nullptr && *detail != '\0') {
    MotionSerial.print(' ');
    MotionSerial.print(detail);
  }
  MotionSerial.println();
}

static void sendStatus() {
  MotionSerial.print("STATUS ENABLED=");
  MotionSerial.print(driversEnabled ? 1 : 0);
  MotionSerial.print(" ESTOP=");
  MotionSerial.print(estopLatched ? 1 : 0);
  MotionSerial.print(" FAULT=");
  MotionSerial.print(faultCode);
  MotionSerial.print(" BUSY=");
  MotionSerial.print(systemBusy() ? 1 : 0);

  MotionSerial.print(" X=");
  MotionSerial.print(xAxis.motor.currentPosition());
  MotionSerial.print(" XT=");
  MotionSerial.print(xAxis.motor.targetPosition());
  MotionSerial.print(" XH=");
  MotionSerial.print(xAxis.referenceSet ? 1 : 0);
  MotionSerial.print(" XMM=");
  MotionSerial.print(pulsesToMm(xAxis, xAxis.motor.currentPosition()), 6);
  MotionSerial.print(" XTMM=");
  MotionSerial.print(pulsesToMm(xAxis, xAxis.motor.targetPosition()), 6);
  MotionSerial.print(" XSPMM=");
  MotionSerial.print(xAxis.pulsesPerMm, 6);
  MotionSerial.print(" XVMM=");
  MotionSerial.print(xAxis.requestedMaxSpeed / xAxis.pulsesPerMm, 3);
  MotionSerial.print(" XAMM=");
  MotionSerial.print(xAxis.requestedAcceleration / xAxis.pulsesPerMm, 3);

  MotionSerial.print(" Z=");
  MotionSerial.print(zAxis.motor.currentPosition());
  MotionSerial.print(" ZT=");
  MotionSerial.print(zAxis.motor.targetPosition());
  MotionSerial.print(" ZH=");
  MotionSerial.print(zAxis.referenceSet ? 1 : 0);
  MotionSerial.print(" ZMM=");
  MotionSerial.print(pulsesToMm(zAxis, zAxis.motor.currentPosition()), 6);
  MotionSerial.print(" ZTMM=");
  MotionSerial.print(pulsesToMm(zAxis, zAxis.motor.targetPosition()), 6);
  MotionSerial.print(" ZSPMM=");
  MotionSerial.print(zAxis.pulsesPerMm, 6);
  MotionSerial.print(" ZVMM=");
  MotionSerial.print(zAxis.requestedMaxSpeed / zAxis.pulsesPerMm, 3);
  MotionSerial.print(" ZAMM=");
  MotionSerial.println(zAxis.requestedAcceleration / zAxis.pulsesPerMm, 3);
}

static bool motionCommandAllowed() {
  if (estopLatched || strcmp(faultCode, "NONE") != 0) {
    replyError("FAULTED", faultCode);
    return false;
  }
  if (!driversEnabled) {
    replyError("DISABLED", "send ENABLE first");
    return false;
  }
  return true;
}

static void processMillimetreMove(char **tokens,
                                  size_t tokenCount,
                                  bool absoluteMove) {
  const char *command = absoluteMove ? "GOTO_MM" : "MOVE_MM";
  if (tokenCount < 3 || ((tokenCount - 1U) % 2U) != 0U) {
    replyError("SYNTAX",
               absoluteMove ? "GOTO_MM X <mm> [Z <mm>]"
                            : "MOVE_MM X <mm> [Z <mm>]");
    return;
  }

  bool setX = false;
  bool setZ = false;
  long targetX = xAxis.motor.targetPosition();
  long targetZ = zAxis.motor.targetPosition();

  for (size_t i = 1; i + 1 < tokenCount; i += 2) {
    ControlledAxis *axis = findAxis(tokens[i]);
    bool *axisWasSet = nullptr;
    long *target = nullptr;
    if (axis == &xAxis) {
      axisWasSet = &setX;
      target = &targetX;
    } else if (axis == &zAxis) {
      axisWasSet = &setZ;
      target = &targetZ;
    } else {
      replyError("AXIS", tokens[i]);
      return;
    }
    if (*axisWasSet) {
      replyError("AXIS", "duplicate axis");
      return;
    }

    float millimetres = 0.0f;
    long converted = 0;
    if (!parseFloatStrict(tokens[i + 1], millimetres)) {
      replyError("NUMBER", tokens[i + 1]);
      return;
    }
    if (!mmToPulses(*axis, millimetres, converted)) {
      replyError("RANGE", "millimetre value is outside position range");
      return;
    }
    // A millimetre value does not always map to an integer STEP pulse after
    // empirical calibration. mmToPulses() rounds to the nearest pulse so
    // commands such as MOVE_MM Z 10 remain usable with fractional scales.
    if (millimetres != 0.0f && converted == 0L) {
      replyError("RESOLUTION", "distance is smaller than one STEP pulse");
      return;
    }

    if (absoluteMove) {
      *target = converted;
    } else {
      const int64_t relativeTarget =
          static_cast<int64_t>(axis->motor.currentPosition()) + converted;
      if (relativeTarget > LONG_MAX || relativeTarget < LONG_MIN) {
        replyError("RANGE", "relative target is outside position range");
        return;
      }
      *target = static_cast<long>(relativeTarget);
    }
    *axisWasSet = true;
  }

  if (!motionCommandAllowed()) {
    return;
  }
  if (setX) {
    xAxis.motor.moveTo(targetX);
  }
  if (setZ) {
    zAxis.motor.moveTo(targetZ);
  }
  replyOk(command);
}

static void processGoto(char **tokens, size_t tokenCount) {
  if (tokenCount < 3 || ((tokenCount - 1U) % 2U) != 0U) {
    replyError("SYNTAX", "GOTO X <position> [Z <position>]");
    return;
  }

  bool setX = false;
  bool setZ = false;
  long targetX = xAxis.motor.targetPosition();
  long targetZ = zAxis.motor.targetPosition();

  for (size_t i = 1; i + 1 < tokenCount; i += 2) {
    long value = 0;
    if (!parseLongStrict(tokens[i + 1], value)) {
      replyError("NUMBER", tokens[i + 1]);
      return;
    }
    if (strcmp(tokens[i], "X") == 0 && !setX) {
      setX = true;
      targetX = value;
    } else if (strcmp(tokens[i], "Z") == 0 && !setZ) {
      setZ = true;
      targetZ = value;
    } else {
      replyError("AXIS", tokens[i]);
      return;
    }
  }

  if (!motionCommandAllowed()) {
    return;
  }
  if (setX) {
    xAxis.motor.moveTo(targetX);
  }
  if (setZ) {
    zAxis.motor.moveTo(targetZ);
  }
  replyOk("GOTO");
}

static void processMillimetreDynamics(char **tokens,
                                      size_t tokenCount,
                                      bool speedCommand) {
  const char *command = speedCommand ? "SPEED_MM" : "ACCEL_MM";
  if (tokenCount != 3) {
    replyError("SYNTAX",
               speedCommand ? "SPEED_MM X|Z|ALL <mm_per_second>"
                            : "ACCEL_MM X|Z|ALL <mm_per_second_squared>");
    return;
  }

  float physicalValue = 0.0f;
  if (!parseFloatStrict(tokens[2], physicalValue) || physicalValue <= 0.0f) {
    replyError("RANGE", "value must be positive");
    return;
  }

  const float minimumPhysicalValue =
      speedCommand ? MIN_OPERATING_SPEED_MM_S : MIN_OPERATING_ACCEL_MM_S2;
  const float maximumPhysicalValue =
      speedCommand ? MAX_OPERATING_SPEED_MM_S : MAX_OPERATING_ACCEL_MM_S2;
  if (physicalValue < minimumPhysicalValue ||
      physicalValue > maximumPhysicalValue) {
    replyError("RANGE",
               speedCommand ? "speed must be 1..100 mm/s"
                            : "acceleration must be 5..200 mm/s^2");
    return;
  }

  const bool allAxes = strcmp(tokens[1], "ALL") == 0;
  ControlledAxis *axis = findAxis(tokens[1]);
  if (!allAxes && axis == nullptr) {
    replyError("AXIS", tokens[1]);
    return;
  }

  ControlledAxis *axes[2] = {&xAxis, &zAxis};
  const float halfStepLimit =
      speedCommand ? MAX_CONFIGURED_SPEED : MAX_CONFIGURED_ACCELERATION;
  for (size_t i = 0; i < 2; ++i) {
    if (!allAxes && axes[i] != axis) {
      continue;
    }
    const float pulseValue = physicalValue * axes[i]->pulsesPerMm;
    if (pulseValue < 1.0f) {
      replyError("RESOLUTION", "value is below one pulse per second");
      return;
    }
    if (pulseValue > halfStepLimit) {
      replyError("RANGE", "value exceeds the calibrated pulse-rate limit");
      return;
    }
  }

  for (size_t i = 0; i < 2; ++i) {
    ControlledAxis *selected = axes[i];
    if (!allAxes && selected != axis) {
      continue;
    }
    const float pulseValue = physicalValue * selected->pulsesPerMm;
    if (speedCommand) {
      selected->requestedMaxSpeed = pulseValue;
      selected->motor.setMaxSpeed(pulseValue);
    } else {
      selected->requestedAcceleration = pulseValue;
      selected->motor.setAcceleration(pulseValue);
    }
  }
  replyOk(command);
}

static void processCommand(char *line) {
  while (*line == ' ' || *line == '\t') {
    ++line;
  }
  if (*line == '\0') {
    return;
  }

  uppercaseInPlace(line);
  char *tokens[10] = {nullptr};
  size_t tokenCount = 0;
  char *save = nullptr;
  char *token = strtok_r(line, " ,\t", &save);
  while (token != nullptr && tokenCount < 10) {
    tokens[tokenCount++] = token;
    token = strtok_r(nullptr, " ,\t", &save);
  }
  if (token != nullptr) {
    replyError("SYNTAX", "too many tokens");
    return;
  }

  const char *command = tokens[0];

  if (strcmp(command, "PING") == 0) {
    MotionSerial.println("OK PONG LASER_XZ 2.5");
    return;
  }
  if (strcmp(command, "STATUS") == 0) {
    sendStatus();
    return;
  }
  if (strcmp(command, "HELP") == 0) {
    MotionSerial.println(
        "OK COMMANDS=PING,STATUS,ENABLE,DISABLE,STOP,ESTOP,CLEAR,"
        "MOVE_MM,GOTO_MM,SPEED_MM,ACCEL_MM,ZERO,MOVE,GOTO,SPEED,ACCEL");
    return;
  }
  if (strcmp(command, "ENABLE") == 0) {
    if (estopLatched || strcmp(faultCode, "NONE") != 0) {
      replyError("FAULTED", faultCode);
      return;
    }
    setDriversEnabled(true);
    replyOk("ENABLE");
    return;
  }
  if (strcmp(command, "STOP") == 0) {
    stopAllMotion();
    replyOk("STOP");
    return;
  }
  if (strcmp(command, "DISABLE") == 0) {
    stopAllMotion();
    setDriversEnabled(false);
    xAxis.referenceSet = false;
    zAxis.referenceSet = false;
    replyOk("DISABLE");
    return;
  }
  if (strcmp(command, "ESTOP") == 0) {
    latchEstop("SERIAL");
    replyOk("ESTOP");
    return;
  }
  if (strcmp(command, "CLEAR") == 0) {
    stopAllMotion();
    estopLatched = false;
    faultCode = "NONE";
    replyOk("CLEAR");
    return;
  }
  if (strcmp(command, "MOVE") == 0) {
    if (tokenCount != 3) {
      replyError("SYNTAX", "MOVE X|Z <relative_half_steps>");
      return;
    }
    ControlledAxis *axis = findAxis(tokens[1]);
    long distance = 0;
    if (axis == nullptr) {
      replyError("AXIS", tokens[1]);
      return;
    }
    if (!parseLongStrict(tokens[2], distance)) {
      replyError("NUMBER", tokens[2]);
      return;
    }
    if (!motionCommandAllowed()) {
      return;
    }
    axis->motor.move(distance);
    replyOk("MOVE");
    return;
  }
  if (strcmp(command, "MOVE_MM") == 0) {
    processMillimetreMove(tokens, tokenCount, false);
    return;
  }
  if (strcmp(command, "GOTO_MM") == 0) {
    processMillimetreMove(tokens, tokenCount, true);
    return;
  }
  if (strcmp(command, "GOTO") == 0) {
    processGoto(tokens, tokenCount);
    return;
  }
  if (strcmp(command, "HOME") == 0) {
    if (tokenCount != 2) {
      replyError("SYNTAX", "HOME X|Z|ALL");
      return;
    }
    replyError("UNSUPPORTED", "this controller has no homing inputs");
    return;
  }
  if (strcmp(command, "ZERO") == 0) {
    if (tokenCount != 2 || systemBusy()) {
      replyError("SYNTAX", "ZERO X|Z|ALL (only while idle)");
      return;
    }
    if (strcmp(tokens[1], "X") == 0) {
      xAxis.motor.setCurrentPosition(0);
      xAxis.referenceSet = true;
    } else if (strcmp(tokens[1], "Z") == 0) {
      zAxis.motor.setCurrentPosition(0);
      zAxis.referenceSet = true;
    } else if (strcmp(tokens[1], "ALL") == 0) {
      xAxis.motor.setCurrentPosition(0);
      zAxis.motor.setCurrentPosition(0);
      xAxis.referenceSet = true;
      zAxis.referenceSet = true;
    } else {
      replyError("AXIS", tokens[1]);
      return;
    }
    replyOk("ZERO");
    return;
  }
  if (strcmp(command, "SPEED") == 0 || strcmp(command, "ACCEL") == 0) {
    if (tokenCount != 3) {
      replyError("SYNTAX", "SPEED|ACCEL X|Z|ALL <positive_value>");
      return;
    }
    float value = 0.0f;
    const bool isSpeedCommand = strcmp(command, "SPEED") == 0;
    const float maximumValue =
        isSpeedCommand ? MAX_CONFIGURED_SPEED : MAX_CONFIGURED_ACCELERATION;
    if (!parseFloatStrict(tokens[2], value) || value < 1.0f ||
        value > maximumValue) {
      replyError("RANGE",
                  isSpeedCommand ? "speed must be 1..20000"
                                : "acceleration must be 1..100000");
      return;
    }
    const bool allAxes = strcmp(tokens[1], "ALL") == 0;
    ControlledAxis *axis = findAxis(tokens[1]);
    if (!allAxes && axis == nullptr) {
      replyError("AXIS", tokens[1]);
      return;
    }

    ControlledAxis *axes[2] = {&xAxis, &zAxis};
    for (size_t i = 0; i < 2; ++i) {
      ControlledAxis *selected = axes[i];
      if (!allAxes && selected != axis) {
        continue;
      }
      if (strcmp(command, "SPEED") == 0) {
        selected->requestedMaxSpeed = value;
        selected->motor.setMaxSpeed(value);
      } else {
        selected->requestedAcceleration = value;
        selected->motor.setAcceleration(value);
      }
    }
    replyOk(command);
    return;
  }

  if (strcmp(command, "SPEED_MM") == 0 ||
      strcmp(command, "ACCEL_MM") == 0) {
    processMillimetreDynamics(tokens,
                              tokenCount,
                              strcmp(command, "SPEED_MM") == 0);
    return;
  }

  replyError("UNKNOWN_COMMAND", command);
}

static void serviceSerial() {
  while (MotionSerial.available() > 0) {
    const char incoming = static_cast<char>(MotionSerial.read());
    if (incoming == '\r') {
      continue;
    }
    if (incoming == '\n') {
      rxBuffer[rxLength] = '\0';
      processCommand(rxBuffer);
      rxLength = 0;
      continue;
    }
    if (rxLength + 1U >= RX_BUFFER_SIZE) {
      rxLength = 0;
      replyError("LINE_TOO_LONG", "maximum 159 characters");
      continue;
    }
    rxBuffer[rxLength++] = incoming;
  }
}

void setup() {
  xAxis.motor.begin();
  zAxis.motor.begin();

  pinMode(DRIVERS_ENABLE_PIN, OUTPUT);
  setDriversEnabled(false);

  xAxis.motor.setMaxSpeed(xAxis.requestedMaxSpeed);
  xAxis.motor.setAcceleration(xAxis.requestedAcceleration);
  zAxis.motor.setMaxSpeed(zAxis.requestedMaxSpeed);
  zAxis.motor.setAcceleration(zAxis.requestedAcceleration);

  MotionSerial.begin(115200);
  MotionSerial.println("READY LASER_XZ 2.5");
  sendStatus();
}

void loop() {
  serviceSerial();

  if (driversEnabled && !estopLatched && strcmp(faultCode, "NONE") == 0) {
    const uint32_t nowUs = micros();
    serviceAxis(xAxis, nowUs);
    serviceAxis(zAxis, nowUs);
  }

  const bool busy = systemBusy();
  if (wasBusy && !busy) {
    MotionSerial.print("EVENT IDLE X=");
    MotionSerial.print(xAxis.motor.currentPosition());
    MotionSerial.print(" Z=");
    MotionSerial.println(zAxis.motor.currentPosition());
  }
  wasBusy = busy;
}
