# Build environment for the flintstone QEMU machine (see qemu/setup.sh docker).
# Matches the monorepo approach: Debian + the minimal set QEMU needs for a
# TCG-only qemu-system-arm build; run.sh runs QEMU in this same image. Host
# arch is followed (--platform), which is what we want: the guest is always
# emulated anyway.
FROM debian:12

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y \
        --no-install-recommends \
        git \
        build-essential \
        cpio \
        flex \
        bison \
        pkg-config \
        libglib2.0-dev \
        libpixman-1-dev \
        zlib1g-dev \
        libslirp-dev \
        python3 \
        python3-pip \
    && pip3 install --break-system-packages 'meson>=1.5' ninja \
    && rm -rf /var/lib/apt/lists/*
