# Onyx

Nest Thermostat E: i.MX6UL rev1.2, Cortex-A7, 128 MiB LPDDR2 and
512 MiB NAND. The machine provides NAND/UBI, UARTs, LCDIF scanout,
I2C with LM3695 backlight, front button, USB device controller, SDMA
for the ECSPIs (panel, EM35x radio) and the UARTs, and ADC1: the battery
divider on channel 4 (a 3.90 V pack) and the ring's Hall pair on channels
7 and 6, turned through `/machine/wheel-position`, and PWM8 for the piezo
(the UI plays its clicks, 2 kHz for 3 ms). The firmware presents 60 frames
a second. The launcher runs
`ui/backplate.py --board onyx` on UART3 (`/dev/ttysensormcu0`): it reports
the firmware the unit's own backplate ran (mono 1.4.11, BSL 4.0) as model
Backplate-7.19 (the EVT backplate's "X" has no firmware in nlbpupdater's
plist, which fails every system update) and acks the onyx's MCU mode
request (0x8b, ack 0x36). WiFi is the
diamond3's BCM43341 model on usdhc2 (brcmfmac, brcmfmac43340-sdio; WL_REG_ON
GPIO2_IO16, host wake GPIO2_IO17) on QEMU's user network: the unit joins
"QEMU" and gets 10.0.2.15, offline unless `--cloud`; `--ssh PORT` and
`--mqtt HOST[:PORT]` work as on the diamond3. The Thread radio and CAAM
cryptography are not modelled.

## Development Linux

Supply a logical NAND image containing the public Onyx loader and a
`root0` UBIFS volume with `/boot/kernel_fdt.itb`:

```sh
python3 boards/onyx/launch.py --qemu qemu-src/build/qemu-system-arm \
  --nand /path/to/onyx-buildroot.bin --open-security --ui
```

The launcher prints the selected UI URL and a USB host socket. The UI
shows LCDIF scanout, UART console and the front button. The USB socket
speaks [the CI HDRC host protocol](usb-host-bridge-protocol.md).

`--open-security` emulates open SEC_CONFIG fuses. The unchanged production
loader then accepts an unsigned development FIT. Default security fuses
remain closed: without a valid per-unit unlock token, the loader disconnects
UART and retains FIT signature checks. Emulation does not authenticate HAB
signatures or provide an unlock for physical closed devices.

The NAND boot stub loads the IVT image at data offset `0x100000`, sets
boot clocks and jumps to U-Boot. It does not execute DCD or train DDR.

## Mask ROM and USB SDP

A complete 96 KiB i.MX6UL ROM dump can execute at address zero:

```sh
python3 boards/onyx/launch.py --qemu qemu-src/build/qemu-system-arm \
  --nand /path/to/onyx-buildroot.bin --open-security --ui \
  --boot-rom /path/to/onyx-rom.bin \
  --sdp-image /path/to/u-boot-sdp-5.8d44-onyx-0NUR.hab \
  --sdp-fit /path/to/kernel_fdt.itb
```

The ROM enumerates as Freescale `15a2:007d` and processes the host's USB
HID SDP commands. The host loads the FIT at `0x83000000`, uploads the
loader unchanged to its IVT address, and asks the ROM to jump. U-Boot
selects its factory USB boot command; the FIT's initramfs attaches UBI
and mounts the NAND root. The ROM dump, loader and NAND images stay external.
ROM NAND boot with FCB/ECC and closed-device HAB verification are unsupported.

## Stock firmware

```sh
./run.sh --onyx --nand onyx-logical.bin --ui    # first boot
./run.sh --onyx --ui -machine secure-boot=off   # console login
```

The pre-boot (`scripts/provision.py --onyx`) gives root the password
`nest`, allows root over ssh, keeps all but critical messages off the
console, keeps the screen on, saves the `QEMU` WiFi network (with slirp's
gateway and DNS; the MAC is the saved environment's `ethaddr`) and the
backplate's wiring, and marks the setup done; `--no-provision` skips it. It boots
with open fuses for U-Boot's prompt. With closed fuses, the default,
U-Boot disconnects the console UART and the kernel's console is
`/dev/null`, so a console login needs `-machine secure-boot=off`.

This uses `.qemu/onyx/nand-state.bin`. To generate it from an existing
logical NAND and a stock Onyx `.imx` loader:

```sh
python3 boards/onyx/onyx_env.py source.bin .qemu/onyx/nand-state.bin \
  --uboot /path/to/u-boot-5.8d44-onyx-0NUR.imx
```

The source is read-only; output must not exist. This helper copies the
source's `system-config`, `root0` and `root1` volumes, including per-unit
state. A Diamond3 source root is not an Onyx Linux image.

The emulated UID is `51454d5500000001`. Thermal trim and a sensor sample
are synthetic. A watchdog reset reboots the unit (the kernel reboots
through it, a system update included); the short pre-boot boot ignores it.
