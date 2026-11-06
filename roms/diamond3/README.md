# Diamond3 boot ROM

`bootrom.bin` is the complete i.MX6SoloX revision 1.2 mask-ROM dump:
98,304 bytes mapped at `0x00000000..0x00017fff`, including reset vectors.

SHA-256: `7da7fb949eb6c96501a12a2057c48fc682ee58069aa0391960433200a442f25c`.

From the QEMU repository:

```sh
qemu-src/build/qemu-system-arm \
  -M diamond3,boot-rom="$PWD/roms/diamond3/bootrom.bin" \
  -display none -serial stdio -monitor none
```

This selects real ROM execution instead of the emulator's NAND loader
stub. Fuse state, boot straps and HAB hardware dependencies require
additional modelling before ROM-authenticated boot can be verified.
