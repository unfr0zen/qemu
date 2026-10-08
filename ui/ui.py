#!/usr/bin/env python3
"""
Serve a web page that shows the emulated Nest (the gen 3's Display-3.4,
"Flintstone", or the gen 2, "j49"; --name says which) as a person would see it — the round face with its screen, the rotating
wheel and the button — with the device's console alongside.

run.sh --ui starts this next to QEMU. Everything it knows comes from the
flintstone.board chardev (see hw/arm/flintstone_board.c): the page's
button drives the line the gpio-keys node listens on, and the firmware's
own driven lines (poweroff, wifi reset, hall power, battery divider)
are reported as they change. The wheel is a machine property the page
sets through QMP (the adc model turns the detent count into the two Hall
samples). The screen comes from QMP screendump, converted to PNG here and
streamed over SSE. Nothing in the firmware knows it is being watched.

The log is the guest's serial console, which QEMU serves on a socket
(--console), so the page reads exactly what the terminal sees.

Standard library only:

    ui.py --board /tmp/flintstone-ui.XXXX/board.sock
          [--qmp QMP.sock] [--console tcp:HOST:PORT] [--port 8075]
          [--pins button=HU-BUTTON,...]
          [--parent PID --cleanup DIR]
"""
import argparse
import base64
import codecs
import collections
import hashlib
import json
import os
import queue
import re
import shutil
import signal
import socket
import socketserver
import struct
import sys
import termios
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
# The page's wordmark typeface (Inter, SIL OFL; see fonts/OFL-Inter.txt).
FONTS = os.path.join(HERE, "fonts")

def dial(spec, timeout=5):
    """
    Connect to "tcp:HOST:PORT" or "unix:PATH" (a bare path also works).
    QEMU may be in a container, so everything runs over TCP there; native
    builds can use unix sockets.
    """
    if spec.startswith("tcp:"):
        _, host, port = spec.split(":", 2)
        sock = socket.create_connection((host, int(port)), timeout=timeout)
        return sock
    path = spec[5:] if spec.startswith("unix:") else spec
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect(path)
    sock.settimeout(None)
    return sock

def parse_pins(spec):
    """
    'button=HU-BUTTON,poweroff=POWEROFF' -> {"button": "HU-BUTTON", ...}.
    """
    return dict(kv.split("=", 1) for kv in (spec or "").split(",") if "=" in kv)


class Board:
    """The line state QEMU reports, and the listeners waiting on it."""

    def __init__(self, spec):
        self.spec = spec
        self.lock = threading.Lock()
        self.pins = {}
        self.linked = False
        self.sock = None
        self.listeners = []

    def snapshot(self):
        with self.lock:
            return {"linked": self.linked, "pins": dict(self.pins)}

    def subscribe(self):
        q = queue.Queue()
        with self.lock:
            self.listeners.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.listeners.remove(q)

    def publish(self):
        state = self.snapshot()
        with self.lock:
            for q in self.listeners:
                q.put(state)

    def publish_tone(self, hz, t):
        """The piezo starts (hz) or stops (0) at guest time t (us). Each one
        goes out, unlike line states, which coalesce: a click is 3 ms."""
        with self.lock:
            for q in self.listeners:
                q.put({"tone": {"hz": hz, "t": t}})

    def drive(self, line, level):
        with self.lock:
            sock = self.sock
        if sock:
            sock.sendall(f"set {line} {level}\n".encode())

    def run(self):
        """Follow the chardev for as long as QEMU lives, and across restarts."""
        while True:
            sock = None
            try:
                # A connect can block while QEMU still holds a previous
                # client (it serves one at a time), so bound it and retry.
                sock = dial(self.spec, timeout=2)
                sock.settimeout(None)
                with self.lock:
                    self.sock = sock
                sock.sendall(b"dump\n")
                self.follow(sock)
            except OSError:
                pass
            # QEMU exited (the guest powered off), restarted, or turned us away.
            with self.lock:
                was_linked = self.linked
                self.sock = None
                self.linked = False
                self.pins = {}
            if sock:
                sock.close()
            if was_linked:
                self.publish()
            time.sleep(0.2)

    def follow(self, sock):
        buf = b""
        while True:
            data = sock.recv(4096)
            if not data:
                return
            # Connected is not linked: a second client waits in QEMU's
            # listen backlog, and only hears anything once it is served.
            with self.lock:
                self.linked = True
            buf += data
            *lines, buf = buf.split(b"\n")
            for line in lines:
                parts = line.decode(errors="replace").split()
                # "POWEROFF out 1000 1234567"
                if len(parts) >= 3 and parts[2].isdigit():
                    with self.lock:
                        self.pins[parts[0]] = int(parts[2])
                    # "BEEPER out 2000 1234567": the piezo, level in Hz
                    if parts[0] == "BEEPER" and len(parts) >= 4 and \
                            parts[3].isdigit():
                        self.publish_tone(int(parts[2]), int(parts[3]))
            self.publish()


class ConsoleLog:
    """
    The device's console, connected live. QEMU serves the serial on a
    socket (its own logfile= would hide inside a container's cache), so
    this client reads what the terminal would, replays it to new viewers
    and accepts input back from the page. Two views of the same stream:
    lines (for the scripts) and the raw text (for the page's terminal).
    """

    KEEP = 4000                            # lines a new viewer is replayed
    KEEP_RAW = 256 * 1024                  # raw text a new terminal replays

    def __init__(self, spec, echo=False):
        self.spec = spec
        self.echo = echo        # also copy the console to our stdout
        self.lock = threading.Lock()
        self.lines = []
        self.listeners = []
        self.raw = ""
        self.raw_listeners = []
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.sock = None
        self.pending = b""      # input waiting for the socket to come back

    def subscribe_raw(self):
        q = queue.Queue()
        with self.lock:
            self.raw_listeners.append(q)
            return q, self.raw

    def unsubscribe_raw(self, q):
        with self.lock:
            self.raw_listeners.remove(q)

    def subscribe(self):
        q = queue.Queue()
        with self.lock:
            self.listeners.append(q)
            return q, list(self.lines)

    def unsubscribe(self, q):
        with self.lock:
            self.listeners.remove(q)

    def send(self, text):
        data = text.encode() if isinstance(text, str) else text
        with self.lock:
            sock = self.sock
            if not sock:
                self.pending += data
                return
        try:
            sock.sendall(data)
        except OSError:
            pass

    def run(self):
        buf = b""
        while True:
            try:
                sock = dial(self.spec, timeout=2)
                sock.settimeout(None)
                with self.lock:
                    self.sock = sock
                    pending, self.pending = self.pending, b""
                if pending:
                    sock.sendall(pending)
                buf = b""
                while True:
                    data = sock.recv(4096)
                    if not data:
                        raise OSError("console closed")
                    if self.echo:
                        sys.stdout.buffer.write(data)
                        sys.stdout.buffer.flush()
                    self._push_raw(self.decoder.decode(data))
                    buf += data
                    *lines, buf = buf.split(b"\n")
                    if lines:
                        self._push(l.strip(b"\r") for l in lines)
            except OSError:
                pass
            with self.lock:
                self.sock = None
            time.sleep(0.3)

    def _push_raw(self, text):
        if not text:
            return
        with self.lock:
            self.raw = (self.raw + text)[-self.KEEP_RAW:]
            for q in self.raw_listeners:
                q.put(text)

    def _push(self, lines):
        text = [l.decode(errors="replace") for l in lines if l.strip()]
        if not text:
            return
        with self.lock:
            self.lines = (self.lines + text)[-self.KEEP:]
            for q in self.listeners:
                q.put(text)


def ppm_to_png(data):
    """QEMU screendump PPM (P6) -> PNG bytes. Returns (png, digest)."""
    # "P6", then width, height and maxval separated by whitespace (with
    # optional comments), then exactly one whitespace byte before the
    # pixels - which may themselves start with whitespace-valued bytes.
    m = re.match(rb"P6(?:\s+|#[^\n]*\n)+(\d+)(?:\s+|#[^\n]*\n)+(\d+)"
                 rb"(?:\s+|#[^\n]*\n)+(\d+)\s", data)
    if not m:
        raise ValueError("not a P6 PPM")
    w, h = int(m.group(1)), int(m.group(2))
    raw = data[m.end():m.end() + w * h * 3]

    def chunk(tag, payload):
        c = struct.pack(">I", len(payload)) + tag + payload
        return c + struct.pack(">I", zlib.crc32(tag + payload) & 0xffffffff)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    stride = w * 3
    scanlines = b"".join(b"\x00" + raw[y:y + stride] for y in range(0, len(raw), stride))
    idat = zlib.compress(scanlines, 4)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) \
        + chunk(b"IEND", b"")
    return png, hashlib.sha1(png).hexdigest()[:16]


class Latest:
    """A viewer's mailbox: the newest frame and the newest fps, nothing
    older. A viewer slower than the screen skips frames instead of falling
    behind."""

    def __init__(self):
        self.cond = threading.Condition()
        self.frame = None
        self.fps = None

    def put(self, frame=None, fps=None):
        with self.cond:
            if frame is not None:
                self.frame = frame
            if fps is not None:
                self.fps = fps
            self.cond.notify()

    def get(self, timeout):
        """Returns (frame, fps), each None if unchanged; (None, None) on
        timeout."""
        with self.cond:
            if self.frame is None and self.fps is None:
                self.cond.wait(timeout)
            frame, fps, self.frame, self.fps = self.frame, self.fps, None, None
            return frame, fps


class Screen:
    """QMP screendump polled faster than the panel refreshes (60 Hz), so
    every frame the guest shows is caught; each new one is pushed to the
    viewers, with how many new frames the guest showed in the last second."""

    def __init__(self, qmp, tmpfile, period=0.01):
        self.qmp = qmp
        self.tmp = tmpfile          # a path QEMU and this process can see
        self.period = period
        self.lock = threading.Lock()
        self.listeners = []
        self.frame = None            # base64 png
        self.digest = None
        self.shown = collections.deque()    # times of new frames, last 1 s

    def subscribe(self):
        q = Latest()
        with self.lock:
            self.listeners.append(q)
            backlog = self.frame
        return q, backlog

    def unsubscribe(self, q):
        with self.lock:
            self.listeners.remove(q)

    def grab(self):
        r = self.qmp.execute("screendump",
                             {"filename": self.tmp, "format": "ppm"})
        if "error" in r:
            return
        try:
            with open(self.tmp, "rb") as f:
                data = f.read()
        except OSError:
            return
        # Only a new frame is worth encoding.
        digest = hashlib.sha1(data).digest()
        if digest == self.digest:
            return
        try:
            png, _ = ppm_to_png(data)
        except (ValueError, struct.error, IndexError):
            return                  # caught mid-write: the next poll has it
        frame = base64.b64encode(png).decode("ascii")
        with self.lock:
            self.digest = digest
            self.frame = frame
            self.shown.append(time.monotonic())
            for q in self.listeners:
                q.put(frame=frame)

    def fps(self):
        now = time.monotonic()
        with self.lock:
            while self.shown and self.shown[0] < now - 1:
                self.shown.popleft()
            n = len(self.shown)
            for q in self.listeners:
                q.put(fps=n)

    def run(self):
        last = time.monotonic()
        while True:
            time.sleep(self.period)
            if self.qmp:
                self.grab()
            if time.monotonic() - last >= 1:
                last = time.monotonic()
                self.fps()


class Qmp:
    """Just enough QMP for screendump and qom-get/qom-set on /machine."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.sock = None
        self.buf = b""

    def _line(self):
        while b"\n" not in self.buf:
            data = self.sock.recv(65536)
            if not data:
                raise OSError("QMP closed")
            self.buf += data
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line)

    def _call(self, command, arguments=None):
        msg = {"execute": command}
        if arguments:
            msg["arguments"] = arguments
        self.sock.sendall(json.dumps(msg).encode() + b"\n")
        while True:
            reply = self._line()
            if "return" in reply or "error" in reply:  # not an event
                return reply

    def execute(self, command, arguments=None):
        with self.lock:
            for attempt in (0, 1):
                try:
                    if not self.sock:
                        self.sock = dial(self.path)
                        self.buf = b""
                        self._line()                    # the greeting
                        self._call("qmp_capabilities")
                    return self._call(command, arguments)
                except (OSError, ValueError):
                    if self.sock:
                        self.sock.close()
                    self.sock = None
            return {"error": {"desc": "QEMU is not reachable"}}

    def wheel_position(self):
        r = self.execute("qom-get", {"path": "/machine",
                                     "property": "wheel-position"})
        if "return" in r:
            return int(r["return"])
        return None

    def turn_wheel(self, delta):
        pos = self.wheel_position()
        if pos is None:
            return {"error": {"desc": "the wheel is not reachable"}}
        r = self.execute("qom-set", {"path": "/machine",
                                     "property": "wheel-position",
                                     "value": pos + int(delta)})
        return r if "error" in r else {"return": pos + int(delta)}


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        # HTTPServer's own server_bind resolves this host's FQDN, which can
        # stall for tens of seconds on slow reverse DNS. Nothing uses it.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def make_handler(board, log, screen, qmp, pins, name):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        def send_body(self, code, ctype, body):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass                    # the page gave up on it (a poll)

        def start_events(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True

        def do_GET(self):
            if self.path == "/":
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    self.send_body(200, "text/html; charset=utf-8", f.read())
            elif self.path == "/config":
                body = json.dumps({"pins": pins, "name": name}).encode()
                self.send_body(200, "application/json", body)
            elif self.path == "/wheel":
                pos = qmp.wheel_position() if qmp else None
                self.send_body(200, "application/json",
                               json.dumps({"position": pos}).encode())
            elif self.path == "/events":
                self.events()
            elif self.path == "/screen":
                self.screen_stream()
            elif self.path == "/logs":
                self.logs()
            elif self.path == "/console/raw":
                self.console_raw()
            elif self.path.startswith("/vendor/"):
                self.vendor(self.path[len("/vendor/"):])
            elif self.path.startswith("/font/"):
                self.font(self.path[len("/font/"):])
            else:
                self.send_body(404, "text/plain", b"not found\n")

        def do_POST(self):
            # The press and the release are separate so a hold lasts as
            # long as the pointer is down; the firmware times any holds
            # off the line itself.
            if self.path == "/button/press" and "button" in pins:
                board.drive(pins["button"], 1)
                self.send_body(204, "text/plain", b"")
            elif self.path == "/button/release" and "button" in pins:
                board.drive(pins["button"], 0)
                self.send_body(204, "text/plain", b"")
            elif self.path == "/wheel/turn":
                if not qmp:
                    self.send_body(503, "text/plain", b"no QMP\n")
                    return
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    want = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    want = None
                if not isinstance(want, dict) or \
                        not isinstance(want.get("delta"), int):
                    self.send_body(400, "text/plain", b"bad delta\n")
                    return
                r = qmp.turn_wheel(want["delta"])
                body = json.dumps({"position": r.get("return"),
                                   "error": r.get("error", {}).get("desc")})
                self.send_body(200 if "return" in r else 500,
                               "application/json", body.encode())
            elif self.path == "/console/send":
                # A line typed on the page, into the guest's console.
                if not log:
                    self.send_body(503, "text/plain", b"no console\n")
                    return
                n = int(self.headers.get("Content-Length") or 0)
                log.send(self.rfile.read(n).decode(errors="replace") + "\r")
                self.send_body(204, "text/plain", b"")
            elif self.path == "/console/input":
                # Keystrokes from the page's terminal, sent as typed.
                if not log:
                    self.send_body(503, "text/plain", b"no console\n")
                    return
                n = int(self.headers.get("Content-Length") or 0)
                log.send(self.rfile.read(n).decode(errors="replace"))
                self.send_body(204, "text/plain", b"")
            else:
                self.send_body(404, "text/plain", b"not found\n")

        def events(self):
            self.start_events()
            q = board.subscribe()
            try:
                state = board.snapshot()
                while True:
                    if state is not None:
                        self.wfile.write(f"data: {json.dumps(state)}\n\n".encode())
                    self.wfile.flush()
                    try:
                        item = q.get(timeout=15)
                    except queue.Empty:
                        item = board.snapshot()     # keep-alive
                    # Coalesce a burst of states into the latest; tones all go.
                    state = None
                    while True:
                        if "tone" in item:
                            self.wfile.write(("event: tone\ndata: " +
                                              json.dumps(item["tone"]) +
                                              "\n\n").encode())
                        else:
                            state = item
                        if q.empty():
                            break
                        item = q.get_nowait()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                board.unsubscribe(q)

        def screen_stream(self):
            self.start_events()
            q, backlog = screen.subscribe()
            try:
                if backlog:
                    self.wfile.write(f"data: {backlog}\n\n".encode())
                    self.wfile.flush()
                while True:
                    frame, fps = q.get(timeout=15)
                    if frame is None and fps is None:
                        self.wfile.write(b": keep-alive\n\n")
                    if fps is not None:
                        self.wfile.write(f"event: fps\ndata: {fps}\n\n".encode())
                    if frame:
                        self.wfile.write(f"data: {frame}\n\n".encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                screen.unsubscribe(q)

        def vendor(self, name):
            """Third-party page assets kept next to index.html (xterm.js)."""
            types = {".js": "text/javascript", ".css": "text/css"}
            ext = os.path.splitext(name)[1]
            path = os.path.join(HERE, "vendor", name)
            if "/" in name or ext not in types or not os.path.isfile(path):
                self.send_body(404, "text/plain", b"not found\n")
                return
            with open(path, "rb") as f:
                self.send_body(200, types[ext] + "; charset=utf-8", f.read())

        def font(self, name):
            """A webfont kept next to the page."""
            path = os.path.join(FONTS, name)
            if "/" in name or not name.endswith(".ttf") or not os.path.isfile(path):
                self.send_body(404, "text/plain", b"not found\n")
                return
            with open(path, "rb") as f:
                self.send_body(200, "font/ttf", f.read())

        def console_raw(self):
            """The console as the terminal sees it: JSON text chunks, SSE."""
            if not log:
                self.send_body(503, "text/plain", b"no console\n")
                return
            self.start_events()
            q, backlog = log.subscribe_raw()
            try:
                chunk = backlog
                while True:
                    if chunk:
                        self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                        self.wfile.flush()
                    try:
                        chunk = q.get(timeout=15)
                        while not q.empty():        # coalesce a burst
                            chunk += q.get_nowait()
                    except queue.Empty:
                        self.wfile.write(b": keep-alive\n\n")
                        chunk = ""
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                log.unsubscribe_raw(q)

        def logs(self):
            """The device's console, as Server-Sent Events, backlog first."""
            if not log:
                self.send_body(503, "text/plain", b"no console log\n")
                return
            self.start_events()
            q, backlog = log.subscribe()
            try:
                lines = backlog
                while True:
                    for line in lines:
                        self.wfile.write(f"data: {line}\n\n".encode())
                    self.wfile.flush()
                    try:
                        lines = q.get(timeout=15)
                    except queue.Empty:
                        self.wfile.write(b": keep-alive\n\n")
                        lines = []
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                log.unsubscribe(q)

    return Handler


# The terminal's settings before Keyboard made it raw, to put back on exit.
saved_tty = None


def restore_tty():
    global saved_tty
    if saved_tty is not None:
        termios.tcsetattr(0, termios.TCSADRAIN, saved_tty)
        saved_tty = None


class Keyboard:
    """
    run.sh's terminal as a second keyboard on the console, beside the
    page's: raw, so every key (Ctrl-C included) goes to the guest, whose
    echo comes back to both. Ctrl-] quits QEMU, as does Ctrl-A X as under
    -nographic; Ctrl-A Ctrl-A sends a Ctrl-A.
    """

    def __init__(self, log, qmp, parent):
        self.log = log
        self.qmp = qmp
        self.parent = parent

    def run(self):
        global saved_tty
        saved_tty = termios.tcgetattr(0)
        raw = termios.tcgetattr(0)
        raw[0] &= ~(termios.BRKINT | termios.ICRNL | termios.INPCK |
                    termios.ISTRIP | termios.IXON)
        raw[3] &= ~(termios.ECHO | termios.ICANON | termios.IEXTEN |
                    termios.ISIG)
        raw[6][termios.VMIN], raw[6][termios.VTIME] = 1, 0
        termios.tcsetattr(0, termios.TCSADRAIN, raw)   # output as it was
        escape = False
        while True:
            data = os.read(0, 1024)
            if not data:
                return
            out = bytearray()
            for b in data:
                if escape:
                    escape = False
                    if b in (ord("x"), ord("X")):
                        self.quit()
                        return
                    if b != 0x01:
                        continue
                elif b == 0x01:
                    escape = True
                    continue
                elif b == 0x1d:                         # Ctrl-]
                    self.quit()
                    return
                out.append(b)
            if out:
                self.log.send(bytes(out))

    def quit(self):
        restore_tty()
        sys.stderr.write("\r\nQEMU: quit\r\n")
        if self.qmp:
            # QEMU may be gone before its answer makes it out: no answer
            # proves nothing, so watch for it to go instead.
            self.qmp.execute("quit")
        if not self.parent:
            return
        for _ in range(50):
            try:
                os.kill(self.parent, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)
        os.kill(self.parent, signal.SIGTERM)    # docker run passes it on


def leave(cleanup):
    try:
        restore_tty()
    except (termios.error, OSError):
        pass                    # the terminal went first; leave all the same
    if cleanup:
        shutil.rmtree(cleanup, ignore_errors=True)
    os._exit(0)


def follow_parent(pid, cleanup):
    """Go when QEMU goes: run.sh execs it, so it keeps run.sh's PID. Watched
    by reparenting, not kill(pid, 0): a dead QEMU stays a zombie (and its
    PID can be reused) until whoever started run.sh reaps it."""
    while os.getppid() == pid:
        time.sleep(0.2)
    leave(cleanup)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--board", required=True,
                    help="flintstone.board chardev (tcp:HOST:PORT or path)")
    ap.add_argument("--qmp", help="QEMU's QMP (tcp:HOST:PORT or path)")
    ap.add_argument("--console", help="the guest's serial (tcp:HOST:PORT or path)")
    ap.add_argument("--echo-console", action="store_true",
                    help="also print the console on stdout (run.sh's terminal)")
    ap.add_argument("--keyboard", action="store_true",
                    help="type into the console from stdin, a terminal "
                         "(Ctrl-] or Ctrl-A X quits QEMU)")
    ap.add_argument("--screen-file",
                    help="scratch PPM path QEMU screendump writes into "
                         "(must be shared with QEMU)")
    ap.add_argument("--port", type=int, default=8075, help="port to serve on")
    ap.add_argument("--pins", help="name=LINE,... from the board's wiring")
    ap.add_argument("--name", default="Nest",
                    help="the unit, as the page titles it (e.g. 'Nest gen 3')")
    ap.add_argument("--screen-period", type=float, default=0.01,
                    help="seconds between screendumps")
    ap.add_argument("--parent", type=int,
                    help="exit when this process does (QEMU, via run.sh)")
    ap.add_argument("--cleanup", help="directory to remove on exit")
    args = ap.parse_args()

    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: leave(args.cleanup))
    if args.parent:
        threading.Thread(target=follow_parent,
                         args=(args.parent, args.cleanup), daemon=True).start()

    board = Board(args.board)
    threading.Thread(target=board.run, daemon=True).start()

    qmp = Qmp(args.qmp) if args.qmp else None
    screen = Screen(qmp, args.screen_file or
                    os.path.join(os.environ.get("TMPDIR", "/tmp"),
                                 "flintstone-screen.ppm"),
                    args.screen_period)
    threading.Thread(target=screen.run, daemon=True).start()

    log = ConsoleLog(args.console, args.echo_console) if args.console else None
    if log:
        threading.Thread(target=log.run, daemon=True).start()

    # Bound before the terminal goes raw: a failed bind must not leave it so.
    server = Server(("127.0.0.1", args.port),
                    make_handler(board, log, screen, qmp, parse_pins(args.pins),
                                 args.name))
    if log and args.keyboard and os.isatty(0):
        threading.Thread(target=Keyboard(log, qmp, args.parent).run,
                         daemon=True).start()
    if not args.parent:     # run.sh prints it before QEMU takes the tty
        print(f"Flintstone UI: http://127.0.0.1:{args.port}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
