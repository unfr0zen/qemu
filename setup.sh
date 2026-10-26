#!/bin/sh
#
# Clone/patch/build the flintstone QEMU machine.
#
# Usage:
#   ./setup.sh            # native build (Linux, or macOS with brew deps)
#   ./setup.sh docker     # build inside Docker (default on macOS)
#
# Output: qemu-src/build-docker/qemu-system-arm (docker)
#         qemu-src/build/qemu-system-arm         (native)

set -e

script_path="$(cd "$(dirname "$0")" && pwd)"
repo_root="$script_path"
src="$script_path/qemu-src"

if [ ! -e "$src/.git" ]; then
    echo "--- Cloning QEMU v11.0.0 (same base as the dm-lumyo t33 stack) ---"
    git clone --depth 1 --branch v11.0.0 \
        https://github.com/qemu/qemu.git "$src" ||
    git clone --depth 1 --branch v11.0.0 \
        https://gitlab.com/qemu-project/qemu.git "$src"
fi

# The flintstone machine lives on top of the clone as the patch stack in
# patches/. They are applied directly into the tree (not via submodule
# commits) so they stay the single source of truth, like the t33 series.
if [ ! -f "$src/hw/arm/flintstone.c" ]; then
    echo "--- Applying patches ---"
    # One at a time: a single `git apply a.patch b.patch` checks every
    # patch against the base tree, so later patches can't touch files
    # that earlier ones create.
    for p in "$script_path"/patches/*.patch; do
        git -C "$src" apply "$p"
    done
fi

builddir="${QEMU_BUILD_DIR:-build}"

if [ "$1" = "docker" ]; then
    tag="imx6-qemu-build:$(shasum -a 256 "$script_path/docker/Dockerfile" | cut -c1-12)"
    if ! docker image inspect "$tag" > /dev/null 2>&1; then
        echo "--- Building Docker image $tag (one-time) ---"
        docker build --platform "linux/$(uname -m | sed 's/aarch64/arm64/')" \
            -t "$tag" "$script_path/docker"
    fi
    builddir=build-docker
    docker run --rm --init \
        -v "$repo_root:$repo_root" \
        -w "$src" \
        -u "$(id -u):$(id -g)" \
        -e "QEMU_BUILD_DIR=$builddir" \
        -e "GIT_CONFIG_COUNT=1" \
        -e "GIT_CONFIG_KEY_0=safe.directory" \
        -e "GIT_CONFIG_VALUE_0=*" \
        "$tag" sh -c '
            set -e
            mkdir -p "$QEMU_BUILD_DIR"
            if [ ! -f "$QEMU_BUILD_DIR/build.ninja" ]; then
                (cd "$QEMU_BUILD_DIR" && ../configure --target-list=arm-softmmu \
                    --enable-slirp --disable-docs --disable-sdl \
                    --disable-gtk --disable-vnc)
            fi
            ninja -C "$QEMU_BUILD_DIR" qemu-system-arm
        '
else
    # Native build
    # On macOS, configure probes libraries with Apple's cc, which searches
    # Homebrew's lib dir, but the link may run with a Homebrew clang (an
    # llvm@N on PATH) that does not; give both the same search path.
    if [ "$(uname -s)" = Darwin ] && command -v brew > /dev/null; then
        LIBRARY_PATH="$(brew --prefix)/lib${LIBRARY_PATH:+:$LIBRARY_PATH}"
        export LIBRARY_PATH
    fi
    cd "$src"
    mkdir -p "$builddir"
    if [ ! -f "$builddir/build.ninja" ]; then
        cd "$builddir"
        ../configure --target-list=arm-softmmu \
            --enable-slirp --disable-docs --disable-sdl --disable-gtk
    fi
    ninja -C "$src/$builddir" qemu-system-arm
fi
echo "--- Built: $src/$builddir/qemu-system-arm ---"
