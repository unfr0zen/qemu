# nest-qemu — boot Nest thermostat firmware without hardware

QEMU machines for two Nest Learning Thermostats that boot each unit's
**own stock firmware** from its NAND, with no emulator-specific rebuilds:

| Machine | Unit | SoC | Firmware it boots |
| --- | --- | --- | --- |
| `-M flintstone` | gen 3 (Display-3.4, `diamond3`) | i.MX6SoloX | U-Boot 2014.04, Linux 4.1.15, UBIFS root |
| `-M j49` | gen 2 (Display-2.x) | TI AM3703 | x-loader, U-Boot 2012.10, Linux 2.6.37, JFFS2 root |

Both boot to `login:` and run the Nest UI, with the display, ring, button,
piezo, backplate, WiFi and Thread radio modelled. `--ui` serves the unit as
a web page.

Not affiliated with Google or Nest. The stock firmware is theirs, and none
of it ships in this repo: you supply your own image (below).

## Setup

Works on **Linux** and **macOS** (Apple silicon or Intel). By default QEMU
builds and runs inside a Docker container, so the host only needs:

| Tool | macOS | Linux (Debian/Ubuntu) |
| --- | --- | --- |
| Docker | Docker Desktop (or OrbStack, Colima) | `docker.io` or Docker Engine, your user in the `docker` group |
| git | Xcode command line tools (`xcode-select --install`) or `brew install git` | `apt install git` |
| python3 | Xcode command line tools or `brew install python` | `apt install python3` |
| shasum | built in | `apt install perl` (usually there already) |

The Python helpers (`provision.py`, `ui/`) use the standard library only:
no pip packages, no virtualenv. `--ui` needs a web browser. No submodules
either: `setup.sh` fetches QEMU itself.

For a native build instead (`./setup.sh` without `docker`, no Docker
needed to run), install QEMU's build dependencies:

```sh
# macOS (Homebrew)
brew install meson ninja pkg-config glib pixman libslirp
# Debian/Ubuntu (the list docker/Dockerfile installs)
sudo apt install git build-essential flex bison pkg-config libglib2.0-dev \
    libpixman-1-dev zlib1g-dev libslirp-dev python3 pipx
pipx install 'meson>=1.5' && pipx install ninja   # apt's meson is too old
```

```sh
git clone <this repo> nest-qemu && cd nest-qemu
./setup.sh docker    # once (~15 min): shallow-clone QEMU v11.0.0 into qemu-src/,
                     # apply patches/, build qemu-system-arm in Docker
```

`./setup.sh` without `docker` builds natively instead. Run it again to
rebuild after editing `qemu-src/` (about a minute). It applies `patches/` only to a fresh clone:
after pulling new patches, `rm -rf qemu-src` and run it again.

## First boot: bring your NAND image

No NAND image ships here: a stock image carries its unit's data and its
owner's credentials. You supply one ([the formats each machine
expects](docs/nand-image.md)) and pass it once with `--nand`. It seeds a
working copy in `.qemu/` that later boots run on; your image is never
written.

```sh
# gen 3: a 512 MiB logical image (data pages only)
./run.sh --nand nand-logical.bin          # seeds .qemu/nand-state.bin
./run.sh                                  # later boots
./run.sh --ui                             # + web page: http://127.0.0.1:8075

# gen 2: the raw chip, data + OOB pages
./run.sh --gen2 --nand nand-j49.raw       # seeds .qemu/j49-nand-state.raw
./run.sh --gen2 --ui
```

Before each boot a short pre-boot sets up a root login (`root` / `nest`),
a quiet console, the emulated WiFi network, and skips the out-of-box setup
(`--no-provision` boots the NAND as it is). Start over from the image with
`NAND_FRESH=1 ./run.sh --nand FILE`. Ctrl-A X (or Ctrl-] with `--ui`) quits.

Other options: `--cloud` (internet access), `--ssh PORT`, `--mqtt HOST`,
`--stock-image FILE`, `--root0`/`--root1`, and more in the header of
`run.sh` and [docs/qemu.md](docs/qemu.md#runsh-options).

## Testing a patched nlclient

`--nlclient FILE` boots an isolated NAND and installs FILE over the
guest's nlclient after boot:

```sh
./run.sh --ui --nlclient FILE
```

The build that produces FILE, and the rest of that harness, live in the
parent repository.

Native builds are now preferred when both native and Docker builds exist.
`QEMU_BUILD_DIR=build-docker` selects Docker explicitly; an unavailable
Docker backend or forbidden UI listener fails before provisioning.

## Docs

- [docs/qemu.md](docs/qemu.md): the gen 3, with options, NAND state, the
  web page, WiFi, the pre-boot, the GPU, the patch list and debugging
- [docs/qemu-j49.md](docs/qemu-j49.md): the gen 2
- [docs/nand-image.md](docs/nand-image.md): the image formats, the NAND
  layout and where the firmware keeps its state
- [docs/spec-flintstone.md](docs/spec-flintstone.md),
  [docs/spec-j49.md](docs/spec-j49.md): the hardware facts the models are
  built from

## Layout

| Path | What |
| --- | --- |
| `patches/` | the machines, as a patch series on QEMU v11.0.0 (`98b060d`): the source of truth |
| `qemu-src/` | the QEMU clone `setup.sh` makes and patches (git-ignored) |
| `run.sh`, `setup.sh` | boot, build |
| `provision.py`, `skip-setup.py`, `j49_env.py` | the pre-boot, and its helpers |
| `ui/` | the web page, and the backplate MCU and Thread radio played on the host |
| `docker/` | the build image (its tag is the Dockerfile's hash: editing it means a new image) |
| `.qemu/` | NAND working copies and per-run scratch (git-ignored) |

To change a machine, commit your work in `qemu-src/` and regenerate the
series: `git format-patch 98b060d..HEAD -o ../patches/`. `setup.sh` applies
the series uncommitted, so to start from commits instead:
`git -C qemu-src stash && git -C qemu-src am ../patches/*.patch`.
A `qemu-src/build*` configured at another path bakes that path in: remove
it and run `setup.sh` again.

Still missing: USB device mode, ENET, other Thread nodes on the air.

## License

GPL-2.0, like QEMU: see [LICENSE](LICENSE). "Nest" and the Learning
Thermostat are Google/Nest trademarks, and the stock firmware is their
copyrighted work; neither ships in this repo, and neither is distributed
through it. The wordmark font is
[Inter](https://github.com/rsms/inter), under the SIL Open Font License
(ui/fonts/OFL-Inter.txt).
