import ipaddress
import logging
import os
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network

from fastapi import Request

logger = logging.getLogger(__name__)


def _parse_trusted_cidrs(raw: str) -> tuple[IPv4Network | IPv6Network, ...]:
    trusted = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            trusted.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            logger.warning("Ignoring invalid TRUSTED_PROXY_CIDRS entry: %r", part)
    return tuple(trusted)


def get_client_ip(request: Request, trusted_cidrs: str | None = None) -> str:
    """Resolve the client IP, honoring X-Forwarded-For only when trust is enabled.

    With TRUSTED_PROXY_CIDRS unset/empty (the default) X-Forwarded-For is ignored and
    the direct TCP peer (request.client.host) is returned, preserving the historical
    behavior. When TRUSTED_PROXY_CIDRS is a comma-separated list of proxy CIDRs, the
    X-Forwarded-For chain is walked right-to-left: hops inside a trusted CIDR are proxy
    hops and are skipped, and the first untrusted hop is returned as the real client.
    Every listed proxy MUST overwrite X-Forwarded-For from the untrusted client, or
    the header cannot be trusted.
    """
    peer = request.client.host if request.client else None

    if trusted_cidrs is None:
        trusted_cidrs = os.getenv("TRUSTED_PROXY_CIDRS", "")
    if not trusted_cidrs:
        return peer or "unknown"

    trusted = _parse_trusted_cidrs(trusted_cidrs)

    xff = request.headers.get("x-forwarded-for", "")
    if not xff:
        return peer or "unknown"

    hops: list[IPv4Address | IPv6Address] = []
    for token in xff.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            hops.append(ipaddress.ip_address(token))
        except ValueError:
            continue

    for hop in reversed(hops):
        if any(hop in net for net in trusted):
            continue
        return str(hop)

    return peer or "unknown"
