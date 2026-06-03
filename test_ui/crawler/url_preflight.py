"""Network-time URL safety checks for crawler fetches.

Static `sites.yml` validation intentionally avoids DNS/network I/O. The
crawler performs this second-stage preflight immediately before fetching so
redirects and DNS-resolved private targets are rejected at the boundary that
actually talks to the network.
"""

from __future__ import annotations

import asyncio
import socket
from ipaddress import ip_address
from urllib.parse import urljoin, urlsplit

import aiohttp

from .. import network_sandbox
from ..common.url_safety import UnsafeCrawlURL, validate_crawl_url

_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_REDIRECT_RETRYABLE_ERRORS = (
    aiohttp.ClientConnectorError,
    aiohttp.ServerConnectionError,
    asyncio.TimeoutError,
    OSError,
)


async def assert_url_allowed_for_crawl(
    url: str,
    *,
    allow_private: bool,
    check_dns: bool,
    check_redirects: bool,
    max_redirects: int,
) -> str:
    """Validate one URL at crawler time, including DNS and redirect targets."""
    current_url = validate_crawl_url(url, allow_private=allow_private)
    await _assert_dns_allowed(current_url, allow_private=allow_private, enabled=check_dns)
    if not check_redirects:
        return current_url

    timeout = aiohttp.ClientTimeout(total=10)
    for _ in range(max_redirects + 1):
        status, location = await _fetch_redirect_location_with_retry(
            current_url, timeout=timeout
        )
        if status not in _REDIRECT_STATUSES:
            return current_url
        if not location:
            raise UnsafeCrawlURL(f"redirect response missing Location: {current_url}")
        current_url = validate_crawl_url(
            urljoin(current_url, location), allow_private=allow_private
        )
        await _assert_dns_allowed(
            current_url, allow_private=allow_private, enabled=check_dns
        )

    raise UnsafeCrawlURL(f"redirect chain exceeded {max_redirects} hops: {url}")


async def _assert_dns_allowed(
    url: str, *, allow_private: bool, enabled: bool
) -> None:
    if allow_private or not enabled:
        return

    parsed = urlsplit(url)
    hostname = parsed.hostname
    if not hostname:
        raise UnsafeCrawlURL("site URL must include a hostname")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    for resolved_ip in await _resolve_host_ips(hostname, port):
        if _is_private_ip(resolved_ip):
            raise UnsafeCrawlURL(
                f"URL host resolves to a private or reserved address: {hostname}"
            )


async def _resolve_host_ips(hostname: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(
        hostname,
        port,
        type=socket.SOCK_STREAM,
    )
    return sorted({info[4][0] for info in infos})


def _is_private_ip(value: str) -> bool:
    ip = ip_address(value)
    return any(
        (
            ip.is_private,
            ip.is_loopback,
            ip.is_link_local,
            ip.is_multicast,
            ip.is_reserved,
            ip.is_unspecified,
        )
    )


async def _fetch_redirect_location(
    session: aiohttp.ClientSession, url: str
) -> tuple[int, str | None]:
    async with session.get(
        url,
        allow_redirects=False,
        headers={"Range": "bytes=0-0"},
    ) as response:
        return response.status, response.headers.get("Location")


async def _fetch_redirect_location_with_retry(
    url: str, *, timeout: aiohttp.ClientTimeout
) -> tuple[int, str | None]:
    """Fetch one redirect hop, refreshing egress rules once on connection failure."""
    for attempt in range(2):
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                return await _fetch_redirect_location(session, url)
        except _REDIRECT_RETRYABLE_ERRORS:
            if attempt == 1:
                raise
            network_sandbox.configure_from_env()

    raise RuntimeError("unreachable redirect preflight retry state")


__all__ = ["assert_url_allowed_for_crawl"]
