# Automated motion sessions

The **Sessions** tab runs long, repeatable motion recipes without blocking the
GUI. It provides two mirrored recipes:

- **Stepped Z serpentine scan** covers the Z travel and shifts X after each pass.
- **Stepped X serpentine scan** covers the X travel and shifts Z after each pass.

Both recipes default to a 200 mm scan, 1 mm increments, a one-second
post-move measurement dwell, a +1 mm cross-axis shift, and a selected number
of loops. The Excel beam analysis defaults to a **100 mm target diameter**;
this value can be changed in the Sessions tab without changing the motion path.

The session uses the existing firmware 2.5 ASCII UART commands. No special
session command or firmware change is required. The GUI sends one normal motion
command at a time and waits for the controller's `EVENT IDLE` reply. It then
captures Head A absolute power in dBm from the E5574A during the configured
dwell before it can send the next move.

## Stepped Z serpentine path

The default recipe values are:

| Parameter | Default |
|---|---:|
| Z scan span | 200 mm |
| Z increment | 1 mm |
| Post-move measurement dwell | 1 second |
| X shift after each Z pass | +1 mm |
| Beam target diameter | 100 mm |
| Session speed | 5 mm/s |
| Session acceleration | 20 mm/s² |
| Number of loops | 1 |

The stage must start at `Z = 0 mm`. One loop performs:

1. Send 200 separate `MOVE_MM Z 1` moves. After each physical completion, wait
   one second and capture an E5574A power reading. This moves Z from 0 to 200 mm.
2. Send `MOVE_MM X 1`.
3. Send 200 separate `MOVE_MM Z -1` moves, again measuring during each
   post-move dwell. This moves Z from 200 mm back to 0 mm.
4. Send `MOVE_MM X 1`.

The next loop begins with the next upward Z pass. Therefore, with the defaults:

| Result | Value |
|---|---:|
| Commands/actions per loop | 402 |
| Z position after every complete loop | 0 mm |
| X advance per complete loop | +2 mm |
| Approximate duration per loop | 00:09:50 |
| Maximum loops when starting at X = 0 mm | 100 |

For `N` loops, the expected final coordinate is:

```text
final X = starting X + N × 2 × X-shift
final Z = 0 mm
```

The GUI calculates this before starting and rejects a session whose final X
would exceed 200 mm. For example, the default +1 mm shift permits 100 loops
from X = 0 mm, but only 25 loops from X = 150 mm.

## Stepped X serpentine path

Select **Stepped X serpentine scan** to mirror the same operation. The stage
must start at `X = 0 mm`. One loop performs:

1. Send 200 separate `MOVE_MM X 1` moves. After each physical completion, wait
   one second and capture an E5574A power reading. This moves X from 0 to 200 mm.
2. Send `MOVE_MM Z 1`.
3. Send 200 separate `MOVE_MM X -1` moves with the same post-move acquisition.
   This moves X from 200 mm back to 0 mm.
4. Send `MOVE_MM Z 1`.

With the default X recipe:

| Result | Value |
|---|---:|
| Commands/actions per loop | 402 |
| X position after every complete loop | 0 mm |
| Z advance per complete loop | +2 mm |
| Approximate duration per loop | 00:09:50 |
| Maximum loops when starting at Z = 0 mm | 100 |

For `N` loops, the expected final coordinate is:

```text
final X = 0 mm
final Z = starting Z + N × 2 × Z-shift
```

The GUI rejects an X session if X is not at zero or if the calculated final Z
would exceed 200 mm. For example, the default +1 mm Z shift permits 100 loops
from Z = 0 mm, but only 25 loops from Z = 150 mm.

## What the one-second dwell means

The one-second value is a true dwell after the controller confirms physical
motion completion. It applies to scan-axis steps and cross-axis shifts. During
that dwell, the GUI waits briefly for mechanical/optical settling and reads
E5574A Head A in absolute dBm mode. Speed and acceleration still define the
motion profile inside each move.

At the defaults, a 1 mm move with 20 mm/s² acceleration is triangular because
the axis cannot reach the requested 5 mm/s maximum speed over such a short
distance. Its ideal motion time is:

```text
t = 2 × sqrt(distance / acceleration)
  = 2 × sqrt(1 / 20)
  ≈ 0.447 seconds
```

The default 1 mm move takes approximately 0.447 seconds, followed by the full
one-second dwell. If GPIB acquisition takes longer than the dwell, the GUI
continues waiting and does not overlap commands. The real session becomes
slower, not unsafe or backlogged.

The displayed estimate includes ideal triangular/trapezoidal motion time, the
post-move dwell for every action, and a small scheduler margin. Mechanical
load, GPIB acquisition time, host scheduling, and UART latency can make the
actual time slightly longer.

## Safe operating procedure

Before using a long-running session:

1. Verify both positive directions using small manual jogs. `X +` must move in
   the intended scan direction and `Z +` must move upward.
2. Verify the complete path is mechanically clear and that the laser/fibre
   cable can traverse the full envelope without pulling or bending sharply.
3. Connect the GUI and confirm firmware 2.x, no fault, and software E-stop
   released.
4. Enable the motor drivers.
5. Move the stage to the safe lower-left origin: X at the desired scan start
   and Z at the bottom of its travel.
6. Click **Set X/Z zero**. Both `XH` and `ZH` must report `1`.
7. Connect the E5574A in **E5574A / GPIB** and verify one manual Power meter
   reading from optical Head A.
8. Open **Sessions**, select the recipe, enter the number of loops, and review
   the calculated duration, command count, dwell, target diameter and total
   cross-axis advance.
9. Click **Start session** and confirm the displayed path and Excel report.

The Start button is accepted only while the controller is connected, idle,
enabled, not E-stopped, and fault-free. Both coordinate references are required.
The selected scan axis must be at 0 mm, and sufficient positive travel must
remain on the other axis for all cross-axis shifts.

There are no physical limit-switch inputs in the current controller build.
Software coordinates cannot replace hard stops or a correctly wired hardware
emergency-stop circuit. Keep the laser disabled or safely terminated while
commissioning a new motion recipe.

## Pause, resume, stop, and faults

- **Pause** lets the current move and its E5574A power acquisition finish, then
  prevents the next command from being sent. **Resume** continues from the next
  unsent action.
- **Stop session**, the toolbar **STOP motion** button, or the `Esc` key sends
  `STOP` and ends the session while the drivers remain enabled.
- **E-STOP** sends the firmware software E-stop command and aborts the session.
- **Disable drivers** aborts the session before disabling motor holding torque.
  A vertical mechanism may fall when disabled.
- UART disconnect, driver disable, E-stop, controller fault, or any `ERR` reply
  automatically aborts the session.

An E5574A disconnect, timeout, non-numeric response, or failure to verify
absolute dBm mode also aborts the session. The workbook records the failed
measurement instead of silently leaving an unexplained gap.

After a stopped or aborted session, manually return the stage to a known safe
position. The Z recipe cannot restart until Z is back at 0 mm; the X recipe
cannot restart until X is back at 0 mm. If position was lost mechanically,
establish the origin again before continuing.

## UART traffic generated by the recipe

At the start, the GUI sends:

```text
SPEED_MM ALL 5
ACCEL_MM ALL 20
```

For the Z recipe, a representative beginning is:

```text
MOVE_MM Z 1
MOVE_MM Z 1
...
MOVE_MM Z 1
MOVE_MM X 1
MOVE_MM Z -1
...
```

For the X recipe, the mirrored traffic is:

```text
MOVE_MM X 1
MOVE_MM X 1
...
MOVE_MM X 1
MOVE_MM Z 1
MOVE_MM X -1
...
```

Each line is printable ASCII with an `LF` terminator. The **UART bus log** tab
shows every transmitted line as `TX >` and every controller reply as `RX <`.
The firmware normally acknowledges a move and later emits `EVENT IDLE` when it
has physically completed. See `SERIAL_PROTOCOL.md` for the exact byte format,
all replies, and the complete command reference.

The on-screen session event log records starts, measurements, pauses, resumes,
stops and faults. It is also appended to:

```text
%LOCALAPPDATA%\LaserStageController\sessions.log
```

The complete UART traffic remains in:

```text
%LOCALAPPDATA%\LaserStageController\serial.log
```

Every completed, stopped or aborted session also writes a timestamped Excel
workbook in the project's `Session Reports` folder. In the supplied project,
the location is `D:\Laser communication\Session Reports`. Click **Open Excel
reports** in the Sessions tab to open the folder.
The workbook contains:

- **Summary** for configuration, result status, min/average/max dBm and the
  principal beam-position metrics;
- **Beam Map** with a native Excel X-Z scatter chart containing every valid
  reading across the complete scan. Power bands are coloured relative to the
  in-target peak, and the chart overlays the configured target
  circle, target center, linear-power-weighted centroid and peak location. A
  radial-distance-versus-relative-power chart is included beside it;
- **Weather Map** with a continuous radar-style colour field. It uses
  12-nearest-point inverse-distance interpolation in linear power and converts
  the result back to dB across the complete measured X/Z envelope. The target
  circle is retained as the region of interest, and interpolated cells remain
  separate from raw data;
- **Beam Profiles** with X and Z centerline curves, a measured radial-average
  curve and a visible -3 dB reference;
- **Beam Heatmap** with average dBm at every measured X/Z location, including
  readings outside the target circle. Only genuinely unmeasured cells are blank;
- **Beam Data** with the processed position, dBm, relative-power, radius and
  inside/outside classification for every valid reading;
- **Measurements** with one row per completed move, exact elapsed second,
  timestamps, loop/phase, command, controller X/Z position, numeric dBm value,
  raw GPIB response and result status;
- **Events** with the complete ordered move/idle/acquire/pause/stop/error story.

The target center is inferred from the midpoint of the measured X and Z
extents. For firmware 2.5, positions are calculated from the raw X/Z pulse
coordinates in the same `EVENT IDLE` that releases each measurement, using the
controller's reported pulses-per-mm calibration. They therefore include pulse
rounding and are more exact than simply accumulating requested millimetres.
They are still open-loop controller positions, not independent encoder
feedback. The report intentionally presents
descriptive offset, spread and standard-deviation metrics without declaring a
pass/fail result until the project defines acceptable pointing and uniformity
tolerances.

## Plot an existing workbook without running a session

Use the **Excel Beam Plotter** tab when measurements have already been recorded
or when a legacy report contains only `Summary`, `Measurements` and `Events`:

1. Click **Browse .xlsx** and select the session workbook.
2. Confirm the beam target diameter and the X/Z pulses-per-mm calibration.
   This high-resolution project defaults to X = 160 and Z = 91.428571
   pulses/mm, corresponding to 6.25 µm and 10.9375 µm per pulse respectively.
3. Keep **Use raw EVENT IDLE pulse positions** enabled for legacy workbooks.
   The importer converts the pulse coordinates in each measurement row to mm,
   preserving STEP-pulse rounding instead of relying only on accumulated
   requested positions.
4. Keep automatic target-center inference enabled unless the physical optical
   target center is known independently.
5. Click **Load workbook and preview**, then choose **Weather heatmap**,
   **Measured point map**, **X centerline profile**, **Z centerline profile** or
   **Radial average profile**. No hardware connection is required.
6. Click **Create plotted Excel copy**. The original workbook is preserved and
   a new `_BeamPlots.xlsx` copy is created beside it with `Beam Map`,
   `Weather Map`, `Beam Profiles`, `Beam Heatmap`,
   `Beam Data` and the original audit sheets.

Accepted position headers include `Controller X/Z position (mm)`, legacy
`Expected X/Z (mm)` and simple `X/Z position (mm)` names. The power column must
be numeric and labelled `Power (dBm)`, `Power dBm` or `dBm`.

For a complete 100 mm-diameter map with 1 mm sampling, configure the scan and
cross-axis advance so the measured envelope covers at least 100 mm by 100 mm.
For example, a Z scan span of 100 mm, X shift of 1 mm and 50 loops covers an X
advance of 100 mm while repeatedly scanning Z from 0 to 100 mm and back. Check
the full mechanical path, expected duration and laser safety before starting.

## Adding future recipes

The Sessions tab is intentionally recipe-based. A new session should provide:

1. A validated parameter set.
2. A deterministic list or generator of ordinary UART actions.
3. A path-envelope check before execution.
4. An estimated duration.
5. Human-readable phase and progress labels.

The shared scheduler already provides non-blocking execution, one-command-at-a-
time flow control, pause/resume, stop, persistent logging, and fault/disconnect
abort handling. Future scan ideas can therefore be added as new recipe choices
without changing the firmware protocol.
