# NAND image

This repo carries no NAND image: a stock image carries its unit's data
and its owner's credentials. `run.sh --nand FILE` takes an image you
provide, and seeds the working copy in `.qemu/` from it on the first boot
([NAND state](qemu.md#nand-state)). The machines expect a different format
per generation.

## Gen 3 (`flintstone`)

A **logical** full-chip image: 512 MiB, data pages only (2048 bytes each),
with the BCH parity and the spare area removed and the bad-block-marker swap
undone. The GPMI/BCH model rebuilds the
controller's view of the chip from it.

A raw chip read is not the same thing: its 2176-byte pages hold metadata,
a pad byte, and four interleaved 512-byte data and BCH18 parity chunks
(544 MiB in all). Cutting the last 128 bytes off each page does not give
the logical image: the chunks have to be de-interleaved into it.

### Layout

| NAND partition | Offset | Size | Contents |
| --- | ---: | ---: | --- |
| `u-boot` | `0x00000000` | 4 MiB | FCB, DBBT, two copies of U-Boot 2014.04 (IVTs at `0x100400` and `0x280400`) |
| `ubipart` | `0x00400000` | 500 MiB | UBI volumes |
| `oopsdata` | `0x1f800000` | 8 MiB | Oops storage and BBT |

4096 erase blocks of 128 KiB; `ubipart` is 4000 of them. UBI puts its VID
header at block offset `0x800` and the data at `0x1000`, so a logical
erase block is 126,976 bytes (`0x1f000`). UBI maps those dynamically: a
volume's name is stable, its blocks' addresses are not.

### How the firmware boots

The boot ROM stub loads the U-Boot that fw1's IVT describes
([spec](spec-flintstone.md#stock-boot-image-facts-from-uboot-fw1bin-ivt-at-file-offset-0x400)).
Its `bootcmd` tries `nandboot0`, then `nandboot1`, then `mmcboot`, then
resets. `nandbootN` attaches UBI, mounts `rootN`, loads
`/boot/kernel_fdt.itb` to `0x83000000` and runs `bootm` with the model's
FIT configuration: `nlmodel=Display-3.4`, so `Display-3.4@3` (`kernel@1`
and `fdt@3`), with `root=ubi0:rootN rootfstype=ubifs rootwait`. A system
update switches the order (`run.sh --root0`/`--root1` does the same).

### Where the running firmware keeps its state

The root file system (`root0`, UBIFS) is mounted read-only. Everything the
unit writes at run time goes to five UBIFS volumes that `/etc/fstab` mounts
under `/media` (`sync` except `log` and `scratch`), plus a tmpfs `/tmp` and
`/var/run`. Paths on the read-only root link into them. Sizes are the UBI
volume sizes.

| Volume | Size | Mounted at | Holds | Reached through |
| --- | ---: | --- | --- | --- |
| `env0`, `env1` | 1.1 MiB each | — | U-Boot environment, two copies (CRC and a generation byte) | `uboot-get` / `uboot-set` |
| `root0`, `root1` | 104 MiB each | `/` (ro) | the firmware image; `root1` is the fallback | — |
| `system-config` | 8.1 MiB | `/media/system-config` | per-unit system state: SSH host keys (`ssh`), D-Bus machine id (`dbus`), monit state (`monit`) | `/etc/nestlabs/system`, `/var/lib/dbus`, `/var/lib/monit` |
| `user-config` | 12.1 MiB | `/media/user-config` | what the owner set up: `settings.config` (setup flags, language, location, preferences), connman's saved WiFi networks (`connman`), the time zone (`localtime`) | `/etc/nestlabs/user`, `/var/lib/connman`, `/etc/localtime` |
| `data` | 24.1 MiB | `/media/data` | what nlclient learns and keeps across reboots: schedules, energy history, the learning and control state (`*.recovery`, `*.json`), crash records | `/var/nestlabs/data` |
| `log` | 24.1 MiB | `/media/log` | syslog (`messages`) and nlclient's logs, rotated (`name#N`) | `/var/log`, `/var/nestlabs/log` |
| `provision` | 2.1 MiB | — | per-unit factory data; not mounted | `provision-get` (a link to `plist-edit`) |
| `scratch` | 193.5 MiB | `/media/scratch` | a work area; `/etc/init.d/nestlabs` empties it at boot (core dumps aside) | — |

So a unit's own history is in `user-config` (who set it up, and how) and
`data` (what it learned); `system-config` and `provision` belong to the
hardware rather than the owner. The pre-boot (`provision.py`) writes
`user-config` (`settings.config`, `connman/`) and, on the read-only root,
`/etc/shadow` and `/etc/sysctl.conf`. `--stock-image` empties the five
writable volumes ([a never-set-up unit](qemu.md#a-never-set-up-unit)).

## Gen 2 (`j49`)

The **raw** chip: 2112-byte pages, the 2048 data bytes then the 64-byte
OOB, with the GPMC's BCH4 parity in the OOB (7 bytes per 512 at OOB
offset 36), as `nand.raw`. The GPMC model recomputes the ECC of
every page the firmware writes. Partition
map and boot chain: [spec-j49.md](spec-j49.md#boot).
