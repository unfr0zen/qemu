#!/usr/bin/env python3
"""
The other end of the spinel-ncp device's chardev: the Nest Thermostat E's
802.15.4 / Thread radio, an EM358 running OpenThread's network
co-processor ("ot-ncp-app", which the firmware ships in
/usr/share/marble/data/firmware/). wpantund's spinel driver drives it
through spi-hdlc-adapter. This is the application; the SPI port (frame
headers, the interrupt) is the device in QEMU (hw/misc/spinel_ncp.c).

Chardev:
  from QEMU:  'F' len16 bytes   a spinel frame from the host (len16 LE)
              'd' byte          in raw mode, a byte the host clocked in
              'r' level         nRESET
              'w' level         nWAKE
  to QEMU:    'F' len16 bytes   a spinel frame for the host; in raw mode
                                the reply the next transaction clocks out
              'b' 0             the chip rebooted (drop what the port holds)
              'm' raw           the port's mode: 1 raw (the bootloader),
                                0 spinel

Spinel frame: header (0x80 | IID << 4 | TID), command and property as
packed unsigned integers (7 bits a byte, low first), then the value.

What the chip does: a lone radio with no saved network, so wpantund
brings it up and finds it "offline".
  boot (nRESET released, a RESET command, power-on): unsolicited
       LAST_STATUS RESET_EXTERNAL / RESET_SOFTWARE / RESET_POWER_ON
  NOOP                 LAST_STATUS OK
  PROP_VALUE_GET       PROP_VALUE_IS with the value it holds, or
                       LAST_STATUS PROP_NOT_FOUND
  PROP_VALUE_SET       keeps the value, PROP_VALUE_IS with it; the
                       streams (debug, raw, net) go out on the air, where
                       nobody is: LAST_STATUS OK
  PROP_VALUE_INSERT    PROP_VALUE_INSERTED, REMOVE: PROP_VALUE_REMOVED
  MAC_SCAN_STATE       a beacon, energy or discover scan lasts the scan
                       period on each channel of the mask and hears
                       nobody: an energy scan reports the noise floor on
                       each (MAC_ENERGY_SCAN_RESULT), then MAC_SCAN_STATE
                       goes back to idle
  NCP_VERSION          the version string of the application in flash

The bootloader: nWAKE low while nRESET rises (zb-loader --force-bootloader,
its "bootmode") starts Nest's SPI bootloader, which zb-loader
(nlbl-em35x-1.04) drives to flash a new ot-ncp-app.bin: wpantund runs it
when ncp-upgrade-check finds NCP_VERSION missing from the firmware file.
A command is one transaction (the host keeps the chip selected after it),
the reply a later one that clocks out 0xee; the bootloader takes the
command as its last byte arrives, and the interrupt goes low once the
reply is ready.
  command: ff fe len16 (bytes that follow) | 02 00 cmd16 | 4 bytes |
           address u32 | length u32 | [data] | cafe1a7e (LE) | ff
  reply:   ff fe 11 00 | 02 00 cmd16 | 0 u32 | value u32 | 0 u32 | ff
  2 erase  the range reads back 0xff; zb-loader reads the reply 2 s on,
           without waiting for the interrupt
  3 write  up to 64 bytes of data at the address (a fixed 64-byte field);
           the value is the address
  7 CRC    the value is the CRC-32 of the range (reflected, polynomial
           0xedb88320, initial ~0, no final inversion)
The application sits at 0x08002000, behind the bootloader's 8 KiB.
zb-loader's last step, a ConnectIP "firmware seems responsive" probe,
fails on any OpenThread image (the chip as well as this model), so
wpantund's FirmwareUpgradeCommand ends in "|| true"; wpantund then finds
the new version.

With --flash FILE the application survives a restart, as the chip's
flash does: it is read from FILE and saved there after the bootloader
writes it. Without it, and until the first update, the chip runs the
firmware the unit shipped with (NCP_VERSION).
"""

import argparse
import os
import re
import socket
import struct
import sys
import threading
import time
import zlib

# ot-ncp-app.bin as 6.1-9 ships it: the string ncp-upgrade-check looks for.
NCP_VERSION = "OPENTHREAD/1.0d824; Marble-Onyx1-release; Aug  6 2019 02:29:15"
VERSION_RE = re.compile(rb"OPENTHREAD/[ -~]+")

APP_BASE = 0x08002000       # behind the bootloader
APP_SIZE = 0x7e000          # to the end of the EM358's 512 KiB
BL_MAGIC = 0xcafe1a7e
BL_ERASE, BL_WRITE, BL_CRC = 2, 3, 7
PROTOCOL_VERSION = (4, 3)

CMD_NOOP, CMD_RESET, CMD_GET, CMD_SET, CMD_INSERT, CMD_REMOVE = range(6)
CMD_IS, CMD_INSERTED, CMD_REMOVED = 6, 7, 8

STATUS_OK = 0
STATUS_INVALID_COMMAND = 5
STATUS_PROP_NOT_FOUND = 13
STATUS_RESET_POWER_ON, STATUS_RESET_EXTERNAL, STATUS_RESET_SOFTWARE = (
    112, 113, 114)

PROP_LAST_STATUS = 0
PROP_MAC_SCAN_STATE, PROP_MAC_SCAN_MASK, PROP_MAC_SCAN_PERIOD = 0x30, 0x31, 0x32
PROP_MAC_ENERGY_SCAN_RESULT = 0x39
SCAN_BEACON, SCAN_ENERGY, SCAN_DISCOVER = 1, 2, 3
NOISE_FLOOR = -95           # dBm, what an energy scan hears on each channel
PROP_STREAM_DEBUG, PROP_STREAM_RAW = 0x70, 0x71
PROP_STREAM_NET, PROP_STREAM_NET_INSECURE = 0x72, 0x73

CAPS = (
    5,          # COUNTERS
    12,         # UNSOL_UPDATE_FILTER
    13,         # MCU_POWER_STATE
    17,         # 802_15_4_2006
    24,         # 802_15_4_2450MHZ_OQPSK
    32,         # CONFIG_FTD
    48,         # ROLE_ROUTER
    49,         # ROLE_SLEEPY
    52,         # NET_THREAD_1_0
    520,        # CHILD_SUPERVISION
    1027,       # THREAD_JOINER
)

CHANNELS = bytes(range(11, 27))
MESH_LOCAL_PREFIX = bytes.fromhex("fdde ad00 beef 0000")


def packed(n):
    out = bytearray()
    while True:
        b = n & 0x7f
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def unpack(data, i):
    """A packed unsigned integer at data[i]: (value, next index)."""
    n = shift = 0
    while i < len(data):
        b = data[i]
        i += 1
        n |= (b & 0x7f) << shift
        shift += 7
        if not b & 0x80:
            return n, i
    raise ValueError("truncated packed integer")


def crc32_nest(data):
    """zb-loader's CRC: zlib's CRC-32 without the final inversion."""
    return zlib.crc32(data) ^ 0xffffffff


def defaults(eui64, version):
    ext = bytes((eui64[0] ^ 0x02,)) + eui64[1:]     # the extended address
    ll = bytes.fromhex("fe80 0000 0000 0000") + ext
    u8 = struct.Struct("<B").pack
    s8 = struct.Struct("<b").pack
    u16 = struct.Struct("<H").pack
    u32 = struct.Struct("<L").pack
    return {
        1: packed(PROTOCOL_VERSION[0]) + packed(PROTOCOL_VERSION[1]),
        2: version.encode() + b"\0",
        3: packed(3),                       # INTERFACE_TYPE: Thread
        4: packed(0),                       # VENDOR_ID
        5: b"".join(packed(c) for c in CAPS),
        6: u8(1),                           # INTERFACE_COUNT
        8: eui64,                           # HWADDR
        9: u8(0),                           # LOCK
        12: u8(4),                          # HOST_POWER_STATE: online
        13: u8(0),                          # MCU_POWER_STATE: on
        0x20: u8(0),                        # PHY_ENABLED
        0x21: u8(11),                       # PHY_CHAN
        0x22: CHANNELS,                     # PHY_CHAN_SUPPORTED
        0x23: u32(2405000),                 # PHY_FREQ, kHz
        0x24: s8(-75),                      # PHY_CCA_THRESHOLD
        0x25: s8(0),                        # PHY_TX_POWER
        0x26: s8(-100),                     # PHY_RSSI
        0x27: s8(-100),                     # PHY_RX_SENSITIVITY
        0x30: u8(0),                        # MAC_SCAN_STATE: idle
        0x31: CHANNELS,                     # MAC_SCAN_MASK
        0x32: u16(300),                     # MAC_SCAN_PERIOD, ms
        0x34: ext,                          # MAC_15_4_LADDR
        0x35: u16(0xfffe),                  # MAC_15_4_SADDR
        0x36: u16(0xffff),                  # MAC_15_4_PANID
        0x37: u8(0),                        # MAC_RAW_STREAM_ENABLED
        0x38: u8(0),                        # MAC_PROMISCUOUS_MODE
        0x3a: u32(0),                       # MAC_DATA_POLL_PERIOD
        0x40: u8(0),                        # NET_SAVED
        0x41: u8(0),                        # NET_IF_UP
        0x42: u8(0),                        # NET_STACK_UP
        0x43: u8(0),                        # NET_ROLE: detached
        0x44: b"OpenThread\0",              # NET_NETWORK_NAME
        0x45: bytes.fromhex("dead00beef00cafe"),    # NET_XPANID
        0x46: bytes(16),                    # NET_NETWORK_KEY
        0x47: u32(0),                       # NET_KEY_SEQUENCE_COUNTER
        0x48: u32(0),                       # NET_PARTITION_ID
        0x49: u8(0),                        # NET_REQUIRE_JOIN_EXISTING
        0x4a: u32(624),                     # NET_KEY_SWITCH_GUARDTIME, h
        0x4b: bytes(16),                    # NET_PSKC
        0x52: b"",                          # THREAD_CHILD_TABLE
        0x54: u8(64),                       # THREAD_LEADER_WEIGHT
        0x55: u8(64),                       # THREAD_LOCAL_LEADER_WEIGHT
        0x56: b"",                          # THREAD_NETWORK_DATA
        0x57: u8(0),                        # THREAD_NETWORK_DATA_VERSION
        0x58: b"",                          # THREAD_STABLE_NETWORK_DATA
        0x59: u8(0),                        # ..._STABLE_NETWORK_DATA_VERSION
        0x5a: b"",                          # THREAD_ON_MESH_NETS
        0x5b: b"",                          # THREAD_OFF_MESH_ROUTES
        0x5c: b"",                          # THREAD_ASSISTING_PORTS
        0x5d: u8(0),                        # ..._ALLOW_LOCAL_NET_DATA_CHANGE
        0x5e: u8(0x0f),                     # THREAD_MODE: rx-on, FTD
        0x60: ll,                           # IPV6_LL_ADDR
        0x61: MESH_LOCAL_PREFIX + ext,      # IPV6_ML_ADDR
        0x62: MESH_LOCAL_PREFIX + u8(64),   # IPV6_ML_PREFIX
        0x63: b"",                          # IPV6_ADDRESS_TABLE
        0x64: b"",                          # IPV6_ROUTE_TABLE
        0x65: u8(0),                        # IPV6_ICMP_PING_OFFLOAD
        0x66: b"",                          # IPV6_MULTICAST_ADDRESS_TABLE
        0x1302: ext,                        # MAC_EXTENDED_ADDR
        0x1500: u32(240),                   # THREAD_CHILD_TIMEOUT, s
        0x1501: u16(0xfffe),                # THREAD_RLOC16
        0x1502: u8(16),                     # THREAD_ROUTER_UPGRADE_THRESHOLD
        0x1503: u32(48),                    # THREAD_CONTEXT_REUSE_DELAY, s
        0x1504: u8(120),                    # THREAD_NETWORK_ID_TIMEOUT, s
        0x1505: b"",                        # THREAD_ACTIVE_ROUTER_IDS
        0x1507: u8(1),                      # THREAD_ROUTER_ROLE_ENABLED
        0x150b: b"",                        # THREAD_NEIGHBOR_TABLE
        0x150c: u8(10),                     # THREAD_CHILD_COUNT_MAX
        0x4001: u8(4),                      # DEBUG_NCP_LOG_LEVEL
    }


class Link:
    """The chardev: host frames and nRESET in, frames and reboots out."""

    def __init__(self, spec, debug=False):
        self.spec = spec
        self.debug = debug
        self.sock = None
        self.lock = threading.Lock()

    def connect(self):
        _, host, port = self.spec.split(":", 2)
        while True:
            try:
                self.sock = socket.create_connection((host, int(port)),
                                                     timeout=3)
                self.sock.settimeout(None)
                self.log(f"connected to {self.spec}")
                return
            except OSError:
                time.sleep(0.5)

    def log(self, msg):
        if self.debug:
            print(f"{time.monotonic():.3f} {msg}", file=sys.stderr, flush=True)

    def sendall(self, data):
        with self.lock:
            try:
                self.sock.sendall(data)
            except OSError:
                pass

    def frame(self, data):
        self.log(f"-> {data.hex()}")
        self.sendall(b"F" + struct.pack("<H", len(data)) + data)

    def rebooted(self):
        self.sendall(b"b\0")

    def mode(self, raw):
        self.sendall(b"m" + bytes((raw,)))

    def messages(self):
        """Yield ('F', frame), ('d', byte), ('r', level) and ('w', level)
        until the connection drops."""
        buf = b""
        while True:
            data = self.sock.recv(4096)
            if not data:
                raise OSError("closed")
            buf += data
            while len(buf) >= 2:
                if buf[0] in b"rwd":
                    yield chr(buf[0]), buf[1]
                    buf = buf[2:]
                elif buf[0] == ord("F"):
                    if len(buf) < 3:
                        break
                    n = struct.unpack_from("<H", buf, 1)[0]
                    if len(buf) < 3 + n:
                        break
                    yield "F", buf[3:3 + n]
                    buf = buf[3 + n:]
                else:
                    self.log(f"bad message {buf[0]:#x}")
                    buf = buf[1:]


class Chip:
    def __init__(self, link, eui64, flash_file=None):
        self.link = link
        self.eui64 = eui64
        self.flash_file = flash_file
        self.app = None         # None: the firmware the unit shipped with
        if flash_file and os.path.exists(flash_file):
            with open(flash_file, "rb") as f:
                self.app = bytearray(f.read().ljust(APP_SIZE, b"\xff"))
        self.props = defaults(eui64, self.version() or "")
        self.nreset = self.nwake = True
        self.bootloader = False
        self.bl_frame = bytearray()
        self.running = self.version() is not None
        self.flashed = False
        self.boots = 0

    def version(self):
        if self.app is None:
            return NCP_VERSION
        m = VERSION_RE.search(self.app)
        return m.group().decode() if m else None

    def boot(self, cause):
        self.boots += 1         # ends a scan in progress
        if not self.nwake:
            self.bootloader = True
            self.bl_frame.clear()
            self.link.mode(1)
            self.link.log("bootloader")
            return
        if self.bootloader:
            self.bootloader = False
            self.link.mode(0)
            self.save()
        version = self.version()
        self.running = version is not None
        if not self.running:
            self.link.log("no application in flash")
            return
        self.props = defaults(self.eui64, version)
        self.link.rebooted()
        self.status(0, cause)

    def save(self):
        if not self.flashed or not self.flash_file:
            return
        self.flashed = False
        tmp = self.flash_file + ".tmp"
        with open(tmp, "wb") as f:
            f.write(self.app.rstrip(b"\xff"))
        os.replace(tmp, self.flash_file)
        print(f"spinel_ncp: flashed {self.version()}", file=sys.stderr,
              flush=True)

    def connected(self):
        self.nreset = self.nwake = True
        self.bootloader = False

    def reply(self, tid, cmd, prop, value=b""):
        self.link.frame(bytes((0x80 | tid,)) + packed(cmd) + packed(prop) +
                        value)

    def status(self, tid, code):
        self.reply(tid, CMD_IS, PROP_LAST_STATUS, packed(code))

    def event(self, kind, val):
        if kind == "w":
            self.nwake = bool(val)
            return
        if kind == "d":
            if self.bootloader and self.nreset:
                self.bl_byte(val)
            return
        if kind == "r":
            level = bool(val)
            if level and not self.nreset:
                self.boot(STATUS_RESET_EXTERNAL)
            elif level and self.nreset:
                self.boot(STATUS_RESET_POWER_ON)    # a new chardev link
            self.nreset = level
            return
        if not self.nreset:
            return
        if self.bootloader:
            self.bl_command(val)
            return
        if not self.running:
            return
        self.link.log(f"<- {val.hex()}")
        try:
            self.command(val)
        except (ValueError, IndexError) as e:
            self.link.log(f"bad frame {val.hex()}: {e}")

    def command(self, f):
        if len(f) < 2 or f[0] & 0xc0 != 0x80:
            return
        tid = f[0] & 0x0f
        cmd, i = unpack(f, 1)
        if cmd == CMD_NOOP:
            self.status(tid, STATUS_OK)
            return
        if cmd == CMD_RESET:
            self.boot(STATUS_RESET_SOFTWARE)
            return
        if cmd not in (CMD_GET, CMD_SET, CMD_INSERT, CMD_REMOVE):
            self.status(tid, STATUS_INVALID_COMMAND)
            return
        prop, i = unpack(f, i)
        value = f[i:]
        if cmd == CMD_GET:
            if prop == PROP_LAST_STATUS:
                self.status(tid, STATUS_OK)
            elif prop in self.props:
                self.reply(tid, CMD_IS, prop, self.props[prop])
            else:
                self.status(tid, STATUS_PROP_NOT_FOUND)
        elif cmd == CMD_SET:
            if prop in (PROP_STREAM_DEBUG, PROP_STREAM_RAW, PROP_STREAM_NET,
                        PROP_STREAM_NET_INSECURE):
                if tid:
                    self.status(tid, STATUS_OK)
                return
            self.props[prop] = bytes(value)
            self.reply(tid, CMD_IS, prop, self.props[prop])
            if prop == PROP_MAC_SCAN_STATE and value and value[0] in (
                    SCAN_BEACON, SCAN_ENERGY, SCAN_DISCOVER):
                threading.Thread(target=self.scan, daemon=True,
                                 args=(value[0], self.boots)).start()
        elif cmd == CMD_INSERT:
            self.reply(tid, CMD_INSERTED, prop, value)
        else:
            self.reply(tid, CMD_REMOVED, prop, value)

    def bl_byte(self, b):
        """A command frame, a byte at a time: ff fe len16, then len bytes."""
        f = self.bl_frame
        if not f and b != 0xff:
            return                      # a read's 0xee, or noise
        f.append(b)
        if len(f) == 2 and b != 0xfe:
            f.clear()
            return
        if len(f) < 4:
            return
        n = struct.unpack_from("<H", f, 2)[0] + 4
        if n > 256:
            f.clear()
        elif len(f) == n:
            self.bl_command(bytes(f))
            f.clear()

    def bl_command(self, f):
        n = len(f)
        if n < 21 or struct.unpack_from("<L", f, n - 5)[0] != BL_MAGIC:
            self.link.log(f"bootloader: bad frame {f[:n].hex()}")
            return
        cmd = struct.unpack_from("<H", f, 6)[0]
        addr, length = struct.unpack_from("<LL", f, 12)
        off = addr - APP_BASE
        if off < 0 or off + length > APP_SIZE:
            self.link.log(f"bootloader: {cmd} out of range {addr:#x}+{length}")
            return
        if self.app is None:
            self.app = bytearray(b"\xff" * APP_SIZE)
        value = 0
        if cmd == BL_ERASE:
            self.app[off:off + length] = b"\xff" * length
            self.flashed = True
        elif cmd == BL_WRITE:
            if length > 64:
                return
            self.app[off:off + length] = f[20:20 + length]
            self.flashed = True
            value = addr
        elif cmd == BL_CRC:
            value = crc32_nest(bytes(self.app[off:off + length]))
        else:
            self.link.log(f"bootloader: command {cmd}")
            return
        self.link.frame(b"\xff\xfe\x11\x00" + struct.pack("<HHLLL", 2, cmd, 0,
                                                         value, 0) + b"\xff")

    def scan(self, kind, boot):
        period = struct.unpack("<H", self.props[PROP_MAC_SCAN_PERIOD])[0]
        for channel in self.props[PROP_MAC_SCAN_MASK] or CHANNELS:
            time.sleep(period / 1000)
            if boot != self.boots:
                return
            if kind == SCAN_ENERGY:
                self.reply(0, CMD_IS, PROP_MAC_ENERGY_SCAN_RESULT,
                           struct.pack("<Bb", channel, NOISE_FLOOR))
        if boot == self.boots:
            self.props[PROP_MAC_SCAN_STATE] = b"\0"
            self.reply(0, CMD_IS, PROP_MAC_SCAN_STATE, b"\0")


def follow_parent(pid):
    # Reparenting, not kill(pid, 0): a dead parent stays a zombie (and its
    # PID can be reused) until whoever started run.sh reaps it.
    while os.getppid() == pid:
        time.sleep(1)
    os._exit(0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", default="tcp:127.0.0.1:8106",
                    help="the spinel-ncp chardev (tcp:HOST:PORT)")
    ap.add_argument("--parent", type=int, help="exit when this PID exits")
    ap.add_argument("--debug", action="store_true", help="log the frames")
    ap.add_argument("--eui64", default="18:b4:30:00:00:00:00:01",
                    help="the radio's EUI-64 (default: a placeholder)")
    ap.add_argument("--flash", metavar="FILE",
                    help="the chip's application flash, kept across restarts "
                         "(default: the shipped firmware, every start)")
    args = ap.parse_args()
    eui64 = bytes.fromhex(args.eui64.replace(":", ""))
    if len(eui64) != 8:
        ap.error("--eui64 takes 8 bytes")
    if args.parent:
        threading.Thread(target=follow_parent, args=(args.parent,),
                         daemon=True).start()
    link = Link(args.serial, args.debug)
    chip = Chip(link, eui64, args.flash)
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
