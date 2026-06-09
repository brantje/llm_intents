"""Fetch webpage tool."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

import aiohttp
import voluptuous as vol
from bs4 import BeautifulSoup, Tag
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .base_tool import BaseTool
from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers import llm
    from homeassistant.util.json import JsonObjectType

_LOGGER = logging.getLogger(__name__)

BINARY_CONTENT_TYPE_PREFIXES = (
    "image/",
    "video/",
    "audio/",
    "application/octet-stream",
    "application/pdf",
    "application/zip",
    "application/gzip",
    "application/x-gzip",
    "application/x-tar",
    "application/x-7z-compressed",
    "application/vnd.",
    "font/",
)

TEXT_CONTENT_TYPE_PREFIXES = (
    "text/",
    "application/xhtml+xml",
    "application/xml",
    "application/json",
    "application/javascript",
    "application/ld+json",
)

NOISE_TAGS = {
    "script",
    "style",
    "nav",
    "aside",
    "noscript",
    "iframe",
    "svg",
    "form",
}

NOISE_ROLES = {
    "navigation",
    "banner",
    "contentinfo",
    "complementary",
    "search",
}

NOISE_CLASS_TOKENS = frozenset(
    {
        "nav",
        "menu",
        "navbar",
        "header",
        "footer",
        "sidebar",
        "breadcrumb",
        "modal",
        "popup",
        "overlay",
        "advert",
        "social",
        "share",
        "newsletter",
        "subscribe",
        "comment",
        "related",
        "promo",
        "skip",
        "widget",
    },
)

NOISE_CLASS_PREFIXES = (
    "cookie-",
    "banner-",
    "site-header",
    "page-header",
    "main-header",
    "global-header",
    "nav-",
    "menu-",
    "footer-",
    "sidebar-",
    "widget-area",
)

NOISE_CLASS_SUFFIXES = (
    "-nav",
    "-menu",
    "-footer",
)

CONTENT_CLASS_TOKENS = frozenset(
    {
        "article",
        "post",
        "entry",
        "main",
        "agenda",
        "summary",
        "uitagenda",
    },
)

CONTENT_CLASS_PREFIXES = (
    "content-",
    "entry-content",
    "page-content",
    "post-content",
    "main-content",
    "site-content",
    "event-",
    "event-list",
    "page-body",
    "site-body",
    "site-main",
    "type-page",
)

TITLE_SEPARATORS = (" | ", " - ", " :: ")

BLOCK_TAGS = [
    "p",
    "li",
    "blockquote",
    "pre",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "td",
    "th",
    "tr",
    "dt",
    "dd",
]

DIV_BLOCK_MIN_CHARS = 40
NOISE_SURVIVABILITY_RATIO = 0.6
MIN_MAIN_CONTENT_CHARS = 100
MIN_SITE_TITLE_CHARS = 10
MIN_SUBSTANTIVE_BLOCK_CHARS = 2
MAX_CALENDAR_DAY_CHARS = 2
MIN_NAV_LINK_COUNT = 3
MAX_NAV_LINK_TEXT_RATIO = 0.75
MAIN_CONTENT_SCORE_THRESHOLD = 0.75

RESPONSE_INSTRUCTION = """
Use the fetched webpage content to answer the user's question.
If the content does not contain enough information, say so clearly.
Do not invent details that are not supported by the fetched content.
""".strip()

HEADING_PREFIXES = {
    "h1": "# ",
    "h2": "## ",
    "h3": "### ",
    "h4": "#### ",
    "h5": "##### ",
    "h6": "###### ",
}

CONSENT_URL_PATTERN = re.compile(
    r"consent|privacy-gate|cookie|gdpr|myprivacy|cmp\.",
    re.IGNORECASE,
)
CONSENT_TITLE_PATTERN = re.compile(
    r"privacy gate|cookie|consent|gdpr|we value your privacy",
    re.IGNORECASE,
)
BOT_TITLE_PATTERN = re.compile(
    r"please wait|verification|captcha|access denied|just a moment",
    re.IGNORECASE,
)
JS_REQUIRED_PATTERN = re.compile(
    r"javascript.+(?:required|enable|disabled|turned off)",
    re.IGNORECASE,
)
SPA_HTML_MARKERS = (
    "shreddit",
    "app-root",
    "__next_data__",
    'id="root"',
)


class FetchWebpageTool(BaseTool):
    """Tool for fetching and parsing webpages."""

    name = "fetch_webpage"
    description = (
        "Fetch a webpage from a URL and return its main content in a form suitable "
        "for answering questions about the page."
    )
    prompt_description = (
        "When the user provides a URL or asks about a specific webpage, use this tool "
        "to retrieve the page content before answering."
    )

    parameters = vol.Schema(
        {
            vol.Required(
                "url",
                description="The HTTP or HTTPS URL of the webpage to fetch",
            ): str,
        },
    )

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Call the tool."""
        url = tool_input.tool_args["url"].strip()
        _LOGGER.info("Fetch webpage requested for: %s", url)

        if not is_valid_http_url(url):
            return {"error": "Only http:// and https:// URLs are supported"}

        try:
            return await self._fetch_webpage(hass, url)
        except aiohttp.ClientError as err:
            _LOGGER.exception("Fetch webpage network error for %s", url)
            return {"error": f"Error fetching webpage: {err!s}"}
        except Exception as err:
            _LOGGER.exception("Fetch webpage encountered an error for %s", url)
            return {"error": f"Error fetching webpage: {err!s}"}

    async def _fetch_webpage(
        self,
        hass: HomeAssistant,
        url: str,
    ) -> JsonObjectType:
        """Fetch and parse a webpage."""
        from .fetch_webpage_bypass import (  # noqa: PLC0415
            config_from_mapping,
            content_is_usable,
            fetch_with_gate_bypass,
            parse_page_state,
        )

        config_data = hass.data[DOMAIN].get("config", {})
        entry = next(iter(hass.config_entries.async_entries(DOMAIN)))
        config = config_from_mapping({**config_data, **entry.options})

        session = async_get_clientsession(hass)
        fetch_result = await fetch_with_gate_bypass(session, url, config=config)
        if isinstance(fetch_result, dict):
            if "error" in fetch_result:
                return fetch_result
            return fetch_result

        state = parse_page_state(
            fetch_result.html,
            final_url=fetch_result.final_url,
            config=config,
        )
        if not content_is_usable(state):
            unfetchable = detect_unfetchable_page(
                fetch_result.html,
                final_url=fetch_result.final_url,
                title=state.title,
            )
            if unfetchable:
                payload: JsonObjectType = {
                    "error": unfetchable,
                    "url": fetch_result.final_url,
                }
                if state.gate.value != "none":
                    payload["reason"] = state.gate.value
                return payload
            if not state.content.strip():
                return {
                    "error": "No readable content could be extracted from the page",
                    "url": fetch_result.final_url,
                }

        return {
            "url": fetch_result.final_url,
            "title": state.title,
            "content": state.content,
            "truncated": state.truncated,
            "instruction": RESPONSE_INSTRUCTION,
        }


def is_valid_http_url(url: str) -> bool:
    """Return True when the URL uses an allowed HTTP scheme."""
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def is_text_content_type(content_type: str) -> bool:
    """Return True when the content type is suitable for text extraction."""
    media_type = content_type.split(";", 1)[0].strip().lower()
    if not media_type:
        return True

    if any(media_type.startswith(prefix) for prefix in BINARY_CONTENT_TYPE_PREFIXES):
        return False

    return any(
        media_type.startswith(prefix) or media_type == prefix.rstrip("/")
        for prefix in TEXT_CONTENT_TYPE_PREFIXES
    )


async def read_limited_body(resp: aiohttp.ClientResponse, max_bytes: int) -> bytes | None:
    """Read the response body up to max_bytes."""
    chunks: list[bytes] = []
    total = 0

    async for chunk in resp.content.iter_chunked(8192):
        total += len(chunk)
        if total > max_bytes:
            return None
        chunks.append(chunk)

    return b"".join(chunks)


def detect_unfetchable_page(
    html: str,
    *,
    final_url: str,
    title: str,
) -> str | None:
    """Return an error message when static HTML cannot yield readable content."""
    if CONSENT_URL_PATTERN.search(final_url) or CONSENT_TITLE_PATTERN.search(title):
        return "Page requires cookie or privacy consent before content can be fetched"

    if BOT_TITLE_PATTERN.search(title):
        return "Page returned a bot-check or verification screen instead of content"

    if JS_REQUIRED_PATTERN.search(title):
        return "Page requires JavaScript; no readable content was available in the HTML"

    soup = BeautifulSoup(html, "html5lib")
    noscript = soup.find("noscript")
    if noscript:
        noscript_text = normalize_whitespace(noscript.get_text(" ", strip=True))
        if JS_REQUIRED_PATTERN.search(noscript_text):
            return "Page requires JavaScript; no readable content was available in the HTML"

    html_lower = html.lower()
    body = soup.body
    body_text_len = (
        len(normalize_whitespace(body.get_text(" ", strip=True))) if body else 0
    )
    if body_text_len < MIN_MAIN_CONTENT_CHARS and any(
        marker in html_lower for marker in SPA_HTML_MARKERS
    ):
        return "Page appears to load content with JavaScript; no readable content was in the HTML"

    return None


def parse_webpage(
    html: str,
    *,
    base_url: str,
    link_mode: str,
    content_format: str,
    max_chars: int,
) -> tuple[str, str, bool]:
    """Parse HTML into title, content, and truncation flag."""
    soup = BeautifulSoup(html, "html5lib")
    root = find_main_content(soup)
    remove_noise(root)
    title = extract_title(soup, root)

    content = render_content(root, base_url=base_url, link_mode=link_mode, content_format=content_format)
    if not content.strip():
        root = find_main_content(soup)
        remove_tag_noise(root)
        content = render_content(
            root,
            base_url=base_url,
            link_mode=link_mode,
            content_format=content_format,
        )

    content = normalize_whitespace(content)
    truncated = len(content) > max_chars
    if truncated:
        content = content[:max_chars].rstrip()

    return title, content, truncated


def extract_title(soup: BeautifulSoup, root: Tag | None = None) -> str:
    """Extract the page title, preferring on-page headings over site titles."""
    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        return normalize_whitespace(og_title["content"])

    twitter_title = soup.find("meta", attrs={"name": "twitter:title"})
    if twitter_title and twitter_title.get("content"):
        return normalize_whitespace(twitter_title["content"])

    for search_root in (root, soup):
        if not search_root:
            continue
        for tag_name in ("h1", "h2"):
            heading = search_root.find(tag_name)
            if heading:
                text = normalize_whitespace(heading.get_text(" ", strip=True))
                if text:
                    return text

    if soup.title and soup.title.string:
        return normalize_site_title(normalize_whitespace(soup.title.string))

    return ""


def normalize_site_title(title: str) -> str:
    """Prefer the page-specific part of a compound document title."""
    for separator in TITLE_SEPARATORS:
        if separator not in title:
            continue
        first, *rest = title.split(separator)
        if first and len(first) >= MIN_SITE_TITLE_CHARS and rest:
            return first
    return title


def find_main_content(soup: BeautifulSoup) -> Tag:
    """Find the most relevant content container."""
    for selector in ("main", "article", '[role="main"]'):
        element = soup.select_one(selector)
        if element and len(element.get_text(strip=True)) >= MIN_MAIN_CONTENT_CHARS:
            return element

    if not soup.body:
        return soup

    candidates: list[tuple[float, int, Tag]] = []
    for element in soup.body.find_all(["main", "article", "section", "div"]):
        if element.name == "form":
            continue
        score = score_content_element(element)
        if score <= 0:
            continue
        candidates.append((score, element_depth(element), element))

    if not candidates:
        return soup.body

    max_score = max(score for score, _, _ in candidates)
    threshold = max_score * MAIN_CONTENT_SCORE_THRESHOLD
    top_candidates = [
        (score, depth, element)
        for score, depth, element in candidates
        if score >= threshold
    ]
    top_candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    for _, _, element in top_candidates:
        if content_survives_noise(element):
            return element

    if soup.body:
        return soup.body

    return soup


def content_survives_noise(element: Tag) -> bool:
    """Return True when a candidate still has readable text after boilerplate removal."""
    working = clone_tag(element)
    remove_noise(working)
    return len(working.get_text(strip=True)) >= MIN_MAIN_CONTENT_CHARS


def score_content_element(element: Tag) -> float:
    """Score a container by text density and likelihood of being main content."""
    text = element.get_text(" ", strip=True)
    text_length = len(text)
    if text_length < MIN_MAIN_CONTENT_CHARS:
        return 0.0

    link_text_length = sum(
        len(anchor.get_text(" ", strip=True)) for anchor in element.find_all("a")
    )
    link_density = link_text_length / text_length
    score = text_length * (1 - min(link_density * 1.5, 0.9))

    if element.name in {"main", "article"}:
        score *= 1.5
    if element.get("role") == "main":
        score *= 1.5

    classes = " ".join(element.get("class", []))
    element_id = element.get("id", "")
    if class_id_is_noise(classes, element_id):
        score *= 0.05
    if class_id_is_content(classes, element_id):
        score *= 1.3

    return score


def iter_class_id_tokens(classes: str, element_id: str) -> list[str]:
    """Return normalized class and id tokens for heuristic matching."""
    tokens = classes.split()
    if element_id:
        tokens.append(element_id)
    return tokens


def class_id_is_noise(classes: str, element_id: str) -> bool:
    """Return True when class/id tokens indicate boilerplate."""
    for token in iter_class_id_tokens(classes, element_id):
        normalized = token.lower()
        if normalized in NOISE_CLASS_TOKENS:
            return True
        if any(normalized.startswith(prefix) for prefix in NOISE_CLASS_PREFIXES):
            return True
        if any(normalized.endswith(suffix) for suffix in NOISE_CLASS_SUFFIXES):
            return True
    return False


def class_id_is_content(classes: str, element_id: str) -> bool:
    """Return True when class/id tokens indicate primary content."""
    for token in iter_class_id_tokens(classes, element_id):
        normalized = token.lower()
        if normalized in CONTENT_CLASS_TOKENS:
            return True
        if any(normalized.startswith(prefix) for prefix in CONTENT_CLASS_PREFIXES):
            return True
    return False


def render_content(
    root: Tag,
    *,
    base_url: str,
    link_mode: str,
    content_format: str,
) -> str:
    """Render parsed content using the configured format."""
    if content_format == "markdown":
        return render_markdown(root, base_url=base_url, link_mode=link_mode)
    if content_format == "text":
        return render_plain_text(root, base_url=base_url, link_mode=link_mode)
    return render_paragraphs(root, base_url=base_url, link_mode=link_mode)


def element_depth(element: Tag) -> int:
    """Return DOM depth so specific containers beat page wrappers."""
    depth = 0
    parent = element.parent
    while parent and getattr(parent, "name", None):
        depth += 1
        parent = parent.parent
    return depth


def is_noise_element(element: Tag) -> bool:
    """Return True when an element is likely boilerplate."""
    if not isinstance(element, Tag) or not element.name:
        return False

    if element.name in NOISE_TAGS:
        return True

    attrs = element.attrs or {}
    role = (attrs.get("role") or "").lower()
    if role in NOISE_ROLES:
        return True

    classes = " ".join(attrs.get("class", []))
    element_id = attrs.get("id", "")
    if class_id_is_noise(classes, element_id):
        return True

    if attrs.get("aria-hidden") == "true":
        return True

    style = (attrs.get("style") or "").replace(" ", "").lower()
    return "display:none" in style


def is_tag_noise_element(element: Tag) -> bool:
    """Return True when only tag/role attributes indicate boilerplate."""
    if not isinstance(element, Tag) or not element.name:
        return False

    if element.name in NOISE_TAGS:
        return True

    attrs = element.attrs or {}
    role = (attrs.get("role") or "").lower()
    if role in NOISE_ROLES:
        return True

    if attrs.get("aria-hidden") == "true":
        return True

    style = (attrs.get("style") or "").replace(" ", "").lower()
    return "display:none" in style


def remove_noise(root: Tag) -> None:
    """Remove non-content elements from the parsed tree."""
    root_text_length = len(root.get_text(strip=True))
    for element in root.find_all(name=True):
        if not is_noise_element(element):
            continue
        element_text_length = len(element.get_text(strip=True))
        if (
            root_text_length > 0
            and element_text_length / root_text_length > NOISE_SURVIVABILITY_RATIO
        ):
            continue
        element.decompose()


def remove_tag_noise(root: Tag) -> None:
    """Remove only obvious boilerplate tags when class heuristics were too aggressive."""
    for element in root.find_all(name=True):
        if is_tag_noise_element(element):
            element.decompose()


def is_substantive_block(element: Tag, text: str) -> bool:
    """Return True when a block likely contains meaningful page content."""
    if len(text) < MIN_SUBSTANTIVE_BLOCK_CHARS:
        return False
    if text.isdigit() and len(text) <= MAX_CALENDAR_DAY_CHARS:
        return False
    if is_noise_element(element):
        return False

    if element.name in {"td", "th"}:
        return True

    anchors = element.find_all("a")
    if len(anchors) >= MIN_NAV_LINK_COUNT:
        link_text_length = sum(
            len(normalize_whitespace(anchor.get_text(" ", strip=True)))
            for anchor in anchors
        )
        if link_text_length / max(len(text), 1) > MAX_NAV_LINK_TEXT_RATIO:
            return False

    return True


def deduplicate_blocks(blocks: list[str]) -> list[str]:
    """Remove repeated blocks commonly found in menus and footers."""
    seen: set[str] = set()
    unique_blocks: list[str] = []
    for block in blocks:
        key = block.casefold()
        if key in seen:
            continue
        seen.add(key)
        unique_blocks.append(block)
    return unique_blocks


def collect_div_text_blocks(root: Tag) -> list[str]:
    """Collect substantive text from div elements without nested block tags."""
    blocks: list[str] = []
    for element in root.find_all("div"):
        if element.find(BLOCK_TAGS):
            continue

        text = normalize_whitespace(element.get_text(" ", strip=True))
        if len(text) < DIV_BLOCK_MIN_CHARS:
            continue
        if not is_substantive_block(element, text):
            continue
        blocks.append(text)

    return blocks


def collect_text_blocks(root: Tag) -> list[str]:
    """Collect meaningful text blocks from a content root."""
    blocks: list[str] = []
    for element in root.find_all(BLOCK_TAGS):
        if element.find_parent(BLOCK_TAGS) and element.name in {"li", "td", "th", "dd"}:
            continue

        text = normalize_whitespace(element.get_text(" ", strip=True))
        if not is_substantive_block(element, text):
            continue
        blocks.append(text)

    if not blocks:
        blocks = collect_div_text_blocks(root)

    return deduplicate_blocks(blocks)


def render_paragraphs(root: Tag, *, base_url: str, link_mode: str) -> str:
    """Render compact paragraph text."""
    working_root = clone_tag(root)
    references: list[tuple[str, str]] = []
    if link_mode != "none":
        transform_links(working_root, base_url=base_url, link_mode=link_mode, references=references)

    blocks = collect_text_blocks(working_root)
    if not blocks:
        text = normalize_whitespace(working_root.get_text(" ", strip=True))
        if len(text) >= MIN_MAIN_CONTENT_CHARS:
            blocks = [text]

    content = "\n\n".join(block for block in blocks if block)
    return append_references(content, references, link_mode)


def render_markdown(root: Tag, *, base_url: str, link_mode: str) -> str:
    """Render markdown-ish text preserving basic structure."""
    working_root = clone_tag(root)
    references: list[tuple[str, str]] = []
    if link_mode != "none":
        transform_links(working_root, base_url=base_url, link_mode=link_mode, references=references)

    blocks: list[str] = []
    for element in working_root.find_all(BLOCK_TAGS):
        if element.find_parent(BLOCK_TAGS) and element.name in {"li", "td", "th", "dd"}:
            continue

        text = normalize_whitespace(element.get_text(" ", strip=True))
        if not is_substantive_block(element, text):
            continue

        if element.name in HEADING_PREFIXES:
            blocks.append(f"{HEADING_PREFIXES[element.name]}{text}")
        elif element.name == "li":
            blocks.append(f"- {text}")
        elif element.name == "blockquote":
            blocks.append(f"> {text}")
        else:
            blocks.append(text)

    if not blocks:
        blocks = collect_div_text_blocks(working_root)

    blocks = deduplicate_blocks(blocks)
    if not blocks:
        text = normalize_whitespace(working_root.get_text(" ", strip=True))
        if len(text) >= MIN_MAIN_CONTENT_CHARS:
            blocks = [text]

    content = "\n\n".join(block for block in blocks if block)
    return append_references(content, references, link_mode)


def render_plain_text(root: Tag, *, base_url: str, link_mode: str) -> str:
    """Render minimal cleaned text."""
    working_root = clone_tag(root)
    references: list[tuple[str, str]] = []
    if link_mode != "none":
        transform_links(working_root, base_url=base_url, link_mode=link_mode, references=references)
    content = normalize_whitespace(working_root.get_text(" ", strip=True))
    return append_references(content, references, link_mode)


def clone_tag(root: Tag) -> Tag:
    """Return a detached copy of the root tag."""
    return BeautifulSoup(str(root), "html5lib").find(root.name) or root


def transform_links(
    root: Tag,
    *,
    base_url: str,
    link_mode: str,
    references: list[tuple[str, str]],
) -> None:
    """Replace anchor tags with formatted link text."""
    for anchor in root.find_all("a"):
        href = anchor.get("href")
        link_text = normalize_whitespace(anchor.get_text(" ", strip=True))
        if href and link_text:
            replacement = format_link(
                link_text,
                urljoin(base_url, href),
                link_mode,
                references,
            )
            anchor.replace_with(replacement)
        elif link_text:
            anchor.replace_with(link_text)
        else:
            anchor.decompose()


def format_link(
    text: str,
    href: str,
    link_mode: str,
    references: list[tuple[str, str]],
) -> str:
    """Format a hyperlink according to the configured link mode."""
    if link_mode == "inline":
        return f"{text} ({href})"

    if link_mode == "text":
        return text

    reference_number = get_reference_number(href, references)
    return f"{text} [{reference_number}]"


def get_reference_number(href: str, references: list[tuple[str, str]]) -> int:
    """Return a stable reference number for a URL."""
    for index, (_, existing_href) in enumerate(references, start=1):
        if existing_href == href:
            return index

    references.append(("", href))
    return len(references)


def append_references(
    content: str,
    references: list[tuple[str, str]],
    link_mode: str,
) -> str:
    """Append numbered references when using reference link mode."""
    if link_mode != "references" or not references:
        return content

    lines = [content, "", "References:"]
    for index, (_, href) in enumerate(references, start=1):
        lines.append(f"[{index}] {href}")

    return "\n".join(lines)


def normalize_whitespace(text: str) -> str:
    """Collapse repeated whitespace."""
    return re.sub(r"\s+", " ", text).strip()
