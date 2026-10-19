"""Exercise UDC tokens and guest DMA without firmware, through QEMU qtest."""
import os
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


@unittest.skipUnless(os.environ.get("QEMU_SYSTEM_ARM"), "set QEMU_SYSTEM_ARM to a patched QEMU")
class UdcTest(unittest.TestCase):
    BASE = 0x02184000
    QH = 0x80001000
    TD = 0x80002000
    DATA = 0x80003000

    def setUp(self):
        self.addCleanup(self.cleanup)
        self.directory = tempfile.TemporaryDirectory(prefix="udc-qtest-")
        self.root = Path(self.directory.name)
        self.stderr = (self.root / "stderr").open("w+")
        self.sockets = []
        self.streams = []
        self.proc = subprocess.Popen([
            os.environ["QEMU_SYSTEM_ARM"], "-M", "flintstone,usb0-hostchar=usb",
            "-accel", "qtest", "-display", "none", "-nodefaults",
            "-qtest", f"unix:{self.root}/qtest,server=on,wait=off",
            "-chardev", f"socket,id=usb,path={self.root}/bridge,server=on,wait=off",
        ], stdout=subprocess.DEVNULL, stderr=self.stderr)
        for _ in range(250):
            if (self.root / "qtest").exists() and (self.root / "bridge").exists():
                break
            if self.proc.poll() is not None:
                self.fail((self.root / "stderr").read_text())
            time.sleep(0.02)
        for name in ("qtest", "bridge"):
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sockets.append(sock)
            sock.settimeout(3)
            sock.connect(str(self.root / name))
            self.streams.append(sock.makefile("rwb", buffering=0))
        self.qtest, self.bridge = self.streams
        self.write(self.BASE + 0x1a8, 2)
        self.write(self.BASE + 0x140, 1)
        self.assertEqual(self.bridge.readline(), b"CONNECT\n")
        self.write(self.BASE + 0x158, self.QH)
        self.write(self.BASE + 0x1c4, 0x00800080)

    def cleanup(self):
        if hasattr(self, "proc"):
            self.proc.terminate()
            self.proc.wait(timeout=5)
        for stream in getattr(self, "streams", []):
            stream.close()
        for sock in getattr(self, "sockets", []):
            sock.close()
        self.stderr.close()
        self.directory.cleanup()

    def command(self, text):
        self.qtest.write((text + "\n").encode())
        reply = self.qtest.readline().decode().strip()
        self.assertTrue(reply.startswith("OK"), reply)
        return reply

    def write(self, addr, value):
        self.command(f"writel {addr:#x} {value:#x}")

    def read(self, addr):
        return int(self.command(f"readl {addr:#x}").split()[1], 16)

    def token(self, text, expected):
        self.bridge.write((text + "\n").encode())
        self.assertEqual(self.bridge.readline().decode().strip(), expected)

    def prime(self, ep, direction, length, payload=b"", next_td=1):
        qh = self.QH + (ep * 2 + direction) * 64
        self.write(qh, 512 << 16)
        self.write(qh + 8, self.TD)
        self.write(self.TD, next_td)
        self.write(self.TD + 4, (length << 16) | 0x8080)
        self.write(self.TD + 8, self.DATA)
        if payload:
            self.command(f"write {self.DATA:#x} {len(payload)} 0x{payload.hex()}")
        self.write(self.BASE + 0x1b0, 1 << (ep + 16 * direction))

    def test_acknowledged_tokens(self):
        self.token("TOKENIN 1 512", "TOKEN 1 IN NAK")
        self.token("TOKENOUT 1 6162", "TOKEN 1 OUT NAK 0")
        self.write(self.BASE + 0x1c4, 0x00810081)
        self.token("TOKENIN 1 512", "TOKEN 1 IN STALL")
        self.token("TOKENOUT 1 6162", "TOKEN 1 OUT STALL 0")
        self.write(self.BASE + 0x1c4, 0x00800080)
        self.prime(1, 1, 3, b"abc")
        self.token("TOKENIN 1 512", "TOKEN 1 IN ACK 616263")
        self.assertTrue(self.read(self.BASE + 0x1bc) & (1 << 17))
        self.token("TOKENIN 1 512", "TOKEN 1 IN NAK")
        self.prime(1, 1, 0)
        self.token("TOKENIN 1 512", "TOKEN 1 IN ACK")
        self.prime(1, 0, 512)
        self.token("TOKENOUT 1 616263", "TOKEN 1 OUT ACK 3")
        self.assertEqual(self.command(f"read {self.DATA:#x} 3"), "OK 0x616263")
        self.assertFalse(self.read(self.TD + 4) & 0x80)
        self.prime(0, 1, 18)
        self.token("TOKENIN 0 0", "TOKEN 0 IN NAK")
        # An IOC ending a short response must leave the next request intact.
        self.prime(1, 1, 3, b"abc", next_td=self.TD + 64)
        self.write(self.TD + 64, 1)
        self.write(self.TD + 68, (3 << 16) | 0x8080)
        self.write(self.TD + 72, self.DATA + 4096)
        self.command(f"write {self.DATA + 4096:#x} 3 0x646566")
        self.token("TOKENIN 1 512", "TOKEN 1 IN ACK 616263")
        self.token("TOKENIN 1 512", "TOKEN 1 IN ACK 646566")


if __name__ == "__main__":
    unittest.main()
