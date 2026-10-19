"""USB/IP wire-level tests; no kernel module or firmware required."""
import errno
import socket
import struct
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ui"))
from usbip import (Device, Session, VERSION, ECONNRESET, EREMOTEIO,
                   control, receive, serve_client, AutoAttach, host_commands, imported_port)  # noqa: E402
from usbhost import Config, DeviceDescriptor  # noqa: E402


class FakeBridge:
    def __init__(self):
        self.io_lock = threading.Lock()
        self.connected = True
        self.closed = False
        self.generation = 0
        self.timeout = 0.5
        self.calls = []
        self.handler = lambda ep, direction, value: ("NAK", b"" if direction else 0)

    def token(self, ep, direction, value):
        self.calls.append((ep, direction, value))
        return self.handler(ep, direction, value)

    def setup(self, setup):
        self.calls.append(("SETUP", setup))

    def reset(self):
        self.calls.append(("RESET",))


def device(bridge):
    dev = Device.__new__(Device)
    dev.bridge = bridge
    dev.descriptor = DeviceDescriptor(bytes.fromhex(
        '120100020000004064245100000101020301'))
    dev.config = Config(bytes.fromhex(
        '090220000101008032090400000208065000'
        '0705810200020007050102000200'))
    dev.endpoints = {0x81: SimpleNamespace(type="bulk", maxpacket=512),
                     1: SimpleNamespace(type="bulk", maxpacket=512),
                     0x82: SimpleNamespace(type="int", maxpacket=16)}
    return dev


def submit(seq, ep, direction, length, data=b"", setup=b"\0" * 8, flags=0):
    return struct.pack("!IIIIIIiiii8s", 1, seq, 0x10001, direction, ep,
                       flags, length, 0, -1, 0, setup) + data


def reply(sock):
    header = receive(sock, 48)
    command, seq = struct.unpack_from("!II", header)
    status, actual = struct.unpack_from("!ii", header, 20)
    return command, seq, status, actual


class UsbIpTest(unittest.TestCase):
    def setUp(self):
        self.host, self.server = socket.socketpair()
        self.host.settimeout(2)
        self.bridge = FakeBridge()
        self.device = device(self.bridge)
        self.thread = None
        self.errors = []

    def tearDown(self):
        self.host.close()
        if self.thread:
            self.thread.join(2)
            self.assertFalse(self.thread.is_alive())
        self.server.close()
        self.assertEqual(self.errors, [])

    def start(self, fn):
        def run():
            try:
                fn()
            except EOFError:
                pass
            except Exception as error:
                self.errors.append(error)
        self.thread = threading.Thread(target=run)
        self.thread.start()

    def session(self):
        self.start(lambda: Session(self.server, self.device).run())

    def test_device_list(self):
        self.start(lambda: serve_client(self.server, self.device))
        packet = struct.pack("!HHI", VERSION, 0x8005, 0)
        for byte in packet:
            self.host.sendall(bytes([byte]))
        self.assertEqual(receive(self.host, 12), struct.pack("!HHII", VERSION, 5, 0, 1))
        description = receive(self.host, 312)
        self.assertEqual(description[256:288].rstrip(b"\0"), b"1-1")
        self.assertEqual(struct.unpack_from("!HH", description, 300), (0x2464, 0x51))
        self.assertEqual(receive(self.host, 4), b"\x08\x06\x50\0")

    def test_unknown_import(self):
        self.start(lambda: serve_client(self.server, self.device))
        self.host.sendall(struct.pack("!HHI32s", VERSION, 0x8003, 0, b"2-2"))
        self.assertEqual(receive(self.host, 8), struct.pack("!HHI", VERSION, 3, 1))

    def test_import_addresses_guest(self):
        self.bridge.handler = lambda *args: ("ACK", b"")
        self.start(lambda: serve_client(self.server, self.device))
        self.host.sendall(struct.pack("!HHI32s", VERSION, 0x8003, 0, b"1-1"))
        self.assertEqual(receive(self.host, 8), struct.pack("!HHI", VERSION, 3, 0))
        receive(self.host, 312)
        self.assertEqual(self.bridge.calls[:2], [("RESET",),
                         ("SETUP", struct.pack("<BBHHH", 0, 5, 1, 0, 0))])

    def test_bulk_short_packet(self):
        self.bridge.handler = lambda ep, direction, value: ("ACK", b"hello")
        self.session()
        self.host.sendall(submit(4, 1, 1, 1024))
        self.assertEqual(reply(self.host), (3, 4, 0, 5))
        self.assertEqual(receive(self.host, 5), b"hello")

    def test_bulk_out_preserves_payload(self):
        self.bridge.handler = lambda ep, direction, value: ("ACK", len(value))
        self.session()
        payload = bytes(range(256)) * 5
        self.host.sendall(submit(2, 1, 0, len(payload), payload))
        self.assertEqual(reply(self.host), (3, 2, 0, len(payload)))
        self.assertEqual(b"".join(c[2] for c in self.bridge.calls), payload)

    def test_zero_packet_flag(self):
        self.bridge.handler = lambda ep, direction, value: ("ACK", len(value))
        self.session()
        self.host.sendall(submit(2, 1, 0, 512, b"x" * 512, flags=0x40))
        self.assertEqual(reply(self.host), (3, 2, 0, 512))
        self.assertEqual(self.bridge.calls[-1], (1, 0, b""))

    def test_short_not_ok(self):
        self.bridge.handler = lambda *args: ("ACK", b"")
        self.session()
        self.host.sendall(submit(3, 1, 1, 64, flags=1))
        self.assertEqual(reply(self.host), (3, 3, -EREMOTEIO, 0))

    def test_stall(self):
        self.bridge.handler = lambda *args: ("STALL", b"")
        self.session()
        self.host.sendall(submit(3, 1, 1, 64))
        self.assertEqual(reply(self.host), (3, 3, -errno.EPIPE, 0))

    def test_unlink_and_independent_endpoints(self):
        # An interrupt endpoint can NAK indefinitely while bulk completes.
        self.bridge.handler = lambda ep, direction, value: (
            ("NAK", b"") if ep == 2 else ("ACK", b"data"))
        self.session()
        self.host.sendall(submit(10, 2, 1, 16) + submit(11, 1, 1, 4))
        self.assertEqual(reply(self.host), (3, 11, 0, 4))
        self.assertEqual(receive(self.host, 4), b"data")
        self.host.sendall(struct.pack("!IIIIII24x", 2, 12, 0x10001, 0, 0, 10))
        self.assertEqual(reply(self.host), (4, 12, -ECONNRESET, 0))
        self.host.sendall(struct.pack("!IIIIII24x", 2, 13, 0x10001, 0, 0, 11))
        self.assertEqual(reply(self.host), (4, 13, 0, 0))
        self.host.sendall(submit(14, 1, 1, 4))
        self.assertEqual(reply(self.host), (3, 14, 0, 4))
        self.assertEqual(receive(self.host, 4), b"data")

    def test_control_out_and_status(self):
        self.bridge.handler = lambda ep, direction, value: (
            "ACK", b"" if direction else len(value))
        setup = struct.pack("<BBHHH", 0x21, 0x20, 0, 0, 7)
        control(self.bridge, setup, b"1234567")
        self.assertEqual(self.bridge.calls, [("SETUP", setup),
                                           (0, 0, b"1234567"), (0, 1, 0)])

    def test_control_in_short_and_status(self):
        self.bridge.handler = lambda ep, direction, value: (
            "ACK", b"desc" if direction else 0)
        setup = struct.pack("<BBHHH", 0x80, 6, 0x100, 0, 18)
        self.assertEqual(control(self.bridge, setup), b"desc")
        self.assertEqual(self.bridge.calls[-1], (0, 0, b""))

    def test_disconnect_ends_import(self):
        self.session()
        self.host.sendall(submit(10, 2, 1, 16))
        self.bridge.connected = False
        # A pending URB may complete with ENODEV before the socket closes.
        first = self.host.recv(1)
        if first:
            header = first + receive(self.host, 47)
            self.assertEqual(struct.unpack_from("!i", header, 20)[0], -errno.ENODEV)
            self.assertEqual(self.host.recv(1), b"")
        self.thread.join(2)
        self.assertFalse(self.thread.is_alive())


class AutoAttachTest(unittest.TestCase):
    def test_matching_host_port(self):
        output = """Port 00: <Port in Use> at High Speed(480Mbps)
  1-1 -> usbip://127.0.0.1:3241/1-1
Port 03: <Port in Use> at High Speed(480Mbps)
  1-2 -> usbip://127.0.0.1:3240/1-1
"""
        self.assertEqual(imported_port(output, 3240), 3)
        self.assertIsNone(imported_port(output, 3242))

    @patch("usbip.sys.platform", "linux")
    @patch("usbip.os.geteuid", return_value=1000)
    @patch("usbip.shutil.which", side_effect=lambda name: "/usr/bin/" + name)
    @patch("usbip.subprocess.run", return_value=SimpleNamespace(returncode=0))
    def test_noninteractive_host_detection(self, run, which, geteuid):
        self.assertEqual(host_commands(), ["sudo", "-n", "/usr/bin/usbip"])
        self.assertEqual(run.call_args_list[0].args[0],
                         ["sudo", "-n", "/usr/bin/modprobe", "vhci-hcd"])
        self.assertEqual(run.call_args_list[1].args[0],
                         ["sudo", "-n", "/usr/bin/usbip", "port"])

    @patch("usbip.sys.platform", "linux")
    @patch("usbip.os.geteuid", return_value=1000)
    @patch("usbip.shutil.which", side_effect=lambda name: "/usr/bin/" + name)
    @patch("usbip.subprocess.run", return_value=SimpleNamespace(
        returncode=1, stderr="sudo: a password is required"))
    def test_unavailable_privileges(self, run, which, geteuid):
        with self.assertRaisesRegex(RuntimeError, "password is required"):
            host_commands()

    def test_attach_and_cleanup_on_disconnect(self):
        attachment = AutoAttach.__new__(AutoAttach)
        attachment.port = 3240
        attachment.bridge = FakeBridge()
        attachment.bridge.closed = True
        attachment.stopped = threading.Event()
        result = SimpleNamespace(returncode=0, stdout="", stderr="attached port: 3")
        with patch.object(attachment, "run_command", return_value=result) as command:
            attachment.run()
        self.assertEqual(command.call_args_list[0].args,
                         ("-d", "-t", "3240", "attach", "-r", "127.0.0.1", "-b", "1-1"))
        self.assertEqual(command.call_args_list[-1].args, ("detach", "-p", "3"))


if __name__ == "__main__":
    unittest.main()
