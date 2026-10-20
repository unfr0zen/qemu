#!/usr/bin/env python3
"""Build the static ARM filesystem helper; print its absolute path."""
import argparse
import os
from pathlib import Path
import shlex
import shutil
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(Path(__file__).resolve().parents[1] /
                                               ".qemu" / "fs-agent"))
    args = parser.parse_args()
    source = Path(__file__).with_name("fs_agent.c")
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if os.environ.get("FS_CC"):
        compiler = shlex.split(os.environ["FS_CC"])
    else:
        compiler = None
        for name in ("arm-linux-gnueabihf-gcc", "arm-none-linux-gnueabihf-gcc", "zig"):
            if shutil.which(name):
                compiler = ([name, "cc", "-target", "arm-linux-musleabihf"]
                            if name == "zig" else [name])
                break
        if not compiler:
            parser.error("--fs needs arm-linux-gnueabihf-gcc or zig (or set FS_CC)")
    subprocess.run([*compiler, "-Os", "-static", "-s",
                    "-Wall", "-Wextra", "-Werror", str(source), "-o", str(output)], check=True)
    print(output)


if __name__ == "__main__":
    main()
