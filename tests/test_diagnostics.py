"""Tests for the IEEE 2030.5 Smart Meter diagnostics."""

from dataclasses import replace
from unittest.mock import AsyncMock

from aio2030dot5 import MeterProfile
from homeassistant.components.diagnostics import REDACTED
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator
from syrupy.assertion import SnapshotAssertion

from . import MOCK_HOST, MOCK_LFDI, MOCK_SFDI, setup_integration


@pytest.mark.usefixtures("mock_meter")
async def test_diagnostics(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    mock_config_entry: MockConfigEntry,
    snapshot: SnapshotAssertion,
) -> None:
    """Diagnostics carry no identifiers, address or key material."""
    await setup_integration(hass, mock_config_entry)
    result = await get_diagnostics_for_config_entry(
        hass, hass_client, mock_config_entry
    )
    text = str(result)
    assert MOCK_LFDI not in text
    assert MOCK_SFDI not in text
    assert MOCK_HOST not in text
    assert "PRIVATE KEY" not in text
    assert "CERTIFICATE" not in text
    assert result == snapshot


async def test_diagnostics_redact_the_serial_number(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    mock_meter: AsyncMock,
    mock_config_entry: MockConfigEntry,
    profile: MeterProfile,
) -> None:
    """A meter that reports its serial number does not have it shared."""
    mock_meter.discover.return_value = replace(profile, serial_number="SN-4711")
    await setup_integration(hass, mock_config_entry)
    result = await get_diagnostics_for_config_entry(
        hass, hass_client, mock_config_entry
    )
    assert result["profile"]["serial_number"] == REDACTED
    assert "SN-4711" not in str(result)
