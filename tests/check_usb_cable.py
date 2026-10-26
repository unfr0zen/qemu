#!/usr/bin/env python3
"""Check real MCP cable control, stock firmware, and Linux USB/IP attachment.

Run against a booted gen 3 unit with host USB attached:
    python3 tests/check_usb_cable.py --url http://127.0.0.1:8075 --host-busid 5-1

The selected host device must belong to this unit. JSON observations go to
stdout; the cable is left plugged in, including when a check fails.
"""
import argparse
import json
from pathlib import Path
import select
import subprocess
import sys
import time


PRESENT = ("/sys/devices/soc0/soc/2100000.aips-bus/2184000.usb/"
           "power_supply/imx_usb_charger/present")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--host-busid", required=True,
                        help="local USB/IP bus ID from usbip port, e.g. 5-1")
    args = parser.parse_args()
    script = Path(__file__).resolve().parents[1] / "ui" / "mcp_server.py"
    host = Path("/sys/bus/usb/devices") / args.host_busid
    with subprocess.Popen([sys.executable, str(script), "--url", args.url],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          text=True) as proc:
        sequence = 0

        def request(method, params):
            nonlocal sequence
            sequence += 1
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": sequence,
                                         "method": method, "params": params}) + "\n")
            proc.stdin.flush()
            if not select.select([proc.stdout], [], [], 30)[0]:
                raise AssertionError(f"MCP request timed out: {method}")
            result = json.loads(proc.stdout.readline())
            assert result["id"] == sequence, result
            assert "error" not in result, result
            return result["result"]

        def tool(name, arguments):
            result = request("tools/call", {"name": name, "arguments": arguments})
            assert not result["isError"], result
            return result["content"][0]["text"]

        def host_device():
            try:
                return {key: (host / key).read_text().strip()
                        for key in ("idVendor", "idProduct", "devnum")}
            except FileNotFoundError:
                return None

        def observe(connected):
            start = time.monotonic()
            deadline = start + 20
            while time.monotonic() < deadline:
                output = tool("console_exec", {"command": f"cat {PRESENT}",
                                               "timeout_s": 5})
                assert output.startswith("exit 0\n"), output
                present = int(output.splitlines()[1])
                device = host_device()
                if present == int(connected) and bool(device) == connected:
                    state = json.loads(tool("usb", {}))
                    assert state == {"connected": connected, "vbus": connected}, state
                    registers = {}
                    for name, addr in (("otgsc", 0x021841a4),
                                       ("vbus_status", 0x020c81c0),
                                       ("charger_status", 0x020c81d0)):
                        result = json.loads(tool("qmp", {
                            "command": "human-monitor-command",
                            "arguments": {"command-line": f"xp /1wx {addr:#x}"}}))
                        registers[name] = result["return"].strip()
                    record = {"connected": connected, "present": present,
                              "host": device, "registers": registers,
                              "settled_s": round(time.monotonic() - start, 3)}
                    print(json.dumps(record), flush=True)
                    return device
                time.sleep(0.1)
            raise AssertionError(f"USB did not settle: present={present}, host={device}")

        try:
            request("initialize", {"protocolVersion": "2025-06-18"})
            assert any(t["name"] == "usb" for t in request("tools/list", {})["tools"])
            assert json.loads(tool("usb", {})) == {"connected": True, "vbus": True}
            previous = observe(True)
            for _ in range(3):
                assert json.loads(tool("usb", {"connected": False}))["vbus"] is False
                observe(False)
                # An unplugged cable stays down and repeating unplug is harmless.
                tool("usb", {"connected": False})
                time.sleep(2)
                observe(False)
                assert json.loads(tool("usb", {"connected": True}))["vbus"] is True
                current = observe(True)
                assert current["devnum"] != previous["devnum"], (previous, current)
                assert current["idVendor"] == previous["idVendor"]
                assert current["idProduct"] == previous["idProduct"]
                previous = current
            print(json.dumps({"result": "passed", "cycles": 3}), flush=True)
        finally:
            try:
                tool("usb", {"connected": True})
            finally:
                proc.stdin.close()
                proc.wait(timeout=10)


if __name__ == "__main__":
    main()
