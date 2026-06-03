"""Crawler-time URL preflight tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from test_ui.common.url_safety import UnsafeCrawlURL
from test_ui.crawler import engine as crawler_engine
from test_ui.crawler import url_preflight


@pytest.mark.asyncio
async def test_preflight_blocks_dns_resolved_private_address(monkeypatch):
    async def fake_resolve(hostname: str, port: int) -> list[str]:
        assert hostname == "public.example"
        assert port == 443
        return ["10.0.0.10"]

    monkeypatch.setattr(url_preflight, "_resolve_host_ips", fake_resolve)

    with pytest.raises(UnsafeCrawlURL, match="resolves to a private"):
        await url_preflight.assert_url_allowed_for_crawl(
            "https://public.example",
            allow_private=False,
            check_dns=True,
            check_redirects=False,
            max_redirects=5,
        )


@pytest.mark.asyncio
async def test_preflight_blocks_redirect_to_private_resolved_target(monkeypatch):
    async def fake_resolve(hostname: str, port: int) -> list[str]:
        return {
            "public.example": ["93.184.216.34"],
            "internal.example": ["192.168.1.20"],
        }[hostname]

    async def fake_fetch_redirect_location(session, url: str) -> tuple[int, str | None]:
        if url == "https://public.example":
            return 302, "https://internal.example/admin"
        return 200, None

    monkeypatch.setattr(url_preflight, "_resolve_host_ips", fake_resolve)
    monkeypatch.setattr(
        url_preflight, "_fetch_redirect_location", fake_fetch_redirect_location
    )

    with pytest.raises(UnsafeCrawlURL, match="resolves to a private"):
        await url_preflight.assert_url_allowed_for_crawl(
            "https://public.example",
            allow_private=False,
            check_dns=True,
            check_redirects=True,
            max_redirects=5,
        )


@pytest.mark.asyncio
async def test_preflight_returns_final_public_redirect_target(monkeypatch):
    async def fake_resolve(hostname: str, port: int) -> list[str]:
        return ["93.184.216.34"]

    async def fake_fetch_redirect_location(session, url: str) -> tuple[int, str | None]:
        if url == "https://public.example":
            return 302, "/next"
        return 200, None

    monkeypatch.setattr(url_preflight, "_resolve_host_ips", fake_resolve)
    monkeypatch.setattr(
        url_preflight, "_fetch_redirect_location", fake_fetch_redirect_location
    )

    final_url = await url_preflight.assert_url_allowed_for_crawl(
        "https://public.example",
        allow_private=False,
        check_dns=True,
        check_redirects=True,
        max_redirects=5,
    )

    assert final_url == "https://public.example/next"


@pytest.mark.asyncio
async def test_preflight_refreshes_egress_allowlist_and_retries_connection_failure(
    monkeypatch,
):
    calls: list[str] = []

    async def fake_resolve(hostname: str, port: int) -> list[str]:
        return ["93.184.216.34"]

    async def fake_fetch_redirect_location(session, url: str) -> tuple[int, str | None]:
        calls.append(f"fetch:{url}")
        if calls.count(f"fetch:{url}") == 1:
            raise OSError("firewall blocked stale CDN IP")
        return 200, None

    def fake_refresh() -> None:
        calls.append("refresh")

    monkeypatch.setattr(url_preflight, "_resolve_host_ips", fake_resolve)
    monkeypatch.setattr(
        url_preflight, "_fetch_redirect_location", fake_fetch_redirect_location
    )
    monkeypatch.setattr(
        url_preflight.network_sandbox, "configure_from_env", fake_refresh
    )

    final_url = await url_preflight.assert_url_allowed_for_crawl(
        "https://public.example",
        allow_private=False,
        check_dns=True,
        check_redirects=True,
        max_redirects=5,
    )

    assert final_url == "https://public.example"
    assert calls == [
        "fetch:https://public.example",
        "refresh",
        "fetch:https://public.example",
    ]


@pytest.mark.asyncio
async def test_crawler_refreshes_egress_allowlist_before_site_fetch(
    tmp_path, monkeypatch
):
    calls: list[str] = []

    def fake_refresh() -> None:
        calls.append("refresh")

    class FakeCrawler:
        def __init__(self, *, config):
            self.config = config

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def arun(self, url: str, *, config):
            calls.append(f"fetch:{url}")
            return SimpleNamespace(success=False, error_message="stop")

    async def fake_preflight(self, url: str) -> str:
        calls.append(f"preflight:{url}")
        return url

    monkeypatch.setattr(
        crawler_engine.network_sandbox, "configure_from_env", fake_refresh
    )
    monkeypatch.setattr(crawler_engine, "AsyncWebCrawler", FakeCrawler)
    monkeypatch.setattr(crawler_engine.CrawlerEngine, "_preflight_url", fake_preflight)

    engine = crawler_engine.CrawlerEngine()
    await engine.save_assets("https://public.example/page", "site-1", tmp_path)

    assert calls == [
        "refresh",
        "preflight:https://public.example/page",
        "fetch:https://public.example/page",
    ]
