#!/usr/bin/env python3
"""Make the unit usable in the emulator: login, WiFi, no setup, quiet console.

A unit booted from a stock image has root locked, has no saved network and
starts in the out-of-box setup. This fixes all three on the NAND before
the real boot, with the device's own kernel doing the writes: a short boot
where U-Boot's prompt (no login needed) runs U-Boot's own boot command
for the active root file system (the first bootcmd tries: root0 on the
image as provided, root1 after a system update) with init=/bin/sh, and the shell

- gives root the password "nest" if the account is locked (a DES crypt in
  the active root's /etc/shadow: busybox login rejects empty passwords);
- keeps all but critical messages off the console once the unit's sysctl
  runs, before any driver loads (kernel.printk in /etc/sysctl.conf): the
  lm3695_bl backlight driver logs two info messages at every brightness
  change, every couple of seconds, and 6.4-8's brcmfmac logs its routine
  ones as errors. dmesg still has them all;
- saves the WiFi network the way connman does after a first join
  (user-config: connman/wifi_<mac>_<ssid hex>_managed_none/settings,
  AutoConnect), unless it is saved already. connman only autoconnects to
  saved networks. The MAC is U-Boot's ethaddr, which the wlan init script
  gives the interface. Writing through the kernel honours the UBIFS
  journal, which a rebuild from the host would not;
- marks the out-of-box setup done, with the settings the phone app's
  ConfigDone handler writes (skip-setup.py's: every *_completed flag, Eco
  onboarding, language, location), so the unit boots to the thermostat;
- saves the wiring backplate.py reports (its default terminals) as the
  unit's own (hvac_pins, hvac_wires in settings.config, as nlclient
  stores them): nlclient opens on "The wiring to your equipment has
  changed" whenever the backplate's pins differ from the saved ones, and
  the image's saved wiring differs from the stub's defaults.

Anything already in place is left alone. run.sh runs this before every
boot, on the NAND that boot uses; nothing is kept on the host, so every
checkout and every NAND image behave the same.

The gen 2 (--gen2 NAND, the j49 machine) has no U-Boot prompt to stop at:
its release U-Boot is silent. There the shell comes from the unit's own
environment, edited as fw_setenv would (j49_env.py): the boot command's
addmodel gets init=/bin/sh for this one boot and is put back after. Root
gets "nest" even when it has a password already: a stock image can carry one
nobody here knows (NoLongerEvil's installer sets one), and as the gen 2's
inittab runs no getty, one goes on the console (ttyO0). Its
writable partitions are JFFS2 on MTD (user-config is mtdblock11), so the
same changes go through the kernel's JFFS2, and --factory erases those
partitions with flash_eraseall instead, all but system-config: there the
factory's radio calibration (wl1271-nvs.bin, which the unit's first boot
turns into its wl12xx-nvs.bin) stays and what the unit made of it since
(that NVS, its checksum, the SSH host keys) goes.

With --factory it does none of that and instead makes the NAND look like a
unit that was never set up (run.sh --stock-image): the volumes the running
firmware writes (FACTORY_WIPE: the owner's settings and networks, what the
unit learned, logs, host keys) are emptied with ubiupdatevol -t, which
erases every block they hold, and the firmware's UBIFS lays a new, empty
file system on each at its next mount. The root file systems, the U-Boot
environment and the factory provision volume are not touched; the root is
never mounted read-write.

Usage: provision.py [--ssid QEMU | --factory] [--gen2 NAND]
                    [--transcript FILE] -- QEMU-COMMAND...
       (the command must boot the machine with -nographic and the NAND
       writable: its serial console is driven over stdin/stdout)
"""
import argparse
import importlib.util
import os
import re
import select
import shlex
import subprocess
import sys
import time

ROOT_HASH = "saiHAK.ldgCRI"       # DES crypt of "nest"
HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS = "/media/user-config/settings.config"
FACTORY_WIPE = ("system-config", "user-config", "data", "log", "scratch")
PRINTK = "kernel.printk = 3 4 1 7"   # console: KERN_CRIT and more urgent


class Console:
    def __init__(self, cmd, transcript=None):
        self.transcript = open(transcript, "wb") if transcript else None
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT)
        self.buf = b""
        self.n = 0

    def expect(self, pattern, timeout):
        """Wait for a regex in the output; return its match or None."""
        rx = re.compile(pattern)
        end = time.time() + timeout
        while time.time() < end:
            m = rx.search(self.buf)
            if m:
                self.buf = self.buf[m.end():]
                return m
            r, _, _ = select.select([self.proc.stdout], [], [], 0.2)
            if r:
                data = os.read(self.proc.stdout.fileno(), 65536)
                if not data:
                    return None
                if self.transcript:
                    self.transcript.write(data)
                    self.transcript.flush()
                self.buf += data
        return None

    def send(self, text):
        self.proc.stdin.write(text.encode())
        self.proc.stdin.flush()

    def run(self, command, timeout=60):
        """Run a shell command; return its exit status, None on timeout."""
        self.n += 1
        # Arithmetic, so the echo of the typed line never matches. On the
        # command's own line: what's typed while a command reprograms the
        # console UART (stty) is lost.
        self.send(f"{command}; echo @@$(({self.n}+0))@@ $?\n")
        m = self.expect(rb"@@%d@@ (\d+)" % self.n, timeout)
        return int(m.group(1)) if m else None

    def quit(self):
        try:
            self.send("\x01x")                 # QEMU's mux: quit
            self.proc.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            self.proc.kill()
        try:
            self.proc.stdin.close()
        except OSError:
            pass


def printenv(con, name):
    con.send(f"printenv {name}\n")
    # A whole line of its own (U-Boot ends them "\n\r"), not the echo.
    m = con.expect(rb"[\r\n]%s=([^\r\n]*)[\r\n]" % name.encode(), 10)
    con.expect(rb"=> ", 5)
    return m.group(1).decode() if m else None


def shell(con, root_rw=True, root=None):
    """From power-on to a root shell on the stock kernel, booted from the
    root file system U-Boot boots (a system update switches it; root=N
    switches it to rootN first, as the update does); return the MAC and
    that volume's name."""
    if not con.expect(rb"U-Boot 20", 120):
        raise RuntimeError("U-Boot never started")
    # Its env sets how long autoboot waits for a key (none at all after a
    # system update: bootdelay 0), so keep one waiting until the prompt.
    deadline = time.time() + 30
    while time.time() < deadline:
        con.send("\n")
        if con.expect(rb"=> ", 0.2):
            break
    else:
        raise RuntimeError("no U-Boot prompt")
    mac = printenv(con, "ethaddr")
    if not mac or not re.fullmatch(r"[0-9A-Fa-f:]{17}", mac):
        raise RuntimeError("U-Boot has no ethaddr")
    mac = mac.replace(":", "").lower()
    if root is not None:
        set_root(con, root)
    # bootcmd tries its boot commands in order ("run nandboot1 ||
    # run nandboot0 || ..."); the first is the active one. Run it as it
    # is, with the kernel's init replaced by a shell.
    m = re.match(r"run (\w+)", printenv(con, "bootcmd") or "")
    boot = printenv(con, m.group(1)) if m else None
    root = re.search(r"root=ubi0:(\w+)", boot or "")
    if not root or " && bootm " not in boot:
        raise RuntimeError(f"no root file system in U-Boot's bootcmd "
                           f"({m.group(1) if m else 'bootcmd'}: {boot})")
    con.send(boot.replace(" && bootm ", " init=/bin/sh && bootm ", 1) + "\n")
    deadline = time.time() + 120
    while time.time() < deadline:
        con.send("echo RDY$((6*7))\n")
        if con.expect(rb"RDY42", 3):
            break
    else:
        raise RuntimeError("the kernel never reached a shell")
    if con.run("stty -echo; mount -t proc proc /proc") != 0:
        raise RuntimeError("could not mount /proc")
    if root_rw and con.run("mount -o remount,rw /") != 0:
        raise RuntimeError(f"could not remount {root.group(1)} read-write")
    return mac, root.group(1)


def gen2_prepare(nand):
    """Read the gen 2's MAC and active root from its environment and make
    its next boot a shell; return them and the env to put back."""
    j49_env = load("j49_env", "j49_env.py")
    env = j49_env.read_env(nand)
    mac = env.get("ethaddr", "")
    if not re.fullmatch(r"[0-9A-Fa-f:]{17}", mac):
        raise RuntimeError("the environment has no ethaddr")
    m = re.match(r"run (\w+)", env.get("bootcmd", ""))
    root = re.search(r"root=/dev/mtdblock(\d+)", env.get(m.group(1), "")
                     if m else "")
    if not root or "addmodel" not in env:
        raise RuntimeError("no root file system in the environment's bootcmd")
    restore = {"addmodel": env["addmodel"]}
    j49_env.set_env(nand, {"addmodel": env["addmodel"] + " init=/bin/sh"})
    # mtdblock7 is root0, 9 root1 (the kernel's table starts with the chip)
    name = {"7": "root0", "9": "root1"}.get(root.group(1),
                                            f"mtdblock{root.group(1)}")
    return mac.replace(":", "").lower(), name, restore


def gen2_shell(con, root_rw=True):
    """The kernel boots straight into the shell gen2_prepare asked for."""
    deadline = time.time() + 180
    while time.time() < deadline:
        con.send("echo RDY$((6*7))\n")
        if con.expect(rb"RDY42", 3):
            break
    else:
        raise RuntimeError("the kernel never reached a shell")
    if con.run("stty -echo; mount -t proc proc /proc") != 0:
        raise RuntimeError("could not mount /proc")
    if root_rw and con.run("mount -o remount,rw /") != 0:
        raise RuntimeError("could not remount the root read-write")


def set_root(con, n):
    """At U-Boot's prompt: make bootcmd try rootN first, and save it."""
    bootcmd = printenv(con, "bootcmd") or ""
    m = re.match(r"run nandboot([01]) \|\| run nandboot([01])", bootcmd)
    if not m or m.group(1) == m.group(2):
        raise RuntimeError(f"bootcmd does not start with both nandboots: {bootcmd}")
    if m.group(1) == str(n):
        return
    new = f"run nandboot{n} || run nandboot{1 - n}" + bootcmd[m.end():]
    con.send(f"setenv bootcmd '{new}'\n")
    con.expect(rb"=> ", 5)
    con.send("saveenv\n")
    if not con.expect(rb"=> ", 60) or printenv(con, "bootcmd") != new:
        raise RuntimeError("could not save bootcmd")


def root_login(con, replace=False):
    """Give a locked root the password "nest"; with replace, whatever
    password it has."""
    if con.run(f"grep -q '^root:{ROOT_HASH}:' /etc/shadow") == 0:
        return "root login: root / nest already"
    if not replace and con.run("grep -q '^root:[!*]*:' /etc/shadow") != 0:
        return "root login: already set"
    if con.run(f"sed -i 's|^root:[^:]*:|root:{ROOT_HASH}:|' /etc/shadow") != 0:
        raise RuntimeError("could not set root's password")
    return "root login: root / nest"


# Ahead of rcS, as a loop of its own: busybox init starts its respawn
# entries only once sysinit is done, and NoLongerEvil's rcS never is (it
# ends in sshconnect's endless loop).
GETTY = ('::sysinit:/bin/sh -c "while :; do /sbin/getty -L ttyO0 115200 '
         'vt100; done &"')


def console_login(con):
    """The gen 2's inittab runs no getty: a login on the console, as the
    gen 3 has."""
    if con.run(f"grep -qF '{GETTY}' /etc/inittab") == 0:
        return "console login: already"
    if con.run("{ printf '%s\\n' '# A login on the console (qemu/provision.py)' "
               f"'{GETTY}'; grep -v 'qemu/provision.py\\|getty -L ttyO0' "
               "/etc/inittab; } > /etc/inittab.new && "
               "mv /etc/inittab.new /etc/inittab") != 0:
        raise RuntimeError("could not write /etc/inittab")
    return "console login: getty on ttyO0"


def quiet_console(con):
    if con.run(f"grep -qF '{PRINTK}' /etc/sysctl.conf") == 0:
        return "console: quiet already"
    # The stock file sets no kernel.printk: any is ours, from an earlier run.
    if con.run("sed -i -e '/^# .* the console (qemu\\/provision.py)$/d' "
               "-e '/^kernel.printk *=/d' /etc/sysctl.conf && "
               f"printf '%s\\n' '# Only critical messages on the console (qemu/"
               f"provision.py)' '{PRINTK}' >> /etc/sysctl.conf") != 0:
        raise RuntimeError("could not write /etc/sysctl.conf")
    return "console: only critical messages (dmesg keeps them all)"


def nestlocal(con, gen2=False):
    """Whether ha/qemu_install.py has put nestlocal on this root: run.sh
    --mqtt only relays the broker to it."""
    if con.run("[ -x /nestlabs/sbin/nestlocal ]") == 0:
        return "nestlocal: installed (run.sh --mqtt HOST bridges it)"
    return ("nestlocal: not installed (for --mqtt: python3 ha/qemu_install.py"
            + (" --gen2)" if gen2 else ")"))


# QEMU's user-mode network is fixed: the guest gets .15, the host is
# reachable as .2 and the DNS forwarder is .3.
SLIRP_ADDR, SLIRP_GW, SLIRP_DNS, SLIRP_PREFIX = \
    "10.0.2.15", "10.0.2.2", "10.0.2.3", "24"
IPV4_STATIC = [f"IPv4.method=manual", f"IPv4.local_address={SLIRP_ADDR}",
               f"IPv4.netmask_prefixlen={SLIRP_PREFIX}",
               f"IPv4.gateway={SLIRP_GW}",
               f"Nameservers.configuration={SLIRP_DNS};"]


def wifi(con, mac, ssid):
    """Save the network, with slirp's addresses set by hand.

    DHCP gives the guest an address but no gateway here, so its only
    route is "default dev wlan0 scope link": it ARPs for every peer
    directly. slirp answers ARP for its own network only, so a
    connection forwarded in from the host - whose address the guest
    sees as the host's real one, 172.17.0.1 from Docker's proxy, not
    10.0.2.2 - is received and never answered. That is what made ssh
    hang in the banner exchange with sshd listening and the SYNs
    arriving. A gateway fixes it, and outbound traffic with it.
    """
    ssid_hex = ssid.encode().hex()
    service = f"wifi_{mac}_{ssid_hex}_managed_none"
    lines = [f"[{service}]", f"Name={ssid}", f"SSID={ssid_hex}",
             "Favorite=true", "AutoConnect=true",
             *IPV4_STATIC,
             "IPv6.method=auto", "IPv6.privacy=prefered"]
    d = f"/media/user-config/connman/{service}"
    if con.run(f"[ -e {d}/settings ]") != 0:
        if con.run(f"mkdir -p {d} && chmod 700 {d} && printf '%s\\n' "
                   f"{' '.join(map(shlex.quote, lines))} > {d}/settings && "
                   f"chmod 600 {d}/settings") != 0:
            raise RuntimeError("could not save the network")
        return f"wifi: \"{ssid}\" saved with a gateway, joined at boot"
    if con.run(f"grep -q '^IPv4.gateway={SLIRP_GW}$' {d}/settings") == 0:
        return f"wifi: \"{ssid}\" already saved"
    # An older run saved it with DHCP: drop its IPv4 lines and append ours.
    add = " ".join(map(shlex.quote, IPV4_STATIC))
    if con.run(f"sed -i -e '/^IPv4\\./d' -e '/^Nameservers/d' {d}/settings && "
               f"printf '%s\\n' {add} >> {d}/settings") != 0:
        raise RuntimeError("could not rewrite the network")
    return f"wifi: \"{ssid}\" given slirp's gateway"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def onboarding(con):
    if not set_settings(con, load("skip_setup", "skip-setup.py").setup_values()):
        return "onboarding: already skipped"
    return "onboarding: skipped (boots to the thermostat)"


def wiring(con):
    backplate = load("backplate", "ui/backplate.py")
    wires, pins = backplate.wiring(backplate.DEFAULT_WIRES.split(","))
    if not set_settings(con, {
            "hvac_pins": "".join(str(b) for b in pins),
            "hvac_wires": ",".join(str(b) for b in wires)}):
        return "wiring: already the backplate's"
    return f"wiring: saved as the backplate's ({backplate.DEFAULT_WIRES})"


def set_settings(con, values):
    """Put these keys in settings.config; False if they already were."""
    lines = [f'  <s key="{k}" value="{v}"/>' for k, v in values.items()]
    if con.run(" && ".join(f"grep -qF {shlex.quote(l)} {SETTINGS}"
                           for l in lines)) == 0:
        return False
    # A unit that never ran nlclient has no file yet: start the one it writes.
    if con.run(f"[ -f {SETTINGS} ] || printf '%s\\n' "
               f"'<?xml version=\"1.0\" encoding=\"ISO-8859-1\"?>' "
               f"'<settings>' '</settings>' > {SETTINGS}") != 0:
        raise RuntimeError("could not create settings.config")
    # Every key we set goes, then ours go in before the closing tag.
    drop = "|".join(f'"{k}"' for k in values)
    if con.run(f"grep -vE '</settings>|{drop}' {SETTINGS} > {SETTINGS}.new "
               f"&& printf '%s\\n' {' '.join(map(shlex.quote, lines))} "
               f"'</settings>' >> {SETTINGS}.new && "
               f"mv {SETTINGS}.new {SETTINGS}") != 0:
        raise RuntimeError("could not write settings.config")
    return True


def factory(con):
    """Empty the volumes the running firmware writes; return what was done."""
    if con.run("mount -t sysfs sysfs /sys") != 0:
        raise RuntimeError("could not mount /sys")
    for vol in FACTORY_WIPE:
        # The volume's device node, found by name: ubi0_<id>.
        if con.run(f"n=$(grep -l '^{vol}$' /sys/class/ubi/ubi0_*/name) && "
                   f"ubiupdatevol /dev/$(basename $(dirname $n)) -t", 60) != 0:
            raise RuntimeError(f"could not empty {vol}")
    return f"factory: emptied {', '.join(FACTORY_WIPE)}"


GEN2_FACTORY_FILES = ("wl1271-nvs.bin",)   # system-config's, from the factory


def gen2_factory(con):
    """Erase the MTD partitions the running firmware writes (their JFFS2
    starts over, empty, at the next mount), and system-config down to what
    the factory put there."""
    keep = " ".join(GEN2_FACTORY_FILES)
    if con.run("mount -t jffs2 /dev/mtdblock10 /media/system-config && "
               "cd /media/system-config && for f in * .[!.]*; do "
               f"case \" {keep} \" in *\" $f \"*) ;; *) rm -rf \"$f\" ;; esac; "
               "done; cd / && sync && umount /media/system-config", 60) != 0:
        raise RuntimeError("could not clear system-config")
    for vol in FACTORY_WIPE:
        if vol == "system-config":
            continue
        # this mtd-utils' flash_erase takes no -q, and 0 blocks as none
        if con.run(f"n=$(grep '\"{vol}\"' /proc/mtd | cut -d: -f1) && "
                   f"[ -n \"$n\" ] && flash_eraseall /dev/$n > /dev/null",
                   300) != 0:
            raise RuntimeError(f"could not erase {vol}")
    return (f"factory: erased {', '.join(v for v in FACTORY_WIPE if v != 'system-config')}"
            f"; system-config down to {keep}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ssid", default="QEMU",
                    help="the access point's SSID (the wifi chip's ssid "
                         "property)")
    ap.add_argument("--factory", action="store_true",
                    help="empty the firmware's writable volumes instead")
    ap.add_argument("--root", type=int, choices=(0, 1),
                    help="boot this root file system first from now on "
                         "(the gen 3's U-Boot env; run.sh --root0/--root1)")
    ap.add_argument("--gen2", metavar="NAND",
                    help="the gen 2 (j49) whose NAND image this is")
    ap.add_argument("--transcript", help="save the console session here")
    ap.add_argument("cmd", nargs=argparse.REMAINDER,
                    help="-- then the QEMU command line")
    args = ap.parse_args()
    cmd = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
    if not cmd:
        ap.error("no QEMU command given")

    print("provision: preparing the unit...",
          file=sys.stderr)
    if args.gen2:
        gen2(args, cmd)
        return
    con = Console(cmd, args.transcript)
    done = []
    try:
        if args.factory:
            shell(con, root_rw=False, root=args.root)
            done.append(factory(con))
            con.run("sync")
        else:
            mac, root = shell(con, root=args.root)
            done.append(f"root file system: {root}")
            done.append(root_login(con))
            done.append(quiet_console(con))
            if con.run("mount -t ubifs ubi0:user-config "
                       "/media/user-config") != 0:
                raise RuntimeError("could not mount user-config")
            done.append(wifi(con, mac, args.ssid))
            done.append(onboarding(con))
            done.append(wiring(con))
            done.append(nestlocal(con))
            # Unmounting commits the UBIFS journals before we pull the plug.
            if con.run("sync; umount /media/user-config") != 0:
                raise RuntimeError("user-config did not unmount cleanly")
            if con.run("mount -o remount,ro /") != 0:
                raise RuntimeError(f"{root} did not remount read-only")
    except RuntimeError as e:
        tail = con.buf.decode(errors="replace").strip().splitlines()[-5:]
        print(*("provision: " + d for d in done), f"provision: failed: {e}",
              *("  " + l for l in tail), sep="\n", file=sys.stderr)
        sys.exit(1)
    finally:
        con.quit()
    print(*("provision: " + d for d in done), sep="\n", file=sys.stderr)


def gen2(args, cmd):
    done = []
    try:
        mac, root, restore = gen2_prepare(args.gen2)
    except (RuntimeError, ValueError) as e:
        print(f"provision: failed: {e}", file=sys.stderr)
        sys.exit(1)
    con = Console(cmd, args.transcript)
    try:
        if args.factory:
            gen2_shell(con, root_rw=False)
            done.append(gen2_factory(con))
            con.run("sync")
        else:
            gen2_shell(con)
            done.append(f"root file system: {root}")
            done.append(root_login(con, replace=True))
            done.append(console_login(con))
            done.append(quiet_console(con))
            if con.run("mount -t jffs2 /dev/mtdblock11 "
                       "/media/user-config") != 0:
                raise RuntimeError("could not mount user-config")
            done.append(wifi(con, mac, args.ssid))
            done.append(onboarding(con))
            done.append(wiring(con))
            done.append(nestlocal(con, gen2=True))
            if con.run("sync; umount /media/user-config") != 0:
                raise RuntimeError("user-config did not unmount cleanly")
            if con.run("mount -o remount,ro /") != 0:
                raise RuntimeError(f"{root} did not remount read-only")
    except RuntimeError as e:
        tail = con.buf.decode(errors="replace").strip().splitlines()[-5:]
        print(*("provision: " + d for d in done), f"provision: failed: {e}",
              *("  " + l for l in tail), sep="\n", file=sys.stderr)
        sys.exit(1)
    finally:
        con.quit()
        load("j49_env", "j49_env.py").set_env(args.gen2, restore)
    print(*("provision: " + d for d in done), sep="\n", file=sys.stderr)


if __name__ == "__main__":
    main()
