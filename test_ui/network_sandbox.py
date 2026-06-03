"""Docker egress allowlist setup.

This is intentionally a container-start concern, not a crawler concern. The
app-level URL checks reject obvious SSRF targets; this module programs a
network-namespace firewall so unexpected browser/resource fetches cannot leave
the container unless their resolved IPs are allowlisted.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import yaml

_CHAIN = "AFR_EGRESS_ALLOWLIST"
_DEFAULT_SITE_FILES = (
    Path("/app/sites.yml"),
    Path("/app/test_ui/sites.yml"),
    Path("/data/.cache/sites.yml"),
)


def main() -> None:
    try:
        configure_from_env()
    except Exception as exc:
        print(f"egress allowlist setup failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def configure_from_env(env: dict[str, str] | None = None) -> None:
    """Configure iptables from AFR_* env vars, or no-op when disabled."""
    source_env = os.environ if env is None else env
    if not _truthy(source_env.get("AFR_EGRESS_ALLOWLIST_ENABLED")):
        return

    entries = collect_allowlist_entries(source_env)
    ipv4_networks, ipv6_networks = resolve_allowlist_entries(entries)
    nameservers_v4, nameservers_v6 = _read_resolver_nameservers()

    _apply_iptables(
        "iptables",
        target_networks=ipv4_networks,
        dns_networks=nameservers_v4,
    )
    _apply_iptables(
        "ip6tables",
        target_networks=ipv6_networks,
        dns_networks=nameservers_v6,
    )

    print(
        "egress allowlist enabled: "
        f"{len(ipv4_networks)} IPv4 and {len(ipv6_networks)} IPv6 target ranges"
    )


def collect_allowlist_entries(env: dict[str, str]) -> list[str]:
    """Collect explicit entries plus safe automatic service/site hosts."""
    entries: list[str] = []
    entries.extend(_split_entries(env.get("AFR_EGRESS_ALLOWLIST", "")))

    analyzer_url = env.get("AFR_AI_ANALYZER_SERVICE_URL")
    if analyzer_url:
        analyzer_host = _host_from_entry(analyzer_url)
        if analyzer_host:
            entries.append(analyzer_host)

    include_sites = _truthy(env.get("AFR_EGRESS_ALLOWLIST_INCLUDE_SITES", "true"))
    if include_sites:
        sites_file = Path(env["AFR_EGRESS_SITES_FILE"]) if env.get(
            "AFR_EGRESS_SITES_FILE"
        ) else _first_existing_site_file()
        if sites_file:
            entries.extend(_load_site_hosts(sites_file))

    return _dedupe(entries)


def resolve_allowlist_entries(
    entries: list[str],
) -> tuple[list[ipaddress.IPv4Network], list[ipaddress.IPv6Network]]:
    ipv4: list[ipaddress.IPv4Network] = []
    ipv6: list[ipaddress.IPv6Network] = []

    for raw_entry in entries:
        entry = raw_entry.strip()
        if not entry:
            continue
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            host = _host_from_entry(entry) or entry
            if "*" in host:
                raise ValueError(
                    f"wildcard allowlist entries are not supported: {entry}"
                )
            for ip in _resolve_host(host):
                network = ipaddress.ip_network(ip, strict=False)
                _append_network(ipv4, ipv6, network)
        else:
            _append_network(ipv4, ipv6, network)

    return sorted(set(ipv4), key=str), sorted(set(ipv6), key=str)


def _apply_iptables(
    binary: str,
    *,
    target_networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network],
    dns_networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> None:
    _run([binary, "-N", _CHAIN], check=False)
    _run([binary, "-F", _CHAIN])

    _run([binary, "-A", _CHAIN, "-o", "lo", "-j", "ACCEPT"])
    _run(
        [
            binary,
            "-A",
            _CHAIN,
            "-m",
            "conntrack",
            "--ctstate",
            "ESTABLISHED,RELATED",
            "-j",
            "ACCEPT",
        ]
    )
    for network in dns_networks:
        _run(
            [
                binary,
                "-A",
                _CHAIN,
                "-p",
                "udp",
                "-d",
                str(network),
                "--dport",
                "53",
                "-j",
                "ACCEPT",
            ]
        )
        _run(
            [
                binary,
                "-A",
                _CHAIN,
                "-p",
                "tcp",
                "-d",
                str(network),
                "--dport",
                "53",
                "-j",
                "ACCEPT",
            ]
        )
    for network in target_networks:
        _run([binary, "-A", _CHAIN, "-d", str(network), "-j", "ACCEPT"])
    _run([binary, "-A", _CHAIN, "-j", "REJECT"])

    if _run([binary, "-C", "OUTPUT", "-j", _CHAIN], check=False).returncode != 0:
        _run([binary, "-I", "OUTPUT", "1", "-j", _CHAIN])


def _run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _read_resolver_nameservers() -> tuple[
    list[ipaddress.IPv4Network], list[ipaddress.IPv6Network]
]:
    ipv4: list[ipaddress.IPv4Network] = []
    ipv6: list[ipaddress.IPv6Network] = []
    resolv_conf = Path("/etc/resolv.conf")
    if not resolv_conf.exists():
        return ipv4, ipv6

    for line in resolv_conf.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[0] != "nameserver":
            continue
        try:
            network = ipaddress.ip_network(parts[1], strict=False)
        except ValueError:
            continue
        _append_network(ipv4, ipv6, network)
    return ipv4, ipv6


def _append_network(
    ipv4: list[ipaddress.IPv4Network],
    ipv6: list[ipaddress.IPv6Network],
    network: ipaddress.IPv4Network | ipaddress.IPv6Network,
) -> None:
    if isinstance(network, ipaddress.IPv4Network):
        ipv4.append(network)
    else:
        ipv6.append(network)


def _split_entries(raw: str) -> list[str]:
    return [entry for chunk in raw.split(",") for entry in chunk.split()]


def _host_from_entry(entry: str) -> str | None:
    parsed = urlsplit(entry if "://" in entry else f"//{entry}")
    return parsed.hostname


def _resolve_host(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos})


def _first_existing_site_file() -> Path | None:
    return next((path for path in _DEFAULT_SITE_FILES if path.exists()), None)


def _load_site_hosts(path: Path) -> list[str]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    sites = payload.get("sites", [])
    if not isinstance(sites, list):
        return []

    hosts: list[str] = []
    for site in sites:
        if not isinstance(site, dict):
            continue
        url = site.get("url")
        if not isinstance(url, str):
            continue
        host = _host_from_entry(url)
        if host:
            hosts.append(host)
    return hosts


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = value.strip().lower()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def _truthy(value: str | None) -> bool:
    return value is not None and value.strip().lower() in {"1", "true", "yes", "on"}


if __name__ == "__main__":
    main()
