# Branching and release workflow

The `main` branch contains the last validated integrated system. Do not develop
new changes directly on `main`.

## Every update

1. Synchronize the local `main` branch with GitHub.
2. Create a new branch before editing:
   - `feature/<short-name>` for new capability;
   - `fix/<short-name>` for a correction;
   - `docs/<short-name>` for documentation only;
   - `release/<version>` for a validated release candidate.
3. Keep each branch focused on one coherent update.
4. Run the complete Python test suite and compile all three STM32 sketches.
5. Push the branch and review its changes before merging into `main`.
6. Package release binaries only after the source has passed validation.

## Release contents

The repository tracks source code, tests, documentation, wiring references,
and only the currently validated Windows GUI and STM32 firmware artifacts.
Local session workbooks, temporary plots, build caches, and historical
executables are intentionally excluded.

## Hardware calibration rule

Microstep switch changes and mechanical calibration changes must be committed
together with their matching GUI defaults, firmware constants, tests, and
documentation. A release must never combine firmware and a GUI built for
different pulses-per-millimetre values.
