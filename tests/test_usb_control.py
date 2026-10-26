"""MCP USB cable control through the UI's HTTP API."""
import json
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ui"))
import mcp_server  # noqa: E402
import ui  # noqa: E402


class UsbControlTest(unittest.TestCase):
    def setUp(self):
        self.connected = True
        self.qmp = Mock()
        self.qmp.execute.side_effect = self.execute
        self.server = ui.Server(("127.0.0.1", 0), ui.make_handler(
            None, None, None, self.qmp, {}, "Test"))
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self.close)
        url = f"http://127.0.0.1:{self.server.server_port}"
        self.url = patch.object(mcp_server, "base_url", url)
        self.url.start()

    def close(self):
        self.url.stop()
        self.server.shutdown()
        self.thread.join(5)
        self.server.server_close()

    def execute(self, command, arguments):
        self.assertEqual(arguments["path"], "/machine")
        if command == "qom-set":
            self.assertEqual(arguments["property"], "usb-connected")
            self.connected = arguments["value"]
            return {"return": {}}
        self.assertEqual(command, "qom-get")
        self.assertIn(arguments["property"], ("usb-connected", "usb-vbus"))
        return {"return": self.connected}

    def test_mcp_plug_unplug_and_read(self):
        self.assertEqual(mcp_server.get_json("/usb"),
                         {"connected": True, "vbus": True})
        for connected in (False, False, True, True):
            result = mcp_server.t_usb({"connected": connected})
            self.assertEqual(json.loads(result[0]["text"]),
                             {"connected": connected, "vbus": connected})
        self.qmp.reset_mock()
        self.assertEqual(json.loads(mcp_server.t_usb({})[0]["text"]),
                         {"connected": True, "vbus": True})
        self.assertTrue(all(call.args[0] == "qom-get"
                            for call in self.qmp.execute.call_args_list))

    def test_invalid_requests_do_not_change_cable(self):
        for body in (b"{", [], {"connected": 1}, {"connected": "false"},
                     {"connected": None}, {"other": True}):
            with self.subTest(body=body), self.assertRaisesRegex(
                    mcp_server.ToolError, "HTTP 400"):
                mcp_server.post("/usb", body)
        self.qmp.execute.assert_not_called()

    def test_unsupported_machine_is_reported(self):
        self.qmp.execute.side_effect = None
        self.qmp.execute.return_value = {
            "error": {"desc": "Property 'usb-connected' not found"}}
        with self.assertRaisesRegex(mcp_server.ToolError, "HTTP 503.*not found"):
            mcp_server.t_usb({"connected": False})


if __name__ == "__main__":
    unittest.main()
