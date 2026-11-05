"""Firmware-free Onyx DDR, thermal and NAND protocol checks."""
import json
import os
from pathlib import Path
import select
import socket
import struct
import subprocess
import tempfile
import time
import unittest


@unittest.skipUnless(os.environ.get("QEMU_SYSTEM_ARM"), "set QEMU_SYSTEM_ARM to a patched QEMU")
class OnyxHardwareTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="onyx-qtest-")
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.proc = subprocess.Popen([
            os.environ["QEMU_SYSTEM_ARM"], "-M", "onyx", "-accel", "qtest",
            "-display", "none", "-nodefaults",
            "-qtest", f"unix:{root}/qtest,server=on,wait=off",
            "-qmp", f"unix:{root}/qmp,server=on,wait=off",
            "-chardev", f"socket,id=uart,path={root}/uart,server=on,wait=off",
            "-serial", "chardev:uart",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop)
        for name in ("qtest", "qmp", "uart"):
            deadline = time.monotonic() + 5
            while not (root / name).exists():
                self.assertIsNone(self.proc.poll(), "QEMU exited during startup")
                self.assertLess(time.monotonic(), deadline, "QEMU socket timeout")
                time.sleep(0.01)
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(sock.close)
            sock.settimeout(5)
            sock.connect(str(root / name))
            if name == "uart":
                self.uart = sock
                continue
            stream = sock.makefile("rwb", buffering=0)
            self.addCleanup(stream.close)
            setattr(self, name, stream)
        self.assertIn("QMP", json.loads(self.qmp.readline()))
        self.monitor("qmp_capabilities")

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)

    def monitor(self, command):
        self.qmp.write(json.dumps({"execute": command}).encode() + b"\n")
        while True:
            result = json.loads(self.qmp.readline())
            if "event" not in result:
                self.assertIn("return", result)
                return result["return"]

    def command(self, text):
        self.qtest.write(text.encode() + b"\n")
        reply = self.qtest.readline().decode().strip()
        self.assertTrue(reply.startswith("OK"), reply)
        return reply.split()

    def read(self, address):
        return int(self.command(f"readl {address:#x}")[1], 16)

    def write(self, address, value):
        self.command(f"writel {address:#x} {value:#x}")

    def test_lpddr2_commands(self):
        self.write(0x021b001c, 1 << 15)
        self.assertEqual(self.read(0x021b001c), (1 << 15) | (1 << 14))
        self.write(0x021b001c, 0x00058060)
        self.assertTrue(self.read(0x021b001c) & (1 << 10))
        self.write(0x021b001c, 0)
        self.assertEqual(self.read(0x021b001c), 0)

    def test_fuses_and_temperature(self):
        self.assertEqual(self.read(0x021bc420), 0x51454d55)
        self.assertEqual(self.read(0x021bc460), 2)
        self.assertEqual(self.read(0x021bc4e0), 1500 << 20)
        self.write(0x020c8188, 1)
        self.write(0x020c8184, 2)
        self.assertEqual(self.read(0x020c8180), (1460 << 8) | 6)
        self.write(0x020c8188, 4)
        self.assertFalse(self.read(0x020c8180) & 4)
        self.monitor("system_reset")
        self.assertEqual(self.read(0x020c8180), 1)
        self.assertEqual(self.read(0x021bc460), 2)

    def test_iomux_readback_and_reset(self):
        for address, value in ((0x020e0084, 5), (0x020e0088, 5),
                               (0x020e0310, 0x1b0b1), (0x020e0624, 3)):
            self.write(address, value)
            self.assertEqual(self.read(address), value)
        self.monitor("system_reset")
        for address in (0x020e0084, 0x020e0088, 0x020e0310, 0x020e0624):
            self.assertEqual(self.read(address), 0)
        self.write(0x02020040, ord("A"))
        self.assertEqual(self.uart.recv(1), b"A")

    def test_uart_tx_pinmux(self):
        self.write(0x020e0088, 5)
        self.write(0x02020040, ord("A"))
        self.assertEqual(self.uart.recv(1), b"A")
        for mode in (1, 5):
            self.write(0x020e0084, mode)
            self.write(0x02020040, ord("X"))
            self.assertFalse(select.select([self.uart], [], [], 0.05)[0])
            self.assertTrue(self.read(0x02020098) & 8)  # TXDC still completes.
        self.write(0x020e0084, 0x10)  # SION does not change the mux mode.
        self.write(0x02020040, ord("B"))
        self.assertEqual(self.uart.recv(1), b"B")

    def receive_byte(self):
        deadline = time.monotonic() + 1
        while not self.read(0x02020098) & 1:
            self.assertLess(time.monotonic(), deadline, "UART receive timeout")
            time.sleep(0.001)
        return self.read(0x02020000) & 0xff

    def assert_input_discarded(self, value):
        self.uart.sendall(value)
        deadline = time.monotonic() + 0.05
        while time.monotonic() < deadline:
            self.assertFalse(self.read(0x02020098) & 1)
            time.sleep(0.001)

    def test_uart_rx_pinmux_and_daisy(self):
        self.write(0x02020084, 7)  # UART soft reset released, RX/TX enabled.
        self.write(0x020e0624, 3)
        self.uart.sendall(b"A")
        self.assertEqual(self.receive_byte(), ord("A"))
        self.write(0x020e0084, 5)  # Disconnecting TX does not disconnect RX.
        self.uart.sendall(b"B")
        self.assertEqual(self.receive_byte(), ord("B"))
        self.write(0x020e0088, 5)
        self.assert_input_discarded(b"C")
        self.write(0x02020084, 0)  # UART soft reset must not undo the pad mux.
        self.write(0x02020084, 7)
        self.assert_input_discarded(b"D")
        self.write(0x020e0088, 0)
        self.write(0x020e0624, 0)
        self.assert_input_discarded(b"E")
        self.write(0x020e0624, 3)
        self.uart.sendall(b"F")
        self.assertEqual(self.receive_byte(), ord("F"))

    def nand_command(self, command, address):
        descriptor, data = 0x80001000, 0x80002000
        payload = bytes((command, address))
        self.command(f"write {data:#x} 2 0x{payload.hex()}")
        words = struct.pack("<IIII", 0, (2 << 16) | (1 << 12) | 0x42,
                            data, (1 << 17) | 2)
        self.command(f"write {descriptor:#x} 16 0x{words.hex()}")
        self.write(0x01804110, descriptor)
        self.write(0x01804140, 1)

    def test_onfi_geometry_and_crc(self):
        self.nand_command(0x90, 0x20)
        self.assertEqual(bytes(self.read(0x018060a0) for _ in range(4)), b"ONFI")
        self.nand_command(0xec, 0)
        pages = bytes(self.read(0x018060a0) for _ in range(768))
        page = pages[:256]
        self.assertEqual(page, pages[256:512])
        self.assertEqual(page, pages[512:])
        self.assertEqual(struct.unpack_from("<I", page, 80)[0], 2048)
        self.assertEqual(struct.unpack_from("<H", page, 84)[0], 64)
        self.assertEqual(struct.unpack_from("<II", page, 92), (64, 4096))
        self.assertEqual(page[112], 4)
        crc = 0x4f4e
        for value in page[:254]:
            crc ^= value << 8
            for _ in range(8):
                crc = ((crc << 1) ^ (0x8005 if crc & 0x8000 else 0)) & 0xffff
        self.assertEqual(struct.unpack_from("<H", page, 254)[0], crc)
        self.write(0x01806060, 0x1234)
        self.monitor("system_reset")
        self.assertEqual(self.read(0x01806060), 0)


if __name__ == "__main__":
    unittest.main()
