#!/usr/bin/env python3
"""
Virtual USB host for the cihdrc-udc bridge: the other end of the QEMU
device model's chardev socket (docs/usb-host-bridge-protocol.md).

The QEMU side is only the ChipIDEA UDC transport (registers, dQH/dTD DMA,
IRQs); everything a real host does lives here, over the bridge's line
protocol (all payloads lowercase hex, lines end with \n):

    -> SETUP <16 hex>   8-byte setup packet into dQH0, ENDPTSETUPSTAT, IRQ
    -> INEP <ep> <max>  IN token; the model serves the primed dTD chain
    -> OUTEP <ep> <hex> OUT token with data, into the primed dTD chain
    -> RESET / SPEED <hs|fs>
    <- INDATA <ep> <hex>  reply to INEP (empty = NAK or ZLP; the protocol
                          does not distinguish the two)
    <- SETUPACK / RESETDONE / CONNECT / DISCONNECT

Readings of the protocol where it is ambiguous (simplest choice taken):
  - SETUPACK is a gate: the data stage is not driven until the guest
    driver has taken the setup packet, so INEP never races the driver.
  - OUTEP has no reply; OUT data is fire-and-forget, chunked at 4 KiB per
    line (a dTD page) so the guest's primed chain always has room, and the
    next synchronisation point is the CSW / status stage.
  - An empty INDATA before the expected length is a NAK: retried until the
    deadline, then treated as a short read.  A non-empty short packet ends
    the data phase immediately, as on a real wire.
  - The status stage of an OUT or no-data control transfer is INEP 0 0;
    its empty INDATA is accepted as the ZLP without further retries (the
    model answers it either way, and a NAK here is indistinguishable from
    success - harmless for the transfers we run).
  - There is no STALL event, so a stalled bulk endpoint just looks like a
    permanent NAK; the BOT layer breaks out of that with the mass-storage
    reset + CLEAR_FEATURE(halt) recovery sequence when a CSW times out or
    carries status 2.

What this drives, in order: bus RESET + SPEED hs, GET_DESCRIPTOR(device,
9 then 64), SET_ADDRESS, string descriptors (langid, manufacturer,
product, serial - the Nest's usb-gadget init sets iSerialNumber to the
unit serial), GET_DESCRIPTOR(configuration, 9 then wTotalLength),
SET_CONFIGURATION(1).  The configuration tree is walked properly
(config -> interface association -> interfaces -> endpoints) and the MSC
function found by class 0x08/0x06/0x50 (fallback: any interface with one
bulk-IN and one bulk-OUT endpoint); vendor/product are parsed, never
assumed (stock: 0525:a4a5 'Linux File-Stor Gadget', Nest: 2464:0051).

On the two bulk endpoints this speaks SCSI Bulk-Only Transport: 31-byte
CBW (43425355), data phase chunked at 4 KiB, 13-byte CSW (53425355) with
tag/residue/status checks, and enough SCSI for a FAT volume: INQUIRY,
TEST UNIT READY, REQUEST SENSE, READ CAPACITY(10), READ(10), WRITE(10),
MODE SENSE(6) minimal, PREVENT ALLOW MEDIUM REMOVAL (no-op-ok).

The FAT layer mounts the LUN as a block device (sector cache with
write-back flush, coalescing runs into single WRITE(10)s) and supports
FAT12/16/32: label and cluster geometry from the boot sector, directory
listing and file reads through cluster chains, and 'put' - allocate free
clusters, build the chain, write every FAT copy, drop an 8.3 entry into
the target directory (extending it if it is a cluster directory), update
FSInfo and the boot-sector copy on FAT32.  No LFN: the device drops
short names fine.

After the payload swaps the gadget (rmmod g_mass_storage; modprobe
g_acm_ms), the host side of the CDC-ACM function is here too: the comm
interface (class 0x02/subclass 0x02) is found through its IAD or as the
interface after it, its data interface (class 0x0A) carries raw user
bytes on the bulk pair (abstract control sends no encapsulated-command
framing on the data endpoints, so we never issue those requests), and
SET_LINE_CODING (0x20) + SET_CONTROL_LINE_STATE (0x22, DTR|RTS asserted -
the gadget's ttyGS0 getty keys off DTR) bring the link up.  'acm'
bridges that pair to stdin/stdout (raw tty, Ctrl-] to leave);
'auto-exploit' stages a file over MSC, drops the link, waits out the
re-enumeration (the payload sleeps about a minute) and opens the shell.

CLI (see also --help):
    ui/usbhost.py --socket /tmp/nest-usb0.sock info
    ui/usbhost.py --socket tcp:127.0.0.1:7111 info
    ui/usbhost.py --socket PATH put localfile.bin --name PAYLOAD.BIN
    ui/usbhost.py --socket PATH get /LOGS/CURRENT --out current.log
    ui/usbhost.py --socket PATH shell      # raw ep0/bulk/MSC/FAT REPL
    ui/usbhost.py --socket PATH acm --run-cmd 'id; cat /proc/uptime'
    ui/usbhost.py --socket PATH auto-exploit payload.tar.gz \
        --run-cmd 'id; cat /proc/uptime'
    ui/usbhost.py --selftest               # no QEMU needed

--selftest exercises the descriptor parser (a g_mass_storage-shaped
tree, an IAD-grouped CDC blob, and a g_acm_ms composite with CDC-ACM +
MSC), the CBW/CSW builders, and the FAT layer against mkfs.vfat images
(FAT12 1440K, FAT16 16M, FAT32 64M) built in /tmp: put/get/readback,
fsck.vfat, and mtools as an independent parser (or a minimal in-code
mkfs when dosfstools is missing).
"""
import argparse
import os
import socket
import struct
import subprocess
import sys
import threading
import time
from queue import Empty, Queue

# ---- constants ------------------------------------------------------------

GET_DESCRIPTOR = 6
SET_ADDRESS = 5
SET_CONFIGURATION = 9
CLEAR_FEATURE = 1
ENDPOINT_HALT = 0
MSC_RESET = 0xFF

CBW_SIG = 0x43425355
CSW_SIG = 0x53425355
CBW_LEN = 31
CSW_LEN = 13
BULK_CHUNK = 4096          # bytes per INEP/OUTEP line: a dTD page

SCSI_TEST_UNIT_READY = 0x00
SCSI_REQUEST_SENSE = 0x03
SCSI_INQUIRY = 0x12
SCSI_MODE_SENSE6 = 0x1A
SCSI_PREVENT_ALLOW = 0x1E
SCSI_READ_CAPACITY = 0x25
SCSI_READ10 = 0x28
SCSI_WRITE10 = 0x2A

ATTR_RO = 0x01
ATTR_HIDDEN = 0x02
ATTR_SYSTEM = 0x04
ATTR_VOLUME_ID = 0x08
ATTR_DIR = 0x10
ATTR_ARCHIVE = 0x20
ATTR_LFN = 0x0F


class BridgeError(Exception):
    pass


class UsbError(Exception):
    pass


class MscError(Exception):
    pass


class FatError(Exception):
    pass


# ---- bridge transport -----------------------------------------------------

class Bridge:
    """The chardev socket: line framing and request/event correlation.

    A reader thread sorts INDATA replies into per-endpoint queues and
    everything else (SETUPACK, RESETDONE, CONNECT, DISCONNECT) into one
    event queue; one control transfer runs at a time, so SETUPACK is
    simply the next event after SETUP.
    """

    def __init__(self, spec, timeout=5.0, debug=False):
        self.spec = spec
        self.timeout = timeout
        self.debug = debug
        self.events = Queue()
        self.ep_data = {}
        self.closed = False
        self.connected = None            # last CONNECT/DISCONNECT, if seen
        self.lock = threading.Lock()
        self._connect()
        self.thread = threading.Thread(target=self._reader, daemon=True)
        self.thread.start()

    def _connect(self):
        if self.spec.startswith("tcp:"):
            _, host, port = self.spec.split(":", 2)
            self.sock = socket.create_connection((host, int(port)),
                                                 timeout=self.timeout)
        else:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(self.timeout)
            self.sock.connect(self.spec)
        self.sock.settimeout(None)

    def _reader(self):
        buf = b""
        try:
            while True:
                data = self.sock.recv(1 << 16)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    self._handle(line.decode("ascii", "replace").strip())
        except OSError:
            pass
        self.closed = True
        self.events.put(("CLOSED", None))

    def _handle(self, line):
        if self.debug:
            print(f"<- {line}", file=sys.stderr)
        parts = line.split()
        if not parts:
            return
        if parts[0] == "INDATA" and len(parts) >= 2:
            ep = int(parts[1])
            payload = bytes.fromhex(parts[2]) if len(parts) > 2 else b""
            self.ep_data.setdefault(ep, Queue()).put(payload)
        elif parts[0] in ("SETUPACK", "RESETDONE", "CONNECT", "DISCONNECT"):
            if parts[0] in ("CONNECT", "DISCONNECT"):
                self.connected = parts[0] == "CONNECT"
            self.events.put((parts[0], None))

    def _send(self, line):
        if self.closed:
            raise BridgeError("bridge connection closed")
        with self.lock:
            self.sock.sendall(line.encode() + b"\n")
        if self.debug:
            print(f"-> {line[:120]}", file=sys.stderr)

    def _wait_event(self, name, timeout):
        deadline = time.monotonic() + timeout
        seen = []
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                for e in seen:
                    self.events.put(e)      # keep for other waiters
                raise BridgeError(f"timed out waiting for {name}")
            try:
                ev = self.events.get(timeout=left)
            except Empty:
                continue
            if self.closed:
                raise BridgeError("bridge connection closed")
            if ev[0] == name:
                for e in seen:
                    self.events.put(e)
                return
            seen.append(ev)

    def setup(self, packet):
        if len(packet) != 8:
            raise BridgeError("setup packet must be 8 bytes")
        self._send(f"SETUP {packet.hex()}")
        self._wait_event("SETUPACK", self.timeout)

    def inep(self, ep, maxbytes):
        """One IN token; the reply payload (b"" = NAK or ZLP)."""
        self._send(f"INEP {ep} {int(maxbytes)}")
        q = self.ep_data.setdefault(ep, Queue())
        try:
            data = q.get(timeout=self.timeout)
        except Empty:
            raise BridgeError(f"timed out waiting for INDATA ep {ep}")
        if self.debug and data:
            print(f"   ep{ep} data {len(data)} bytes", file=sys.stderr)
        return data

    def outep(self, ep, data):
        self._send(f"OUTEP {ep} {data.hex()}")

    def reset(self):
        self._send("RESET")
        self._wait_event("RESETDONE", self.timeout)

    def speed(self, s):
        if s not in ("hs", "fs"):
            raise BridgeError(f"unknown speed {s}")
        self._send(f"SPEED {s}")

    def wait_connect(self, timeout=None):
        """Block until the gadget pulls the virtual cable up."""
        if self.connected:
            return
        deadline = time.monotonic() + (timeout or self.timeout)
        while self.connected is None or not self.connected:
            left = deadline - time.monotonic()
            if left <= 0 or self.closed:
                raise BridgeError("gadget did not connect")
            try:
                ev = self.events.get(timeout=left)
            except Empty:
                continue
            if ev[0] == "CLOSED":
                raise BridgeError("bridge connection closed")


# ---- descriptors ----------------------------------------------------------

class DeviceDescriptor:
    def __init__(self, raw):
        if len(raw) < 18:
            raise UsbError(f"short device descriptor ({len(raw)} bytes)")
        self.raw = raw
        (self.bcd_usb, self.dev_class, self.dev_subclass, self.dev_protocol,
         self.ep0_maxpacket, self.vendor, self.product, self.bcd_device,
         self.i_manufacturer, self.i_product, self.i_serial,
         self.num_configs) = struct.unpack_from("<HBBBBHHHBBBB", raw, 2)

    def __str__(self):
        return (f"USB {self.bcd_usb >> 8:x}.{self.bcd_usb & 0xff:x} "
                f"idVendor={self.vendor:04x} idProduct={self.product:04x} "
                f"bcdDevice={self.bcd_device:04x} ep0 maxpacket="
                f"{self.ep0_maxpacket}")


class Endpoint:
    def __init__(self, raw):
        (self.addr, self.attrs, self.maxpacket,
         self.interval) = struct.unpack_from("<BBHB", raw, 2)

    @property
    def number(self):
        return self.addr & 0x0F

    @property
    def is_in(self):
        return bool(self.addr & 0x80)

    @property
    def type(self):
        return ("control", "iso", "bulk", "int")[self.attrs & 3]

    def __str__(self):
        return (f"ep {self.addr:#04x} {self.type} "
                f"{'in' if self.is_in else 'out'} maxpacket "
                f"{self.maxpacket}")


class Interface:
    def __init__(self, number, alt, iface_class, subclass, protocol,
                 string_idx, iad=None):
        self.number = number
        self.alt = alt
        self.iface_class = iface_class
        self.subclass = subclass
        self.protocol = protocol
        self.string_idx = string_idx
        self.iad = iad                # (function class, subclass, protocol)
        self.endpoints = []

    def __str__(self):
        s = (f"interface {self.number} alt {self.alt} class "
             f"{self.iface_class:02x}/{self.subclass:02x}/{self.protocol:02x}"
             f" ({len(self.endpoints)} endpoints)")
        if self.iad:
            s += f" via IAD {self.iad[0]:02x}/{self.iad[1]:02x}/{self.iad[2]:02x}"
        return s


class Config:
    def __init__(self, raw):
        if len(raw) < 9 or raw[1] != 2:
            raise UsbError("not a configuration descriptor")
        self.raw = raw
        (self.total_length, self.iface_count, self.cfg_value,
         self.i_cfg, self.attributes,
         self.max_power) = struct.unpack_from("<HBBBBB", raw, 2)
        self.interfaces = self._parse(raw)

    @staticmethod
    def _parse(raw):
        # Walk the descriptor tree: configuration -> [IAD grouping]
        # -> interfaces -> endpoints.  An IAD (type 0x0b) covers the
        # interfaces from bFirstInterface for bInterfaceCount of them.
        iads = []
        off = raw[0]
        while off + 2 <= len(raw):
            dlen, dtype = raw[off], raw[off + 1]
            if dlen < 2 or off + dlen > len(raw):
                break
            if dtype == 0x0B and dlen >= 8:
                iads.append((raw[off + 2], raw[off + 3],
                             (raw[off + 4], raw[off + 5], raw[off + 6])))
            off += dlen

        def iad_of(num):
            for first, count, t in iads:
                if first <= num < first + count:
                    return t
            return None

        interfaces = []
        cur = None
        off = raw[0]
        while off + 2 <= len(raw):
            dlen, dtype = raw[off], raw[off + 1]
            if dlen < 2 or off + dlen > len(raw):
                break
            if dtype == 0x04 and dlen >= 9:
                cur = Interface(raw[off + 2], raw[off + 3], raw[off + 5],
                                raw[off + 6], raw[off + 7], raw[off + 8],
                                iad_of(raw[off + 2]))
                interfaces.append(cur)
            elif dtype == 0x05 and dlen >= 7 and cur is not None:
                cur.endpoints.append(Endpoint(raw[off:off + dlen]))
            off += dlen
        return interfaces

    def find_msc(self):
        """The mass-storage function: an 0x08/0x06/0x50 interface with one
        bulk-IN and one bulk-OUT endpoint; failing that, any interface
        with exactly one of each."""
        for iface in self.interfaces:
            eps = [e for e in iface.endpoints if e.type == "bulk"]
            ins = [e for e in eps if e.is_in]
            outs = [e for e in eps if not e.is_in]
            if (iface.iface_class, iface.subclass,
                    iface.protocol) == (0x08, 0x06, 0x50) \
                    and len(ins) == 1 and len(outs) == 1:
                return iface, ins[0], outs[0]
        for iface in self.interfaces:
            eps = [e for e in iface.endpoints if e.type == "bulk"]
            ins = [e for e in eps if e.is_in]
            outs = [e for e in eps if not e.is_in]
            if len(ins) == 1 and len(outs) == 1:
                return iface, ins[0], outs[0]
        return None

    def find_acm(self):
        """The CDC-ACM function of a composite gadget (g_acm_ms): the
        communications interface (class 0x02, subclass 0x02 ACM) plus its
        data interface (class 0x0A) and the data bulk pair.  Preferred
        pairing is a shared IAD, then the interface right after comm.
        Returns (comm_iface, data_iface, ep_in, ep_out) or None."""
        def bulk_pair(iface):
            eps = [e for e in iface.endpoints if e.type == "bulk"]
            ins = [e for e in eps if e.is_in]
            outs = [e for e in eps if not e.is_in]
            if len(ins) == 1 and len(outs) == 1:
                return ins[0], outs[0]
            return None

        data_ifs = [i for i in self.interfaces
                    if i.iface_class == 0x0A and bulk_pair(i)]
        for comm in (i for i in self.interfaces
                     if i.iface_class == 0x02 and i.subclass == 0x02):
            cands = [i for i in data_ifs if comm.iad and i.iad == comm.iad]
            if not cands:
                cands = [i for i in data_ifs if i.number == comm.number + 1]
            if not cands:
                cands = data_ifs
            if cands:
                ep_in, ep_out = bulk_pair(cands[0])
                return comm, cands[0], ep_in, ep_out
        return None


# ---- control transfers + enumeration --------------------------------------

class UsbHost:
    """ep0: the SETUP / data / status state machine, then a real host's
    enumeration sequence."""

    def __init__(self, bridge, timeout=5.0):
        self.bridge = bridge
        self.timeout = timeout
        self.ep0_maxpacket = 64
        self.device = None
        self.config = None
        self.address = 0
        self.strings = {}

    def control(self, rtype, request, value=0, index=0, length=0, data=None):
        """One control transfer per the USB spec.  IN requests return the
        data stage; the status stage runs in the opposite direction."""
        self.bridge.setup(struct.pack("<BBHHH", rtype & 0xFF, request & 0xFF,
                                      value, index, length))
        if rtype & 0x80:                       # device -> host
            out = self._ep0_read(length)
            self.bridge.outep(0, b"")          # status: OUT ZLP
            return out
        if data:                               # host -> device
            for off in range(0, len(data), self.ep0_maxpacket):
                self.bridge.outep(0, data[off:off + self.ep0_maxpacket])
        self.bridge.inep(0, 0)                 # status: IN ZLP
        return None

    def _ep0_read(self, length):
        out = b""
        deadline = time.monotonic() + self.timeout
        while len(out) < length:
            want = min(self.ep0_maxpacket, length - len(out))
            chunk = self._inep_until(0, want, deadline)
            if not chunk:
                break                          # NAK to the end: short read
            out += chunk
            if len(chunk) < want:
                break                          # short packet ends the stage
        return out[:length]

    def _inep_until(self, ep, maxbytes, deadline):
        """INEP, retrying empty (NAK) answers until the deadline."""
        while True:
            data = self.bridge.inep(ep, maxbytes)
            if data or time.monotonic() >= deadline:
                return data
            time.sleep(0.005)

    def string_descriptor(self, index, langid=0):
        raw = self.control(0x80, GET_DESCRIPTOR, 0x0300 | index, langid, 255)
        if len(raw) < 2 or raw[1] != 3:
            return None
        try:
            return raw[2:].decode("utf-16-le").rstrip("\0")
        except UnicodeDecodeError:
            return raw[2:].decode("utf-16-le", "replace").rstrip("\0")

    def enumerate(self, address=5):
        self.bridge.reset()
        self.bridge.speed("hs")
        self.address = 0
        # a real host reads 9 bytes first to learn ep0's maxpacket
        short = self.control(0x80, GET_DESCRIPTOR, 0x0100, 0, 9)
        if len(short) < 9:
            raise UsbError(f"device descriptor read failed ({short.hex()})")
        self.ep0_maxpacket = short[7] or 64
        full = self.control(0x80, GET_DESCRIPTOR, 0x0100, 0, 64)
        self.device = DeviceDescriptor(full)
        self.control(0x00, SET_ADDRESS, address)
        self.address = address
        # strings: langid first, then every index the device names
        lang_raw = self.control(0x80, GET_DESCRIPTOR, 0x0300, 0, 4)
        langid = struct.unpack("<H", lang_raw[2:4])[0] if len(lang_raw) >= 4 \
            else 0x0409
        for idx in (self.device.i_manufacturer, self.device.i_product,
                    self.device.i_serial):
            if idx:
                self.strings[idx] = self.string_descriptor(idx, langid)
        cfg = self.control(0x80, GET_DESCRIPTOR, 0x0200, 0, 9)
        if len(cfg) < 9:
            raise UsbError("configuration descriptor read failed")
        total = struct.unpack_from("<H", cfg, 2)[0]
        self.config = Config(self.control(0x80, GET_DESCRIPTOR, 0x0200, 0,
                                          total))
        self.control(0x00, SET_CONFIGURATION, 1)
        return self.device

    def describe(self):
        d = self.device
        s = str(d)
        for label, idx in (("manufacturer", d.i_manufacturer),
                           ("product", d.i_product),
                           ("serial", d.i_serial)):
            if self.strings.get(idx):
                s += f"\n  {label}: {self.strings[idx]}"
        s += f"\n  configuration {self.config.cfg_value}: " \
             f"{len(self.config.interfaces)} interface(s)"
        for iface in self.config.interfaces:
            s += f"\n    {iface}"
            for ep in iface.endpoints:
                s += f"\n      {ep}"
        return s


# ---- SCSI bulk-only transport ---------------------------------------------

class Msc:
    def __init__(self, host, iface, ep_in, ep_out, lun=0, timeout=10.0):
        self.host = host
        self.iface = iface.number
        self.ep_in = ep_in.addr
        self.ep_out = ep_out.addr
        self.lun = lun
        self.timeout = timeout
        self.tag = 0

    @staticmethod
    def build_cbw(tag, dlen, dir_in, lun, cdb):
        if not 1 <= len(cdb) <= 16:
            raise MscError(f"CDB length {len(cdb)} out of range")
        cbw = struct.pack("<IIIBBB", CBW_SIG, tag, dlen,
                          0x80 if dir_in else 0, lun, len(cdb))
        return cbw + cdb.ljust(16, b"\0")

    @staticmethod
    def parse_csw(raw, tag):
        if len(raw) != CSW_LEN:
            raise MscError(f"CSW is {len(raw)} bytes, not {CSW_LEN}")
        sig, rtag, residue, status = struct.unpack("<IIIB", raw)
        if sig != CSW_SIG:
            raise MscError(f"bad CSW signature {sig:#010x}")
        if rtag != tag:
            raise MscError(f"CSW tag {rtag:#x} does not match CBW {tag:#x}")
        return residue, status

    def _command(self, cdb, data=b"", in_len=0):
        """CBW, data phase, CSW.  Returns the IN data; raises on CSW
        status 1 (with sense) and 2 (after reset recovery)."""
        self.tag = (self.tag + 1) & 0xFFFFFFFF
        dir_in = in_len > 0
        dlen = in_len if dir_in else len(data)
        self.host.bridge.outep(self.ep_out,
                               self.build_cbw(self.tag, dlen, dir_in,
                                              self.lun, cdb))
        got = b""
        if dir_in:
            deadline = time.monotonic() + self.timeout
            while len(got) < dlen:
                want = min(BULK_CHUNK, dlen - len(got))
                chunk = self.host._inep_until(self.ep_in, want, deadline)
                if not chunk:
                    break
                got += chunk
                if len(chunk) < want:
                    break
        else:
            for off in range(0, len(data), BULK_CHUNK):
                self.host.bridge.outep(self.ep_out,
                                       data[off:off + BULK_CHUNK])
        residue, status = self.parse_csw(self._read_csw(), self.tag)
        if status == 1:
            raise MscError(f"{cdb[0]:#04x} failed: "
                           f"{self._sense_text()} (residue {residue})")
        if status == 2:
            self.reset_recovery()
            raise MscError(f"{cdb[0]:#04x} phase error, ran BOT reset "
                           f"recovery")
        if residue:
            raise MscError(f"{cdb[0]:#04x} residue {residue} of {dlen}")
        return got

    def _read_csw(self):
        deadline = time.monotonic() + self.timeout
        while True:
            csw = self.host._inep_until(self.ep_in, CSW_LEN, deadline)
            if len(csw) == CSW_LEN:
                return csw
            if not csw or time.monotonic() >= deadline:
                raise MscError(f"no CSW ({len(csw)} bytes)")

    def reset_recovery(self):
        """Bulk-only mass storage reset, then CLEAR_FEATURE(halt) on both
        bulk endpoints - what a host does on a stalled BOT transfer."""
        self.host.control(0x21, MSC_RESET, 0, self.iface, 0)
        self.host.control(0x02, CLEAR_FEATURE, ENDPOINT_HALT,
                          self.ep_in | 0x80, 0)
        self.host.control(0x02, CLEAR_FEATURE, ENDPOINT_HALT,
                          self.ep_out, 0)

    def _sense_text(self):
        key, asc, ascq = self.request_sense()
        return f"sense key {key} asc {asc:#04x} ascq {ascq:#04x}"

    # -- SCSI --
    def request_sense(self):
        raw = self._command(bytes([SCSI_REQUEST_SENSE, 0, 0, 0, 18, 0]),
                            in_len=18)
        return raw[2] & 0x0F, raw[12], raw[13]

    def test_unit_ready(self, retries=20, interval=1.0):
        for _ in range(retries):
            try:
                self._command(bytes(6))
                return
            except MscError:
                time.sleep(interval)
        self._command(bytes(6))

    def inquiry(self):
        raw = self._command(bytes([SCSI_INQUIRY, 0, 0, 0, 36, 0]), in_len=36)
        if len(raw) < 36:
            raise MscError(f"short INQUIRY ({len(raw)} bytes)")
        periph = raw[0]
        return dict(peripheral=periph & 0x1F,
                    removable=bool(raw[1] & 0x80),
                    version=raw[2],
                    vendor=raw[8:16].decode("ascii", "replace").strip(),
                    product=raw[16:32].decode("ascii", "replace").strip(),
                    rev=raw[32:36].decode("ascii", "replace").strip(),
                    raw=raw)

    def read_capacity(self):
        raw = self._command(bytes([SCSI_READ_CAPACITY]) + bytes(9), in_len=8)
        blocks, blocksize = struct.unpack(">II", raw)
        return blocks + 1, blocksize       # returns the last LBA

    def mode_sense6(self):
        return self._command(bytes([SCSI_MODE_SENSE6, 0, 0x3F, 0, 4, 0]),
                             in_len=4)

    def prevent_allow(self, prevent=False):
        cdb = bytes([SCSI_PREVENT_ALLOW, 0, 0, 0, 1 if prevent else 0, 0])
        self._command(cdb)

    def read10(self, lba, count, blocksize):
        cdb = bytes([SCSI_READ10, 0]) + lba.to_bytes(4, "big") + \
            bytes([0]) + count.to_bytes(2, "big") + bytes(1)
        return self._command(cdb, in_len=count * blocksize)

    def write10(self, lba, data, blocksize):
        cdb = bytes([SCSI_WRITE10, 0]) + lba.to_bytes(4, "big") + \
            bytes([0]) + (len(data) // blocksize).to_bytes(2, "big") + \
            bytes(1)
        self._command(cdb, data=data)


# ---- CDC-ACM --------------------------------------------------------------

ACM_SET_LINE_CODING = 0x20
ACM_SET_CONTROL_LINE_STATE = 0x22
ACM_DTR = 0x01
ACM_RTS = 0x02


class Acm:
    """The CDC-ACM function's control requests and data pipes.  The data
    interface of the 'abstract control' subclass carries plain user bytes
    - no encapsulated-command framing on the bulk endpoints - so the
    terminal just forwards stdin/stdout to the two endpoints.  We never
    issue SEND_ENCAPSULATED_COMMAND (0x00) / RESPONSE_AVAILABLE (0x01);
    a gadget that stalls them is simply never asked."""

    def __init__(self, host, comm, ep_in, ep_out, timeout=5.0):
        self.host = host
        self.comm = comm.number
        self.ep_in = ep_in.addr
        self.ep_out = ep_out.addr
        self.timeout = timeout

    def set_line_coding(self, rate=115200, stopbits=0, parity=0, databits=8):
        self.host.control(0x21, ACM_SET_LINE_CODING, 0, self.comm, 7,
                          data=struct.pack("<IBBB", rate, stopbits, parity,
                                           databits))

    def set_control_line_state(self, dtr=True, rts=True):
        bits = (ACM_DTR if dtr else 0) | (ACM_RTS if rts else 0)
        self.host.control(0x21, ACM_SET_CONTROL_LINE_STATE, bits, self.comm,
                          0)

    def write(self, data):
        for off in range(0, len(data), BULK_CHUNK):
            self.host.bridge.outep(self.ep_out, data[off:off + BULK_CHUNK])

    def read(self, maxbytes=BULK_CHUNK):
        """One poll; b"" means nothing to read right now."""
        deadline = time.monotonic() + 0.1
        return self.host._inep_until(self.ep_in, maxbytes, deadline)


# ---- block device over MSC ------------------------------------------------

class ScsiDisk:
    """A LUN as a block device: read_sectors(lba, n) -> bytes,
    write_sectors(lba, data)."""

    MAX_BLOCKS = 64                        # 32 KiB per command, HS-safe

    def __init__(self, msc):
        self.msc = msc
        self.blocks, self.blocksize = msc.read_capacity()

    def read_sectors(self, lba, count):
        if lba + count > self.blocks:
            raise MscError(f"read past end: LBA {lba}+{count} of "
                           f"{self.blocks}")
        out = b""
        while count:
            n = min(self.MAX_BLOCKS, count)
            out += self.msc.read10(lba, n, self.blocksize)
            lba, count = lba + n, count - n
        return out

    def write_sectors(self, lba, data):
        bs = self.blocksize
        if len(data) % bs:
            data = data.ljust((len(data) + bs - 1) // bs * bs, b"\0")
        if lba + len(data) // bs > self.blocks:
            raise MscError(f"write past end: LBA {lba}+{len(data) // bs} of "
                           f"{self.blocks}")
        for off in range(0, len(data), self.MAX_BLOCKS * bs):
            self.msc.write10(lba + off // bs, data[off:off + self.MAX_BLOCKS
                                                   * bs], bs)


# ---- FAT12/16/32 ----------------------------------------------------------

class FatVolume:
    """A FAT volume on a block device (read_sectors/write_sectors), with a
    sector cache whose writes flush back coalesced into runs."""

    def __init__(self, block):
        self.block = block
        self.cache = {}                    # lba -> bytearray
        self.dirty = set()
        self._mount()

    # -- sector cache --
    def _sec(self, lba):
        s = self.cache.get(lba)
        if s is None:
            s = bytearray(self.block.read_sectors(lba, 1))
            self.cache[lba] = s
        return s

    def flush(self):
        for lba in sorted(self.dirty):
            run = 1
            while lba + run in self.dirty:
                run += 1
            data = b"".join(bytes(self._sec(lba + i)) for i in range(run))
            self.block.write_sectors(lba, data)
            for i in range(run):
                self.dirty.discard(lba + i)

    def _put(self, lba, off, data):
        s = self._sec(lba)
        s[off:off + len(data)] = data
        self.dirty.add(lba)

    # -- mount --
    def _mount(self):
        bs = self.block.read_sectors(0, 1)
        if len(bs) < 512 or bs[510:512] != b"\x55\xaa":
            raise FatError("no FAT boot sector (bad 55 aa signature)")
        (self.bps, self.spc, self.reserved, self.nfats, self.root_entries,
         self.total16, _media, self.fat_size16) = struct.unpack_from(
            "<HBHBHHBH", bs, 11)
        self.total32 = struct.unpack_from("<I", bs, 32)[0]
        total = self.total16 or self.total32
        if not self.bps or not self.spc or not total:
            raise FatError("bad BPB geometry")
        self.fat_size32 = struct.unpack_from("<I", bs, 36)[0]
        self.fat_sectors = self.fat_size16 or self.fat_size32
        self.root_cluster = struct.unpack_from("<I", bs, 40)[0] or 2
        self.fsinfo_sector = struct.unpack_from("<H", bs, 48)[0]
        self.backup_boot = struct.unpack_from("<H", bs, 50)[0]
        if not self.fat_sectors:
            raise FatError("bad FAT size")
        self.root_sectors = (self.root_entries * 32 + self.bps - 1) // self.bps
        pre_data = self.reserved + self.nfats * self.fat_sectors
        self.fat_start = self.reserved
        self.root_start = pre_data
        self.data_start = pre_data + self.root_sectors
        clusters = (total - self.data_start) // self.spc
        self.max_cluster = clusters + 1          # last valid cluster number
        if clusters < 4085:
            self.fat_type = 12
            self.eoc = 0xFF8
        elif clusters < 65525:
            self.fat_type = 16
            self.eoc = 0xFFF8
        else:
            self.fat_type = 32
            self.eoc = 0x0FFFFFF8
        self.cluster_bytes = self.spc * self.bps
        # label: boot-sector copy first, else the volume-label dir entry
        self.label = None
        if len(bs) >= 54 and bs[38] in (0x28, 0x29):
            self.label = bs[43:54].decode("ascii", "replace").strip()
        elif len(bs) >= 82 and bs[66] in (0x28, 0x29):
            self.label = bs[71:82].decode("ascii", "replace").strip()
        if not self.label:
            for e in self.iter_dir(self._root_dir()):
                if e["attr"] & ATTR_VOLUME_ID and not e["attr"] & ATTR_LFN \
                        and not e["attr"] & ATTR_DIR:
                    self.label = e["name"]
                    break
        self.free_count = sum(1 for c in range(2, self.max_cluster + 1)
                              if self.fat_get(c) == 0)
        self.next_free = 2

    def _root_dir(self):
        """The root directory: None = the fixed FAT12/16 area, else the
        first cluster of the FAT32 chain."""
        if self.fat_type == 32:
            return self.root_cluster
        return None

    def __str__(self):
        total = (self.total16 or self.total32) * self.bps
        return (f"FAT{self.fat_type} '{self.label or '?'}' "
                f"{total // (1 << 20)} MiB, {self.bps} B sectors x "
                f"{self.spc} = {self.cluster_bytes} B clusters, "
                f"{self.max_cluster - 1} clusters, {self.free_count} free")

    # -- FAT entries (all copies kept in step) --
    def _fat_offset(self, cl):
        return {12: cl + cl // 2, 16: cl * 2, 32: cl * 4}[self.fat_type]

    def fat_get(self, cl):
        off = self._fat_offset(cl)
        raw = self._fat_read(off)
        if self.fat_type == 12:
            v = int.from_bytes(raw[:2], "little")
            return (v & 0xFFF) if cl % 2 == 0 else (v >> 4)
        v = int.from_bytes(raw[:4], "little")
        return v & 0xFFFF if self.fat_type == 16 else v & 0x0FFFFFFF

    def _fat_read(self, off):
        lba, o = divmod(off, self.bps)
        s = self._sec(self.fat_start + lba)
        if o + 4 <= self.bps:
            return bytes(s[o:o + 4])
        return bytes(s[o:]) + bytes(self._sec(self.fat_start + lba + 1)
                                    [:o + 4 - self.bps])

    def fat_set(self, cl, val):
        off = self._fat_offset(cl)
        cur = bytearray(self._fat_read(off))
        if self.fat_type == 12:
            v = int.from_bytes(cur[:2], "little")
            v = (v & 0xF000) | (val & 0xFFF) if cl % 2 == 0 \
                else (v & 0xF) | ((val & 0xFFF) << 4)
            cur[0:2] = v.to_bytes(2, "little")
            nb = 2
        elif self.fat_type == 16:
            cur[0:2] = (val & 0xFFFF).to_bytes(2, "little")
            nb = 2
        else:
            cur[0:4] = (val & 0x0FFFFFFF).to_bytes(4, "little")
            nb = 4
        for i in range(self.nfats):
            base = self.fat_start + i * self.fat_sectors
            lba, o = divmod(off, self.bps)
            self._put(base + lba, o, cur[:min(nb, self.bps - o)])
            if o + nb > self.bps:
                self._put(base + lba + 1, 0, cur[self.bps - o:nb])

    # -- clusters --
    def cluster_sector(self, cl):
        return self.data_start + (cl - 2) * self.spc

    def iter_chain(self, first):
        cl = first
        seen = set()
        while 2 <= cl <= self.max_cluster:
            if cl in seen:
                raise FatError(f"FAT loop at cluster {cl}")
            seen.add(cl)
            yield cl
            cl = self.fat_get(cl)
            if cl >= self.eoc:
                return
        if cl:
            raise FatError(f"bad FAT link {cl:#x}")

    def read_chain(self, first, size):
        out = b""
        for cl in self.iter_chain(first):
            if len(out) >= size:
                break
            out += bytes(self.block.read_sectors(self.cluster_sector(cl),
                                                 self.spc))
        return out[:size]

    def alloc_chain(self, nbytes):
        """Free clusters from the scan, linked and terminated; writes FAT
        entries for every copy.  Returns the first cluster (0 for no
        bytes) and the count."""
        need = (nbytes + self.cluster_bytes - 1) // self.cluster_bytes
        if not need:
            return 0, 0
        free = []
        cl = self.next_free
        while len(free) < need and cl <= self.max_cluster:
            if self.fat_get(cl) == 0:
                free.append(cl)
            cl += 1
        if len(free) < need:
            cl = 2
            while len(free) < need and cl <= self.max_cluster:
                if self.fat_get(cl) == 0 and cl not in free:
                    free.append(cl)
                cl += 1
        if len(free) < need:
            raise FatError(f"volume full: need {need} clusters, "
                           f"{self.free_count} free")
        for i, c in enumerate(free):
            self.fat_set(c, free[i + 1] if i + 1 < len(free) else self.eoc)
        self.free_count -= len(free)
        self.next_free = min(free[-1] + 1, self.max_cluster)
        return free[0], len(free)

    def free_chain(self, first):
        n = 0
        for cl in self.iter_chain(first):
            self.fat_set(cl, 0)
            n += 1
        self.free_count += n
        return n

    def zero_cluster(self, cl):
        for i in range(self.spc):
            self._put(self.cluster_sector(cl) + i, 0, b"\0" * self.bps)

    # -- directories --
    def _load_dir(self, cl):
        """Directory bytes and the writeback handle (None = the fixed
        FAT12/16 root area, else the chain's first cluster)."""
        if cl is None:
            return self.block.read_sectors(self.root_start,
                                           self.root_sectors), None
        out = b""
        for c in self.iter_chain(cl):
            out += self.block.read_sectors(self.cluster_sector(c), self.spc)
        return out, cl

    def _store_dir(self, buf, first):
        """Write directory bytes back through the sector cache; a chain
        directory is written cluster by cluster (its clusters are not
        necessarily contiguous)."""
        if first is None:
            for off in range(0, len(buf), self.bps):
                self._put(self.root_start + off // self.bps, 0,
                          buf[off:off + self.bps])
            return
        off = 0
        for cl in self.iter_chain(first):
            for i in range(self.spc):
                self._put(self.cluster_sector(cl) + i, 0,
                          buf[off:off + self.bps])
                off += self.bps

    @staticmethod
    def _name83(raw):
        base = raw[0:8].decode("ascii", "replace").rstrip()
        ext = raw[8:11].decode("ascii", "replace").rstrip()
        return f"{base}.{ext}" if ext else base

    def iter_dir(self, cl):
        """Entries of a directory: 8.3 name, attribute, first cluster,
        size, and the slot index in the directory bytes."""
        buf, _ = self._load_dir(cl)
        out = []
        for off in range(0, len(buf) - 31, 32):
            raw = buf[off:off + 32]
            if raw[0] == 0x00:
                break
            if raw[0] == 0xE5:
                continue
            attr = raw[11]
            if attr & ATTR_LFN == ATTR_LFN:
                continue
            cluster = struct.unpack_from("<H", raw, 26)[0]
            if self.fat_type == 32:
                cluster |= struct.unpack_from("<H", raw, 20)[0] << 16
            out.append(dict(name=self._name83(raw[0:11]), attr=attr,
                            cluster=cluster,
                            size=struct.unpack_from("<I", raw, 28)[0],
                            slot=off // 32, raw=raw))
        return out

    @staticmethod
    def _to83(name):
        base, dot, ext = name.upper().rpartition(".")
        if not dot:
            base, ext = name.upper(), ""
        valid = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
                    "_$%~'-@{}()!#^&")
        def clean(s):
            return "".join(c if c in valid else "_" for c in s)
        base, ext = clean(base), clean(ext)
        if not (1 <= len(base) <= 8 and len(ext) <= 3):
            raise FatError(f"{name!r} does not fit an 8.3 name "
                           f"(pass --name NAME.EXT, up to 8+3)")
        return (base.ljust(8) + ext.ljust(3)).encode("ascii")

    @staticmethod
    def _dos_time(t):
        date = ((t.tm_year - 1980) << 9) | (t.tm_mon << 5) | t.tm_mday
        tm = (t.tm_hour << 11) | (t.tm_min << 5) | (t.tm_sec // 2)
        return date, tm

    def resolve(self, path):
        """'/A/B' -> the directory it names; '' or '/' -> the root."""
        cl = self._root_dir()
        for part in [p for p in path.split("/") if p]:
            hit = [e for e in self.iter_dir(cl)
                   if e["name"].upper() == part.upper()]
            if not hit:
                raise FatError(f"no {part!r} in directory")
            if not hit[0]["attr"] & ATTR_DIR:
                raise FatError(f"{part!r} is not a directory")
            cl = hit[0]["cluster"]
        return cl

    def read_file(self, path):
        parent, _, name = path.rstrip("/").rpartition("/")
        for e in self.iter_dir(self.resolve(parent)):
            if e["name"].upper() == name.upper():
                if e["attr"] & ATTR_DIR:
                    raise FatError(f"{path} is a directory")
                return self.read_chain(e["cluster"], e["size"])
        raise FatError(f"no such file: {path}")

    def put_file(self, data, name, dest="/"):
        """Insert (or overwrite) a file: allocate a cluster chain, write
        the data, keep every FAT copy in step, drop an 8.3 entry into the
        directory (extending a cluster directory when full), and update
        FSInfo + the boot-sector copy on FAT32."""
        dest = dest.rstrip("/")
        dir_cl = self.resolve(dest)
        name83 = self._to83(name)
        buf, dir_first = self._load_dir(dir_cl)
        entries = self.iter_dir(dir_cl)
        slot = None
        for e in entries:
            if e["raw"][0:11] == name83:
                if e["attr"] & ATTR_DIR:
                    raise FatError(f"{name}: is a directory")
                if e["cluster"]:
                    self.free_chain(e["cluster"])
                slot = e["slot"]
                break
        if slot is None:
            for i in range(len(buf) // 32):
                if buf[i * 32] in (0x00, 0xE5):
                    slot = i
                    break
        if slot is None:
            if dir_first is None:
                raise FatError("root directory is full (fixed FAT12/16 "
                               "area; delete something first)")
            # extend the directory: link a zeroed cluster to the chain end
            chain = list(self.iter_chain(dir_first))
            new_cl, _ = self.alloc_chain(self.cluster_bytes)
            self.fat_set(chain[-1], new_cl)
            self.zero_cluster(new_cl)
            buf += b"\0" * self.cluster_bytes
            slot = (len(buf) - self.cluster_bytes) // 32
        first, _ = self.alloc_chain(len(data))
        off = 0
        for cl in self.iter_chain(first) if first else []:
            chunk = data[off:off + self.cluster_bytes]
            sector = self.cluster_sector(cl)
            for i in range(self.spc):
                piece = chunk[i * self.bps:(i + 1) * self.bps]
                if piece:
                    self._put(sector + i, 0, piece)
            off += self.cluster_bytes
        t = time.localtime()
        date, tm = self._dos_time(t)
        e = bytearray(32)
        e[0:11] = name83
        e[11] = ATTR_ARCHIVE
        e[13] = 0                       # creation time centiseconds
        e[14:16] = struct.pack("<H", tm)
        e[16:18] = struct.pack("<H", date)
        e[18:20] = struct.pack("<H", date)   # access date
        e[20:22] = struct.pack("<H", first >> 16)
        e[22:24] = struct.pack("<H", tm)
        e[24:26] = struct.pack("<H", date)
        e[26:28] = struct.pack("<H", first & 0xFFFF)
        e[28:32] = struct.pack("<I", len(data))
        buf = bytearray(buf)
        buf[slot * 32:(slot + 1) * 32] = e
        self._store_dir(bytes(buf), dir_first)
        self._update_fsinfo()
        self.flush()

    def _update_fsinfo(self):
        if self.fat_type != 32:
            return
        free = self.free_count & 0xFFFFFFFF
        nxt = self.next_free & 0xFFFFFFFF
        for lba in {self.fsinfo_sector} - {0}:
            self._put(lba, 488, struct.pack("<II", free, nxt))
        self._put(0, 488, struct.pack("<II", free, nxt))

    def list_dir(self, path="/"):
        return self.iter_dir(self.resolve(path))


# ---- selftest -------------------------------------------------------------

class MemDisk:
    """A block device over an in-memory image (the selftest's LUN)."""

    def __init__(self, data):
        self.data = bytearray(data)
        self.blocksize = 512

    def read_sectors(self, lba, count):
        return bytes(self.data[lba * 512:(lba + count) * 512])

    def write_sectors(self, lba, data):
        self.data[lba * 512:lba * 512 + len(data)] = data


def build_fat16_image(size, label="SELFTEST"):
    """A minimal FAT16 image for hosts without dosfstools: BPB, two
    nearly-empty FATs, a zeroed root directory."""
    bps, spc, reserved, nfats, root_entries = 512, 4, 1, 2, 512
    total = size // bps
    root_secs = root_entries * 32 // bps
    clusters = (total - reserved - root_secs) // spc - nfats
    fat_secs = max(1, (clusters * 2 + nfats + bps - 1) // bps)
    img = bytearray(total * bps)
    bs = bytearray(bps)
    bs[0:3] = b"\xeb\x3c\x90"
    struct.pack_into("<HBHBHHBH", bs, 11, bps, spc, reserved, nfats,
                     root_entries, total if total < 0x10000 else 0, 0xF8,
                     fat_secs)
    struct.pack_into("<HBH", bs, 24, 32, 64, 0)      # track/heads/hidden
    bs[38] = 0x29                                   # ext boot signature
    bs[43:54] = label.ljust(11).encode("ascii")
    bs[510:512] = b"\x55\xaa"
    img[:bps] = bs
    for i in range(nfats):
        off = (reserved + i * fat_secs) * bps
        img[off:off + 4] = b"\xf8\xff\xff\xff"
    return img


def make_image(path, fmt, size, label):
    if fmt == "code":
        return build_fat16_image(size, label)
    # mkfs.vfat -C takes the size in 1 KiB blocks
    subprocess.run(["mkfs.vfat", "-C", "-n", label]
                   + (["-F", str(fmt)] if fmt in (12, 32) else [])
                   + [path, str(size // 1024)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with open(path, "rb") as f:
        return f.read()


def check(cond, label, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}"
          + (f" ({detail})" if detail and not cond else ""))
    return cond


def test_descriptors():
    print("descriptor parser")
    ok = True
    # g_mass_storage shape: config -> MSC interface -> 2 bulk endpoints
    cfg = (bytes([9, 2, 32, 0, 1, 1, 0, 0x80, 50])
           + bytes([9, 4, 0, 0, 2, 0x08, 0x06, 0x50, 0])
           + bytes([7, 5, 0x81, 2, 0x00, 2, 0])
           + bytes([7, 5, 0x01, 2, 0x00, 2, 0]))
    c = Config(cfg)
    msc = c.find_msc()
    ok &= check(msc is not None, "finds the MSC interface")
    if msc:
        iface, ep_in, ep_out = msc
        ok &= check(iface.protocol == 0x50
                    and ep_in.addr == 0x81 and ep_out.addr == 0x01
                    and ep_in.maxpacket == 512,
                    "bulk endpoints parsed from the tree",
                    f"{iface} {ep_in} {ep_out}")
    # IAD-grouped function: the association covers both interfaces
    cfg2 = (bytes([9, 2, 55, 0, 2, 1, 0, 0x80, 50])
            + bytes([8, 0x0B, 0, 2, 0x02, 0x0C, 0x01, 0])
            + bytes([9, 4, 0, 0, 1, 0x0E, 0x01, 0x03, 0])
            + bytes([7, 5, 0x82, 3, 0x40, 0, 1])
            + bytes([9, 4, 1, 0, 2, 0x0A, 0x00, 0x00, 0])
            + bytes([7, 5, 0x83, 2, 0x00, 2, 0])
            + bytes([7, 5, 0x03, 2, 0x00, 2, 0])
            + bytes([7, 5, 0x04, 2, 0x00, 2, 0]))   # not a 1+1 bulk pair
    c2 = Config(cfg2)
    ok &= check(len(c2.interfaces) == 2, "IAD blob: two interfaces",
                str(len(c2.interfaces)))
    ok &= check(c2.interfaces[1].iad == (0x02, 0x0C, 0x01),
                "IAD attached to the covered interface",
                str(c2.interfaces[1].iad))
    ok &= check(c2.find_msc() is None, "no MSC in a CDC blob")
    # g_acm_ms shape: IAD grouping comm+data, then the MSC function
    cfg3 = (bytes([9, 2, 0, 0, 3, 1, 0, 0x80, 50])
            + bytes([8, 0x0B, 0, 2, 0x02, 0x02, 0x00, 0])   # IAD: CDC
            + bytes([9, 4, 0, 0, 1, 0x02, 0x02, 0x00, 0])    # comm (ACM)
            + bytes([7, 5, 0x83, 3, 8, 0, 2])                # notify, int
            + bytes([9, 4, 1, 0, 2, 0x0A, 0x00, 0x00, 0])    # data
            + bytes([7, 5, 0x82, 2, 0x00, 2, 0])             # bulk in
            + bytes([7, 5, 0x02, 2, 0x00, 2, 0])             # bulk out
            + bytes([9, 4, 2, 0, 2, 0x08, 0x06, 0x50, 0])    # MSC
            + bytes([7, 5, 0x85, 2, 0x00, 2, 0])
            + bytes([7, 5, 0x05, 2, 0x00, 2, 0]))
    total = len(cfg3)
    cfg3 = bytes([9, 2, total & 0xFF, total >> 8, 3, 1, 0, 0x80, 50]) \
        + cfg3[9:]
    c3 = Config(cfg3)
    acm = c3.find_acm()
    ok &= check(acm is not None, "finds the CDC-ACM function")
    if acm:
        comm, data, ep_in, ep_out = acm
        ok &= check(comm.iface_class == 0x02 and comm.subclass == 0x02
                    and data.iface_class == 0x0A
                    and ep_in.addr == 0x82 and ep_out.addr == 0x02,
                    "comm/data pairing via IAD with the bulk data pair",
                    f"{comm} {data} {ep_in} {ep_out}")
        ok &= check(comm.iad == data.iad == (0x02, 0x02, 0x00),
                    "IAD attaches to every covered interface",
                    f"{comm.iad} {data.iad}")
    msc = c3.find_msc()
    ok &= check(msc is not None and msc[1].addr == 0x85 and msc[2].addr == 0x05,
                "MSC still found in the composite", str(msc))
    # vendor/product come from the descriptor, not assumptions
    dev = DeviceDescriptor(bytes([18, 1])
                           + struct.pack("<HBBBBHHHBBBB", 0x0200, 0, 0, 0,
                                         64, 0x2464, 0x0051, 0, 0, 0, 3, 1))
    ok &= check(dev.vendor == 0x2464 and dev.product == 0x0051
                and dev.ep0_maxpacket == 64 and dev.num_configs == 1,
                "device descriptor fields")
    dev2 = DeviceDescriptor(bytes([18, 1])
                            + struct.pack("<HBBBBHHHBBBB", 0x0200, 0, 0, 0,
                                          64, 0x0525, 0xA4AA, 0, 0, 0, 1, 1))
    ok &= check(dev2.vendor == 0x0525 and dev2.product == 0xA4AA,
                "Linux g_acm_ms default id parsed too")
    return ok


def test_bot():
    print("CBW/CSW builders")
    ok = True
    cbw = Msc.build_cbw(0xDEADBEEF, 512, True, 0,
                        bytes([SCSI_READ10, 0]) + (16).to_bytes(4, "big")
                        + bytes([0]) + (1).to_bytes(2, "big") + bytes(1))
    ok &= check(len(cbw) == 31, "CBW is 31 bytes", str(len(cbw)))
    sig, tag, dlen, flags, lun, dlen_cdb = struct.unpack_from("<IIIBBB", cbw)
    ok &= check(sig == CBW_SIG and tag == 0xDEADBEEF and dlen == 512
                and flags == 0x80 and lun == 0 and dlen_cdb == 10,
                "CBW fields", f"{sig:#x} {tag:#x} {dlen} {flags:#x}")
    ok &= check(cbw[25:31] == b"\0" * 6, "CDB zero-padded to 16")
    csw = struct.pack("<IIIB", CSW_SIG, 7, 4, 0)
    res, st = Msc.parse_csw(csw, 7)
    ok &= check(res == 4 and st == 0, "CSW parse")
    for bad, why in ((struct.pack("<IIIB", 0x1234, 7, 0, 0), "signature"),
                     (struct.pack("<IIIB", CSW_SIG, 8, 0, 0), "tag"),
                     (b"\0" * 12, "length")):
        try:
            Msc.parse_csw(bad, 7)
            ok &= check(False, f"CSW rejects bad {why}")
        except MscError:
            ok &= check(True, f"CSW rejects bad {why}")
    return ok


def test_fat(fmt, size):
    name = {12: "FAT12", 16: "FAT16", 32: "FAT32"}[fmt]
    print(f"FAT layer on {name} ({size // (1 << 20) or size // 1024}"
          f" {'MiB' if size >= 1 << 20 else 'KiB'})")
    path = f"/tmp/usbhost-selftest-fat{fmt}.img"
    try:
        img = make_image(path, fmt, size, "NEST")
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        if fmt == 16:
            print(f"  mkfs.vfat unavailable ({e}); in-code FAT16 image")
            img = build_fat16_image(size)
        else:
            print(f"  SKIP (no mkfs.vfat: {e})")
            return True
    disk = MemDisk(img)
    # a subdirectory to put into, when mtools can make one
    try:
        subprocess.run(["mmd", "-i", path, "::/SUB"], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(path, "rb") as f:
            disk = MemDisk(f.read())
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass
    vol = FatVolume(disk)
    ok = check(vol.fat_type == fmt, f"detected {name}", f"got FAT{vol.fat_type}")
    ok &= check(vol.label == "NEST", "label", repr(vol.label))
    fresh = vol.list_dir()
    ok &= check(all(e["attr"] & (ATTR_VOLUME_ID | ATTR_DIR) for e in fresh)
                and len(fresh) <= 2,
                "fresh root holds only the volume label (and the mtools "
                "subdirectory)",
                str([(e["name"], hex(e["attr"])) for e in fresh]))
    # three files: tiny, multi-cluster, empty
    hello = b"hello nest dropbox\n"
    big = bytes(range(256)) * 391            # 100096 B, spans clusters
    empty = b""
    vol.put_file(hello, "HELLO.TXT")
    vol.put_file(big, "BIG.BIN")
    vol.put_file(empty, "EMPTY.BIN")
    ok &= check(vol.read_file("/HELLO.TXT") == hello, "read back HELLO.TXT")
    ok &= check(vol.read_file("/BIG.BIN") == big, "read back BIG.BIN")
    ok &= check(vol.read_file("/EMPTY.BIN") == empty, "read back EMPTY.BIN")
    names = {e["name"] for e in vol.list_dir()}
    ok &= check({"HELLO.TXT", "BIG.BIN", "EMPTY.BIN"} <= names,
                "root listing has all three", str(names))
    entry = [e for e in vol.list_dir() if e["name"] == "BIG.BIN"][0]
    ok &= check(entry["size"] == len(big), "directory size field")
    vol.put_file(hello * 40, "HELLO.TXT")   # overwrite: free + rechain
    ok &= check(vol.read_file("/HELLO.TXT") == hello * 40,
                "overwrite rewrites the same entry")
    subdir = [e for e in vol.list_dir() if e["attr"] & ATTR_DIR]
    if subdir:
        vol.put_file(big[:5000], "IN.TXT", "/SUB")
        ok &= check(vol.read_file("/SUB/IN.TXT") == big[:5000],
                    "put and read back through a subdirectory")
    with open(path, "wb") as f:
        f.write(disk.data)
    for tool, args in ((["fsck.vfat", "-n", path], "fsck.vfat"),
                       (["mdir", "-i", path, "::/"], "mtools mdir"),
                       (["mtype", "-i", path, "::/BIG.BIN"], "mtools mtype")):
        try:
            r = subprocess.run(tool, capture_output=True)
            if tool[0] == "mtype":
                ok &= check(r.stdout == big, f"{tool[0]} reads BIG.BIN "
                            f"independently", f"rc={r.returncode}")
            elif tool[0] == "fsck.vfat":
                ok &= check(r.returncode == 0, "fsck.vfat clean",
                            r.stdout.decode("ascii", "replace").strip())
            else:
                # mdir prints 8.3 names space-padded, not dotted
                ok &= check(b"BIG" in r.stdout, "mtools sees the entry")
        except FileNotFoundError:
            print(f"  note: {tool[0]} not installed, skipped")
    if os.path.exists(path):
        os.unlink(path)
    return ok


def selftest():
    print("usbhost selftest")
    ok = test_descriptors()
    ok &= test_bot()
    ok &= test_fat(12, 1474560)
    ok &= test_fat(16, 16 << 20)
    ok &= test_fat(32, 64 << 20)
    print("SELFTEST " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


# ---- CLI ------------------------------------------------------------------

def open_gadget(spec, timeout, debug, wait_connect=None):
    bridge = Bridge(spec, timeout=timeout, debug=debug)
    if wait_connect:
        bridge.wait_connect(wait_connect)
    host = UsbHost(bridge, timeout=timeout)
    host.enumerate()
    return bridge, host


def open_device(spec, timeout, debug, lun, wait_connect=None):
    bridge, host = open_gadget(spec, timeout, debug, wait_connect)
    msc_if = host.config.find_msc()
    if not msc_if:
        raise UsbError("no mass-storage function in the configuration")
    iface, ep_in, ep_out = msc_if
    msc = Msc(host, iface, ep_in, ep_out, lun=lun, timeout=timeout)
    msc.test_unit_ready()
    return bridge, host, msc, ScsiDisk(msc)


def find_acm_or_die(host):
    acm = host.config.find_acm()
    if not acm:
        raise UsbError("no CDC-ACM function in the configuration "
                       "(expected a comm interface class 0x02 + data "
                       "interface 0x0A with a bulk pair)")
    return acm


def acm_terminal(bridge, host, acm_ifs, timeout, run_cmd=None,
                 interactive=None):
    """Bridge the CDC-ACM data endpoints to stdin/stdout: DTR up (the
    gadget's ttyGS0 getty keys off it), a reader thread draining the bulk
    IN endpoint, and the main thread forwarding stdin (raw when a tty,
    line-buffered otherwise).  Ctrl-] (0x1d) ends a raw session."""
    comm, data, ep_in, ep_out = acm_ifs
    acm = Acm(host, comm, ep_in, ep_out, timeout=timeout)
    acm.set_line_coding()
    acm.set_control_line_state(dtr=True, rts=True)
    stop = threading.Event()
    idle = [time.monotonic()]            # last byte seen from the gadget
    out = sys.stdout.buffer

    def reader():
        while not stop.is_set() and not bridge.closed:
            try:
                data = bridge.inep(ep_in.addr, BULK_CHUNK)
            except (BridgeError, OSError):
                break
            if data:
                idle[0] = time.monotonic()
                out.write(data)
                out.flush()

    threading.Thread(target=reader, daemon=True).start()
    if interactive is None:
        interactive = sys.stdin.isatty()
    if run_cmd:
        acm.write(run_cmd.encode() + b"\n")
        # until the gadget goes quiet (or the timeout) - output prints
        # as it arrives, through the reader above
        end = time.monotonic() + timeout
        while time.monotonic() < min(end, idle[0] + 5.0):
            time.sleep(0.2)
        if not interactive:
            stop.set()
            return
    raw = None
    try:
        if interactive:
            import termios
            import tty
            raw = termios.tcgetattr(0)
            tty.setraw(0)
        while not stop.is_set() and not bridge.closed:
            b = sys.stdin.buffer.read(1)
            if not b or b == b"\x1d":
                break
            acm.write(b)
    except KeyboardInterrupt:
        pass
    finally:
        if raw is not None:
            termios.tcsetattr(0, termios.TCSADRAIN, raw)
        stop.set()
        print()


def cmd_info(args):
    bridge, host, msc, disk = open_device(args.socket, args.timeout,
                                          args.debug, args.lun)
    print(host.describe())
    inq = msc.inquiry()
    print(f"SCSI: {inq['vendor']} {inq['product']} rev {inq['rev']}"
          f"{' (removable)' if inq['removable'] else ''}")
    msc.mode_sense6()
    msc.prevent_allow(False)
    print(f"LUN {args.lun}: {disk.blocks} blocks x {disk.blocksize} B = "
          f"{disk.blocks * disk.blocksize / (1 << 20):.1f} MiB")
    vol = FatVolume(disk)
    print(vol)
    for e in vol.list_dir("/"):
        kind = "dir" if e["attr"] & ATTR_DIR else \
            "vol" if e["attr"] & ATTR_VOLUME_ID else f"{e['size']:>9} B"
        print(f"  {e['name']:<20} {kind}")
    bridge.sock.close()


def cmd_put(args):
    with open(args.localfile, "rb") as f:
        data = f.read()
    name = args.name or os.path.basename(args.localfile)
    bridge, host, msc, disk = open_device(args.socket, args.timeout,
                                          args.debug, args.lun)
    vol = FatVolume(disk)
    msc.prevent_allow(True)
    dest, _, fname = name.rpartition("/")
    if not fname:
        dest, fname = "", dest
    vol.put_file(data, fname, "/" + dest)
    msc.prevent_allow(False)
    entry = [e for e in vol.list_dir("/" + dest)
             if e["name"].upper() == fname.upper()]
    print(f"put {fname}: {len(data)} bytes, "
          f"cluster {entry[0]['cluster'] if entry else '?'} -> {vol}")
    bridge.sock.close()


def cmd_get(args):
    bridge, host, msc, disk = open_device(args.socket, args.timeout,
                                          args.debug, args.lun)
    vol = FatVolume(disk)
    data = vol.read_file(args.remote)
    out = args.out or args.remote.rsplit("/", 1)[-1]
    with open(out, "wb") as f:
        f.write(data)
    print(f"got {args.remote}: {len(data)} bytes -> {out}")
    bridge.sock.close()


def cmd_acm(args):
    bridge, host = open_gadget(args.socket, args.timeout, args.debug,
                               wait_connect=args.wait)
    acm_terminal(bridge, host, find_acm_or_die(host), args.timeout,
                 run_cmd=args.run_cmd)


def cmd_auto_exploit(args):
    """The end-to-end demo: stage the payload over MSC, then wait out the
    gadget's re-enumeration as a CDC-ACM composite and drop into its
    ttyGS0 shell."""
    with open(args.tgzfile, "rb") as f:
        data = f.read()
    name = args.name or os.path.basename(args.tgzfile)
    print(f"[1/3] staging {name} ({len(data)} bytes) over MSC...")
    bridge, host, msc, disk = open_device(args.socket, args.timeout,
                                          args.debug, args.lun)
    vol = FatVolume(disk)
    msc.prevent_allow(True)
    vol.put_file(data, name)
    msc.prevent_allow(False)
    entry = [e for e in vol.list_dir("/")
             if e["name"].upper() == name.upper()]
    print(f"      put {name}: {len(data)} bytes, cluster "
          f"{entry[0]['cluster'] if entry else '?'} -> {vol}")
    print("[2/3] bus reset, dropping the link; waiting up to "
          f"{args.wait:g} s for the gadget to come back...")
    bridge.reset()
    bridge.sock.close()
    deadline = time.monotonic() + args.wait
    while True:
        try:
            bridge = Bridge(args.socket, timeout=args.timeout,
                            debug=args.debug)
            break
        except OSError:
            if time.monotonic() > deadline:
                raise BridgeError("gadget did not re-enumerate in time")
            time.sleep(0.5)
    bridge.wait_connect(max(1.0, deadline - time.monotonic()))
    host = UsbHost(bridge, timeout=max(args.timeout, 30.0))
    host.enumerate()
    d = host.device
    serial = host.strings.get(d.i_serial, "")
    print(f"[3/3] re-enumerated: {d.vendor:04x}:{d.product:04x} "
          f"{host.strings.get(d.i_product, '')!r} serial {serial!r}")
    acm = host.config.find_acm()
    if not acm:
        if host.config.find_msc():
            print("came back as mass storage only - the payload did not "
                  "load g_acm_ms; use 'info' to look at it")
        else:
            print("no CDC-ACM and no MSC function in the new device")
        return
    print("      CDC-ACM found; opening ttyGS0 "
          "(Ctrl-] to leave the terminal)")
    acm_terminal(bridge, host, acm, args.timeout, run_cmd=args.run_cmd)


SHELL_HELP = """commands:
  reset | speed hs|fs        bus reset / port speed
  setup HEX16                raw 8-byte setup packet (waits SETUPACK)
  in EP MAX                  raw INEP, prints the hex reply
  out EP HEX                 raw OUTEP (HEX may be empty)
  ctrl RT REQ VAL IDX LEN    one full control transfer (hex numbers)
  info                       enumerate + MSC + FAT summary
  ls [PATH] | cat PATH       FAT directory / file
  put PATH_LOCAL NAME [DIR]  FAT insert (same as the CLI put)
  inquiry | sense | tur | capacity | read10 LBA N | write10 LBA HEX
  quit"""


def cmd_shell(args):
    bridge = Bridge(args.socket, timeout=args.timeout, debug=args.debug)
    host = UsbHost(bridge, timeout=args.timeout)
    state = {}

    def ensure():
        if "disk" not in state:
            h, m, d = open_state(bridge, args)
            state.update(host=h, msc=m, disk=d)

    def open_state(br, args):
        host.enumerate()
        msc_if = host.config.find_msc()
        if not msc_if:
            raise UsbError("no mass-storage function found")
        iface, ep_in, ep_out = msc_if
        msc = Msc(host, iface, ep_in, ep_out, lun=args.lun,
                  timeout=args.timeout)
        return host, msc, ScsiDisk(msc)

    print(SHELL_HELP)
    while True:
        try:
            line = input("usbhost> ").strip()
        except EOFError:
            return
        except KeyboardInterrupt:
            print()
            continue
        parts = line.split()
        if not parts:
            continue
        cmd, rest = parts[0], parts[1:]
        try:
            if cmd in ("q", "quit", "exit"):
                return
            elif cmd == "help":
                print(SHELL_HELP)
            elif cmd == "reset":
                bridge.reset()
            elif cmd == "speed" and rest:
                bridge.speed(rest[0])
            elif cmd == "setup" and rest:
                bridge.setup(bytes.fromhex(rest[0]))
                print("SETUPACK")
            elif cmd == "in" and len(rest) >= 2:
                print(bridge.inep(int(rest[0], 0), int(rest[1], 0)).hex())
            elif cmd == "out" and len(rest) >= 1:
                bridge.outep(int(rest[0], 0),
                             bytes.fromhex(rest[1]) if len(rest) > 1
                             else b"")
            elif cmd == "ctrl" and len(rest) >= 5:
                rtype = int(rest[0], 16)
                length = int(rest[4], 0)
                out = host.control(rtype, int(rest[1], 16), int(rest[2], 16),
                                   int(rest[3], 16), length)
                print(out.hex() if out is not None else "(status ok)")
            elif cmd == "info":
                ensure()
                print(host.describe())
                inq = state["msc"].inquiry()
                print(f"SCSI: {inq['vendor']} {inq['product']} "
                      f"rev {inq['rev']}")
                print(FatVolume(state["disk"]))
            elif cmd in ("ls", "cat"):
                ensure()
                if cmd == "ls":
                    for e in FatVolume(state["disk"]).list_dir(
                            rest[0] if rest else "/"):
                        print(f"  {e['name']:<20} attr {e['attr']:#04x} "
                              f"cluster {e['cluster']} size {e['size']}")
                else:
                    out = FatVolume(state["disk"]).read_file(rest[0])
                    print(f"({len(out)} bytes) {out[:512].hex()}")
            elif cmd == "put" and len(rest) >= 2:
                ensure()
                with open(rest[0], "rb") as f:
                    data = f.read()
                vol = FatVolume(state["disk"])
                vol.put_file(data, rest[1], rest[2] if len(rest) > 2 else "/")
                print("done")
            elif cmd == "inquiry":
                ensure()
                print(state["msc"].inquiry())
            elif cmd == "sense":
                ensure()
                print(state["msc"].request_sense())
            elif cmd == "tur":
                ensure()
                state["msc"].test_unit_ready()
                print("ready")
            elif cmd == "capacity":
                ensure()
                print(state["msc"].read_capacity())
            elif cmd == "read10" and len(rest) >= 2:
                ensure()
                data = state["msc"].read10(int(rest[0], 0), int(rest[1], 0),
                                           state["disk"].blocksize)
                print(data.hex())
            elif cmd == "write10" and len(rest) >= 2:
                ensure()
                state["msc"].write10(int(rest[0], 0),
                                     bytes.fromhex(rest[1]).ljust(
                                         state["disk"].blocksize, b"\0"),
                                     state["disk"].blocksize)
                print("done")
            else:
                print("unknown command; help lists them")
        except (BridgeError, UsbError, MscError, FatError,
                ValueError, OSError) as e:
            print(f"error: {e}")


def main():
    ap = argparse.ArgumentParser(
        description="Virtual USB host for the cihdrc-udc chardev bridge")
    ap.add_argument("--socket", metavar="PATH|tcp:HOST:PORT",
                    help="bridge chardev socket")
    ap.add_argument("--timeout", type=float, default=10.0,
                    help="seconds per USB operation (default 10)")
    ap.add_argument("--debug", action="store_true", help="log bridge lines")
    ap.add_argument("--lun", type=int, default=0, help="mass-storage LUN")
    ap.add_argument("--selftest", action="store_true",
                    help="offline test: descriptors, BOT, FAT (no QEMU)")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("info", help="enumerate, descriptors, medium, root dir")
    p = sub.add_parser("put", help="insert a file into the FAT volume")
    p.add_argument("localfile")
    p.add_argument("--name", help="destination 8.3 name (default: basename)")
    p = sub.add_parser("get", help="read a file out of the volume")
    p.add_argument("remote")
    p.add_argument("--out", help="output file (default: remote basename)")
    sub.add_parser("shell", help="raw ep0/bulk/MSC/FAT REPL")
    p = sub.add_parser("acm", help="bridge CDC-ACM ttyGS0 to the terminal")
    p.add_argument("--run-cmd", help="run one command first, then "
                    "interact (non-tty stdin: run and exit)")
    p.add_argument("--wait", type=float, default=30.0,
                   help="seconds to wait for the gadget to connect "
                        "(default 30)")
    p = sub.add_parser("auto-exploit", help="stage a payload over MSC, wait "
                       "for re-enumeration, open the ACM shell")
    p.add_argument("tgzfile")
    p.add_argument("--name", help="8.3 name for the staged file "
                    "(default: basename)")
    p.add_argument("--run-cmd", help="command to run on ttyGS0 first "
                    "(e.g. 'id; cat /proc/uptime')")
    p.add_argument("--wait", type=float, default=300.0,
                   help="seconds to wait for re-enumeration (default 300)")
    args = ap.parse_args()

    if args.selftest:
        sys.exit(selftest())
    if not args.socket or not args.cmd:
        ap.error("need --socket and a command (or --selftest)")
    try:
        {"info": cmd_info, "put": cmd_put, "get": cmd_get,
         "shell": cmd_shell, "acm": cmd_acm,
         "auto-exploit": cmd_auto_exploit}[args.cmd](args)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
