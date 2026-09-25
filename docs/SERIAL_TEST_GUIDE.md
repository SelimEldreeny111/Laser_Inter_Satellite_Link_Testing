# Standalone STM32 serial test guide

This procedure tests firmware 2.5 directly, without the Windows GUI.

## UART connection

Use a USB-UART adapter with **3.3 V logic**, not RS-232 voltage levels.

| USB-UART adapter | STM32F401RCT6 |
|---|---|
| `TX` | `PA10` (USART1 RX) |
| `RX` | `PA9` (USART1 TX) |
| `GND` | Controller/driver common GND |

Use **115200 baud, 8 data bits, no parity, 1 stop bit, no flow control**. Set
the terminal line ending to **newline (LF)** or **CR+LF**. Do not connect the
adapter VCC unless the controller board specifically requires it.

Arduino IDE Serial Monitor, PuTTY and Tera Term can all be used. Close the GUI
first because only one application can normally open a COM port at a time.

## First safe test

Keep the laser off, put the mechanism near the middle of its travel, and begin
with the drivers disabled. After opening the COM port, reset the STM32. It sends:

```text
READY LASER_XZ 2.5
STATUS ENABLED=0 ESTOP=0 FAULT=NONE BUSY=0 ...
```

Type and send one line at a time:

```text
PING
STATUS
CLEAR
ENABLE
ZERO ALL
SPEED_MM ALL 5
ACCEL_MM ALL 20
MOVE_MM X 1 Z 1
STATUS
MOVE_MM X -1 Z -1
STATUS
DISABLE
```

Expected key replies are:

```text
OK PONG LASER_XZ 2.5
OK CLEAR
OK ENABLE
OK ZERO
OK SPEED_MM
OK ACCEL_MM
OK MOVE_MM
EVENT IDLE X=160 Z=91
```

After the first `MOVE_MM X 1 Z 1`, `STATUS` must show `X=160`, `Z=91`,
`XMM=1.000000`, approximately `ZMM=0.995313`, `XSPMM=160.000000` and
`ZSPMM=91.428571`. The difference is the expected nearest-pulse rounding of the
empirical Z calibration. The return move should finish at `X=0 Z=0`.

## Commands for normal control

Commands are case-insensitive ASCII lines. Spaces and commas are accepted as
separators.

| Command | Function |
|---|---|
| `PING` | Check identity and firmware version. |
| `STATUS` | Read driver, fault, motion, position, target and calibration state. |
| `HELP` | Print the compact command list. |
| `CLEAR` | Clear the software E-stop/fault latch; drivers stay disabled. |
| `ENABLE` | Enable both drivers through the shared active-low PB10 signal. |
| `STOP` | Immediately stop motion; holding torque remains. |
| `DISABLE` | Stop, disable holding torque and invalidate both zero-reference flags. |
| `ESTOP` | Latch the serial software E-stop and disable both drivers. |
| `ZERO X` | Set the idle X coordinate to 0 without moving. |
| `ZERO Z` | Set the idle Z coordinate to 0 without moving. |
| `ZERO ALL` | Set both idle coordinates to 0 without moving. |
| `MOVE_MM X 5` | Move X by +5 mm relative to its current position. |
| `MOVE_MM Z -2` | Move Z by -2 mm relative to its current position. |
| `MOVE_MM X 5 Z 2` | Start both relative axis moves from one command. |
| `GOTO_MM X 25` | Move X to absolute coordinate 25 mm. |
| `GOTO_MM X 25 Z 80` | Move both axes to absolute coordinates. |
| `SPEED_MM X 20` | Set X maximum speed to 20 mm/s. `Z` or `ALL` also work. |
| `ACCEL_MM ALL 40` | Set both accelerations to 40 mm/s^2. |

Firmware 2.5 accepts `SPEED_MM` values from 1 to 100 mm/s and `ACCEL_MM`
values from 5 to 200 mm/s^2. Start with the 20 mm/s and 40 mm/s^2 defaults.

Both axes start from the same accepted two-axis command, but they use
independent acceleration ramps. This is positioning motion, not coordinated
CNC linear interpolation.

Legacy diagnostic commands use raw pulses rather than millimetres:

```text
MOVE X 10
GOTO X 100 Z 50
SPEED ALL 200
ACCEL ALL 400
```

With the compiled X calibration, `MOVE X 1` is an exact **6.25 µm** jog. On Z,
`MOVE Z 1` is an exact **10.9375 µm** calibrated jog.

## What the GUI UART bus log means

The second GUI tab, **UART bus log**, uses explicit direction markers:

```text
[12:34:56.100] TX > MOVE_MM X 1 Z 1
[12:34:56.105] RX < OK MOVE_MM
[12:34:56.430] RX < EVENT IDLE X=160 Z=91
```

- `TX >` is the exact command line sent from the PC to STM32 PA10.
- `RX <` is the exact response line received from STM32 PA9.
- `SYSTEM` is a GUI connection or parsing message and is not UART traffic.

The on-screen checkbox shows or hides repetitive automatic `STATUS` polling.
The complete log always includes that traffic at:

```text
%LOCALAPPDATA%\LaserStageController\serial.log
```

## Calibration and direction

Both axes in the supplied X-Z drawings use a 20-tooth GT2 pulley and a 2 mm
pitch belt. In 1/32 microstep mode:

```text
6400 motor STEP pulses/revolution
20 teeth x 2 mm/tooth = 40 mm/revolution
6400 / 40 = 160 pulses/mm = 6.25 µm/pulse nominally
```

If a measured 10 mm command does not travel 10 mm, calculate:

```text
new pulses/mm = old pulses/mm x commanded distance / measured distance
```

Edit the X or Z calibration constant near the top of
`firmware/Laser_XZ_Controller/Laser_XZ_Controller.ino`, upload again, then
repeat the test. If an axis moves in the wrong direction, change only its
`X_POSITIVE_DIR_LEVEL` or `Z_POSITIVE_DIR_LEVEL` constant; do not swap a motor
coil while powered.

## Common rejected-command replies

| Reply | Meaning and action |
|---|---|
| `ERR DISABLED send ENABLE first` | Send `ENABLE`, then retry the move. |
| `ERR FAULTED ESTOP` | Send `CLEAR`, then `ENABLE`. |
| `ERR SYNTAX ...` | Correct the command format shown after the error code. |
| `ERR AXIS ...` | Use only `X`, `Z`, or `ALL` where allowed. |
| `ERR NUMBER ...` | A numeric argument is malformed. Use a dot as the decimal separator. |
| `ERR RESOLUTION ...` | The requested value rounds below one STEP pulse. |
| `ERR RANGE ...` | The number or calibrated pulse rate is outside firmware limits. |
| `ERR UNSUPPORTED this controller has no homing inputs` | Use `ZERO`; this hardware has no home switches. |

There are no software travel limits in this hardware version. Fit physical
stops, supervise every commissioning move, and use small distances first.
