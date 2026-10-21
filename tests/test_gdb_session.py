"""Debugger ownership, HTTP/MCP integration, and live QEMU protocol checks."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ui"))
from fs_channel import FileChannel  # noqa: E402
from gdb_session import GdbSession  # noqa: E402
import mcp_server  # noqa: E402
import ui  # noqa: E402


class OwnershipTest(unittest.TestCase):
    def setUp(self):
        self.files = FileChannel("tcp:127.0.0.1:1", "rw")
        self.session = GdbSession(self.files.endpoint, self.files)
        self.debug = Mock(running=False)
        self.debug.command.side_effect = [b"PacketSize=1000", b"l<architecture>arm</architecture>", b"T05"]

    def attach(self):
        with patch("gdb_session.Debugger", return_value=self.debug):
            self.session.execute({"action": "attach"})

    def test_attached_session_excludes_files_until_detached(self):
        self.attach()
        with patch("fs_channel.Debugger") as connection:
            with self.assertRaisesRegex(OSError, "detach"):
                self.files.transfer("/etc/hostname")
            connection.assert_not_called()
        self.session.execute({"action": "detach"})
        self.assertFalse(self.files.debug_active)
        self.debug.expect_ok.assert_called_with("D;1")

    def test_filesystem_transfer_excludes_attachment(self):
        acquired = threading.Event()
        release = threading.Event()

        def transfer():
            with self.files.lock:
                acquired.set()
                release.wait(5)

        thread = threading.Thread(target=transfer)
        thread.start()
        try:
            self.assertTrue(acquired.wait(2))
            with patch("gdb_session.Debugger") as connection:
                with self.assertRaisesRegex(OSError, "busy"):
                    self.session.execute({"action": "attach"})
                connection.assert_not_called()
        finally:
            release.set()
            thread.join(5)

    def test_failed_filesystem_restore_prevents_attachment(self):
        self.files.failed = True
        with patch("gdb_session.Debugger") as connection:
            with self.assertRaisesRegex(OSError, "restoration failed"):
                self.session.execute({"action": "attach"})
            connection.assert_not_called()

    def test_invalid_register_batch_has_no_partial_writes(self):
        self.attach()
        with self.assertRaises(ValueError):
            self.session.execute({"action": "registers", "values": {"r0": 1, "pc": -1}})
        self.debug.set_register.assert_not_called()

    def test_trace_timeout_returns_partial_result_and_stops_cpu(self):
        self.attach()
        self.debug.registers.return_value = [0] * 17
        self.debug.read.return_value = bytes.fromhex("0000a0e1")
        def command(value):
            self.debug.running = True
            raise TimeoutError("deadline")

        def stop():
            self.debug.running = False
            return b"T02"

        self.debug.command.side_effect = command
        self.debug.stop.side_effect = stop
        result = self.session.execute({"action": "trace", "count": 4})
        self.assertFalse(result["complete"])
        self.assertEqual(result["trace"], [])
        self.assertIn("deadline", result["error"])
        self.debug.stop.assert_called_once()

    def test_exec_path_stops_at_page_boundary(self):
        self.attach()
        self.debug.read.side_effect = [b"/bin", b"/true\0"]
        self.assertEqual(self.session.exec_path(0x8ffffffc), "/bin/true")
        self.assertEqual(self.debug.read.call_args_list[0].args, (0x8ffffffc, 4))

    def test_active_exec_trace_rejects_manual_stepping(self):
        self.attach()
        self.session.exec_active = True
        with self.assertRaisesRegex(ValueError, "exec_stop"):
            self.session.execute({"action": "step"})

    def test_http_mcp_and_qmp_exclusion(self):
        self.attach()
        qmp = Mock()
        server = ui.Server(("127.0.0.1", 0), ui.make_handler(
            None, None, None, qmp, {}, "Test", filesystem=self.files, debugger=self.session))
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            with patch.object(mcp_server, "base_url", f"http://127.0.0.1:{server.server_port}"):
                self.assertTrue(json.loads(mcp_server.t_gdb({})[0]["text"])["attached"])
                for command in ("stop", "cont", "system_reset", "human-monitor-command"):
                    with self.assertRaisesRegex(mcp_server.ToolError, "GDB session active"):
                        mcp_server.t_qmp({"command": command})
                with self.assertRaisesRegex(mcp_server.ToolError, "detach"):
                    mcp_server.t_pull_file({"path": "/proc/1/comm", "via": "sidechannel"})
                qmp.execute.assert_not_called()
                mcp_server.t_gdb({"action": "detach"})
        finally:
            server.shutdown()
            thread.join(5)
            server.server_close()


QEMU = os.environ.get("QEMU_SYSTEM_ARM")


@unittest.skipUnless(QEMU, "set QEMU_SYSTEM_ARM to exercise the live GDB stub")
class LiveQemuTest(unittest.TestCase):
    def setUp(self):
        machine = "flintstone" if "flintstone" in self._testMethodName else "virt"
        self.address = 0x40000000 if machine == "virt" else 0x80000000
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        options = ["-cpu", "cortex-a15"] if machine == "virt" else []
        self.proc = subprocess.Popen([
            QEMU, "-M", machine, *options, "-S", "-display", "none",
            "-nodefaults", "-nic", "none", "-gdb", f"tcp:127.0.0.1:{port}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.addCleanup(self.cleanup)
        self.session = GdbSession(f"tcp:127.0.0.1:{port}")
        for _ in range(100):
            try:
                self.session.execute({"action": "attach"})
                break
            except ConnectionRefusedError:
                if self.proc.poll() is not None:
                    self.fail(self.proc.stderr.read().decode())
                time.sleep(0.02)
        else:
            self.fail("QEMU GDB stub did not start")

    def cleanup(self):
        if self.session.debug:
            self.session.debug.sock.close()
        self.proc.terminate()
        self.proc.wait(timeout=5)
        self.proc.stderr.close()

    def test_step_breakpoint_watchpoint_interrupt_and_detach(self):
        def call(action, **kwargs):
            if "address" not in kwargs and action in ("read_memory", "write_memory", "breakpoint"):
                kwargs["address"] = self.address
            return self.session.execute(dict(action=action, **kwargs))

        # ARM nop; branch to nop.
        code = "0000a0e1fdffffea"
        call("write_memory", data=code)
        self.assertEqual(call("read_memory", size=8)["data"], code)
        call("registers", values={"pc": self.address, "r0": "0x1234"})
        trace = call("trace", count=4)
        self.assertTrue(trace["complete"])
        self.assertEqual([frame["pc"] for frame in trace["trace"]],
                         [f"0x{self.address + offset:08x}" for offset in (0, 4, 0, 4)])
        self.assertEqual([frame["instruction"] for frame in trace["trace"]],
                         ["0000a0e1", "fdffffea"] * 2)
        self.assertTrue(all(frame["registers"]["r0"] == "0x00001234" for frame in trace["trace"]))
        call("step")
        self.assertEqual(call("registers")["pc"], f"0x{self.address + 4:08x}")
        self.assertEqual(call("registers")["r0"], "0x00001234")
        for kind in (0, 1, 2, 3, 4):
            call("breakpoint", type=kind)
            call("breakpoint", type=kind, enabled=False)
        call("breakpoint")
        self.assertTrue(call("continue")["running"])
        for _ in range(100):
            if not call("status")["running"]:
                break
            time.sleep(0.01)
        else:
            self.fail("breakpoint did not stop QEMU")
        self.assertEqual(call("registers")["pc"], f"0x{self.address:08x}")
        call("breakpoint", enabled=False)
        call("breakpoint", address=self.address + 8)
        call("continue")
        self.assertTrue(call("status")["running"])
        self.assertFalse(call("interrupt")["running"])
        call("breakpoint", address=self.address + 8, enabled=False)
        call("breakpoint", address=self.address + 4)
        self.assertFalse(call("detach")["attached"])
        call("attach")
        call("continue")
        time.sleep(0.02)
        self.assertTrue(call("status")["running"], "detach left a breakpoint behind")
        call("detach")

    def test_flintstone(self):
        self.test_step_breakpoint_watchpoint_interrupt_and_detach()

    def test_trace_thumb_instruction_widths(self):
        self.session.execute({"action": "write_memory", "address": self.address,
                              "data": "00bfaff3008000bf"})
        self.session.execute({"action": "registers", "values": {
            "pc": self.address | 1, "cpsr": "0x400001f3"}})
        result = self.session.execute({"action": "trace", "count": 3})
        self.assertTrue(result["complete"], result)
        self.assertEqual([frame["instruction"] for frame in result["trace"]],
                         ["00bf", "aff30080", "00bf"])
        self.assertTrue(all(frame["thumb"] for frame in result["trace"]))
        for count in (0, -1, 257):
            with self.assertRaises(ValueError):
                self.session.execute({"action": "trace", "count": count})

    def test_trace_stops_on_watchpoint(self):
        self.session.execute({"action": "write_memory", "address": self.address,
                              "data": "000081e50000a0e1"})
        self.session.execute({"action": "registers", "values": {
            "pc": self.address, "r0": "0x12345678", "r1": self.address + 256}})
        self.session.execute({"action": "breakpoint", "type": 2, "address": self.address + 256})
        result = self.session.execute({"action": "trace", "count": 2})
        self.assertFalse(result["complete"], result)
        self.assertEqual(len(result["trace"]), 1)
        self.assertIn("watch:", result["stop_reply"])
        self.assertFalse(result["running"])

    def test_exec_log_and_auto_resume(self):
        call = self.session.execute
        call({"action": "write_memory", "address": self.address,
              "data": "0000a0e1fdffffea"})
        call({"action": "write_memory", "address": self.address + 512,
              "data": b"/bin/true\0".hex()})
        call({"action": "registers", "values": {"pc": self.address, "r0": self.address + 512}})
        call({"action": "exec_start", "address": self.address})
        for _ in range(100):
            result = call({"action": "exec_read"})
            if len(result["events"]) >= 2:
                break
            self.assertIsNone(result["exec_error"], result)
            time.sleep(0.02)
        else:
            self.fail("exec trace did not auto-resume for a second hit")
        self.assertTrue(all(event["path"] == "/bin/true" for event in result["events"]))
        stopped = call({"action": "exec_stop"})
        self.assertFalse(stopped["running"])
        self.assertFalse(stopped["exec_tracing"])
        self.assertEqual(stopped["breakpoints"], [])
        cursor = call({"action": "exec_read"})["cursor"]
        self.assertEqual(call({"action": "exec_read", "since": cursor})["events"], [])
        # A restarted reader and worker share a monotonic event cursor.
        call({"action": "exec_start", "address": self.address})
        for _ in range(100):
            result = call({"action": "exec_read", "since": cursor})
            if result["events"]:
                break
            time.sleep(0.01)
        self.assertGreater(result["cursor"], cursor)
        call({"action": "detach"})
        self.assertFalse(self.session.exec_active)

    def test_exec_log_flintstone(self):
        self.test_exec_log_and_auto_resume()

if __name__ == "__main__":
    unittest.main()
