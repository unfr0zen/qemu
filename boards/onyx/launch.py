#!/usr/bin/env python3
"""Launch Onyx NAND bring-up with an optional serial/QMP web console."""
import argparse
import os
from pathlib import Path
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import time


REPO = Path(__file__).resolve().parents[2]


def pick_port(start):
    while True:
        with socket.socket() as listener:
            try:
                listener.bind(("127.0.0.1", start))
            except OSError:
                start += 1
                continue
        return start


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def launch(args):
    extra = args.extra[1:] if args.extra[:1] == ["--"] else args.extra
    command = [args.qemu, "-M", "onyx", "-m", "128M",
               "-drive", f"if=none,id=nand,format=raw,file={args.nand.replace(',', ',,')},"
               f"snapshot={args.snapshot}", "-nic", "none", "-parallel", "none",
               "-watchdog-action", "none"]
    icount = os.environ.get("QEMU_ICOUNT", "off")
    if icount != "off":
        command += ["-icount", icount]
    container = f"onyx-{os.getpid()}"
    docker = ["docker", "run", "--rm", "--init", "--name", container,
              "-v", f"{REPO}:{REPO}", "-w", str(REPO)]
    if not args.ui:
        command += ["-nographic", *extra]
        if args.docker:
            docker += ["-it"] if sys.stdin.isatty() else ["-i"]
            command = docker + shlex.split(os.environ.get("QEMU_DOCKER_ARGS", "")) + [args.docker] + command
        os.execvp(command[0], command)

    runtime = REPO / ".qemu"
    runtime.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="onyx-ui-", dir=runtime) as directory:
        http = pick_port(args.ui_port)
        qmp = pick_port(8101)
        console = pick_port(qmp + 1)
        bind = "0.0.0.0" if args.docker else "127.0.0.1"
        command += ["-display", "none", "-monitor", "none",
                    "-qmp", f"tcp:{bind}:{qmp},server=on,wait=off",
                    "-chardev", f"socket,id=con,host={bind},port={console},"
                    "server=on,wait=on,nodelay=on", "-serial", "chardev:con", *extra]
        if args.docker:
            for port in (qmp, console):
                docker += ["-p", f"127.0.0.1:{port}:{port}"]
            command = docker + shlex.split(os.environ.get("QEMU_DOCKER_ARGS", "")) + [args.docker] + command
        ui_command = [sys.executable, str(REPO / "ui/ui.py"),
                      "--board", str(Path(directory) / "unmodelled-board.sock"),
                      "--qmp", f"tcp:127.0.0.1:{qmp}",
                      "--console", f"tcp:127.0.0.1:{console}", "--echo-console",
                      "--screen-file", str(Path(directory) / "screen.ppm"),
                      "--screen-period", "1", "--port", str(http),
                      "--name", "Nest onyx", "--parent", str(os.getpid())]
        keyboard = sys.stdin.isatty() and sys.stdout.isatty()
        if keyboard:
            ui_command.append("--keyboard")
        children = []

        def interrupted(signum, _frame):
            raise SystemExit(128 + signum)

        for sig in (signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, interrupted)
        try:
            ui = subprocess.Popen(ui_command, stdin=None if keyboard else subprocess.DEVNULL)
            children.append(ui)
            qemu = subprocess.Popen(command, stdin=subprocess.DEVNULL)
            children.append(qemu)
            print(f"onyx UI: http://127.0.0.1:{http}", file=sys.stderr)
            print("  Serial/QMP only; display, ring, backplate, radio and USB are not modelled.",
                  file=sys.stderr)
            while qemu.poll() is None:
                if ui.poll() is not None:
                    return ui.returncode or 1
                time.sleep(0.1)
            return qemu.returncode
        except KeyboardInterrupt:
            return 130
        finally:
            for child in reversed(children):
                stop(child)
            if args.docker:
                subprocess.run(["docker", "rm", "-f", container],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               check=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu", required=True)
    parser.add_argument("--nand", required=True)
    parser.add_argument("--snapshot", choices=("on", "off"), default="off")
    parser.add_argument("--ui", action="store_true")
    parser.add_argument("--ui-port", type=int, default=8075)
    parser.add_argument("--docker", metavar="IMAGE")
    parser.add_argument("extra", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not 1 <= args.ui_port <= 65535:
        parser.error("--ui-port must be between 1 and 65535")
    return launch(args)


if __name__ == "__main__":
    sys.exit(main())
