"""Without the recorder, the meter's interval history is not read at all."""

from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from . import setup_integration


async def test_no_recorder_no_import(
    hass: HomeAssistant,
    mock_meter: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Without the recorder there is nowhere to import to."""
    await setup_integration(hass, mock_config_entry)
    await hass.async_block_till_done(wait_background_tasks=True)
    mock_meter.list_interval_sets.assert_not_awaited()
