"""Render A8 quads through the GPU command front end without firmware."""
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest


@unittest.skipUnless(os.environ.get("QEMU_SYSTEM_ARM"), "set QEMU_SYSTEM_ARM")
class VivanteRenderer:
    host_gpu = False
    request_host_gpu = False
    render_node = None

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="vivante-qtest-")
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "qtest"
        qmp_path = Path(directory.name) / "qmp"
        self.log = tempfile.TemporaryFile()
        self.addCleanup(self.log.close)
        gpu_args = []
        node = self.render_node or os.environ.get("VIVANTE_RENDER_NODE")
        if node:
            gpu_args = ["-global", f"vivante-gc.host-render-node={node}"]
        enable_host = self.host_gpu or self.request_host_gpu
        self.proc = subprocess.Popen([
            os.environ["QEMU_SYSTEM_ARM"], "-M", "diamond3", "-accel", "qtest",
            "-display", "none", "-nodefaults", "-global",
            f"vivante-gc.host-gpu={'on' if enable_host else 'off'}",
            "-qtest", f"unix:{path},server=on,wait=off",
            "-qmp", f"unix:{qmp_path},server=on,wait=off",
            "-qtest-log", "/dev/null",
        ] + gpu_args, stdout=self.log, stderr=self.log)
        self.addCleanup(self.stop)
        deadline = time.monotonic() + 5
        while not path.exists() or not qmp_path.exists():
            self.assertIsNone(self.proc.poll(), "QEMU startup failed")
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
        self.sock = socket.socket(socket.AF_UNIX)
        self.addCleanup(self.sock.close)
        self.sock.settimeout(5)
        self.sock.connect(str(path))
        self.stream = self.sock.makefile("rb")
        self.addCleanup(self.stream.close)
        self.qmp_sock = socket.socket(socket.AF_UNIX)
        self.addCleanup(self.qmp_sock.close)
        self.qmp_sock.settimeout(5)
        self.qmp_sock.connect(str(qmp_path))
        self.qmp_stream = self.qmp_sock.makefile("rb")
        self.addCleanup(self.qmp_stream.close)
        self.assertIn("QMP", json.loads(self.qmp_stream.readline()))
        self.qmp("qmp_capabilities")
        devices = self.qmp("qom-list", path="/machine/unattached")
        self.gpu = "/machine/unattached/" + next(
            device["name"] for device in devices
            if device["type"] == "child<vivante-gc>")
        self.draws = 0
        self.command("writel 0x1800418 0x80000000")
        self.states = {
            # Position, colour and texcoord; END is relative to each run.
            0x180: 0x10000008, 0x181: 0x20100008, 0x182: 0x10400088,
            0x193: 0x01000000, 0x194: 80,
            # A no-op VS forwards the three input registers to its outputs.
            0x200: 1, 0x201: 3, 0x202: 3,
            0x204: 0x010200, 0x208: 0x020100,
            0x280: 32 << 16, 0x281: 32 << 16, 0x282: 0x3f000000,
            0x283: 32 << 16, 0x284: 32 << 16, 0x285: 0x3f000000,
            0x28a: 1 << 4, 0x28d: 1 << 16,
            0x302: 64 << 16, 0x303: 64 << 16,
            0x400: 2, 0x401: 1, 0x402: 3,
            0x500: 1 << 24, 0x50a: 0x05110541,
            0x50b: 0xf06, 0x50c: 0x02000000, 0x50d: 64 * 4,
            # A8, clamp, linear filtering, no mips, identity swizzle.
            0x800: 0x83152, 0x810: (8 << 16) | 8,
            0x870: 1 << 26, 0x900: 0x03000000,
        }
        # TEXLD t1, t1.xy; MOV t1.xyz, t2.xyz (white RGB, atlas alpha).
        instructions = [
            0x18 | (1 << 12) | (1 << 16) | (15 << 23) |
            (0xe4 << 35) | (1 << 43) | (1 << 44) | (0xe4 << 54),
            9 | (1 << 12) | (1 << 16) | (7 << 23) |
            (1 << 99) | (2 << 100) | (0xe4 << 110),
        ]
        for i, instruction in enumerate(instructions):
            for j, word in enumerate(struct.unpack(
                    "<4I", instruction.to_bytes(16, "little"))):
                self.states[0x1800 + i * 4 + j] = word
        vertices = bytearray(80 * 6)
        for i, (x, y, u, v) in enumerate([
            (-1, -1, 0, 0), (-1, 1, 0, 1), (1, -1, 1, 0),
            (1, -1, 1, 0), (-1, 1, 0, 1), (1, 1, 1, 1),
        ]):
            struct.pack_into("<4f", vertices, i * 80, x, y, 0, 1)
            struct.pack_into("<4f", vertices, i * 80 + 16, 1, 1, 1, 1)
            struct.pack_into("<4f", vertices, i * 80 + 64, u, v, 0, 1)
        self.write(0x81000000, vertices)
        self.atlas = bytearray(128)
        for y in range(8):
            for x in range(8):
                self.atlas[self.offset(x, y, 16, 1)] = 255 if x >= 4 else 0
        self.write(0x83000000, self.atlas)

    def stop(self):
        self.proc.terminate()
        self.proc.wait(timeout=5)

    def command(self, command):
        self.sock.sendall(command.encode() + b"\n")
        reply = self.stream.readline().decode().split()
        self.assertEqual(reply[0], "OK", reply)
        return reply

    def qmp(self, execute, **arguments):
        self.qmp_sock.sendall(json.dumps({
            "execute": execute, "arguments": arguments,
        }).encode() + b"\n")
        while True:
            reply = json.loads(self.qmp_stream.readline())
            if "event" not in reply:
                self.assertNotIn("error", reply)
                return reply["return"]

    def write(self, address, data):
        for off in range(0, len(data), 1024):
            chunk = data[off:off + 1024]
            self.command(f"write {address + off:#x} {len(chunk)} 0x{chunk.hex()}")

    @staticmethod
    def offset(x, y, stride, bpp):
        return (y // 4) * stride * 4 + (x // 4 * 16 + y % 4 * 4 + x % 4) * bpp

    def draw(self):
        words = []
        for reg, value in self.states.items():
            words.extend([(1 << 27) | (1 << 16) | reg, value])
        words.extend([5 << 27, 4, 0, 2])  # Two independent triangles.
        words.extend([(1 << 27) | (1 << 16) | 0xe01, 0x40, 2 << 27, 0])
        self.write(0x84000000, struct.pack(f"<{len(words)}I", *words))
        self.command("writel 0x1800654 0x04000000")
        self.command("writel 0x1800658 0x10000")
        self.command("clock_step 10000000")
        self.draws += 1
        accelerated = self.host_gpu and (self.states[0x810] & 0xffff) <= 8192
        counter = "host-draws" if accelerated else "software-draws"
        self.assertEqual(self.qmp("qom-get", path=self.gpu, property=counter),
                         self.draws)
        if self.host_gpu:
            self.log.seek(0)
            log = self.log.read().decode()
            self.assertIn("vivante-gc: host GPU", log)
            self.assertNotIn("texture unloadable", log)
            expected = os.environ.get("VIVANTE_EXPECT_RENDERER")
            if expected:
                self.assertIn(expected, log)

    def pixel(self, x, y):
        address = 0x80000000 + self.states[0x50c] + self.offset(x, y, 256, 4)
        return int(self.command(f"readl {address:#x}")[1], 16)

    def check_quad(self):
        for y in (8, 48, 56):
            self.assertEqual(self.pixel(8, y), 0)
            self.assertEqual(self.pixel(56, y), 0xffffffff)


class VivanteTests(VivanteRenderer, unittest.TestCase):
    def test_a8_and_final_vertex_texcoord(self):
        self.draw()
        self.check_quad()

    def test_cached_texture_on_new_target(self):
        self.draw()
        self.states[0x50c] += 0x10000
        self.draw()
        self.check_quad()

    def test_atlas_update(self):
        self.draw()
        self.write(0x83000000, bytes(255 - a for a in self.atlas))
        self.states[0x50c] += 0x10000
        self.draw()
        self.assertEqual(self.pixel(8, 48), 0xffffffff)
        self.assertEqual(self.pixel(56, 48), 0)

    def test_partial_atlas_rows(self):
        for layout in ("tiled", "linear", "supertiled"):
            with self.subTest(layout=layout):
                stride = 64 if layout == "supertiled" else 16
                self.states[0x800] = 0x83152 | (3 << 20 if layout == "linear" else 0)
                self.states[0x870] = (2 if layout == "supertiled" else 1) << 26
                self.states[0xb00] = stride
                atlas = bytearray(stride * (64 if layout == "supertiled" else 8))
                self.write(0x83000000, atlas)
                self.states[0x50c] += 0x10000
                self.draw()
                # Change the top-left tile; leave the other rows untouched.
                for y in range(4):
                    for x in range(4):
                        off = y * stride + x if layout == "linear" else y * 4 + x
                        atlas[off] = 255
                self.write(0x83000000, atlas)
                self.draw()
                self.assertEqual(self.pixel(8, 8), 0xffffffff)
                self.assertEqual(self.pixel(56, 8), 0)
                self.assertEqual(self.pixel(8, 56), 0)
                for y in range(4, 8):
                    for x in range(4, 8):
                        if layout == "linear":
                            off = y * stride + x
                        elif layout == "supertiled":
                            off = 48 + (y % 4) * 4 + x % 4
                        else:
                            off = self.offset(x, y, stride, 1)
                        atlas[off] = 255
                self.write(0x83000000, atlas)
                self.states[0x50c] += 0x10000
                self.draw()
                self.assertEqual(self.pixel(8, 8), 0xffffffff)
                self.assertEqual(self.pixel(56, 56), 0xffffffff)
                self.assertEqual(self.pixel(56, 8), 0)
                self.write(0x83000000, bytes(len(atlas)))
                self.states[0x50c] += 0x10000
                self.draw()
                self.assertEqual(self.pixel(8, 8), 0)

    def test_a8_alpha_swizzles(self):
        for alpha, expected in ((3, 0xffffffff), (5, 0xffffffff), (4, 0), (0, 0)):
            self.states[0x870] = (1 << 26) | ((alpha << 12 | 0x210) << 8)
            self.states[0x50c] += 0x10000
            self.draw()
            self.assertEqual(self.pixel(56, 48), expected)
            self.assertEqual(self.pixel(8, 48), 0xffffffff if alpha == 5 else 0)

    def test_a8_mipmap_update(self):
        self.states[0x1800] = (self.states[0x1800] & ~63) | 0x1b  # TEXLDL
        self.states[0x800] |= 1 << 9
        self.states[0x830] = (32 << 11) | (32 << 1)  # Clamp to mip level one.
        self.states[0x910] = 0x03100000
        self.write(0x83100000, bytes([255]) * 64)
        self.draw()
        self.assertEqual(self.pixel(8, 48), 0xffffffff)
        self.assertEqual(self.pixel(56, 48), 0xffffffff)
        mip = bytearray(64)
        for y in range(4):
            for x in range(2, 4):
                mip[y * 4 + x] = 255
        self.write(0x83100000, mip)
        self.states[0x50c] += 0x10000
        self.draw()
        self.assertEqual(self.pixel(8, 48), 0)
        self.assertEqual(self.pixel(56, 48), 0xffffffff)

    def test_texture_cache_format_reuse(self):
        self.draw()
        # Fill the 256-entry cache, then reuse its first A8 object for ARGB.
        for i in range(1, 256):
            self.states[0x900] = 0x03000000 + i * 4096
            self.draw()
        self.states[0x800] = (0x83152 & ~(31 << 13)) | (7 << 13)
        self.states[0x900] += 4096
        self.states[0x50c] += 0x10000
        self.write(0x80000000 + self.states[0x900], bytes([0, 0, 0, 255]) * 128)
        self.draw()
        self.assertEqual(self.pixel(56, 48), 0xffffffff)
        self.assertEqual(self.pixel(8, 48), 0xffffffff)

    def stencil_target(self, top):
        """D24S8 depth/stencil at 0x85000000: stencil `top` in rows 0-31."""
        self.states.update({0x500: 1 << 4, 0x504: 0x05000000, 0x505: 64 * 4})
        stencil = bytearray(64 * 64 * 4)
        for y in range(32):
            for x in range(64):
                stencil[self.offset(x, y, 64 * 4, 4)] = top
        self.write(0x85000000, stencil)

    def stencil(self, x, y):
        address = 0x85000000 + self.offset(x, y, 64 * 4, 4)
        return int(self.command(f"readl {address:#x}")[1], 16) & 0xff

    def test_stencil_masks_cover_quad(self):
        self.stencil_target(1)
        # EQUAL against reference 1, all bits compared, nothing written.
        self.states.update({0x506: 2, 0x507: 1 | (1 << 8) | (0xff << 16)})
        self.draw()
        self.assertEqual(self.pixel(56, 8), 0xffffffff)
        self.assertEqual(self.pixel(56, 48), 0)

    def test_two_sided_stencil_ops(self):
        self.stencil_target(0)
        # ALWAYS; the quad faces front: INCR_WRAP there, DECR_WRAP on back.
        self.states.update({
            0x506: 7 | (6 << 4) | ((7 | (7 << 4)) << 16),
            0x507: 2 | (0xff << 16) | (0xff << 24),
            0x528: 0, 0x52e: 0xffff,
        })
        self.draw()
        self.assertEqual(self.stencil(56, 8), 1)
        self.assertEqual(self.stencil(8, 48), 1)
        self.states[0x506] = 7 | (7 << 4)  # Front DECR_WRAP: back to zero.
        self.states[0x50c] += 0x10000
        self.draw()
        self.assertEqual(self.stencil(56, 8), 0)

    def wide_atlas(self, width):
        self.states[0x810] = (4 << 16) | width
        stride = (width + 15) & ~15
        atlas = bytearray(stride * 4)
        for y in range(4):
            for x in range(width // 2, width):
                atlas[self.offset(x, y, stride, 1)] = 255
        self.write(0x83000000, atlas)
        self.draw()
        self.check_quad()

    def test_8192_wide_atlas(self):
        self.wide_atlas(8192)

    def test_oversized_texture_fallback(self):
        self.wide_atlas(65535)


@unittest.skipUnless(sys.platform == "darwin" or
                     os.environ.get("VIVANTE_HOST_TESTS") == "1",
                     "requires macOS or VIVANTE_HOST_TESTS=1 with EGL hardware")
class VivanteHostTests(VivanteTests):
    host_gpu = True


@unittest.skipUnless(sys.platform == "linux", "requires Linux EGL")
class VivanteEGLFallbackTests(VivanteRenderer, unittest.TestCase):
    request_host_gpu = True
    render_node = "/dev/null"

    def test_unavailable_render_node(self):
        self.draw()
        self.check_quad()
        self.assertEqual(self.qmp("qom-get", path=self.gpu,
                                  property="host-draws"), 0)
        self.log.seek(0)
        self.assertIn("using software", self.log.read().decode())


if __name__ == "__main__":
    unittest.main()
