#!/usr/bin/env python3
"""
Fake backplate (sensor) MCU for the Nest Display-3.4 emulation.

The product's front unit talks to the MCU on its wall backplate over a
UART (the board's "sensor MCU" serial, /dev/ttysensormcu0 = UART6; the
backplate owns the ambient light sensor, PIR, proximity, touch and
temperature inputs). nlclient keeps the panel asleep until the ALS
reports ("did not light display - waiting for ALSvis"); this stub speaks
that protocol so the app can get real sensor data.

Frame format (as nlclient speaks it, byte-for-byte on the wire):

    d5 aa 96 | type:u16le | len:u16le | payload | crc16:u16le

CRC-16/CCITT, poly 0x1021, init 0x0000, MSB-first, over type+len+payload
(everything between the magic and the trailer), trailer little-endian.
Max payload 254 bytes (len+2 <= 0x100).

Messages the front unit accepts. The wire type is NOT the index into its
handler table at 0x1c8898: once attached, the receiver (0x1c721c) maps the
wire type to an event id through the byte table at 0x429480+0x1e0 first.
Wire types and payloads (from nlclient's disassembly):
  0x02 MSG_TEMP      4 B: s16 centi-degC, u16 per-mille RH (0x8000 temp =
                     "BP reset", 0xffff RH = invalid)
  0x05 MSG_PIR       4 B: two u16; any non-zero pair means motion (a wake)
  0x07 MSG_NEAR_PIR  2 B
  0x08 MSG_PROX      40 B
  0x0a MSG_ALS       4 B: u16 visible, u16 infrared - the live ambient light
                     the display logic uses, raw counts, 0xffff = error
  0x22 MSG_TEMP_BUF  N x 4 B, same layout as MSG_TEMP
  0x27 MSG_PIR_BUF   N x 8 B
  0x28 MSG_PROX_BUF  N x 16 B
  0x29 MSG_ALS_BUF   N x 6 B: u16 (unused), u16 min, u16 max - event log only
  0x2f MSG_END_BUF   empty ("compatible": no count check), or a u16 count of
                     the accepted Temp/Temp2/Pir/Prox/Als buffers
(0x04 is the HVAC Wires message and 0x09 the Pins - never send those.)

Display logic, as far as the backplate matters: brightness follows the
filtered visible count on brightness.plist's curve (never under 50 %), a
dark panel only lights when vis > 4 (DarkRoom), a jump of more than 150
counts is treated as a spike and delays the update, PIR/prox motion wakes
the display, and without a TEMP in the last 61 s a wake waits up to 1 s
for one.

Wiring and the handshake (the front unit's side, nlclient disassembly):
  0xff reset (after a UART break) -> MSG_WIRES 0x04 (13 B, per terminal:
       0 W1, 1 Y1, 2 G, 3 O/B, 4 AUX/W2, 7 Y2, 8 C, 9 Rc, 10 Rh, 11 STAR;
       bit0 = connected) then MSG_PINS 0x09 (15 B: 0 W1, 1 Y1, 2 C, 3 Rc,
       4 Rh, 5 G, 6 O/B, 7 AUX/W2, 9 Y2, 11 STAR; 1 = wire inserted).
       Without them, after 6 resets the front raises "please reattach",
       and without an R wire "power might be off". Wires sent any other
       time count as a backplate-initiated reset.
  0x90 -> 0x10 FW_ID (3 B), then each answer draws the next request:
       0x98 -> 0x18 FW version, 0x99 -> 0x19 FW info, 0x9d -> 0x1d BSL id
       (u16), 0x9b -> 0x1b BSL version, 0x9c -> 0x1c BSL info, 0x9f -> 0x1f
       serial, 0x9e -> 0x1e model (strings: raw bytes, no NUL; the two
       info replies are "description version user@host date time"). We
       start out as the firmware the stock nlbpfirmware.plist ships for
       Backplate-6.x (TFE 2.3.11, BSL 3.1) field for field, so its
       firmware sees nothing to update; after a firmware update we report
       what was flashed.
  0x85 -> 0x17 BP status (u16).
  0xc0 (u32 power-steal mask), 0x8f (our wires echoed), 0x83 (tick), 0xb1:
       no reply. "Attached" itself comes from a detect GPIO.
After the handshake the front wants a frame at least every 5 s; the live
reports below provide that.

Firmware update (nlbpupdater, run by osm when a system update carries newer
backplate firmware; the plist's S-record image for our model, one line per
record). The front's controller walks WaitingUpdateBeginAck ->
WaitingUpdateSRecordAcks -> WaitingUpdateEndAck; every step is answered
with the one update reply, and an unanswered step is resent (6 tries,
then a reset and a failed update):
  0x91 BEGIN   3 B: u16 seq (0), u8 kind ('M' mono, 'B' BSL)
  0x92 RECORD  u16 seq (1, 2, ...), then one S-record line, no CR/LF
  0x93 END     2 B: u16 seq (records + 1)
  -> 0x11 UPDATE_ACK  2 B: u16 seq, echoing the step's
A BSL takes the whole image before it programs, and speaks nothing else
meanwhile, so the live reports pause from BEGIN until the new firmware
runs. A mono image carries its own build strings (description, version,
date, user@host, NUL-separated), which the firmware's info reply is made
of; that's where the new identity comes from. nlbpupdater only rewrites
the BSL on a version change, and the version is in the S0 record.
With --flash FILE what was flashed survives a restart, as the MCU's flash
does: the identities it runs are read from FILE (JSON) and saved there
after every update. Without it every start runs the stock firmware, and a
system that shipped newer backplate firmware updates it again at each boot
(and, doing so, raises "The wiring to your equipment has changed").

Switching (the front unit's side): 0x82 SWITCH, 2 B: u8 circuit, u8 state
(1 on, 0 off), numbered as MSG_WIRES' terminals: nlclient's "sent circuit
change request for switch id (0) with state (1)" is W1 on. The backplate
answers each with 0x06 ACK_SWITCH, the same two bytes; until it does the
controller waits (WaitingAcks), deferring everything else, the next
switch included.

The room: the temperature reported follows what the unit switches. It
drifts towards --ambient (time constant --room-tau hours), rises by
--heat-rate degrees an hour while W1 or AUX/W2 is on, and falls by
--cool-rate while Y1 or Y2 is; with a heat pump (O/B wired) the compressor
heats unless O/B is on (O orientation) and cools while it is.

Sensor buffers: the backplate samples the temperature every 30 s, and the
front asks for what it has with 0xa2 FLUSH; the answer is 0x22 TEMP_BUF
messages (the samples, oldest first, each as MSG_TEMP's 4 bytes) then
0x2f END_BUF (u16, how many buffer messages came before it), which the
front acks with 0xa3. nlclient's history (nlHistory) is built from these,
not from the live MSG_TEMP: with none, its time to temperature predictor
has no temperature to start from and the unit reports no time to target.

By default this backplate reports a steadily lit room (vis 200, about 92 %
brightness), starting at 21.5 C / 45 %RH, and someone in front of it (PIR
motion on every poll), so the display wakes and stays on. --no-presence leaves the
room empty: the display then sleeps after the client's inactivity timeout
like a real unit on a quiet wall.
"""
import argparse
import json
import math
import os
import socket
import struct
import sys
import threading
import time

MAGIC = b"\xd5\xaa\x96"
MAX_PAYLOAD = 0x400     # the largest the front sends: an update record

# back -> front message types (see module docstring)
MSG_TEMP = 0x02
MSG_WIRES = 0x04
MSG_PIR = 0x05
MSG_ACK_SWITCH = 0x06
MSG_PINS = 0x09
MSG_ALS = 0x0a
MSG_UPDATE_ACK = 0x11
MSG_TEMP_BUF = 0x22
MSG_END_BUF = 0x2f

# front -> back firmware update requests
MSG_SWITCH = 0x82
MSG_FLUSH = 0xa2
MSG_UPDATE_BEGIN = 0x91
MSG_UPDATE_RECORD = 0x92
MSG_UPDATE_END = 0x93

SAMPLE_PERIOD = 30      # seconds between buffered temperature samples
MAX_SAMPLES = 240       # what the buffer holds: two hours of them

# Terminal -> byte index in MSG_WIRES / MSG_PINS (they differ).
WIRE_INDEX = {"W1": 0, "Y1": 1, "G": 2, "O/B": 3, "AUX/W2": 4, "Y2": 7,
              "C": 8, "Rc": 9, "Rh": 10, "STAR": 11}
PIN_INDEX = {"W1": 0, "Y1": 1, "C": 2, "Rc": 3, "Rh": 4, "G": 5, "O/B": 6,
             "AUX/W2": 7, "Y2": 9, "STAR": 11}
DEFAULT_WIRES = "Rh,C,W1,Y1,G"


def wiring(terminals):
    """MSG_WIRES and MSG_PINS payloads for these connected terminals."""
    wires = bytearray(13)
    pins = bytearray(15)
    for w in terminals:
        wires[WIRE_INDEX[w]] = 1
        pins[PIN_INDEX[w]] = 1
    return bytes(wires), bytes(pins)


def fw_info(version, description, host, user, date):
    """A firmware info record as the MCU reports it: the build strings its
    image carries (Backplate-6.x TFE 2.3.11 at 0x800f129), space-separated
    as nlclient parses them (0x1ea7f4): description (a "(...)" group stays
    with it), version, user@host, then the date to the end. It compares
    every field with the image it ships (nlbpfirmware.plist); any
    difference schedules a backplate update."""
    return f"{description} {version} {user}@{host} {date}".encode()


# What the MCU starts out running: the Backplate-6.x production firmware the
# stock nlbpfirmware.plist ships (TFE (BP_A2) 2.3.11, BSL 3.1).
MONO = dict(version="2.3.11", description="TFE (BP_A2)",
            host="jenkins-agent-190-v17-emb-prod", user="jenkins-slave",
            date="2019-04-05 20:26:32")
BSL = dict(version="3.1", description="BSL",
           host="jenkins-agent-190-v17-emb-prod", user="jenkins-slave",
           date="2019-04-05 20:26:31")


def srec_image(records):
    """S-record lines -> (S0 header text, {address: data}). None if any
    record's checksum is wrong."""
    header, mem = "", {}
    for line in records:
        raw = bytes.fromhex(line[2:])
        if len(raw) != raw[0] + 1 or sum(raw) & 0xFF != 0xFF:
            return None
        alen = {"0": 2, "1": 2, "2": 3, "3": 4}.get(line[1])
        if alen is None:
            continue                      # S5/S7..S9: counts and entry points
        data = raw[1 + alen:-1]
        if line[1] == "0":
            header = data.decode("ascii", "replace")
        else:
            mem[int.from_bytes(raw[1:1 + alen], "big")] = data
    return header, mem


def build_strings(mem, version):
    """A mono image's build strings: description, version, date, user@host,
    NUL-separated, found by the version the S0 record names."""
    base = min(mem)
    img = bytearray(max(a + len(d) for a, d in mem.items()) - base)
    for a, d in mem.items():
        img[a - base:a - base + len(d)] = d
    at = img.find(b"\0" + version.encode() + b"\0")
    if at < 0:
        return None
    start = img.rfind(b"\0", 0, at) + 1
    fields = img[start:].split(b"\0", 4)[:4]
    description, _, date, userhost = (f.decode("ascii", "replace") for f in fields)
    user, _, host = userhost.partition("@")
    return dict(version=version, description=description, host=host,
                user=user, date=date)


def crc16_ccitt(data, crc=0):
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def frame(msg_type, payload=b""):
    body = struct.pack("<HH", msg_type, len(payload)) + payload
    return MAGIC + body + struct.pack("<H", crc16_ccitt(body))


def follow_parent(pid):
    # Reparenting, not kill(pid, 0): a dead parent stays a zombie (and its
    # PID can be reused) until whoever started run.sh reaps it.
    while os.getppid() == pid:
        time.sleep(1)
    os._exit(0)


class Backplate:
    def __init__(self, spec, debug=False, als_vis=200, als_ir=60,
                 temp_c=21.5, rh=45.0, presence=True, period=2.0,
                 wires=("Rh", "C", "W1", "Y1", "G"), flash_file=None,
                 ambient=16.0, heat_rate=3.0, cool_rate=2.5, room_tau=8.0):
        self.spec = spec
        self.debug = debug
        self.als_vis = als_vis
        self.als_ir = als_ir
        self.temp_c = temp_c
        self.rh = rh
        # the room around it: degrees C, degrees C an hour, hours
        self.ambient = ambient
        self.heat_rate = heat_rate
        self.cool_rate = cool_rate
        self.room_tau = room_tau
        self.circuits = set()                 # the terminals switched on
        self.room_time = time.monotonic()
        self.samples = []                     # TEMP_BUF samples not yet flushed
        self.sample_time = self.room_time
        self.presence = presence
        self.period = period
        self.wires = wires
        self.attached = threading.Event()     # the front unit reset us
        self.mono = dict(MONO)
        self.bsl = dict(BSL)
        self.flash_file = flash_file
        if flash_file and os.path.exists(flash_file):
            with open(flash_file) as f:
                saved = json.load(f)
            self.mono, self.bsl = saved["mono"], saved["bsl"]
        self.update = None                    # (kind, {seq: record}) in the BSL
        self.buf = b""
        self.sock = None
        self.lock = threading.Lock()

    # ---- wire ----
    def connect(self):
        while True:
            try:
                if self.spec.startswith("tcp:"):
                    _, host, port = self.spec.split(":", 2)
                    s = socket.create_connection((host, int(port)), timeout=3)
                else:
                    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    s.settimeout(3)
                    s.connect(self.spec)
                s.settimeout(None)
                if self.debug:
                    print(f"connected to {self.spec}", file=sys.stderr)
                return s
            except OSError:
                time.sleep(0.5)

    def send(self, msg_type, payload=b""):
        f = frame(msg_type, payload)
        with self.lock:
            try:
                self.sock.sendall(f)
            except OSError:
                pass
        if self.debug:
            print(f"-> type=0x{msg_type:04x} len={len(payload)} "
                  f"{payload.hex()[:48]}", file=sys.stderr)

    def frames(self, sock):
        """Yield (type, payload, crc_ok) frames from the front unit."""
        while True:
            while self.buf[:3] != MAGIC:
                i = self.buf.find(MAGIC)
                if i < 0:
                    self.buf = self.buf[-2:]
                    data = sock.recv(4096)
                    if not data:
                        raise OSError("closed")
                    if self.debug:
                        print(f"raw: {data.hex()}", file=sys.stderr)
                    self.buf += data
                    continue
                self.buf = self.buf[i:]
            if len(self.buf) < 7:
                data = sock.recv(4096)
                if not data:
                    raise OSError("closed")
                if self.debug:
                    print(f"raw: {data.hex()}", file=sys.stderr)
                self.buf += data
                continue
            mtype, mlen = struct.unpack_from("<HH", self.buf, 3)
            if mlen > MAX_PAYLOAD:
                self.buf = self.buf[1:]            # not a header: resync
                continue
            total = 7 + mlen + 2
            if len(self.buf) < total:
                data = sock.recv(4096)
                if not data:
                    raise OSError("closed")
                if self.debug:
                    print(f"raw: {data.hex()}", file=sys.stderr)
                self.buf += data
                continue
            fr = self.buf[:total]
            body = fr[3:-2]
            want = struct.unpack("<H", fr[-2:])[0]
            payload = fr[7:-2]
            ok = crc16_ccitt(body) == want
            # A bad frame may be the tail of one cut short: rescan past its
            # magic only, so the frames behind it aren't lost with it.
            self.buf = self.buf[total if ok else 1:]
            yield mtype, payload, ok

    # ---- the room ----
    def hvac(self):
        """(heating, cooling) as the circuits switched on make them."""
        on = self.circuits
        compressor = bool(on & {WIRE_INDEX["Y1"], WIRE_INDEX["Y2"]})
        heating = bool(on & {WIRE_INDEX["W1"], WIRE_INDEX["AUX/W2"]})
        cooling = compressor
        if "O/B" in self.wires:              # a heat pump, O orientation
            reversing = WIRE_INDEX["O/B"] in on
            heating = heating or (compressor and not reversing)
            cooling = compressor and reversing
        return heating, cooling

    def room_step(self):
        """Move the room's temperature on to now."""
        now = time.monotonic()
        hours = (now - self.room_time) / 3600
        self.room_time = now
        heating, cooling = self.hvac()
        self.temp_c += (self.ambient - self.temp_c) * (1 - math.exp(-hours / self.room_tau))
        self.temp_c += (self.heat_rate * heating - self.cool_rate * cooling) * hours

    def switch(self, circuit, state):
        self.room_step()                      # what the old state did, so far
        if state:
            self.circuits.add(circuit)
        else:
            self.circuits.discard(circuit)
        heating, cooling = self.hvac()
        print(f"backplate: circuit {circuit} {'on' if state else 'off'} "
              f"(heating {int(heating)}, cooling {int(cooling)}, room "
              f"{self.temp_c:.2f} C)", file=sys.stderr, flush=True)

    # ---- sensor data ----
    def temp_payload(self):
        return struct.pack("<hH", round(self.temp_c * 100), round(self.rh * 10))

    def report(self):
        """The live readings: ambient light (kept constant - a jump over 150
        counts reads as a spike), temperature, and motion if someone's there."""
        self.send(MSG_ALS, struct.pack("<HH", self.als_vis, self.als_ir))
        self.room_step()
        self.send(MSG_TEMP, self.temp_payload())
        while time.monotonic() - self.sample_time >= SAMPLE_PERIOD:
            self.sample_time += SAMPLE_PERIOD
            self.samples = self.samples[-(MAX_SAMPLES - 1):] + [self.temp_payload()]
        if self.presence:
            self.send(MSG_PIR, struct.pack("<HH", 0x03E8, 0x03E8))

    def flush(self):
        """The samples since the last flush, then how many messages held them."""
        samples, self.samples = self.samples, []
        chunks = [samples[i:i + 32] for i in range(0, len(samples), 32)]
        for chunk in chunks:
            self.send(MSG_TEMP_BUF, b"".join(chunk))
        self.send(MSG_END_BUF, struct.pack("<H", len(chunks)))

    def report_wiring(self):
        """The answer to a reset: which terminals have a wire (and power)."""
        wires, pins = wiring(self.wires)
        self.send(MSG_WIRES, wires)
        self.send(MSG_PINS, pins)

    # ---- loop ----
    def stream(self):
        """Like the real MCU, push the live readings on our own clock: the
        front unit only polls (0xff) now and then, and motion has to keep
        arriving for the display to stay awake."""
        self.attached.wait()
        while True:
            time.sleep(self.period)
            if self.update is None:
                self.report()

    def serve_forever(self):
        threading.Thread(target=self.stream, daemon=True).start()
        while True:
            sock = self.connect()
            self.sock = sock
            try:
                for mtype, payload, ok in self.frames(sock):
                    if self.debug:
                        print(f"<- type=0x{mtype:04x} len={len(payload)} "
                              f"crc={'ok' if ok else 'BAD'} "
                              f"{payload.hex()[:48]}", file=sys.stderr)
                    if ok:
                        self.handle(mtype, payload)
            except OSError as e:
                if self.debug:
                    print(f"connection lost ({e}), reconnecting",
                          file=sys.stderr)
                try:
                    sock.close()
                except OSError:
                    pass
                time.sleep(0.5)

    def identity(self, mtype):
        """The handshake reply to a request, or None."""
        m, b = self.mono, self.bsl
        return {
            0x90: (0x10, b"\x01\x00\x00"),                  # FW id
            0x98: (0x18, m["version"].encode()),             # FW version
            0x99: (0x19, fw_info(**m)),                      # FW info
            0x9d: (0x1d, b"\x01\x00"),                      # BSL id
            0x9b: (0x1b, b["version"].encode()),             # BSL version
            0x9c: (0x1c, fw_info(**b)),                      # BSL info
            0x9f: (0x1f, b"02AA01AB0000001"),                # serial
            0x9e: (0x1e, b"Backplate-6.0"),                  # model
            0x85: (0x17, b"\x00\x00"),                      # BP status
        }.get(mtype)

    def flash(self, kind, records):
        """Program the image and run it. False if it doesn't parse."""
        parsed = srec_image(records)
        if parsed is None:
            return False
        header, mem = parsed
        version = header.rpartition("-")[2]           # "tfe-2.3.27", "bsl-3.1"
        if kind == "B":
            self.bsl["version"] = version
        else:
            strings = build_strings(mem, version) if mem else None
            if strings is None:
                return False
            self.mono = strings
        print(f"backplate: flashed {'BSL' if kind == 'B' else 'mono'} "
              f"{version} ({len(records)} records)", file=sys.stderr)
        if self.flash_file:
            with open(self.flash_file + ".new", "w") as f:
                json.dump({"mono": self.mono, "bsl": self.bsl}, f, indent=1)
            os.replace(self.flash_file + ".new", self.flash_file)
        return True

    def handle(self, mtype, payload):
        if mtype == MSG_SWITCH and len(payload) == 2:
            circuit, state = payload
            if (circuit in self.circuits) != bool(state):
                self.switch(circuit, state)
            self.send(MSG_ACK_SWITCH, payload)
        elif mtype == MSG_FLUSH:
            self.flush()
        elif mtype == 0xFF:
            self.update = None                 # a reset leaves the BSL unflashed
            self.report_wiring()
            self.attached.set()
        elif mtype == MSG_UPDATE_BEGIN and len(payload) == 3:
            seq, kind = struct.unpack("<HB", payload)
            self.update = (chr(kind), {})
            self.send(MSG_UPDATE_ACK, struct.pack("<H", seq))
        elif mtype == MSG_UPDATE_RECORD and self.update and len(payload) > 2:
            seq = struct.unpack_from("<H", payload)[0]
            line = payload[2:].decode("ascii", "replace")
            if srec_image([line]) is not None:     # a bad record goes unacked
                self.update[1][seq] = line
                self.send(MSG_UPDATE_ACK, struct.pack("<H", seq))
        elif mtype == MSG_UPDATE_END and self.update and len(payload) == 2:
            kind, records = self.update
            self.send(MSG_UPDATE_ACK, payload)
            if not self.flash(kind, [records[s] for s in sorted(records)]):
                print("backplate: update image rejected, keeping the old "
                      "firmware", file=sys.stderr)
            self.update = None
        elif self.update is None and self.identity(mtype):
            self.send(*self.identity(mtype))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", default="tcp:127.0.0.1:8105",
                    help="UART6 chardev (tcp:HOST:PORT or unix path)")
    ap.add_argument("--parent", type=int, help="exit when this PID exits")
    ap.add_argument("--debug", action="store_true", help="log every frame and raw chunk")
    ap.add_argument("--als-vis", type=lambda x: int(x, 0), default=200,
                    help="ambient light, visible counts (> 4 lights a dark "
                         "panel; 200 is about 92%% brightness)")
    ap.add_argument("--als-ir", type=lambda x: int(x, 0), default=60,
                    help="ambient light, infrared counts")
    ap.add_argument("--temp", type=float, default=21.5, help="degrees C")
    ap.add_argument("--rh", type=float, default=45.0, help="relative humidity %%")
    ap.add_argument("--ambient", type=float, default=16.0,
                    help="degrees C the room drifts towards with nothing on")
    ap.add_argument("--heat-rate", type=float, default=3.0,
                    help="degrees C an hour the heating adds")
    ap.add_argument("--cool-rate", type=float, default=2.5,
                    help="degrees C an hour the cooling takes away")
    ap.add_argument("--room-tau", type=float, default=8.0,
                    help="hours for the room to drift most of the way to --ambient")
    ap.add_argument("--no-presence", dest="presence", action="store_false",
                    help="nobody in front: no PIR motion, the display sleeps")
    ap.add_argument("--period", type=float, default=2.0,
                    help="seconds between live reports (ALS, temp, motion)")
    ap.add_argument("--wires", default=DEFAULT_WIRES,
                    help="connected terminals, of " + ",".join(WIRE_INDEX))
    ap.add_argument("--flash", metavar="FILE",
                    help="the MCU's flash: the firmware it runs, kept across "
                         "restarts (default: stock, every start)")
    args = ap.parse_args()
    unknown = set(args.wires.split(",")) - set(WIRE_INDEX)
    if unknown:
        ap.error(f"unknown terminals: {', '.join(sorted(unknown))}")
    if args.parent:
        threading.Thread(target=follow_parent, args=(args.parent,),
                         daemon=True).start()
    Backplate(args.serial, debug=args.debug, als_vis=args.als_vis,
              als_ir=args.als_ir, temp_c=args.temp, rh=args.rh,
              presence=args.presence, period=args.period,
              wires=args.wires.split(","),
              flash_file=args.flash, ambient=args.ambient,
              heat_rate=args.heat_rate, cool_rate=args.cool_rate,
              room_tau=args.room_tau).serve_forever()


if __name__ == "__main__":
    main()
