# Build environment for the flintstone QEMU machine (see qemu/setup.sh docker).
# Matches the monorepo approach: Debian + the minimal set QEMU needs for a
# TCG-only qemu-system-arm build; run.sh runs QEMU in this same image. Host
# arch is followed (--platform), which is what we want: the guest is always
# emulated anyway.
#
# Two stages: tools, what setup.sh docker builds locally; and ci, CI's image on
# Docker Hub (docker/.circleci/config.yml), which adds QEMU's base at QEMU_BASE
# already compiled through ccache, so applying the patch stack rebuilds only
# what it touches.
FROM debian:12 AS tools

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y \
        --no-install-recommends \
        git \
        build-essential \
        ccache \
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
        python3-pytest \
    && pip3 install --break-system-packages 'meson>=1.5' ninja \
    && rm -rf /var/lib/apt/lists/*

FROM tools AS ci
ARG QEMU_BASE
# ccache without the directory in its hashes: the tree is built here under
# /opt and in CI under the checkout, from the same relative paths.
ENV CC="ccache gcc" CCACHE_DIR=/opt/ccache CCACHE_NOHASHDIR=1
RUN git clone --depth 1 --branch "$QEMU_BASE" https://gitlab.com/qemu-project/qemu.git /opt/qemu \
    && echo "$QEMU_BASE" > /opt/qemu-base \
    && mkdir /opt/qemu/build && cd /opt/qemu/build \
    && ../configure --target-list=arm-softmmu --enable-slirp --disable-docs --disable-sdl --disable-gtk \
    && ninja \
    && cd /opt/qemu && rm -rf build \
    && ccache -s
