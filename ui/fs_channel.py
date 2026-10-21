"""Live ARM Linux file access through QEMU's debugger; no guest helper."""
import errno
import os
import posixpath
import secrets
import socket
import stat
import struct
import threading
import time

LIMIT = 16 * 1024 * 1024
VECTOR = 0xffff0008
KERNEL = 0xc0000000
BUFFER_SIZE = 65536
DATA_OFFSET = 12288
CHUNK = 32768
O_DIRECTORY = 0o40000  # ARM Linux differs from asm-generic here.
O_NOFOLLOW = 0o100000


class Debugger:
    """The GDB remote operations needed to invoke ARM Linux syscalls."""

    def __init__(self, endpoint, timeout):
        host, port = endpoint.removeprefix("tcp:").rsplit(":", 1)
        self.deadline = time.monotonic() + timeout
        self.sock = socket.create_connection((host, int(port)), timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.buffer = b""
        self.position = 0
        self.running = False  # QEMU stops on debugger connection.

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("filesystem debugger deadline exceeded")
        self.sock.settimeout(remaining)

    def byte(self):
        if self.position == len(self.buffer):
            self.remaining()
            self.buffer = self.sock.recv(65536)
            self.position = 0
            if not self.buffer:
                raise ConnectionError("QEMU debugger disconnected")
        value = self.buffer[self.position]
        self.position += 1
        return value

    def receive(self):
        while self.byte() != ord("$"):
            pass
        packet = bytearray()
        while True:
            value = self.byte()
            if value == ord("#"):
                break
            packet.append(value)
        checksum = bytes((self.byte(), self.byte()))
        if int(checksum, 16) != sum(packet) % 256:
            raise OSError("invalid QEMU debugger checksum")
        self.sock.sendall(b"+")
        data = bytearray()
        index = 0
        while index < len(packet):
            value = packet[index]
            if value == ord("}"):
                index += 1
                data.append(packet[index] ^ 32)
            elif value == ord("*"):
                index += 1
                data.extend(data[-1:] * (packet[index] - 29))
            else:
                data.append(value)
            index += 1
        result = bytes(data)
        if result.startswith((b"T", b"S")):
            self.running = False
        return result

    def command(self, command):
        data = command.encode()
        self.remaining()
        if command in ("c", "s"):
            self.running = True
        self.sock.sendall(b"$" + data + b"#" + f"{sum(data) % 256:02x}".encode())
        return self.receive()

    def expect_ok(self, command):
        result = self.command(command)
        if result != b"OK":
            raise OSError(f"QEMU debugger rejected {command.split(',')[0]}: {result!r}")

    def register(self, number):
        value = self.command(f"p{number:x}")
        if len(value) != 8:
            raise OSError(f"QEMU debugger has no ARM register {number}")
        return int.from_bytes(bytes.fromhex(value.decode()), "little")

    def set_register(self, number, value):
        self.expect_ok(f"P{number:x}=" + struct.pack("<I", value & 0xffffffff).hex())

    def registers(self):
        return [self.register(i) for i in range(16)] + [self.register(25)]

    def set_registers(self, registers):
        for number, value in enumerate(registers[:16]):
            self.set_register(number, value)
        self.set_register(25, registers[16])

    def read(self, address, size):
        data = bytearray()
        while len(data) < size:
            count = min(size - len(data), 1024)
            value = self.command(f"m{address + len(data):x},{count:x}")
            if len(value) != count * 2:
                raise OSError(f"QEMU cannot read guest memory at {address + len(data):#x}")
            data.extend(bytes.fromhex(value.decode()))
        return bytes(data)

    def write(self, address, data):
        for offset in range(0, len(data), 1024):
            chunk = data[offset:offset + 1024]
            self.expect_ok(f"M{address + offset:x},{len(chunk):x}:" + chunk.hex())

    def breakpoint(self, address, enabled=True):
        self.expect_ok(f"{'Z' if enabled else 'z'}0,{address:x},4")

    def stop(self):
        if self.running:
            self.deadline = time.monotonic() + 5
            self.sock.sendall(b"\x03")
            self.receive()


class Syscalls:
    """Borrow a syscall frame, stopping before Linux's return-to-user path.

    The ARM EABI dispatcher pushes r4/r5 followed by an 18-word pt_regs.
    QEMU CPU breakpoints leave the guest's instruction bytes untouched.
    """

    def __init__(self, debugger):
        self.debugger = debugger
        self.saved = None
        self.frame = None
        self.breakpoints = set()
        self.pending = False

    def breakpoint(self, address, enabled=True):
        self.debugger.breakpoint(address, enabled)
        if enabled:
            self.breakpoints.add(address)
        else:
            self.breakpoints.discard(address)

    def acquire(self):
        debug = self.debugger
        debug.command("qSupported:multiprocess+;xmlRegisters=arm")
        xml = debug.command("qXfer:features:read:target.xml:0,fff")
        if b"<architecture>arm</architecture>" not in xml:
            raise OSError("filesystem sidechannel requires 32-bit ARM Linux")
        debug.command("?")
        if debug.read(VECTOR, 4) != struct.pack("<I", 0xe59ffff0):
            raise OSError("unsupported Linux syscall vector (kernel not ready)")
        self.breakpoint(VECTOR)
        for _ in range(32):
            debug.command("c")
            if debug.register(15) != VECTOR:
                raise OSError("QEMU stopped outside the syscall vector")
            for _ in range(128):
                pc = debug.register(15)
                instruction = struct.unpack("<I", debug.read(pc, 4))[0]
                # ldrcc pc, [table, syscall_number, lsl #2]
                if instruction & 0x0ff0fff0 == 0x0790f100:
                    break
                debug.command("s")
            else:
                raise OSError("unsupported ARM Linux syscall dispatcher")
            registers = debug.registers()
            frame = debug.read(registers[13], 80)
            user_psr = struct.unpack_from("<I", frame, 72)[0]
            if registers[16] & 0x3f != 0x13 or user_psr & 0x1f != 0x10:
                raise OSError("unsupported ARM Linux syscall frame")
            table = registers[(instruction >> 16) & 15]
            if not all(KERNEL <= address < 0xe0000000 for address in
                       (table, registers[13], registers[14])):
                raise OSError("invalid Linux syscall context")
            addresses = struct.unpack("<4I", debug.read(table + 12, 16))
            if not all(KERNEL <= address < 0xe0000000 for address in addresses):
                raise OSError("invalid Linux syscall table")
            self.saved, self.frame, self.table = registers, frame, table
            self.breakpoint(VECTOR, False)
            self.breakpoint(registers[14])
            if self.call(201) == 0:  # geteuid32
                return
            self.restore()
            self.breakpoint(VECTOR)
        raise PermissionError("no root task reached the Linux syscall dispatcher")

    def call(self, number, *arguments):
        debug = self.debugger
        args = [value & 0xffffffff for value in arguments] + [0] * (6 - len(arguments))
        registers = self.saved.copy()
        registers[:6] = args
        registers[7] = number
        registers[15] = struct.unpack("<I", debug.read(self.table + number * 4, 4))[0]
        if not KERNEL <= registers[15] < 0xe0000000:
            raise OSError("invalid Linux syscall target")
        self.pending = True
        debug.write(registers[13], struct.pack("<II", *args[4:6]))
        debug.set_registers(registers)
        while True:
            debug.command("c")
            if debug.register(15) != self.saved[14]:
                raise OSError("QEMU stopped outside the Linux syscall return")
            if debug.register(13) == self.saved[13]:
                break
        self.pending = False
        result = debug.register(0)
        if result >= 0xfffff001:
            error = 0x100000000 - result
            raise OSError(error, os.strerror(error))
        return result

    def restore(self):
        if self.saved is not None:
            if self.pending:
                raise OSError("injected syscall has not returned; QEMU must remain stopped")
            self.debugger.write(self.saved[13], self.frame)
            self.debugger.set_registers(self.saved)
            if (self.debugger.registers() != self.saved or
                    self.debugger.read(self.saved[13], 80) != self.frame):
                raise OSError("guest syscall context restoration did not verify")
            self.saved = None
        for address in list(self.breakpoints):
            self.breakpoint(address, False)


class FileChannel:
    def __init__(self, endpoint, mode):
        self.endpoint = endpoint
        self.mode = mode
        self.lock = threading.Lock()
        self.failed = False

    def transfer(self, path, data=None, mode=None, timeout=300):
        if not isinstance(path, str) or not path.startswith("/") or "\0" in path:
            raise ValueError("path must be absolute, without NUL bytes")
        if len(path.encode()) >= 4000:
            raise ValueError("path is too long")
        if data is not None and self.mode != "rw":
            raise PermissionError("filesystem channel is read-only (--fs ro)")
        if data is not None and len(data) > LIMIT:
            raise ValueError("filesystem transfers are limited to 16 MiB")
        permissions = None
        if mode is not None:
            if not isinstance(mode, str) or not mode or any(c not in "01234567" for c in mode):
                raise ValueError("mode must be octal permissions, e.g. 755")
            permissions = int(mode, 8)
            if permissions > 0o777:
                raise ValueError("mode must be between 000 and 777")
        with self.lock:
            if self.failed:
                raise OSError("filesystem debugger failed; inspect the stopped QEMU before restarting it")
            debug = Debugger(self.endpoint, timeout)
            calls = Syscalls(debug)
            memory = None
            try:
                calls.acquire()
                # MAP_POPULATE faults pages in before debug memory access.
                memory = calls.call(192, 0, BUFFER_SIZE, 3, 0x8022, -1, 0)
                if data is None:
                    return self.read_file(calls, memory, path)
                self.write_file(calls, memory, path, data, permissions)
                return b""
            finally:
                debug.deadline = max(debug.deadline, time.monotonic() + 5)
                try:
                    if memory is not None and not calls.pending:
                        calls.call(91, memory, BUFFER_SIZE)
                    calls.restore()
                    debug.expect_ok("D;1")
                except (OSError, ValueError):
                    self.failed = True
                    try:
                        debug.stop()
                    except OSError:
                        pass
                    state = "QEMU left stopped" if not debug.running else "QEMU stop could not be confirmed"
                    raise OSError(f"filesystem debugger could not restore the task; {state}") from None
                finally:
                    debug.sock.close()

    @staticmethod
    def string(calls, address, value):
        calls.debugger.write(address, value.encode() + b"\0")
        return address

    def read_file(self, calls, memory, path):
        self.string(calls, memory, path)
        fd = calls.call(5, memory, 0x40800, 0)  # O_RDONLY | O_NONBLOCK | O_NOATIME
        try:
            calls.call(197, fd, memory + 8192)  # fstat64
            mode = struct.unpack_from("<I", calls.debugger.read(memory + 8192, 32), 16)[0]
            if not stat.S_ISREG(mode):
                raise OSError(errno.EINVAL, "not a regular file", path)
            data = bytearray()
            while True:
                count = calls.call(3, fd, memory + DATA_OFFSET, min(CHUNK, LIMIT + 1 - len(data)))
                if not count:
                    return bytes(data)
                data.extend(calls.debugger.read(memory + DATA_OFFSET, count))
                if len(data) > LIMIT:
                    raise OSError(errno.EFBIG, "filesystem transfers are limited to 16 MiB", path)
        finally:
            if not calls.pending:
                calls.call(6, fd)

    def write_file(self, calls, memory, path, data, permissions):
        original = self.string(calls, memory, path)
        exists = True
        try:
            calls.call(196, original, memory + 8192)  # lstat64
        except FileNotFoundError:
            exists = False
        if exists:
            metadata = calls.debugger.read(memory + 8192, 32)
            old_mode = struct.unpack_from("<I", metadata, 16)[0]
            uid, gid = struct.unpack_from("<II", metadata, 24)
            if not stat.S_ISREG(old_mode):
                raise OSError(errno.EINVAL, "destination is not a regular file", path)
        directory = self.string(calls, memory + 4096, posixpath.dirname(path))
        parent = calls.call(5, directory, O_DIRECTORY | 0x800, 0)
        temp = memory + 4096
        fd = None
        created = False
        try:
            self.string(calls, temp, path + ".mcp-" + secrets.token_hex(8))
            fd = calls.call(5, temp, O_NOFOLLOW | 0xc1, 0o600)  # O_WRONLY | O_CREAT | O_EXCL
            created = True
            offset = 0
            while offset < len(data):
                chunk = data[offset:offset + CHUNK]
                calls.debugger.write(memory + DATA_OFFSET, chunk)
                done = 0
                while done < len(chunk):
                    count = calls.call(4, fd, memory + DATA_OFFSET + done, len(chunk) - done)
                    if not count:
                        raise OSError(errno.EIO, "short guest write", path)
                    done += count
                offset += len(chunk)
            if exists:
                calls.call(207, fd, uid, gid)  # fchown32
            calls.call(94, fd, permissions if permissions is not None else
                       (old_mode & 0o777 if exists else 0o644))  # fchmod
            calls.call(118, fd)  # fsync
            closing = fd
            fd = None
            calls.call(6, closing)
            calls.call(38, temp, original)  # rename
            created = False
            calls.call(118, parent)
        finally:
            if fd is not None and not calls.pending:
                calls.call(6, fd)
            if created and not calls.pending:
                calls.call(10, temp)  # unlink
            if not calls.pending:
                calls.call(6, parent)
