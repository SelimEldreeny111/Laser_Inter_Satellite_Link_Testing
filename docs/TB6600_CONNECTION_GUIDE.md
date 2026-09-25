# TB6600 connection guide for the STM32F401 X-Z stage

## Required driver count

The X and Z motors must remain independent, so the system requires **two motor
drivers: one driver per motor**. A TB6600 contains one bipolar stepper power
stage and drives one motor. Do not connect the two motors in parallel or series
to one TB6600 output.

Either of these arrangements can use the existing firmware:

- two TB6600 modules, one for X and one for Z; or
- one original DRV8825 for one axis and one TB6600 for the other axis.

The rest of this guide shows the recommended two-TB6600 arrangement.

## Important module variation

TB6600-labelled modules are not all electrically identical. Their supply
range, optocoupler input voltage, enable behaviour, current labels and DIP
switch truth tables can differ. Use the voltage range and switch table printed
on the exact module. Do not copy switch positions from a different enclosure.

Before applying power, obtain a clear photograph of:

1. the power/motor terminal labels;
2. the `PUL`, `DIR` and `ENA` terminal labels; and
3. the full current/microstep DIP-switch table.

## Power and motor connections

Make all connections with the motor supply switched off.

| Connection | X TB6600 | Z TB6600 |
|---|---|---|
| Motor supply positive | `V+`, `DC+` or the equivalent printed terminal | Same |
| Motor supply negative | `V-`, `DC-` or the equivalent printed terminal | Same |
| Motor coil 1 | `A+` and `A-` | `A+` and `A-` |
| Motor coil 2 | `B+` and `B-` | `B+` and `B-` |

The existing 12 V supply may be used only if 12 V is inside the voltage range
printed on the module. Feed both drivers from separate branches of an
adequately rated supply. Use sensible wire size and branch fusing; do not route
one driver's motor current through the small terminals or wires of the other.

Identify each motor coil pair with an ohmmeter. Two wires belonging to one coil
show a small resistance; unrelated wires read open circuit. Put one measured
pair on `A+`/`A-` and the other pair on `B+`/`B-`. Swapping the two wires of one
pair reverses that motor. Never connect or disconnect a motor while its driver
is powered.

## Recommended 3.3 V STM32 control interface

Generic TB6600 modules commonly use opto-isolated inputs intended for a 5 V or
higher control loop. Do not assume that the module is safe or reliable when
connected directly to an STM32 3.3 V GPIO. Use a `ULN2003A` or `ULN2803A`
open-collector interface as shown below. Five channels are used.

```text
REGULATED +5 V  -----------------------------------------------+
                                                              |
                X TB6600                 Z TB6600              |
             PUL+ DIR+ ENA+           PUL+ DIR+ ENA+ ----------+
              |    |    |              |    |    |
              +----+----+--------------+----+----+

STM32             ULN2003A/ULN2803A output             TB6600 input
PA0  ----------> IN1              OUT1 --------------> X PUL-
PA1  ----------> IN2              OUT2 --------------> X DIR-
PB0  ----------> IN3              OUT3 --------------> Z PUL-
PB1  ----------> IN4              OUT4 --------------> Z DIR-
PB10 ----------> IN5              OUT5 --------+-----> X ENA-
                                               +-----> Z ENA-

STM32 GND ---- 5 V supply GND ---- ULN GND
ULN COM: leave unconnected for these optocoupler inputs
```

This common-anode arrangement preserves the existing firmware polarity:

- an STM32 STEP output going HIGH makes the ULN output sink current and
  produces a TB6600 input pulse;
- PA1 controls X direction and PB1 controls Z direction;
- PB10 is shared by both enable inputs.

An individual NPN transistor such as a 2N2222 may replace each ULN channel. Use
one transistor per signal, a 2.2 kohm GPIO-to-base resistor, a 10 kohm
base-to-ground resistor, emitter to STM32/5 V ground, and collector to the
corresponding TB6600 minus input.

The 5 V used for the optocouplers must be regulated. Its ground must connect to
STM32 ground and the ULN/transistor emitters. Do not connect raw 12 V to any
STM32 pin.

### Enable behaviour check

The existing firmware assumes the common module behaviour in which an asserted
`ENA`/offline optocoupler disables the motor outputs. With the circuit above:

| Controller state | PB10 | Expected motor state |
|---|---:|---|
| Startup or `DISABLE` | HIGH | Outputs off; no holding torque |
| `ENABLE` | LOW | Outputs on; holding torque available |

Some modules implement or label enable differently. First test at low current
with the laser off. If the motors hold only after `DISABLE`, change this line in
`firmware/Laser_XZ_Controller/Laser_XZ_Controller.ino`:

```cpp
static const bool DRIVER_ENABLE_ACTIVE_LEVEL = HIGH;
```

Then upload the firmware again. Do not leave `ENA` disconnected in the final
system because the GUI would be unable to remove holding torque on `DISABLE` or
software E-stop.

## High-resolution DIP-switch settings

Both axes use a 1.8 degree motor and a 20-tooth, 2 mm-pitch GT2 pulley:

```text
travel per revolution = 20 teeth x 2 mm = 40 mm
6400 STEP pulses/revolution / 40 mm/revolution = 160 pulses/mm
nominal smallest increment = 1 / 160 mm = 6.25 micrometres
```

Firmware 2.5 requires each driver to be set to **6400 pulses/revolution**, also
described as `1/32` microstep. Use the switch combination printed on the exact
TB6600 module; clone switch tables are not interchangeable. If the exact module
does not offer 6400 pulses/revolution, do not operate this firmware build.

If a different microstep setting is deliberately selected, update both
firmware calibration constants using this table:

| Driver setting | Pulses/revolution | Required pulses/mm for 20T GT2 |
|---:|---:|---:|
| Full step | 200 | 5 |
| Half step | 400 | 10 |
| Quarter step | 800 | 20 |
| 1/8 step | 1600 | 40 |
| 1/16 step | 3200 | 80 |
| 1/32 step | 6400 | 160 |

```cpp
static const float X_STEP_PULSES_PER_MM = 160.0f;
static const float Z_STEP_PULSES_PER_MM = 91.428571f;
```

The Z value preserves the empirical calibration from the measured 20 mm command
that originally produced 35 mm. The working 400-pulse value was
`5.7142857 pulses/mm`; multiplying it by the 16× microstep change gives
`91.428571 pulses/mm`, or **10.9375 µm per STEP pulse**. X is nominally
**6.25 µm per STEP pulse**. Recheck Z over a long measured distance after the
DIP-switch change and refine this constant if needed.

## Current-limit switch settings

The motor-family datasheet in this project lists 2 A per phase as the upper
motor rating. The number printed on a TB6600 module may represent peak current
instead of RMS current, and clone tables vary. For initial commissioning,
select the lowest available setting no higher than approximately 1.0 to 1.5 A,
verify smooth movement and monitor motor/driver temperature. Increase only if
the mechanism needs more torque, and never exceed the motor rating or the
specific module's documented continuous capability.

Power-supply input current is not equal to motor phase current; a low displayed
PSU current does not prove that the phase-current switch setting is wrong.

## Firmware compatibility

The UART command set and GUI do not change. The firmware continues to use:

| Axis/function | STM32 pin |
|---|---|
| X STEP | PA0 |
| X direction | PA1 |
| Z STEP | PB0 |
| Z direction | PB1 |
| Shared enable | PB10 |

The main firmware now generates a 10 us STEP-high pulse and waits 5 us after a
direction change. These conservative values are suitable for opto-isolated
TB6600 modules while remaining compatible with DRV8825 carriers.

## First-power test

1. Keep the laser disabled and arrange the mechanics so neither axis can crash
   or allow a vertical load to fall.
2. Switch off the motor supply and verify both coil pairs and every terminal.
3. Set both drivers to 6400 pulses/revolution and a low current setting.
4. Verify that no motor wire is connected to an STM32 or control-input terminal.
5. Power the controller and drivers. Check for heat, smell or abnormal current
   before commanding movement.
6. Upload `firmware/Dual_Motor_Test/Dual_Motor_Test.ino`. Both motors should
   rotate one revolution forward, pause, rotate one revolution backward and
   pause.
7. Upload `firmware/Laser_XZ_Controller/Laser_XZ_Controller.ino` for GUI use.
8. In a 115200-baud serial terminal, send these newline-terminated commands:

```text
PING
ENABLE
ZERO ALL
SPEED_MM ALL 5
ACCEL_MM ALL 20
MOVE_MM X 1
MOVE_MM Z 1
STATUS
```

Each 1 mm X move generates 160 STEP pulses. A 1 mm Z request rounds to the
nearest calibrated pulse (approximately 91 pulses). Verify both distances with
a dial indicator or suitable displacement gauge. If a direction is reversed,
change only that axis's
`X_POSITIVE_DIR_LEVEL` or `Z_POSITIVE_DIR_LEVEL` constant, or swap the two wires
within one motor coil pair while power is off.
