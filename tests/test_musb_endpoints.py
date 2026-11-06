"""MUSB must NAK bulk traffic until an endpoint has a maximum packet size."""
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import unittest

BASE = 0x480ab000


@unittest.skipUnless(os.environ.get("QEMU_SYSTEM_ARM"), "set QEMU_SYSTEM_ARM")
class MusbEndpointTests(unittest.TestCase):
    def test_bulk_endpoint_configuration(self):
        with tempfile.TemporaryDirectory(prefix="musb-qtest-") as directory:
            root = Path(directory)
            with subprocess.Popen([
                os.environ["QEMU_SYSTEM_ARM"], "-M", "j49", "-accel", "qtest",
                "-display", "none", "-nodefaults",
                "-qtest", f"unix:{root}/qtest,server=on,wait=off",
                "-chardev", f"socket,id=host,path={root}/usb,server=on,wait=off",
                "-global", "omap3-musb.hostchar=host",
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as proc:
                try:
                    streams = []
                    sockets = []
                    for name in ("qtest", "usb"):
                        deadline = time.monotonic() + 5
                        while not (root / name).exists():
                            self.assertIsNone(proc.poll())
                            self.assertLess(time.monotonic(), deadline)
                            time.sleep(0.01)
                        sock = socket.socket(socket.AF_UNIX)
                        sockets.append(sock)
                        sock.settimeout(5)
                        sock.connect(str(root / name))
                        streams.append(sock.makefile("rwb", buffering=0))
                    qt, usb = streams

                    def command(stream, text):
                        stream.write(text.encode() + b"\n")
                        return stream.readline().decode().strip()

                    def write(offset, value, size="w"):
                        self.assertEqual(command(qt, f"write{size} {BASE+offset:#x} {value:#x}"), "OK")

                    write(1, 0x40, "b")
                    self.assertEqual(usb.readline(), b"CONNECT\n")
                    self.assertEqual(command(usb, "TOKENOUT 1 aabb"), "TOKEN 1 OUT NAK")
                    write(0x114, 512)
                    packet = bytes(range(256)) + b"x" * 35
                    self.assertEqual(command(usb, "TOKENOUT 1 " + packet.hex()), "TOKEN 1 OUT ACK 291")
                    self.assertEqual(int(command(qt, f"readw {BASE+0x118:#x}").split()[1], 16), 291)
                    self.assertEqual(int(command(qt, f"readl {BASE+0x24:#x}").split()[1], 16), 0x03020100)
                    write(0x116, 0)
                    write(0x24, 0x44332211, "l")
                    write(0x112, 1)
                    self.assertEqual(command(usb, "TOKENIN 1 512"), "TOKEN 1 IN NAK")
                    write(0x110, 512)
                    self.assertEqual(command(usb, "TOKENIN 1 512"), "TOKEN 1 IN ACK 11223344")
                finally:
                    for stream in streams:
                        stream.close()
                    for sock in sockets:
                        sock.close()
                    proc.terminate()
                    proc.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
