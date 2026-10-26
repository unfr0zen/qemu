#!/usr/bin/env python3
"""Export the flintstone UDC to Linux vhci_hcd over USB/IP (TCP 3240).

The guest owns descriptors and USB functions. This process translates URBs
into acknowledged bridge tokens; it does not interpret mass-storage data.
See docs/usb-host-bridge-protocol.md for setup and host attachment.
"""
import argparse
import errno
import os
import re
import shutil
import subprocess
import socket
import struct
import sys
import threading
import time
from queue import Empty, Queue

from usbhost import Bridge, BridgeError, Config, DeviceDescriptor

VERSION = 0x0111
BUSID = b"1-1"
DEVID = 0x00010001
# USB/IP status values use Linux errno numbers, including on macOS.
ECONNRESET = 104
EREMOTEIO = 121
MAX_TRANSFER = 16 * 1024 * 1024


def receive(sock, length):
    data = bytearray()
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise EOFError("USB/IP client disconnected")
        data.extend(chunk)
    return bytes(data)


class TokenBridge(Bridge):
    def __init__(self, *args, **kwargs):
        self.tokens = Queue()
        self.io_lock = threading.Lock()
        self.generation = 0
        super().__init__(*args, **kwargs)

    def _handle(self, line):
        if line.startswith("TOKEN "):
            parts = line.split(maxsplit=4)
            self.tokens.put((int(parts[1]), parts[2], parts[3],
                             parts[4] if len(parts) == 5 else ""))
        else:
            if line == "DISCONNECT":
                self.generation += 1
            super()._handle(line)

    def close(self):
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()

    def token(self, ep, direction, value):
        # Replies are synchronous, so one bridge token is in flight at a time.
        with self.io_lock:
            command = "TOKENIN" if direction else "TOKENOUT"
            payload = str(value) if direction else value.hex()
            self._send(f"{command} {ep} {payload}")
            try:
                rep_ep, rep_dir, status, payload = self.tokens.get(
                    timeout=self.timeout)
            except Empty:
                raise BridgeError("no TOKEN reply; rebuild QEMU with patch 0105")
            if (rep_ep, rep_dir) != (ep, "IN" if direction else "OUT"):
                raise BridgeError("out-of-order TOKEN reply")
            return status, bytes.fromhex(payload) if direction else int(payload)


def transfer(bridge, ep, direction, value, cancelled):
    """Retry NAKs until completion, cancellation, or a gadget disconnect."""
    generation = bridge.generation
    while not cancelled.is_set():
        if bridge.closed or not bridge.connected or bridge.generation != generation:
            raise OSError(errno.ENODEV, "gadget disconnected")
        status, result = bridge.token(ep, direction, value)
        if status == "ACK":
            return result
        if status == "STALL":
            raise OSError(errno.EPIPE, "endpoint stalled")
        if status != "NAK":
            raise OSError(errno.EIO, "UDC transfer failed")
        cancelled.wait(0.002)
    raise OSError(ECONNRESET, "URB cancelled")


def control(bridge, setup, data=b"", cancelled=None):
    cancelled = cancelled or threading.Event()
    rtype, request, value, index, length = struct.unpack("<BBHHH", setup)
    with bridge.io_lock:
        bridge.setup(setup)
    result = bytearray()
    if rtype & 0x80:
        while len(result) < length:
            count = min(64, length - len(result))
            chunk = transfer(bridge, 0, 1, count, cancelled)
            result.extend(chunk)
            if len(chunk) < count:
                break
        transfer(bridge, 0, 0, b"", cancelled)
    else:
        for offset in range(0, length, 64):
            chunk = data[offset:offset + 64]
            if transfer(bridge, 0, 0, chunk, cancelled) != len(chunk):
                raise OSError(errno.EIO, "short control OUT")
        transfer(bridge, 0, 1, 0, cancelled)
    return bytes(result)


class Device:
    def __init__(self, bridge):
        self.bridge = bridge
        self.bridge.reset()
        self.bridge.speed("hs")
        self.descriptor = DeviceDescriptor(self.control(0x80, 6, 0x100, 18))
        if self.descriptor.ep0_maxpacket != 64:
            raise BridgeError("USB/IP exporter requires high-speed EP0 (64 bytes)")
        header = self.control(0x80, 6, 0x200, 9)
        if len(header) < 9:
            raise BridgeError("short configuration descriptor")
        self.config = Config(self.control(0x80, 6, 0x200,
                                         struct.unpack_from("<H", header, 2)[0]))
        self.control(0, 5, 1)
        self.endpoints = {e.addr: e for i in self.config.interfaces
                          for e in i.endpoints}

    def control(self, rtype, request, value=0, length=0):
        # Enumeration must have a deadline even when the guest never primes EP0.
        cancelled = threading.Event()
        timer = threading.Timer(self.bridge.timeout, cancelled.set)
        timer.start()
        try:
            return control(self.bridge, struct.pack("<BBHHH", rtype, request,
                                                   value, 0, length),
                           cancelled=cancelled)
        finally:
            timer.cancel()

    def description(self):
        d = self.descriptor
        interfaces = [i for i in self.config.interfaces if i.alt == 0]
        return struct.pack("!256s32sIIIHHHBBBBBB", b"/qemu/flintstone/usb0",
                           BUSID, 1, 1, 3, d.vendor, d.product, d.bcd_device,
                           d.dev_class, d.dev_subclass, d.dev_protocol,
                           self.config.cfg_value, d.num_configs, len(interfaces))

    def interface_data(self):
        return b"".join(bytes((i.iface_class, i.subclass, i.protocol, 0))
                        for i in self.config.interfaces if i.alt == 0)


class Session:
    """One importing host; independent endpoint workers keep NAKs from blocking IO."""
    def __init__(self, sock, device):
        self.sock = sock
        self.device = device
        self.lock = threading.Lock()
        self.pending = {}
        self.queues = {}
        self.workers = []
        self.stopped = threading.Event()

    def reply(self, command, seq, status, actual=0, data=b""):
        self.sock.sendall(struct.pack("!IIIIIiiiii8x", command, seq, 0, 0, 0,
                                     status, actual, 0, 0, 0) + data)

    def worker(self, queue):
        while not self.stopped.is_set():
            try:
                urb = queue.get(timeout=0.1)
            except Empty:
                continue
            seq, direction, ep, flags, length, setup, data, cancelled = urb
            status, actual, result = 0, 0, bytearray()
            try:
                if cancelled.is_set():
                    continue
                if ep == 0:
                    if bool(setup[0] & 0x80) != bool(direction) or \
                            struct.unpack_from("<H", setup, 6)[0] != length:
                        raise OSError(errno.EINVAL, "invalid control URB")
                    result = control(self.device.bridge, setup, data, cancelled)
                    actual = len(result) if direction else length
                else:
                    endpoint = self.device.endpoints.get(ep | (direction << 7))
                    if endpoint is None or endpoint.type == "iso":
                        raise OSError(errno.EPIPE, "unsupported endpoint")
                    packet = endpoint.maxpacket & 0x7ff
                    while actual < length or length == 0:
                        count = min(packet, length - actual)
                        value = count if direction else data[actual:actual + count]
                        chunk = transfer(self.device.bridge, ep, direction,
                                         value, cancelled)
                        done = len(chunk) if direction else chunk
                        if direction:
                            result.extend(chunk)
                        actual += done
                        if done < count or actual == length:
                            break
                    if not direction and length and length % packet == 0 and flags & 0x40:
                        transfer(self.device.bridge, ep, 0, b"", cancelled)
                if direction and actual < length and flags & 1:
                    status = -EREMOTEIO
            except OSError as error:
                status = -error.errno
            except BridgeError:
                # A lost bridge reply leaves stream correlation undefined.
                self.stop()
                return
            with self.lock:
                if self.pending.pop(seq, None) is not None:
                    try:
                        self.reply(3, seq, status, actual,
                                   bytes(result) if direction else b"")
                    except OSError:
                        self.stop()
                        return

    def stop(self):
        self.stopped.set()
        for cancelled in list(self.pending.values()):
            cancelled.set()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def run(self):
        generation = self.device.bridge.generation

        def monitor():
            while not self.stopped.wait(0.05):
                bridge = self.device.bridge
                if bridge.closed or not bridge.connected or bridge.generation != generation:
                    self.stop()
                    return

        watcher = threading.Thread(target=monitor, daemon=True)
        watcher.start()
        try:
            while not self.stopped.is_set():
                header = receive(self.sock, 48)
                command, seq, devid, direction, ep = struct.unpack_from("!IIIII", header)
                if command == 2:
                    target = struct.unpack_from("!I", header, 20)[0]
                    with self.lock:
                        cancelled = self.pending.pop(target, None)
                        if cancelled:
                            cancelled.set()
                        self.reply(4, seq, -ECONNRESET if cancelled else 0)
                    continue
                if command != 1 or devid != DEVID or direction > 1 or ep > 15:
                    raise ValueError("invalid USB/IP URB header")
                flags, length, frame, packets, interval = struct.unpack_from("!Iiiii", header, 20)
                if length < 0 or length > MAX_TRANSFER or packets not in (0, -1):
                    raise ValueError("invalid size or unsupported isochronous URB")
                data = receive(self.sock, length) if not direction else b""
                key = (ep, direction) if ep else (0, 0)
                with self.lock:
                    if seq in self.pending or len(self.pending) >= 1024:
                        raise ValueError("duplicate URB or too many pending URBs")
                    cancelled = threading.Event()
                    self.pending[seq] = cancelled
                    if key not in self.queues:
                        self.queues[key] = Queue()
                        worker = threading.Thread(target=self.worker,
                                                  args=(self.queues[key],), daemon=True)
                        self.workers.append(worker)
                        worker.start()
                    self.queues[key].put((seq, direction, ep, flags, length,
                                          header[40:48], data, cancelled))
        finally:
            self.stop()
            for worker in self.workers:
                worker.join(self.device.bridge.timeout + 1)
            watcher.join()


def serve_client(sock, device):
    version, operation, status = struct.unpack("!HHI", receive(sock, 8))
    if version != VERSION or status:
        raise ValueError("invalid USB/IP management header")
    if operation == 0x8005:
        sock.sendall(struct.pack("!HHII", VERSION, 5, 0, 1) +
                     device.description() + device.interface_data())
    elif operation == 0x8003:
        busid = receive(sock, 32).rstrip(b"\0")
        if busid != BUSID:
            sock.sendall(struct.pack("!HHI", VERSION, 3, 1))
            return
        # vhci_hcd handles SET_ADDRESS locally; the guest must already be addressed.
        device.bridge.reset()
        device.control(0, 5, 1)
        sock.sendall(struct.pack("!HHI", VERSION, 3, 0) + device.description())
        sock.settimeout(None)
        Session(sock, device).run()
    else:
        raise ValueError("unsupported USB/IP management operation")


def host_commands():
    if not sys.platform.startswith("linux"):
        raise RuntimeError("automatic USB attachment requires Linux")
    usbip = shutil.which("usbip")
    modprobe = shutil.which("modprobe")
    if not usbip or not modprobe:
        raise RuntimeError("automatic USB requires usbip and modprobe on PATH")
    prefix = [] if os.geteuid() == 0 else ["sudo", "-n"]
    if prefix and not shutil.which("sudo"):
        raise RuntimeError("automatic USB requires root or non-interactive sudo")
    for command in ([modprobe, "vhci-hcd"], [usbip, "port"]):
        result = subprocess.run(prefix + command, capture_output=True, text=True,
                                timeout=10)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "vhci_hcd is unavailable")
    return prefix + [usbip]


def imported_port(output, port):
    current = None
    for line in output.splitlines():
        match = re.match(r"Port (\d+):", line)
        if match:
            current = int(match[1])
        if f"usbip://127.0.0.1:{port}/1-1" in line:
            return current
    return None


class AutoAttach:
    def __init__(self, command, port, bridge):
        self.command = command
        self.port = port
        self.bridge = bridge
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run_command(self, *args):
        return subprocess.run(self.command + list(args), capture_output=True,
                              text=True, timeout=10)

    def run(self):
        attached_port = None
        generation = self.bridge.generation
        try:
            result = self.run_command("-d", "-t", str(self.port), "attach",
                                      "-r", "127.0.0.1", "-b", "1-1")
            match = re.search(r"attached port: (\d+)", result.stdout + result.stderr)
            if match:
                attached_port = int(match[1])
            if result.returncode:
                raise RuntimeError(result.stderr.strip() or "usbip attach failed")
            # The kernel may still be enumerating when usbip attach returns.
            for _ in range(50):
                if attached_port is not None:
                    break
                output = self.run_command("port")
                attached_port = imported_port(output.stdout, self.port)
                if attached_port is not None:
                    break
                time.sleep(0.1)
            if attached_port is None:
                raise RuntimeError("cannot identify attached USB/IP port")
            print(f"USB attached to host (vhci port {attached_port})", flush=True)
            while not self.stopped.wait(0.1):
                if self.bridge.closed or self.bridge.generation != generation:
                    break
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
            print(f"USB auto-attach: {error}", file=sys.stderr)
        finally:
            if attached_port is not None:
                try:
                    self.run_command("detach", "-p", str(attached_port))
                except (OSError, subprocess.TimeoutExpired) as error:
                    print(f"USB detach: {error}", file=sys.stderr)

    def close(self):
        self.stopped.set()
        self.thread.join(12)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", help="UDC Unix socket or tcp:HOST:PORT")
    parser.add_argument("--listen", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3240)
    parser.add_argument("--parent", type=int, help="exit when launcher exits")
    parser.add_argument("--attach", action="store_true", help="attach to this Linux host")
    parser.add_argument("--check-host", action="store_true", help="check Linux auto-attach prerequisites")
    args = parser.parse_args()
    command = None
    if args.check_host or args.attach:
        try:
            command = host_commands()
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
            parser.exit(1, f"USB: {error}\n")
    if args.check_host:
        return
    if not args.socket:
        parser.error("--socket is required")
    attachment = None
    if args.parent:
        def follow_parent():
            while True:
                try:
                    os.kill(args.parent, 0)
                except ProcessLookupError:
                    if attachment:
                        attachment.close()
                    os._exit(0)
                time.sleep(0.5)
        threading.Thread(target=follow_parent, daemon=True).start()
    with socket.create_server((args.listen, args.port)) as server:
        print(f"USB/IP listening on {args.listen}:{args.port}, busid 1-1", flush=True)
        bridge = None
        try:
            while True:
                try:
                    if bridge is None or bridge.closed:
                        if bridge is not None:
                            bridge.close()
                        bridge = TokenBridge(args.socket)
                    bridge.wait_connect(timeout=3600)
                    device = Device(bridge)
                    if attachment:
                        attachment.close()
                    if command:
                        attachment = AutoAttach(command, args.port, bridge)
                    generation = bridge.generation
                    server.settimeout(0.25)
                    while not bridge.closed and bridge.connected and bridge.generation == generation:
                        try:
                            client, address = server.accept()
                        except socket.timeout:
                            continue
                        with client:
                            client.settimeout(5)
                            try:
                                serve_client(client, device)
                            except (EOFError, OSError, ValueError) as error:
                                print(f"USB/IP client: {error}", file=sys.stderr)
                except (ConnectionRefusedError, FileNotFoundError):
                    # QEMU may not have opened its chardev yet.
                    time.sleep(0.1)
                except (BridgeError, OSError, ValueError) as error:
                    print(f"USB/IP: {error}", file=sys.stderr)
                    if bridge:
                        bridge.close()
                        bridge = None
                    time.sleep(1)
        finally:
            if attachment:
                attachment.close()
            if bridge:
                bridge.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
