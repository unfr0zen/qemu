#!/usr/bin/env python3
"""UART2 endpoint for Diamond's CC2530 radio; record host traffic.

The Diamond board connects its radio to UART2. This endpoint leaves the
radio silent while preserving the bytes sent by the guest for protocol work.
"""
import argparse
import os
import socket
import sys
import threading
import time


def follow_parent(pid):
    while os.getppid() == pid:
        time.sleep(1)
    os._exit(0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", required=True, help="tcp:HOST:PORT UART2 chardev")
    ap.add_argument("--parent", type=int, help="exit when this PID exits")
    args = ap.parse_args()
    _, host, port = args.serial.split(":", 2)
    if args.parent:
        threading.Thread(target=follow_parent, args=(args.parent,),
                         daemon=True).start()
    while True:
        try:
            with socket.create_connection((host, int(port))) as radio:
                print("diamond radio: UART2 connected", file=sys.stderr,
                      flush=True)
                while data := radio.recv(4096):
                    print(f"diamond radio: tx {data.hex(' ')}", file=sys.stderr,
                          flush=True)
        except OSError:
            time.sleep(0.5)


if __name__ == "__main__":
    main()
