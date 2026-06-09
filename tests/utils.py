"""
Common test utilities for LLM intents tests.

This module provides helper classes and functions used by tests but does not
contain test cases itself.
"""

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, Mock


class MockContext:
    """
    Mock async context manager for HTTP responses.

    Used to simulate aiohttp's async with statement pattern for HTTP requests.
    """

    def __init__(self, response: AsyncMock) -> None:
        """Initialize with a response object."""
        self.response = response

    async def __aenter__(self) -> AsyncMock:
        """Return the response when entering the context."""
        return self.response

    async def __aexit__(self, *args: object) -> None:
        """Clean up when exiting the context."""


def mock_session(status: int, data: dict) -> AsyncMock:
    """Create a mock HTTP session."""
    session = AsyncMock()

    def mock_get(*args: object, **kwargs: Any) -> MockContext:
        return MockContext(mock_response(status, data))

    session.get = Mock(side_effect=mock_get)
    return session


def mock_response(status: int, data: dict) -> AsyncMock:
    """Create a mock HTTP response."""
    response = AsyncMock()
    response.status = status
    response.json = AsyncMock(return_value=data)
    return response


class MockResponseContent:
    """Mock aiohttp response content stream."""

    def __init__(self, body: bytes) -> None:
        """Initialize with response body bytes."""
        self._body = body

    async def iter_chunked(self, chunk_size: int) -> AsyncIterator[bytes]:
        """Yield the full body as a single chunk."""
        yield self._body


def mock_html_response(
    status: int,
    text: str,
    *,
    content_type: str = "text/html; charset=utf-8",
    final_url: str = "https://example.com/page",
    charset: str = "utf-8",
) -> AsyncMock:
    """Create a mock HTTP response for HTML/text fetches."""
    response = AsyncMock()
    response.status = status
    response.headers = {"Content-Type": content_type}
    response.url = final_url
    response.charset = charset
    response.content = MockResponseContent(text.encode(charset))
    return response


def mock_html_session(
    status: int,
    text: str,
    *,
    content_type: str = "text/html; charset=utf-8",
    final_url: str = "https://example.com/page",
    charset: str = "utf-8",
) -> AsyncMock:
    """Create a mock HTTP session for HTML/text fetches."""
    session = AsyncMock()

    def mock_get(*args: object, **kwargs: Any) -> MockContext:
        return MockContext(
            mock_html_response(
                status,
                text,
                content_type=content_type,
                final_url=final_url,
                charset=charset,
            ),
        )

    session.get = Mock(side_effect=mock_get)
    return session


def mock_html_session_sequence(
    responses: list[dict[str, object]],
) -> AsyncMock:
    """Create a mock session that returns a sequence of HTML responses."""
    session = AsyncMock()
    call_index = {"value": 0}

    def next_response(*args: object, **kwargs: object) -> MockContext:
        index = min(call_index["value"], len(responses) - 1)
        call_index["value"] += 1
        spec = responses[index]
        return MockContext(
            mock_html_response(
                int(spec.get("status", 200)),  # type: ignore[arg-type]
                str(spec.get("text", "")),
                content_type=str(spec.get("content_type", "text/html; charset=utf-8")),
                final_url=str(spec.get("final_url", "https://example.com/page")),
                charset=str(spec.get("charset", "utf-8")),
            ),
        )

    session.get = Mock(side_effect=next_response)
    session.post = Mock(side_effect=next_response)
    session.request_count = call_index
    return session
