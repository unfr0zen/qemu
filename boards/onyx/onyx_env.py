#!/usr/bin/env python3
"""Build an Onyx working NAND from a diamond3 logical NAND.

Seed env0/env1 with bootside, brightness and nlmodel=Display-4.x. Copy the
system-config, root0 and root1 volumes verbatim, including their per-unit
state and diamond3 firmware. No UBIFS filesystem contents are edited.
Embed the supplied Onyx U-Boot .imx image in the NAND boot partition.
The source is read-only and the output must not already exist.
"""
import argparse
from pathlib import Path
import struct
import zlib


NAND_SIZE = 512 * 1024 * 1024
PEB_SIZE = 128 * 1024
UBI_START = 4 * 1024 * 1024
UBI_END = 504 * 1024 * 1024
ENV_SIZE = 65536
RECORD_SIZE = 172
LAYOUT_VOLUME = 0x7fffefff
COPIED_VOLUMES = (b"system-config", b"root0", b"root1")


def ubi_crc(data):
    return zlib.crc32(data) ^ 0xffffffff


def read_headers(source):
    """Map the newest physical block for each UBI volume/logical block."""
    blocks = {}
    for offset in range(UBI_START, UBI_END, PEB_SIZE):
        source.seek(offset)
        ec = source.read(64)
        if ec[:4] != b"UBI#":
            continue
        if struct.unpack_from(">I", ec, 60)[0] != ubi_crc(ec[:60]):
            continue
        vid_offset, data_offset = struct.unpack_from(">II", ec, 16)
        if (vid_offset, data_offset) != (0x800, 0x1000):
            raise ValueError("source requires 2 KiB pages and 4 KiB UBI data offsets")
        source.seek(offset + vid_offset)
        vid = source.read(64)
        if vid[:4] != b"UBI!" or struct.unpack_from(">I", vid, 60)[0] != ubi_crc(vid[:60]):
            continue
        volume, lnum = struct.unpack_from(">II", vid, 8)
        sequence = struct.unpack_from(">Q", vid, 40)[0]
        key = volume, lnum
        if key not in blocks or sequence > blocks[key][0]:
            blocks[key] = sequence, offset
    return blocks


def read_block(source, offset):
    source.seek(offset)
    return source.read(PEB_SIZE)


def environment(data):
    env = data[:ENV_SIZE]
    if len(env) != ENV_SIZE or struct.unpack_from("<I", env)[0] != zlib.crc32(env[5:]):
        raise ValueError("environment CRC does not match the 64 KiB redundant format")
    pairs = dict(item.split(b"=", 1) for item in env[5:].split(b"\0\0", 1)[0].split(b"\0")
                 if b"=" in item)
    return env[4], pairs


def encode_environment(values, generation):
    payload = b"\0".join(key + b"=" + value for key, value in values.items()) + b"\0\0"
    payload = payload.ljust(ENV_SIZE - 5, b"\0")
    return struct.pack("<IB", zlib.crc32(payload), generation) + payload


def seed_block(template, volume, lnum, sequence, payload):
    """Reuse source geometry, with fresh VID metadata and a raw payload."""
    block = bytearray(template[:0x1000] + b"\xff" * (PEB_SIZE - 0x1000))
    vid = bytearray(block[0x800:0x840])
    vid[6] = 0  # Not a wear-levelling copy.
    struct.pack_into(">II", vid, 8, volume, lnum)
    struct.pack_into(">II", vid, 16, 0, 0)  # Dynamic volumes have no data-size/used-LEBs fields.
    struct.pack_into(">I", vid, 32, 0)
    struct.pack_into(">Q", vid, 40, sequence)
    struct.pack_into(">I", vid, 60, ubi_crc(vid[:60]))
    block[0x800:0x840] = vid
    block[0x1000:0x1000 + len(payload)] = payload
    return block


def read_uboot(path):
    image = path.read_bytes()
    if len(image) < 44 or image[:3] != b"\xd1\x00\x20" or image[3] not in (0x40, 0x41):
        raise ValueError("U-Boot must be an IVT-wrapped Onyx .imx image")
    entry, boot_data, address = (struct.unpack_from("<I", image, offset)[0]
                                 for offset in (4, 16, 20))
    offset = boot_data - address
    if offset < 32 or offset + 12 > len(image):
        raise ValueError("U-Boot boot-data pointer is outside the image")
    start, size, plugin = struct.unpack_from("<III", image, offset)
    if (address - start != 0x400 or plugin != 0 or size < 0x420 or size > 0x180000 or
            len(image) > size - 0x400 or len(image) > 0x17fc00):
        raise ValueError("unsupported Onyx NAND U-Boot image layout")
    if (start < 0x80000000 or start + size > 0x88000000 or
            not address <= entry <= address + len(image) - 4):
        raise ValueError("U-Boot loads outside Onyx RAM or enters outside its payload")
    return image


def write_uboot(output, image):
    for offset in (0x100400, 0x280400):
        output.seek(offset)
        output.write(image)


def seed(source_path, output_path, uboot_path):
    if source_path.stat().st_size != NAND_SIZE:
        raise ValueError("source must be a 512 MiB logical NAND, without OOB")
    image = read_uboot(uboot_path)
    with source_path.open("rb") as source:
        blocks = read_headers(source)
        layouts = [value for (volume, _), value in blocks.items() if volume == LAYOUT_VOLUME]
        if not layouts:
            raise ValueError("source has no valid UBI layout volume")
        layout = read_block(source, max(layouts)[1])
        records, copies, volume_ids = {}, {}, {}
        for volume in range(128):
            record = layout[0x1000 + volume * RECORD_SIZE:0x1000 + (volume + 1) * RECORD_SIZE]
            length = struct.unpack_from(">H", record, 14)[0]
            name = record[16:16 + length]
            if name not in (b"env0", b"env1") + COPIED_VOLUMES:
                continue
            if struct.unpack_from(">I", record, 168)[0] != ubi_crc(record[:168]):
                raise ValueError("source volume-table CRC is invalid")
            if record[12] != 1 or record[13] != 0:
                raise ValueError("source volume must be dynamic and not updating")
            records[name] = record
            volume_ids[name] = volume
            if name in (b"env0", b"env1") and (volume, 0) in blocks:
                block = read_block(source, blocks[volume, 0][1])
                try:
                    copies[name] = environment(block[0x1000:]), block
                except ValueError:
                    pass  # The other redundant copy may still be valid.
        if len(records) != 5 or not copies:
            raise ValueError("source needs env0, env1, system-config, root0, root1 "
                             "and at least one valid environment")
        names = list(copies)
        active = names[0]
        if len(names) == 2:
            a, b = (copies[name][0][0] for name in names)
            if (a == 255 and b == 0) or (not (a == 0 and b == 255) and b > a):
                active = names[1]
        _, original = copies[active][0]
        values = {
            b"bootside": original.get(b"bootside", b"0"),
            b"brightness": original.get(b"brightness", b"0"),
            b"nlmodel": b"Display-4.x",
        }
        if values[b"bootside"] not in (b"0", b"1"):
            raise ValueError("source bootside must be 0 or 1")
        if not values[b"brightness"].isdigit() or int(values[b"brightness"]) > 4095:
            raise ValueError("source brightness must be 0..4095")
        empty = bytes(168) + struct.pack(">I", ubi_crc(bytes(168)))
        table = bytearray(empty * 128)
        for name, record in records.items():
            start = volume_ids[name] * RECORD_SIZE
            table[start:start + RECORD_SIZE] = record
        template = copies[active][1]
        sequence = max(value[0] for value in blocks.values()) + 1
        output_blocks = [seed_block(layout, LAYOUT_VOLUME, i, sequence + i, table)
                         for i in range(2)]
        output_blocks.extend(seed_block(template, volume_ids[name], 0, sequence + i + 2,
                                        encode_environment(values, 1 - i))
                             for i, name in enumerate((b"env0", b"env1")))
        copied_ids = {volume_ids[name] for name in COPIED_VOLUMES}
        copied_offsets = [offset for (volume, _), (_, offset) in sorted(blocks.items())
                          if volume in copied_ids]
    with output_path.open("xb") as output, source_path.open("rb") as source:
        erased = b"\xff" * (1024 * 1024)
        for _ in range(512):
            output.write(erased)
        output.seek(UBI_START)
        for block in output_blocks:
            output.write(block)
        for offset in copied_offsets:
            output.write(read_block(source, offset))
        write_uboot(output, image)
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="diamond3 logical NAND; read-only")
    parser.add_argument("output", type=Path, help="new Onyx NAND; must not exist")
    parser.add_argument("--uboot", type=Path, required=True, help="Onyx U-Boot .imx to embed")
    args = parser.parse_args()
    try:
        values = seed(args.source, args.output, args.uboot)
    except (OSError, ValueError) as error:
        parser.exit(1, f"onyx-env: {error}\n")
    print("Created NAND with env0, env1, system-config, root0, root1: " +
          ", ".join(f"{key.decode()}={value.decode()}" for key, value in values.items()))


if __name__ == "__main__":
    main()
