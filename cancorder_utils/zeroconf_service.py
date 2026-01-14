"""
Zeroconf advertisement helper shared by CANcorder logger proxies.

Keeps the Zeroconf dependency optional while providing a thin wrapper that
handles default metadata, IP resolution, and clean shutdown.
"""

from __future__ import annotations

import socket
from typing import Callable, Dict, Optional

try:
    from zeroconf import ServiceInfo, Zeroconf
except ImportError:  # pragma: no cover - optional dependency
    ServiceInfo = None
    Zeroconf = None

SERVICE_TYPE = "_ecuconnect-log._tcp.local."
SERVICE_NAME_PREFIX = "ECUconnect-Logger"

LogFunc = Callable[[str], None]


def _get_local_ip() -> str:
    """Best-effort detection of the primary IPv4 address."""
    try:
        test_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            test_sock.connect(("8.8.8.8", 80))
            return test_sock.getsockname()[0]
        finally:
            test_sock.close()
    except OSError:
        return "127.0.0.1"


class LoggerZeroconfService:
    """Publish logger availability via Zeroconf/mDNS."""

    def __init__(
        self,
        port: int,
        service_name: Optional[str] = None,
        properties: Optional[Dict[str, str]] = None,
        logger: Optional[LogFunc] = None,
    ):
        self.port = port
        self.service_name = service_name or f"{SERVICE_NAME_PREFIX} {socket.gethostname()}"
        self.properties = properties or {}
        self.logger = logger

        self._zeroconf: Optional[Zeroconf] = None
        self._service_info: Optional[ServiceInfo] = None

    @staticmethod
    def is_available() -> bool:
        return Zeroconf is not None and ServiceInfo is not None

    def start(self) -> bool:
        """Register the service if the zeroconf dependency exists."""
        if not self.is_available():
            self._log("Skipping Zeroconf advertisement (install 'zeroconf' to enable).")
            return False

        try:
            hostname = socket.gethostname().rstrip(".") or "localhost"
            ip_addr = _get_local_ip()
            addresses = [socket.inet_aton(ip_addr)]
        except OSError as exc:
            self._log(f"Failed to determine host IP for Zeroconf: {exc}")
            return False

        service_full_name = f"{self._sanitize_name(self.service_name)}.{SERVICE_TYPE}"
        props = {k: str(v).encode("utf-8") for k, v in self.properties.items()}

        try:
            self._service_info = ServiceInfo(
                type_=SERVICE_TYPE,
                name=service_full_name,
                port=self.port,
                addresses=addresses,
                server=f"{hostname}.local.",
                properties=props,
            )
            self._zeroconf = Zeroconf()
            self._zeroconf.register_service(self._service_info)
            self._log(f"Zeroconf service '{self.service_name}' published on {ip_addr}:{self.port}")
            return True
        except Exception as exc:  # pragma: no cover - network stack errors
            self._log(f"Failed to publish Zeroconf service: {exc}")
            self._cleanup()
            return False

    def stop(self):
        """Unregister the service if it was published."""
        if self._zeroconf and self._service_info:
            try:
                self._zeroconf.unregister_service(self._service_info)
                self._log(f"Zeroconf service '{self.service_name}' removed.")
            except Exception as exc:  # pragma: no cover - best-effort cleanup
                self._log(f"Error while unregistering Zeroconf service: {exc}")
            finally:
                self._cleanup()
        else:
            self._cleanup()

    def _cleanup(self):
        if self._zeroconf:
            try:
                self._zeroconf.close()
            except Exception:
                pass
        self._zeroconf = None
        self._service_info = None

    def _log(self, message: str):
        if self.logger:
            self.logger(message)

    @staticmethod
    def _sanitize_name(name: str) -> str:
        return name.strip().strip(".")
