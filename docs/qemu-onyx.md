# Onyx U-Boot bring-up

`onyx` is the Nest Thermostat E board: i.MX6UL rev1.2, 128 MiB LPDDR2
and 512 MiB NAND. The machine supports stock U-Boot initialization, not an
interactive prompt or a complete Onyx Linux boot.

## Run

```sh
./run.sh --onyx --ui
```

The launcher uses `.qemu/onyx/nand-state.bin`, including the U-Boot image
embedded in its boot partition. No separate firmware file, `-bios`, or
per-volume arguments are needed. The web UI defaults to
<http://127.0.0.1:8075>; `--ui-port PORT` selects another starting port.
Omit `--ui` for the terminal-only serial console.

The UI provides serial output and a reset button through QMP. Onyx's
screen, ring, backplate, radio and USB are not modelled. The launcher
disables watchdog actions for bring-up so unsuccessful boots do not
continually restart. QMP reset still restarts the machine.

All launcher and model code is in the repository. Firmware and NAND images
are user-supplied, sensitive, gitignored files; none is included in git.
Temporary runtime directories hold only sockets and screen-dump scratch
files and are removed when the launcher exits.

## Build the working NAND

Supply a diamond3 logical NAND and the Onyx release image
`u-boot-5.8d44-onyx-0NUR.imx`:

```sh
mkdir -p .qemu/onyx
python3 boards/onyx/onyx_env.py \
  .qemu/nand-state.bin .qemu/onyx/nand-state.bin \
  --uboot /path/to/u-boot-5.8d44-onyx-0NUR.imx
```

The source is read-only and the output must not already exist. The helper
embeds U-Boot with IVTs at NAND offsets `0x100400` and `0x280400`, then
builds a UBI partition containing `env0`, `env1`, `system-config`, `root0`
and `root1`.

The redundant 64 KiB environments retain only `bootside` and `brightness`;
`nlmodel` is set to `Display-4.x`. An absent `bootside` or `brightness`
defaults to zero. The three filesystem volumes are copied block-for-block,
including the source's per-unit system state and diamond3 firmware. UBIFS
contents and journals are not edited. Other volumes are omitted. Treat the
working image as sensitive because it includes the source's system state.

The early `Using default environment` banner is normal: this firmware
defers importing stored variables until its environment-sanitization step.
The copied roots contain diamond3 FIT configurations and signatures, not
a bootable Onyx Linux image or an unlock token for the emulated UID.

## Boot and UART behaviour

The ROM stub reads the NAND IVT and boot data, loads U-Boot into DDR, and
jumps to its entry point after device reset. It initializes the ARM PLL to
792 MHz; U-Boot divides it to 396 MHz. DCD execution, physical DDR training
and ROM HAB authentication are not modelled. The MMDC acknowledges LPDDR2
configuration and mode-register reads; GPMI/BCH exposes ONFI geometry.

The emulated UID is `51454d5500000001`, not a physical unit's identity.
Read-only shadow fuses select closed security mode and supply synthetic
thermal calibration; the fixed sensor sample gives a reported 49 C.

With no valid unlock token, U-Boot prints
`Unlocking token not present or invalid (-1)`, sets uninterruptible
autoboot and disconnects UART1 by muxing its TX/RX pads to GPIO. Serial
output stops there, but the CPU continues booting with FIT signature
verification enabled. Reset restores the initial mux state and U-Boot
sets up the UART again.

IOMUXC stores its mux, pad-control and input-select registers and routes
UART1 DCE using its TX/RX pads and receive daisy selector. Other peripheral
routes, electrical pad characteristics and IOMUXC GPR behaviour are not
modelled. No console-unlock bypass is implemented.
