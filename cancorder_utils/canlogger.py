#!/usr/bin/env python3
"""
ECUconnect CAN Logger Simulator

A stand-in for the ECUconnect CAN Logger hardware.
Uses python-can with gs_usb interface (CANable, etc.) to read CAN frames
and streams them via TCP in the ECUconnect Logger binary format.

Packet format: [timestamp:8][id:4][flags:1][dlc:1][data:0-64]
- timestamp: uint64_t, big-endian, microseconds since epoch
- id: uint32_t, big-endian
- flags: uint8_t bitfield (bit0=EXT, bit1=FD, bit2=BRS, bit3=ESI)
- dlc: uint8_t
- data: 0-64 bytes

Default TCP port: 2518
"""

import argparse
import can
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Optional, Tuple

from .zeroconf_service import LoggerZeroconfService, SERVICE_NAME_PREFIX
from .canlogger_common import (
    HEADER_SIZE,
    AUTO_PORT_START,
    ts,
    color,
    find_available_port,
    zeroconf_log,
    pack_frame as _pack_frame,
    ClientManager,
    client_handler_thread,
    tcp_server_thread,
    BINARY_PROTOCOL_HELP,
    TESTING_CONNECTION_HELP,
    SEE_ALSO_HELP,
)

DEFAULT_BITRATE = 500000
DEFAULT_INTERFACE = "gs_usb"

HELP_DESCRIPTION = """\
ECUconnect CAN Logger Simulator
===============================

A software stand-in for the ECUconnect CAN Logger hardware. This tool reads
CAN frames from a USB CAN adapter and streams them via TCP using the same
binary protocol as the real ECUconnect Logger device.

Use this to test applications that expect to connect to an ECUconnect Logger
without needing the actual hardware.
"""

HELP_EPILOG = BINARY_PROTOCOL_HELP + """

SUPPORTED CAN INTERFACES
========================

gs_usb (recommended for macOS)
  USB CAN adapters using the gs_usb protocol. Requires libusb access.
  Supported devices:
    • CANable / CANable Pro
    • candleLight / candleLightFD
    • USB2CAN
    • BudgetCAN
    • canable.io devices

  Usage:    --interface gs_usb --channel 0
  macOS:    Requires sudo for USB access
  Linux:    Use socketcan instead (kernel driver provides better integration)

socketcan (Linux only)
  Native Linux CAN interface via the kernel's SocketCAN subsystem.

  Setup:    sudo ip link set can0 up type can bitrate 500000
  Usage:    --interface socketcan --channel can0

  Also supports virtual CAN for testing:
  Setup:    sudo modprobe vcan && sudo ip link add vcan0 type vcan
            sudo ip link set vcan0 up
  Usage:    --interface socketcan --channel vcan0

slcan (Serial/USB-serial adapters)
  CAN adapters using the SLCAN/LAWICEL protocol over serial ports.

  Usage:    --interface slcan --channel /dev/ttyUSB0
  macOS:    --interface slcan --channel /dev/tty.usbmodem1234


EXAMPLES
========

Basic usage (gs_usb on macOS):
  sudo python3 %(prog)s

With verbose output (shows each CAN frame):
  sudo python3 %(prog)s -v

Auto-detect CAN interface:
  sudo python3 %(prog)s --auto

Custom port and bitrate:
  sudo python3 %(prog)s --port 3000 --bitrate 250000

Linux with SocketCAN:
  python3 %(prog)s --interface socketcan --channel can0

SLCAN adapter:
  python3 %(prog)s --interface slcan --channel /dev/ttyUSB0

Multiple CAN channels (run multiple instances):
  python3 %(prog)s -i socketcan -c can0 -p 2518 &
  python3 %(prog)s -i socketcan -c can1 -p 2519 &


""" + TESTING_CONNECTION_HELP + """

PLATFORM NOTES
==============

macOS:
  • Use gs_usb interface (direct USB access via libusb)
  • Requires sudo for USB device access
  • Install: pip3 install python-can gs_usb pyserial

Linux:
  • Prefer socketcan interface (kernel driver)
  • No sudo needed if user is in 'dialout' or 'can' group
  • Set up interface first: sudo ip link set can0 up type can bitrate 500000
  • Install: pip3 install python-can pyserial

Windows:
  • Use slcan or supported USB adapters
  • May need vendor drivers installed
  • Install: pip3 install python-can pyserial


TROUBLESHOOTING
===============

"Access denied (insufficient permissions)" on macOS:
  → Run with sudo: sudo python3 %(prog)s

"No CAN device found":
  → Check USB connection: system_profiler SPUSBDataType | grep -i can
  → Try --auto flag to auto-detect
  → Verify correct interface/channel options

"Network interface not found" on Linux:
  → Bring up CAN interface: sudo ip link set can0 up type can bitrate 500000
  → Check interface exists: ip link show can0

High CPU usage:
  → Normal when receiving high-volume CAN traffic
  → Use without -v flag to reduce console output overhead

Clients not receiving data:
  → Verify CAN bus has traffic (use -v to see frames)
  → Check firewall allows TCP port 2518
  → Ensure CAN bitrate matches the bus


""" + SEE_ALSO_HELP

def check_dependencies():
    """Check and import required dependencies."""
    global can, gs_usb

    try:
        import can as can_module
        can = can_module
    except ImportError:
        print("Error: python-can not installed.")
        print()
        print("Install with:")
        print("  pip3 install python-can")
        print()
        print("For gs_usb support (CANable, candleLight, etc.):")
        print("  pip3 install python-can gs_usb")
        print()
        print("For serial/SLCAN support:")
        print("  pip3 install python-can pyserial")
        sys.exit(1)

    try:
        import gs_usb as gs_usb_module
        gs_usb = gs_usb_module
    except ImportError:
        gs_usb = None




def pack_frame(msg: can.Message) -> bytes:
    """Pack a CAN message into ECUconnect Logger binary format."""
    ts_us = int(time.time() * 1_000_000)
    return _pack_frame(
        ts_us,
        msg.arbitration_id,
        msg.is_extended_id,
        bytes(msg.data),
        is_fd=bool(getattr(msg, "is_fd", False) or len(msg.data) > 8),
        bitrate_switch=bool(getattr(msg, "bitrate_switch", False)),
        error_state_indicator=bool(getattr(msg, "error_state_indicator", False)),
    )


def open_can_bus(interface: str, channel: object, bitrate: int, **extra_kwargs: object) -> can.Bus:
    """
    Open CAN bus with FD capability preferred and graceful fallback.

    Some backends reject `fd=True`; retry without it in that case.
    """
    kwargs = {
        "interface": interface,
        "channel": channel,
        "bitrate": bitrate,
        **extra_kwargs,
    }
    try:
        return can.Bus(fd=True, **kwargs)
    except TypeError:
        return can.Bus(**kwargs)
    except Exception:
        return can.Bus(**kwargs)




def find_gs_usb_device() -> Optional[can.Bus]:
    """Try to find and open a gs_usb device."""
    try:
        bus = open_can_bus("gs_usb", 0, DEFAULT_BITRATE)
        return bus
    except Exception as e:
        print(f"{ts()} {color('[can]', '31')} gs_usb init failed: {e}")
        return None


def find_any_can_device(bitrate: int) -> Optional[Tuple[can.Bus, str]]:
    """Try to find any available CAN device."""
    interfaces_to_try = [
        ("gs_usb", {"channel": 0}),
        ("slcan", {"channel": "/dev/tty.usbmodem*", "ttyBaudrate": 115200}),
        ("socketcan", {"channel": "can0"}),
        ("socketcan", {"channel": "vcan0"}),
    ]

    for interface, kwargs in interfaces_to_try:
        try:
            channel = kwargs.get("channel")
            extras = {k: v for k, v in kwargs.items() if k != "channel"}
            bus = open_can_bus(interface, channel, bitrate, **extras)
            print(f"{ts()} {color('[can]', '32')} Found CAN device: {interface} ({bus.channel_info})")
            return bus, interface
        except Exception:
            continue

    return None


def can_receiver_thread(bus: can.Bus, client_manager: ClientManager, verbose: bool):
    """Thread that receives CAN frames and broadcasts to clients."""
    frame_count = 0
    start_time = time.time()

    print(f"{ts()} {color('[can]', '32')} CAN receiver started on {bus.channel_info}")

    for msg in bus:
        if getattr(msg, "is_error_frame", False):
            if verbose:
                print(f"{ts()} {color('[can]', '33')} SKIP error frame")
            continue

        if msg.is_remote_frame:
            if verbose:
                print(f"{ts()} {color('[can]', '33')} SKIP remote frame id=0x{msg.arbitration_id:X}")
            continue

        frame_count += 1
        packet = pack_frame(msg)

        if client_manager.client_count() > 0:
            client_manager.broadcast(packet)

        if verbose:
            id_str = f"{msg.arbitration_id:08X}" if msg.is_extended_id else f"{msg.arbitration_id:03X}"
            data_hex = bytes(msg.data).hex()
            clients = client_manager.client_count()
            print(f"{ts()} {color('[can]', '36')} #{frame_count:5d} ID={id_str} DLC={len(msg.data)} DATA={data_hex} -> {clients} client(s)")




def main():
    parser = argparse.ArgumentParser(
        description=HELP_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=HELP_EPILOG,
    )
    parser.add_argument(
        "--port", "-p",
        type=int,
        default=None,
        metavar="PORT",
        help="TCP server port (default: auto-pick >=42420)."
    )
    parser.add_argument(
        "--bitrate", "-b",
        type=int,
        default=DEFAULT_BITRATE,
        metavar="BPS",
        help=f"CAN bus bitrate in bits/second (default: {DEFAULT_BITRATE}). "
             "Common values: 125000, 250000, 500000, 1000000"
    )
    parser.add_argument(
        "--interface", "-i",
        type=str,
        default=DEFAULT_INTERFACE,
        metavar="IFACE",
        help="python-can interface type (default: gs_usb). "
             "Options: gs_usb, socketcan, slcan, pcan, vector, etc."
    )
    parser.add_argument(
        "--channel", "-c",
        type=str,
        default="0",
        metavar="CHAN",
        help="CAN channel/device identifier (default: 0). "
             "For gs_usb: 0, 1, etc. "
             "For socketcan: can0, vcan0, etc. "
             "For slcan: /dev/ttyUSB0, /dev/tty.usbmodem1234, etc."
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print each received CAN frame to stdout. "
             "Useful for debugging but may impact performance at high bus loads."
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Auto-detect CAN interface. Tries gs_usb, slcan, and socketcan "
             "in sequence until one succeeds."
    )
    parser.add_argument(
        "--service-name",
        type=str,
        default=None,
        metavar="NAME",
        help="Custom Zeroconf service name. "
             "Defaults to 'ECUconnect-Logger <hostname>:<port>'."
    )
    parser.add_argument(
        "--no-zeroconf",
        action="store_true",
        help="Disable Zeroconf service advertisement."
    )

    args = parser.parse_args()

    requested_port = args.port
    if requested_port is None:
        try:
            listen_port = find_available_port()
        except RuntimeError as e:
            print(f"{ts()} {color('[error]', '31')} {e}")
            return 1
    else:
        listen_port = requested_port

    check_dependencies()

    port_label = f"{listen_port}" if requested_port is not None else f"{listen_port} (auto)"
    print(f"{ts()} {color('[init]', '34')} ECUconnect Logger Simulator starting...")
    print(f"{ts()} {color('[init]', '34')} TCP port: {port_label}, CAN bitrate: {args.bitrate}")

    bus = None

    if args.auto:
        result = find_any_can_device(args.bitrate)
        if result:
            bus, args.interface = result
        else:
            bus = None
    else:
        try:
            channel = int(args.channel) if args.channel.isdigit() else args.channel
            bus = open_can_bus(args.interface, channel, args.bitrate)
            print(f"{ts()} {color('[can]', '32')} CAN bus opened: {args.interface} ({bus.channel_info})")
        except Exception as e:
            print(f"{ts()} {color('[can]', '31')} Failed to open CAN bus: {e}")
            channel_input = args.channel if isinstance(args.channel, str) else str(args.channel)
            prefer_socketcan = (
                args.interface == DEFAULT_INTERFACE
                and channel_input.lower().startswith(("can", "vcan"))
            )
            if prefer_socketcan:
                print(f"{ts()} {color('[can]', '34')} Retrying with socketcan interface on channel {channel_input}...")
                try:
                    bus = open_can_bus("socketcan", channel_input, args.bitrate)
                    args.interface = "socketcan"  # Update interface to reflect actual usage
                    print(f"{ts()} {color('[can]', '32')} CAN bus opened: socketcan ({bus.channel_info})")
                except Exception as sock_err:
                    print(f"{ts()} {color('[can]', '31')} SocketCAN retry failed: {sock_err}")
            if bus is None:
                print(f"{ts()} {color('[can]', '33')} Trying auto-detection...")
                result = find_any_can_device(args.bitrate)
                if result:
                    bus, args.interface = result

    if bus is None:
        print(f"{ts()} {color('[error]', '31')} No CAN device found!")
        print()
        print("Make sure you have a supported CAN adapter connected:")
        print("  - CANable (gs_usb)")
        print("  - SLCAN device")
        print("  - SocketCAN interface (Linux)")
        print()
        print("Install required packages:")
        print("  pip3 install python-can gs_usb pyserial")
        sys.exit(1)

    client_manager = ClientManager()

    server_thread = threading.Thread(
        target=tcp_server_thread,
        args=(listen_port, client_manager, None),
        daemon=True
    )
    server_thread.start()

    can_thread = threading.Thread(
        target=can_receiver_thread,
        args=(bus, client_manager, args.verbose),
        daemon=True
    )
    can_thread.start()

    print(f"{ts()} {color('[ready]', '32')} Simulator ready. Clients can connect to port {listen_port}")
    print(f"{ts()} {color('[ready]', '32')} Press Ctrl+C to stop")

    zeroconf_service = None
    if not args.no_zeroconf:
        default_name = args.service_name or f"{SERVICE_NAME_PREFIX} {socket.gethostname()}:{listen_port}"
        metadata = {
            "process": Path(__file__).name,
            "interface": args.interface,
            "channel": args.channel,
            "bitrate": str(args.bitrate),
        }
        zeroconf_service = LoggerZeroconfService(
            port=listen_port,
            service_name=default_name,
            properties=metadata,
            logger=zeroconf_log,
        )
        zeroconf_service.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print(f"\n{ts()} {color('[exit]', '33')} Shutting down...")
        stats = client_manager.stats
        print(f"{ts()} {color('[stats]', '34')} Frames sent: {stats['frames_sent']}, dropped: {stats['frames_dropped']}, bytes: {stats['bytes_sent']}")
    finally:
        if zeroconf_service:
            zeroconf_service.stop()

        bus.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
