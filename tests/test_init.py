"""Tests for setting up and polling an IEEE 2030.5 meter."""

from dataclasses import replace
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock

from aio2030dot5 import (
    Identity,
    MeterAuthError,
    MeterBusy,
    MeterConnectionError,
    MeterHttpError,
    MeterIdentityMismatch,
    MeterNotFound,
    MeterProfile,
    MeterSnapshot,
    QualityFlags,
)
from aiohttp.test_utils import TestClient
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.sensor import ATTR_STATE_CLASS, SensorStateClass
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.ieee_2030_5.const import (
    CONF_IDENTITY,
    CONF_REGISTERED,
    DOMAIN,
    TOU_DELIVERED,
    awaiting_registration_issue_id,
)

from . import (
    BROKEN_IDENTITY,
    IMPORTED,
    MOCK_LFDI,
    MOCK_SFDI,
    PEAK,
    POWER,
    SLOW_POLLS,
    STORE_KEY,
    TIER_1,
    TIER_4,
    poll,
    setup_integration,
    stored_profile,
    with_tier,
)
from .repairs import process_repair_fix_flow, start_repair_fix_flow

# Read on every poll; everything else rides along every 15 minutes.
FAST_KEYS = {"power", "energy_summation_delivered", "energy_summation_received"}


async def test_setup_and_unload(
    hass: HomeAssistant, mock_meter: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """Setup discovers the meter and reads it once, on one connection."""
    await setup_integration(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert mock_meter.discover.await_count == 1
    assert mock_meter.read.await_count == 1  # the setup read feeds the first refresh
    assert mock_meter.read.await_args.kwargs["partial"] is True
    assert float(hass.states.get(POWER).state) == 2439
    assert float(hass.states.get(IMPORTED).state) == 42239863
    await hass.config_entries.async_unload(mock_config_entry.entry_id)
    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED


async def test_cached_profile_skips_discovery(
    hass: HomeAssistant,
    mock_meter: AsyncMock,
    mock_config_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
    profile: MeterProfile,
) -> None:
    """A cached profile of this meter saves the ~30-request discovery."""
    stored_profile(hass_storage, profile)
    await setup_integration(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.LOADED
    mock_meter.discover.assert_not_awaited()


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"certificate_lfdi": "A" * 40}, id="another-meter"),
        pytest.param({"version": 0}, id="old-format"),
    ],
)
async def test_unusable_cached_profile_is_rediscovered(
    hass: HomeAssistant,
    mock_meter: AsyncMock,
    mock_config_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
    profile: MeterProfile,
    change: dict[str, Any],
) -> None:
    """A cache from another meter or library version is not used."""
    stored_profile(hass_storage, profile)
    hass_storage[STORE_KEY]["data"]["profile"].update(change)
    await setup_integration(hass, mock_config_entry)
    assert mock_meter.discover.await_count == 1


async def test_stale_cached_profile_is_rediscovered(
    hass: HomeAssistant,
    mock_meter: AsyncMock,
    mock_config_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
    profile: MeterProfile,
    snapshot_data: MeterSnapshot,
) -> None:
    """A cached path the meter no longer has means its tree changed."""
    stored_profile(hass_storage, profile)
    gone = replace(snapshot_data, errors={"power": MeterNotFound("/upt/1/mr/1/r", 404)})
    mock_meter.read.side_effect = [gone, snapshot_data]
    await setup_integration(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert mock_meter.discover.await_count == 1


async def test_awaiting_registration(
    hass: HomeAssistant,
    mock_meter: AsyncMock,
    unregistered_entry: MockConfigEntry,
    identity: Identity,
    repairs_client: TestClient,
    issue_registry: ir.IssueRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Until the utility registers the certificate: a repair, then it just works."""
    entry = unregistered_entry
    mock_meter.discover.side_effect = MeterAuthError("refused")
    await setup_integration(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert entry.error_reason_translation_key == "awaiting_registration"
    assert "Unexpected error" not in caplog.text
    issue_id = awaiting_registration_issue_id(entry.entry_id)
    issue = issue_registry.async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.is_fixable
    assert issue.translation_placeholders["lfdi"].replace("-", "") == identity.lfdi

    # Checked too early: the issue stays, with its instructions.
    flow = await start_repair_fix_flow(repairs_client, DOMAIN, issue_id)
    assert flow["step_id"] == "confirm"
    assert flow["description_placeholders"]["sfdi"] == MOCK_SFDI
    flow = await process_repair_fix_flow(repairs_client, flow["flow_id"], json={})
    assert flow["type"] == "abort"
    assert flow["reason"] == "still_unregistered"
    assert issue_registry.async_get_issue(DOMAIN, issue_id) is not None

    # The utility registered the certificate.
    mock_meter.discover.side_effect = None
    flow = await start_repair_fix_flow(repairs_client, DOMAIN, issue_id)
    flow = await process_repair_fix_flow(repairs_client, flow["flow_id"], json={})
    assert flow["type"] == "create_entry"
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.data[CONF_REGISTERED] is True
    assert issue_registry.async_get_issue(DOMAIN, issue_id) is None


async def test_the_meter_can_be_named_before_it_accepts_the_certificate(
    hass: HomeAssistant,
    mock_meter: AsyncMock,
    unregistered_entry: MockConfigEntry,
    repairs_client: TestClient,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """The device exists while registration is pending, so the dialog that ends
    setup can name it; the sensors that appear later carry that name."""
    entry = unregistered_entry
    mock_meter.discover.side_effect = MeterAuthError("refused")
    await setup_integration(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    (device,) = dr.async_entries_for_config_entry(device_registry, entry.entry_id)
    assert device.name == "Electric meter"
    device_registry.async_update_device(device.id, name_by_user="Solar meter")

    mock_meter.discover.side_effect = None  # the utility registered the certificate
    issue_id = awaiting_registration_issue_id(entry.entry_id)
    flow = await start_repair_fix_flow(repairs_client, DOMAIN, issue_id)
    await process_repair_fix_flow(repairs_client, flow["flow_id"], json={})
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    (same,) = dr.async_entries_for_config_entry(device_registry, entry.entry_id)
    assert same.id == device.id
    assert same.manufacturer == "Itron"  # filled in once the meter answered
    power = entity_registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.unique_id}_power"
    )
    assert power == "sensor.solar_meter_power"


async def test_fix_flow_for_a_removed_entry(
    hass: HomeAssistant,
    mock_meter: AsyncMock,
    unregistered_entry: MockConfigEntry,
    repairs_client: TestClient,
) -> None:
    """An issue whose entry is gone just closes."""
    entry = unregistered_entry
    mock_meter.discover.side_effect = MeterAuthError("refused")
    await setup_integration(hass, entry)
    issue_id = awaiting_registration_issue_id(entry.entry_id)
    flow = await start_repair_fix_flow(repairs_client, DOMAIN, issue_id)
    await hass.config_entries.async_remove(entry.entry_id)
    ir.async_create_issue(  # as if left behind
        hass,
        DOMAIN,
        issue_id,
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="awaiting_registration",
        data={"entry_id": entry.entry_id},
    )
    flow = await process_repair_fix_flow(repairs_client, flow["flow_id"], json={})
    assert flow["type"] == "create_entry"


async def test_removing_the_entry_leaves_nothing_behind(
    hass: HomeAssistant,
    mock_meter: AsyncMock,
    unregistered_entry: MockConfigEntry,
    issue_registry: ir.IssueRegistry,
    hass_storage: dict[str, Any],
) -> None:
    """The repair issue and the stored profile go with the entry."""
    entry = unregistered_entry
    mock_meter.discover.side_effect = MeterAuthError("refused")
    await setup_integration(hass, entry)
    hass_storage[STORE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORE_KEY,
        "data": {},
    }
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert not issue_registry.issues
    assert STORE_KEY not in hass_storage
    assert not dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)


async def test_rejected_after_registration_starts_reauth(
    hass: HomeAssistant, mock_meter: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """A registered certificate the meter refuses (and no renewal) needs the user."""
    mock_meter.discover.side_effect = MeterAuthError("refused")
    await setup_integration(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]


@pytest.mark.usefixtures("mock_meter")
async def test_expired_certificate_never_reaches_the_meter(
    hass: HomeAssistant,
    mock_meter_create: AsyncMock,
    mock_config_entry: MockConfigEntry,
    identity: Identity,
    freezer: FrozenDateTimeFactory,
) -> None:
    """An expired certificate is caught before connecting."""
    freezer.move_to(identity.not_after + timedelta(days=1))
    await setup_integration(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    assert mock_config_entry.error_reason_translation_key == "certificate_expired"
    mock_meter_create.assert_not_awaited()
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]


@pytest.mark.usefixtures("mock_meter")
async def test_unusable_identity(
    hass: HomeAssistant,
    mock_meter_create: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A stored certificate that cannot be loaded is a setup error."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        data={**mock_config_entry.data, CONF_IDENTITY: BROKEN_IDENTITY},
    )
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    assert mock_config_entry.error_reason_translation_key == "invalid_identity"
    assert not hass.config_entries.flow.async_progress()  # nothing to retry
    mock_meter_create.assert_not_awaited()


async def test_meter_unreachable_at_setup(
    hass: HomeAssistant, mock_meter: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """Setup retries while the meter does not answer, with the reason."""
    mock_meter.discover.side_effect = MeterConnectionError("down")
    await setup_integration(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
    assert mock_config_entry.error_reason_translation_key == "connection_error"


async def test_polling_cadence(
    hass: HomeAssistant,
    mock_meter: AsyncMock,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Demand and the import/export registers every minute; the rest every 15."""
    await setup_integration(hass, mock_config_entry)
    await poll(hass, freezer, SLOW_POLLS)
    calls = mock_meter.read.await_args_list[1:]  # after the setup read
    keys = [{c.key for c in call.args[1]} for call in calls]
    assert all(k <= FAST_KEYS for k in keys[:-1])
    assert keys[-1] > FAST_KEYS  # the 15th minute reads everything
    assert calls[-1].kwargs["include_time"] is True
    assert calls[0].kwargs["include_time"] is False


async def test_a_failed_slow_poll_is_retried_next_minute(
    hass: HomeAssistant,
    mock_meter: AsyncMock,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The slow channels are due again until a slow read succeeds."""
    await setup_integration(hass, mock_config_entry)
    await poll(hass, freezer, SLOW_POLLS - 1)
    mock_meter.read.side_effect = MeterConnectionError("down")
    await poll(hass, freezer)  # the slow poll fails
    mock_meter.read.side_effect = None
    await poll(hass, freezer)
    assert mock_meter.read.await_args.kwargs["include_time"] is True


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(MeterConnectionError("down"), id="unreachable"),
        pytest.param(
            MeterIdentityMismatch("192.0.2.19", MOCK_LFDI, "B" * 40), id="moved"
        ),
    ],
)
async def test_failed_poll_marks_unavailable_and_recovers(
    hass: HomeAssistant,
    mock_meter: AsyncMock,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    error: Exception,
) -> None:
    """A failed poll makes the readings unavailable until the next good one."""
    await setup_integration(hass, mock_config_entry)
    mock_meter.read.side_effect = error
    await poll(hass, freezer)
    assert hass.states.get(POWER).state == STATE_UNAVAILABLE
    mock_meter.read.side_effect = None
    await poll(hass, freezer)
    assert float(hass.states.get(POWER).state) == 2439


async def test_busy_meter_is_left_alone_for_five_minutes(
    hass: HomeAssistant,
    mock_meter: AsyncMock,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A meter answering busy is not polled again until its retry_after."""
    await setup_integration(hass, mock_config_entry)
    mock_meter.read.side_effect = MeterBusy("busy")
    await poll(hass, freezer)
    calls = mock_meter.read.await_count
    mock_meter.read.side_effect = None
    await poll(hass, freezer, 4)
    assert mock_meter.read.await_count == calls
    await poll(hass, freezer, 2)
    assert mock_meter.read.await_count > calls


async def test_rejected_while_running_starts_reauth(
    hass: HomeAssistant,
    mock_meter: AsyncMock,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The meter stops accepting the certificate, and there is no renewal."""
    await setup_integration(hass, mock_config_entry)
    mock_meter.read.side_effect = MeterAuthError("refused")
    await poll(hass, freezer)
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]


async def test_a_failing_channel_is_unknown_not_stale(
    hass: HomeAssistant,
    mock_meter: AsyncMock,
    mock_config_entry: MockConfigEntry,
    snapshot_data: MeterSnapshot,
    freezer: FrozenDateTimeFactory,
) -> None:
    """One channel failing does not blank the meter, nor keep an old value."""
    await setup_integration(hass, mock_config_entry)
    values = dict(snapshot_data.values)
    del values["power"]
    mock_meter.read.return_value = replace(
        snapshot_data,
        values=values,
        errors={"power": MeterHttpError("/upt/1/mr/1/r", 500)},
    )
    await poll(hass, freezer)
    assert hass.states.get(POWER).state == STATE_UNKNOWN
    assert float(hass.states.get(IMPORTED).state) == 42239863


async def test_missing_path_rediscovers_and_sets_up_again(
    hass: HomeAssistant,
    mock_meter: AsyncMock,
    mock_config_entry: MockConfigEntry,
    snapshot_data: MeterSnapshot,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A 404 while polling re-walks the meter (hourly at most) and reloads."""
    await setup_integration(hass, mock_config_entry)
    gone = replace(snapshot_data, errors={"power": MeterNotFound("/upt/1/mr/1/r", 404)})
    mock_meter.read.return_value = gone
    await poll(hass, freezer)
    assert mock_meter.discover.await_count == 1  # rediscovered less than an hour ago
    freezer.tick(timedelta(hours=1))
    await poll(hass, freezer)
    assert mock_meter.discover.await_count >= 2
    assert mock_config_entry.state is ConfigEntryState.LOADED


async def test_a_failed_rediscovery_keeps_polling(
    hass: HomeAssistant,
    mock_meter: AsyncMock,
    mock_config_entry: MockConfigEntry,
    snapshot_data: MeterSnapshot,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Rediscovery can fail too; polling carries on."""
    await setup_integration(hass, mock_config_entry)
    mock_meter.read.return_value = replace(
        snapshot_data, errors={"power": MeterNotFound("/upt/1/mr/1/r", 404)}
    )
    mock_meter.discover.side_effect = MeterConnectionError("down")
    freezer.tick(timedelta(hours=1))
    await poll(hass, freezer)
    assert mock_meter.discover.await_count == 2
    assert float(hass.states.get(IMPORTED).state) == 42239863


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_implausible_readings_are_unknown(
    hass: HomeAssistant,
    mock_meter: AsyncMock,
    mock_config_entry: MockConfigEntry,
    snapshot_data: MeterSnapshot,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A spike, or a reading the meter flags, never reaches statistics."""
    await setup_integration(hass, mock_config_entry)
    values = dict(snapshot_data.values)
    values["energy_summation_delivered"] = replace(
        values["energy_summation_delivered"], value=9e12
    )
    values["power"] = replace(values["power"], value=-4e6)
    values["power_maximum_delivered"] = replace(
        values["power_maximum_delivered"], quality=QualityFlags.QUESTIONABLE
    )
    values["not_in_the_profile"] = values["power"]
    spiked = with_tier(snapshot_data, 1, value=9e12)
    # One bad read, on the poll that reads every channel (several consistent
    # ones would be a new baseline: a replaced or reset meter).
    await poll(hass, freezer, SLOW_POLLS - 1)
    mock_meter.read.return_value = replace(spiked, values=values)
    await poll(hass, freezer)
    assert mock_meter.read.await_args.kwargs["include_time"] is True
    assert hass.states.get(IMPORTED).state == STATE_UNKNOWN
    assert hass.states.get(POWER).state == STATE_UNKNOWN
    assert hass.states.get(TIER_1).state == STATE_UNKNOWN
    assert hass.states.get(PEAK).state == STATE_UNKNOWN
    mock_meter.read.return_value = snapshot_data
    await poll(hass, freezer)
    assert float(hass.states.get(IMPORTED).state) == 42239863


async def test_questionable_tier_values_are_unknown(
    hass: HomeAssistant,
    mock_meter: AsyncMock,
    mock_config_entry: MockConfigEntry,
    snapshot_data: MeterSnapshot,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The meter's own quality flags apply to ToU tiers too."""
    await setup_integration(hass, mock_config_entry)
    mock_meter.read.return_value = with_tier(
        snapshot_data, 1, quality=QualityFlags.QUESTIONABLE
    )
    await poll(hass, freezer, SLOW_POLLS)
    assert hass.states.get(TIER_1).state == STATE_UNKNOWN


async def test_a_tier_that_starts_later_gets_a_sensor(
    hass: HomeAssistant,
    mock_meter: AsyncMock,
    mock_config_entry: MockConfigEntry,
    snapshot_data: MeterSnapshot,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A tier empty at setup (another season's rate) appears once it counts."""
    await setup_integration(hass, mock_config_entry)
    assert hass.states.get(TIER_4) is None
    mock_meter.read.return_value = with_tier(snapshot_data, 4, value=250.0)
    await poll(hass, freezer, SLOW_POLLS)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert hass.states.get(TIER_4).state == "250.0"


async def test_a_flapping_tier_reloads_only_once(
    hass: HomeAssistant,
    mock_meter: AsyncMock,
    mock_meter_create: AsyncMock,
    mock_config_entry: MockConfigEntry,
    snapshot_data: MeterSnapshot,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
) -> None:
    """A new tier is remembered before the entry is set up again for it.

    When it reads empty during that setup, it keeps its sensor, and its
    value on the next polls is no news: no reload on every slow poll.
    """
    counting = with_tier(snapshot_data, 4, value=250.0)
    await setup_integration(hass, mock_config_entry)
    assert mock_meter_create.await_count == 1
    # Counting while polling, empty again at the setup that follows.
    mock_meter.read.side_effect = [counting, snapshot_data]
    await poll(hass, freezer)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert mock_meter_create.await_count == 2  # set up again, once
    assert hass_storage[STORE_KEY]["data"]["tou_tiers"][TOU_DELIVERED] == [1, 2, 3, 4]
    assert hass.states.get(TIER_4).state == STATE_UNKNOWN  # kept, while empty

    mock_meter.read.side_effect = None
    mock_meter.read.return_value = counting
    await poll(hass, freezer, 3 * SLOW_POLLS)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert mock_meter_create.await_count == 2
    assert hass.states.get(TIER_4).state == "250.0"


async def test_a_register_reset_is_confirmed_before_it_shows(
    hass: HomeAssistant,
    mock_meter: AsyncMock,
    mock_config_entry: MockConfigEntry,
    snapshot_data: MeterSnapshot,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A register that really starts over shows its new, lower value.

    Only once the lower series has lasted a day (a meter answering 0 for a
    while is not a reset); the sensor is total_increasing, so statistics take
    the drop as a new cycle.
    """
    await setup_integration(hass, mock_config_entry)
    values = dict(snapshot_data.values)
    values["energy_summation_delivered"] = replace(
        values["energy_summation_delivered"], value=100.0
    )
    mock_meter.read.return_value = replace(snapshot_data, values=values)

    async def ten_minutes_later() -> None:
        freezer.tick(timedelta(minutes=10))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    for _ in range(6 * 24):  # a day of reads, ten minutes apart
        await ten_minutes_later()
        assert hass.states.get(IMPORTED).state == STATE_UNKNOWN
    await ten_minutes_later()
    state = hass.states.get(IMPORTED)
    assert float(state.state) == 100.0
    assert state.attributes[ATTR_STATE_CLASS] == SensorStateClass.TOTAL_INCREASING
    assert "last_reset" not in state.attributes
    assert "energy_summation_delivered register moved to a new baseline" in caplog.text


async def test_the_device_describes_the_meter(
    hass: HomeAssistant,
    mock_meter: AsyncMock,
    mock_config_entry: MockConfigEntry,
    profile: MeterProfile,
    device_registry: dr.DeviceRegistry,
) -> None:
    """What the meter says about itself (Itron leaves most of it empty)."""
    mock_meter.discover.return_value = replace(
        profile, model="Model X", serial_number="SN-1", hardware_version="HW 2"
    )
    await setup_integration(hass, mock_config_entry)
    device = device_registry.async_get_device_by_identifier(
        (DOMAIN, MOCK_LFDI), mock_config_entry.entry_id
    )
    assert device is not None
    assert (
        device.manufacturer,
        device.model,
        device.serial_number,
        device.hw_version,
        device.sw_version,
    ) == ("Itron", "Model X", "SN-1", "HW 2", "3.2.50")
