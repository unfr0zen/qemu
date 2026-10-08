#!/bin/sh
#
# Boot the gen3 stock firmware in the flintstone machine (or, with --gen2,
# the gen 2's in the j49 machine).
#
# The stock U-Boot image is carved out of the NAND image by the machine's
# boot ROM stub itself: nothing external is loaded, the ROM reads the NAND
# backend exactly like the real part would.
#
# Usage:
#   ./run.sh                     # U-Boot + kernel console (Ctrl-A X quits)
#   ./run.sh --ui                # also serve a web page of the device:
#                                   #   the display, the wheel, the button,
#                                   #   and the console. Prints its URL.
#                                   #   The terminal stays the console too
#                                   #   (Ctrl-] or Ctrl-A X quits).
#   ./run.sh --ui --nlclient FILE # isolated NAND, install FILE after boot
#   ./run.sh --cloud             # let the unit reach the internet (by
#                                   #   default it gets DHCP and no further)
#   ./run.sh --no-provision      # boot the NAND exactly as it is
#   ./run.sh --nand FILE         # seed the NAND state from FILE (only
#                                   #   when there is no state yet, or with
#                                   #   NAND_FRESH=1)
#   ./run.sh --mqtt HOST[:PORT]  # the unit reaches an MQTT broker at
#                                   #   10.0.2.100:1883, and nothing else
#   ./run.sh --nic-extra OPTS    # more -nic user options (guestfwd=...)
#   ./run.sh --ssh PORT          # the unit's port 22 on 127.0.0.1:PORT
#   ./run.sh --stock-image FILE  # write FILE: the image as a unit that
#                                   #   was never set up, then exit
#   ./run.sh -d guest_log ...    # extra QEMU args pass through
#   ./run.sh --root1             # boot root1 first from now on (the other
#                                   #   system-update slot; --root0 back), as
#                                   #   a system update switches it
#   ./run.sh --gen2 [--ui]       # the gen 2 (j49) from its own image
#
# QEMU_FORCE_DOCKER=1 runs a native build through Linux/Docker as well.
# QEMU_DOCKER_ARGS adds options to that docker run (a network to join, say).

set -e

script_path="$(cd "$(dirname "$0")" && pwd)"
repo_root="$script_path"

# Handle the test launcher before choosing QEMU. It calls us again without
# --nlclient after preparing a separate NAND and a deployment worker.
for arg in "$@"; do
    if [ "$arg" = "--nlclient" ]; then
        exec python3 "$script_path/../nlclient/tools/run_ui_test.py" "$@"
    fi
done

QEMU=""
build_candidates="${QEMU_BUILD_DIR:-build build-docker}"
[ "$QEMU_FORCE_DOCKER" = 1 ] && build_candidates=build-docker
for b in $build_candidates; do
    if [ -x "$script_path/qemu-src/$b/qemu-system-arm" ]; then
        QEMU="$script_path/qemu-src/$b/qemu-system-arm"
        builddir="$b"
        break
    fi
done
if [ -z "$QEMU" ]; then
    echo "ERROR: qemu-system-arm not built yet — run: ./setup.sh docker" >&2
    exit 1
fi

# Options of our own, before anything QEMU should see.
ui=0
ui_port="${UI_PORT:-8075}"
cloud=0
provision=1
stock_image=""
mqtt=""
nic_extra=""
ssh_port=""
gen2=0
root=""
nand_arg=""
while [ $# -gt 0 ]; do
    case "$1" in
        --gen2) gen2=1; shift ;;
        --root0) root=0; shift ;;
        --root1) root=1; shift ;;
        --ui) ui=1; shift ;;
        --cloud) cloud=1; shift ;;
        --no-provision) provision=0; shift ;;
        --stock-image) stock_image="$2"; shift 2 ;;
        --nand) nand_arg="$2"; shift 2 ;;
        --ui-port) ui_port="$2"; shift 2 ;;
        --mqtt) mqtt="$2"; shift 2 ;;
        --nic-extra) nic_extra="$nic_extra,$2"; shift 2 ;;
        --ssh) ssh_port="$2"; shift 2 ;;
        *) break ;;
    esac
done

# Fail before modifying NAND or starting UI helpers when the host cannot
# run this backend. In particular, a printed UI URL must not mask a
# denied Docker socket or a forbidden local listener.
if [ "$builddir" = build-docker ] || [ "$QEMU_FORCE_DOCKER" = 1 ]; then
    if ! docker info > /dev/null; then
        echo "ERROR: Docker is unavailable; QEMU was not started." >&2
        exit 1
    fi
fi
if [ "$ui" = 1 ]; then
    python3 - <<'PY'
import socket
import sys
try:
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
except OSError as error:
    sys.exit(f'ERROR: cannot open a local UI listener: {error}')
PY
fi

# Is a TCP port already listening? Uses python3, which --ui needs anyway.
port_busy() {
    python3 -c "
import socket, sys
s = socket.socket()
s.settimeout(0.3)
sys.exit(0 if s.connect_ex(('127.0.0.1', int(sys.argv[1]))) == 0 else 1)
" "$1" 2>/dev/null
}
pick_port() {
    p=$1
    while port_busy "$p"; do
        p=$((p + 1))
    done
    echo "$p"
}

# The boot ROM stub lives in the machine: it reads fw1's IVT (mtd0, 0x400
# into the image) in the NAND image and loads what its boot data describes,
# then starts the CPU at the IVT entry, whichever U-Boot the firmware put
# there. It applies the image's DCD first (clocks, pads, DDR), as the ROM does.

# The de-ECC'd logical NAND image is the pristine source. By default the
# machine runs on a persistent working copy of it (.qemu/nand-state.bin,
# an APFS/reflink clone, so it is instant and shares blocks), so what the
# firmware writes -- UBIFS journal recovery, sshd host keys, settings --
# survives across boots, like a real unit. The image itself is never written.
#   NAND_FRESH=1     start over from the pristine image
#   NAND_SNAPSHOT=1  throwaway run: writes go to a temporary overlay
#   NAND_RW=1        write straight into $NAND (never point this at your
#                      only copy of the image)
# No image ships in this repo: the pristine image is whatever --nand FILE
# (or $NAND) names, else .qemu/nand.bin. It is only needed to seed the
# state; once the state exists, a plain run boots that alone.
NAND_PRISTINE="$repo_root/.qemu/nand.bin"
machine=flintstone
MACHINE_ARGS="-M flintstone -m 512M"
NAND_STATE_DEFAULT="$repo_root/.qemu/nand-state.bin"
if [ "$gen2" = 1 ]; then
    # The gen 2 boots its raw NAND (data + OOB pages), as nand.raw. No
    # image ships here: it carries the unit's data and its owner's
    # credentials.
    machine=j49
    MACHINE_ARGS="-M j49 -m 64M"
    NAND_PRISTINE="$repo_root/.qemu/nand.raw"
    NAND_STATE_DEFAULT="$repo_root/.qemu/j49-nand-state.raw"
fi
[ -n "$nand_arg" ] && NAND="$nand_arg"
nand_given="$NAND"
NAND="${NAND:-$NAND_PRISTINE}"
NAND_STATE="${NAND_STATE:-$NAND_STATE_DEFAULT}"
use_state=0
[ -z "$stock_image" ] && [ "$NAND_SNAPSHOT" != "1" ] && [ "$NAND_RW" != "1" ] &&
    [ "$NAND_FRESH" != "1" ] && [ -f "$NAND_STATE" ] && use_state=1
if [ "$use_state" = 0 ] && [ ! -f "$NAND" ]; then
    echo "ERROR: NAND image not found: $NAND" >&2
    echo "  no NAND state at $NAND_STATE either: seed it with ./run.sh$([ "$gen2" = 1 ] && echo " --gen2") --nand FILE" >&2
    exit 1
fi
if [ "$use_state" = 1 ] && [ -n "$nand_given" ]; then
    echo "NAND state: $NAND_STATE exists, $nand_given ignored (NAND_FRESH=1 to reseed)" >&2
fi
# The backplate MCU's flash (backplate.py --flash) belongs to the unit the
# NAND is: it sits next to the image, and a copy of the NAND takes it along.
clone_backplate() {                # clone_backplate SRC_NAND DST_NAND
    rm -f "$2.backplate"
    [ -f "$1.backplate" ] && cp "$1.backplate" "$2.backplate"
    return 0
}
NAND_SNAP=off
if [ -n "$stock_image" ]; then
    # A new file from the image, which provision.py --factory then wipes.
    rm -f "$stock_image"
    cp -c "$NAND" "$stock_image" 2>/dev/null ||
        cp --reflink=auto "$NAND" "$stock_image"
    NAND="$(cd "$(dirname "$stock_image")" && pwd)/$(basename "$stock_image")"
elif [ "$NAND_SNAPSHOT" = "1" ]; then
    # A scratch clone, made anew each run: the provisioning boot below
    # writes to the NAND, and the pristine image must never be written.
    NAND_SCRATCH="$repo_root/.qemu/nand-snapshot.bin"
    mkdir -p "$(dirname "$NAND_SCRATCH")"
    rm -f "$NAND_SCRATCH"
    cp -c "$NAND" "$NAND_SCRATCH" 2>/dev/null ||
        cp --reflink=auto "$NAND" "$NAND_SCRATCH"
    clone_backplate "$NAND" "$NAND_SCRATCH"
    NAND="$NAND_SCRATCH"
    NAND_SNAP=on
elif [ "$NAND_RW" != "1" ]; then
    if [ "$NAND_FRESH" = "1" ] || [ ! -f "$NAND_STATE" ]; then
        mkdir -p "$(dirname "$NAND_STATE")"
        rm -f "$NAND_STATE"
        cp -c "$NAND" "$NAND_STATE" 2>/dev/null ||
            cp --reflink=auto "$NAND" "$NAND_STATE"
        clone_backplate "$NAND" "$NAND_STATE"
        echo "NAND state: fresh copy of $(basename "$NAND") -> $NAND_STATE" >&2
    fi
    NAND="$NAND_STATE"
fi

# Guest time pacing. By default the guest runs on plain TCG with the host's
# clock: real time, and the fastest by far (measured 2026-09-27: login at
# 33 s, vs ~6 min with shift=3 and ~10 min with shift=auto, whose adaptive
# shift starves CPU-heavy userspace). Early bring-up needed -icount for the
# vendor U-Boot's delay loops (see docs/spec-flintstone.md); the finished models don't.
# QEMU_ICOUNT=shift=3 gives the old instruction-paced, deterministic boot
# (the dm-lumyo t33 README's knob), handy under gdb or for bisecting.
QEMU_ICOUNT="${QEMU_ICOUNT:-off}"
ICOUNT_ARGS=""
[ "$QEMU_ICOUNT" != "off" ] && ICOUNT_ARGS="-icount $QEMU_ICOUNT"

extra=""
case "$*" in
    *-serial*|*-nographic*|*-display*|*-monitor*) ;;
    *) extra="-nographic" ;;
esac

# The network behind the wifi chip's access point (SSID "QEMU"): QEMU's
# user-mode stack, so the unit gets 10.0.2.15 by DHCP and nothing past the
# virtual router. Online (--cloud), the unit signs in to Nest's service as
# the device the image came from and takes on that account's state (the
# owner's wiring, units, firmware updates into the NAND), so every boot
# differs; offline, every checkout boots the same. A -nic/-netdev of your
# own replaces it.
#
# --mqtt HOST[:PORT] adds one way out: 10.0.2.100:1883 in the guest is
# relayed to the broker (a guestfwd, one bash /dev/tcp relay per
# connection), for ha/nestlocal. The -nic then goes with the user's
# arguments, since its relay command has spaces.
NIC="user,model=bcm43341"
[ "$gen2" = 1 ] && NIC="user,model=wl1271"
[ "$cloud" = 0 ] && NIC="$NIC,restrict=on"
if [ -n "$mqtt" ]; then
    case "$mqtt" in *:*) ;; *) mqtt="$mqtt:1883" ;; esac
    NIC="$NIC,guestfwd=tcp:10.0.2.100:1883-cmd:bash -c 'exec 3<>/dev/tcp/${mqtt%:*}/${mqtt##*:} || exit 1; cat <&3 & cat >&3; kill \$!'"
fi
# --ssh PORT: the unit's sshd on the host's 127.0.0.1:PORT. In Docker QEMU
# listens on the container's every address and the port is published.
DOCKER_SSH_PORT=""
if [ -n "$ssh_port" ]; then
    if [ "$builddir" = "build-docker" ] || [ "$QEMU_FORCE_DOCKER" = "1" ]; then
        nic_extra="$nic_extra,hostfwd=tcp:0.0.0.0:$ssh_port-:22"
        DOCKER_SSH_PORT="-p 127.0.0.1:$ssh_port:$ssh_port"
    else
        nic_extra="$nic_extra,hostfwd=tcp:127.0.0.1:$ssh_port-:22"
    fi
fi
if [ -n "$mqtt" ] || [ -n "$nic_extra" ]; then
    set -- "$@" -nic "$NIC$nic_extra"
fi
NIC_ARGS="-nic $NIC"
[ "$NIC" = none ] && NIC_ARGS=""
case "$*" in
    *-nic*|*-netdev*|*-net\ *) NIC_ARGS="" ;;
esac

# The Docker build produces a Linux binary; run it in the same image
# setup.sh used (tag derived from the Dockerfile, same as there).
use_docker=0
if [ "$builddir" = "build-docker" ] || [ "$QEMU_FORCE_DOCKER" = "1" ]; then
    use_docker=1
    tag="imx6-qemu-build:$(shasum -a 256 "$script_path/docker/Dockerfile" | cut -c1-12)"
fi

# Make the unit usable before every boot: a root login (root / nest), a
# quiet console, and the network above saved so connman joins it at boot. provision.py does it in a short
# boot of its own (~10 s) in which the unit's own kernel writes the NAND
# (U-Boot -> init=/bin/sh). Whatever is already in place is left alone;
# nothing is kept on the host, so any checkout and any NAND boot the same.
PROVISION_QEMU="$MACHINE_ARGS -nographic -nic none"
run_provision() {
    [ "$gen2" = 1 ] && set -- --gen2 "$NAND" "$@"
    [ "$gen2" = 0 ] && [ -n "$root" ] && set -- --root "$root" "$@"
    if [ "$use_docker" = 1 ]; then
        python3 "$script_path/provision.py" "$@" -- \
            docker run --rm -i --init -v "$repo_root:$repo_root" \
            -w "$repo_root" "$tag" "$QEMU" $PROVISION_QEMU \
            -drive if=none,id=nand,format=raw,file="$NAND",snapshot=off
    else
        python3 "$script_path/provision.py" "$@" -- "$QEMU" $PROVISION_QEMU \
            -drive if=none,id=nand,format=raw,file="$NAND",snapshot=off
    fi
}
# --root0/--root1: the boot slot, which a system update switches through
# U-Boot's bootcmd and the unit keeps from then on. The gen 2's environment
# is edited here, as fw_setenv would; the gen 3's at U-Boot's prompt, by the
# pre-boot.
if [ -n "$root" ] && [ "$gen2" = 1 ]; then
    slot=$(python3 "$script_path/j49_env.py" "$NAND" --root "$root") || exit 1
    echo "boot slot: $slot" >&2
elif [ -n "$root" ] && [ "$provision" = 0 ] && [ -z "$stock_image" ]; then
    echo "ERROR: --root$root needs the pre-boot on the gen 3 (drop --no-provision)" >&2
    exit 1
fi
if [ "$gen2" = 1 ] && { [ "$provision" = 1 ] || [ -n "$stock_image" ]; }; then
    # The release U-Boot silences the console (its built-in silent=1 blanks
    # the first console= it passes); the unit's own environment, edited as
    # fw_setenv would, adds a second one. The firmware is unchanged.
    python3 "$script_path/j49_env.py" "$NAND" \
        'addmodel=setenv bootargs ${bootargs} nlmodel=${nlmodel} console=ttyO0,115200'
fi
if [ -n "$stock_image" ]; then
    if ! run_provision --factory; then
        rm -f "$NAND"
        echo "stock image: failed" >&2
        exit 1
    fi
    echo "stock image: $NAND (boot it with NAND=$NAND NAND_SNAPSHOT=1" \
         "./run.sh$([ "$gen2" = 1 ] && echo " --gen2") --no-provision)" >&2
    exit 0
elif [ "$provision" = 1 ]; then
    run_provision || echo "provision: booting the unit as it is" >&2
fi

# The board around the SoC, as a web page (--ui): the round display, the
# rotating wheel and the button, plus the console. Every part of it is a
# machine property or a chardev, so nothing in the firmware changes and a
# boot without --ui is untouched. The sockets are TCP on localhost rather
# than unix: QEMU runs in a container here, and a unix socket does not
# cross the file-sharing boundary. ui.py follows QEMU (via --parent $$,
# which exec keeps) and cleans the scratch dir when it goes.
UI_ARGS=""
DOCKER_UI_PORTS=""
keyboard=0                         # ui.py types the terminal into the console
if [ "$ui" = 1 ]; then
    extra=""                       # the page owns the display and the console
                                   # (ui.py shares it with the terminal)
    # One per run: two units up at once must not share screendumps. A run
    # killed outright leaves its own behind; drop those whose run is gone.
    for d in "$repo_root"/.qemu/ui-*; do
        [ -d "$d" ] && ! kill -0 "${d##*-}" 2>/dev/null && rm -rf "$d"
    done
    ui_dir="$repo_root/.qemu/ui-$$"
    mkdir -p "$ui_dir"
    board_port=$(pick_port 8101)
    qmp_port=$(pick_port $((board_port + 1)))
    console_port=$(pick_port $((qmp_port + 1)))
    hmp_port=$(pick_port $((console_port + 1)))
    bp_port=$(pick_port $((hmp_port + 1)))
    ncp_port=$(pick_port $((bp_port + 1)))
    http_port=$(pick_port "$ui_port")
    if [ -n "$QEMU_UI_INFO" ]; then
        python3 - "$QEMU_UI_INFO" "$$" "$http_port" "$hmp_port" "$ui_dir" "$NAND" <<'PY'
import json
from pathlib import Path
import sys
path, pid, http, hmp, directory, nand = sys.argv[1:]
info = dict(pid=int(pid), ui_url=f'http://127.0.0.1:{http}',
            hmp_host='127.0.0.1', hmp_port=int(hmp),
            screendump=str(Path(directory) / 'hmp-shot.ppm'), nand=nand)
target = Path(path)
target.parent.mkdir(parents=True, exist_ok=True)
scratch = target.with_suffix('.tmp')
scratch.write_text(json.dumps(info, indent=2) + '\n')
scratch.replace(target)
PY
    fi
    # QEMU binds 0.0.0.0 in the container: a published -p port arrives on
    # the container's ethernet address, not its loopback. A native build
    # gets the same; it is a localhost dev box either way.
    bind=0.0.0.0
    if [ "$gen2" = 1 ]; then
        # UART1 is the console, UART3 the backplate's serial (ttyO2).
        SERIAL_ARGS="-serial chardev:con -serial null -serial chardev:bp"
        BOARD_ARGS="-global driver=j49.board,property=chardev,value=boardui \
-chardev socket,id=ncp,host=$bind,port=$ncp_port,server=on,wait=off,nodelay=on \
-global driver=em35x-ncp,property=chardev,value=ncp \
-global driver=j49.board,property=poweroff-exit,value=on"
        PINS="button=HU-BUTTON,poweroff=POWEROFF,wifi_enable=WIFI-ENABLE,beeper_en=BEEPER-EN"
    else
        SERIAL_ARGS="-serial chardev:con -serial null -serial null -serial null -serial null -serial chardev:bp"
        BOARD_ARGS="-global driver=flintstone.board,property=chardev,value=boardui \
-chardev socket,id=ncp,host=$bind,port=$ncp_port,server=on,wait=off,nodelay=on \
-global driver=em35x-ncp,property=chardev,value=ncp \
-global driver=flintstone.board,property=poweroff-exit,value=on"
        PINS="button=HU-BUTTON,poweroff=POWEROFF,wifi_reset=WIFI-RESET,hall_enable=HALL-ENABLE,vbatt_en=VBATT-EN,beeper_en=BEEPER-EN"
    fi
    UI_ARGS="-chardev socket,id=boardui,host=$bind,port=$board_port,server=on,wait=off,nodelay=on \
-chardev socket,id=con,host=$bind,port=$console_port,server=on,wait=off,nodelay=on \
-chardev socket,id=bp,host=$bind,port=$bp_port,server=on,wait=off,nodelay=on \
$SERIAL_ARGS $BOARD_ARGS \
-qmp tcp:$bind:$qmp_port,server=on,wait=off \
-mon chardev=hmp,mode=readline -chardev socket,id=hmp,host=$bind,port=$hmp_port,server=on,wait=off,nodelay=on \
-display none -monitor none"
    if [ "$builddir" = "build-docker" ] || [ "$QEMU_FORCE_DOCKER" = "1" ]; then
        DOCKER_UI_PORTS="-p 127.0.0.1:$board_port:$board_port \
-p 127.0.0.1:$qmp_port:$qmp_port \
-p 127.0.0.1:$console_port:$console_port \
-p 127.0.0.1:$hmp_port:$hmp_port \
-p 127.0.0.1:$bp_port:$bp_port \
-p 127.0.0.1:$ncp_port:$ncp_port"
    fi
    # The terminal is a second keyboard beside the page's (ui.py makes it
    # raw; Ctrl-] or Ctrl-A X quits). An & job's stdin is /dev/null unless
    # given one.
    ui_stdin=/dev/null
    if [ -t 0 ] && [ -t 1 ]; then
        keyboard=1
        ui_stdin=/dev/tty
    fi
    python3 "$script_path/ui/ui.py" \
        --board "tcp:127.0.0.1:$board_port" \
        --qmp "tcp:127.0.0.1:$qmp_port" \
        --console "tcp:127.0.0.1:$console_port" \
        --echo-console $([ "$keyboard" = 1 ] && echo --keyboard) \
        --screen-file "$ui_dir/screen.ppm" \
        --port "$http_port" \
        --pins "$PINS" \
        --name "Nest gen $([ "$gen2" = 1 ] && echo 2 || echo 3)" \
        --parent $$ --cleanup "$ui_dir" < "$ui_stdin" &
    # The fake backplate (sensor) MCU on the sensor-MCU serial: answers
    # the front unit and streams bright ALS samples so nlclient lights
    # the display (it keeps the panel asleep until the ALS reports).
    python3 "$script_path/ui/backplate.py" --serial "tcp:127.0.0.1:$bp_port" \
        --flash "$NAND.backplate" --parent $$ &
    # The Thread radio (em35x-ncp on ECSPI3, McSPI2 on the gen 2): the
    # ConnectIP network co-processor wpantund drives (ui/em35x.py
    # --debug logs it). The gen 2's EUI-64 is the unit's own, from its
    # environment (hwaddr1), as the unit reports its 15.4 MAC address.
    NCP_ARGS=""
    if [ "$gen2" = 1 ]; then
        eui64=$(python3 "$script_path/j49_env.py" "$NAND" | sed -n 's/^hwaddr1=//p')
        NCP_ARGS="--gen2${eui64:+ --eui64 $eui64}"
    fi
    python3 "$script_path/ui/em35x.py" $NCP_ARGS \
        --serial "tcp:127.0.0.1:$ncp_port" --parent $$ &
    echo "$machine UI: http://127.0.0.1:$http_port" >&2
    echo "  board tcp:$board_port · qmp tcp:$qmp_port · console tcp:$console_port · hmp tcp:$hmp_port · backplate tcp:$bp_port · ncp tcp:$ncp_port" >&2
fi

# With --ui on a terminal the terminal is ui.py's; QEMU keeps off it.
[ "$keyboard" = 1 ] && exec < /dev/null
if [ "$use_docker" = 1 ]; then
    tty_flags=""
    if [ -t 0 ] && [ -t 1 ] && [ "$keyboard" = 0 ]; then
        tty_flags="-it"
    fi
    # Named after this run: a docker CLI that dies without passing its
    # signal on (SIGKILL, say) leaves QEMU running in its container,
    # so drop those whose run is gone, and any stale one with our name.
    for c in $(docker ps -a --filter name=^$machine- --format '{{.Names}}'); do
        pid=${c#$machine-}
        if [ "$pid" = $$ ] || ! kill -0 "$pid" 2>/dev/null; then
            docker rm -f "$c" >/dev/null
        fi
    done
    # The repo is mounted at its host path, so the FW path is valid as-is.
    exec docker run --rm --init --name "$machine-$$" $tty_flags \
        -v "$repo_root:$repo_root" \
        -w "$repo_root" \
        $DOCKER_UI_PORTS $DOCKER_SSH_PORT \
        $QEMU_DOCKER_ARGS \
        "$tag" "$QEMU" $MACHINE_ARGS $extra $ICOUNT_ARGS \
            -drive if=none,id=nand,format=raw,file="$NAND",snapshot=$NAND_SNAP \
            $NIC_ARGS \
            $UI_ARGS \
            "$@"
fi

    exec "$QEMU" $MACHINE_ARGS $extra $ICOUNT_ARGS \
        -drive if=none,id=nand,format=raw,file="$NAND",snapshot=$NAND_SNAP \
        $NIC_ARGS \
        $UI_ARGS \
        "$@"
