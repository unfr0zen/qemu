# AGENTS.md

Guidance for coding agents working in this repository.

QEMU machines that boot the stock firmware of Nest Learning Thermostats from a user-supplied NAND image: `-M diamond3` (i.MX6SoloX), `-M j49` (TI AM3703), and early U-Boot bring-up for `-M onyx` (Nest Thermostat E, i.MX6UL). The user-supplied Diamond3 mask ROM is tracked at `roms/diamond3/bootrom.bin`. NAND images, signed U-Boot and other firmware remain external and must not be added.

## Commands

```sh
./setup.sh docker          # clone QEMU v11.0.0 into qemu-src/, apply patches/, build in Docker
./setup.sh                 # same, native build (qemu-src/build/qemu-system-arm)
rm -rf qemu-src && ./setup.sh   # needed after patches/ changes: patches apply only to a fresh clone

./run.sh --nand nand-logical.bin   # diamond3 first boot (seeds the working copy in .qemu/)
./run.sh --ui                      # + web UI at http://127.0.0.1:8075 (needed by the MCP server)
./run.sh --j49 --nand nand-j49.raw
./run.sh --onyx --ui              # .qemu/onyx/nand-state.bin includes U-Boot
NAND_FRESH=1 ./run.sh --nand FILE  # re-seed .qemu/ from FILE
./run.sh -d guest_errors,unimp ... # extra args pass through to QEMU
```

Guest login `root` / `nest`. Quit: Ctrl-A X (Ctrl-] with `--ui`). Full option list: header of `run.sh`.

CI checks (`.circleci/config.yml`), reproducible locally:

```sh
# patches: numbered 0001..N without gaps, apply one at a time, checkpatch-clean (new/edited patches only)
perl qemu-src/scripts/checkpatch.pl --no-signoff -q patches/NNNN-*.patch
# QEMU's ARM qtests (qom-test and test-hmp instantiate diamond3 and j49)
cd qemu-src/build && ./pyvenv/bin/meson test --suite qtest-arm --no-rebuild --print-errorlogs
cd qemu-src/build && ./pyvenv/bin/meson test --suite qtest-arm --no-rebuild qtest-arm/qom-test   # one test

# scripts
python3 -m py_compile $(git ls-files '*.py')
pipx run ruff==0.17.0 check --isolated --select E9,F $(git ls-files '*.py')
for f in $(git ls-files '*.sh'); do sh -n "$f"; done
shellcheck --severity=warning $(git ls-files '*.sh')
```

## Architecture

**The machines are a patch series, not a fork.** `patches/*.patch` (on QEMU v11.0.0, `98b060d`) are the source of truth; `qemu-src/` is a gitignored clone. `setup.sh` applies them one at a time with `git apply` (a later patch may touch a file an earlier one creates) and skips applying if `qemu-src/hw/arm/flintstone.c` exists. To change a model: work in `qemu-src/`, commit, and regenerate the series with `git format-patch 98b060d..HEAD -o ../patches/` (docs/qemu.md, "Developing the machine"). Recent history adds fixes as new patches at the end of the stack rather than editing old ones; subjects use QEMU's prefixes (`hw/arm: ...`, `hw/net: bcm43341: ...`). Patch-to-model maps: docs/qemu.md "Hardware model" (diamond3), docs/qemu-j49.md "Hardware model" (j49).

- diamond3: i.MX6SX SoC container + `diamond3` machine, GPMI NAND/BCH, a boot-ROM stub that loads U-Boot from NAND (applies the IVT's DCD), LCDIF, Vivante GC400T GPU (software 3D pipeline), BCM43341 SDIO Wi-Fi, vf610 ADC magwheel, PWM piezo, EM357 Thread radio SPI.
- j49: OMAP3 peripherals (INTC, GP timers, PRCM, GPMC+BCH4 NAND, TPS65921, UART, I2C, GPIO, WDT, MMCHS, McSPI, SDMA, DSS) + `j49` machine, WL1271 Wi-Fi, ADBS-A320 ring sensor.

- onyx: existing i.MX6UL SoC with MMDC and GPMI/BCH, fixed-offset NAND U-Boot ROM loader, synthetic shadow fuses/thermal sample, and IOMUXC UART1 DCE routing. Locked firmware disconnects UART after the token check; display and thermostat peripherals are not modelled.

**Host side (Python, standard library only)** — QEMU exposes chardevs and QMP over sockets; these processes are the "rest of the hardware":

- `run.sh` — picks the built binary (`qemu-src/build` or `build-docker`), keeps the NAND working copy in `.qemu/`, runs the pre-boot, then launches QEMU plus the helpers below.
- `scripts/provision.py` — the pre-boot: a short boot with `init=/bin/sh` from U-Boot that uses the guest's own kernel to set the root password, quiet printk, keep the display on, save Wi-Fi, fix resolv.conf, mark OOB setup done and save backplate wiring. Writes go through the kernel so UBIFS/JFFS2 journals stay consistent; never edit the filesystem images from the host. `--no-provision` skips it.
- `ui/ui.py` — `--ui` web server (`ui/index.html`): screen via QMP screendump streamed over SSE, button/firmware lines via the `flintstone.board` chardev, wheel via a QMP machine property, serial console shared with the terminal. Its HTTP API is the only path to the unit for other tools.
- `ui/backplate.py` — fake backplate MCU on the sensor UART (framed protocol reverse-engineered from nlclient: temp, PIR, ALS, wiring).
- `ui/em35x.py` — the EM35x ConnectIP NCP application behind the `em35x-ncp` device's chardev (framing lives in QEMU, the app here).
- `ui/mcp_server.py` — MCP server (`nest` in `.mcp.json`) wrapping ui.py's HTTP API: screenshots, recordings (ffmpeg if present), inputs, console, files, clock, backplate, QMP. Requires a running `./run.sh --ui`.
- `scripts/skip-setup.py` — standalone helper: skip OOB setup on a running unit.
- `boards/<machine>/` — host code for one board only: `j49/j49_env.py` (read/edit the j49 U-Boot env on a raw NAND with fresh BCH4 ECC), `diamond/diamond_radio.py` (diamond CC2530 UART2 endpoint). `onyx/onyx_env.py` constructs the working NAND with embedded U-Boot and selected volumes, without editing UBIFS contents; `onyx/launch.py` supervises NAND boot and the serial/QMP UI. Shared helpers take `--board` instead.

`--nlclient FILE` execs `../nlclient/tools/run_ui_test.py`, a sibling repo not in this tree.

## Docs

- `docs/qemu-onyx.md` — Nest Thermostat E (`onyx`) U-Boot UART bring-up; no full firmware boot support.
- `docs/qemu.md`, `docs/qemu-j49.md` — usage, NAND state, network, pre-boot, hardware model, debugging (`-d unimp`, gdb, symbol recovery), not-yet-modelled list.
- `docs/spec-flintstone.md`, `docs/spec-j49.md` — board memory maps and peripherals.
- `docs/nand-image.md` — image formats (diamond3: 512 MiB logical, data pages only; j49: raw 2048+64 OOB pages) and partition layout.
