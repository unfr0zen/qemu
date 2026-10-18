# CI HDRC UDC <-> virtual USB host protocol

The QEMU device `cihdrc-udc` (hw/usb/cihdrc-udc.c) emulates the ChipIDEA
USB controller in device (peripheral) mode as seen by the guest kernel
driver (drivers/usb/chipidea, 4.1.15). The USB *host* side lives outside
QEMU: a Python process speaking this line protocol over a chardev socket
(-chardev socket,path=...,server=on,wait=off + the machine's usb0-hostchar
property). All payloads are lowercase hex. Lines end with \n. The Python
side owns all USB protocol logic (enumeration, descriptors, MSC/BOT); the
QEMU side is only the UDC transport: registers, dQH/dTD DMA, IRQs.

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
    RESET                   USB bus reset. Model: sets USBSTS_URI/PCI,
                            clears DEVICEADDR, reverts to addressed-default
                            state, answers RESETDONE.
    SPEED <hs|fs>           Reported port speed (PORTSC PSPD). Default hs.

## QEMU -> host

    INDATA <ep> <hex>       Reply to INEP. Empty payload = NAK (nothing
                            primed or zero-length packet).
    SETUPACK                The guest driver took the SETUP (ENDPTSETUPSTAT
                            bit cleared after it read the buffer).
    RESETDONE               Reply to RESET.
    CONNECT                 The gadget driver enabled the UDC (USBMODE
                            device + run/session active) - virtual cable
                            attached.
    DISCONNECT              UDC stopped (module unload / soft disconnect).

Error/unprimed cases never drop the connection; both sides tolerate
reordering of unrelated endpoint traffic. The Python host serializes one
control transfer at a time on ep0.
