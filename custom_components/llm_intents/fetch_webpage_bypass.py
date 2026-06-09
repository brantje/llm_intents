"""HTTP-only bypass strategies for cookie/consent and bot-check gates."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from http import HTTPStatus
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import aiohttp
from bs4 import BeautifulSoup

from .const import (
    CONF_FETCH_WEBPAGE_BYPASS_GATES,
    CONF_FETCH_WEBPAGE_CONTENT_FORMAT,
    CONF_FETCH_WEBPAGE_LINK_MODE,
    CONF_FETCH_WEBPAGE_MAX_BYTES,
    CONF_FETCH_WEBPAGE_MAX_CHARS,
    CONF_FETCH_WEBPAGE_TIMEOUT,
    CONF_FETCH_WEBPAGE_USER_AGENT,
    DEFAULT_FETCH_WEBPAGE_MAX_BYTES,
    DEFAULT_FETCH_WEBPAGE_TIMEOUT,
    SERVICE_DEFAULTS,
)

if TYPE_CHECKING:
    from homeassistant.util.json import JsonObjectType

from .fetch_webpage import (
    BOT_TITLE_PATTERN,
    CONSENT_TITLE_PATTERN,
    CONSENT_URL_PATTERN,
    JS_REQUIRED_PATTERN,
    MIN_MAIN_CONTENT_CHARS,
    SPA_HTML_MARKERS,
    detect_unfetchable_page,
    is_text_content_type,
    parse_webpage,
    read_limited_body,
)

_LOGGER = logging.getLogger(__name__)

MAX_BYPASS_REQUESTS = 3
MAX_TOTAL_REQUESTS = MAX_BYPASS_REQUESTS + 1
RETRYABLE_HTTP_STATUSES = {
    HTTPStatus.UNAUTHORIZED,
    HTTPStatus.FORBIDDEN,
    HTTPStatus.ACCEPTED,
    HTTPStatus.SERVICE_UNAVAILABLE,
}

DEFAULT_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

CONSENT_URL_PARAMS = (
    "callbackUrl",
    "returnUrl",
    "redirect",
    "url",
    "next",
    "destination",
)

CONSENT_FORM_KEYWORDS = (
    "accept",
    "agree",
    "consent",
    "continue",
    "allow",
    "ok",
)

GENERIC_CONSENT_COOKIES = {
    "OptanonAlertBoxClosed": "2024-01-01T00:00:00.000Z",
    "OptanonConsent": (
        "isGpcEnabled=0&datestamp=Mon+Jan+01+2024+00%3A00%3A00+GMT%2B0000"
        "&version=202401.1.0&groups=C0001%3A1%2CC0002%3A1%2CC0003%3A1%2CC0004%3A1"
    ),
    "CookieConsent": (
        '{"stamp":"fetch-webpage","necessary":true,"preferences":true,'
        '"statistics":true,"marketing":true}'
    ),
    "cookieconsent_status": "dismiss",
}

DPG_CONSENT_COOKIES = {
    "pg_cookie_consent": "true",
    "authId": "fetch-webpage-consent",
}

DOMAIN_CONSENT_COOKIES: dict[str, dict[str, str]] = {
    "dpgmedia.nl": DPG_CONSENT_COOKIES,
    "dpgmedia.be": DPG_CONSENT_COOKIES,
    "myprivacy.dpgmedia.nl": DPG_CONSENT_COOKIES,
    "myprivacy.dpgmedia.be": DPG_CONSENT_COOKIES,
    "nu.nl": DPG_CONSENT_COOKIES,
    "hln.be": DPG_CONSENT_COOKIES,
}


class GateType(StrEnum):
    """Classification of unfetchable page gates."""

    NONE = "none"
    CONSENT = "consent"
    BOT_CHECK = "bot_check"
    JS_REQUIRED = "js_required"


@dataclass
class FetchHtmlResult:
    """Successful HTML fetch result."""

    html: str
    final_url: str
    http_status: int
    request_count: int = 1
    bypass_recovered: bool = False


@dataclass
class FetchWebpageConfig:
    """Fetch webpage runtime configuration."""

    timeout: int
    max_bytes: int
    max_chars: int
    link_mode: str
    content_format: str
    user_agent: str
    bypass_gates: bool = True


@dataclass
class ParsedPageState:
    """Parsed page content and gate metadata."""

    title: str
    content: str
    truncated: bool
    gate: GateType
    gate_message: str | None = None


@dataclass
class BypassContext:
    """Mutable state while attempting gate bypass."""

    cookies: dict[str, str] = field(default_factory=dict)
    request_count: int = 0
    bypass_recovered: bool = False
    initial_gate: GateType = GateType.NONE


def config_from_mapping(config_data: dict) -> FetchWebpageConfig:
    """Build fetch config from Home Assistant config/options mapping."""
    return FetchWebpageConfig(
        timeout=int(
            config_data.get(
                CONF_FETCH_WEBPAGE_TIMEOUT,
                SERVICE_DEFAULTS.get(
                    CONF_FETCH_WEBPAGE_TIMEOUT,
                    DEFAULT_FETCH_WEBPAGE_TIMEOUT,
                ),
            ),
        ),
        max_bytes=int(
            config_data.get(
                CONF_FETCH_WEBPAGE_MAX_BYTES,
                SERVICE_DEFAULTS.get(
                    CONF_FETCH_WEBPAGE_MAX_BYTES,
                    DEFAULT_FETCH_WEBPAGE_MAX_BYTES,
                ),
            ),
        ),
        max_chars=int(
            config_data.get(
                CONF_FETCH_WEBPAGE_MAX_CHARS,
                SERVICE_DEFAULTS.get(CONF_FETCH_WEBPAGE_MAX_CHARS, 16000),
            ),
        ),
        link_mode=config_data.get(
            CONF_FETCH_WEBPAGE_LINK_MODE,
            SERVICE_DEFAULTS.get(CONF_FETCH_WEBPAGE_LINK_MODE, "references"),
        ),
        content_format=config_data.get(
            CONF_FETCH_WEBPAGE_CONTENT_FORMAT,
            SERVICE_DEFAULTS.get(CONF_FETCH_WEBPAGE_CONTENT_FORMAT, "paragraphs"),
        ),
        user_agent=config_data.get(
            CONF_FETCH_WEBPAGE_USER_AGENT,
            SERVICE_DEFAULTS.get(
                CONF_FETCH_WEBPAGE_USER_AGENT,
                DEFAULT_BROWSER_USER_AGENT,
            ),
        ),
        bypass_gates=bool(
            config_data.get(
                CONF_FETCH_WEBPAGE_BYPASS_GATES,
                SERVICE_DEFAULTS.get(CONF_FETCH_WEBPAGE_BYPASS_GATES, True),
            ),
        ),
    )


def build_fetch_headers(
    user_agent: str,
    *,
    browser_mode: bool = False,
    cookies: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build HTTP request headers for webpage fetching."""
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8",
    }
    if browser_mode:
        headers.update(
            {
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate",
                "Cache-Control": "no-cache",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
            },
        )
    if cookies:
        headers["Cookie"] = cookies_to_header(cookies)
    return headers


def cookies_to_header(cookies: dict[str, str]) -> str:
    """Serialize cookies for a Cookie request header."""
    return "; ".join(f"{name}={value}" for name, value in cookies.items())


def classify_gate(html: str, *, final_url: str, title: str) -> GateType:
    """Classify the type of gate blocking page content."""
    if CONSENT_URL_PATTERN.search(final_url) or CONSENT_TITLE_PATTERN.search(title):
        return GateType.CONSENT
    if BOT_TITLE_PATTERN.search(title):
        return GateType.BOT_CHECK
    if JS_REQUIRED_PATTERN.search(title):
        return GateType.JS_REQUIRED

    soup = BeautifulSoup(html, "html5lib")
    noscript = soup.find("noscript")
    if noscript:
        noscript_text = noscript.get_text(" ", strip=True)
        if JS_REQUIRED_PATTERN.search(noscript_text):
            return GateType.JS_REQUIRED

    html_lower = html.lower()
    body = soup.body
    body_text_len = len(body.get_text(" ", strip=True)) if body else 0
    if body_text_len < MIN_MAIN_CONTENT_CHARS and any(
        marker in html_lower for marker in SPA_HTML_MARKERS
    ):
        return GateType.JS_REQUIRED

    return GateType.NONE


def parse_page_state(
    html: str,
    *,
    final_url: str,
    config: FetchWebpageConfig,
) -> ParsedPageState:
    """Parse HTML and determine whether content is usable."""
    title, content, truncated = parse_webpage(
        html,
        base_url=final_url,
        link_mode=config.link_mode,
        content_format=config.content_format,
        max_chars=config.max_chars,
    )
    content_len = len(content.strip())
    gate = GateType.NONE
    gate_message = None

    if content_len <= MIN_MAIN_CONTENT_CHARS:
        gate_message = detect_unfetchable_page(
            html,
            final_url=final_url,
            title=title,
        )
        if gate_message:
            gate = classify_gate(html, final_url=final_url, title=title)

    return ParsedPageState(
        title=title,
        content=content,
        truncated=truncated,
        gate=gate,
        gate_message=gate_message,
    )


def content_is_usable(state: ParsedPageState) -> bool:
    """Return True when parsed content is sufficient."""
    return len(state.content.strip()) > MIN_MAIN_CONTENT_CHARS


def merge_cookies_for_url(url: str, existing: dict[str, str] | None = None) -> dict[str, str]:
    """Merge generic and domain-specific consent cookies for a URL."""
    merged = dict(GENERIC_CONSENT_COOKIES)
    if existing:
        merged.update(existing)

    hostname = urlparse(url).hostname or ""
    hostname = hostname.lower()
    for suffix, domain_cookies in DOMAIN_CONSENT_COOKIES.items():
        if hostname == suffix or hostname.endswith(f".{suffix}"):
            merged.update(domain_cookies)
    return merged


def extract_consent_target_url(consent_url: str) -> str | None:
    """Extract the original target URL from a consent redirect URL."""
    query = parse_qs(urlparse(consent_url).query)
    for param in CONSENT_URL_PARAMS:
        values = query.get(param)
        if values and values[0]:
            return values[0]
    return None


def same_origin(url: str, action: str) -> bool:
    """Return True when action resolves to the same origin as url."""
    base = urlparse(url)
    target = urlparse(urljoin(url, action))
    return (
        base.scheme == target.scheme
        and base.hostname == target.hostname
        and base.port == target.port
    )


def extract_consent_form(html: str, *, page_url: str) -> tuple[str, dict[str, str]] | None:
    """Find a consent acceptance form on a gate page."""
    soup = BeautifulSoup(html, "html5lib")
    for form in soup.find_all("form"):
        action = form.get("action") or page_url
        if not same_origin(page_url, action):
            continue

        submit_name = None
        submit_value = None
        for button in form.find_all(["button", "input"]):
            button_type = (button.get("type") or "submit").lower()
            if button_type not in {"submit", "button"}:
                continue
            label = " ".join(
                filter(
                    None,
                    [
                        button.get("value", ""),
                        button.get_text(" ", strip=True),
                        button.get("name", ""),
                    ],
                ),
            ).lower()
            if any(keyword in label for keyword in CONSENT_FORM_KEYWORDS):
                submit_name = button.get("name")
                submit_value = button.get("value") or "1"
                break

        if submit_name is None:
            continue

        fields: dict[str, str] = {}
        for element in form.find_all(["input", "textarea", "select"]):
            name = element.get("name")
            if not name or name == submit_name:
                continue
            input_type = (element.get("type") or "text").lower()
            if input_type in {"submit", "button", "image"}:
                continue
            fields[name] = element.get("value") or ""

        fields[submit_name] = submit_value or "1"
        return urljoin(page_url, action), fields

    return None


async def fetch_html(
    session: aiohttp.ClientSession,
    url: str,
    *,
    headers: dict[str, str],
    timeout_seconds: int,
    max_bytes: int,
) -> FetchHtmlResult | JsonObjectType:
    """Fetch a single URL and return HTML or an error payload."""
    client_timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    async with session.get(
        url,
        headers=headers,
        timeout=client_timeout,
        allow_redirects=True,
    ) as resp:
        http_status = resp.status
        final_url = str(resp.url)

        if http_status != HTTPStatus.OK:
            return {
                "error": f"Fetch webpage received HTTP {http_status} from {url}",
                "http_status": http_status,
                "final_url": final_url,
            }

        content_type = resp.headers.get("Content-Type", "")
        if not is_text_content_type(content_type):
            return {
                "error": (
                    "Unsupported content type for webpage fetch: "
                    f"{content_type.split(';', 1)[0] or 'unknown'}"
                ),
            }

        body = await read_limited_body(resp, max_bytes)
        if body is None:
            return {"error": f"Response exceeded maximum size of {max_bytes} bytes"}

        charset = resp.charset or "utf-8"
        html = body.decode(charset, errors="replace")
        return FetchHtmlResult(html=html, final_url=final_url, http_status=http_status)


async def post_form_html(  # noqa: PLR0913
    session: aiohttp.ClientSession,
    url: str,
    fields: dict[str, str],
    *,
    headers: dict[str, str],
    timeout_seconds: int,
    max_bytes: int,
) -> FetchHtmlResult | JsonObjectType:
    """POST a consent form and return the resulting HTML."""
    client_timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    async with session.post(
        url,
        headers={
            **headers,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data=urlencode(fields),
        timeout=client_timeout,
        allow_redirects=True,
    ) as resp:
        http_status = resp.status
        final_url = str(resp.url)
        if http_status != HTTPStatus.OK:
            return {
                "error": f"Fetch webpage received HTTP {http_status} from {url}",
                "http_status": http_status,
                "final_url": final_url,
            }

        content_type = resp.headers.get("Content-Type", "")
        if not is_text_content_type(content_type):
            return {
                "error": (
                    "Unsupported content type for webpage fetch: "
                    f"{content_type.split(';', 1)[0] or 'unknown'}"
                ),
            }

        body = await read_limited_body(resp, max_bytes)
        if body is None:
            return {"error": f"Response exceeded maximum size of {max_bytes} bytes"}

        charset = resp.charset or "utf-8"
        html = body.decode(charset, errors="replace")
        return FetchHtmlResult(html=html, final_url=final_url, http_status=http_status)


def gate_error_payload(
    state: ParsedPageState,
    *,
    final_url: str,
) -> JsonObjectType:
    """Build an error response for a blocked page."""
    if state.gate_message:
        payload: JsonObjectType = {"error": state.gate_message, "url": final_url}
        if state.gate != GateType.NONE:
            payload["reason"] = state.gate.value
        return payload
    if not state.content.strip():
        return {
            "error": "No readable content could be extracted from the page",
            "url": final_url,
        }
    return {}


async def attempt_gate_bypass(  # noqa: PLR0913
    session: aiohttp.ClientSession,
    original_url: str,
    *,
    config: FetchWebpageConfig,
    context: BypassContext,
    current_html: str,
    current_final_url: str,
    gate: GateType,
) -> FetchHtmlResult | None:
    """Try HTTP bypass strategies until content recovers or the cap is hit."""
    remaining = MAX_TOTAL_REQUESTS - context.request_count
    if remaining <= 0:
        return None

    strategies: list[str] = []
    if gate in {GateType.BOT_CHECK, GateType.CONSENT}:
        strategies.append("browser_headers")
    if gate == GateType.CONSENT:
        strategies.extend(["consent_cookies", "consent_url", "consent_form"])

    for strategy in strategies:
        if context.request_count >= MAX_TOTAL_REQUESTS:
            break

        if strategy == "browser_headers":
            _LOGGER.info("Fetch webpage bypass: browser headers retry for %s", original_url)
            headers = build_fetch_headers(
                DEFAULT_BROWSER_USER_AGENT,
                browser_mode=True,
                cookies=context.cookies or None,
            )
            result = await fetch_html(
                session,
                original_url,
                headers=headers,
                timeout_seconds=config.timeout,
                max_bytes=config.max_bytes,
            )
            context.request_count += 1
            if isinstance(result, dict):
                continue
            state = parse_page_state(
                result.html,
                final_url=result.final_url,
                config=config,
            )
            if content_is_usable(state):
                result.request_count = context.request_count
                result.bypass_recovered = True
                context.bypass_recovered = True
                return result

        elif strategy == "consent_cookies":
            _LOGGER.info("Fetch webpage bypass: consent cookies for %s", original_url)
            context.cookies = merge_cookies_for_url(original_url, context.cookies)
            headers = build_fetch_headers(
                config.user_agent,
                browser_mode=True,
                cookies=context.cookies,
            )
            result = await fetch_html(
                session,
                original_url,
                headers=headers,
                timeout_seconds=config.timeout,
                max_bytes=config.max_bytes,
            )
            context.request_count += 1
            if isinstance(result, dict):
                continue
            state = parse_page_state(
                result.html,
                final_url=result.final_url,
                config=config,
            )
            if content_is_usable(state):
                result.request_count = context.request_count
                result.bypass_recovered = True
                context.bypass_recovered = True
                return result

        elif strategy == "consent_url":
            target_url = extract_consent_target_url(current_final_url) or original_url
            if target_url == current_final_url and gate == GateType.CONSENT:
                target_url = original_url
            _LOGGER.info(
                "Fetch webpage bypass: consent URL recovery for %s via %s",
                original_url,
                target_url,
            )
            context.cookies = merge_cookies_for_url(target_url, context.cookies)
            headers = build_fetch_headers(
                config.user_agent,
                browser_mode=True,
                cookies=context.cookies,
            )
            result = await fetch_html(
                session,
                target_url,
                headers=headers,
                timeout_seconds=config.timeout,
                max_bytes=config.max_bytes,
            )
            context.request_count += 1
            if isinstance(result, dict):
                continue
            state = parse_page_state(
                result.html,
                final_url=result.final_url,
                config=config,
            )
            if content_is_usable(state):
                result.request_count = context.request_count
                result.bypass_recovered = True
                context.bypass_recovered = True
                return result
            current_html = result.html
            current_final_url = result.final_url

        elif strategy == "consent_form":
            form = extract_consent_form(current_html, page_url=current_final_url)
            if not form:
                continue
            action, fields = form
            _LOGGER.info("Fetch webpage bypass: consent form POST to %s", action)
            headers = build_fetch_headers(
                config.user_agent,
                browser_mode=True,
                cookies=context.cookies or None,
            )
            result = await post_form_html(
                session,
                action,
                fields,
                headers=headers,
                timeout_seconds=config.timeout,
                max_bytes=config.max_bytes,
            )
            context.request_count += 1
            if isinstance(result, dict):
                continue
            state = parse_page_state(
                result.html,
                final_url=result.final_url,
                config=config,
            )
            if content_is_usable(state):
                result.request_count = context.request_count
                result.bypass_recovered = True
                context.bypass_recovered = True
                return result

    return None


async def fetch_with_gate_bypass(  # noqa: PLR0911
    session: aiohttp.ClientSession,
    url: str,
    *,
    config: FetchWebpageConfig,
) -> FetchHtmlResult | JsonObjectType:
    """Fetch a webpage, attempting HTTP gate bypass when needed."""
    context = BypassContext()
    headers = build_fetch_headers(config.user_agent)

    initial = await fetch_html(
        session,
        url,
        headers=headers,
        timeout_seconds=config.timeout,
        max_bytes=config.max_bytes,
    )
    context.request_count = 1

    if isinstance(initial, dict):
        http_status = initial.get("http_status")
        if (
            config.bypass_gates
            and isinstance(http_status, int)
            and http_status in RETRYABLE_HTTP_STATUSES
            and context.request_count < MAX_TOTAL_REQUESTS
        ):
            _LOGGER.info(
                "Fetch webpage bypass: browser headers retry after HTTP %s for %s",
                http_status,
                url,
            )
            retry_headers = build_fetch_headers(
                DEFAULT_BROWSER_USER_AGENT,
                browser_mode=True,
            )
            retry = await fetch_html(
                session,
                url,
                headers=retry_headers,
                timeout_seconds=config.timeout,
                max_bytes=config.max_bytes,
            )
            context.request_count += 1
            if isinstance(retry, FetchHtmlResult):
                initial = retry
            else:
                return initial
        else:
            return initial

    state = parse_page_state(
        initial.html,
        final_url=initial.final_url,
        config=config,
    )
    if content_is_usable(state):
        initial.request_count = context.request_count
        return initial

    if state.gate == GateType.NONE:
        error = gate_error_payload(state, final_url=initial.final_url)
        return error or initial

    context.initial_gate = state.gate
    if not config.bypass_gates:
        error = gate_error_payload(state, final_url=initial.final_url)
        return error or initial

    bypass_result = await attempt_gate_bypass(
        session,
        url,
        config=config,
        context=context,
        current_html=initial.html,
        current_final_url=initial.final_url,
        gate=state.gate,
    )
    if bypass_result is not None:
        bypass_result.request_count = context.request_count
        return bypass_result

    error = gate_error_payload(state, final_url=initial.final_url)
    return error or initial
