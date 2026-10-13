#!/usr/bin/env python3
"""
An MCP server (stdio) that lets an agent drive the emulated Nest: look at
the screen (a frame, a burst of frames, a recording), press the button,
turn the wheel, play input sequences, use the console and move files in
and out of the guest.

Everything goes through ui.py's HTTP API, so the unit must be up with
./run.sh --ui. QEMU serves each chardev to one client and ui.py holds
them; this process holds no QEMU socket of its own.

Standard library only (recordings are encoded with ffmpeg when it is on
PATH, and kept as PNG frames when it is not):

    mcp_server.py [--url http://127.0.0.1:8075]

Without --url (or NEST_UI_URL) it talks to the newest unit running on
this host, and looks again when that one goes.

.mcp.json at the repo root registers it for Claude Code.
"""
import argparse
import base64
import bisect
import datetime
import hashlib
import http.client
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

import ui   # ui.py, beside this file: its PNG encoder

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
RECORDINGS = os.path.join(REPO, ".qemu", "recordings")
PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")

base_url = None             # --url / NEST_UI_URL; None finds the unit
found_url = None            # the unit found, until it stops answering


class ToolError(Exception):
    """A failure the agent should read, returned as an error result."""


# ---------------------------------------------------------------- HTTP

def running_units():
    """The pages of the units running on this host, newest first. run.sh
    --ui gives each ui.py the first free port from 8075 (or --ui-port),
    so the port is read off its command line."""
    try:
        ps = subprocess.run(["ps", "-ax", "-o", "etime=,args="], capture_output=True,
                            text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    units = []
    for line in ps.splitlines():
        etime, _, args = line.strip().partition(" ")
        if not re.search(r"/ui\.py(\s|$)", args):
            continue
        m = re.search(r"--port[= ](\d+)", args)
        # etime is [[dd-]hh:]mm:ss
        days, _, hms = etime.rpartition("-")
        age = int(days or 0) * 86400
        for part in hms.split(":"):
            age = age * 60 + int(part) if part.isdigit() else age
        units.append((age, f"http://127.0.0.1:{m.group(1) if m else 8075}"))
    return [url for _, url in sorted(units)]


def unit_url(fresh=False):
    """The page to talk to: the one given, else the newest unit running."""
    global found_url
    if base_url:
        return base_url
    if fresh or not found_url:
        units = running_units()
        found_url = units[0] if units else None
    if not found_url:
        raise ToolError("no unit running: start one with ./run.sh --ui "
                        "(or pass --url / set NEST_UI_URL)")
    return found_url


def request(method, path, body=None, timeout=10):
    """One request to ui.py: returns (status, content-type, bytes)."""
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
    for fresh in (False, True):
        url = unit_url(fresh)
        req = urllib.request.Request(url + path, data=data, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.headers.get("Content-Type", ""), r.read()
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace").strip()
            if e.code == 404:
                msg += (f" (the unit at {url} runs a ui.py older than this "
                        "server: restart it)")
            raise ToolError(f"{method} {path}: HTTP {e.code} {msg}") from None
        except (urllib.error.URLError, OSError) as e:
            # Never reached a page: the unit found may have gone and
            # another come up, so look again (only when none was given).
            if base_url or fresh:
                raise ToolError(f"no UI at {url} ({e}): start the unit with "
                                "./run.sh --ui, or pass --url / set NEST_UI_URL") from None


def get_json(path, timeout=10):
    return json.loads(request("GET", path, timeout=timeout)[2])


def post(path, body=None, timeout=10):
    return request("POST", path, body if body is not None else b"", timeout)[2]


# ---------------------------------------------------------------- frames

def png_size(png):
    return struct.unpack(">II", png[16:24])


def upright(png):
    """
    The frame as the unit shows it. The gen 3 scans out a 480x640 portrait
    framebuffer that the panel shows turned a quarter anticlockwise (the
    page draws it with rotate(-90)), of which the round glass shows the
    top 480 rows: the result is that 480x480 square. Other sizes are
    shown as they are.
    Decodes only ui.py's own PNGs (8-bit RGB, filter 0); anything else is
    returned untouched.
    """
    w, h = png_size(png)
    if (w, h) != (480, 640):
        return png
    pos, idat = 8, b""
    while pos < len(png):
        n, tag = struct.unpack(">I4s", png[pos:pos + 8])
        if tag == b"IHDR" and png[pos + 16:pos + 18] != b"\x08\x02":
            return png
        if tag == b"IDAT":
            idat += png[pos + 8:pos + 8 + n]
        pos += 12 + n
    raw = zlib.decompress(idat)
    stride = 1 + 3 * w
    if len(raw) != stride * h or any(raw[::stride]):
        return png
    h = w                                   # the rows under the glass
    flat = b"".join(raw[y * stride + 1:(y + 1) * stride] for y in range(h))
    # Turned anticlockwise, row i of the result is column w-1-i, top down.
    out = bytearray(3 * w * h)
    for i, c in enumerate(range(w - 1, -1, -1)):
        base = i * 3 * h
        for k in range(3):
            out[base + k:base + 3 * h:3] = flat[3 * c + k::3 * w]
    return ui.ppm_to_png(b"P6\n%d %d\n255\n" % (h, w) + bytes(out))[0]


def grab():
    """The newest frame, upright, as PNG bytes."""
    return upright(request("GET", "/screen.png")[2])


def image(png):
    return {"type": "image", "data": base64.b64encode(png).decode("ascii"),
            "mimeType": "image/png"}


def text(s):
    return {"type": "text", "text": s if isinstance(s, str) else
            json.dumps(s, indent=1)}


def save(png, path):
    path = os.path.abspath(os.path.expanduser(path))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(png)
    return path


def settle(quiet_ms, timeout_s):
    """Wait until the screen has not changed for quiet_ms; returns the
    frame and whether it settled before timeout_s."""
    frame = grab()
    deadline = time.monotonic() + timeout_s
    stable_since = time.monotonic()
    while time.monotonic() < deadline:
        if (time.monotonic() - stable_since) * 1000 >= quiet_ms:
            return frame, True
        time.sleep(0.05)
        now = grab()
        if now != frame:
            frame, stable_since = now, time.monotonic()
    return frame, False


class Recorder:
    """
    Follows ui.py's /screen stream (a frame each time the screen changes)
    into PNG files on disk with their arrival times, then encodes them
    into a constant-rate video, holding each frame until the next.
    """

    def __init__(self, path, fps, max_s):
        self.path = path
        self.fps = fps
        self.max_s = max_s
        self.dir = tempfile.mkdtemp(prefix="frames-", dir=os.path.dirname(path))
        self.frames = []            # (seconds from start, file)
        self.times = []             # the seconds alone, for bisect
        self.cache = (None, None)   # (index, frame) shown_at last read
        self.error = None
        self.started = time.monotonic()
        self.stopped = None
        self.conn = None
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        u = urllib.parse.urlsplit(unit_url())
        try:
            self.conn = http.client.HTTPConnection(u.hostname, u.port or 80,
                                                   timeout=30)
            self.conn.request("GET", "/screen")
            resp = self.conn.getresponse()
            event, data = "message", []
            while self.stopped is None:
                if time.monotonic() - self.started > self.max_s:
                    self.stopped = time.monotonic()
                    break
                line = resp.readline()
                if not line:
                    break
                line = line.rstrip(b"\r\n")
                if line.startswith(b"event:"):
                    event = line[6:].strip().decode()
                elif line.startswith(b"data:"):
                    data.append(line[5:].strip())
                elif not line:
                    if event == "message" and data:
                        self.add(base64.b64decode(b"".join(data)))
                    event, data = "message", []
        except (OSError, http.client.HTTPException, ValueError) as e:
            if self.stopped is None:
                self.error = str(e)

    def add(self, png):
        name = os.path.join(self.dir, f"{len(self.frames):06d}.png")
        with open(name, "wb") as f:
            f.write(png)
        t = time.monotonic() - self.started
        self.frames.append((t, name))
        self.times.append(t)

    def stop(self):
        if self.stopped is None:
            self.stopped = time.monotonic()
        if self.conn and self.conn.sock:
            try:
                self.conn.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.thread.join(5)
        return self.stopped - self.started

    def encode(self, duration):
        """The video at self.path, or None (no ffmpeg, or it failed: the
        frames stay in self.dir)."""
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg or not self.frames:
            return None
        with open(self.frames[0][1], "rb") as f:
            turn = png_size(f.read(24)) == (480, 640)
        vf = ("crop=480:480:0:0,transpose=2," if turn else "") + "format=yuv420p"
        p = subprocess.Popen([ffmpeg, "-y", "-loglevel", "error", "-f", "image2pipe",
                              "-framerate", str(self.fps), "-i", "-", "-vf", vf,
                              "-movflags", "+faststart", self.path],
                             stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        # Each tick of the output shows the frame on screen at that time.
        try:
            for tick in range(max(round(duration * self.fps), 1)):
                p.stdin.write(self.shown_at(tick / self.fps)[1])
            p.stdin.close()
        except BrokenPipeError:
            pass
        err = p.stderr.read().decode(errors="replace")
        if p.wait():
            self.error = "ffmpeg: " + err.strip()[-500:]
            return None
        return self.path

    def shown_at(self, t):
        """(time, PNG bytes) of the frame on screen t s in."""
        i = max(bisect.bisect_right(self.times, t) - 1, 0)
        if self.cache[0] != i:
            with open(self.frames[i][1], "rb") as f:
                self.cache = (i, (self.frames[i][0], f.read()))
        return self.cache[1]

    def keyframes(self, n, duration):
        """What the screen showed at n times spread evenly over the
        recording, upright; a frame shown at several of them comes once."""
        out = []
        for i in range(n if self.frames else 0):
            t = duration * i / max(n - 1, 1)
            png = self.shown_at(t)[1]
            if not out or out[-1][1] != png:
                out.append((t, png))
        return [(t, upright(png)) for t, png in out]


recorder = None
recorder_lock = threading.Lock()


def record_start(fps=30, path=None, max_s=300):
    global recorder
    with recorder_lock:
        if recorder:
            raise ToolError("a recording is already running: record_stop first")
        request("GET", "/screen.png")         # fail now if there is no UI
        if not path:
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            path = os.path.join(RECORDINGS, f"{stamp}.mp4")
        path = os.path.abspath(os.path.expanduser(path))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        recorder = Recorder(path, fps, max_s)
        return path


def record_stop(keyframes=4, keep_frames=False):
    global recorder
    with recorder_lock:
        rec, recorder = recorder, None
    if not rec:
        raise ToolError("no recording is running")
    duration = rec.stop()
    video = rec.encode(duration)
    shots = rec.keyframes(keyframes, duration)
    info = {"duration_s": round(duration, 2), "frames": len(rec.frames),
            "video": video}
    if video and not keep_frames:
        shutil.rmtree(rec.dir, ignore_errors=True)
    else:
        info["frames_dir"] = rec.dir
    if rec.error:
        info["error"] = rec.error
    content = [text(info)]
    for t, png in shots:
        content += [text(f"t={t:.2f}s"), image(png)]
    return content


# ---------------------------------------------------------------- input

def press(hold_ms=100):
    post("/button/press")
    try:
        time.sleep(hold_ms / 1000)
    finally:
        post("/button/release")


def turn(detents):
    r = json.loads(post("/wheel/turn", {"delta": int(detents)}))
    return r.get("position")


# ---------------------------------------------------------------- console

ANSI = re.compile(r"\x1b(\[[0-9;?]*[ -/]*[@-~]|\][^\x07]*\x07|[()][0-9A-Za-z]|.)")
PROMPT = re.compile(r"[#$] $")
LOGIN = re.compile(r"login: $")
USER, PASSWORD = "root", "nest"         # the login the pre-boot sets up
console_lock = threading.RLock()
seq = [os.getpid() % 10000 * 1000]


CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")    # CR, and XON/XOFF


def clean(s):
    return CONTROL.sub("", ANSI.sub("", s))


def cread(since=None, wait=0.0):
    q = {"wait": wait} if since is None else {"since": since, "wait": wait}
    return get_json("/console/read?" + urllib.parse.urlencode(q), timeout=wait + 10)


def csend(data):
    post("/console/input", data.encode() if isinstance(data, str) else data)


def cursor():
    return cread()["end"]


def wait_for(pattern, since, timeout_s):
    """Read on from `since` until `pattern` matches the cleaned text.
    Returns (match or None, cleaned text, end offset)."""
    rx = re.compile(pattern) if isinstance(pattern, str) else pattern
    seen, end = "", since
    deadline = time.monotonic() + timeout_s
    while True:
        m = rx.search(seen)
        if m:
            return m, seen, end
        left = deadline - time.monotonic()
        if left <= 0:
            return None, seen, end
        r = cread(end, min(left, 5))
        seen += clean(r["text"])
        end = r["end"]


def tail(n=200):
    return clean(cread()["text"][-n:])


def ensure_shell(user, password, timeout_s=20):
    """Get the console to a shell prompt, logging in if it is at login:."""
    t = tail()
    if PROMPT.search(t):
        return
    if not LOGIN.search(t):
        c = cursor()
        csend("\r")
        m, t, _ = wait_for(r"(?:[#$]|login:) $", c, 3)
        if not m:
            raise ToolError("the console shows neither a shell prompt nor "
                            "login: (busy, or still booting). Last output:\n"
                            + tail(400))
        if PROMPT.search(t):
            return
    c = cursor()
    csend(user + "\r")
    m, _, c = wait_for(r"[Pp]assword: ?$", c, 5)
    if not m:
        raise ToolError("no password prompt after the user name")
    csend(password + "\r")
    m, t, _ = wait_for(r"(?:[#$]|login:) $", c, timeout_s)
    if not m or not PROMPT.search(t):
        raise ToolError("login failed. Last output:\n" + tail(400))


def shquote(s):
    return "'" + s.replace("'", "'\\''") + "'"


def execute(command, timeout_s=30, login=True, user=USER, password=PASSWORD):
    """
    Run one shell command line on the console and return what it printed
    and its exit status. The line goes in as
        echo <start>; command eval '<command>'; echo <end><status>
    with each marker computed by the shell ($((N+0))), so the echo of the
    typed line (and any wrapping of it) can never match one. `command`
    keeps a failing special builtin (": > /ro/file") from abandoning the
    rest of the line, end marker included.
    """
    with console_lock:
        if login:
            ensure_shell(user, password)
        seq[0] += 1
        n = seq[0]
        start = re.compile(rf":mcp:{n}:s:\n?")
        end = re.compile(rf":mcp:{n}:e:(\d+):")
        c = cursor()
        csend(f"echo :mcp:$(({n}+0)):s:; command eval {shquote(command)}; "
              f"echo :mcp:$(({n}+0)):e:$?:\r")
        m, out, c = wait_for(end, c, timeout_s)
        s = start.search(out)
        if not m:
            csend("\x03")
            wait_for(PROMPT, c, 3)
            return None, out[s.end():] if s else out, True
        if not PROMPT.search(out[m.end():]):
            wait_for(PROMPT, c, 2)
        return int(m.group(1)), out[s.end() if s else 0:m.start()], False


def md5_of(path):
    status, out, _ = execute(f"md5sum {path}")
    return out.split()[0] if status == 0 and out.split() else None


def guest_base64():
    """The guest's base64 coder: (encode FILE, decode SRC DST) command
    templates, or None. busybox may lack base64; the gen 3 has openssl."""
    # openssl's stderr goes too: it warns of a missing openssl.cnf, and the
    # console carries stderr in with the data.
    for tool, enc, dec in (
            ("base64", "base64 {}", "base64 -d {} > {}"),
            ("openssl", "openssl base64 -in {} 2>/dev/null",
             "openssl base64 -d -in {} -out {} 2>/dev/null")):
        if execute(f"command -v {tool} >/dev/null 2>&1")[0] == 0:
            return enc, dec
    return None


def pull(path, timeout_s=300):
    """A guest file's bytes over the console: base64, or od without it."""
    q = shquote(path)
    if execute(f"test -f {q} && test -r {q}")[0]:
        why = execute(f"ls -ld {q}")[1].strip()
        raise ToolError(f"cannot read {path}: not a readable regular file ({why})")
    coder = guest_base64()
    if coder:
        status, out, timed_out = execute(coder[0].format(q), timeout_s)
        decode = lambda s: base64.b64decode("".join(s.split()))
    else:
        status, out, timed_out = execute(f"od -An -v -tx1 {q}", timeout_s)
        decode = lambda s: bytes.fromhex("".join(s.split()))
    if timed_out:
        raise ToolError(f"reading {path} took over {timeout_s} s")
    if status:
        raise ToolError(f"cannot read {path}: {out.strip()}")
    data = decode(out)
    if md5_of(q) not in (None, hashlib.md5(data).hexdigest()):
        raise ToolError(f"{path} came over corrupted (md5 differs): try again")
    return data


def push(data, path, mode=None, timeout_s=600):
    """Write bytes to a guest file over the console, checked by md5."""
    q = shquote(path)
    tmp = shquote(path + ".mcp-part")
    with console_lock:
        coder = guest_base64()
        if coder:
            # Lines of 64 (openssl's decoder takes no longer), 32 at a
            # time, each batch read by its own head once it is running:
            # the tty's 4 KiB input buffer overflows if the guest is sent
            # more than it has read (it asks with XOFF, which QEMU's
            # serial does not heed). Echo off, so the lines do not come
            # back.
            b64 = base64.b64encode(data).decode("ascii")
            lines = [b64[i:i + 64] + "\r" for i in range(0, len(b64), 64)]
            deadline = time.monotonic() + timeout_s
            status, out, _ = execute(f"stty -echo; : > {tmp}.b64")
            try:
                for i in range(0, len(lines), 32):
                    if status:
                        break
                    batch = lines[i:i + 32]
                    seq[0] += 1
                    n = seq[0]
                    c = cursor()
                    csend(f"echo :mcp:$(({n}+0)):r:; head -n {len(batch)} >> {tmp}.b64; "
                          f"echo :mcp:$(({n}+0)):e:$?:\r")
                    m, _, c = wait_for(rf":mcp:{n}:r:", c, 10)
                    if m:
                        csend("".join(batch))
                        left = max(deadline - time.monotonic(), 1)
                        m, out, c = wait_for(rf":mcp:{n}:e:(\d+):", c, min(left, 30))
                    if not m:
                        csend("\x03")
                        wait_for(PROMPT, c, 3)
                        execute(f"rm -f {tmp}.b64")
                        raise ToolError(f"writing {path} stalled at byte {i * 48}")
                    status = int(m.group(1))
            finally:
                execute("stty echo", 5)
            if not status:
                status, out, _ = execute(coder[1].format(f"{tmp}.b64", tmp) +
                                         f" && rm -f {tmp}.b64", 60)
        else:
            # printf with octal escapes, a line at a time.
            status, out, _ = execute(f": > {tmp}")
            for i in range(0, len(data), 160):
                if status:
                    break
                chunk = "".join(f"\\{b:03o}" for b in data[i:i + 160])
                status, out, _ = execute(f"printf '{chunk}' >> {tmp}")
        if status:
            execute(f"rm -f {tmp} {tmp}.b64")
            raise ToolError(f"cannot write {path}: {out.strip()} "
                            "(a read-only root needs: mount -o remount,rw /)")
        if md5_of(tmp) not in (None, hashlib.md5(data).hexdigest()):
            execute(f"rm -f {tmp}")
            raise ToolError(f"{path} arrived corrupted (md5 differs): try again")
        chmod = f" && chmod {mode} {q}" if mode else ""
        status, out, _ = execute(f"mv -f {tmp} {q}{chmod}")
        if status:
            raise ToolError(f"cannot write {path}: {out.strip()}")


# ---------------------------------------------------------------- ssh

# The guest's sshd is old: it offers only ssh-rsa and ssh-dss host keys,
# and makes new ones whenever its state is fresh. It has no sftp-server,
# so files go as `cat` over a plain ssh session (scp would need one).
SSH_OPTS = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "HostKeyAlgorithms=+ssh-rsa", "-o", "PubkeyAcceptedAlgorithms=+ssh-rsa",
            "-o", "ConnectTimeout=5", "-o", "LogLevel=ERROR",
            "-o", "PreferredAuthentications=password,keyboard-interactive"]
askpass = []


def ssh_port():
    """The host port QEMU forwards to the guest's port 22 (run.sh --ssh),
    or None."""
    if not shutil.which("ssh"):
        return None
    r = json.loads(post("/qmp", {"execute": "human-monitor-command",
                                 "arguments": {"command-line": "info usernet"}}))
    # "TCP[HOST_FORWARD]  18   127.0.0.1  2291   10.0.2.15    22  0  0"
    m = re.search(r"HOST_FORWARD\]\s+\d+\s+\S+\s+(\d+)\s+\S+\s+22\s",
                  r.get("return", ""))
    return int(m.group(1)) if m else None


def ssh(port, command, stdin=b"", timeout_s=600):
    """Run a command on the guest as root over ssh; (status, stdout,
    stderr). 255 is ssh's own failure (no connection, refused login)."""
    if not askpass:
        fd, path = tempfile.mkstemp(prefix="nest-askpass-")
        os.write(fd, f"#!/bin/sh\necho {shquote(PASSWORD)}\n".encode())
        os.close(fd)
        os.chmod(path, 0o700)
        askpass.append(path)
    env = dict(os.environ, SSH_ASKPASS=askpass[0], SSH_ASKPASS_REQUIRE="force")
    host = urllib.parse.urlsplit(unit_url()).hostname
    try:
        r = subprocess.run(["ssh", "-p", str(port), *SSH_OPTS, f"{USER}@{host}", command],
                           input=stdin, capture_output=True, env=env, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        raise ToolError(f"ssh took over {timeout_s} s") from None
    return r.returncode, r.stdout, r.stderr.decode(errors="replace").strip()


def transfer(via, over_ssh, over_console):
    """over_ssh(port) where ssh works and `via` allows it, else
    over_console(); returns (result, how it went)."""
    if via not in ("auto", "ssh", "console"):
        raise ToolError("via is auto, ssh or console")
    if via != "console":
        port = ssh_port()
        if port is None and via == "ssh":
            raise ToolError("no ssh: start the unit with ./run.sh --ui --ssh PORT "
                            "(and have ssh on PATH)")
        if port is not None:
            try:
                return over_ssh(port), f"ssh (port {port})"
            except SshDown as e:
                if via == "ssh":
                    raise ToolError(f"ssh failed: {e}") from None
                return over_console(), f"console (ssh failed: {e})"
    return over_console(), "console"


class SshDown(Exception):
    """ssh itself failed (no answer yet, login refused): the console may
    still do."""


def pull_ssh(port, path, timeout_s):
    status, out, err = ssh(port, f"cat {shquote(path)}", timeout_s=timeout_s)
    if status == 255:
        raise SshDown(err)
    if status:
        raise ToolError(f"cannot read {path}: {err}")
    return out


def push_ssh(port, data, path, mode, timeout_s):
    q, tmp = shquote(path), shquote(path + ".mcp-part")
    chmod = f"chmod {mode} {tmp} && " if mode else ""
    status, _, err = ssh(port, f"cat > {tmp} && {chmod}mv -f {tmp} {q} || "
                         f"{{ rm -f {tmp}; exit 1; }}", data, timeout_s)
    if status == 255:
        raise SshDown(err)
    if status:
        raise ToolError(f"cannot write {path}: {err} "
                        "(a read-only root needs: mount -o remount,rw /)")


# ---------------------------------------------------------------- tools

def t_status(a):
    state = get_json("/state")
    named = {fn: state["pins"].get(line) for fn, line in state["wiring"].items()}
    out = {"qemu_running": state.get("linked"), "unit": state.get("name"),
           "page": unit_url(),
           "other_units": [u for u in running_units() if u != unit_url()],
           "wheel_position": state.get("wheel"),
           "lines (0..1000, >=500 is high)": named}
    try:
        out["console_tail"] = tail(300)
    except ToolError as e:
        out["console_tail"] = str(e)
    return [text(out)]


def t_screenshot(a):
    if a.get("settle_ms"):
        png, settled = settle(a["settle_ms"], a.get("timeout_s", 10))
        notes = [] if settled else ["the screen did not settle before timeout_s"]
    else:
        png, notes = grab(), []
    w, h = png_size(png)
    notes.append(f"{w}x{h}")
    if a.get("save_to"):
        notes.append("saved " + save(png, a["save_to"]))
    return [image(png), text(", ".join(notes))]


def t_capture_frames(a):
    count = min(max(int(a.get("count", 6)), 1), 32)
    interval = max(int(a.get("interval_ms", 500)), 0) / 1000
    out, last, t0 = [], None, time.monotonic()
    for i in range(count):
        if i:
            time.sleep(max(t0 + i * interval - time.monotonic(), 0))
        png = grab()
        t = time.monotonic() - t0
        if a.get("changes_only") and png == last:
            continue
        last = png
        label = f"frame {i} t={t:.2f}s"
        if a.get("save_dir"):
            label += " saved " + save(png, os.path.join(a["save_dir"], f"frame-{i:03d}.png"))
        out += [text(label), image(png)]
    return out


def t_press_button(a):
    press(int(a.get("hold_ms", 100)))
    return [text(f"pressed for {int(a.get('hold_ms', 100))} ms")]


def t_turn_wheel(a):
    pos = turn(a["detents"])
    return [text(f"wheel at {pos}")]


def t_input_sequence(a):
    steps = a.get("steps") or []
    out, log = [], []
    rec_path = None
    if a.get("record"):
        rec_path = record_start(a.get("fps", 30))
    try:
        for i, st in enumerate(steps):
            if "press" in st:
                press(int(st["press"]))
                log.append(f"{i}: press {st['press']} ms")
            elif "button" in st:
                post("/button/press" if st["button"] == "down" else "/button/release")
                log.append(f"{i}: button {st['button']}")
            elif "turn" in st:
                log.append(f"{i}: turn {st['turn']} -> {turn(st['turn'])}")
            elif "wait" in st:
                time.sleep(int(st["wait"]) / 1000)
                log.append(f"{i}: wait {st['wait']} ms")
            elif "type" in st:
                csend(st["type"])
                log.append(f"{i}: typed {st['type']!r}")
            elif "screenshot" in st:
                out += [text(f"step {i}"), image(grab())]
                log.append(f"{i}: screenshot")
            else:
                raise ToolError(f"step {i}: unknown step {st}")
            if a.get("step_delay_ms"):
                time.sleep(int(a["step_delay_ms"]) / 1000)
        if a.get("screenshot_after", True):
            time.sleep(int(a.get("after_ms", 300)) / 1000)
            out += [text("after the sequence"), image(grab())]
    finally:
        if rec_path:
            out = record_stop(a.get("keyframes", 4)) + out
    return [text("\n".join(log))] + out


def t_record_start(a):
    path = record_start(int(a.get("fps", 30)), a.get("path"),
                        int(a.get("max_s", 300)))
    return [text(f"recording to {path}: call record_stop to finish "
                 f"(stops on its own after {int(a.get('max_s', 300))} s)")]


def t_record_stop(a):
    return record_stop(int(a.get("keyframes", 4)), a.get("keep_frames", False))


def t_console_exec(a):
    status, out, timed_out = execute(a["command"], float(a.get("timeout_s", 30)),
                                     a.get("login", True))
    limit = int(a.get("max_chars", 20000))
    note = ""
    if len(out) > limit:
        note = f"\n[... {len(out) - limit} more characters cut: redirect to a file and pull_file it]"
        out = out[-limit:]
    if timed_out:
        return [text(f"timed out after {a.get('timeout_s', 30)} s; sent Ctrl-C. "
                     f"Output so far:\n{out}{note}")]
    return [text(f"exit {status}\n{out}{note}")]


def t_console_send(a):
    data = a.get("text", "")
    if a.get("ctrl"):
        data += chr(ord(a["ctrl"].lower()[0]) & 0x1f)
    if a.get("enter"):
        data += "\r"
    c = cursor()
    csend(data)
    return [text(f"sent {data!r}; cursor before it: {c} (pass it to "
                 "console_read or console_wait as since)")]


def t_console_read(a):
    since = a.get("since")
    r = cread(since, min(float(a.get("wait_s", 0)), 30))
    s = clean(r["text"])
    limit = int(a.get("max_chars", 8000))
    if len(s) > limit:
        s = s[-limit:]
    note = ""
    if since is not None and r["start"] > since:
        note = f"(text between {since} and {r['start']} scrolled out of the buffer)\n"
    return [text(f"cursor {r['end']}\n{note}{s}")]


def t_console_wait(a):
    since = a.get("since")
    if since is None:
        since = cursor()
    m, seen, end = wait_for(a["pattern"], since, float(a.get("timeout_s", 30)))
    if not m:
        return [text(f"no match within {a.get('timeout_s', 30)} s; cursor {end}\n"
                     + seen[-2000:])]
    lo = seen.rfind("\n", 0, max(m.start() - 1, 0))
    return [text(f"matched {m.group(0)!r}; cursor {end}\n"
                 + seen[max(lo, m.start() - 1000, 0):m.end() + 500])]


def t_pull_file(a):
    timeout_s = float(a.get("timeout_s", 300))
    data, how = transfer(a.get("via", "auto"),
                         lambda port: pull_ssh(port, a["path"], timeout_s),
                         lambda: pull(a["path"], timeout_s))
    if a.get("local_path"):
        return [text(f"{len(data)} bytes saved to {save(data, a['local_path'])} "
                     f"via {how}")]
    try:
        s = data.decode()
    except UnicodeDecodeError:
        raise ToolError(f"{a['path']} is binary ({len(data)} bytes): "
                        "give local_path to save it") from None
    return [text(s)]


def t_push_file(a):
    if "local_path" in a:
        with open(os.path.expanduser(a["local_path"]), "rb") as f:
            data = f.read()
    elif "content" in a:
        data = a["content"].encode()
    else:
        raise ToolError("give content or local_path")
    timeout_s = float(a.get("timeout_s", 600))
    _, how = transfer(a.get("via", "auto"),
                      lambda port: push_ssh(port, data, a["path"], a.get("mode"), timeout_s),
                      lambda: push(data, a["path"], a.get("mode"), timeout_s))
    return [text(f"wrote {len(data)} bytes to {a['path']} via {how}")]


def t_qmp(a):
    body = {"execute": a["command"]}
    if a.get("arguments"):
        body["arguments"] = a["arguments"]
    return [text(json.loads(post("/qmp", body, timeout=60)))]


def schema(props, required=()):
    return {"type": "object", "properties": props, "required": list(required)}


INT, NUM, STR, BOOL = ({"type": t} for t in ("integer", "number", "string", "boolean"))


def d(base, desc):
    return dict(base, description=desc)


TOOLS = {
    "status": (t_status, "Is QEMU up, the board lines (button, poweroff, ...), the wheel position and the console's last lines.", schema({})),
    "screenshot": (t_screenshot, "The unit's screen now, upright as the device shows it.",
                   schema({"settle_ms": d(INT, "first wait until the screen has not changed for this long (an animation ending)"),
                           "timeout_s": d(NUM, "longest to wait for settle_ms (default 10)"),
                           "save_to": d(STR, "also save the PNG to this path")})),
    "capture_frames": (t_capture_frames, "Several screenshots at a fixed interval, returned together: for animations and transitions.",
                       schema({"count": d(INT, "frames, 1..32 (default 6)"),
                               "interval_ms": d(INT, "between frames (default 500)"),
                               "changes_only": d(BOOL, "skip frames identical to the previous one"),
                               "save_dir": d(STR, "also save frame-NNN.png files here")})),
    "press_button": (t_press_button, "Press the front button (the display face) and release it after hold_ms.",
                     schema({"hold_ms": d(INT, "how long to hold (default 100; a long press is a few seconds)")})),
    "turn_wheel": (t_turn_wheel, "Turn the ring by a number of detents (positive and negative turn opposite ways); returns the new position.",
                   schema({"detents": INT}, ["detents"])),
    "input_sequence": (t_input_sequence,
                       "Play a list of inputs in order, optionally recorded, with screenshots where asked and one at the end. "
                       "Steps: {\"press\": ms}, {\"button\": \"down\"|\"up\"}, {\"turn\": detents}, {\"wait\": ms}, "
                       "{\"type\": \"console keystrokes\"}, {\"screenshot\": true}.",
                       schema({"steps": {"type": "array", "items": {"type": "object"}},
                               "step_delay_ms": d(INT, "pause after every step (default 0)"),
                               "screenshot_after": d(BOOL, "screenshot after the last step (default true)"),
                               "after_ms": d(INT, "wait before that screenshot (default 300)"),
                               "record": d(BOOL, "record a video of the sequence (record_stop's result is included)"),
                               "fps": d(INT, "video frame rate when recording (default 30)"),
                               "keyframes": d(INT, "frames of the recording to return as images (default 4)")},
                              ["steps"])),
    "record_start": (t_record_start, "Start recording the screen to an MP4 (every frame the unit shows). Other tools keep working meanwhile; record_stop finishes it.",
                     schema({"fps": d(INT, "output frame rate (default 30)"),
                             "path": d(STR, "output file (default .qemu/recordings/<time>.mp4)"),
                             "max_s": d(INT, "stop on its own after this long (default 300)")})),
    "record_stop": (t_record_stop, "Stop the recording and encode it (ffmpeg; without it the PNG frames are kept). Returns the path and a few frames as images.",
                    schema({"keyframes": d(INT, "frames spread over the recording to return as images (default 4)"),
                            "keep_frames": d(BOOL, "keep the PNG frames next to the video")})),
    "console_exec": (t_console_exec, "Run a shell command line on the unit's console and return its output and exit status. Logs in as root/nest first if the console is at login:. Not for interactive programs: use console_send for those.",
                     schema({"command": STR,
                             "timeout_s": d(NUM, "default 30; on timeout Ctrl-C is sent"),
                             "max_chars": d(INT, "keep the last this many characters of output (default 20000)"),
                             "login": d(BOOL, "log in when at login: (default true)")},
                            ["command"])),
    "console_send": (t_console_send, "Type raw keystrokes into the console (interactive programs, Ctrl-C, U-Boot). Returns a cursor for console_read/console_wait.",
                     schema({"text": d(STR, "characters to type"),
                             "enter": d(BOOL, "press Enter after the text"),
                             "ctrl": d(STR, "a control key after the text: \"c\" for Ctrl-C, \"d\" for Ctrl-D")})),
    "console_read": (t_console_read, "Console output after a cursor (from a previous call); without one, the recent backlog. Returns the new cursor.",
                     schema({"since": d(INT, "cursor to read on from"),
                             "wait_s": d(NUM, "wait up to this long for new output (max 30)"),
                             "max_chars": d(INT, "keep the last this many characters (default 8000)")})),
    "console_wait": (t_console_wait, "Wait until the console prints something matching a regex (boot messages, login:, a crash). Typed input is echoed, so pick a pattern your own command line does not contain.",
                     schema({"pattern": d(STR, "Python regex, matched against output with ANSI codes and CRs removed"),
                             "since": d(INT, "cursor to search from (default: now, so pass one from before the action you are waiting on)"),
                             "timeout_s": d(NUM, "default 30")},
                            ["pattern"])),
    "pull_file": (t_pull_file, "Read a file from the guest's filesystem: over ssh when the unit runs with --ssh (MB/s), else over the console (md5-checked, ~130 KB/s). Text comes back inline; binary needs local_path.",
                  schema({"path": d(STR, "guest path"),
                          "local_path": d(STR, "save to this host path instead of returning it"),
                          "via": d(STR, "auto (default: ssh if forwarded, else console), ssh or console"),
                          "timeout_s": d(NUM, "default 300")},
                         ["path"])),
    "push_file": (t_push_file, "Write a file into the guest: over ssh when the unit runs with --ssh (MB/s), else over the console (md5-checked, ~60 KB/s). Lands as path.mcp-part and is moved into place. The root filesystem is read-only until: mount -o remount,rw /",
                  schema({"path": d(STR, "guest path"),
                          "via": d(STR, "auto (default: ssh if forwarded, else console), ssh or console"),
                          "content": d(STR, "text to write"),
                          "local_path": d(STR, "host file to send instead of content"),
                          "mode": d(STR, "chmod mode, e.g. 755"),
                          "timeout_s": d(NUM, "default 600")},
                         ["path"])),
    "qmp": (t_qmp, "Send a QMP command to QEMU, e.g. system_reset (reboot), stop/cont (pause), human-monitor-command (HMP). quit ends QEMU and with it ui.py.",
            schema({"command": STR, "arguments": {"type": "object"}}, ["command"])),
}

INSTRUCTIONS = """\
Drives an emulated Nest thermostat (QEMU, started with ./run.sh --ui).
status first to see whether it is up. screenshot / capture_frames show the
screen; press_button, turn_wheel and input_sequence are the device's
inputs; record_start/record_stop make a video. console_exec runs shell
commands as root; pull_file/push_file move files. Console tools return a
cursor: pass it as `since` to console_read/console_wait so nothing printed
in between is missed."""


# ---------------------------------------------------------------- protocol

out_lock = threading.Lock()


def reply(msg):
    with out_lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def call_tool(rid, name, args):
    try:
        if name not in TOOLS:
            raise ToolError(f"no tool {name}")
        result = {"content": TOOLS[name][0](args or {}), "isError": False}
    except ToolError as e:
        result = {"content": [text(str(e))], "isError": True}
    except KeyError as e:
        result = {"content": [text(f"missing argument {e}")], "isError": True}
    except Exception as e:                  # a bug here, not the agent's
        result = {"content": [text(f"{type(e).__name__}: {e}")], "isError": True}
    reply({"jsonrpc": "2.0", "id": rid, "result": result})


def handle(msg):
    method, rid = msg.get("method"), msg.get("id")
    if rid is None:
        return                              # a notification: nothing to say
    if method == "initialize":
        want = msg.get("params", {}).get("protocolVersion")
        reply({"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": want if want in PROTOCOLS else PROTOCOLS[0],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "nest-qemu", "version": "1.0"},
            "instructions": INSTRUCTIONS}})
    elif method == "ping":
        reply({"jsonrpc": "2.0", "id": rid, "result": {}})
    elif method == "tools/list":
        reply({"jsonrpc": "2.0", "id": rid, "result": {"tools": [
            {"name": n, "description": desc, "inputSchema": sch}
            for n, (_, desc, sch) in TOOLS.items()]}})
    elif method == "tools/call":
        p = msg.get("params", {})
        # Its own thread: a long call (a recording, a slow command) must
        # not hold up pings or the next call.
        threading.Thread(target=call_tool, args=(rid, p.get("name"), p.get("arguments")),
                         daemon=True).start()
    else:
        reply({"jsonrpc": "2.0", "id": rid,
               "error": {"code": -32601, "message": f"no method {method}"}})


def main():
    global base_url
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", default=os.environ.get("NEST_UI_URL"),
                    help="ui.py's address (run.sh --ui prints it); by default "
                         "the newest unit running here")
    url = ap.parse_args().url
    base_url = url.rstrip("/") if url else None
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply({"jsonrpc": "2.0", "id": None,
                   "error": {"code": -32700, "message": "parse error"}})
            continue
        handle(msg)
    # The client went: a recording left running still becomes a video.
    if recorder:
        record_stop(0)
    for path in askpass:
        os.unlink(path)


if __name__ == "__main__":
    main()
