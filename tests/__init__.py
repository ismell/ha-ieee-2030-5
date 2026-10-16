"""Tests for the IEEE 2030.5 Smart Meter integration."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

from aio2030dot5 import (
    Identity,
    Meter,
    MeterProfile,
    MeterSnapshot,
    PeerCertificate,
    PeerInfo,
    TouRegister,
    TransportStats,
)
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.ieee_2030_5.const import (
    CONF_DCAP_PATH,
    CONF_IDENTITY,
    CONF_METER_LFDI,
    CONF_METER_SFDI,
    CONF_PENDING_IDENTITY,
    CONF_REGISTERED,
    DOMAIN,
    SCAN_INTERVAL,
    SLOW_INTERVAL,
    TOU_DELIVERED,
)

MOCK_HOST = "192.0.2.19"
MOCK_PORT = 8081
MOCK_SFDI = "651057190448"
MOCK_LFDI = "F289A6F0493D756E057A2B5B59538112697B5A15"

# What the integration keeps in its Store, per meter.
STORE_KEY = f"{DOMAIN}.{MOCK_SFDI}"
# Every SLOW_POLLS-th poll reads every channel and checks a pending renewal.
SLOW_POLLS = int(SLOW_INTERVAL / SCAN_INTERVAL)
# A stored certificate that cannot be loaded.
BROKEN_IDENTITY = {"cert_pem": "x", "key_pem": "y"}

POWER = "sensor.electric_meter_power"
IMPORTED = "sensor.electric_meter_energy_imported"
TIER_1 = "sensor.electric_meter_energy_imported_tier_1"
TIER_4 = "sensor.electric_meter_energy_imported_tier_4"
PEAK = "sensor.electric_meter_peak_demand_imported"


def peer(
    lfdi: str = MOCK_LFDI, sfdi: str = MOCK_SFDI, *, accepted: bool = True
) -> PeerInfo:
    """What probe_tls() reports."""
    return PeerInfo(
        PeerCertificate(der=b"\x30", chain=(b"\x30",), lfdi=lfdi, sfdi=sfdi),
        client_accepted=accepted,
        auth_reason=None if accepted else "certificate_rejected",
    )


def make_meter(
    profile: MeterProfile, snapshot: MeterSnapshot, *, accepted: bool = True
) -> AsyncMock:
    """The meter, as reached with one client certificate."""
    meter = AsyncMock(spec=Meter)
    meter.host = MOCK_HOST
    meter.port = MOCK_PORT
    meter.stats = TransportStats()
    meter.probe_tls.return_value = peer(accepted=accepted)
    meter.discover.return_value = profile
    meter.read.return_value = snapshot
    return meter


def route(create: AsyncMock, default: AsyncMock, by_lfdi: dict[str, AsyncMock]) -> None:
    """Meter.create() returns the meter for the certificate it is given."""

    def _create(host: str, identity: Identity, *args: Any, **kwargs: Any) -> Any:
        return by_lfdi.get(identity.lfdi, default)

    create.side_effect = _create


def entry_data(identity: Identity, *, registered: bool = True) -> dict[str, Any]:
    """The data of a configured meter."""
    return {
        CONF_HOST: MOCK_HOST,
        CONF_PORT: MOCK_PORT,
        CONF_DCAP_PATH: "/dcap",
        CONF_METER_SFDI: MOCK_SFDI,
        CONF_METER_LFDI: MOCK_LFDI,
        CONF_IDENTITY: identity.to_dict(),
        CONF_REGISTERED: registered,
    }


def meter_entry(identity: Identity, **data: Any) -> MockConfigEntry:
    """An entry for the meter, with some of its data replaced."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=f"Electric meter {MOCK_SFDI}",
        unique_id=MOCK_SFDI,
        data={**entry_data(identity), **data},
    )


def pending_entry(current: Identity, pending: Identity) -> MockConfigEntry:
    """A registered entry with a renewal waiting for registration."""
    return meter_entry(current, **{CONF_PENDING_IDENTITY: pending.to_dict()})


def lfdi_of(data: dict[str, Any] | None) -> str | None:
    """The LFDI of a stored identity (never compare or print PEM text)."""
    return None if data is None else Identity.from_dict(data).lfdi


def stored_profile(hass_storage: dict[str, Any], profile: MeterProfile) -> None:
    """A profile cached by an earlier start."""
    hass_storage[STORE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORE_KEY,
        "data": {"profile": profile.to_dict(), "guard": {}},
    }


def with_tier(snapshot: MeterSnapshot, tier: int, **changes: Any) -> MeterSnapshot:
    """The snapshot with one delivered ToU tier changed."""
    register = snapshot.tou[TOU_DELIVERED]
    tiers = {t: replace(m, **changes) if t == tier else m for t, m in register.items()}
    return replace(snapshot, tou={**snapshot.tou, TOU_DELIVERED: TouRegister(tiers)})


async def setup_integration(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Add and set up an entry."""
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def poll(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, times: int = 1
) -> None:
    """Let the coordinator poll, a minute apart."""
    for _ in range(times):
        freezer.tick(SCAN_INTERVAL)
        async_fire_time_changed(hass)
        await hass.async_block_till_done()


async def retry_setup(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """Let a setup that is not ready yet retry (the first retries come quickly)."""
    freezer.tick(timedelta(minutes=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


async def start_slow_poll(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """Poll up to the slow poll, and start it without waiting for it."""
    await poll(hass, freezer, SLOW_POLLS - 1)
    freezer.tick(SCAN_INTERVAL)
    async_fire_time_changed(hass)


class Gate:
    """A probe_tls() that blocks until released: a probe waiting on the meter."""

    def __init__(self, result: PeerInfo) -> None:
        """Answer with this once released."""
        self.result = result
        self.calls = 0
        self.release = asyncio.Event()

    @property
    def probe(self) -> Callable[[], Awaitable[PeerInfo]]:
        """The side effect for probe_tls."""

        async def probe_tls() -> PeerInfo:
            self.calls += 1
            await self.release.wait()
            return self.result

        return probe_tls

    async def wait_calls(self, n: int) -> None:
        """Wait until the probe was called n times (time is frozen: count turns)."""
        for _ in range(1000):
            if self.calls >= n:
                return
            await asyncio.sleep(0)
        raise AssertionError(f"probe_tls was called {self.calls} times, not {n}")


async def renew_from_reconfigure(
    hass: HomeAssistant, entry: MockConfigEntry, new: Identity
) -> ConfigFlowResult:
    """Renew the certificate from Reconfigure, generating `new`."""
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "renew_identity"}
    )
    assert result["type"] is FlowResultType.MENU
    with patch(
        "custom_components.ieee_2030_5.config_flow.generate_identity",
        return_value=new,
    ):
        return await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "generate_identity"}
        )


async def discard_from_reconfigure(
    hass: HomeAssistant, entry: MockConfigEntry
) -> ConfigFlowResult:
    """Discard the pending certificate from Reconfigure, confirmed."""
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "discard_renewal"}
    )
    assert result["type"] is FlowResultType.FORM
    return await hass.config_entries.flow.async_configure(result["flow_id"], {})
