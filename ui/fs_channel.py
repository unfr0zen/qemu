"""Binary file transfers over the guest helper's dedicated UART."""
import socket
import struct
import threading

LIMIT = 16 * 1024 * 1024
CHUNK = 1024


class FileChannel:
    def __init__(self, endpoint, mode):
        self.endpoint = endpoint
        self.mode = mode
        self.lock = threading.Lock()
        self.sock = None

    def close(self):
        if self.sock:
            self.sock.close()
            self.sock = None

    def receive(self, count):
        data = bytearray()
        while len(data) < count:
            chunk = self.sock.recv(count - len(data))
            if not chunk:
                raise OSError("filesystem channel disconnected")
            data.extend(chunk)
        return bytes(data)

    def transfer(self, path, data=None, mode=None, timeout=300):
        if not isinstance(path, str) or not path.startswith("/") or "\0" in path:
            raise ValueError("path must be absolute, without NUL bytes")
        encoded = path.encode()
        if len(encoded) >= 4000:
            raise ValueError("path is too long")
        if data is not None and self.mode != "rw":
            raise PermissionError("filesystem channel is read-only (--fs ro)")
        if data is not None and len(data) > LIMIT:
            raise ValueError("filesystem transfers are limited to 16 MiB")
        permissions = 0xffffffff
        if mode is not None:
            if not isinstance(mode, str) or not mode or any(c not in "01234567" for c in mode):
                raise ValueError("mode must be octal permissions, e.g. 755")
            permissions = int(mode, 8)
            if permissions > 0o777:
                raise ValueError("mode must be between 000 and 777")
        with self.lock:
            try:
                if self.sock is None:
                    host, port = self.endpoint.removeprefix("tcp:").rsplit(":", 1)
                    self.sock = socket.create_connection((host, int(port)), timeout)
                self.sock.settimeout(timeout)
                self.sock.sendall(struct.pack("!4sIIII", b"NFS1", 1 if data is None else 2,
                                              len(encoded), len(data or b""), permissions) + encoded)
                if data is not None:
                    for offset in range(0, len(data), CHUNK):
                        self.sock.sendall(data[offset:offset + CHUNK])
                        if self.receive(1) != b".":
                            raise OSError("invalid filesystem chunk acknowledgement")
                magic, error, size = struct.unpack("!4sII", self.receive(12))
                if magic != b"NFS1" or size > LIMIT:
                    raise OSError("invalid filesystem response")
                result = self.receive(size)
            except (OSError, ValueError):
                self.close()
                raise
            if error:
                raise OSError(error, result.decode(errors="replace"), path)
            return result
