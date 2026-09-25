# Laser X-Z serial protocol 2.5

For a handoff-ready description of every command, reply, controller state,
movement equation, timing rule, speed/acceleration limit and GUI workflow, see
`GUI_DEVELOPER_INTERFACE_SPEC.md` in this directory.

The STM32 and GUI communicate through USART1 at **115200 baud, 8 data bits, no
parity and 1 stop bit**. Commands are printable ASCII followed by a newline
(`\n`). Commands are case-insensitive.

Firmware 2.x performs the millimetre-to-pulse conversion. The normal control
commands therefore use mm, mm/s and mm/s^2. Internally, position remains an
integer number of driver STEP pulses. The UART protocol is unchanged whether
the power stage uses DRV8825 or TB6600 modules.

## Exact data representation on UART

The protocol is **plain ASCII text**, not a binary packet protocol. Numerical
arguments are written as normal base-10 text using a period as the decimal
separator. For example, the value `-2.5` is transmitted as four separate ASCII
characters: `-`, `2`, `.`, `5`. It is not transmitted as an IEEE-754 float or
as a binary integer.

Each command or reply is one line. The GUI terminates transmitted commands with
line-feed (`LF`, hexadecimal `0A`, decimal byte value `10`). The firmware ignores
carriage-return (`CR`, hexadecimal `0D`), so terminal programs may use either
`LF` or `CR+LF`. Controller replies use `CR+LF` (`0D 0A`). Lines may contain at
most 159 characters before the terminator.

Example command as readable text:

```text
MOVE_MM X 1 Z -2.5<LF>
```

The exact bytes on UART are:

| View | Byte sequence |
|---|---|
| ASCII | `M O V E _ M M [space] X [space] 1 [space] Z [space] - 2 . 5 [LF]` |
| Hexadecimal | `4D 4F 56 45 5F 4D 4D 20 58 20 31 20 5A 20 2D 32 2E 35 0A` |
| Decimal byte values | `77 79 86 69 95 77 77 32 88 32 49 32 90 32 45 50 46 53 10` |

For example, the written number `1` is the ASCII byte `31` hexadecimal (49
decimal), **not** binary byte `01`. A controller acknowledgement such as
`OK MOVE_MM<CR><LF>` appears in hexadecimal as:

```text
4F 4B 20 4D 4F 56 45 5F 4D 4D 0D 0A
```

USART1 uses 115200 baud and `8N1`. At the electrical framing level, every ASCII
byte is sent as one low start bit, eight data bits least-significant-bit first,
no parity bit, and one high stop bit. The signal level must be 3.3 V UART logic;
it is not RS-232 voltage.

Use readable decimal commands such as `MOVE_MM X 12.5`. Do not type the
hexadecimal byte listing into the controller; hexadecimal is shown only to make
logic-analyser and terminal hex-mode captures easy to interpret.

## Millimetre motion commands

| Command | Meaning |
|---|---|
| `MOVE_MM X 5` | Move X by +5 mm from its current position. |
| `MOVE_MM Z -0.5` | Move Z by -0.5 mm. |
| `MOVE_MM X 10 Z 2` | Start relative moves on both axes in one command. |
| `GOTO_MM X 25` | Move X to absolute coordinate 25 mm. |
| `GOTO_MM X 25 Z 8` | Start an absolute move on both axes. |
| `SPEED_MM X 20` | Set X maximum speed to 20 mm/s; allowed range is 1-100 mm/s. |
| `SPEED_MM ALL 10` | Set both axes to 10 mm/s. |
| `ACCEL_MM Z 20` | Set Z acceleration to 20 mm/s^2; allowed range is 5-200 mm/s^2. |
| `ACCEL_MM ALL 40` | Set both axes to 40 mm/s^2. |

Both axes begin from the same accepted X/Z command, but each uses its own
acceleration ramp. This is appropriate for positioning a laser terminal; it is
not CNC linear-path interpolation.

Firmware converts MOVE_MM distances and GOTO_MM coordinates to the nearest
integer STEP pulse. A nonzero value that rounds to zero is rejected with
`ERR RESOLUTION`. Exact one-pulse fine jogging is available through the raw
`MOVE X|Z 1` command and is what the GUI's micrometre jog control uses.

The firmware 2.5 profile reports X = 160 pulses/mm (**6.25 µm/pulse**) and the
empirically calibrated Z = 91.428571 pulses/mm (**10.9375 µm/pulse**).

## Controller and safety commands

| Command | Meaning |
|---|---|
| `PING` | Link test. Returns `OK PONG LASER_XZ 2.5`. |
| `STATUS` | Return controller, position and calibration state. |
| `ENABLE` | Enable both active-low stepper-driver outputs. |
| `STOP` | Immediately stop both axes while retaining holding torque. |
| `DISABLE` | Stop and remove holding torque; coordinate-reference flags are invalidated. |
| `ESTOP` | Latch a software emergency stop and disable the drivers. |
| `CLEAR` | Clear the latched software E-stop; drivers remain disabled. |
| `ZERO X` | Set the idle X coordinate to zero without moving. `Z` and `ALL` are accepted. |
| `HELP` | Return a compact command list. |

`ENABLE` is required before any movement. Movement is rejected while the
software E-stop is latched. There are no homing inputs; `HOME` returns
`ERR UNSUPPORTED`.

## Legacy diagnostic commands

The following low-level commands remain available. Their values are raw driver
STEP pulses, STEP pulses/s and STEP pulses/s²:

```text
MOVE X 100
GOTO X 500 Z 1000
SPEED X 1200
ACCEL X 800
```

They are useful for low-level commissioning. The GUI uses `MOVE X|Z <pulses>`
for exact micrometre fine jogs and millimetre commands for normal positioning
and sessions.

## Replies

- `OK ...` means the command was accepted.
- `ERR <code> <detail>` means the command was rejected.
- `EVENT ...` reports an asynchronous transition such as idle or software E-stop.
- `STATUS ...` contains space-separated `KEY=VALUE` fields.

Example:

```text
STATUS ENABLED=1 ESTOP=0 FAULT=NONE BUSY=0 X=160 XT=160 XH=1 XMM=1.000000 XTMM=1.000000 XSPMM=160.000000 XVMM=20.000 XAMM=40.000 Z=91 ZT=91 ZH=1 ZMM=0.995313 ZTMM=0.995313 ZSPMM=91.428571 ZVMM=20.000 ZAMM=40.000
```

Important fields:

| Field | Meaning |
|---|---|
| `ENABLED` | Driver outputs enabled. |
| `ESTOP` | E-stop latch active. |
| `FAULT` | `NONE` or the active controller fault code. |
| `BUSY` | At least one axis is moving. |
| `X`, `Z` | Current raw STEP-pulse positions. |
| `XT`, `ZT` | Raw target positions. |
| `XMM`, `ZMM` | Current positions in millimetres. |
| `XTMM`, `ZTMM` | Target positions in millimetres. |
| `XSPMM`, `ZSPMM` | Compiled STEP pulses/mm calibration. |
| `XVMM`, `ZVMM` | Configured maximum speed in mm/s. |
| `XAMM`, `ZAMM` | Configured acceleration in mm/s^2. |
| `XH`, `ZH` | Coordinate-reference-set flags; set by `ZERO`. |

## Long-running GUI sessions

An automated session is implemented by the desktop application, not by a new
firmware command. For either stepped serpentine recipe, the GUI first sends
`SPEED_MM ALL 5` and `ACCEL_MM ALL 20`, then sends one `MOVE_MM` command at a
time. It waits for the asynchronous `EVENT IDLE` reply before another move can
be issued and additionally enforces the requested one-second scan-step cadence.

A default loop contains 200 `MOVE_MM Z 1` commands, `MOVE_MM X 1`, 200
`MOVE_MM Z -1` commands, and a final `MOVE_MM X 1`. Any third-party GUI can
implement the same session using only this documented protocol. The mirrored X
recipe uses 200 `MOVE_MM X 1` commands, `MOVE_MM Z 1`, 200 `MOVE_MM X -1`
commands, and a final `MOVE_MM Z 1`. An implementation should validate the
complete 0-200 mm path before starting, serialize moves on `EVENT IDLE`, stop on
any `ERR`, and provide operator-accessible `STOP` and `ESTOP` controls. See
`SESSIONS.md` for both complete recipes and their timing behavior.
