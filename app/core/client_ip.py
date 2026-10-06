"""The client's IP address, for rate limits and the audit log.

X-Forwarded-For is attacker-controlled unless it was written by a proxy we run. So the header is
only read when the direct peer is in ``TRUSTED_PROXIES``, and then the right-most hop that is not
itself a trusted proxy is the client (everything to its left was supplied by the client).
"""

import ipaddress
from functools import lru_cache
from typing import Optional, Tuple

from fastapi import Request

from app.core.config import settings

IpNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


@lru_cache(maxsize=8)
def _networks(raw: Tuple[str, ...]) -> Tuple[IpNetwork, ...]:
    return tuple(ipaddress.ip_network(item, strict=False) for item in raw)


def _is_trusted(host: str, networks: Tuple[IpNetwork, ...]) -> bool:
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(addr in net for net in networks)


def client_ip(request: Optional[Request]) -> str:
    if request is None or not request.client:
        return "unknown"
    peer = request.client.host
    networks = _networks(tuple(settings.TRUSTED_PROXIES))
    if not networks or not _is_trusted(peer, networks):
        return peer
    forwarded = request.headers.get("x-forwarded-for")
    if not forwarded:
        return peer
    for hop in reversed([h.strip() for h in forwarded.split(",")]):
        try:
            ipaddress.ip_address(hop)
        except ValueError:
            return peer  # a hop we cannot parse: do not guess
        if not _is_trusted(hop, networks):
            return hop
    return peer
