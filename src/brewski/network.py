"""Address and hostname for the LCD networking screen.

The UDP lookup does not send a packet. It asks the kernel which address it
would use to leave the machine, which is the address another computer on
the LAN can use.
"""

from __future__ import annotations

import os
import socket
import time

from brewski.lcd import COLUMNS

_CACHE_SECONDS = 2.0
_cache: tuple[float, tuple[str, str]] | None = None


def display_identity() -> tuple[str, str]:
    """The address line and the hostname, ready for a 20-column row."""

    global _cache
    now = time.monotonic()
    if _cache is not None and now - _cache[0] < _CACHE_SECONDS:
        return _cache[1]
    found = format_identity(_primary_ipv4(), _hostname(), _listen_port())
    _cache = (now, found)
    return found


def clear_identity_cache() -> None:
    global _cache
    _cache = None


def format_identity(ip: str | None, hostname: str, port: int) -> tuple[str, str]:
    """Pair an IPv4 address with the port Brewski is listening on."""

    name = hostname.strip() or "—"
    if not ip:
        return "No address", name
    shown = f"{ip}:{port}"
    if len(shown) > COLUMNS:
        shown = ip
    return shown, name


def _hostname() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return ""


def _listen_port() -> int:
    raw = os.environ.get("BREWSKI_PORT", "8080")
    try:
        port = int(raw)
    except ValueError:
        return 8080
    if port < 1 or port > 65535:
        return 8080
    return port


def _primary_ipv4() -> str | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(0.2)
        sock.connect(("192.0.2.1", 9))
        address = sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()
    if not address or address.startswith("127."):
        return None
    return address
