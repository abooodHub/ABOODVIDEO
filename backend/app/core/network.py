"""Network and Client IP Resolution Helpers with Trusted Proxy Verification."""
import ipaddress
import logging
from typing import List, Optional, Union
from fastapi import Request
from app.core.config import settings

logger = logging.getLogger(__name__)

_cached_trusted_networks: Optional[List[Union[ipaddress.IPv4Network, ipaddress.IPv6Network]]] = None
_cached_trusted_proxies_cfg: Optional[List[str]] = None


def get_trusted_networks() -> List[Union[ipaddress.IPv4Network, ipaddress.IPv6Network]]:
    """Return parsed IPv4/IPv6 networks configured in settings.TRUSTED_PROXIES."""
    global _cached_trusted_networks, _cached_trusted_proxies_cfg
    raw_list = settings.TRUSTED_PROXIES
    if isinstance(raw_list, str):
        raw_list = [i.strip() for i in raw_list.split(",") if i.strip()]

    if _cached_trusted_networks is not None and _cached_trusted_proxies_cfg == raw_list:
        return _cached_trusted_networks

    networks: List[Union[ipaddress.IPv4Network, ipaddress.IPv6Network]] = []
    for item in raw_list:
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            logger.warning(f"Invalid trusted proxy CIDR or IP configured: {item}")

    _cached_trusted_proxies_cfg = list(raw_list)
    _cached_trusted_networks = networks
    return networks


def is_trusted_proxy(ip_str: Optional[str]) -> bool:
    """Check if an IP string belongs to any configured trusted proxy networks."""
    if not ip_str or ip_str == "unknown":
        return False
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False

    for network in get_trusted_networks():
        if ip in network:
            return True
    return False


def get_real_client_ip(request: Request) -> str:
    """
    Extract authentic client IP address behind trusted reverse proxies (e.g. Nginx).

    Security Guarantee:
    - If direct peer (request.client.host) is NOT a trusted proxy, forwarding headers
      (X-Real-IP, X-Forwarded-For) are STRICTLY IGNORED to prevent IP spoofing.
    - If direct peer IS a trusted proxy, X-Real-IP is preferred (Nginx $remote_addr).
    - If X-Forwarded-For is evaluated, it is traversed from right-to-left to locate the
      first untrusted external client IP.
    """
    if not request.client or not request.client.host:
        return "unknown"

    peer_ip = request.client.host

    # Direct connection is not from a trusted proxy: untrusted direct client
    if not is_trusted_proxy(peer_ip):
        return peer_ip

    # Peer is trusted: inspect X-Real-IP first
    x_real_ip = request.headers.get("X-Real-IP")
    if x_real_ip:
        candidate = x_real_ip.strip()
        try:
            ipaddress.ip_address(candidate)
            return candidate
        except ValueError:
            pass

    # Inspect X-Forwarded-For: traverse right-to-left
    x_forwarded_for = request.headers.get("X-Forwarded-For")
    if x_forwarded_for:
        raw_ips = [ip.strip() for ip in x_forwarded_for.split(",") if ip.strip()]
        for candidate in reversed(raw_ips):
            try:
                ip_obj = ipaddress.ip_address(candidate)
                if not is_trusted_proxy(str(ip_obj)):
                    return str(ip_obj)
            except ValueError:
                continue
        if raw_ips:
            try:
                ipaddress.ip_address(raw_ips[0])
                return raw_ips[0]
            except ValueError:
                pass

    return peer_ip
