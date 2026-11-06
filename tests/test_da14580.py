"""Firmware-free Diamond3 BLE UART, GPIO reset, GTL and HCI checks."""
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import time
import unittest

UART = 0x021f4000
GPIO = 0x020ac000


@unittest.skipUnless(os.environ.get("QEMU_SYSTEM_ARM"), "set QEMU_SYSTEM_ARM")
class DA14580Tests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="da14580-")
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.proc = subprocess.Popen([
            os.environ["QEMU_SYSTEM_ARM"], "-M", "diamond3", "-accel", "qtest",
            "-display", "none", "-nodefaults",
            "-qtest", f"unix:{root}/qtest,server=on,wait=off",
            "-qmp", f"unix:{root}/qmp,server=on,wait=off",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop)
        for name in ("qtest", "qmp"):
            deadline = time.monotonic() + 5
            while not (root / name).exists():
                self.assertIsNone(self.proc.poll(), "QEMU startup failed")
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)
            sock = socket.socket(socket.AF_UNIX)
            self.addCleanup(sock.close)
            sock.settimeout(5)
            sock.connect(str(root / name))
            stream = sock.makefile("rwb", buffering=0)
            self.addCleanup(stream.close)
            setattr(self, name, stream)
        self.assertIn("QMP", json.loads(self.qmp.readline()))
        self.monitor("qmp_capabilities")
        self.pending = bytearray()
        self.write(UART + 0x80, 1)
        self.write(UART + 0x84, 7)

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)

    def monitor(self, command, arguments=None):
        request = {"execute": command}
        if arguments is not None:
            request["arguments"] = arguments
        self.qmp.write(json.dumps(request).encode() + b"\n")
        while True:
            result = json.loads(self.qmp.readline())
            if "event" not in result:
                self.assertIn("return", result)
                return result["return"]

    def prop(self, name, value):
        self.monitor("qom-set", {"path": "/chardevs/diamond3-ble",
                                  "property": name, "value": value})

    def command(self, text):
        self.qtest.write(text.encode() + b"\n")
        result = self.qtest.readline().decode().split()
        self.assertEqual(result[0], "OK", result)
        return result

    def read(self, address):
        return int(self.command(f"readl {address:#x}")[1], 16)

    def write(self, address, value):
        self.command(f"writel {address:#x} {value:#x}")

    def send(self, payload):
        for byte in payload:
            self.write(UART + 0x40, byte)

    def receive(self, count):
        for _ in range(1000):
            if len(self.pending) >= count:
                result = bytes(self.pending[:count])
                del self.pending[:count]
                return result
            self.command("clock_step 1000000")
            while self.read(UART + 0x98) & 1:
                self.pending.append(self.read(UART) & 255)
        self.fail(f"UART timeout: {self.pending.hex()} (wanted {count})")

    def download(self):
        self.assertEqual(self.receive(1), b"\x02")
        image = bytes(range(256)) * 8 + b"\x42"
        self.send(b"\x01" + struct.pack("<H", len(image)))
        self.assertEqual(self.receive(1), b"\x06")
        self.send(image)
        self.assertEqual(self.receive(1), b"\x42")
        self.send(b"\x06")
        self.assertEqual(self.receive(9), bytes.fromhex("05010d10000d000000"))

    def test_download_gtl_and_reset(self):
        self.download()
        # Fragmented reset command; source task 0x10 is reflected in the event.
        self.send(bytes.fromhex("05020d0d00"))
        self.send(bytes.fromhex("1000010001"))
        self.assertEqual(self.receive(11), bytes.fromhex("05000d10000d0002000100"))
        self.send(bytes.fromhex("05060d0d001000010003"))
        reply = self.receive(27)
        self.assertEqual(reply[:9], bytes.fromhex("05080d10000d000700"))
        self.assertEqual(reply[9:15], bytes.fromhex("804501000002"))
        # Oversize GTL length is rejected without overrunning the packet buffer.
        self.send(bytes.fromhex("05020d0d001000ffff"))
        self.send(bytes.fromhex("05020d0d001000010001"))
        self.assertEqual(self.receive(11)[-2:], b"\x01\x00")
        self.write(GPIO + 4, 1 << 16)
        self.write(GPIO, 1 << 16)
        self.command("clock_step 200000000")
        self.assertFalse(self.read(UART + 0x98) & 1)
        self.write(GPIO, 0)
        self.assertEqual(self.receive(1), b"\x02")
        self.send(bytes.fromhex("010100"))
        self.assertEqual(self.receive(1), b"\x06")
        self.send(b"\x55")
        self.assertEqual(self.receive(1), b"\x55")
        self.send(b"\x15")
        self.assertEqual(self.receive(1), b"\x02")

    def hci(self, opcode, payload=b""):
        self.send(b"\x01" + struct.pack("<HB", opcode, len(payload)) + payload)
        header = self.receive(3)
        return header + self.receive(header[2])

    def att(self, request):
        l2cap = struct.pack("<HH", len(request), 4) + request
        self.send(b"\x02" + struct.pack("<HH", 0x0040, len(l2cap)) + l2cap)
        header = self.receive(5)
        self.assertEqual(header[:3], bytes.fromhex("024020"))
        payload = self.receive(struct.unpack_from("<H", header, 3)[0])
        self.assertEqual(payload[2:4], b"\x04\x00")
        self.assertEqual(self.receive(8), bytes.fromhex("0413050140000100"))
        return payload[4:]

    def test_hci_scan_connection_and_gatt(self):
        self.prop("hci-test-firmware", True)
        self.assertEqual(self.hci(0x0c03), bytes.fromhex("040e0401030c00"))
        reply = self.hci(0x1001)
        self.assertEqual(reply[6:8], b"\x00\x06")
        # Unknown commands and malformed known commands return errors.
        self.assertEqual(self.hci(0x1234)[6], 1)
        self.assertEqual(self.hci(0x200c, b"\x01")[6], 0x12)
        self.prop("peer-present", True)
        self.assertEqual(self.hci(0x200c, b"\x01\x00")[6], 0)
        report = self.receive(38)
        self.assertIn(b"QEMU BLE test peer", report)
        self.hci(0x200c, b"\x00\x00")
        self.hci(0x200b, bytes.fromhex("01100010000000"))
        self.hci(0x200c, b"\x01\x00")
        reports = self.receive(76)
        self.assertEqual(reports[5], 0)
        self.assertEqual(reports[43], 4)
        self.hci(0x200c, b"\x00\x00")
        params = bytes.fromhex("1000100000000100000000020006000c000000c80000000000")
        self.send(b"\x01" + struct.pack("<HB", 0x200d, len(params)) + params)
        connection = self.receive(29)
        self.assertEqual(connection[:7], bytes.fromhex("040f0400010d20"))
        self.assertIn(bytes.fromhex("043e1301004000"), connection)
        self.assertEqual(self.att(bytes.fromhex("020502")), bytes.fromhex("031700"))
        self.assertEqual(self.att(bytes.fromhex("100100ffff0128")),
                         bytes.fromhex("011001000a"))
        primary = self.att(bytes.fromhex("100100ffff0028"))
        self.assertEqual(primary, bytes.fromhex("1106010005000018060008000a18"))
        self.assertEqual(self.att(bytes.fromhex("0a0300")), b"\x0bQEMU BLE test peer")
        self.assertEqual(self.att(bytes.fromhex("0a0800")), b"\x0bQEMU")
        self.assertEqual(self.hci(0x1405, b"\x40\x00")[-1], 211)
        self.prop("hci-test-firmware", False)
        self.assertEqual(self.receive(1), b"\x02")


if __name__ == "__main__":
    unittest.main()
