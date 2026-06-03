"""URL safety checks for crawler targets.

The dashboard lets an operator add URLs that are later fetched by crawler
subprocesses. Keep the default posture conservative: public HTTP(S) targets
only, with an explicit settings override for intentional internal-site tests.
"""

from __future__ import annotations

from ipaddress import ip_address
from urllib.parse import unquote, urlsplit


class UnsafeCrawlURL(ValueError):
    """URL is not safe to crawl with the default SSRF posture."""


_LOCAL_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
}


def validate_crawl_url(url: str, *, allow_private: bool = False) -> str:
    """Validate a crawl target URL and return it unchanged.

    Blocks non-HTTP schemes and common SSRF targets. This intentionally avoids
    DNS resolution: validation must be deterministic and must not perform
    network I/O during dashboard writes or sites.yml loads.
    """
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise UnsafeCrawlURL("site URL must use http or https")

    if parsed.username or parsed.password:
        raise UnsafeCrawlURL("site URL must not include username or password")

    hostname = parsed.hostname
    if not hostname:
        raise UnsafeCrawlURL("site URL must include a hostname")

    host = unquote(hostname).rstrip(".").lower()
    if not host:
        raise UnsafeCrawlURL("site URL must include a hostname")

    if not allow_private and _is_private_hostname(host):
        raise UnsafeCrawlURL(f"site URL host is not allowed by default: {hostname}")

    return url


def _is_private_hostname(host: str) -> bool:
    if host in _LOCAL_HOSTNAMES or host.endswith(".localhost"):
        return True

    try:
        ip = ip_address(host)
    except ValueError:
        # Obfuscated IPv4 forms like 2130706433, 0177.0.0.1, 0x7f.0.0.1,
        # or percent-encoded host labels are ambiguous across clients. Treat
        # them as unsafe unless the operator explicitly opts into private targets.
        if _looks_like_obfuscated_ip(host):
            return True
        return False

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


def _looks_like_obfuscated_ip(host: str) -> bool:
    if "%" in host:
        return True
    if host.isdigit() or host.startswith("0x"):
        return True
    labels = host.split(".")
    if len(labels) == 4 and all(label for label in labels):
        if all(label.isdigit() for label in labels):
            return True
        if any(label.startswith("0x") for label in labels):
            return True
    return False


__all__ = ["UnsafeCrawlURL", "validate_crawl_url"]
