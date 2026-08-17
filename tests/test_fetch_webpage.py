"""Tests for the Fetch Webpage tool."""

from unittest.mock import MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm

from custom_components.llm_intents.const import (
    CONF_FETCH_WEBPAGE_BYPASS_GATES,
    CONF_FETCH_WEBPAGE_CONTENT_FORMAT,
    CONF_FETCH_WEBPAGE_LINK_MODE,
    CONF_FETCH_WEBPAGE_MAX_BYTES,
    CONF_FETCH_WEBPAGE_MAX_CHARS,
    CONF_FETCH_WEBPAGE_TIMEOUT,
    CONF_FETCH_WEBPAGE_USER_AGENT,
    CONF_FETCH_WEBPAGES,
    DOMAIN,
)
from custom_components.llm_intents.fetch_webpage import (
    FetchWebpageTool,
    PageContent,
    detect_unfetchable_page,
    is_text_content_type,
    is_valid_http_url,
    parse_webpage,
)

from .utils import mock_html_session, mock_html_session_sequence


def content_text(content: PageContent) -> str:
    """Flatten typed page content into a single string for assertions."""
    return " ".join(block.text for block in content.blocks)


def json_content_text(content: dict) -> str:
    """Flatten tool response content JSON into a single string."""
    return " ".join(block["text"] for block in content["blocks"])


@pytest.fixture
def config() -> dict:
    """Return default fetch webpage config."""
    return {
        CONF_FETCH_WEBPAGES: True,
        CONF_FETCH_WEBPAGE_MAX_CHARS: 16000,
        CONF_FETCH_WEBPAGE_TIMEOUT: 15,
        CONF_FETCH_WEBPAGE_MAX_BYTES: 5 * 1024 * 1024,
        CONF_FETCH_WEBPAGE_LINK_MODE: "references",
        CONF_FETCH_WEBPAGE_CONTENT_FORMAT: "paragraphs",
        CONF_FETCH_WEBPAGE_USER_AGENT: "Tools for Assist Fetch Webpage",
        CONF_FETCH_WEBPAGE_BYPASS_GATES: True,
    }


@pytest.fixture
def tool(config: dict, mock_hass: HomeAssistant) -> FetchWebpageTool:
    """Create a FetchWebpageTool instance."""
    mock_hass.data[DOMAIN]["config"] = config
    mock_hass.config_entries.async_entries.return_value = [MagicMock(options={})]
    return FetchWebpageTool(config, mock_hass)


SAMPLE_HTML = """
<!DOCTYPE html>
<html>
<head><title>Test Page</title></head>
<body>
<nav>Skip navigation</nav>
<main>
  <h1>Main Heading</h1>
  <p>Main paragraph with <a href="/docs">documentation</a>.</p>
</main>
</body>
</html>
"""

BODY_ONLY_HTML = """
<!DOCTYPE html>
<html>
<head><title>Body Page</title></head>
<body>
  <p>Body paragraph with <a href="https://example.com/help">help</a>.</p>
</body>
</html>
"""

MULTI_ARTICLE_HTML = """
<!DOCTYPE html>
<html>
<head><title>News Frontpage</title></head>
<body>
  <article class="headline"><h2>Small headline one</h2><p>Short teaser text.</p></article>
  <article class="headline"><h2>Small headline two</h2><p>Another short teaser.</p></article>
  <div class="news-list">
    <p>Breaking story with enough detail to be useful when extracted from the page body.</p>
    <p>Second paragraph describing the event, background, and additional context for readers.</p>
    <p>Third paragraph with more information so the extracted content clearly exceeds the minimum.</p>
  </div>
</body>
</html>
"""

TWEAKERS_HEADLINES_HTML = """
<!DOCTYPE html>
<html>
<head><title>Tweakers</title></head>
<body>
<div class="contentArea">
  <div class="headlines">
    <div class="headlines-1">
      <div class="headlines-head">
        <h2 class="fp-title"><a href="/nieuws/list/">Laatste nieuws</a></h2>
      </div>
      <div class="headlineItem news">
        <article class="headline">
          <time class="headline--time" datetime="12:45">12:45</time>
          <a class="headline--anchor" href="/nieuws/1">Nothing-baas: flagshipkiller kan niet meer bestaan</a>
          <a aria-label="81 reacties" class="comment-counter" href="/nieuws/1#reacties">81</a>
        </article>
      </div>
      <div class="headlineItem news">
        <article class="headline">
          <time class="headline--time" datetime="12:03">12:03</time>
          <a class="headline--anchor" href="/nieuws/2">Gerucht: Microsoft overweegt afsplitsing XBOX</a>
        </article>
      </div>
    </div>
    <div class="headlines-2">
      <h2 class="headline--day">vrijdag 12 juni</h2>
      <div class="headlineItem news">
        <article class="headline">
          <time class="headline--time" datetime="09:24">09:24</time>
          <a class="headline--anchor" href="/nieuws/3">VS blokkeert Anthropic Claude vanwege zorgen jailbreak</a>
        </article>
      </div>
    </div>
  </div>
</div>
</body>
</html>
"""

NOISY_SITE_HTML = """
<!DOCTYPE html>
<html>
<head>
  <title>Example Site - Long generic site description for every page</title>
</head>
<body>
  <div id="menu" class="site-menu">
    <ul>
      <li><a href="/a">HOME</a></li>
      <li><a href="/b">ABOUT</a></li>
      <li><a href="/c">CONTACT</a></li>
    </ul>
  </div>
  <div class="wrapper">
    <div id="page-content" class="content event-list">
      <h2>Events</h2>
      <p>Find upcoming events in the city.</p>
      <h2>Summer Festival</h2>
      <p>Saturday 12 June 2026 at the town square.</p>
      <h2>Jazz Night</h2>
      <p>Friday 18 June 2026 at the music cafe.</p>
    </div>
  </div>
  <div class="site-footer footer">
    <a href="/privacy">Privacy</a>
  </div>
</body>
</html>
"""

ELEMENTOR_PAGE_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta property="og:title" content="Membership Plans | Example Site" />
</head>
<body>
  <main class="site-main page type-page">
    <div class="elementor-widget elementor-widget-heading">
      <h2>Membership options</h2>
    </div>
    <div class="elementor-widget elementor-widget-text-editor">
      <p>Choose a plan that fits your needs.</p>
    </div>
    <div class="ha-pricing-table-header"><h3>Family plan</h3></div>
    <div class="elementor-widget elementor-widget-text-editor">
      <p>Includes access for up to four people.</p>
    </div>
  </main>
</body>
</html>
"""

SUPER_SIDEBAR_HTML = """
<!DOCTYPE html>
<html>
<head><title>Explore</title></head>
<body>
  <div class="page-with-super-sidebar layout-page">
    <main class="content">
      <h1>Explore Projects</h1>
      <p>Popular open source projects hosted on GitLab.</p>
      <p>Including community-maintained tools and libraries.</p>
    </main>
  </div>
</body>
</html>
"""

HACKER_NEWS_HTML = """
<!DOCTYPE html>
<html>
<head><title>Hacker News</title></head>
<body>
<table>
  <tr><td>1.</td><td><span class="titleline"><a href="https://example.com/1">Show HN: Example project alpha</a></span></td></tr>
  <tr><td>2.</td><td><span class="titleline"><a href="https://example.com/2">Launch HN: Example project beta with a longer title</a></span></td></tr>
  <tr><td>3.</td><td><span class="titleline"><a href="https://example.com/3">Ask HN: What are you working on this week?</a></span></td></tr>
  <tr><td>4.</td><td><span class="titleline"><a href="https://example.com/4">A deep dive into static HTML parsing techniques</a></span></td></tr>
</table>
</body>
</html>
"""

CONSENT_PAGE_HTML = """
<!DOCTYPE html>
<html><head><title>Privacy Gate</title></head><body></body></html>
"""

JS_REQUIRED_HTML = """
<!DOCTYPE html>
<html>
<head><title>Internet Archive</title></head>
<body>
  <noscript>Javascript is required for this site.</noscript>
  <div id="root"></div>
</body>
</html>
"""

BOT_CHECK_HTML = """
<!DOCTYPE html>
<html><head><title>Please wait for verification</title></head><body></body></html>
"""


def test_is_valid_http_url() -> None:
    """Test URL validation."""
    assert is_valid_http_url("https://example.com/page")
    assert is_valid_http_url("http://192.168.1.10/local")
    assert not is_valid_http_url("ftp://example.com")
    assert not is_valid_http_url("https://")
    assert not is_valid_http_url("not-a-url")


def test_is_text_content_type() -> None:
    """Test textual content type detection."""
    assert is_text_content_type("text/html; charset=utf-8")
    assert is_text_content_type("application/json")
    assert not is_text_content_type("image/png")
    assert not is_text_content_type("application/pdf")
    assert not is_text_content_type("video/mp4")


def test_parse_webpage_extracts_main_content() -> None:
    """Test that semantic main content is preferred."""
    title, content, truncated = parse_webpage(
        SAMPLE_HTML,
        base_url="https://example.com/page",
        link_mode="text",
        content_format="paragraphs",
        max_chars=16000,
    )

    assert title == "Main Heading"
    text = content_text(content)
    assert "Main Heading" in text
    assert "documentation" in text
    assert "Skip navigation" not in text
    assert truncated is False


def test_parse_webpage_ignores_small_article_cards() -> None:
    """Test that pages with many small article cards fall back to richer content."""
    _, content, truncated = parse_webpage(
        MULTI_ARTICLE_HTML,
        base_url="https://example.com/",
        link_mode="text",
        content_format="paragraphs",
        max_chars=16000,
    )

    text = content_text(content)
    assert "Breaking story with enough detail" in text
    assert "Small headline one" not in text
    assert truncated is False


def test_parse_webpage_extracts_headline_articles() -> None:
    """Test that list-style article cards export their headline links."""
    _, content, truncated = parse_webpage(
        TWEAKERS_HEADLINES_HTML,
        base_url="https://tweakers.net/",
        link_mode="none",
        content_format="paragraphs",
        max_chars=16000,
    )

    text = content_text(content)
    assert "Laatste nieuws" in text
    assert "12:45 - Nothing-baas: flagshipkiller kan niet meer bestaan" in text
    assert "12:03 - Gerucht: Microsoft overweegt afsplitsing XBOX" in text
    assert "vrijdag 12 juni" in text
    assert "09:24 - VS blokkeert Anthropic Claude vanwege zorgen jailbreak" in text
    assert truncated is False


def test_parse_webpage_fallback_to_body() -> None:
    """Test fallback to body when no semantic container exists."""
    title, content, truncated = parse_webpage(
        BODY_ONLY_HTML,
        base_url="https://example.com",
        link_mode="text",
        content_format="paragraphs",
        max_chars=16000,
    )

    assert title == "Body Page"
    text = content_text(content)
    assert "Body paragraph" in text
    assert "help" in text
    assert truncated is False


def test_parse_webpage_truncation() -> None:
    """Test content truncation."""
    _, content, truncated = parse_webpage(
        BODY_ONLY_HTML,
        base_url="https://example.com",
        link_mode="text",
        content_format="text",
        max_chars=10,
    )

    assert truncated is True
    assert content.text_length() == 10


def test_link_mode_references() -> None:
    """Test reference-style links."""
    _, content, _ = parse_webpage(
        BODY_ONLY_HTML,
        base_url="https://example.com",
        link_mode="references",
        content_format="text",
        max_chars=16000,
    )

    assert "help [1]" in content_text(content)
    assert content.references == [(1, "https://example.com/help")]
    assert "References:" not in content_text(content)


def test_link_mode_inline() -> None:
    """Test inline links."""
    _, content, _ = parse_webpage(
        BODY_ONLY_HTML,
        base_url="https://example.com",
        link_mode="inline",
        content_format="text",
        max_chars=16000,
    )

    assert "help (https://example.com/help)" in content_text(content)
    assert not content.references


def test_link_mode_text() -> None:
    """Test text-only links."""
    _, content, _ = parse_webpage(
        BODY_ONLY_HTML,
        base_url="https://example.com",
        link_mode="text",
        content_format="text",
        max_chars=16000,
    )

    text = content_text(content)
    assert "help" in text
    assert "https://example.com/help" not in text


def test_link_mode_none() -> None:
    """Test that link handling can be disabled."""
    _, content, _ = parse_webpage(
        BODY_ONLY_HTML,
        base_url="https://example.com",
        link_mode="none",
        content_format="text",
        max_chars=16000,
    )

    text = content_text(content)
    assert "help" in text
    assert "https://example.com/help" not in text
    assert not content.references


def test_parse_webpage_typed_structure() -> None:
    """Test that content is returned as typed blocks with separate references."""
    _, content, _ = parse_webpage(
        SAMPLE_HTML,
        base_url="https://example.com/page",
        link_mode="references",
        content_format="paragraphs",
        max_chars=16000,
    )

    assert content.blocks[0].type == "heading"
    assert content.blocks[0].level == 1
    assert content.blocks[0].text == "Main Heading"
    assert "documentation [1]" in content.blocks[1].text
    assert content.references == [(1, "https://example.com/docs")]


def test_content_format_markdown() -> None:
    """Test markdown-style formatting."""
    _, content, _ = parse_webpage(
        SAMPLE_HTML,
        base_url="https://example.com/page",
        link_mode="text",
        content_format="markdown",
        max_chars=16000,
    )

    assert content.blocks[0].type == "heading"
    assert content.blocks[0].level == 1
    assert content.blocks[0].text == "Main Heading"


def test_parse_webpage_filters_navigation_noise() -> None:
    """Test that div-based menus and footers are excluded from content."""
    title, content, truncated = parse_webpage(
        NOISY_SITE_HTML,
        base_url="https://example.com/events",
        link_mode="none",
        content_format="paragraphs",
        max_chars=16000,
    )

    assert title == "Events"
    text = content_text(content)
    assert "Summer Festival" in text
    assert "Jazz Night" in text
    assert "HOME ABOUT CONTACT" not in text.replace(" ", "")
    assert "Privacy" not in text
    assert truncated is False


def test_parse_webpage_elementor_content() -> None:
    """Test that Elementor-style class names are not treated as boilerplate."""
    title, content, truncated = parse_webpage(
        ELEMENTOR_PAGE_HTML,
        base_url="https://example.com/membership/",
        link_mode="none",
        content_format="paragraphs",
        max_chars=16000,
    )

    assert title == "Membership Plans | Example Site"
    text = content_text(content)
    assert "Membership options" in text
    assert "Includes access for up to four people" in text
    assert "Family plan" in text
    assert truncated is False


def test_parse_webpage_keeps_super_sidebar_wrapper() -> None:
    """Test that page-with-super-sidebar is not treated as removable sidebar noise."""
    title, content, truncated = parse_webpage(
        SUPER_SIDEBAR_HTML,
        base_url="https://gitlab.com/explore",
        link_mode="none",
        content_format="paragraphs",
        max_chars=16000,
    )

    assert title == "Explore Projects"
    assert "Popular open source projects hosted on GitLab." in content_text(content)
    assert truncated is False


def test_parse_webpage_extracts_table_rows() -> None:
    """Test that table-based pages like Hacker News yield story titles."""
    title, content, truncated = parse_webpage(
        HACKER_NEWS_HTML,
        base_url="https://news.ycombinator.com/",
        link_mode="none",
        content_format="paragraphs",
        max_chars=16000,
    )

    assert title == "Hacker News"
    text = content_text(content)
    assert "Show HN: Example project alpha" in text
    assert "Ask HN: What are you working on this week?" in text
    assert content.text_length() > 100
    assert truncated is False


def test_detect_unfetchable_consent_url() -> None:
    """Test consent-wall detection from redirect URLs."""
    reason = detect_unfetchable_page(
        CONSENT_PAGE_HTML,
        final_url="https://myprivacy.dpgmedia.nl/consent?site=nu.nl",
        title="Privacy Gate",
    )

    assert reason is not None
    assert "consent" in reason.lower()


def test_detect_unfetchable_js_required() -> None:
    """Test JavaScript-only page detection."""
    reason = detect_unfetchable_page(
        JS_REQUIRED_HTML,
        final_url="https://archive.org/",
        title="Internet Archive",
    )
    assert reason is not None
    assert "javascript" in reason.lower()

    title_reason = detect_unfetchable_page(
        CONSENT_PAGE_HTML,
        final_url="https://archive.org/",
        title="Javascript is required for this site.",
    )
    assert title_reason is not None
    assert "javascript" in title_reason.lower()


def test_detect_unfetchable_bot_check() -> None:
    """Test bot-check page detection from title."""
    reason = detect_unfetchable_page(
        BOT_CHECK_HTML,
        final_url="https://www.reddit.com/r/python/",
        title="Please wait for verification",
    )

    assert reason is not None
    assert "verification" in reason.lower()


def test_detect_unfetchable_returns_none_for_normal_page() -> None:
    """Test that normal pages are not flagged as unfetchable."""
    assert (
        detect_unfetchable_page(
            SAMPLE_HTML,
            final_url="https://example.com/page",
            title="Test Page",
        )
        is None
    )


async def test_fetch_success(tool: FetchWebpageTool) -> None:
    """Test successful webpage fetch."""
    with patch(
        "custom_components.llm_intents.fetch_webpage.async_get_clientsession",
        return_value=mock_html_session(
            200,
            SAMPLE_HTML,
            final_url="https://example.com/page",
        ),
    ):
        result = await tool.async_call(
            tool.hass,
            llm.ToolInput(tool_name="fetch_webpage", tool_args={"url": "https://example.com/page"}, id="1"),
            llm.LLMContext(DOMAIN, None, None, None, None),
        )

    assert result["url"] == "https://example.com/page"
    assert result["title"] == "Main Heading"
    assert "Main paragraph" in json_content_text(result["content"])
    assert result["content"]["blocks"]
    assert result["truncated"] is False
    assert "instruction" in result


async def test_fetch_invalid_url(tool: FetchWebpageTool) -> None:
    """Test invalid URL handling."""
    result = await tool.async_call(
        tool.hass,
        llm.ToolInput(tool_name="fetch_webpage", tool_args={"url": "ftp://example.com"}, id="1"),
        llm.LLMContext(DOMAIN, None, None, None, None),
    )

    assert result == {"error": "Only http:// and https:// URLs are supported"}


async def test_fetch_http_error(tool: FetchWebpageTool) -> None:
    """Test HTTP error handling."""
    with patch(
        "custom_components.llm_intents.fetch_webpage.async_get_clientsession",
        return_value=mock_html_session(404, "Not found"),
    ):
        result = await tool.async_call(
            tool.hass,
            llm.ToolInput(
                tool_name="fetch_webpage",
                tool_args={"url": "https://example.com/missing"},
                id="1",
            ),
            llm.LLMContext(DOMAIN, None, None, None, None),
        )

    assert "error" in result
    assert "HTTP 404" in result["error"]


async def test_fetch_binary_content_rejected(tool: FetchWebpageTool) -> None:
    """Test binary content rejection."""
    with patch(
        "custom_components.llm_intents.fetch_webpage.async_get_clientsession",
        return_value=mock_html_session(
            200,
            "binary",
            content_type="image/png",
        ),
    ):
        result = await tool.async_call(
            tool.hass,
            llm.ToolInput(
                tool_name="fetch_webpage",
                tool_args={"url": "https://example.com/image.png"},
                id="1",
            ),
            llm.LLMContext(DOMAIN, None, None, None, None),
        )

    assert result == {"error": "Unsupported content type for webpage fetch: image/png"}


async def test_fetch_response_too_large(tool: FetchWebpageTool) -> None:
    """Test oversized response handling."""
    tool.hass.data[DOMAIN]["config"][CONF_FETCH_WEBPAGE_MAX_BYTES] = 10

    with patch(
        "custom_components.llm_intents.fetch_webpage.async_get_clientsession",
        return_value=mock_html_session(200, "x" * 100),
    ):
        result = await tool.async_call(
            tool.hass,
            llm.ToolInput(
                tool_name="fetch_webpage",
                tool_args={"url": "https://example.com/large"},
                id="1",
            ),
            llm.LLMContext(DOMAIN, None, None, None, None),
        )

    assert result == {"error": "Response exceeded maximum size of 10 bytes"}


async def test_fetch_blocked_bot_page(tool: FetchWebpageTool) -> None:
    """Test that bot-check pages return an explicit error instead of empty content."""
    with patch(
        "custom_components.llm_intents.fetch_webpage.async_get_clientsession",
        return_value=mock_html_session(
            200,
            BOT_CHECK_HTML,
            final_url="https://www.reddit.com/r/python/",
        ),
    ):
        result = await tool.async_call(
            tool.hass,
            llm.ToolInput(
                tool_name="fetch_webpage",
                tool_args={"url": "https://www.reddit.com/r/python/"},
                id="1",
            ),
            llm.LLMContext(DOMAIN, None, None, None, None),
        )

    assert result["url"] == "https://www.reddit.com/r/python/"
    assert "error" in result
    assert "verification" in result["error"].lower()


async def test_fetch_blocked_js_required_page(tool: FetchWebpageTool) -> None:
    """Test that JavaScript-only pages return an explicit error."""
    with patch(
        "custom_components.llm_intents.fetch_webpage.async_get_clientsession",
        return_value=mock_html_session(
            200,
            JS_REQUIRED_HTML,
            final_url="https://archive.org/",
        ),
    ):
        result = await tool.async_call(
            tool.hass,
            llm.ToolInput(
                tool_name="fetch_webpage",
                tool_args={"url": "https://archive.org/"},
                id="1",
            ),
            llm.LLMContext(DOMAIN, None, None, None, None),
        )

    assert result["url"] == "https://archive.org/"
    assert "error" in result
    assert "javascript" in result["error"].lower()


CONSENT_GATE_HTML = """
<!DOCTYPE html>
<html><head><title>Privacy Gate</title></head><body></body></html>
"""

ARTICLE_HTML = """
<!DOCTYPE html>
<html>
<head><title>Breaking News</title></head>
<body>
<main>
  <h1>Breaking News</h1>
  <p>This is a long article body with enough content to pass the minimum threshold for parsed webpage content extraction.</p>
  <p>Additional paragraph with more details about the story and background information for readers.</p>
</main>
</body>
</html>
"""

BOT_GATE_HTML = """
<!DOCTYPE html>
<html><head><title>Please wait for verification</title></head><body></body></html>
"""


async def test_bypass_consent_callback_url(tool: FetchWebpageTool) -> None:
    """Test consent URL recovery via callbackUrl."""
    session = mock_html_session_sequence(
        [
            {
                "status": 200,
                "text": CONSENT_GATE_HTML,
                "final_url": (
                    "https://myprivacy.example.com/consent?"
                    "callbackUrl=https%3A%2F%2Fexample.com%2Farticle"
                ),
            },
            {
                "status": 200,
                "text": ARTICLE_HTML,
                "final_url": "https://example.com/article",
            },
        ],
    )
    with patch(
        "custom_components.llm_intents.fetch_webpage.async_get_clientsession",
        return_value=session,
    ):
        result = await tool.async_call(
            tool.hass,
            llm.ToolInput(
                tool_name="fetch_webpage",
                tool_args={"url": "https://example.com/article"},
                id="1",
            ),
            llm.LLMContext(DOMAIN, None, None, None, None),
        )

    assert result["url"] == "https://example.com/article"
    assert "Breaking News" in json_content_text(result["content"])
    assert session.request_count["value"] <= 4


async def test_bypass_consent_cookies(tool: FetchWebpageTool) -> None:
    """Test consent cookie injection bypass."""
    session = mock_html_session_sequence(
        [
            {
                "status": 200,
                "text": CONSENT_GATE_HTML,
                "final_url": "https://www.nu.nl/artikel",
            },
            {
                "status": 200,
                "text": ARTICLE_HTML,
                "final_url": "https://www.nu.nl/artikel",
            },
        ],
    )
    with patch(
        "custom_components.llm_intents.fetch_webpage.async_get_clientsession",
        return_value=session,
    ):
        result = await tool.async_call(
            tool.hass,
            llm.ToolInput(
                tool_name="fetch_webpage",
                tool_args={"url": "https://www.nu.nl/artikel"},
                id="1",
            ),
            llm.LLMContext(DOMAIN, None, None, None, None),
        )

    assert "Breaking News" in json_content_text(result["content"])
    assert session.request_count["value"] <= 4


async def test_bypass_browser_headers(tool: FetchWebpageTool) -> None:
    """Test browser header retry for bot-check pages."""
    session = mock_html_session_sequence(
        [
            {
                "status": 200,
                "text": BOT_GATE_HTML,
                "final_url": "https://example.com/page",
            },
            {
                "status": 200,
                "text": ARTICLE_HTML,
                "final_url": "https://example.com/page",
            },
        ],
    )
    with patch(
        "custom_components.llm_intents.fetch_webpage.async_get_clientsession",
        return_value=session,
    ):
        result = await tool.async_call(
            tool.hass,
            llm.ToolInput(
                tool_name="fetch_webpage",
                tool_args={"url": "https://example.com/page"},
                id="1",
            ),
            llm.LLMContext(DOMAIN, None, None, None, None),
        )

    assert "Breaking News" in json_content_text(result["content"])
    assert session.request_count["value"] <= 4


async def test_bypass_disabled_returns_error(tool: FetchWebpageTool, config: dict) -> None:
    """Test that bypass can be disabled."""
    config[CONF_FETCH_WEBPAGE_BYPASS_GATES] = False
    tool.hass.data[DOMAIN]["config"] = config
    session = mock_html_session(
        200,
        BOT_GATE_HTML,
        final_url="https://example.com/page",
    )
    with patch(
        "custom_components.llm_intents.fetch_webpage.async_get_clientsession",
        return_value=session,
    ):
        result = await tool.async_call(
            tool.hass,
            llm.ToolInput(
                tool_name="fetch_webpage",
                tool_args={"url": "https://example.com/page"},
                id="1",
            ),
            llm.LLMContext(DOMAIN, None, None, None, None),
        )

    assert "error" in result
    assert result.get("reason") == "bot_check"
    assert session.get.call_count == 1


async def test_bypass_cap_limits_retries(tool: FetchWebpageTool) -> None:
    """Test that bypass attempts are capped at four HTTP requests."""
    session = mock_html_session_sequence(
        [
            {
                "status": 200,
                "text": CONSENT_GATE_HTML,
                "final_url": (
                    "https://myprivacy.example.com/consent?"
                    "callbackUrl=https%3A%2F%2Fexample.com%2Farticle"
                ),
            },
            {"status": 200, "text": CONSENT_GATE_HTML, "final_url": "https://example.com/article"},
            {"status": 200, "text": CONSENT_GATE_HTML, "final_url": "https://example.com/article"},
            {"status": 200, "text": CONSENT_GATE_HTML, "final_url": "https://example.com/article"},
            {"status": 200, "text": CONSENT_GATE_HTML, "final_url": "https://example.com/article"},
        ],
    )
    with patch(
        "custom_components.llm_intents.fetch_webpage.async_get_clientsession",
        return_value=session,
    ):
        result = await tool.async_call(
            tool.hass,
            llm.ToolInput(
                tool_name="fetch_webpage",
                tool_args={"url": "https://example.com/article"},
                id="1",
            ),
            llm.LLMContext(DOMAIN, None, None, None, None),
        )

    assert "error" in result
    assert session.request_count["value"] <= 4
