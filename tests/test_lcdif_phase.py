"""LCDIF latches queued buffers on its 60 Hz frame clock."""
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import unittest

BASE = 0x02220000
RUN = 1 | (1 << 17)


@unittest.skipUnless(os.environ.get("QEMU_SYSTEM_ARM"), "set QEMU_SYSTEM_ARM")
class LcdifPhaseTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="lcdif-qtest-")
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "qtest"
        self.proc = subprocess.Popen([
            os.environ["QEMU_SYSTEM_ARM"], "-M", "diamond3", "-accel", "qtest",
            "-display", "none", "-nodefaults",
            "-qtest", f"unix:{path},server=on,wait=off",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop)
        deadline = time.monotonic() + 5
        while not path.exists():
            self.assertIsNone(self.proc.poll(), "QEMU startup failed")
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
        sock = socket.socket(socket.AF_UNIX)
        self.addCleanup(sock.close)
        sock.settimeout(5)
        sock.connect(str(path))
        self.stream = sock.makefile("rwb", buffering=0)
        self.addCleanup(self.stream.close)
        self.command("writel 0x2220050 0x80001000")
        self.command(f"writel {BASE + 4:#x} {RUN:#x}")

    def stop(self):
        self.proc.terminate()
        self.proc.wait(timeout=5)

    def command(self, command):
        self.stream.write(command.encode() + b"\n")
        reply = self.stream.readline().decode().split()
        self.assertEqual(reply[0], "OK", reply)
        return reply

    def current(self):
        return int(self.command(f"readl {BASE + 0x40:#x}")[1], 16)

    def test_frame_cadence(self):
        self.command("clock_step 25000000")
        self.assertEqual(self.current(), 0x80001000)
        self.command(f"writel {BASE + 0x50:#x} 0x80002000")
        self.command("clock_step 9000000")
        self.assertEqual(self.current(), 0x80002000)
        self.command(f"writel {BASE + 0x50:#x} 0x80003000")
        self.command("clock_step 11000000")
        self.assertEqual(self.current(), 0x80002000)
        self.command("clock_step 6000000")
        self.assertEqual(self.current(), 0x80003000)

    def test_restart_starts_a_full_frame(self):
        self.command("clock_step 25000000")
        self.command(f"writel {BASE + 8:#x} 1")
        self.command("clock_step 10000000")
        self.command(f"writel {BASE + 0x50:#x} 0x80002000")
        self.command(f"writel {BASE + 4:#x} 1")
        self.command("clock_step 15000000")
        self.assertEqual(self.current(), 0x80001000)
        self.command("clock_step 2000000")
        self.assertEqual(self.current(), 0x80002000)


if __name__ == "__main__":
    unittest.main()
