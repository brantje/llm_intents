"""
Live fetch/parse checks against diverse public websites.

Run explicitly (requires network):

    RUN_LIVE_FETCH_TESTS=1 pytest tests/test_fetch_webpage_live_sites.py -v

Or generate a summary report:

    python tests/test_fetch_webpage_live_sites.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import aiohttp
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from custom_components.llm_intents.const import (  # noqa: E402
    DEFAULT_FETCH_WEBPAGE_MAX_BYTES,
    DEFAULT_FETCH_WEBPAGE_TIMEOUT,
    DEFAULT_FETCH_WEBPAGE_USER_AGENT,
)
from custom_components.llm_intents.fetch_webpage_bypass import (  # noqa: E402
    FetchWebpageConfig,
    content_is_usable,
    fetch_with_gate_bypass,
    parse_page_state,
)

DEFAULT_MAX_CHARS = 16000

LIVE_SITE_URLS = [
    # Reference / minimal
    "https://example.com/",
    "https://www.example.org/",
    "https://httpbin.org/html",
    "https://www.w3.org/standards/",
    # News / communities
    "https://news.ycombinator.com/",
    "https://lobste.rs/",
    "https://www.reddit.com/r/python/",
    "https://www.reddit.com/r/homeassistant/",
    "https://news.google.com/home",
    "https://www.bbc.com/news",
    "https://www.reuters.com/world/",
    "https://apnews.com/",
    "https://www.theguardian.com/international",
    "https://www.nytimes.com/",
    "https://www.washingtonpost.com/",
    "https://techcrunch.com/",
    "https://arstechnica.com/",
    "https://www.theverge.com/",
    "https://www.wired.com/",
    "https://stackoverflow.com/questions",
    "https://serverfault.com/",
    "https://superuser.com/",
    # Documentation
    "https://docs.python.org/3/",
    "https://developer.mozilla.org/en-US/docs/Web/HTML",
    "https://kubernetes.io/docs/home/",
    "https://docs.docker.com/get-started/",
    "https://www.home-assistant.io/docs/",
    "https://www.rust-lang.org/learn",
    "https://go.dev/doc/",
    "https://nodejs.org/en/about",
    # Wikipedia / education
    "https://en.wikipedia.org/wiki/Python_(programming_language)",
    "https://en.wikipedia.org/wiki/Main_Page",
    "https://www.khanacademy.org/",
    "https://www.coursera.org/",
    # Government / institutions
    "https://www.usa.gov/",
    "https://www.gov.uk/",
    "https://www.nasa.gov/",
    "https://www.who.int/",
    "https://www.un.org/en",
    # Open source / tech orgs
    "https://github.com/about",
    "https://gitlab.com/explore",
    "https://blog.cloudflare.com/",
    "https://blog.mozilla.org/en/",
    "https://www.debian.org/intro/about",
    "https://www.kernel.org/",
    "https://www.python.org/about/",
    "https://www.postgresql.org/about/",
    # Blogs / essays
    "https://paulgraham.com/articles.html",
    "https://sive.rs/blog",
    "https://jvns.ca/",
    "https://simonwillison.net/",
    # E-commerce / business (public landing pages)
    "https://www.mozilla.org/en-US/firefox/new/",
    "https://www.cloudflare.com/learning/",
    "https://www.digitalocean.com/resources",
    "https://aws.amazon.com/what-is-cloud-computing/",
    # Regional / misc
    "https://www.nu.nl/",
    "https://tweakers.net/",
    "https://nos.nl/",
    "https://www.lemonde.fr/",
    "https://www.spiegel.de/",
    "https://www.bbc.co.uk/sport",
    "https://www.espn.com/",
    "https://www.imdb.com/chart/top/",
    "https://www.gutenberg.org/",
    "https://archive.org/",
    "https://www.creativecommons.org/",
]


@dataclass
class SiteFetchResult:
    """Result of fetching and parsing a single URL."""

    url: str
    status: str
    http_status: int | None = None
    title: str = ""
    content_length: int = 0
    truncated: bool = False
    detail: str = ""
    bypass_recovered: bool = False


def build_live_fetch_config(*, max_chars: int = DEFAULT_MAX_CHARS) -> FetchWebpageConfig:
    """Build fetch config matching the live audit defaults."""
    return FetchWebpageConfig(
        timeout=DEFAULT_FETCH_WEBPAGE_TIMEOUT,
        max_bytes=DEFAULT_FETCH_WEBPAGE_MAX_BYTES,
        max_chars=max_chars,
        link_mode="none",
        content_format="paragraphs",
        user_agent=DEFAULT_FETCH_WEBPAGE_USER_AGENT,
        bypass_gates=True,
    )


async def fetch_and_parse_url(  # noqa: PLR0911
    session: aiohttp.ClientSession,
    url: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> SiteFetchResult:
    """Fetch a URL and parse it using the same rules as FetchWebpageTool."""
    config = build_live_fetch_config(max_chars=max_chars)

    try:
        fetch_result = await fetch_with_gate_bypass(session, url, config=config)
        if isinstance(fetch_result, dict):
            error = fetch_result.get("error", "Unknown fetch error")
            reason = fetch_result.get("reason")
            if reason in {"consent", "bot_check", "js_required"}:
                return SiteFetchResult(
                    url=url,
                    status="blocked",
                    http_status=fetch_result.get("http_status"),
                    detail=error,
                )
            if "HTTP" in error:
                return SiteFetchResult(
                    url=url,
                    status="http_error",
                    http_status=fetch_result.get("http_status"),
                    detail=error,
                )
            if "Unsupported content type" in error:
                return SiteFetchResult(
                    url=url,
                    status="unsupported_type",
                    detail=error,
                )
            if "exceeded maximum size" in error:
                return SiteFetchResult(
                    url=url,
                    status="too_large",
                    detail=error,
                )
            return SiteFetchResult(url=url, status="empty", detail=error)

        state = parse_page_state(
            fetch_result.html,
            final_url=fetch_result.final_url,
            config=config,
        )
        if not content_is_usable(state):
            return SiteFetchResult(
                url=url,
                status="blocked" if state.gate_message else "empty",
                http_status=fetch_result.http_status,
                title=state.title,
                detail=state.gate_message or fetch_result.final_url,
            )

        return SiteFetchResult(
            url=url,
            status="ok",
            http_status=fetch_result.http_status,
            title=state.title,
            content_length=state.content.text_length(),
            truncated=state.truncated,
            detail=fetch_result.final_url,
            bypass_recovered=fetch_result.bypass_recovered,
        )

    except aiohttp.ClientError as err:
        return SiteFetchResult(url=url, status="network_error", detail=str(err))
    except Exception as err:
        return SiteFetchResult(url=url, status="error", detail=str(err))


async def run_live_site_audit(
    urls: list[str] | None = None,
    *,
    concurrency: int = 8,
) -> list[SiteFetchResult]:
    """Fetch and parse many URLs concurrently."""
    targets = urls or LIVE_SITE_URLS
    semaphore = asyncio.Semaphore(concurrency)
    results: list[SiteFetchResult] = []

    async with aiohttp.ClientSession() as session:

        async def run_one(url: str) -> SiteFetchResult:
            async with semaphore:
                return await fetch_and_parse_url(session, url)

        results = await asyncio.gather(*(run_one(url) for url in targets))

    return list(results)


def summarize_results(results: list[SiteFetchResult]) -> str:
    """Return a human-readable summary of live fetch results."""
    by_status: dict[str, list[SiteFetchResult]] = {}
    for result in results:
        by_status.setdefault(result.status, []).append(result)

    lines = [
        f"Live fetch webpage audit ({len(results)} sites)",
        "",
    ]
    for status in (
        "ok",
        "blocked",
        "empty",
        "http_error",
        "unsupported_type",
        "too_large",
        "network_error",
        "error",
    ):
        items = by_status.get(status, [])
        if not items:
            continue
        lines.append(f"{status}: {len(items)}")
        for item in sorted(items, key=lambda r: r.url):
            extra = item.detail
            if status == "ok":
                extra = f"title={item.title[:60]!r}, chars={item.content_length}, truncated={item.truncated}"
                if item.bypass_recovered:
                    extra = f"{extra}, bypass_recovered=True"
            lines.append(f"  - {item.url} ({extra})")
        lines.append("")

    ok_count = len(by_status.get("ok", []))
    bypass_recovered = sum(1 for result in results if result.bypass_recovered)
    lines.append(f"Success rate: {ok_count}/{len(results)} ({100 * ok_count / len(results):.1f}%)")
    if bypass_recovered:
        lines.append(f"Bypass recovered: {bypass_recovered}")
    return "\n".join(lines)


@pytest.mark.enable_socket
@pytest.mark.skipif(
    os.getenv("RUN_LIVE_FETCH_TESTS") != "1",
    reason="Set RUN_LIVE_FETCH_TESTS=1 to run live website fetch tests",
)
async def test_live_sites_minimum_success_rate() -> None:
    """Most diverse public pages should yield non-empty parsed content."""
    results = await run_live_site_audit()
    ok = [result for result in results if result.status == "ok"]
    empty = [result for result in results if result.status == "empty"]

    assert len(LIVE_SITE_URLS) >= 50
    assert len(ok) >= len(LIVE_SITE_URLS) * 0.5, summarize_results(results)
    assert "news.ycombinator.com" in {result.url for result in ok + empty}


@pytest.mark.enable_socket
@pytest.mark.skipif(
    os.getenv("RUN_LIVE_FETCH_TESTS") != "1",
    reason="Set RUN_LIVE_FETCH_TESTS=1 to run live website fetch tests",
)
async def test_live_site_hacker_news_has_content() -> None:
    """Hacker News front page should produce readable content."""
    results = await run_live_site_audit(["https://news.ycombinator.com/"])
    assert results[0].status == "ok"
    assert results[0].content_length > 200


@pytest.mark.enable_socket
@pytest.mark.skipif(
    os.getenv("RUN_LIVE_FETCH_TESTS") != "1",
    reason="Set RUN_LIVE_FETCH_TESTS=1 to run live website fetch tests",
)
async def test_live_site_tweakers_has_content() -> None:
    """Tweakers front page should produce readable content."""
    results = await run_live_site_audit(["https://tweakers.net/"])
    assert results[0].status == "ok", results[0]
    assert results[0].content_length > 200


if __name__ == "__main__":
    report = asyncio.run(run_live_site_audit())
    sys.stdout.write(f"{summarize_results(report)}\n")
