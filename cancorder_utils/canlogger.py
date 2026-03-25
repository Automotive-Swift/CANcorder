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
from typing import Callable, Optional, Tuple

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
    RateLimitedLogger,
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


def open_requested_bus(interface: str, channel_arg: str, bitrate: int, auto: bool) -> Tuple[can.Bus, str]:
    """Open the requested CAN bus or raise RuntimeError."""
    if auto:
        result = find_any_can_device(bitrate)
        if result is None:
            raise RuntimeError("No CAN device found during auto-detection")
        bus, detected_interface = result
        return bus, detected_interface

    channel = int(channel_arg) if channel_arg.isdigit() else channel_arg
    try:
        bus = open_can_bus(interface, channel, bitrate)
        return bus, interface
    except Exception as exc:
        channel_input = channel_arg if isinstance(channel_arg, str) else str(channel_arg)
        prefer_socketcan = (
            interface == DEFAULT_INTERFACE
            and channel_input.lower().startswith(("can", "vcan"))
        )
        if prefer_socketcan:
            try:
                bus = open_can_bus("socketcan", channel_input, bitrate)
                return bus, "socketcan"
            except Exception as socketcan_exc:
                raise RuntimeError(
                    f"Failed to open CAN bus via {interface} ({exc}); "
                    f"socketcan retry failed ({socketcan_exc})"
                ) from socketcan_exc
        raise RuntimeError(f"Failed to open CAN bus via {interface}: {exc}") from exc


def can_receiver_thread(
    open_bus: Callable[[], Tuple[can.Bus, str]],
    client_manager: ClientManager,
    verbose: bool,
    stop_event: threading.Event,
):
    """Maintain a CAN connection and forward frames to TCP clients."""
    frame_count = 0
    bus: Optional[can.Bus] = None
    verbose_limiter = RateLimitedLogger()

    while not stop_event.is_set():
        if bus is None:
            try:
                bus, active_interface = open_bus()
                print(f"{ts()} {color('[can]', '32')} CAN bus opened: {active_interface} ({bus.channel_info})")
            except Exception as exc:
                print(f"{ts()} {color('[can]', '31')} {exc}")
                if stop_event.wait(1.0):
                    break
                print(f"{ts()} {color('[can]', '34')} Retrying CAN connection...")
                continue

        try:
            msg = bus.recv(timeout=0.5)
        except Exception as exc:
            print(f"{ts()} {color('[can]', '31')} CAN interface lost on {bus.channel_info}: {exc}")
            try:
                bus.shutdown()
            except Exception:
                pass
            bus = None
            if stop_event.wait(1.0):
                break
            print(f"{ts()} {color('[can]', '34')} Retrying CAN connection...")
            continue

        if msg is None:
            continue

        if getattr(msg, "is_error_frame", False):
            if verbose:
                verbose_limiter.log(
                    "skip_error_frame",
                    f"{ts()} {color('[can]', '33')} SKIP error frame",
                )
            continue

        if msg.is_remote_frame:
            if verbose:
                verbose_limiter.log(
                    "skip_remote_frame",
                    f"{ts()} {color('[can]', '33')} SKIP remote frame id=0x{msg.arbitration_id:X}",
                )
            continue

        clients = client_manager.client_count()
        if clients == 0:
            continue

        frame_count += 1
        packet = pack_frame(msg)
        client_manager.broadcast(packet)

        if verbose:
            id_str = f"{msg.arbitration_id:08X}" if msg.is_extended_id else f"{msg.arbitration_id:03X}"
            data_hex = bytes(msg.data).hex().upper()
            print(
                f"{ts()} {color('[can]', '36')} "
                f"FWD #{frame_count:5d} ID={id_str} DLC={len(msg.data)} "
                f"DATA={data_hex} -> {clients} client(s)"
            )

    if bus is not None:
        try:
            bus.shutdown()
        except Exception:
            pass




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
        help="Print each CAN frame when it is forwarded to TCP clients. "
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

    client_manager = ClientManager()
    stop_event = threading.Event()

    def open_bus() -> Tuple[can.Bus, str]:
        return open_requested_bus(args.interface, args.channel, args.bitrate, args.auto)

    server_thread = threading.Thread(
        target=tcp_server_thread,
        args=(listen_port, client_manager, stop_event),
        daemon=True
    )
    server_thread.start()

    can_thread = threading.Thread(
        target=can_receiver_thread,
        args=(open_bus, client_manager, args.verbose, stop_event),
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
            "interface": args.interface if not args.auto else "auto",
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
        while not stop_event.is_set():
            if not can_thread.is_alive():
                stop_event.set()
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        print(f"\n{ts()} {color('[exit]', '33')} Shutting down...")
        stop_event.set()
    finally:
        stats = client_manager.stats
        print(f"{ts()} {color('[stats]', '34')} Frames sent: {stats['frames_sent']}, dropped: {stats['frames_dropped']}, bytes: {stats['bytes_sent']}")
        if zeroconf_service:
            zeroconf_service.stop()

        client_manager.close_all()
        can_thread.join(timeout=2.0)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
