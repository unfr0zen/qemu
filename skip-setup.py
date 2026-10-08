#!/usr/bin/env python3
"""Mark the emulated thermostat's out-of-box setup as done.

A unit fresh from the stock image boots into nlclient's setup (language,
network, location, equipment, ...). The app keeps its progress in
/nestlabs/etc/user/settings.config; when the Nest phone app finishes setup
("ConfigDone", 0x754dc in nlclient) it sets the *_completed flags below
and the Eco onboarding flags, and the location step stores
location_country/_zipcode and location_set.
This writes the same settings with nlclient stopped, then restarts it.
The settings live in the NAND working copy, so this sticks until
NAND_FRESH=1.

Needs `./run.sh --ui` and a root shell on the console (docs/qemu.md,
"Root access and setup").

Usage: ./skip-setup.py [--language en_US] [--country US] [--zip 94043]
"""
import argparse
import random
import re
import sys
import threading
import time
import urllib.request

SETTINGS = "/nestlabs/etc/user/settings.config"
COMPLETED = """appsetup_completed date_time_adj_completed devicename_completed
devicewhere_completed interview_completed network_completed
oob_account_completed oob_interview_completed oob_startup_completed
oob_summary_completed oob_temp_completed oob_temps_completed
oob_test_completed oob_where_completed oob_wifi_completed oob_wires_completed
startup_sequence_completed summary_completed system_test_completed
wires_completed""".split()


class Console:
    """The guest console: /logs (SSE) in, /console/send out."""

    def __init__(self, base):
        self.base = base
        self.lines = []
        self.cond = threading.Condition()
        threading.Thread(target=self._follow, daemon=True).start()
        time.sleep(1)                              # let the backlog arrive

    def _follow(self):
        with urllib.request.urlopen(self.base + "/logs") as r:
            for raw in r:
                line = raw.decode(errors="replace").rstrip("\n")
                if line.startswith("data: "):
                    with self.cond:
                        self.lines.append(line[6:])
                        self.cond.notify_all()

    def send(self, text):
        req = urllib.request.Request(self.base + "/console/send",
                                     data=text.encode(), method="POST")
        urllib.request.urlopen(req).read()

    def run(self, command, timeout=120):
        """Run a shell command, return its output lines."""
        tag = f"NGL{random.randrange(10**8):08d}"
        with self.cond:
            start = len(self.lines)
        # The tag is split in the command so only the echo's output matches.
        # On its own line, so a here-document terminator stays alone too.
        self.send(f'{command}\necho "{tag[:3]}""{tag[3:]}" $?')
        deadline = time.time() + timeout
        with self.cond:
            while True:
                for i in range(start, len(self.lines)):
                    m = re.search(rf"{tag} (\d+)$", self.lines[i])
                    if m:
                        out = [l for l in self.lines[start:i] if tag[3:] not in l]
                        return int(m.group(1)), out
                left = deadline - time.time()
                if left <= 0:
                    sys.exit(f"timeout: {command[:60]}")
                self.cond.wait(left)


def setup_values(language="en_US", country="US", zipcode="94043"):
    """The settings.config keys ConfigDone sets, as {key: value}."""
    values = {k: "1" for k in COMPLETED}
    # ConfigDone also marks Eco onboarding done (0x7566c); without it the
    # "Introducing Eco Temperatures" alert greets every boot.
    values.update(is_onboarded_to_eco="1", installed_with_eco="1")
    values.update(language=language, device_locale=language,
                  location_set="1", location_country=country,
                  location_zipcode=zipcode, using_geo_ip="0")
    return values


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ui", default="http://127.0.0.1:8075")
    ap.add_argument("--language", default="en_US",
                    help="a locale nlclient ships (en_US, en_GB, de_DE, ...)")
    ap.add_argument("--country", default="US")
    ap.add_argument("--zip", default="94043")
    args = ap.parse_args()

    values = setup_values(args.language, args.country, args.zip)
    xml = "".join(f'  <s key="{k}" value="{v}"/>\n' for k, v in values.items())
    drop = "|".join(f'"{k}"' for k in values)

    con = Console(args.ui)
    # nlclient rewrites the file from memory, so it must be down meanwhile.
    rc, _ = con.run("monit unmonitor nlclient; kill $(pidof nlclient) 2>/dev/null; "
                    f"sleep 3; cp {SETTINGS} /tmp/settings.bak && "
                    f"grep -vE '</settings>|{drop}' /tmp/settings.bak > {SETTINGS}")
    if rc:
        sys.exit("could not edit the settings (logged in as root?)")
    rc, _ = con.run(f"cat >> {SETTINGS} <<'NGLEOF'\n{xml}</settings>\nNGLEOF")
    if rc:
        sys.exit("writing the settings failed; the old ones are in /tmp/settings.bak")
    con.run("sync; monit monitor nlclient; monit start nlclient")
    print(f"setup marked complete ({args.language}, {args.country} {args.zip}); "
          "nlclient restarting")


if __name__ == "__main__":
    main()
