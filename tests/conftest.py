"""Fixtures for the IEEE 2030.5 Smart Meter tests."""

from collections.abc import Callable, Generator
from unittest.mock import AsyncMock, PropertyMock, patch

from aio2030dot5 import Identity, MeterProfile, MeterSnapshot, TransportStats
from aiohttp.test_utils import TestClient
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    load_json_object_fixture,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.ieee_2030_5.const import CONF_REGISTERED

from . import make_meter, meter_entry, route


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load the integration from custom_components/ in every test."""


@pytest.fixture
def entity_registry_enabled_by_default() -> Generator[None]:
    """Create every entity enabled, including those disabled by default."""
    with patch(
        "homeassistant.helpers.entity.Entity.entity_registry_enabled_default",
        return_value=True,
        new_callable=PropertyMock,
    ):
        yield


@pytest.fixture
def identity() -> Identity:
    """The client identity: a test-only certificate, never registered anywhere."""
    return Identity.from_dict(load_json_object_fixture("identity.json"))


@pytest.fixture
def profile() -> MeterProfile:
    """The meter's resource tree (an Itron Gen5 Riva, anonymised)."""
    return MeterProfile.from_dict(load_json_object_fixture("profile.json"))


@pytest.fixture
def snapshot_data() -> MeterSnapshot:
    """A reading of every channel."""
    return MeterSnapshot.from_dict(load_json_object_fixture("snapshot.json"))


@pytest.fixture
def mock_meter_create() -> Generator[AsyncMock]:
    """Meter.create(), wherever the integration calls it."""
    with patch("aio2030dot5.Meter.create", new_callable=AsyncMock) as create:
        yield create


@pytest.fixture
def mock_meter(
    mock_meter_create: AsyncMock, profile: MeterProfile, snapshot_data: MeterSnapshot
) -> AsyncMock:
    """The meter, reached with the configured certificate: Meter.create() returns it."""
    meter = make_meter(profile, snapshot_data)
    meter.stats = TransportStats(requests=10, connections=2, resumed_connections=1)
    mock_meter_create.return_value = meter
    return meter


@pytest.fixture
def renewed_meter(profile: MeterProfile, snapshot_data: MeterSnapshot) -> AsyncMock:
    """The same meter, reached with a renewed certificate: refused at first."""
    return make_meter(profile, snapshot_data, accepted=False)


@pytest.fixture
def meters(
    mock_meter_create: AsyncMock, mock_meter: AsyncMock, renewed_meter: AsyncMock
) -> Callable[[Identity], None]:
    """Route Meter.create() by certificate: `meters(new)` names the renewed one."""

    def renew(new: Identity) -> None:
        route(mock_meter_create, mock_meter, {new.lfdi: renewed_meter})

    return renew


@pytest.fixture
def mock_setup_entry() -> Generator[AsyncMock]:
    """Override async_setup_entry."""
    with patch(
        "custom_components.ieee_2030_5.async_setup_entry", return_value=True
    ) as mock_setup:
        yield mock_setup


@pytest.fixture
def mock_config_entry(identity: Identity) -> MockConfigEntry:
    """A configured, registered meter."""
    return meter_entry(identity)


@pytest.fixture
def unregistered_entry(identity: Identity) -> MockConfigEntry:
    """An entry whose certificate the utility has not registered yet."""
    return meter_entry(identity, **{CONF_REGISTERED: False})


@pytest.fixture
async def repairs_client(
    hass: HomeAssistant, hass_client: ClientSessionGenerator
) -> TestClient:
    """An HTTP client for the repairs integration's fix flows."""
    assert await async_setup_component(hass, "repairs", {})
    return await hass_client()
