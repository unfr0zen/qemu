# QEMU emulator, gen 1 (`-M diamond`)

`./run.sh --gen1` selects a Diamond board around the same AM3703 SoC used by
the gen 2 `j49` machine. The machine requires a **raw 256 MiB NAND image**:
2112 bytes per page (2048 data and 64 OOB), with the chip's BCH4 ECC in the
OOB. No Diamond NAND image is in this repository.

```sh
./run.sh --gen1 --nand diamond.raw  # seed .qemu/diamond-nand-state.raw
./run.sh --gen1                     # reuse that working copy
./run.sh --gen1 --ui                # console, display, ring, board controls
```

The model uses the OMAP3 boot ROM, GPMC/NAND, TPS65921, WL1271, DSS, and
ADBS ring devices already used by `j49`. Diamond routes ring motion through
the sensor's X axis, uses a PWM backlight instead of the j49 I2C LM3530, and
places its CC2530 radio on UART2 rather than j49's EM357 on McSPI2. With `--ui`,
`ui/diamond_radio.py` connects to UART2 and prints the guest's transmitted
bytes. It does not yet answer radio commands. The existing gen 2 backplate
helper is used as a provisional UART3 peer.

The machine targets a production Diamond-1.9/1.10 board. Earlier revisions
include a 320×480 panel and other board wiring. The Diamond radio protocol,
backplate identity, PWM backlight behavior, and complete stock boot remain
unverified without a Gen1 NAND dump. Gen1 provisioning is disabled; the
working NAND copy is not changed by a pre-boot. `--stock-image` is unavailable.

The Nest-Matter firmware bundle contains separate x-loader,
U-Boot, kernel, and JFFS2 rootfs binaries. The x-loader binaries begin with
ARM instructions, not the GP image header the NAND boot ROM reads from block
zero. The extracted rootfs has both Diamond and Display radio control code,
but its `wpantund.conf` selects `spi-server` on `/dev/spidev2.0`, and its
product configuration names the j49 I2C backlight at `3-0036`. It is a
Gen2-configured rootfs, not a Diamond raw NAND dump. Its `etc/init.d/ncp`
selects CC2530 power and reset control for `Diamond-*` and EM357 control for
`Display-2.*`; `upgrade-ip-modem-app` uses `/dev/ttyO1` for the CC2530.

Board wiring comes from Nest's [5.9.4 board source](https://nest-open-source.googlesource.com/nest-learning-thermostat/5.9.4/linux/+/HEAD/linux/arch/arm/mach-omap2/board-diamond.c)
and [Diamond x-loader configuration](https://nest-open-source.googlesource.com/nest-learning-thermostat/5.9.4/x-loader/+/HEAD/x-loader/include/configs/diamond.h).
