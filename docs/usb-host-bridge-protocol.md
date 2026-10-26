# CI HDRC UDC <-> virtual USB host protocol

`cihdrc-udc` emulates the gen 3 guest's ChipIdea USB device controller:
registers, DMA and interrupts. A host process connects through the
machine's `usb0-hostchar` chardev. Lines end with `\n`; data is lowercase hex.
`ui/usbhost.py` provides a local virtual host, and `ui/usbip.py` forwards
USB requests to a host USB stack.

## Host attachment

On Linux, `./run.sh` loads `vhci_hcd`, exports the guest gadget over USB/IP,
and attaches it automatically. The guest supplies descriptors and USB data.
Mass storage and CDC-ACM use the host's normal drivers. This requires
`vhci_hcd`, `modprobe`, `usbip`, and root or non-interactive sudo access.
Without them, a plain run boots without host USB. `--usb` requires USB;
`--no-usb` disables it. The exporter detaches on exit and reattaches after
a gadget reconnect. Gen 2 has no USB bridge.

macOS has no local virtual USB host controller in this project. `./run.sh
--usb` exports the gadget on loopback TCP port 3240 (or the next free port).
To attach from Linux, tunnel the port printed by `run.sh`:

```sh
ssh -N -L 3240:127.0.0.1:3240 mac-host
sudo modprobe vhci-hcd
sudo usbip attach -r 127.0.0.1 -b 1-1
```

Only one importer can use the bridge. Control, bulk and interrupt URBs are
supported; isochronous transfers and alternate endpoint settings are not.
[USB/IP protocol](https://www.kernel.org/doc/html/latest/usb/usbip_protocol.html).

## Cable control

With `./run.sh --ui`, MCP tool `usb` accepts `{"connected": false}` to
unplug and `{"connected": true}` to plug the cable back in. An empty
object reads the state. The UI API exposes the same control through
`POST /usb`; `GET /usb` reads it. Both return `connected` (the requested
cable state) and `vbus` (the effective voltage, which also requires an
open host bridge). Gen 2 does not expose this control.

Unplugging drops OTGSC B-session-valid and USB1 analog VBUS/charger
status, and latches the VBUS-change interrupt. The stock driver updates
`/sys/devices/soc0/soc/2100000.aips-bus/2184000.usb/power_supply/imx_usb_charger/present`
asynchronously. USB/IP terminates the active import; Linux auto-attach
removes the host device and enumerates it again when the cable is plugged
back in. A manual importer must attach again. The exporter stays running,
and controller or guest resets preserve the requested cable state.

The QMP properties are `/machine/usb-connected` (read/write) and
`/machine/usb-vbus` (read-only). Repeating the current cable setting does
not create an interrupt. Device-controller reset preserves OTG interrupt
status and enables; machine reset clears them.

To check the complete MCP-to-firmware-to-host path, boot the unit with
`./run.sh --ui --usb`, find its local bus ID with `usbip port`, and run:

```sh
python3 tests/check_usb_cable.py --url http://127.0.0.1:8075 --host-busid 5-1
```

This requires an idle guest shell. It checks three unplug/plug cycles,
charger `present`, VBUS registers, host removal, and fresh enumeration.
It prints JSON observations and leaves the cable plugged in.

## host -> QEMU

    SETUP <16 hex>          8-byte setup packet. The model writes it into
                            the dQH0 setup buffer, sets ENDPTSETUPSTAT bit
                            for ep0, raises the UDC interrupt.
    INEP <ep> <maxbytes>    IN token on endpoint <ep> (0 = ep0 IN). The
                            model walks the primed dTD chain for that
                            endpoint, takes up to <maxbytes>, DMAs them out
                            of guest memory, completes dTDs (bytes
                            accounting, IOC), sets ENDPTCOMPLETE, and
                            answers INDATA (ZLP/NAK when nothing primed).
    OUTEP <ep> <hex>        OUT token with data. The model DMAs the bytes
                            into the primed dTD chain for the endpoint,
                            completes dTDs, sets ENDPTCOMPLETE, IRQ.
    TOKENIN <ep> <maxbytes> Acknowledged IN token (endpoint number 0..3).
    TOKENOUT <ep> <hex>     Acknowledged OUT token. Empty payload = ZLP;
                            retry NAK at the host.
    RESET                   USB bus reset. Model: sets USBSTS_URI/PCI,
                            clears DEVICEADDR, reverts to addressed-default
                            state, answers RESETDONE.
    SPEED <hs|fs>           Reported port speed (PORTSC PSPD). Default hs.

## QEMU -> host

    INDATA <ep> <hex>       Reply to INEP. Empty payload = NAK (nothing
                            primed or zero-length packet).
    TOKEN <ep> <IN|OUT> <ACK|NAK|STALL|ERROR> <payload>
                            Reply to TOKENIN/TOKENOUT. IN payload is hex;
                            OUT payload is the number of bytes consumed.
                            Empty ACK is ZLP; NAK consumes nothing.
                            STALL is endpoint halt; ERROR is DMA failure.
    SETUPACK                The guest driver took the SETUP (ENDPTSETUPSTAT
                            bit cleared after it read the buffer).
    RESETDONE               Reply to RESET.
    CONNECT                 VBUS is present and the gadget driver enabled
                            the UDC (USBMODE device + run/session active).
    DISCONNECT              Cable unplugged or UDC stopped (module unload
                            / soft disconnect).

Unprimed endpoints return NAK without closing the bridge. The host
serializes control transfers on EP0.
