"""Onyx ROM loading, IVT validation and optional stock U-Boot smoke tests."""
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest


QEMU = os.environ.get("QEMU_SYSTEM_ARM")
UBOOT = os.environ.get("ONYX_UBOOT")
NAND = os.environ.get("ONYX_NAND")


@unittest.skipUnless(QEMU, "set QEMU_SYSTEM_ARM to a patched QEMU")
class OnyxBootTests(unittest.TestCase):
    def command(self, firmware=None, nand=None):
        command = [QEMU, "-M", "onyx", "-display", "none", "-serial", "stdio",
                   "-monitor", "none", "-watchdog-action", "none"]
        if firmware:
            command += ["-bios", str(firmware)]
        if nand:
            command += ["-drive", f"if=none,id=nand,format=raw,file={nand},snapshot=on"]
        return command

    def capture(self, command, timeout):
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              stdin=subprocess.DEVNULL) as process:
            try:
                output, _ = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                output, _ = process.communicate()
        return output

    def reject(self, image, diagnostic):
        with tempfile.TemporaryDirectory() as directory:
            firmware = Path(directory) / "test.imx"
            firmware.write_bytes(image)
            result = subprocess.run(
                self.command(firmware), capture_output=True, timeout=10,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(diagnostic, result.stderr.decode())

    def test_reject_raw_image(self):
        self.reject(bytes(64), "requires an i.MX IVT image")

    def test_reject_outside_ddr(self):
        image = bytearray(64)
        image[:4] = b"\xd1\x00\x20\x40"
        struct.pack_into("<I", image, 4, 0x90000020)
        struct.pack_into("<I", image, 20, 0x90000000)
        self.reject(image, "firmware loads outside DDR")

    def test_reject_entry_outside_image(self):
        image = bytearray(64)
        image[:4] = b"\xd1\x00\x20\x40"
        struct.pack_into("<I", image, 4, 0x80801000)
        struct.pack_into("<I", image, 20, 0x80800000)
        self.reject(image, "entry outside image")

    def test_reject_nand_without_ivt(self):
        with tempfile.TemporaryDirectory() as directory:
            nand = Path(directory) / "nand.bin"
            with nand.open("wb") as stream:
                stream.truncate(2 * 1024 * 1024)
            result = subprocess.run(self.command(nand=nand), capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"NAND has no valid U-Boot IVT", result.stderr)

    def test_nand_rom_loads_and_enters_payload(self):
        image = bytearray(0x2000)
        image[0x400:0x404] = b"\xd1\x00\x20\x40"
        struct.pack_into("<I", image, 0x404, 0x80800000)
        struct.pack_into("<I", image, 0x410, 0x807ff420)
        struct.pack_into("<I", image, 0x414, 0x807ff400)
        struct.pack_into("<III", image, 0x420, 0x807ff000, len(image), 0)
        # ARM: load UART1 UTXD address, transmit 'X', loop forever.
        struct.pack_into("<IIIII", image, 0x1000,
                         0xe59f0008, 0xe3a01058, 0xe5801000, 0xeafffffe, 0x02020040)
        with tempfile.TemporaryDirectory() as directory:
            nand = Path(directory) / "nand.bin"
            with nand.open("wb") as stream:
                stream.truncate(2 * 1024 * 1024)
                stream.seek(0x100000)
                stream.write(image)
            self.assertEqual(self.capture(self.command(nand=nand), 0.5), b"X")

    def check_stock_uart(self, output, has_volumes=False):
        self.assertIn(b"U-Boot 2015.04", output)
        for line in (
            b"Freescale i.MX6UL rev1.2 at 396 MHz",
            b"Temperature 49 C", b"Board: Onyx", b"DRAM:  128 MiB",
            b"NAND:  512 MiB", b"Using default environment",
            b"In:    serial", b"Out:   serial", b"Err:   serial",
            b"UID:   51454d5500000001",
            b"Unlocking token not present or invalid (-1)",
        ):
            self.assertIn(line, output)
        if has_volumes:
            for warning in (b"Volume env0 not found", b"Volume env1 not found", b"bad CRC",
                            b"Error reading superblock", b"Could not mount ubi0"):
                self.assertNotIn(warning, output)
        self.assertNotIn(b"Normal Boot", output)
        self.assertNotIn(b"Loading file", output)

    @unittest.skipUnless(UBOOT, "set ONYX_UBOOT to a stock Onyx .imx image")
    def test_stock_uboot_uart(self):
        output = self.capture(self.command(UBOOT, NAND), 20 if NAND else 5)
        self.check_stock_uart(output, bool(NAND))

    @unittest.skipUnless(NAND, "set ONYX_NAND to a working Onyx NAND with embedded U-Boot")
    def test_stock_nand_uart(self):
        output = self.capture(self.command(nand=NAND), 20)
        self.check_stock_uart(output, True)


if __name__ == "__main__":
    unittest.main()
