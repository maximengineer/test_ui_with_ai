"""Unit tests for Docker egress allowlist setup."""

from __future__ import annotations

import ipaddress

from test_ui import network_sandbox


def test_configure_from_env_requires_explicit_enable(monkeypatch):
    calls: list[dict[str, str]] = []

    def fake_collect(env: dict[str, str]) -> list[str]:
        calls.append(env)
        return []

    monkeypatch.setattr(network_sandbox, "collect_allowlist_entries", fake_collect)
    monkeypatch.setenv("AFR_EGRESS_ALLOWLIST", "example.com")
    monkeypatch.setenv("AFR_EGRESS_ALLOWLIST_ENABLED", "false")

    network_sandbox.configure_from_env()

    assert calls == []


def test_collect_allowlist_entries_includes_explicit_analyzer_and_sites(tmp_path):
    sites_file = tmp_path / "sites.yml"
    sites_file.write_text(
        "sites:\n"
        "  - id: one\n"
        "    name: One\n"
        "    url: https://example.com/a\n"
        "  - id: two\n"
        "    name: Two\n"
        "    url: https://sub.example.org/b\n",
        encoding="utf-8",
    )

    entries = network_sandbox.collect_allowlist_entries(
        {
            "AFR_EGRESS_ALLOWLIST": "cdn.example.net, 203.0.113.10/32",
            "AFR_AI_ANALYZER_SERVICE_URL": "http://ai-analyzer:3000",
            "AFR_EGRESS_SITES_FILE": str(sites_file),
        }
    )

    assert entries == [
        "cdn.example.net",
        "203.0.113.10/32",
        "ai-analyzer",
        "example.com",
        "sub.example.org",
    ]


def test_resolve_allowlist_entries_splits_ipv4_and_ipv6(monkeypatch):
    def fake_resolve(host: str) -> list[str]:
        assert host == "example.com"
        return ["93.184.216.34", "2001:db8::1"]

    monkeypatch.setattr(network_sandbox, "_resolve_host", fake_resolve)

    ipv4, ipv6 = network_sandbox.resolve_allowlist_entries(
        ["example.com", "203.0.113.0/24", "2001:db8:1::/48"]
    )

    assert ipaddress.ip_network("93.184.216.34/32") in ipv4
    assert ipaddress.ip_network("203.0.113.0/24") in ipv4
    assert ipaddress.ip_network("2001:db8::1/128") in ipv6
    assert ipaddress.ip_network("2001:db8:1::/48") in ipv6


def test_apply_iptables_builds_rejecting_output_chain(monkeypatch):
    calls: list[list[str]] = []

    class Result:
        returncode = 1

    def fake_run(args: list[str], *, check: bool = True):
        calls.append(args)
        return Result()

    monkeypatch.setattr(network_sandbox, "_run", fake_run)

    network_sandbox._apply_iptables(
        "iptables",
        target_networks=[ipaddress.ip_network("93.184.216.34/32")],
        dns_networks=[ipaddress.ip_network("127.0.0.11/32")],
    )

    assert ["iptables", "-F", "AFR_EGRESS_ALLOWLIST"] in calls
    assert [
        "iptables",
        "-A",
        "AFR_EGRESS_ALLOWLIST",
        "-p",
        "udp",
        "-d",
        "127.0.0.11/32",
        "--dport",
        "53",
        "-j",
        "ACCEPT",
    ] in calls
    assert [
        "iptables",
        "-A",
        "AFR_EGRESS_ALLOWLIST",
        "-d",
        "93.184.216.34/32",
        "-j",
        "ACCEPT",
    ] in calls
    assert [
        "iptables",
        "-A",
        "AFR_EGRESS_ALLOWLIST",
        "-j",
        "REJECT",
    ] in calls
    assert ["iptables", "-I", "OUTPUT", "1", "-j", "AFR_EGRESS_ALLOWLIST"] in calls
