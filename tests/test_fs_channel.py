"""Debugger framing, borrowed syscall context, and MCP failure recovery."""
import errno
from pathlib import Path
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ui"))
import fs_channel  # noqa: E402
from fs_channel import Debugger, FileChannel, LIMIT, Syscalls  # noqa: E402
import mcp_server  # noqa: E402
import ui  # noqa: E402


class DebuggerProtocolTest(unittest.TestCase):
    def setUp(self):
        self.left, self.right = socket.socketpair()
        self.addCleanup(self.left.close)
        self.addCleanup(self.right.close)
        self.debug = Debugger.__new__(Debugger)
        self.debug.sock = self.left
        self.debug.deadline = time.monotonic() + 2
        self.debug.buffer = b""
        self.debug.position = 0
        self.debug.running = True

    def packet(self, payload):
        self.right.sendall(b"$" + payload + b"#" + f"{sum(payload) % 256:02x}".encode())

    def test_escaping_and_run_length_encoding(self):
        self.packet(b"T05x:" + b"".join(b"}" + bytes((c ^ 32,)) for c in b"$#}*") + b"0* ")
        self.assertEqual(self.debug.receive(), b"T05x:$#}*0000")
        self.assertFalse(self.debug.running)
        self.assertEqual(self.right.recv(1), b"+")

    def test_bad_checksum_and_eof_are_reported(self):
        self.right.sendall(b"$T05#00")
        with self.assertRaisesRegex(OSError, "checksum"):
            self.debug.receive()
        self.right.close()
        with self.assertRaises(ConnectionError):
            self.debug.receive()

    def test_command_and_register_wire_format(self):
        self.packet(b"78563412")
        self.assertEqual(self.debug.register(3), 0x12345678)
        data = self.right.recv(100)
        self.assertTrue(data.startswith(b"$p3#a3"), data)
        self.packet(b"OK")
        self.debug.set_register(4, 0x87654321)
        self.assertTrue(self.right.recv(100).startswith(b"$P4=21436587#"))


class MemoryDebugger:
    def __init__(self):
        self.regs = list(range(16)) + [0x80010013]
        self.regs[13:16] = [0xc1001fa8, 0xc000f620, 0xc000f73c]
        self.memory = {}
        self.deadline = time.monotonic() + 30
        self.sock = Mock()
        self.running = False
        self.expect_ok = Mock()
        self.stop = Mock()
        self.return_values = []
        self.entered = []

    def read(self, address, size):
        return bytes(self.memory.get(address + i, 0) for i in range(size))

    def write(self, address, data):
        self.memory.update({address + i: value for i, value in enumerate(data)})

    def register(self, number):
        return self.regs[16 if number == 25 else number]

    def registers(self):
        return self.regs.copy()

    def set_registers(self, registers):
        self.regs = registers.copy()

    def breakpoint(self, address, enabled=True):
        pass

    def command(self, command):
        if command == "c":
            self.entered.append((self.regs.copy(), self.read(self.regs[13], 8)))
            result, sp = self.return_values.pop(0)
            self.regs[0], self.regs[13], self.regs[15] = result, sp, 0xc000f620
            self.regs[12] = 0xbad
            return b"T05"
        raise AssertionError(command)


class SyscallContextTest(unittest.TestCase):
    def setUp(self):
        self.debug = MemoryDebugger()
        self.calls = Syscalls(self.debug)
        self.calls.saved = self.debug.registers()
        self.calls.frame = bytes(range(80))
        self.calls.table = 0xc0020000
        self.debug.write(self.calls.saved[13], self.calls.frame)
        self.debug.write(self.calls.table + 192 * 4, struct.pack("<I", 0xc0060000))

    def test_six_arguments_other_task_returns_and_exact_restoration(self):
        original = self.debug.registers()
        self.debug.return_values = [(123, 0xc2001fa8), (0xa0000000, original[13])]
        self.assertEqual(self.calls.call(192, 0, 65536, 3, 0x8022, -1, 0), 0xa0000000)
        regs, args = self.debug.entered[0]
        self.assertEqual(regs[:6], [0, 65536, 3, 0x8022, 0xffffffff, 0])
        self.assertEqual(args, struct.pack("<II", 0xffffffff, 0))
        self.assertEqual(regs[15], 0xc0060000)
        self.calls.restore()
        self.assertEqual(self.debug.registers(), original)
        self.assertEqual(self.debug.read(original[13], 80), bytes(range(80)))

    def test_kernel_errno_still_allows_context_restoration(self):
        original = self.debug.registers()
        self.debug.return_values = [(0xfffffff2, original[13])]
        with self.assertRaises(OSError) as error:
            self.calls.call(192, 0, 65536, 3, 0x8022, -1, 0)
        self.assertEqual(error.exception.errno, errno.EFAULT)
        self.assertFalse(self.calls.pending)
        self.calls.restore()
        self.assertEqual(self.debug.registers(), original)

    def test_pending_call_cannot_restore_over_another_running_task(self):
        self.calls.pending = True
        self.debug.regs[13] = 0xc2000000
        with self.assertRaisesRegex(OSError, "must remain stopped"):
            self.calls.restore()
        self.assertEqual(self.debug.regs[13], 0xc2000000)


class FileChannelTest(unittest.TestCase):
    def setUp(self):
        self.channel = FileChannel("unused", "rw")
        self.debug = MemoryDebugger()
        self.calls = Mock(pending=False)
        self.calls.call.side_effect = lambda nr, *args: 0xa0000000 if nr == 192 else 0
        self.debug_patch = patch.object(fs_channel, "Debugger", return_value=self.debug)
        self.syscall_patch = patch.object(fs_channel, "Syscalls", return_value=self.calls)
        self.debug_factory = self.debug_patch.start()
        self.syscall_patch.start()
        self.addCleanup(self.debug_patch.stop)
        self.addCleanup(self.syscall_patch.stop)

    def test_invalid_requests_and_ro_never_attach_to_guest(self):
        for path, data, mode in (("relative", None, None), ("/nul\0path", None, None),
                                 ("/" + "a" * 4000, None, None),
                                 ("/file", b"x", "888"), ("/file", b"x", "4755"),
                                 ("/file", b"x", ""), ("/file", b"x" * (LIMIT + 1), None)):
            with self.subTest(path=path[:30], mode=mode), self.assertRaises(ValueError):
                self.channel.transfer(path, data, mode)
        self.channel.mode = "ro"
        with self.assertRaises(PermissionError):
            self.channel.transfer("/file", b"forbidden")
        self.debug_factory.assert_not_called()

    def test_regular_file_error_unmaps_restores_and_detaches(self):
        with patch.object(self.channel, "read_file", side_effect=FileNotFoundError(errno.ENOENT, "missing")):
            with self.assertRaises(FileNotFoundError):
                self.channel.transfer("/missing")
        self.calls.call.assert_any_call(91, 0xa0000000, fs_channel.BUFFER_SIZE)
        self.calls.restore.assert_called_once()
        self.debug.expect_ok.assert_called_once_with("D;1")
        self.assertFalse(self.channel.failed)

    def test_unsupported_kernel_detaches_without_calling_syscalls(self):
        self.calls.acquire.side_effect = OSError("unsupported syscall vector")
        with self.assertRaisesRegex(OSError, "unsupported syscall vector"):
            self.channel.transfer("/file")
        self.calls.call.assert_not_called()
        self.calls.restore.assert_called_once()
        self.debug.expect_ok.assert_called_once_with("D;1")
        self.assertFalse(self.channel.failed)

    def test_paused_qemu_is_rejected_before_debugger_attachment(self):
        qmp = Mock()
        qmp.execute.return_value = {"return": {"running": False}}
        server = ui.Server(("127.0.0.1", 0), ui.make_handler(
            None, None, None, qmp, {}, "Test", filesystem=self.channel))
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            with patch.object(mcp_server, "base_url", f"http://127.0.0.1:{server.server_port}"):
                with self.assertRaisesRegex(mcp_server.ToolError, "resume QEMU first"):
                    mcp_server.t_pull_file({"path": "/file"})
            self.debug_factory.assert_not_called()
        finally:
            server.shutdown()
            thread.join(5)
            server.server_close()

    def test_failed_restoration_stops_guest_and_blocks_further_requests(self):
        self.calls.restore.side_effect = OSError("restore failed")
        with patch.object(self.channel, "read_file", return_value=b"data"):
            with self.assertRaisesRegex(OSError, "QEMU left stopped"):
                self.channel.transfer("/file")
        self.debug.stop.assert_called_once()
        self.debug.expect_ok.assert_not_called()
        with self.assertRaisesRegex(OSError, "inspect the stopped QEMU"):
            self.channel.transfer("/file")
        self.assertEqual(self.debug_factory.call_count, 1)

    def test_lost_debugger_connection_does_not_claim_vm_was_stopped(self):
        self.calls.restore.side_effect = OSError("restore failed")
        self.debug.running = True
        self.debug.stop.side_effect = ConnectionError("debugger disconnected")
        with patch.object(self.channel, "read_file", return_value=b"data"):
            with self.assertRaisesRegex(OSError, "stop could not be confirmed"):
                self.channel.transfer("/file")
        self.assertTrue(self.channel.failed)

    def test_http_uses_qmp_stop_if_debugger_recovery_fails(self):
        self.calls.restore.side_effect = OSError("restore failed")
        self.debug.running = True
        self.debug.stop.side_effect = ConnectionError("debugger disconnected")
        qmp = Mock()
        qmp.execute.return_value = {"return": {"running": True}}
        server = ui.Server(("127.0.0.1", 0), ui.make_handler(
            None, None, None, qmp, {}, "Test", filesystem=self.channel))
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            with patch.object(mcp_server, "base_url", f"http://127.0.0.1:{server.server_port}"), \
                    patch.object(self.channel, "read_file", return_value=b"data"):
                with self.assertRaisesRegex(mcp_server.ToolError, "stop could not be confirmed"):
                    mcp_server.t_pull_file({"path": "/file"})
            self.assertEqual([call.args[0] for call in qmp.execute.call_args_list],
                             ["query-status", "stop"])
        finally:
            server.shutdown()
            thread.join(5)
            server.server_close()

    def test_http_mcp_without_console_or_ssh_and_no_ro_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "file with 'quotes' ; $(false)")
            server = ui.Server(("127.0.0.1", 0), ui.make_handler(
                None, None, None, None, {}, "Test", filesystem=self.channel))
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                with patch.object(mcp_server, "base_url", f"http://127.0.0.1:{server.server_port}"), \
                        patch.object(mcp_server, "execute", side_effect=AssertionError("console used")), \
                        patch.object(mcp_server, "ssh_port", side_effect=AssertionError("SSH used")), \
                        patch.object(self.channel, "read_file", side_effect=lambda c, m, p: Path(p).read_bytes()), \
                        patch.object(self.channel, "write_file", side_effect=lambda c, m, p, d, mode: Path(p).write_bytes(d)):
                    result = mcp_server.t_push_file({"path": path, "content": "live data"})
                    self.assertIn("via sidechannel (rw)", result[0]["text"])
                    self.assertEqual(mcp_server.t_pull_file({"path": path})[0]["text"], "live data")
                    self.channel.mode = "ro"
                    with self.assertRaisesRegex(mcp_server.ToolError, "HTTP 403.*read-only"):
                        mcp_server.t_push_file({"path": path, "content": "forbidden"})
                    self.assertEqual(Path(path).read_text(), "live data")
            finally:
                server.shutdown()
                thread.join(5)
                server.server_close()


if __name__ == "__main__":
    unittest.main()
