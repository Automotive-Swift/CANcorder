#!/usr/bin/env python3
"""
Common utilities and classes for ECUconnect CAN Logger Simulators.

This module contains shared code between canlogger.py and rusoku_canlogger.py.
"""

import socket
import struct
import threading
import time
from datetime import datetime
from typing import Any, Dict, Tuple, Optional

# Protocol constants
HEADER_SIZE = 8 + 4 + 1 + 1  # 14 bytes
AUTO_PORT_START = 42420


def ts() -> str:
    """Get current timestamp string."""
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def color(text: str, code: str) -> str:
    """Add ANSI color codes to text."""
    return f"\033[{code}m{text}\033[0m"


def find_available_port(start_port: int = AUTO_PORT_START) -> int:
    """Find an available TCP port >= start_port."""
    for port in range(start_port, 65536):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", port))
            return port
        except OSError:
            continue
        finally:
            sock.close()
    raise RuntimeError("No free TCP port found in the requested range.")


def zeroconf_log(message: str):
    """Log zeroconf-related messages."""
    print(f"{ts()} {color('[zeroconf]', '35')} {message}")


def pack_frame(timestamp_us: int, can_id: int, is_extended: bool, data: bytes) -> bytes:
    """
    Pack a CAN message into ECUconnect Logger binary format.
    
    Format: [timestamp:8][id:4][ext:1][dlc:1][data:0-64]
    """
    ext = 1 if is_extended else 0
    dlc = len(data)
    return struct.pack(">QIBB", timestamp_us, can_id, ext, dlc) + data


class ClientManager:
    """Manages connected TCP clients and broadcasts frames to all of them."""

    def __init__(self, logger: Optional[Any] = None):
        self.clients: Dict[socket.socket, Tuple[str, int]] = {}
        self.lock = threading.Lock()
        self.stats = {
            "frames_sent": 0,
            "frames_dropped": 0,
            "bytes_sent": 0,
        }
        self._logger = logger

    def _log(self, message: str) -> None:
        if self._logger is None:
            print(message)
        else:
            self._logger(message)

    def add_client(self, sock: socket.socket, addr: Tuple[str, int]):
        with self.lock:
            self.clients[sock] = addr
            self._log(f"{ts()} {color('[server]', '34')} Client connected: {addr[0]}:{addr[1]} (total: {len(self.clients)})")

    def remove_client(self, sock: socket.socket):
        with self.lock:
            if sock in self.clients:
                addr = self.clients.pop(sock)
                self._log(f"{ts()} {color('[server]', '33')} Client disconnected: {addr[0]}:{addr[1]} (total: {len(self.clients)})")
                try:
                    sock.close()
                except Exception:
                    pass

    def broadcast(self, data: bytes):
        """Send data to all connected clients."""
        with self.lock:
            dead_clients = []
            for sock, addr in self.clients.items():
                try:
                    sock.sendall(data)
                    self.stats["frames_sent"] += 1
                    self.stats["bytes_sent"] += len(data)
                except (OSError, BrokenPipeError):
                    dead_clients.append(sock)
                    self.stats["frames_dropped"] += 1

        for sock in dead_clients:
            self.remove_client(sock)

    def client_count(self) -> int:
        with self.lock:
            return len(self.clients)

    def close_all(self):
        """Close and remove all client sockets."""
        with self.lock:
            clients = list(self.clients.keys())
        for sock in clients:
            self.remove_client(sock)


def client_handler_thread(sock: socket.socket, addr: Tuple[str, int], client_manager: ClientManager):
    """Handle a single client connection (just keep it alive)."""
    client_manager.add_client(sock, addr)

    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        while True:
            data = sock.recv(1024)
            if not data:
                break
    except (OSError, ConnectionResetError):
        pass
    finally:
        client_manager.remove_client(sock)


def tcp_server_thread(port: int, client_manager: ClientManager, stop_event: Optional[threading.Event] = None):
    """TCP server that accepts client connections."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    
    if stop_event:
        server.settimeout(1.0)  # Allow periodic checks of stop_event
    
    server.bind(("0.0.0.0", port))
    server.listen(5)

    print(f"{ts()} {color('[server]', '32')} TCP server listening on port {port}")

    while True:
        try:
            if stop_event and stop_event.is_set():
                break
                
            client_sock, addr = server.accept()
            t = threading.Thread(
                target=client_handler_thread,
                args=(client_sock, addr, client_manager),
                daemon=True
            )
            t.start()
        except socket.timeout:
            if stop_event and stop_event.is_set():
                break
            continue
        except Exception as e:
            if not (stop_event and stop_event.is_set()):
                print(f"{ts()} {color('[server]', '31')} Accept error: {e}")

    server.close()


# Common help text sections
BINARY_PROTOCOL_HELP = """
BINARY PROTOCOL
===============

Each CAN frame is transmitted as a binary packet with the following format:

  ┌─────────────┬─────────────┬─────────┬─────────┬──────────────────┐
  │ timestamp   │ can_id      │ ext     │ dlc     │ data             │
  │ 8 bytes     │ 4 bytes     │ 1 byte  │ 1 byte  │ 0-64 bytes       │
  └─────────────┴─────────────┴─────────┴─────────┴──────────────────┘

  timestamp : uint64_t, big-endian, microseconds since Unix epoch
  can_id    : uint32_t, big-endian, CAN arbitration ID
  ext       : uint8_t, 0 = standard 11-bit ID, 1 = extended 29-bit ID
  dlc       : uint8_t, data length (0-8 for CAN, 0-64 for CAN-FD)
  data      : raw CAN payload bytes

  Total packet size: 14 + dlc bytes (14-22 bytes for classic CAN)
"""

TESTING_CONNECTION_HELP = """
TESTING THE CONNECTION
======================

Use the included logger_client.py to test:
  python3 logger_client.py localhost

Or with netcat:
  nc localhost 2518 | xxd

Or with the CANyonero app by connecting to this machine's IP on port 2518.
"""

SEE_ALSO_HELP = """
SEE ALSO
========

  logger_client.py  - Test client for receiving streamed CAN frames
  canvoy.py         - CAN bridge for remote ISOTP forwarding
"""
