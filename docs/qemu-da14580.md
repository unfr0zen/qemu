# Diamond3 BLE UART

Diamond3's UART5 connects to a DA14580 transport model when serial port 5
has no explicit backend. GPIO5_IO16 resets it, active high. Use `-serial
none` for that port when supplying all six serial arguments. An explicit
UART5 chardev overrides the built-in model.

The default personality models the UART boot ROM: repeated STX (`02`),
SOH (`01`) plus a little-endian image length, ACK, image bytes, XOR
checksum and the host's final ACK. Firmware bytes are consumed, not
executed. The resulting GTL endpoint sends `GAPM_DEVICE_READY_IND` and
handles reset, device configuration, address queries, advertising/scan
start and cancellation. Unsupported operations return GTL errors; Nest's
custom application task is not implemented.

## BlueZ test personality

`-M diamond3,ble-hci-test=on` selects synthetic HCI test firmware instead
of the boot-ROM/GTL personality. This is not a DA14580 HCI firmware image
or evidence that the stock chip supports HCI. It allows Linux's H4 UART
driver and BlueZ to exercise an emulated BLE controller.

From the Buildroot guest:

```sh
modprobe hci_uart
btattach -B /dev/ttyble0 -P h4 -S 57600 -N &
bluetoothctl power on
```

The QOM object `/chardevs/diamond3-ble` has two writable Boolean properties:

| Property | Effect |
|---|---|
| `hci-test-firmware` | Select the HCI test personality and reset protocol state |
| `peer-present` | Advertise an optional virtual peripheral while scanning |

Change properties with QMP `qom-set`; stop `btattach` before switching
personalities. A full machine reset preserves personality selection.
A GPIO reset clears download, connection and scanning state.

The test peer is `02:00:00:00:00:01`, named `QEMU BLE test peer`, with
synthetic RSSI −45 dBm. It supports active/passive discovery, one central
connection, 23-byte ATT MTU, GAP Device Name/Appearance and Device
Information Manufacturer (`QEMU`) reads, and disconnect. It is absent by
default. There is no host Bluetooth passthrough, RF link, pairing,
encryption, notifications, arbitrary peripheral application or DA14580
Cortex-M0 execution. The advertised capabilities exclude encryption.

```sh
QEMU_SYSTEM_ARM="$PWD/qemu-src/build/qemu-system-arm" \
  python3 -m unittest tests/test_da14580.py -v
```

Protocol references: [Renesas GTL interface](https://www.renesas.com/en/document/mat/um-b-143-renesas-external-processor-interface-gtl-interface),
[Renesas GTL message identifiers](https://github.com/renesas/Saleae_GTL_Decoder/blob/main/GTL_definitions.py),
and the Linux Bluetooth HCI/ATT definitions. Stock DA14580 firmware uses
GTL; Renesas documents the [lack of a general DA14580 HCI release](https://community.renesas.com/wireles-connectivity/f/bluetooth-low-energy/27102/hci-software-release-for-da14580/72829).
