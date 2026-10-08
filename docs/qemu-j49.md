# QEMU emulator, gen 2 (`-M j49`)

`./run.sh --gen2` boots the Nest Learning Thermostat gen 2 (`j49`,
Display-2.x, TI AM3703) from its own NAND image in a QEMU machine of its
own: the GP x-loader, U-Boot 2012.10, the 2.6.37 kernel and the JFFS2 root
file system, unmodified. The unit draws nlclient's UI, reads its ring,
button and backplate, clicks its piezo, joins WiFi and forms its Thread
network. It is the gen 3's emulator ([`qemu.md`](qemu.md)) for the other
generation: the same `run.sh`, web page, pre-boot and stock image, on a
different SoC. The hardware facts the model is built from are in
[`spec-j49.md`](spec-j49.md).

## Quick start

The gen 2 boots from its own image, which carries its unit's data and
credentials, so none is in this repo. Provide one in the raw chip format
as `nand.raw` ([the format](nand-image.md#gen-2-j49)). Then:

```sh
./setup.sh docker                               # once, as for the gen 3 (the series builds both machines)
./run.sh --gen2 --nand nand.raw                 # first boot: seed .qemu/j49-nand-state.raw
./run.sh --gen2                                 # later boots; console on the terminal, Ctrl-A X quits
./run.sh --gen2 --ui                            # also serve the device as a web page (URL printed)
```

Without `--nand`, the first boot also looks for
`.qemu/nand.raw`.

A `qemu-src/` set up before the gen 2 series existed has the gen 3's
patches only (`setup.sh` applies the series to a fresh clone): remove it and
run `setup.sh` again.

Every option of [`run.sh`](qemu.md#runsh-options) works with `--gen2`:
`--ui`, `--cloud`, `--no-provision`, `--mqtt`, `--nic-extra`,
`--stock-image`, `--root0`/`--root1`, `--nand`, `NAND_FRESH`,
`NAND_SNAPSHOT`, `NAND_RW`, `NAND=`.

`--root1` boots the other system-update slot: `root1`, which on a unit
NoLongerEvil installed is still the stock firmware (its installer rewrote
`root0`). It reorders `bootcmd` in the unit's environment as a system update
does, so the unit keeps booting it until `--root0`.

`--mqtt HOST` is nestlocal's (imx6-stuff's `ha/`): install it once with
`python3 ha/qemu_install.py --gen2` there, then `./run.sh --gen2 --ui --mqtt
HOST` (the pre-boot says whether it is installed). The emulated unit then
appears in Home Assistant as a "Nest Thermostat" (`nestlocal_<serial>`),
and setpoints and modes from there reach nlclient and come back
acknowledged. On a unit NoLongerEvil installed, its own integration
publishes the real unit under the same serial (`nolongerevil_<serial>`):
two devices, of which the nestlocal one is the emulator. `--cloud` lets the unit out to the internet as on the gen 3, but
its service is gone: Google ended the gen 2's in October 2025, and a unit
NoLongerEvil installed calls `homeassistant.local`, which QEMU's user-mode
network cannot resolve.

## NAND state

The machine boots the raw NAND: 2112-byte
pages, data then OOB, with the GPMC's BCH4 parity in the OOB.
The GPMC model recomputes the ECC on every page the firmware writes, and
writes like NAND does (program ANDs into the page, erase fills `0xff`).

A few sectors on an image can carry flipped bits in their stored parity itself;
the kernel's BCH decoder fails on those (`BCH decoding failed` on the
console) and JFFS2 reads the data as it is, as it did on the unit.

By default `run.sh --gen2` boots a working copy,
`.qemu/j49-nand-state.raw`, a reflink clone of the `--nand` image made
on first use; `NAND_FRESH=1 ./run.sh --gen2 --nand FILE` starts again from
FILE.

### A never-set-up unit

`./run.sh --gen2 --nand FILE --stock-image .qemu/j49-stock.raw` writes
the image as a unit fresh from the factory. The unit's own kernel erases
`user-config`, `data`, `log` and `scratch` with `flash_eraseall`, and
empties `system-config` down to the one file the factory put there: the
WL1271's calibration, `wl1271-nvs.bin`. At its first boot the wlan init
script turns that into the driver's `wl12xx-nvs.bin` (`TI NLCP upgrade`),
the firmware makes new SSH host keys, and nlclient opens the out-of-box
setup at its language list. Erasing the calibration too would make a unit
no factory shipped: the WL1271 firmware would not boot.

## What boots

The machine's boot ROM does what the AM3703's does for a GP device booting
from NAND: it reads the image at block 0 (a size and load address, then the
code) into SRAM at `0x40200000` and runs it. That is the unit's x-loader,
which loads U-Boot 2012.10 to SDRAM; U-Boot boots `nandboot0`: the
kernel from `boot0` and `root=/dev/mtdblock7` (`root0`).

The release U-Boot is silent (its built-in `silent=1` blanks the first
`console=` it passes), so the pre-boot gives the kernel a console the way
`fw_setenv` would: the environment's `addmodel` appends
`console=ttyO0,115200` ([`j49_env.py`](../j49_env.py) edits the
redundant environment on the image and rewrites its pages' ECC). The
firmware is unchanged.

## The web page (`--ui`)

The page is the gen 3's ([what it shows](qemu.md#the-web-page---ui)), with
the gen 2's board behind it:

- the screen: the DSS's 320x320 panel, drawn smaller than the gen 3's
  (1.75" across to its 2.08"), with the wider black border around it;
- the ring: the ADBS-A320 optical sensor under it. A step of the page's
  wheel (24 to the turn) is 100 sensor counts, a 15° turn of a ring the
  sensor reads at about 2400 counts to the turn, fed in a few counts at
  each of the driver's 60 Hz reports as a turning ring would. That is a
  menu item per three steps (the menu's icons sit about 51° apart) and
  half a degree per step on a slow dial, faster on a quick spin, with
  nlclient's click per item or half-degree. Clockwise is clockwise;
  nlclient swallows the first bit of a turn back (rotaryhysteresis);
- the glass: the ring's click is the TPS65921's PWRON (`twl4030_pwrbutton`,
  KEY_POWER);
- the board lines: the battery disconnect (GPIO 161), the WL1271 enable
  (GPIO 103) and the piezo enable (GPIO 58, active low);
- the piezo: pwm-beeper on GPTIMER11's PWM, reported as its frequency. A
  click is 2 kHz for 3 ms (product.config).

`ui/backplate.py` plays the backplate MCU on UART3 (`/dev/ttyO2`),
as on the gen 3.

`ui/em35x.py --gen2` plays the EM357 Thread radio on McSPI2 (spidev
2.0: chip select 0, nRESET on GPIO 61, nHOST_INT on GPIO 182, no nWAKE
line). The gen 2 runs the same ConnectIP `ip-modem-app` 2.0.4 build 301 as
the gen 3, built for the EM357 (`Sep 23 2015 16:39:22`), whose command
table has `SetTransmitHookActive` (0x6991) where the gen 3's has
0x6993/0x6994; `--gen2` reports that build and answers that command. The
radio's EUI-64 is the unit's own, from its environment's `hwaddr1`.

## WiFi and network

A TI WL1271 sits behind an SDIO card on MMC2. The unit's `wl12xx.ko` (TI
OpenLink `ol_R5.SP4.01`, compat-wireless r5.00.15) uploads
`wl127x-fw-4-sr.bin` and the NVS into it, and mac80211 does the MAC: scans,
authentication and association are 802.11 frames the driver sends through
the chip. The chip model (`wl1271`) is the firmware's side of the host
interface: the partition windows and ELP sleep control, the top (OCP)
registers, the boot handshake (`INIT_COMPLETE`, the command and event
mailboxes, the static data with the firmware's version, `Rev 4.0.0.0.4`),
the commands (an `INTERROGATE` of the memory map, scans, roles,
remain-on-channel), the event mailboxes, the firmware status block, TX
aggregates with their result ring, RX batches. Behind it is one open
access point: SSID `QEMU`, channel 6, BSSID `02:12:71:10:00:01`, which
answers probe requests, authentication and association and bridges data
frames to the NIC as Ethernet.

`run.sh --gen2` puts QEMU's user-mode network behind it
(`-nic user,model=wl1271,restrict=on`): the unit is 10.0.2.15 (set by the pre-boot),
and `--cloud` lifts `restrict=on`, as on the gen 3. `-global
wl1271.trace=on` logs the commands, events and interrupt causes.

## The pre-boot

`provision.py --gen2 NAND` does the gen 3's pre-boot ([the
steps](qemu.md#the-pre-boot-login-screen-wifi-setup)) on the gen 2. There
is no U-Boot prompt to stop at, so the shell comes from the environment:
`addmodel` gets `init=/bin/sh` for that one boot and is put back after. The
writable partitions are JFFS2 on MTD (`user-config` is `mtdblock11`), and
the root (`root0`, `mtdblock7`) is remounted read-write for `/etc`.

Root gets the password `nest` whatever it had: unlike a gen 3 image, a
gen 2 image can carry a password nobody here knows (NoLongerEvil's
installer rewrote `root0` and set one).

The gen 2's `inittab` runs no getty, so the pre-boot adds one on `ttyO0`
for the console's `login:`, as the gen 3 has. It goes ahead of `rcS`, as
a loop of its own: busybox init starts respawn entries only once `rcS`
returns, and NoLongerEvil's never does (it ends in `sshconnect`, which
keeps a reverse SSH tunnel up, through `autossh`, to whatever host
`/media/dropbox/host.txt` names). The unit also answers SSH (dropbear on
port 22) with the same login.

## Hardware model

The machine is the second half of the series in `patches/`, one file
per patch, applied after the gen 3's (0001–0036):

| Patches | Model |
| --- | --- |
| 0037–0039 | OMAP3 interrupt controller (INTCPS) |
| 0040–0042 | GP timers 1–12 (and GPTIMER11's PWM frequency, for the piezo) and the 32 kHz sync counter |
| 0043–0044 | PRCM (CM, PRM), control module, SDRC/SMS |
| 0045–0048 | GPMC with an x16 MT29F2G16 NAND, its prefetch engine and BCH4 |
| 0049–0051 | TPS65921: RTC, MADC (the battery on ADCIN0), interrupts, PWRON, power-off |
| 0052–0054 | UARTs |
| 0055–0057 | I2C controllers |
| 0058–0060 | GPIO banks |
| 0061–0062 | MPU watchdog (WDT2) |
| 0063–0065 | MMCHS around the SDHCI core, with its system-DMA requests |
| 0066 | SDIO card: a voltage window (MMC2 is 1.8 V), and function 0's space past the card's own for the chip (the WL1271's ELP control) |
| 0067–0069 | McSPI, master mode, paced as the shift register is, with its DMA requests |
| 0070–0072 | System DMA controller (SDMA) |
| 0073–0074 | HS USB OTG as an unplugged peripheral |
| 0075–0078 | DSS (DISPC scanout, DSI) and the LM3530 backlight |
| 0079–0080 | ADBS-A320 ring sensor |
| 0081–0084 | WL1271 wifi chip |
| 0085–0086 | The AM3703 (OMAP3630) SoC and its boot ROM |
| 0087–0088 | `j49.board`: the button and the firmware's lines over a chardev |
| 0089–0091 | The `j49` machine |

## Debugging

- The console is the first stop; the pre-boot keeps all but critical
  messages off it, and the unit's syslog (`/var/log/messages`, kept on the
  `log` partition) has them all.
- `-s -S` for gdb, as on the gen 3. The kernel's printk ring is 16 KiB
  (`CONFIG_LOG_BUF_SHIFT=14`).
- `-d unimp,guest_errors`: the SoC maps its unmodelled L3/L4 regions as
  unimplemented devices, so the accesses are logged with their offsets.
- The driver sources are public: Nest's 5.9.4 GPL release (Linux 2.6.37,
  U-Boot 2012.10) and TI's wl12xx.

## Not modelled yet

- Other Thread nodes, and the radio's bootloader, as on the gen 3.
- The panel's SPI side: the Tianma panel's ID reads `00 00 00`.
- USB: the MUSB port reads as unplugged.
- The WL1271's PLT (calibration) mode: the calibrator's `autocalibrate`
  would produce an empty NVS, which the stock image avoids by keeping the
  factory's.
