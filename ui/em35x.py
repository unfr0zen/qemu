#!/usr/bin/env python3
"""
The other end of the em35x-ncp device's chardev: the Nest Display-3.4's
802.15.4 / Thread radio, an EM35x running Silicon Labs' ConnectIP
network co-processor ("ip-modem-app", which the firmware ships in
/usr/share/silabs/data/firmware/). wpantund's silabs plugin drives it
through spi-server. This is the application; the SPI port (framing,
nHOST_INT) is the device in QEMU (hw/misc/em35x_ncp.c).

Chardev (two bytes a message):
  from QEMU:  'd' byte   a stream byte from the host
              's' level  nSSEL      'r' level  nRESET      'w' level  nWAKE
  to QEMU:    'd' byte   a stream byte for the host
              'a' level  SPI port up (the chip runs)
              'f' 0      drop what the port holds (the chip rebooted)
              'z' 0      deep sleep once the port has sent what it holds

Stream, both ways (serial-link.c):
  5b | type u8 | len u16 BE | payload        type 1 = management
Management payload: 01 | id u16 BE | args. Host commands are 0x69xx,
chip callbacks 0x63xx. Args: u/s one byte, v u16 BE, w u32 BE, b a length
byte and that many bytes.

What the chip does (ip-modem-app; the host side from wpantund's
ncp-silabs.so):
  boot (nRESET released, a Reset command, power-on): CBReset(cause:
       3 pin, 6 command, 4 power-on), then unsolicited CBState, CBInitHost
       and CBGetVersions. wpantund wants CBReset within 0.5 s of spi-server
       pulsing nRESET, then CBGetVersions; the version must be one it
       supports ("ConnectIP/2.0.") and the one zb-loader finds in the
       firmware file (2.0.4b301 s4), or it reflashes the chip.
  0x6900 Reset          no reply: reboots (cause 6)
  0x6902 InitHost       CBState, CBInitHost(0), CBGetVersions
  0x6903 GetState       CBState
  0x6904 GetVersions    CBGetVersions
  0x6905 Form           alone on the air, the chip becomes the leader of
                        a network of its own, on the first channel of the
                        mask, after (channels + 1) x 1.98 s of scanning:
                        CBSetAddress (mesh-local EID), CBSetDriverAddress
                        (RLOC), the network keys, CBState (joined), CBForm(0)
  0x6906 Join           finds no parent: after 3 scans of the mask,
                        CBState, CBJoin(0xc0)
  0x6907 Resume         a saved network: nobody to re-attach to, so after a
                        few seconds it leads its own partition again
                        (addresses, CBState, CBJoin(0)); none: CBResume(0x70)
  0x6901 Leave          forgets the network: CBState, CBRebootNetwork(0)
  0x6911 PermitJoin, 0x691f Listen, 0x694e SetActiveWakeup: their callback,
                        0 when joined (0x70 EMBER_INVALID_CALL otherwise)
  0x6931 SetPassthruPort: no reply
  queries of a lone leader: 0x6917 GetRIPEntry (an empty slot; 0xff, an
  enumeration of the valid ones: none), 0x6919 GetCounter (0), 0x691d,
  0x6925, 0x6927
  (TX power mode, CCA threshold, TX power), 0x6934 GPIOGet, 0x6945
  ChannelCalGet (erased), 0x6946 DataPoll, 0x6948 StackDataPoll, 0x6949
  OKToNap, 0x692a Echo, 0x6993/0x6994; 0x6918, 0x691a, 0x6912 and 0x6960
  have no reply
Data frames (types 2 and 4) go out on the air, and nobody is there: no
callback.
  0x691c SetTXPowerMode, 0x6926 SetCCAThreshold, 0x6928 SetRadioPower:
                        their callback, status 0
  0x6914 ActiveScan / 0x6913 EnergyScan (channel mask, duration):
                        (2^duration + 1) x 15.36 ms per channel, in
                        ascending order; an energy scan reports each
                        channel's peak RSSI (0x6310), an active scan
                        hears no beacons; then CBScanReturn(0)
  0x6915 StopScan       ends a running scan with CBScanReturn(0)
  0x691e SleepNoTimer   CB 0x632e, then deep sleep (the port's: the SPI
                        transaction or nWAKE edge that wakes it)
  0x6924 FFWakeup       nothing: the transaction carrying it wakes the chip
  0x690d SetSecurityParams (key, -, flags): CB 0x6306(0); bit 0 of the flags
                        makes it key sequence 1
  0x694c                0x6349 (not joined)
  0x6959 GetGlobalAddresses: one 0x6356 per address, so none
  0x6990 WriteNestMACAddr: 0x6390; the EUI-64 is used from the next boot
  0x6991 SetTransmitHookActive: 0x6391 (--gen2 only: the gen 2's EM357
                        build has it, the gen 3's 0x6993/0x6994 instead)
The network the chip forms is kept like its flash tokens: across nRESET
(a reboot reports it saved, network state 1, and the host resumes it) but
not across a restart of this script, which starts a chip with none.
--debug logs the stream.
"""
import argparse
import os
import socket
import struct
import sys
import threading
import time

# CBGetVersions of the image both 5.9.4-5 and 6.4-8 ship (ip-modem-app.bin:
# stack version 0x2044 and build 301 in its application address table at
# file offset 0x201c, the name and date at 0x6ab8 and 0x6aa0).
NAME = b"ConnectIP\0"
MGMT_VERSION = 7
STACK_VERSION = 0x2044
BUILD = 301
VERSION_TYPE = 1
DATE = b"Sep 23 2015 16:39:49\0"
DATE_GEN2 = b"Sep 23 2015 16:39:22\0"   # the gen 2's EM357 build

BOOT_TIME = 0.03        # nRESET to CBReset; the host waits up to 0.5 s

CAUSE_PWR, CAUSE_PIN, CAUSE_SW = 4, 3, 6

SCAN_CHANNELS = 0x07FFF800  # channels 11-26
NOISE_FLOOR = -95           # dBm, what an energy scan hears on each channel

CB_RESET = 0x6300
CB_STATE = 0x6301
CB_GET_VERSIONS = 0x6302
CB_INIT_HOST = 0x6304
CB_SET_SECURITY = 0x6306
CB_FORM = 0x630A
CB_JOIN = 0x630B
CB_RESUME = 0x630C
CB_PERMIT_JOIN = 0x630D
CB_SET_ADDRESS = 0x6313
CB_SET_DRIVER_ADDRESS = 0x6314
CB_SET_NETWORK_KEYS = 0x6318
CB_LISTEN = 0x631B
CB_ENERGY_SCAN = 0x6310
CB_SCAN_RETURN = 0x6312
CB_SET_TX_POWER_MODE = 0x6316
CB_SET_CCA = 0x6322
CB_SET_RADIO_POWER = 0x6324
CB_SLEEP_NO_TIMER = 0x632E
CB_SET_ACTIVE_WAKEUP = 0x634B
CB_REBOOT_NETWORK = 0x634C
CB_WRITE_MAC = 0x6390

EMBER_INVALID_CALL = 0x70
EMBER_BAD_ARGUMENT = 0x35
EMBER_MAC_BAD_SCAN_DURATION = 0x33
EMBER_MAC_SCANNING = 0x3D
EMBER_JOIN_FAILED = 0xC0

NO_NETWORK, SAVED, JOINING, JOINED = 0, 1, 2, 3
FORM_SCAN_TIME = 1.98   # s per channel scanned when forming or joining
RESUME_TIME = 3.0       # s to give up re-attaching and lead alone


def pack(fmt, *args):
    out = b""
    for f, a in zip(fmt, args):
        if f in "us":
            out += struct.pack(">b" if f == "s" else ">B", a)
        elif f == "v":
            out += struct.pack(">H", a)
        elif f == "w":
            out += struct.pack(">I", a)
        elif f == "b":
            out += bytes((len(a),)) + a
    return out


class Link:
    """The chardev: line events and host stream bytes in, stream bytes and
    port controls out."""

    def __init__(self, spec, debug=False):
        self.spec = spec
        self.debug = debug
        self.sock = None
        self.lock = threading.Lock()

    def connect(self):
        _, host, port = self.spec.split(":", 2)
        while True:
            try:
                self.sock = socket.create_connection((host, int(port)), timeout=3)
                self.sock.settimeout(None)
                self.log(f"connected to {self.spec}")
                return
            except OSError:
                time.sleep(0.5)

    def log(self, msg):
        if self.debug:
            print(f"{time.monotonic():.3f} {msg}", file=sys.stderr, flush=True)

    def send(self, kind, val):
        self.sendall(bytes((ord(kind), val)))

    def sendall(self, data):
        with self.lock:
            try:
                self.sock.sendall(data)
            except OSError:
                pass

    def stream(self, data):
        self.sendall(b"".join(b"d" + bytes((b,)) for b in data))

    def messages(self):
        """Yield (kind, value) from QEMU until the connection drops."""
        buf = b""
        while True:
            data = self.sock.recv(4096)
            if not data:
                raise OSError("closed")
            buf += data
            n = len(buf) // 2 * 2
            for i in range(0, n, 2):
                yield chr(buf[i]), buf[i + 1]
            buf = buf[n:]


class Chip:
    """The ConnectIP NCP: boots, answers management commands, sleeps."""

    def __init__(self, link, eui64, gen2=False):
        self.link = link
        self.gen2 = gen2
        self.lock = threading.Lock()
        self.eui64 = eui64          # as printed; the chip holds it reversed
        self.mac_token = None       # 0x6990's, used from the next boot
        self.lines = {"s": 1, "r": 1, "w": 1}
        self.running = False
        self.boot_timer = None
        self.scan_timer = None
        self.rx = b""
        self.tx_power = 3
        self.key = None             # the network key table's sequence 1
        self.network = None         # the saved network (a flash token)
        self.net_state = NO_NETWORK
        self.net_timer = None
        self.reset_state()

    def reset_state(self):
        self.running = False
        self.rx = b""
        if self.boot_timer:
            self.boot_timer.cancel()
            self.boot_timer = None
        self.stop_scan()
        if self.net_timer:
            self.net_timer.cancel()
            self.net_timer = None
        self.net_state = SAVED if self.network else NO_NETWORK

    # -- lines ----------------------------------------------------------

    def connected(self):
        """A new connection: the chip is off until QEMU tells the lines."""
        with self.lock:
            self.reset_state()

    def event(self, kind, val):
        with self.lock:
            if kind == "d":
                self.receive(val)
                return
            old = self.lines[kind]
            self.lines[kind] = val
            if kind == "r":
                if not val:
                    self.hold_reset()
                elif not old or not self.running:
                    self.power_up(CAUSE_PIN if not old else CAUSE_PWR)
            elif kind == "w":
                self.link.log(f"nWAKE = {val}")

    def hold_reset(self):
        self.link.log("nRESET = 0")
        self.reset_state()
        self.link.send("a", 0)
        self.link.send("f", 0)

    def power_up(self, cause):
        self.reset_state()
        self.link.send("a", 0)
        self.boot_timer = threading.Timer(BOOT_TIME, self.boot, (cause,))
        self.boot_timer.start()

    def boot(self, cause):
        with self.lock:
            self.boot_timer = None
            if not self.lines["r"]:
                return
            if self.mac_token:
                self.eui64 = self.mac_token
            self.running = True
            self.link.log(f"boot, reset cause {cause}")
            self.link.send("f", 0)
            self.link.send("a", 1)
            self.callback(CB_RESET, "u", cause)
            self.init_host()

    # -- stream ---------------------------------------------------------

    def receive(self, byte):
        if not self.running:
            return
        self.rx += bytes((byte,))
        while self.rx:
            start = self.rx.find(b"[")
            if start < 0:
                self.rx = b""
                return
            self.rx = self.rx[start:]
            if len(self.rx) < 4:
                return
            ftype, length = self.rx[1], struct.unpack(">H", self.rx[2:4])[0]
            if len(self.rx) < 4 + length:
                return
            payload, self.rx = self.rx[4:4 + length], self.rx[4 + length:]
            if ftype == 1 and len(payload) >= 3 and payload[0] == 1:
                self.command(struct.unpack(">H", payload[1:3])[0], payload[3:])
            else:
                self.link.log(f"<- type {ftype}: {payload.hex()}")

    def raw(self, cb, data=b""):
        """A callback whose args are given as bytes."""
        self.link.log(f"-> 0x{cb:04x} {data.hex()}")
        self.frame(1, b"\x01" + struct.pack(">H", cb) + data)

    def frame(self, ftype, payload):
        self.link.stream(b"[" + struct.pack(">BH", ftype, len(payload)) + payload)

    def callback(self, cb, fmt="", *args):
        self.link.log(f"-> 0x{cb:04x} {args}")
        self.frame(1, b"\x01" + struct.pack(">H", cb) + pack(fmt, *args))

    # -- management -----------------------------------------------------

    def command(self, cmd, args):
        self.link.log(f"<- 0x{cmd:04x} {args.hex()}")
        if cmd == 0x6900:                       # Reset
            self.power_up(CAUSE_SW)
        elif cmd == 0x6902:                     # InitHost
            self.init_host()
        elif cmd == 0x6903:                     # GetState
            self.state()
        elif cmd == 0x6904:                     # GetVersions
            self.versions()
        elif cmd == 0x6905 and len(args) >= 34:  # Form
            self.form(args)
        elif cmd == 0x6906:                     # Join
            self.join(args)
        elif cmd == 0x6907:                     # Resume
            self.resume()
        elif cmd == 0x6901:                     # Leave
            self.network = None
            self.net_state = NO_NETWORK
            self.state()
            self.callback(CB_REBOOT_NETWORK, "u", 0)
        elif cmd == 0x6911:                     # PermitJoin
            self.callback(CB_PERMIT_JOIN, "u", self.joined_status())
        elif cmd == 0x691F:                     # Listen
            self.callback(CB_LISTEN, "u", self.joined_status())
        elif cmd == 0x694E:                     # SetActiveWakeup
            self.callback(CB_SET_ACTIVE_WAKEUP, "u", self.joined_status())
        elif cmd in (0x6931, 0x6918, 0x691A, 0x6912, 0x6960):
            pass                                # no reply
        elif cmd == 0x6917 and args:            # GetRIPEntry
            if args[0] < 0x40:                  # an empty slot
                self.raw(0x6303, bytes((args[0], 8)) + bytes(16))
        elif cmd == 0x6919 and args:            # GetCounter
            self.raw(0x6305, bytes((args[0], 0, 0)))
        elif cmd == 0x691D:                     # GetTXPowerMode
            self.raw(0x6317, b"\x80\x00")
        elif cmd == 0x6925:                     # GetCCAThreshold
            self.raw(0x6321, b"\x7f")
        elif cmd == 0x6927:                     # GetRadioPower
            self.raw(0x6323, struct.pack(">b", self.tx_power))
        elif cmd == 0x6934 and args:            # GPIOGet
            self.raw(0x632B, b"\x00" if args[0] < 0x30 else b"\xff")
        elif cmd == 0x6945:                     # ChannelCalGet: erased token
            self.raw(0x6341, b"\xff" * 4)
        elif cmd == 0x6946:                     # DataPoll
            self.raw(0x6342, b"\x93\x00")
        elif cmd == 0x6948:                     # StackDataPoll
            if args and any(args[:4]):
                self.raw(0x6342, b"\x93\x00")
            self.raw(0x6344, b"\x00")
        elif cmd == 0x6949:                     # OKToNap: the stack is idle
            self.raw(0x6345, b"\x01")
        elif cmd == 0x692A and args:            # Echo
            self.raw(0x6326, args[:1 + args[0]])
        elif cmd == 0x6993:
            self.raw(0x6393, b"\x00")
        elif cmd == 0x6994:
            self.raw(0x6394, bytes(4))
        elif cmd == 0x691C:                     # SetTXPowerMode
            self.callback(CB_SET_TX_POWER_MODE, "u", 0)
        elif cmd == 0x6926:                     # SetCCAThreshold
            self.callback(CB_SET_CCA, "u", 0)
        elif cmd == 0x6928 and args:            # SetRadioPower
            self.tx_power = struct.unpack(">b", args[:1])[0]
            self.callback(CB_SET_RADIO_POWER, "u", 0)
        elif cmd in (0x6913, 0x6914) and len(args) >= 5:   # Energy/ActiveScan
            mask, duration = struct.unpack(">IB", args[:5])
            self.scan(cmd == 0x6913, mask, duration)
        elif cmd == 0x6915:                     # StopScan
            if self.stop_scan():
                self.callback(CB_SCAN_RETURN, "u", 0)
        elif cmd == 0x691E:                     # SleepNoTimer
            self.callback(CB_SLEEP_NO_TIMER)
            self.link.send("z", 0)
        elif cmd == 0x6924:                     # FFWakeup: the wake is the SPI
            pass
        elif cmd == 0x690D and len(args) >= 17 and args[0] == 16:
            flags = args[-2] << 8 | args[-1]    # SetSecurityParams
            if self.net_state != NO_NETWORK:
                self.callback(CB_SET_SECURITY, "u", EMBER_INVALID_CALL)
                return
            if flags & 1:
                self.key = args[1:17]
            self.callback(CB_SET_SECURITY, "u", 0)
        elif cmd == 0x694C and self.net_state != JOINED:
            self.callback(0x6349, "uuvuvbw", EMBER_INVALID_CALL, 0, 0, 0, 5,
                          bytes(8), 0)
        elif cmd == 0x6959:                     # GetGlobalAddresses: none
            pass
        elif cmd == 0x6990 and args and args[0] == 8:   # WriteNestMACAddr
            self.mac_token = bytes(reversed(args[1:9]))
            self.callback(CB_WRITE_MAC)
        elif cmd == 0x6991 and self.gen2:       # SetTransmitHookActive
            self.callback(0x6391)
        else:
            print(f"em35x: unmodelled command 0x{cmd:04x} {args.hex()}",
                  file=sys.stderr, flush=True)

    def scan(self, energy, mask, duration):
        if self.scan_timer:
            status = EMBER_MAC_SCANNING
        elif duration >= 15:
            status = EMBER_MAC_BAD_SCAN_DURATION
        elif mask & ~SCAN_CHANNELS:
            status = EMBER_BAD_ARGUMENT
        else:
            status = 0
        if status:
            self.callback(CB_SCAN_RETURN, "u", status)
            return
        channels = [c for c in range(11, 27) if mask & (1 << c)] or [11]
        self.scan_step(energy, channels, ((1 << duration) + 1) * 0.01536)

    def scan_step(self, energy, channels, dwell):
        """Listen on channels[0] for dwell seconds, then report it."""
        def done():
            with self.lock:
                if self.scan_timer is not timer:
                    return
                self.scan_timer = None
                if energy:
                    self.callback(CB_ENERGY_SCAN, "us", channels[0],
                                  NOISE_FLOOR)
                if channels[1:]:
                    self.scan_step(energy, channels[1:], dwell)
                else:
                    self.callback(CB_SCAN_RETURN, "u", 0)
        timer = self.scan_timer = threading.Timer(dwell, done)
        timer.start()

    def stop_scan(self):
        """Abort a running scan; whether one was running."""
        timer, self.scan_timer = self.scan_timer, None
        if timer:
            timer.cancel()
        return timer is not None

    # -- the network ----------------------------------------------------

    def joined_status(self):
        return 0 if self.net_state == JOINED else EMBER_INVALID_CALL

    def after(self, delay, fn):
        """Run fn (under the lock) in delay s, unless the chip resets."""
        def fire():
            with self.lock:
                if self.net_timer is timer:
                    self.net_timer = None
                    fn()
        timer = self.net_timer = threading.Timer(delay, fire)
        timer.start()

    def form(self, args):
        name, ula = args[1:17], args[18:26]
        node_type, tx_power = args[26], struct.unpack(">b", args[27:28])[0]
        pan, mask = struct.unpack(">HI", args[28:34])
        if self.net_state != NO_NETWORK or self.net_timer or self.scan_timer:
            self.state()
            self.callback(CB_FORM, "u", EMBER_INVALID_CALL)
            return
        channels = [c for c in range(11, 27) if mask & (1 << c)] or [11]
        self.net_state = JOINING
        self.tx_power = tx_power

        def formed():
            # Every channel is as quiet as the next: the first one wins.
            self.network = {
                "name": name, "ula": ula, "xpanid": os.urandom(8),
                "pan": pan if pan != 0xFFFF else struct.unpack(">H", os.urandom(2))[0],
                "channel": channels[0], "node_type": node_type,
            }
            if not self.key:
                self.key = os.urandom(16)
            self.attached()
            self.callback(CB_FORM, "u", 0)
        self.after((len(channels) + 1) * FORM_SCAN_TIME, formed)

    def join(self, args):
        if self.net_state != NO_NETWORK or self.net_timer or self.scan_timer:
            self.state()
            self.callback(CB_JOIN, "u", EMBER_INVALID_CALL)
            return
        # Its mask is the last 'w' of 'bbvusvw'; nobody answers on it.
        mask = struct.unpack(">I", args[-4:])[0] if len(args) >= 4 else 0
        channels = [c for c in range(11, 27) if mask & (1 << c)] or [11]
        self.net_state = JOINING

        def failed():
            self.net_state = NO_NETWORK
            self.state()
            self.callback(CB_JOIN, "u", EMBER_JOIN_FAILED)
        self.after(3 * len(channels) * FORM_SCAN_TIME, failed)

    def resume(self):
        if self.net_state != SAVED or self.net_timer:
            self.state()
            self.callback(CB_RESUME, "u", EMBER_INVALID_CALL)
            return
        self.addresses()
        self.net_state = JOINING

        def alone():
            # No parent answered: it leads a partition of its own.
            self.net_state = JOINED
            self.state()
            self.callback(CB_JOIN, "u", 0)
        self.after(RESUME_TIME, alone)

    def attached(self):
        self.net_state = JOINED
        self.addresses()
        self.keys()
        self.state()

    def addresses(self):
        ula = self.network["ula"]
        # Mesh-local EID from the EUI-64; the leader's RLOC16 is 0x0000.
        eid = bytes((self.eui64[0] ^ 0x02,)) + self.eui64[1:]
        self.callback(CB_SET_ADDRESS, "b", ula + eid)
        self.callback(CB_SET_DRIVER_ADDRESS, "b",
                      ula + b"\x00\x00\x00\xff\xfe\x00\x00\x00")

    def keys(self):
        self.callback(CB_SET_NETWORK_KEYS, "wbwb", 1, self.key, 0, b"")

    def init_host(self):
        self.state()
        if self.net_state == JOINED:
            self.keys()
            self.addresses()
        self.callback(CB_INIT_HOST, "u", 0)
        self.versions()

    def state(self):
        n = self.network
        if n and self.net_state in (SAVED, JOINED):
            self.callback(CB_STATE, "bbbvuusub", n["name"], n["ula"],
                          n["xpanid"], n["pan"], n["channel"], n["node_type"],
                          self.tx_power, self.net_state,
                          bytes(reversed(self.eui64)))
            return
        # No network: zero name, mesh-local prefix and extended PAN id,
        # PAN 0xfffe, channel 11, node type 2.
        self.callback(CB_STATE, "bbbvuusub", bytes(16), bytes(8), bytes(8),
                      0xFFFE, 11, 2, self.tx_power, self.net_state,
                      bytes(reversed(self.eui64)))

    def versions(self):
        self.callback(CB_GET_VERSIONS, "bvvvub", NAME, MGMT_VERSION,
                      STACK_VERSION, BUILD, VERSION_TYPE,
                      DATE_GEN2 if self.gen2 else DATE)


def follow_parent(pid):
    # Reparenting, not kill(pid, 0): a dead parent stays a zombie (and its
    # PID can be reused) until whoever started run.sh reaps it.
    while os.getppid() == pid:
        time.sleep(1)
    os._exit(0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", default="tcp:127.0.0.1:8106",
                    help="the em35x-ncp chardev (tcp:HOST:PORT)")
    ap.add_argument("--parent", type=int, help="exit when this PID exits")
    ap.add_argument("--debug", action="store_true", help="log the stream")
    ap.add_argument("--eui64", default="18:b4:30:00:00:00:00:01",
                    help="the radio's EUI-64 (default: a placeholder)")
    ap.add_argument("--gen2", action="store_true",
                    help="the gen 2's EM357 firmware build")
    args = ap.parse_args()
    eui64 = bytes.fromhex(args.eui64.replace(":", ""))
    if len(eui64) != 8:
        ap.error("--eui64 takes 8 bytes")
    if args.parent:
        threading.Thread(target=follow_parent, args=(args.parent,),
                         daemon=True).start()
    link = Link(args.serial, args.debug)
    chip = Chip(link, eui64, args.gen2)
    while True:
        link.connect()
        chip.connected()
        try:
            for kind, val in link.messages():
                chip.event(kind, val)
        except OSError as e:
            link.log(f"connection lost ({e}), reconnecting")
            time.sleep(0.5)


if __name__ == "__main__":
    main()
