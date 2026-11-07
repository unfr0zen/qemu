#!/usr/bin/env python3
"""Load an i.MX SDP image through the emulated mask ROM's USB HID interface."""
import argparse
from pathlib import Path
import struct
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'ui'))
from usbip import TokenBridge, control, transfer


def timed(function, seconds=10):
    cancelled = threading.Event()
    timer = threading.Timer(seconds, cancelled.set)
    timer.start()
    try:
        return function(cancelled)
    finally:
        timer.cancel()


def upload(socket, image, fit=None):
    data = image.read_bytes()
    if len(data) < 32 or data[:4] != bytes.fromhex('d1002040'):
        raise ValueError('SDP image must start with an i.MX IVT')
    address = struct.unpack_from('<I', data, 20)[0]
    deadline = time.monotonic() + 15
    while not socket.exists():
        if time.monotonic() > deadline:
            raise TimeoutError('QEMU USB socket did not appear')
        time.sleep(.02)
    bridge = TokenBridge(str(socket), timeout=5)
    try:
        bridge.wait_connect(timeout=15)
        bridge.reset()
        bridge.speed('hs')

        def request(rtype, kind, value=0, length=0, payload=b''):
            setup = struct.pack('<BBHHH', rtype, kind, value, 0, length)
            return timed(lambda cancelled: control(bridge, setup, payload, cancelled))

        descriptor = request(0x80, 6, 0x100, 18)
        if len(descriptor) != 18 or descriptor[8:12] != bytes.fromhex('a2157d00'):
            raise RuntimeError(f'expected i.MX6UL SDP device, got {descriptor.hex()}')
        request(0, 5, 5)
        request(0, 9, 1)

        def report(payload):
            request(0x21, 9, 0x200 | payload[0], len(payload), payload)

        def command(kind, count, destination=address):
            payload = bytearray(17)
            payload[0] = 1
            struct.pack_into('>HI', payload, 1, kind, destination)
            payload[7] = 0x20
            struct.pack_into('>I', payload, 8, count)
            report(payload)

        def receive():
            def read(cancelled):
                result = b''
                while True:
                    chunk = transfer(bridge, 1, 1, 64, cancelled)
                    result += chunk
                    if len(chunk) < 64:
                        return result
            return timed(read)

        def write_file(contents, destination):
            command(0x0404, len(contents), destination)
            for offset in range(0, len(contents), 1024):
                report(b'\x02' + contents[offset:offset + 1024].ljust(1024, b'\0'))
            hab, status = receive(), receive()
            if hab[:1] != b'\x03' or status[:5] != b'\x04\x88\x88\x88\x88':
                raise RuntimeError(f'SDP upload failed: {hab.hex()} {status.hex()}')

        if fit:
            write_file(fit.read_bytes(), 0x83000000)
            print('Onyx ROM: uploaded development FIT to 0x83000000', flush=True)
        write_file(data, address)
        command(0x0b0b, 0)
        hab = receive()
        if hab[:1] != b'\x03':
            raise RuntimeError(f'SDP jump returned no HAB state: {hab.hex()}')
        print(f'Onyx ROM: uploaded {len(data)} unchanged bytes, jumped to IVT {address:#x}; '
              f'HAB report {hab[1:5].hex()}', flush=True)
    finally:
        bridge.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--socket', type=Path, required=True)
    parser.add_argument('--image', type=Path, required=True)
    parser.add_argument('--fit', type=Path, help='FIT for the loader factory USB boot path')
    args = parser.parse_args()
    upload(args.socket, args.image, args.fit)


if __name__ == '__main__':
    main()
