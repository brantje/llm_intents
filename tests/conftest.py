"""Test configuration for LLM Intents integration."""

from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_socket
from homeassistant.core import HomeAssistant

from custom_components.llm_intents.const import DOMAIN


@pytest.hookimpl(trylast=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    """Re-enable sockets for live tests after HA's setup hook disables them."""
    if item.get_closest_marker("enable_socket"):
        pytest_socket.enable_socket()


@pytest.fixture
def mock_hass() -> HomeAssistant:
    """Mock HomeAssistant instance for unit tests that do not boot HA."""
    hass = MagicMock(spec=HomeAssistant)
    hass.async_create_task = AsyncMock()
    hass.data = {DOMAIN: {"config": {}}}
    return hass
