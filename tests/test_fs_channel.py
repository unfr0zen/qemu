"""Exercise the real helper and HTTP/MCP path without SSH or a console."""
import errno
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ui"))
from fs_channel import FileChannel, LIMIT  # noqa: E402
import mcp_server  # noqa: E402
import ui  # noqa: E402


class FileChannelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which("cc")
        if not compiler:
            raise unittest.SkipTest("C compiler required")
        cls.build = tempfile.TemporaryDirectory()
        cls.agent = str(Path(cls.build.name) / "agent")
        subprocess.run([compiler, "-Wall", "-Wextra", "-Werror", "-Os",
                        str(Path(__file__).resolve().parents[1] / "ui" / "fs_agent.c"),
                        "-o", cls.agent], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def setUp(self):
        self.files = tempfile.TemporaryDirectory()
        self.addCleanup(self.files.cleanup)
        self.path = str(Path(self.files.name) / "file with 'quotes' ; $(false)")
        self.left, self.right = socket.socketpair()
        self.proc = subprocess.Popen([self.agent, "--stdio", "rw"],
                                     stdin=self.right, stdout=self.right)
        self.right.close()
        self.channel = FileChannel("unused", "rw")
        self.channel.sock = self.left
        self.addCleanup(self.close)

    def close(self):
        self.channel.close()
        self.proc.wait(timeout=5)

    def test_binary_empty_and_large_roundtrip_preserves_mode(self):
        for data in (b"", bytes(range(256)), os.urandom(256 * 1024)):
            with self.subTest(size=len(data)):
                self.channel.transfer(self.path, data, "640")
                self.assertEqual(self.channel.transfer(self.path), data)
                self.channel.transfer(self.path, b"replacement")
                self.assertEqual(Path(self.path).stat().st_mode & 0o777, 0o640)
                self.assertEqual(list(Path(self.files.name).glob("*.mcp-*")), [])

    def test_file_errors_leave_original_untouched_and_channel_usable(self):
        self.channel.transfer(self.path, b"keep")
        link = str(Path(self.files.name) / "link")
        Path(link).symlink_to(self.path)
        for path, data, expected in ((link, b"replace", errno.EINVAL),
                                     (self.path + "/child", b"x", errno.ENOTDIR),
                                     (self.path + "-missing", None, errno.ENOENT),
                                     (self.files.name, None, errno.EINVAL)):
            with self.subTest(path=path), self.assertRaises(OSError) as error:
                self.channel.transfer(path, data)
            self.assertEqual(error.exception.errno, expected)
        self.assertEqual(self.channel.transfer(self.path), b"keep")

    def test_read_limit_and_input_validation(self):
        with open(self.path, "wb") as f:
            f.truncate(LIMIT + 1)
        with self.assertRaises(OSError) as error:
            self.channel.transfer(self.path)
        self.assertEqual(error.exception.errno, errno.EFBIG)
        for path, data, mode in (("relative", None, None), ("/nul\0path", None, None),
                                 ("/" + "a" * 4000, None, None),
                                 (self.path, b"x", "888"), (self.path, b"x", "4755"),
                                 (self.path, b"x", ""), (self.path, b"x" * (LIMIT + 1), None)):
            with self.subTest(path=path[:30], mode=mode), self.assertRaises(ValueError):
                self.channel.transfer(path, data, mode)

    def test_ro_enforced_by_helper_even_if_host_requests_rw(self):
        self.channel.close()
        self.proc.wait(timeout=5)
        self.left, right = socket.socketpair()
        self.proc = subprocess.Popen([self.agent, "--stdio", "ro"], stdin=right, stdout=right)
        right.close()
        self.channel.sock = self.left
        Path(self.path).write_bytes(b"original")
        with self.assertRaises(OSError) as error:
            self.channel.transfer(self.path, b"forbidden")
        self.assertEqual(error.exception.errno, errno.EROFS)
        self.assertEqual(self.channel.transfer(self.path), b"original")
        self.channel.mode = "ro"
        with self.assertRaises(PermissionError):
            self.channel.transfer(self.path, b"forbidden")

    def test_interrupted_upload_does_not_modify_destination(self):
        Path(self.path).write_bytes(b"original")
        path = self.path.encode()
        self.left.sendall(struct.pack("!4sIIII", b"NFS1", 2, len(path), 2048, 0o644) + path)
        self.left.sendall(b"x" * 1024)
        self.assertEqual(self.channel.receive(1), b".")
        self.channel.close()
        self.proc.wait(timeout=5)
        self.assertEqual(Path(self.path).read_bytes(), b"original")
        self.assertEqual(list(Path(self.files.name).glob("*.mcp-*")), [])

    def test_http_mcp_without_console_or_ssh_and_no_ro_fallback(self):
        server = ui.Server(("127.0.0.1", 0), ui.make_handler(
            None, None, None, None, {}, "Test", filesystem=self.channel))
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            with patch.object(mcp_server, "base_url", f"http://127.0.0.1:{server.server_port}"), \
                    patch.object(mcp_server, "execute", side_effect=AssertionError("console used")), \
                    patch.object(mcp_server, "ssh_port", side_effect=AssertionError("SSH used")):
                result = mcp_server.t_push_file({"path": self.path, "content": "live data"})
                self.assertIn("via sidechannel (rw)", result[0]["text"])
                self.assertEqual(mcp_server.t_pull_file({"path": self.path})[0]["text"], "live data")
                with self.assertRaisesRegex(mcp_server.ToolError, "HTTP 404") as error:
                    mcp_server.t_pull_file({"path": self.path + "-missing"})
                self.assertNotIn("older than", str(error.exception))
                self.channel.mode = "ro"
                with self.assertRaisesRegex(mcp_server.ToolError, "HTTP 403.*read-only"):
                    mcp_server.t_push_file({"path": self.path, "content": "forbidden"})
                self.assertEqual(Path(self.path).read_text(), "live data")
        finally:
            server.shutdown()
            thread.join(5)
            server.server_close()


if __name__ == "__main__":
    unittest.main()
