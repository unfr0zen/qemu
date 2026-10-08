# QEMU emulator (`-M flintstone`)

This repo builds a QEMU machine for the Nest Display-3.4 board (`diamond3`,
i.MX6SoloX) that boots the device's own firmware from a NAND image:
U-Boot 2014.04, the 4.1.15 vendor kernel and the UBIFS root file system,
with no emulator-specific image. The unit reaches `login:` in about 30 s,
draws nlclient's UI, reads a live wheel, button and backplate, and joins
WiFi with internet access. The hardware facts the model is built from
(`fdt@3`, the U-Boot IVT/DCD, the kernel config) are in
[`spec-flintstone.md`](spec-flintstone.md). The gen 2 has a machine of its own,
`-M j49` ([`qemu-j49.md`](qemu-j49.md)).

## Quick start

Build and boot. There is no NAND image in this repo: bring your own
logical NAND image ([what it must be](nand-image.md)) and pass it to the
first boot with `--nand`, which seeds the unit's working copy from it. The
host needs Docker and `python3` (macOS and Linux alike; QEMU builds and
runs in Docker).
The unit boots to the thermostat, with a root login (root / nest), WiFi
joined, setup done.

```sh
./setup.sh docker               # clone QEMU v11.0.0, apply patches/, build QEMU (~15 min once)
./run.sh --nand nand-logical.bin  # first boot: seed .qemu/nand-state.bin from the image
./run.sh                        # later boots; console on the terminal, Ctrl-A X quits
./run.sh --ui                   # also serve the device as a web page (URL printed)
```

`setup.sh` without `docker` builds natively (Linux, or macOS with Homebrew
dependencies). `run.sh` uses whichever build exists, running the Docker one
in the same image.

## `run.sh` options

| Option / variable | Effect |
| --- | --- |
| `--ui` | Serve the web page (default `http://127.0.0.1:8075`); the terminal stays a console too (Ctrl-] or Ctrl-A X quits) |
| `--ui-port N`, `UI_PORT=N` | Web page port |
| `--cloud` | Let the unit reach the internet; by default it gets DHCP (10.0.2.x) and nothing past the virtual router |
| `--no-provision` | Skip the pre-boot: boot the NAND exactly as it is |
| `--nand FILE` | The NAND image to start from: seeds the working copy when there is none yet (or with `NAND_FRESH=1`), and is the source for `NAND_SNAPSHOT`, `NAND_RW` and `--stock-image`. Ignored, with a note, once a working copy exists |
| `--mqtt HOST[:PORT]` | Relay `10.0.2.100:1883` in the guest to an MQTT broker, and nothing else (for nestlocal, imx6-stuff's `ha/`) |
| `--nic-extra OPTS` | More `-nic user` options, e.g. a `guestfwd=` |
| `--stock-image FILE` | Write FILE, the image as a unit that was never set up (below), and exit |
| `--root0`, `--root1` | Boot that system-update slot first from now on, as an update switches it (U-Boot's `bootcmd`; on the gen 3 through the pre-boot, so not with `--no-provision`). Without either, the unit boots the slot it chose itself |
| `NAND_FRESH=1` | Discard the NAND working copy and start again from `--nand` |
| `NAND_SNAPSHOT=1` | Throwaway run: nothing written survives |
| `NAND_RW=1` | Write straight into the `--nand` image; never point it at your only copy |
| `NAND=path` | Same as `--nand` (the flag wins) |
| `QEMU_ICOUNT=shift=3` | Instruction-paced, deterministic time instead of the host clock (minutes to boot) |
| `QEMU_FORCE_DOCKER=1` | Run a native build through Docker as well |
| `QEMU_DOCKER_ARGS=...` | More options for the `docker run` (a network or namespace to join, `--cap-add NET_ADMIN --device /dev/net/tun` for a tap, …) |
| anything else | Passed to QEMU (`-d guest_errors`, `-s -S`, a `-nic` of your own, …) |

## NAND state

The machine boots from a logical NAND image: data pages only, the BCH
parity removed ([NAND image](nand-image.md)).
The GPMI/BCH model recreates the controller's view, bad-block-marker swap
included, and writes like NAND does (program ANDs into the page, erase
fills `0xff`).

By default `run.sh` boots a working copy, `.qemu/nand-state.bin`: a
reflink clone of the `--nand` image, made on first use. Whatever the
firmware writes (settings, saved networks, UBIFS journal recovery, SSH host
keys) survives across boots, as on a real unit. The image itself is never
written. Once the working copy exists, `run.sh` needs no `--nand`;
`NAND_FRESH=1 ./run.sh --nand FILE` starts again from FILE, and
`NAND_SNAPSHOT=1` boots a scratch clone remade on each run. Without a
working copy or a `--nand`, `run.sh` stops and says so.

### A never-set-up unit

`./run.sh --nand FILE --stock-image .qemu/nand-stock.bin` writes the image
as it would look on a unit fresh from the factory: its firmware (`root0`,
`root1`), U-Boot environment and factory `provision` volume byte for byte,
and nothing of the owner or of its use. The volumes the running firmware
writes ([where the state is](nand-image.md#where-the-running-firmware-keeps-its-state):
`system-config`, `user-config`, `data`, `log`, `scratch`) are emptied by
the unit's own kernel with `ubiupdatevol -t` (`provision.py --factory`),
which erases every block they held, so no deleted data stays in the flash
either. At its next boot the firmware lays an empty file system on each,
generates new SSH host keys, and nlclient starts the out-of-box setup at
its language list.

Boot it with `NAND_SNAPSHOT=1 ./run.sh --nand .qemu/nand-stock.bin
--ui --no-provision` to keep it as it is (the root account is locked, as
shipped), or without `--no-provision` for the usual login, WiFi and
skipped setup.

## What boots

The machine's boot ROM stub loads U-Boot from NAND the way the i.MX boot
ROM does: it reads the image's IVT and loads what its boot data describes
(5.9.4-5's U-Boot: `mtd0` +0x100000, 0x61000 bytes to 0x877ff000) and
starts it at the IVT's entry, having applied the image's DCD (clocks,
pads, DDR controller) first, as the ROM does.
U-Boot runs its stock `bootcmd`: UBI attach, `/boot/kernel_fdt.itb` from
the active root file system (`root0` on a unit never updated),
`bootm #Display-3.4@3` ([how the firmware boots](nand-image.md#how-the-firmware-boots)). Linux then brings up
UBIFS, udev, monit, D-Bus, WiFi, sshd and the Nest applications.

On TCG with the host clock (the default), U-Boot reaches the kernel in
about 9 s, `wlan0` appears at about 8.5 s of kernel time, WiFi is joined at
about 10 s, and `login:` arrives at about 30 s.

## The web page (`--ui`)

`run.sh --ui` serves the product drawn after the 3rd-generation Nest, with
nothing in the firmware aware of it:

- the screen: QMP `screendump` polled at 100 Hz, faster than the panel's
  60 Hz, so every frame the unit shows reaches the page (a slow browser
  skips to the newest one instead of falling behind). The Screen panel shows
  the device's fps (new frames on its panel per second) next to the page's;
- the ring: drag, scroll or the arrow keys turn it, 100 px of scroll is
  one detent;
- the glass: click, Enter or Space press it, and it can be held;
- an interactive console (xterm.js). The terminal that started `run.sh`
  is the same console: type in either, both show everything, and Ctrl-C
  goes to the unit. Ctrl-] quits QEMU, and so does Ctrl-A X as without
  `--ui` (Ctrl-A Ctrl-A sends a Ctrl-A);
- the board lines the firmware drives (power-off, WiFi reset, Hall power,
  battery divider, piezo enable);
- the piezo: the clicks and tones the firmware plays (pwm-beeper on PWM1;
  a click is 2 kHz for 3 ms), as a square wave in the browser, timed by
  the guest. The header's "sound" toggle turns it on; browsers play
  nothing before a click on the page.

Everything is a machine property or a chardev on localhost TCP ports
(QEMU runs in a container, and unix sockets do not cross Docker Desktop's
file sharing). `run.sh` prints the ports. The HMP monitor sits on one of
them.

`ui/backplate.py` plays the backplate MCU on the sensor UART. It
answers the front unit's reset with the wiring (`--wires`, default
`Rh,C,W1,Y1,G`) and the identity of an up-to-date Backplate-6.x image, and
streams ambient light, temperature and motion every 2 s so the display
stays lit (`--no-presence` lets it sleep). The temperature is a room's: it
follows the circuits the unit switches (`0x82`, circuit and state), rising
while heat is on (W1, AUX/W2, a heat pump's compressor), falling while
cooling is, and otherwise drifting towards 16 °C (`--ambient`,
`--heat-rate`, `--cool-rate`, `--room-tau`); each switch is acknowledged
(`0x06`), or nlclient's backplate controller stops there. It also keeps a
sample every 30 s and hands them over when the front flushes (`0xa2` →
`0x22` TEMP_BUF…, `0x2f` END_BUF): nlclient's history is built from those,
and without them its time to temperature has nothing to start from and
stays unknown. It takes firmware updates the
way the MCU's bootloader does (`nlbpupdater`'s S-record transfer, which a
system update runs) and then reports the flashed image's identity, read
from the image itself. What was flashed is kept like the MCU's flash, in
`<NAND image>.backplate` next to the image, and travels with it when
`run.sh` copies the NAND (a fresh state, a snapshot): without it, a unit
updated to 6.4-8 (which ships TFE 2.3.27) would find 5.9.4-5's 2.3.11
at every boot, start a backplate update, and open on "The wiring to your
equipment has changed". Its docstring documents the wire format and message types;
`--debug` logs every frame.

`ui/em35x.py` plays the 802.15.4 / Thread radio, the EM35x on ECSPI3
running Silicon Labs' ConnectIP network co-processor (`ip-modem-app`
2.0.4 build 301, the image both 5.9.4-5 and 6.4-8 ship), which wpantund's
silabs plugin drives through `spi-server`. The chip's SPI port is the QEMU
device (`em35x-ncp`: the reply laid out as nSSEL falls, nHOST_INT, deep
sleep and its wake-up), and the script is the application behind it: it
boots on nRESET, answers the management commands with what the chip
sends back, scans with the chip's timing, and, alone on the air, forms a
network of its own and leads it when nlclient asks for one. That network
is kept like the chip's flash tokens: across nRESET and reboots, not
across a restart of the script. Its docstring lists the commands and
replies; `--debug` logs the stream. Without `--ui` no chip is attached and
the firmware sees the radio as absent.

## WiFi and network

A BCM43341 sits behind an SDIO card on usdhc2. The stock `dhd.ko`
(bcmdhd 1.141.66.4) loads the stock firmware (`43341b0`, 6.30.190.36) into
it (6.4-8 drives it with brcmfmac, backported from Linux 4.12, instead), and the unit sees one open access point: SSID `QEMU`, channel 6, BSSID
`02:43:34:10:00:01`. `run.sh` puts QEMU's user-mode network behind it
(`-nic user,model=bcm43341,restrict=on`), so the unit is 10.0.2.15 with a
default route through 10.0.2.2 (set by the pre-boot, below), and nothing
past that. `--cloud`
lifts `restrict=on`. Online, the unit signs in to Nest's service as the
device the image came from and takes on that account's state: °F, and
firmware updates, which it downloads and installs into
the NAND. Offline, every checkout boots the same. A `-nic` or `-netdev` of
your own replaces it.

The unit joins at boot on its own: connman only autoconnects to networks
it has saved, and the pre-boot (below) saves this one.

On the guest, networking is connman plus wpa_supplicant over D-Bus. The
image has no `connmanctl`, `wpa_cli` or `udhcpc`, so by hand:

```sh
dbus-send --system --print-reply --dest=net.connman / \
    net.connman.Manager.GetServices
dbus-send --system --print-reply --dest=net.connman \
    /net/connman/service/wifi_<mac>_51454d55_managed_none \
    net.connman.Service.Connect
```

The chip model answers the ioctls and iovars `dhd_preinit_ioctls` and
`wl_cfg80211` use; unknown gets read zero. It declines tx glomming, host
reordering and proptxstatus, so every frame is a plain SDPCM frame. It
only drives its out-of-band interrupt (GPIO5_IO15, level high) once the
driver routes it there through SEPINT: bcmsdh requests the IRQ before it
can mask it, so a line already high at that point would storm the CPU.
`-global bcm43341.trace=on` logs every backplane register access and every
ioctl (add `-D file` to keep it off the console).

## The pre-boot: login, screen, WiFi, setup

A unit straight from its image has root locked, has no saved network and starts in the out-of-box setup. Before every boot, `run.sh`
runs [`provision.py`](../provision.py), a short (~8 s) boot of
its own. It stops U-Boot, starts the stock kernel with `init=/bin/sh`, and
has that shell:

- give root the password `nest` if the account is locked;
- keep all but critical kernel messages off the console (`kernel.printk
  = 3 4 1 7` in `/etc/sysctl.conf`, which the unit applies early in boot,
  before any driver loads): the backlight driver logs two info messages
  at every brightness change, and 6.4-8's brcmfmac logs its routine ones
  as errors. `dmesg` still has them all;
- save the WiFi network in connman's store,
  `connman/wifi_<mac>_<ssid hex>_managed_none/settings` on `user-config`
  (the MAC is U-Boot's `ethaddr`), with slirp's addresses set by hand
  (10.0.2.15/24, gateway 10.0.2.2, DNS 10.0.2.3). slirp's DHCP leaves the
  guest without a gateway, and a connection forwarded in (`--ssh`) then
  gets no answer: ssh hangs at the banner. A network an older pre-boot
  saved with DHCP is rewritten;
- mark the out-of-box setup done, with the settings the phone app's
  ConfigDone handler writes (every `*_completed` flag, Eco onboarding,
  language, location; the values are `skip-setup.py`'s);
- save the wiring `backplate.py` reports as the unit's own (`hvac_pins`,
  `hvac_wires` in `settings.config`). nlclient opens on "The wiring to
  your equipment has changed" whenever the backplate's pins differ from
  the saved ones, and the image's owner may have had other wires (W1, C, Rh, STAR);
- report whether nestlocal (imx6-stuff's `ha/`) is installed on that root:
  `--mqtt` only relays the broker to it, so without imx6-stuff's
  `ha/qemu_install.py` it changes nothing.

Anything already in place is left alone, and the result is the same for
any checkout and any NAND image, since nothing is kept on the host. The
device's own kernel does the writes, so the UBIFS journal is honoured.
That's the reason not to rebuild volumes from the host: `ubi_reader` reads
the index only, and lists files the journal has since deleted. If the
pre-boot fails, the boot goes on without it;
`--transcript FILE` saves its console.

To do it by hand: U-Boot's prompt needs no login, so a shell comes from
booting the stock kernel with `init=/bin/sh`. Stop autoboot on the console,
`printenv bootcmd`: the first command it runs boots the active root file
system (`nandboot0`, `root0`, on a unit never updated; `nandboot1` after a system
update). Print that one and type it back with `init=/bin/sh` added to its
bootargs, e.g. for `root0`:

```text
ubifsmount ubi0:root0 && ubifsload ${fit_addr} /boot/kernel_fdt.itb && setenv bootargs console=${console},${baudrate} mtdoops.mtddev=${mtdoopsdev} ubi.mtd=ubipart root=ubi0:root0 rootfstype=ubifs rootwait lm3695_bl.default_brightness=${brightness} init=/bin/sh && bootm ${fit_addr}#${nlmodel}
```

That shell has no `/proc` until you mount it (`mount -t proc proc /proc`,
needed for `mount -o remount,rw /`). busybox `login` rejects empty
passwords, and a DES crypt avoids the `$` the console shell would expand:
`root:saiHAK.ldgCRI:…` is "nest".

On a running unit, as root and with `run.sh --ui` up,
`./skip-setup.py` does the setup step live.

## Display and GPU

The LCDIF model scans the guest framebuffer out as a QEMU console at
480x640 32 bpp, and raises the frame-done and vsync interrupts the NXP
mxsfb waits on. nlclient draws with GLES2 through the unit's own Vivante
stack (galcore 5.0.11, `libGAL`/`libEGL`/`libGLESv2`), unmodified, on a
model of the i.MX6SX's GC400T GPU3D (`hw/display/vivante_gc.c`,
`vivante_3d.c`):

- the chip's identity and feature words, the command front end and its
  events, the MMU;
- the resolve engine: fills and tiled/supertiled/linear copies with format
  conversion, which put each frame into the framebuffer and signal
  libGAL's fences;
- tile-status fast clears;
- a software 3D pipeline: vertex fetch, an interpreter for the Vivante
  shader ISA, clipping, rasterisation (split across host threads), texturing
  and the pixel engine (blending, depth/stencil, alpha test).

While the UI animates the unit renders about 50 frames a second; the web
page shows what its screendumps catch of them. `-global
vivante-gc.trace=on -D FILE` logs every register access, command and
resolve; anything the model does not cover is reported once with `-d unimp`.

## Hardware model

The machine is a patch series on QEMU v11.0.0 (`98b060d`) in
`patches/`, applied to the `qemu-src/` clone by `setup.sh`.

| Patches | Model |
| --- | --- |
| 0001–0005 | i.MX6SX SoC container (Cortex-A9, GIC, the 6SX memory map from `fdt@3`) and the `flintstone` machine |
| 0006, 0014 | GPMI NAND controller with APBH DMA and BCH |
| 0007 | Boot ROM stub: loads U-Boot from NAND, resets the CPU on watchdog reboot |
| 0008, 0009 | SDMA and MMDC stubs, enough for their probes |
| 0010 | PFUZE200 PMIC on i2c4 |
| 0011, 0016, 0018, 0019 | vf610 ADCs: the magwheel's two Hall channels (`wheel-position`, `-global imx6.adc.wheel-degrees=N`), the battery divider |
| 0012, 0013, 0017 | LCDIF scanout with its interrupts, and the SoC wiring of the above |
| 0015 | `flintstone.board`: the button, and the firmware-driven lines, over a chardev |
| 0020–0022 | SDIO card (CMD5/52/53, card interrupt, power line); the i.MX uSDHC keeps its interrupt enables through a reset-all, as the vendor kernel's `esdhc_hw_reset()` assumes |
| 0023, 0024 | BCM43341 WiFi chip, and its wiring: GPIO1_IO04 power, GPIO5_IO15 interrupt |
| 0025 | The i.MX I2C controller completes a NAKed byte with its interrupt (no timeout per transfer to an absent device) |
| 0026 | LM3695 backlight driver on i2c2 0x63, a register file for the vendor `lm3695_bl` driver |
| 0027 | Vivante GC400T GPU3D: front end, MMU, resolve engine, fast clear and a software 3D pipeline for the stock galcore stack |
| 0030, 0031 | Boot ROM stub: loads the image fw1's IVT describes (a system update's U-Boot is larger), and applies its DCD |
| 0032 | `em35x-ncp`: the EM357 radio's SPI slave (ECSPI3) and its nRESET, nWAKE, nHOST_INT lines, on a chardev |
| 0033 | `em35x-ncp`: the chip's SPI port (reply framing, nHOST_INT, deep sleep); the chardev carries the byte stream |
| 0034 | BCM43341: the channel answers brcmfmac needs (chanspec, chanspecs by bandwidth, bw_cap, rxchain, country) |
| 0035 | Vivante rasteriser: a pixel on an edge two triangles share is drawn exactly once (no dotted seams) |
| 0036 | i.MX PWM1-4 (the registers `pwm-imx` programs), PWM1's frequency and the piezo enable (GPIO1_IO05) reported on `flintstone.board` |

## Debugging

- The kernel console on `ttymxc0` is the first stop; the vendor U-Boot
  compiles most of its messages out.
- `-d guest_errors` for model complaints, `-d unimp` for accesses to
  unmodelled registers, and `-trace sdhci_*` or `-trace sdcard_*` for SD.
  Add `-msg timestamp=on` to find stalls.
- `-s -S` for gdb. Use TCP, publish the port from Docker, or attach from a
  second container with `--network container:<qemu>`. Kernel symbols:
  `vmlinux-to-elf --base-address 0xc0008000` on the FIT's kernel. The
  printk ring (`__log_buf`) is found by searching memory for
  "Booting Linux".
- `-d nochain,exec` or `-d in_asm` identify spin loops by PC.
- Read the driver source first: bcmdhd, the NXP 4.1.15 drivers, and
  U-Boot v2014.04 are all public.

## Developing the machine

Work in `qemu-src/` (branch `flintstone`), commit, and regenerate the
series; the patches are the source of truth:

```sh
cd qemu-src
git format-patch 98b060d..HEAD -o ../patches/
cd .. && ./setup.sh docker    # incremental rebuild, about a minute
```

A `qemu-src/build*` configured at another path (say, before this repo's
layout moved) bakes that path in: remove it and run `setup.sh` again.

## Not modelled yet

- Other Thread nodes. The radio is alone on the air: scans hear nothing,
  a join finds no parent, and the frames the unit sends to its fabric
  (Protects, a Heat Link) go unanswered.
- The radio's bootloader: an NCP firmware update (`zb-loader
  --app-easyload`) fails. None is needed, as the chip reports the version
  the firmware ships.
- USB device mode (the ChipIdea gadget): no ACM console from the
  emulator (`ci_hdrc.0: no supported roles`).
- ENET: QEMU warns `nic imx.enet.0 has no peer`, which is harmless because
  the guest does not use it.
- Cosmetic: U-Boot reports an i.MX6Q (the OCOTP reads zero) and cannot
  read the temperature.
- The NAND ID gives a 64-byte OOB; the real part is 2048+128 (it would
  need an ONFI parameter page).
