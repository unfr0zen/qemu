"""Onyx raw U-Boot environment seeding, without firmware or real dump data."""
import importlib.util
from pathlib import Path
import struct
import tempfile
import unittest


spec = importlib.util.spec_from_file_location(
    "onyx_env", Path(__file__).resolve().parents[1] / "boards/onyx/onyx_env.py")
onyx_env = importlib.util.module_from_spec(spec)
spec.loader.exec_module(onyx_env)


class OnyxEnvironmentTests(unittest.TestCase):
    def test_environment_crc_and_generation(self):
        values = {b"bootside": b"1", b"brightness": b"512", b"nlmodel": b"Display-4.x"}
        data = onyx_env.encode_environment(values, 7)
        self.assertEqual(len(data), 65536)
        self.assertEqual(onyx_env.environment(data), (7, values))
        damaged = bytearray(data)
        damaged[-1] ^= 1
        with self.assertRaisesRegex(ValueError, "CRC"):
            onyx_env.environment(damaged)

    def test_source_size(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "short.bin"
            source.write_bytes(bytes(64))
            with self.assertRaisesRegex(ValueError, "512 MiB logical"):
                onyx_env.seed(source, Path(directory) / "output.bin", source)

    def uboot_image(self):
        image = bytearray(0x1000)
        image[:4] = b"\xd1\x00\x20\x40"
        struct.pack_into("<I", image, 4, 0x807ff800)
        struct.pack_into("<I", image, 16, 0x807ff420)
        struct.pack_into("<I", image, 20, 0x807ff400)
        struct.pack_into("<III", image, 32, 0x807ff000, 0x1400, 0)
        return bytes(image)

    def test_uboot_can_reserve_unstored_csf_padding(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "uboot.imx"
            image = self.uboot_image()[:0x800]
            path.write_bytes(image)
            self.assertEqual(onyx_env.read_uboot(path), image)

    def test_reject_invalid_uboot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "uboot.imx"
            path.write_bytes(bytes(64))
            with self.assertRaisesRegex(ValueError, "IVT-wrapped"):
                onyx_env.read_uboot(path)
            image = bytearray(self.uboot_image())
            struct.pack_into("<I", image, 16, 0x807ff3ff)
            path.write_bytes(image)
            with self.assertRaisesRegex(ValueError, "boot-data pointer"):
                onyx_env.read_uboot(path)

    def template(self):
        block = bytearray(b"\xff" * onyx_env.PEB_SIZE)
        ec, vid = bytearray(64), bytearray(64)
        ec[:5] = b"UBI#\x01"
        struct.pack_into(">II", ec, 16, 0x800, 0x1000)
        struct.pack_into(">I", ec, 60, onyx_env.ubi_crc(ec[:60]))
        vid[:6] = b"UBI!\x01\x01"
        block[:64] = ec
        block[0x800:0x840] = vid
        return bytes(block)

    def record(self, name):
        record = bytearray(172)
        struct.pack_into(">III", record, 0, 9, 1, 0)
        record[12] = 1
        struct.pack_into(">H", record, 14, len(name))
        record[16:16 + len(name)] = name
        struct.pack_into(">I", record, 168, onyx_env.ubi_crc(record[:168]))
        return record

    def test_seed_copies_selected_volumes_without_editing_filesystems(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = (Path(directory) / name for name in ("source.bin", "onyx.bin"))
            template = self.template()
            table = b"".join(self.record(name) for name in (
                b"env0", b"env1", b"root0", b"root1", b"system-config", b"user-config"))
            source_values = {b"brightness": b"1883", b"nlmodel": b"Display-3.4",
                             b"wifi_password": b"do-not-copy", b"serial": b"do-not-copy"}
            blocks = [onyx_env.seed_block(template, onyx_env.LAYOUT_VOLUME, 0, 1, table)]
            blocks.extend(onyx_env.seed_block(
                template, i, 0, i + 2, onyx_env.encode_environment(source_values, 10 - i))
                for i in range(2))
            filesystem_blocks = {volume: onyx_env.seed_block(
                template, volume, 0, volume + 2, f"filesystem-volume-{volume}".encode())
                for volume in (2, 3, 4)}
            blocks.extend(filesystem_blocks.values())
            blocks.append(onyx_env.seed_block(template, 5, 0, 7, b"private-user-data"))
            with source.open("wb") as stream:
                stream.truncate(onyx_env.NAND_SIZE)
                stream.seek(onyx_env.UBI_START)
                for block in blocks:
                    stream.write(block)
            uboot = Path(directory) / "uboot.imx"
            uboot.write_bytes(self.uboot_image())
            before = source.stat().st_mtime_ns
            values = onyx_env.seed(source, output, uboot)
            self.assertEqual(source.stat().st_mtime_ns, before)
            self.assertEqual(values, {b"bootside": b"0", b"brightness": b"1883",
                                      b"nlmodel": b"Display-4.x"})
            self.assertEqual(output.stat().st_size, onyx_env.NAND_SIZE)
            with output.open("rb") as stream:
                for offset in (0x100400, 0x280400):
                    stream.seek(offset)
                    self.assertEqual(stream.read(0x1000), self.uboot_image())
                mapped = onyx_env.read_headers(stream)
                self.assertEqual(set(mapped), {(onyx_env.LAYOUT_VOLUME, 0),
                                               (onyx_env.LAYOUT_VOLUME, 1),
                                               (0, 0), (1, 0), (2, 0), (3, 0), (4, 0)})
                for volume, original in filesystem_blocks.items():
                    copied = onyx_env.read_block(stream, mapped[volume, 0][1])
                    self.assertEqual(copied, original)
                for volume in range(2):
                    block = onyx_env.read_block(stream, mapped[volume, 0][1])
                    self.assertEqual(onyx_env.environment(block[0x1000:]), (1 - volume, values))
                    self.assertNotIn(b"do-not-copy", block)
                layout = onyx_env.read_block(stream, mapped[onyx_env.LAYOUT_VOLUME, 0][1])
                self.assertNotIn(b"user-config", layout)
            with self.assertRaises(FileExistsError):
                onyx_env.seed(source, output, uboot)


if __name__ == "__main__":
    unittest.main()
