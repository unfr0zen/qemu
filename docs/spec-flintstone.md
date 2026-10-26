# Flintstone (Nest Display-3.4, i.MX6SX) QEMU machine — hardware spec

The model boots the stock chain end to end — the boot ROM stub, U-Boot
2014.04, the vendor kernel and the Nest userspace — with the UI, WiFi and
the NAND read/write model working. Usage and what is modelled:
[`qemu.md`](qemu.md). Still missing: USB device-mode ACM,
ENET, `-icount` pacing.

Everything here follows the stock system: the firmware's Device Tree
(`fdt@3` in `/boot/kernel_fdt.itb` on `root0`, config `Display-3.4@3`), cross-checked
against the stock U-Boot 2014.04 image (`uboot-fw1.bin`) and the vendor
kernel config (4.1.15-5.9.4-5).

## Board identity

| Property | Value |
|---|---|
| DT model | `NestLabs D3 DVT Board` |
| DT compatible | `nestlabs,diamond3-dvt`, `fsl,imx6sx` |
| U-Boot banner board | `Board: Nest Flintstone` |
| env `nlmodel` | `Display-3.4` |
| CPU | 1× Cortex-A9 (DT lists one cpu@0; the 6SX's Cortex-M4 is ignored) |
| DRAM | 512 MiB @ `0x80000000` (DT `memory`: `<0x80000000 0x20000000>`) |
| Console | UART1 `ttymxc0` 115200 (`console=ttymxc0,115200` in stock bootargs) |

The 6SX is *not* an i.MX6Q: three internal buses (AIPS1 `0x02000000`, AIPS2
`0x02100000`, AIPS3 `0x02200000`), UART2–5 on AIPS2, GPMI subsystem on its own
`0x0180xxxx` segment, no PCIe. All IRQ numbers below are from `fdt@3`.

## Memories

| Region | Address | Size | Model |
|---|---|---|---|
| Boot ROM | `0x00000000` | 0x18000 | ROM region (the boot ROM stub writes it) |
| CAAM mem | `0x00100000` | 0x8000 | RAM (per i.MX6 precedent) |
| LPM SRAM | `0x008F8000` | 0x4000 | unimplemented stub |
| OCRAM (mega-fast) | `0x00900000` | 0x20000 | RAM |
| DDR3 | `0x80000000` | 512 MiB | RAM (no MMDC needed to *have* RAM) |

## Boot-critical peripherals (all reused from mainline i.MX6 models)

| Device | Address | IRQ | QEMU model |
|---|---|---|---|
| A9 MPCore (GIC/SCU/TWD) | `0x00A00000` | — | `a9mpcore` (160 IRQs wired: 128 SPI + 32 internal) |
| PL310 L2 | `0x00A02000` | — | `l2x0` |
| CCM | `0x020C4000` | 87/88 | `imx6.ccm` |
| SRC | `0x020D8000` | 91/96 | `imx6.src` |
| SNVS HP | `0x020CC000` | — | `imx7.snvs` |
| GPT | `0x02098000` | 55 | `imx6.gpt` (U-Boot/kernel timebase) |
| EPIT1/2 | `0x020D0000/0x020D4000` | 56/57 | `imx.epit` |
| UART1 | `0x02020000` | 26 | `imx-serial` → first `-serial` |
| UART2–5 | `0x021E8000`+n·0x4000 | 27–30 | `imx-serial` |
| UART6 | `0x022A0000` | 17 | `imx-serial` |
| GPIO1–7 | `0x0209C000`+n·0x4000 | 66..79 | `imx-gpio` (edge-sel + upper-pin) |
| I2C1–3 | `0x021A0000`+n·0x4000 | 36–38 | `imx-i2c` |
| I2C4 | `0x021F8000` | 35 | `imx-i2c` |
| uSDHC1–4 | `0x02190000`+n·0x4000 | 22–25 | `imx-usdhc` (capareg 0x057834b4) |
| USB PHY1/2 | `0x020C9000/0x020CA000` | 44/45 | `imx-usbphy` |
| USB OTG/host1/host2 | `0x02184000`+n·0x200 | 43/42/40 | `chipidea` |
| ENET1 | `0x02188000` | 118/119 | `imx-enet` (`fec-phy-num` prop) |
| ENET2 | `0x021B4000` | 102/103 | `imx-enet` |
| WDOG1/2/3 | `0x020BC000/0x020C0000/0x02288000` | 80/81/11 | `imx2.wdt` |

## Stubbed (unimplemented regions, reads 0 / writes dropped)

LPM-SRAM · PWM1–8 · FLEXCAN1/2 · ANATOP
`0x020C8000` · GPC `0x020DC000` · IOMUXC `0x020E0000` + GPR `0x020E4000` ·
SDMA · CAAM `0x02100000` · MLB · ROMCP · **MMDC `0x021B0000`** · WEIM ·
OCOTP `0x021BC000` · SAI1/2+AUDMUX · QSPI1/2 (+ mem windows
`0x60000000`/`0x70000000`) · QOSC · ADC1/2 · eCSPI4 · SEMA4 · MU.

## Stock boot image facts (from `uboot-fw1.bin`, IVT at file offset 0x400)

U-Boot 2014.04, DCD-based (no SPL), single copy used twice on NAND:

| IVT field | Value |
|---|---|
| entry | `0x87800000` |
| DCD | `0x877FF42C` (starts with `CCGR0..5 = 0xFFFFFFFF` clock-gate writes) |
| boot_data.start | `0x877FF000` |
| boot_data.size | `0x61000` (397 KiB — matches the ~405 KiB carved region) |
| self | `0x877FF400` |
| csf | `0x8785E000` |

NAND (mtd0) layout: FCB page 0 (vendor-encoded, **not** standard BCH18),
DBBT blocks 4–7, fw1 IVT at `0x100400`, identical fw2 at `0x280400`.
`mtdparts=mx6_nand:4m(u-boot),500m(ubipart),8m(oopsdata)`.

Stock boot flow (env): `bootcmd = run nandboot0 || run nandboot1 || run
mmcboot || reset`, where nandbootN mounts UBI volume `rootN`,
`ubifsload 0x83000000 /boot/kernel_fdt.itb`, `bootm ${fit_addr}#Display-3.4@3`.

## Model notes

- `mc->ignore_memory_transaction_failures = true` is required (sabrelite
  sets it too): without it, an unmodelled-register access raises a guest
  data abort and U-Boot's own abort handler re-aborts into an infinite
  vector loop (vectors `0x87800010` ↔ `0x87800200`).
- The APBH model must implement the SFTRST/CLKGATE latch semantics:
  `mxs_dma_reset()` polls `CTRL0` for them, and a read-0 stub can never
  satisfy it.

## Open questions

- OCOTP reads return 0 — if U-Boot/kernel gates on fuse values (e.g. boot
  mode fuses) we may need a tiny OCOTP model with plausible defaults.
