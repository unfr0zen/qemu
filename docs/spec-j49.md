# j49 (Nest Learning Thermostat gen 2, AM3703) QEMU machine — hardware spec

The model boots the stock chain end to end from its NAND image: the
GP x-loader, U-Boot 2012.10, Linux 2.6.37 and the Nest userspace, with the
UI, ring, button, piezo, backplate, WiFi and the Thread radio working.
Usage and what is modelled: [`qemu-j49.md`](qemu-j49.md).

Sources: the machine's stock U-Boot environment, kernel config and root
file system, and Nest's 5.9.4 GPL release
(`arch/arm/mach-omap2/board-diamond*.c`, the drivers).
QEMU 8.2 and qemu-linaro were references for the OMAP3
blocks, whose models QEMU dropped in v10; the ones here are new.

## Board identity

| Property | Value |
|---|---|
| env `nlmodel` | `Display-2.6` (board-diamond.c `DISPLAY_2_4_DATA`, PVT) |
| Kernel machine | `Nest J49` |
| SoC | TI AM3703 (OMAP3630 family): 1× Cortex-A8, MIDR `0x413fc082`, GP device |
| DRAM | 64 MiB mobile DDR @ `0x80000000` |
| NAND | Micron MT29F2G16, 256 MiB x16, GPMC CS0, BCH4 (7 bytes per 512 at OOB 36) |
| Console | UART1 `ttyO0` 115200 |

## Memories

| Region | Address | Size | Model |
|---|---|---|---|
| Boot ROM | `0x40014000` | 32 KiB | ROM: vectors, a monitor for the x-loader's SMCs, the launcher, boot parameters (device 2, NAND) |
| Secure ROM | `0x40000000` | 80 KiB | unimplemented |
| SRAM | `0x40200000` | 64 KiB | RAM: the x-loader runs here |
| SDRAM | `0x80000000` | 64 MiB | RAM; the rest of CS0/CS1's 1 GiB is unimplemented |
| GPMC CS0 | where `GPMC_CONFIG7_0` puts it | | the NAND's command/address/data ports |

## Peripherals

| Device | Address | IRQ | Notes |
|---|---|---|---|
| INTCPS | `0x48200000` | — | 96 level inputs, priorities, SIR |
| GPTIMER1–12 | `0x48318000`, `0x49032000`… | 37–47, 95 | GPTIMER11's PWM drives the piezo |
| 32 kHz sync timer | `0x48320000` | — | |
| CM / PRM | `0x48004000` / `0x48306000` | PRM 11 | DPLL locks, idle status, voltage processors |
| SCM / TAP | `0x48002000` / `0x4830a000` | — | ID `0x2b89102f` |
| SDRC / SMS | `0x6d000000` / `0x6c000000` | — | DLL lock, idle |
| GPMC | `0x6e000000` | 20 | NAND, prefetch engine, BCH4 |
| UART1–4 | `0x4806a000`, `0x4806c000`, `0x49020000`, `0x49042000` | 72, 73, 74, 80 | UART3 is the backplate (`ttyO2`) |
| I2C1–3 | `0x48070000`, `0x48072000`, `0x48060000` | 56, 57, 61 | |
| GPIO1–6 | `0x48310000`, `0x49050000`… | 29–34 | |
| MMC1–3 | `0x4809c000`, `0x480b4000`, `0x480ad000` | 83, 86, 94 | SDHCI at +0x100; DMA 61/62, 47/48, 77/78 |
| McSPI1–4 | `0x48098000`, `0x4809a000`, `0x480b8000`, `0x480ba000` | 65, 66, 91, 48 | DMA requests as Linux numbers them |
| SDMA | `0x48056000` | 12–15 | 32 channels |
| DSS / DSI | `0x48050000` / `0x4804fc00` | 25 | DISPC GFX scanout |
| WDT2 | `0x48314000` | — | |
| MUSB | `0x480ab000` | 92 | unplugged |

## Board

| Function | Where |
|---|---|
| TPS65921 | I2C1 `0x48`–`0x4b`, SYS_NIRQ on INTC 7; PWRON is the ring's click; battery on ADCIN0 through a divide-by-three |
| ADBS-A320 ring sensor | I2C2 `0x57`, MOTION_L on GPIO 108 |
| LM3530 backlight | I2C3 `0x36` |
| Panel (Tianma, s6d05a1) | McSPI1 CS0 (not modelled), DPI 24-bit, 320x320 |
| EM357 Thread radio | McSPI2 CS0 (spidev 2.0), nRESET GPIO 61, nHOST_INT GPIO 182 |
| WL1271 | MMC2 (1.8 V, `vmmc2`), WLAN_EN GPIO 103, WLAN_IRQ GPIO 102 |
| Piezo | GPTIMER11 PWM, nENABLE GPIO 58 |
| Battery disconnect | GPIO 161 |
| Backplate | UART3 |

## Boot

The image at NAND block 0 is the GP x-loader's: a little-endian size and
load address, then the code. The ROM loads it to SRAM and starts it there,
as the AM3703's ROM does for a GP device booting from NAND. The x-loader
loads U-Boot 2012.10 to SDRAM; U-Boot's `bootcmd` runs `nandboot0`
(`nboot.i` the kernel from 0x400000, `root=/dev/mtdblock7`). The kernel's
MTD table starts with the whole chip (`nand0`), so `root0` is `mtd7`.

The U-Boot environment is redundant (CRC32 of the variables, a generation
byte) at `0x340000` and `0x3a0000`; U-Boot boots the valid copy with the
higher generation.

## Model notes

- The GPMC's BCH4 result registers hold, in read mode, the syndrome-ready
  remainder (the computed parity XOR the stored one, times x^52 mod g),
  which the kernel's decoder (Nest's `omap_bch_decoder.c`) expects.
- An MMCHS soft reset resets the SDHCI core, not the card behind it: the
  SDIO card keeps the block sizes the host set.
- The system DMA serves a peripheral's request from a bottom half: the
  peripheral raising it is mid-access, and the transfer goes through its
  own registers.
- McSPI takes byte and halfword accesses (the system DMA moves 8-bit words
  through TX/RX), and in transmit-receive mode shifts a word out only once
  RX is read, so a TX DMA keeps pace with the RX one.
