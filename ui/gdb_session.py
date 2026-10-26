"""Persistent ARM debugging, serialized with the filesystem sidechannel."""
import collections
import select
import threading
import time

from fs_channel import Debugger


def integer(value, maximum=0xffffffff):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("expected an integer or hexadecimal string")
    value = int(value, 0) if isinstance(value, str) else value
    if not 0 <= value <= maximum:
        raise ValueError(f"value must be between 0 and {maximum:#x}")
    return value


class GdbSession:
    def __init__(self, endpoint, filesystem=None):
        self.endpoint = endpoint
        self.filesystem = filesystem
        self.lock = filesystem.lock if filesystem else threading.Lock()
        self.debug = None
        self.reason = None
        self.breakpoints = set()
        self.exec_point = None
        self.exec_active = False
        self.exec_error = None
        self.exec_events = collections.deque(maxlen=512)
        self.exec_cursor = 0
        self.exec_generation = 0

    def poll(self):
        debug = self.debug
        if debug and debug.running and (debug.position < len(debug.buffer) or
                                        select.select([debug.sock], [], [], 0)[0]):
            self.reason = debug.receive().decode()

    def state(self):
        return {"attached": self.debug is not None,
                "running": self.debug.running if self.debug else None,
                "stop_reply": self.reason,
                "exec_tracing": self.exec_active, "exec_error": self.exec_error,
                "breakpoints": [{"type": kind, "address": f"0x{address:08x}", "size": size}
                                for kind, address, size in sorted(self.breakpoints)]}

    def resume(self):
        self.debug.send("c")
        # Consume the ACK so polling waits only for a stop packet.
        if self.debug.byte() != ord("+"):
            raise OSError("QEMU did not acknowledge continue")
        self.reason = None

    def exec_path(self, address):
        data = bytearray()
        while len(data) < 4096:
            # A terminator before a page boundary must not require the next page.
            chunk = self.debug.read(address, min(64, 4096 - (address & 4095), 4096 - len(data)))
            prefix, separator, _ = chunk.partition(b"\0")
            data.extend(prefix)
            if separator:
                return data.decode("utf-8", errors="replace")
            address += len(chunk)
        raise ValueError("exec path is not terminated within 4096 bytes")

    def follow_execs(self, generation):
        while True:
            time.sleep(0.01)
            with self.lock:
                if not self.exec_active or generation != self.exec_generation:
                    return
                debug = self.debug
                debug.deadline = time.monotonic() + 5
                try:
                    self.poll()
                    if debug.running:
                        continue
                    if not self.reason.startswith(("T05", "S05")) or debug.register(15) != self.exec_point[1]:
                        raise OSError("execution stopped outside the exec breakpoint; inspect the guest")
                    self.exec_cursor += 1
                    address = debug.register(0)
                    event = {"id": self.exec_cursor, "r0": f"0x{address:08x}"}
                    try:
                        event["path"] = self.exec_path(address)
                    except (OSError, ValueError) as error:
                        event.update(path=None, error=str(error))
                    self.exec_events.append(event)
                    # Single-step past the breakpoint before resuming the guest.
                    self.reason = debug.command("s").decode()
                    if not self.reason.startswith(("T05", "S05")) or "watch:" in self.reason:
                        raise OSError("exec tracing interrupted by another stop; inspect the guest")
                    self.resume()
                except (OSError, ValueError) as error:
                    self.exec_error = str(error)
                    self.exec_active = False
                    return

    def registers(self):
        names = [f"r{i}" for i in range(13)] + ["sp", "lr", "pc", "cpsr"]
        return dict(zip(names, (f"0x{value:08x}" for value in self.debug.registers())))

    def trace(self, count):
        if not count:
            raise ValueError("trace count must be between 1 and 256")
        frames = []
        debug = self.debug
        try:
            for _ in range(count):
                registers = self.registers()
                pc = int(registers["pc"], 16)
                thumb = bool(int(registers["cpsr"], 16) & 0x20)
                data = debug.read(pc, 2 if thumb else 4)
                if thumb and (int.from_bytes(data, "little") & 0xf800) >= 0xe800:
                    data += debug.read(pc + 2, 2)
                self.reason = debug.command("s").decode()
                frames.append({"pc": registers["pc"], "instruction": data.hex(),
                               "thumb": thumb, "registers": registers,
                               "stop_reply": self.reason})
                # Watchpoints and signals end the trace at the reported stop.
                if "watch:" in self.reason or not self.reason.startswith(("T05", "S05")):
                    break
        except OSError as error:
            message = str(error)
            debug.deadline = time.monotonic() + 5
            try:
                if debug.running:
                    self.reason = debug.stop().decode()
            except OSError as stop_error:
                message += f"; could not confirm stop: {stop_error}"
            return {**self.state(), "trace": frames, "complete": False, "error": message}
        return {**self.state(), "trace": frames, "complete": len(frames) == count}

    def execute(self, want):
        if not isinstance(want, dict):
            raise ValueError("expected a JSON object")
        action = want.get("action", "status")
        if action not in ("status", "attach", "detach", "interrupt", "continue", "step",
                          "registers", "read_memory", "write_memory", "breakpoint", "trace",
                          "exec_start", "exec_read", "exec_stop"):
            raise ValueError("unknown GDB action")
        if not self.lock.acquire(timeout=0.1):
            raise OSError("debugger busy with another request or filesystem transfer; retry")
        try:
            if self.filesystem and self.filesystem.failed:
                raise OSError("filesystem context restoration failed; recover it before debugging")
            if self.debug:
                self.debug.deadline = time.monotonic() + 5
                self.poll()
            if action == "status":
                return self.state()
            if action == "exec_read":
                since = integer(want.get("since", 0), 0xffffffffffffffff)
                return {**self.state(), "cursor": self.exec_cursor,
                        "first_id": self.exec_events[0]["id"] if self.exec_events else None,
                        "events": [event for event in self.exec_events if event["id"] > since]}
            if self.exec_active and action not in ("exec_stop", "detach"):
                raise ValueError("exec trace active; exec_stop or detach before other GDB operations")
            if action == "attach":
                if not self.debug:
                    self.debug = Debugger(self.endpoint, 5)
                    if self.filesystem:
                        self.filesystem.debug_active = True
                    self.debug.command("qSupported:multiprocess+;xmlRegisters=arm")
                    xml = self.debug.command("qXfer:features:read:target.xml:0,fff")
                    if b"<architecture>arm</architecture>" not in xml:
                        raise OSError("GDB tools require a 32-bit ARM target")
                    self.reason = self.debug.command("?").decode()
                return self.state()
            if not self.debug:
                raise ValueError("no GDB session; attach first")
            debug = self.debug
            if action in ("interrupt", "detach", "exec_stop"):
                self.exec_active = False
                if debug.running:
                    self.reason = debug.stop().decode()
                if action == "exec_stop" and self.exec_point:
                    kind, address, size = self.exec_point
                    debug.expect_ok(f"z{kind},{address:x},{size:x}")
                    self.breakpoints.discard(self.exec_point)
                    self.exec_point = None
                if action == "detach":
                    for kind, address, size in sorted(self.breakpoints):
                        debug.expect_ok(f"z{kind},{address:x},{size:x}")
                    debug.expect_ok("D;1")
                    debug.sock.close()
                    self.debug = None
                    self.reason = None
                    self.breakpoints.clear()
                    self.exec_point = None
                    if self.filesystem:
                        self.filesystem.debug_active = False
                return self.state()
            if debug.running:
                raise ValueError("guest is running; interrupt it before this operation")
            if action == "continue":
                self.resume()
            elif action == "exec_start":
                address = integer(want.get("address"))
                point = (0, address, 4)
                if self.exec_point or point in self.breakpoints:
                    raise ValueError("exec breakpoint already owned; exec_stop or remove it first")
                debug.expect_ok(f"Z0,{address:x},4")
                self.breakpoints.add(point)
                self.exec_point = point
                self.exec_error = None
                self.exec_events.clear()
                self.resume()
                self.exec_active = True
                self.exec_generation += 1
                threading.Thread(target=self.follow_execs, args=(self.exec_generation,), daemon=True).start()
            elif action == "step":
                self.reason = debug.command("s").decode()
            elif action == "trace":
                return self.trace(integer(want.get("count", 32), 256))
            elif action == "registers":
                values = want.get("values", {})
                names = {f"r{i}": i for i in range(16)}
                names.update(sp=13, lr=14, pc=15, cpsr=25)
                if not isinstance(values, dict) or any(k not in names for k in values):
                    raise ValueError("registers: use r0..r15, sp, lr, pc, cpsr")
                writes = [(names[k], integer(v)) for k, v in values.items()]
                for number, value in writes:
                    debug.set_register(number, value)
                return self.registers()
            elif action in ("read_memory", "write_memory"):
                address = integer(want.get("address"))
                if action == "read_memory":
                    size = integer(want.get("size"), 65536)
                    data = None
                else:
                    value = want.get("data")
                    if not isinstance(value, str) or len(value) > 131072:
                        raise ValueError("data must be hex bytes, at most 64 KiB")
                    data = bytes.fromhex(value)
                    size = len(data)
                if address + size > 0x100000000:
                    raise ValueError("memory range exceeds the ARM address space")
                if data is None:
                    return {"address": f"0x{address:08x}", "data": debug.read(address, size).hex()}
                debug.write(address, data)
                return {"written": size}
            elif action == "breakpoint":
                kind = integer(want.get("type", 0), 4)
                address = integer(want.get("address"))
                size = integer(want.get("size", 4), 8)
                enabled = want.get("enabled", True)
                if size not in ((2, 4) if kind < 2 else (1, 2, 4, 8)):
                    raise ValueError("invalid breakpoint/watchpoint size")
                if not isinstance(enabled, bool):
                    raise ValueError("enabled must be boolean")
                point = (kind, address, size)
                debug.expect_ok(f"{'Z' if enabled else 'z'}{kind},{address:x},{size:x}")
                if enabled:
                    self.breakpoints.add(point)
                else:
                    self.breakpoints.discard(point)
            return self.state()
        finally:
            self.lock.release()
