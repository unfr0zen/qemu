#!/usr/bin/env python3
"""Check live MCP file access through a UI with no console, QMP or SSH.

Start an isolated gen 3 unit with --ui --fs rw, then pass its filesystem
chardev endpoint (from ui.py's --fs argument). Do not use that unit's file
API concurrently: QEMU permits one client per chardev.

    python3 tests/check_fs_channel.py --channel tcp:127.0.0.1:8108 --mode rw

The guest evidence file is retained. Reboot the same NAND with --fs ro and
repeat with --mode ro and --sha256 from the rw result to check persistence
and read-only enforcement. Observations are JSON lines on stdout.
"""
import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ui"))
from fs_channel import FileChannel  # noqa: E402
import ui  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", required=True)
    parser.add_argument("--mode", choices=("ro", "rw"), required=True)
    parser.add_argument("--path", default="/media/data/mcp-fs-evidence.bin")
    parser.add_argument("--sha256", help="expected retained file hash (ro)")
    args = parser.parse_args()
    channel = FileChannel(args.channel, args.mode)
    server = ui.Server(("127.0.0.1", 0), ui.make_handler(
        None, None, None, None, {}, "Filesystem evidence", filesystem=channel))
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        for method, path in (("GET", "/console/read"), ("POST", "/qmp")):
            request = urllib.request.Request(url + path, method=method,
                                             data=b"{}" if method == "POST" else None)
            try:
                urllib.request.urlopen(request, timeout=5)
                raise AssertionError(f"{path} unexpectedly available")
            except urllib.error.HTTPError as error:
                with error:
                    assert error.code == 503, error.code
                    print(json.dumps({"unavailable": path, "status": error.code,
                                      "reason": error.read().decode().strip()}), flush=True)
        with tempfile.TemporaryDirectory() as directory, subprocess.Popen(
                [sys.executable, str(Path(ui.HERE) / "mcp_server.py"), "--url", url],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as proc:
            sequence = 0

            def request(method, params):
                nonlocal sequence
                sequence += 1
                proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": sequence,
                                            "method": method, "params": params}) + "\n")
                proc.stdin.flush()
                assert select.select([proc.stdout], [], [], 120)[0], method
                result = json.loads(proc.stdout.readline())
                assert result["id"] == sequence and "error" not in result, result
                return result["result"]

            def tool(name, arguments, fails=False):
                result = request("tools/call", {"name": name, "arguments": arguments})
                assert result["isError"] == fails, result
                return result["content"][0]["text"]

            request("initialize", {"protocolVersion": "2025-06-18"})
            print(json.dumps({"mode": args.mode, "pid1": tool("pull_file", {
                "path": "/proc/1/comm", "via": "sidechannel"}).strip()}), flush=True)
            present = tool("pull_file", {"path": "/sys/devices/soc0/soc/2100000.aips-bus/"
                                       "2184000.usb/power_supply/imx_usb_charger/present"}).strip()
            assert present in ("0", "1"), present
            print(json.dumps({"usb_present": int(present)}), flush=True)
            if args.mode == "rw":
                failure = tool("push_file", {"path": "/mcp-fs-evidence", "content": "forbidden"}, fails=True)
                assert "read-only" in failure.lower(), failure
                print(json.dumps({"guest_ro_mount_rejected": failure}), flush=True)
                data = bytes(range(256)) * 1024 + os.urandom(8192)
                source = Path(directory) / "source.bin"
                source.write_bytes(data)
                print(json.dumps({"upload": tool("push_file", {
                    "path": args.path, "local_path": str(source), "mode": "640"})}), flush=True)
                tool("push_file", {"path": args.path + ".empty", "content": ""})
                assert tool("pull_file", {"path": args.path + ".empty"}) == ""
                expected = hashlib.sha256(data).hexdigest()
            else:
                expected = args.sha256
                failure = tool("push_file", {"path": args.path, "content": "forbidden"}, fails=True)
                assert "read-only" in failure, failure
                print(json.dumps({"host_ro_rejected": failure}), flush=True)
                channel.mode = "rw"
                try:
                    channel.transfer(args.path, b"forbidden")
                    raise AssertionError("helper accepted a write in ro mode")
                except OSError as error:
                    assert error.errno == errno.EROFS, error
                    print(json.dumps({"helper_ro_errno": error.errno}), flush=True)
                finally:
                    channel.mode = "ro"
            target = Path(directory) / "received.bin"
            tool("pull_file", {"path": args.path, "local_path": str(target)})
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            assert not expected or digest == expected, (digest, expected)
            print(json.dumps({"result": "passed", "mode": args.mode, "path": args.path,
                              "bytes": target.stat().st_size, "sha256": digest,
                              "console": False, "qmp": False, "ssh": False}), flush=True)
            proc.stdin.close()
            proc.wait(timeout=10)
    finally:
        server.shutdown()
        thread.join(5)
        server.server_close()
        channel.close()


if __name__ == "__main__":
    main()
