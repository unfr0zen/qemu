"""Check Onyx development fuses, writable CAAM RAM and Linux peripherals."""
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import unittest


@unittest.skipUnless(os.environ.get('QEMU_SYSTEM_ARM'), 'set QEMU_SYSTEM_ARM')
class OnyxTests(unittest.TestCase):
    def test_board_registers(self):
        for machine, security in [('onyx', 2), ('onyx,secure-boot=off', 0)]:
            with self.subTest(machine=machine), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'qtest'
                proc = subprocess.Popen([
                    os.environ['QEMU_SYSTEM_ARM'], '-M', machine, '-accel', 'qtest',
                    '-display', 'none', '-nodefaults',
                    '-qtest', f'unix:{path},server=on,wait=off',
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                try:
                    deadline = time.monotonic() + 5
                    while not path.exists():
                        self.assertIsNone(proc.poll(), 'QEMU startup failed')
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(0.01)
                    with socket.socket(socket.AF_UNIX) as sock:
                        sock.settimeout(5)
                        sock.connect(str(path))
                        with sock.makefile('rwb', buffering=0) as stream:
                            def command(text):
                                stream.write(text.encode() + b'\n')
                                reply = stream.readline().decode().split()
                                self.assertEqual(reply[0], 'OK', reply)
                                return int(reply[1], 16) if len(reply) > 1 else None

                            self.assertEqual(command('readl 0x21bc460'), security)
                            command('writel 0x100000 0x12345678')
                            self.assertEqual(command('readl 0x100000'), 0x12345678)
                            self.assertEqual(command('readl 0x2184124') & 0x9f, 0x88)
                            command('writel 0x2184800 0x1234')
                            self.assertEqual(command('readl 0x2184800'), 0x1234)
                            command('writel 0x21c8050 0x80001000')
                            command('writel 0x21c8004 0x20001')
                            command('clock_step 17000000')
                            self.assertEqual(command('readl 0x21c8040'), 0x80001000)
                            self.assertTrue(command('readl 0x20a0008') & (1 << 8))
                finally:
                    proc.terminate()
                    proc.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
