#!/usr/bin/env python3
"""
ECUconnect CAN Logger Simulator (RusokuCAN/TouCAN version)

A stand-in for the ECUconnect CAN Logger hardware.
Uses the RusokuCAN/MacCAN-TouCAN library with Rusoku TouCAN USB adapters
and streams CAN frames via TCP in the ECUconnect Logger binary format.

Packet format: [timestamp:8][id:4][ext:1][dlc:1][data:0-64]
- timestamp: uint64_t, big-endian, microseconds since epoch
- id: uint32_t, big-endian
- ext: uint8_t (0=standard, 1=extended)
- dlc: uint8_t
- data: 0-64 bytes

Default TCP port: 2518
"""

import argparse
import socket
import sys
import threading
import time
from ctypes import *
from pathlib import Path
from typing import Optional
import platform
import signal

from zeroconf_service import LoggerZeroconfService, SERVICE_NAME_PREFIX
from canlogger_common import (
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


HELP_DESCRIPTION = """\
ECUconnect CAN Logger Simulator (RusokuCAN Edition)
===================================================

A software stand-in for the ECUconnect CAN Logger hardware. This tool reads
CAN frames from a Rusoku TouCAN USB adapter using the MacCAN-TouCAN library
and streams them via TCP using the same binary protocol as the real ECUconnect
Logger device.

Use this to test applications that expect to connect to an ECUconnect Logger
without needing the actual hardware.
"""

HELP_EPILOG = BINARY_PROTOCOL_HELP + """

SUPPORTED CAN HARDWARE
======================

Rusoku TouCAN USB (recommended for macOS)
  USB CAN adapters from Rusoku Technologies using the MacCAN-TouCAN driver.
  Supported devices:
    • TouCAN USB (Model F4FS1)

  Usage:    --channel 0
  macOS:    Requires libUVCANTOU.dylib installed in /usr/local/lib


PREDEFINED BIT-RATES
====================

Use --bitrate-index with these values:
   0  = 1000 kbit/s
  -1  =  800 kbit/s
  -2  =  500 kbit/s (default)
  -3  =  250 kbit/s
  -4  =  125 kbit/s
  -5  =  100 kbit/s
  -6  =   50 kbit/s
  -7  =   20 kbit/s
  -8  =   10 kbit/s


EXAMPLES
========

Basic usage:
  python3 %(prog)s

With verbose output (shows each CAN frame):
  python3 %(prog)s -v

Custom port and bitrate (250 kbit/s):
  python3 %(prog)s --port 3000 --bitrate-index -3

Different CAN channel:
  python3 %(prog)s --channel 1


""" + TESTING_CONNECTION_HELP + """

INSTALLATION
============

The MacCAN-TouCAN library must be installed:

  1. Get the library from: https://github.com/mac-can/RusokuCAN.dylib
  2. Build and install:
     cd RusokuCAN.dylib
     ./build_no.sh
     make all
     sudo make install

  This installs libUVCANTOU.dylib to /usr/local/lib


TROUBLESHOOTING
===============

"Library not found":
  → Ensure libUVCANTOU.dylib is installed in /usr/local/lib
  → Or specify path with --library option

"No CAN device found" or init failed:
  → Check USB connection: system_profiler SPUSBDataType | grep -i toucan
  → Verify TouCAN adapter is connected

"Busoff" or communication errors:
  → Check CAN bus wiring and termination
  → Verify bitrate matches the CAN bus

High CPU usage:
  → Normal when receiving high-volume CAN traffic
  → Use without -v flag to reduce console output overhead


""" + SEE_ALSO_HELP

# CAN API V3 constants
CANMODE_DEFAULT = c_uint8(0x00)
CANERR_NOERROR = 0
CANERR_RX_EMPTY = -30
CANREAD_INFINITE = 65535

# Predefined bit-rate indexes (CiA)
CANBTR_INDEX_1M = c_int32(0)
CANBTR_INDEX_800K = c_int32(-1)
CANBTR_INDEX_500K = c_int32(-2)
CANBTR_INDEX_250K = c_int32(-3)
CANBTR_INDEX_125K = c_int32(-4)
CANBTR_INDEX_100K = c_int32(-5)
CANBTR_INDEX_50K = c_int32(-6)
CANBTR_INDEX_20K = c_int32(-7)
CANBTR_INDEX_10K = c_int32(-8)


class OpModeBits(LittleEndianStructure):
    _fields_ = [
        ('mon', c_uint8, 1),
        ('err', c_uint8, 1),
        ('nrtr', c_uint8, 1),
        ('nxtd', c_uint8, 1),
        ('shrd', c_uint8, 1),
        ('niso', c_uint8, 1),
        ('brse', c_uint8, 1),
        ('fdoe', c_uint8, 1)
    ]


class OpMode(Union):
    _fields_ = [
        ('byte', c_uint8),
        ('bits', OpModeBits)
    ]


class BitrateNominal(LittleEndianStructure):
    _fields_ = [
        ('brp', c_uint16),
        ('tseg1', c_uint16),
        ('tseg2', c_uint16),
        ('sjw', c_uint16),
        ('sam', c_uint8)
    ]


class BitrateData(LittleEndianStructure):
    _fields_ = [
        ('brp', c_uint16),
        ('tseg1', c_uint16),
        ('tseg2', c_uint16),
        ('sjw', c_uint16)
    ]


class BitrateRegister(LittleEndianStructure):
    _fields_ = [
        ('frequency', c_int32),
        ('nominal', BitrateNominal),
        ('data', BitrateData)
    ]


class Bitrate(Union):
    _fields_ = [
        ('index', c_int32),
        ('btr', BitrateRegister)
    ]


class Timestamp(LittleEndianStructure):
    _fields_ = [
        ('sec', c_long),
        ('nsec', c_long)
    ]


class MessageFlags(LittleEndianStructure):
    _fields_ = [
        ('xtd', c_uint8, 1),
        ('rtr', c_uint8, 1),
        ('fdf', c_uint8, 1),
        ('brs', c_uint8, 1),
        ('esi', c_uint8, 1),
        ('_unused', c_uint8, 2),
        ('sts', c_uint8, 1)
    ]


class Message(LittleEndianStructure):
    _fields_ = [
        ('id', c_uint32),
        ('flags', MessageFlags),
        ('dlc', c_uint8),
        ('data', c_uint8 * 64),
        ('timestamp', Timestamp)
    ]


class TouCANInterface:
    """Wrapper for the RusokuCAN/MacCAN-TouCAN library."""

    def __init__(self, library_path: str = "libUVCANTOU.dylib"):
        try:
            if platform.system() == 'Darwin':
                from ctypes.util import find_library
                lib_path = find_library(library_path.replace('.dylib', '').replace('lib', ''))
                if lib_path:
                    self._lib = cdll.LoadLibrary(lib_path)
                else:
                    self._lib = cdll.LoadLibrary(library_path)
            else:
                self._lib = cdll.LoadLibrary(library_path)
        except OSError as e:
            raise RuntimeError(
                f"Failed to load RusokuCAN library: {e}\n"
                "Make sure libUVCANTOU.dylib is installed in /usr/local/lib\n"
                "See: https://github.com/mac-can/RusokuCAN.dylib"
            )
        self._handle = -1

    def init(self, channel: int, mode: OpMode) -> int:
        result = self._lib.can_init(c_int32(channel), c_uint8(mode.byte), None)
        if result >= 0:
            self._handle = result
            return 0
        self._handle = -1
        return int(result)

    def exit(self) -> int:
        if self._handle >= 0:
            result = self._lib.can_exit(self._handle)
            self._handle = -1
            return int(result)
        return 0

    def start(self, bitrate: Bitrate) -> int:
        return int(self._lib.can_start(self._handle, byref(bitrate)))

    def reset(self) -> int:
        return int(self._lib.can_reset(self._handle))

    def read(self, timeout: int = CANREAD_INFINITE) -> tuple[int, Optional[Message]]:
        msg = Message()
        result = self._lib.can_read(self._handle, byref(msg), c_uint16(timeout))
        if result == 0:
            return 0, msg
        return int(result), None

    def kill(self) -> int:
        return int(self._lib.can_kill(self._handle))

    def hardware(self) -> str:
        self._lib.can_hardware.restype = c_char_p
        result = self._lib.can_hardware(self._handle)
        return result.decode('utf-8') if result else "unknown"

    def firmware(self) -> str:
        self._lib.can_firmware.restype = c_char_p
        result = self._lib.can_firmware(self._handle)
        return result.decode('utf-8') if result else "unknown"

    def version(self) -> str:
        self._lib.can_version.restype = c_char_p
        result = self._lib.can_version()
        return result.decode('utf-8') if result else "unknown"




def pack_frame(msg: Message) -> bytes:
    """Pack a CAN message into ECUconnect Logger binary format."""
    ts_us = int(time.time() * 1_000_000)
    return _pack_frame(ts_us, msg.id, bool(msg.flags.xtd), bytes(msg.data[:msg.dlc]))




def can_receiver_thread(can: TouCANInterface, client_manager: ClientManager, verbose: bool, stop_event: threading.Event):
    """Thread that receives CAN frames and broadcasts to clients."""
    frame_count = 0

    print(f"{ts()} {color('[can]', '32')} CAN receiver started")

    while not stop_event.is_set():
        result, msg = can.read(timeout=100)  # 100ms timeout for responsiveness

        if result == CANERR_NOERROR and msg is not None:
            if msg.flags.rtr:
                if verbose:
                    print(f"{ts()} {color('[can]', '33')} SKIP remote frame id=0x{msg.id:X}")
                continue

            if msg.flags.sts:
                if verbose:
                    print(f"{ts()} {color('[can]', '33')} SKIP status frame")
                continue

            frame_count += 1
            packet = pack_frame(msg)

            if client_manager.client_count() > 0:
                client_manager.broadcast(packet)

            if verbose:
                id_str = f"{msg.id:08X}" if msg.flags.xtd else f"{msg.id:03X}"
                data_hex = bytes(msg.data[:msg.dlc]).hex()
                clients = client_manager.client_count()
                print(f"{ts()} {color('[can]', '36')} #{frame_count:5d} ID={id_str} DLC={msg.dlc} DATA={data_hex} -> {clients} client(s)")

        elif result != CANERR_RX_EMPTY and result != -50:  # -50 is timeout
            if verbose:
                print(f"{ts()} {color('[can]', '31')} Read error: {result}")




def bitrate_index_to_name(index: int) -> str:
    """Convert bitrate index to human-readable name."""
    names = {
        0: "1000 kbit/s",
        -1: "800 kbit/s",
        -2: "500 kbit/s",
        -3: "250 kbit/s",
        -4: "125 kbit/s",
        -5: "100 kbit/s",
        -6: "50 kbit/s",
        -7: "20 kbit/s",
        -8: "10 kbit/s",
    }
    return names.get(index, f"index {index}")


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
        "--bitrate-index", "-b",
        type=int,
        default=-2,
        metavar="INDEX",
        help="CAN bitrate index (default: -2 = 500 kbit/s). "
             "See --help for list of predefined bitrates."
    )
    parser.add_argument(
        "--channel", "-c",
        type=int,
        default=0,
        metavar="CHAN",
        help="TouCAN channel number (default: 0)"
    )
    parser.add_argument(
        "--library", "-l",
        type=str,
        default="libUVCANTOU.dylib",
        metavar="PATH",
        help="Path to RusokuCAN library (default: libUVCANTOU.dylib)"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print each received CAN frame to stdout. "
             "Useful for debugging but may impact performance at high bus loads."
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

    port_label = f"{listen_port}" if requested_port is not None else f"{listen_port} (auto)"

    print(f"{ts()} {color('[init]', '34')} ECUconnect Logger Simulator (RusokuCAN) starting...")
    print(f"{ts()} {color('[init]', '34')} TCP port: {port_label}, CAN bitrate: {bitrate_index_to_name(args.bitrate_index)}")

    try:
        can = TouCANInterface(args.library)
        print(f"{ts()} {color('[can]', '32')} Library loaded: {can.version()}")
    except RuntimeError as e:
        print(f"{ts()} {color('[error]', '31')} {e}")
        return 1

    op_mode = OpMode()
    op_mode.byte = CANMODE_DEFAULT.value

    print(f"{ts()} {color('[can]', '34')} Initializing TouCAN channel {args.channel}...")
    result = can.init(args.channel, op_mode)
    if result < CANERR_NOERROR:
        print(f"{ts()} {color('[error]', '31')} Failed to initialize CAN interface: error {result}")
        print()
        print("Make sure you have a TouCAN USB adapter connected:")
        print("  - TouCAN USB (Model F4FS1)")
        print()
        print("Check USB devices:")
        print("  system_profiler SPUSBDataType | grep -i toucan")
        return 1

    print(f"{ts()} {color('[can]', '32')} Hardware: {can.hardware()}")
    print(f"{ts()} {color('[can]', '32')} Firmware: {can.firmware()}")

    bitrate = Bitrate()
    bitrate.index = args.bitrate_index

    print(f"{ts()} {color('[can]', '34')} Starting CAN controller at {bitrate_index_to_name(args.bitrate_index)}...")
    result = can.start(bitrate)
    if result < CANERR_NOERROR:
        print(f"{ts()} {color('[error]', '31')} Failed to start CAN controller: error {result}")
        can.exit()
        return 1

    client_manager = ClientManager()
    stop_event = threading.Event()

    def signal_handler(signo, frame):
        print(f"\n{ts()} {color('[exit]', '33')} Received signal, shutting down...")
        stop_event.set()

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    server_thread = threading.Thread(
        target=tcp_server_thread,
        args=(listen_port, client_manager, stop_event),
        daemon=True
    )
    server_thread.start()

    can_thread = threading.Thread(
        target=can_receiver_thread,
        args=(can, client_manager, args.verbose, stop_event),
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
            "interface": "rusokucan",
            "channel": str(args.channel),
            "bitrate_index": str(args.bitrate_index),
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
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass

    print(f"{ts()} {color('[exit]', '33')} Shutting down...")
    stop_event.set()

    stats = client_manager.stats
    print(f"{ts()} {color('[stats]', '34')} Frames sent: {stats['frames_sent']}, dropped: {stats['frames_dropped']}, bytes: {stats['bytes_sent']}")

    if zeroconf_service:
        zeroconf_service.stop()

    can.reset()
    can.exit()
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
