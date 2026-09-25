# HP E5574A through GPIB/VISA

The E5574A is controlled from the Windows host through a VISA-compatible GPIB
interface. The STM32 UART remains dedicated to the X/Z motion controller.

```text
Windows GUI ── USB-UART ── STM32F401 ── stepper drivers
Windows GUI ── GPIB/VISA adapter ── HP E5574A
```

Do not connect the STM32 UART pins directly to the IEEE-488/GPIB connector.
GPIB is a separate parallel instrument bus and requires a GPIB controller or
VISA-compatible adapter.

## Software setup

The standalone GUI already bundles PyVISA and the Excel-writing components.
When running the GUI from Python source, install its requirements with:

```text
python -m pip install -r gui\requirements.txt
```

In both cases, install the system VISA runtime required by the adapter, such as
Keysight IO Libraries Suite or NI-VISA/NI-488.2. PyVISA is only the API; it does
not itself provide a physical GPIB controller.

## GUI setup

1. Power on the E5574A and connect the GPIB adapter to the Windows PC.
2. Confirm the E5574A HP-IB address from its front panel. The documented
   default is address `24`.
3. Start the GUI and open **E5574A / GPIB**.
4. Click **Refresh VISA**.
5. Select or enter a resource such as `GPIB0::24::INSTR`.
6. Click **Connect E5574A** and confirm the identification string.
7. Select the E5574A application and click **Select application**. Selecting
   **Power meter** configures optical Head A for absolute power in dBm.
8. Click **Read measurement**.

## Automated session acquisition

The **Sessions** tab requires both the STM32 and E5574A connections. At the
start of every session, the GUI automatically:

1. exits any nested E5574A application;
2. activates the Powermeter application;
3. selects absolute measurement mode for Head A;
4. sets the power unit to dBm and verifies both settings.

For every X or Z session action, the GUI waits for the STM32 `EVENT IDLE`
confirmation, allows the mechanism to settle, reads Head A during the
configured post-move dwell, and records the numeric dBm value plus the raw GPIB
response. A failed or non-numeric power response aborts the session so missing
measurements are not silently ignored.

## Commands used by the GUI

| Function | E5574A command |
|---|---|
| Identify | `*IDN?` |
| Exit nested application | `:SENS:FUNC MAIN` |
| Select power meter | `:SENS:FUNC POW` |
| Select Head A absolute mode | `:SENS1:POW:MEAS:MOD ABS` |
| Set power unit to dBm | `:SENS1:POW:UNIT DBM` |
| Verify Head A mode | `:SENS1:POW:MEAS:MOD?` (must return `0`) |
| Verify dBm unit | `:SENS1:POW:UNIT?` (must return `0`) |
| Select insertion loss | `:SENS:FUNC IL` |
| Select PDL | `:SENS:FUNC PDL` |
| Select return loss | `:SENS:FUNC RL` |
| Read Head A power | `:SENS1:DATA? POW` |
| Read insertion loss | `:SENS:DATA? IL` |
| Read PDL | `:SENS:DATA? PDL` |
| Read return loss | `:SENS:DATA? RL` |

These commands follow Chapter 8 and Programming Example 2 of the official
[E5574A Optical Loss Analyzer User's Guide](https://www.keysight.com/zz/en/assets/9018-05750/user-manuals/9018-05750.pdf).

Each completed or interrupted session writes a workbook named
`LaserStageSession_<timestamp>.xlsx` inside the project's `Session Reports`
folder. For the supplied project this is
`D:\Laser communication\Session Reports`. Use **Open Excel reports** in the
Sessions tab to open it. The workbook contains:

- **Summary** — configuration, instrument identity, status and power statistics;
- **Measurements** — one row per completed move with exact timing, X/Z position,
  command, numeric power in dBm and raw GPIB response;
- **Events** — the complete ordered session scenario, including movement,
  controller idle, GPIB acquisition, pause/resume, stop and errors.
