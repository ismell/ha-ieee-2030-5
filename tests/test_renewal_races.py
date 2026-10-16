"""Tests for a renewal changed while the meter is being asked about it.

Asking the meter waits for its single connection slot (up to minutes), so a
slow poll, a repair and Reconfigure can overlap. A Gate holds a probe until
the test releases it.
"""

from collections.abc import Callable
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from aio2030dot5 import Identity, MeterProfile, MeterSnapshot, generate_identity
from aiohttp.test_utils import TestClient
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ieee_2030_5.const import (
    CONF_IDENTITY,
    CONF_PENDING_IDENTITY,
    DOMAIN,
    certificate_expiring_issue_id,
    renewal_pending_issue_id,
)

from . import (
    POWER,
    Gate,
    discard_from_reconfigure,
    lfdi_of,
    make_meter,
    meter_entry,
    peer,
    pending_entry,
    renew_from_reconfigure,
    route,
    setup_integration,
    start_slow_poll,
)
from .repairs import process_repair_fix_flow, start_repair_fix_flow


async def test_fix_flow_and_slow_poll_promote_at_once(
    hass: HomeAssistant,
    renewed_meter: AsyncMock,
    meters: Callable[[Identity], None],
    identity: Identity,
    repairs_client: TestClient,
    issue_registry: ir.IssueRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The user checks from the repair issue while a slow poll checks too.

    Both see the renewal accepted; the second finds it switched already.
    """
    new = generate_identity()
    meters(new)
    entry = pending_entry(identity, new)
    await setup_integration(hass, entry)
    gate = Gate(peer())
    renewed_meter.probe_tls.side_effect = gate.probe

    await start_slow_poll(hass, freezer)
    await gate.wait_calls(1)  # the slow poll is probing the renewal
    flow = await start_repair_fix_flow(
        repairs_client, DOMAIN, renewal_pending_issue_id(entry.entry_id)
    )
    task = hass.async_create_task(
        process_repair_fix_flow(repairs_client, flow["flow_id"], json={})
    )
    await gate.wait_calls(2)  # and so is the fix flow
    gate.release.set()
    flow = await task
    await hass.async_block_till_done(wait_background_tasks=True)
    assert flow["type"] == "create_entry"
    assert lfdi_of(entry.data[CONF_IDENTITY]) == new.lfdi
    assert CONF_PENDING_IDENTITY not in entry.data
    assert entry.state is ConfigEntryState.LOADED
    assert not issue_registry.issues


async def test_discard_while_a_slow_poll_probes(
    hass: HomeAssistant,
    renewed_meter: AsyncMock,
    meters: Callable[[Identity], None],
    identity: Identity,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The user discards the renewal while a slow poll is probing it.

    The poll's readings stand, and the discarded certificate is not used,
    although the meter accepts it.
    """
    new = generate_identity()
    meters(new)
    entry = pending_entry(identity, new)
    await setup_integration(hass, entry)
    gate = Gate(peer())
    renewed_meter.probe_tls.side_effect = gate.probe

    await start_slow_poll(hass, freezer)
    await gate.wait_calls(1)
    result = await discard_from_reconfigure(hass, entry)
    assert result["reason"] == "renewal_discarded"

    gate.release.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert float(hass.states.get(POWER).state) == 2439  # the poll succeeded
    assert lfdi_of(entry.data[CONF_IDENTITY]) == identity.lfdi
    assert CONF_PENDING_IDENTITY not in entry.data
    renewed_meter.read.assert_not_awaited()  # no switch, no reload onto it


async def test_a_renewal_restaged_during_a_probe_is_kept(
    hass: HomeAssistant,
    mock_meter: AsyncMock,
    mock_meter_create: AsyncMock,
    renewed_meter: AsyncMock,
    identity: Identity,
    profile: MeterProfile,
    snapshot_data: MeterSnapshot,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A stale probe's answer does not overwrite a renewal staged after it.

    A slow poll probes P1; meanwhile the user discards P1 and renews with P2.
    P1 turns out registered, but switching to it would also delete P2's
    private key (whose LFDI the user may be registering right now).
    """
    p1, p2 = generate_identity(), generate_identity()
    second = make_meter(profile, snapshot_data, accepted=False)
    route(mock_meter_create, mock_meter, {p1.lfdi: renewed_meter, p2.lfdi: second})
    entry = pending_entry(identity, p1)
    await setup_integration(hass, entry)
    gate = Gate(peer())  # P1 is registered by now
    renewed_meter.probe_tls.side_effect = gate.probe

    await start_slow_poll(hass, freezer)
    await gate.wait_calls(1)
    result = await discard_from_reconfigure(hass, entry)
    assert result["reason"] == "renewal_discarded"
    result = await renew_from_reconfigure(hass, entry, p2)
    assert result["reason"] == "renewal_pending"
    assert result["description_placeholders"]["lfdi"].replace("-", "") == p2.lfdi

    gate.release.set()  # P1's probe answers last
    await hass.async_block_till_done(wait_background_tasks=True)
    assert lfdi_of(entry.data.get(CONF_PENDING_IDENTITY)) == p2.lfdi
    assert lfdi_of(entry.data[CONF_IDENTITY]) == identity.lfdi
    assert float(hass.states.get(POWER).state) == 2439


async def test_entry_removed_during_a_flow_probe_leaves_no_issue(
    hass: HomeAssistant,
    renewed_meter: AsyncMock,
    meters: Callable[[Identity], None],
    mock_config_entry: MockConfigEntry,
    issue_registry: ir.IssueRegistry,
) -> None:
    """The entry is removed while a renewal's first probe waits for the meter.

    (A slow poll's probe is cancelled with the entry; a flow's is not.)
    """
    new = generate_identity()
    meters(new)
    await setup_integration(hass, mock_config_entry)
    gate = Gate(peer(accepted=False))
    renewed_meter.probe_tls.side_effect = gate.probe

    task = hass.async_create_task(renew_from_reconfigure(hass, mock_config_entry, new))
    await gate.wait_calls(1)
    await hass.config_entries.async_remove(mock_config_entry.entry_id)
    assert not issue_registry.issues  # removing the entry cleaned up
    gate.release.set()
    result = await task
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert not issue_registry.issues


async def test_renewing_again_is_not_offered_while_a_renewal_is_pending(
    hass: HomeAssistant, meters: Callable[[Identity], None], identity: Identity
) -> None:
    """Replacing a pending renewal would drop its key: discard it first."""
    first = generate_identity()
    meters(first)
    entry = pending_entry(identity, first)
    await setup_integration(hass, entry)
    result = await entry.start_reconfigure_flow(hass)
    assert result["menu_options"] == ["address", "discard_renewal"]


async def test_reminder_does_not_replace_a_renewal_staged_meanwhile(
    hass: HomeAssistant,
    meters: Callable[[Identity], None],
    repairs_client: TestClient,
) -> None:
    """The reminder's menu was opened before Reconfigure staged a renewal."""
    entry = meter_entry(generate_identity(valid_days=30))
    await setup_integration(hass, entry)
    first, second = generate_identity(), generate_identity()
    meters(first)
    flow = await start_repair_fix_flow(
        repairs_client, DOMAIN, certificate_expiring_issue_id(entry.entry_id)
    )
    result = await renew_from_reconfigure(hass, entry, first)
    assert result["reason"] == "renewal_pending"

    with patch(
        "custom_components.ieee_2030_5.repairs.generate_identity",
        return_value=second,
    ):
        flow = await process_repair_fix_flow(
            repairs_client, flow["flow_id"], json={"next_step_id": "generate_identity"}
        )
    assert flow["type"] == "abort"
    assert flow["reason"] == "renewal_already_pending"
    assert lfdi_of(entry.data[CONF_PENDING_IDENTITY]) == first.lfdi


async def test_reconfigure_does_not_replace_a_renewal_staged_meanwhile(
    hass: HomeAssistant,
    mock_meter: AsyncMock,
    mock_meter_create: AsyncMock,
    renewed_meter: AsyncMock,
    profile: MeterProfile,
    snapshot_data: MeterSnapshot,
    repairs_client: TestClient,
) -> None:
    """Reconfigure's menu was opened before the reminder staged a renewal."""
    entry = meter_entry(generate_identity(valid_days=30))
    await setup_integration(hass, entry)
    first, second = generate_identity(), generate_identity()
    refused = make_meter(profile, snapshot_data, accepted=False)
    route(
        mock_meter_create, mock_meter, {first.lfdi: renewed_meter, second.lfdi: refused}
    )
    result = await entry.start_reconfigure_flow(hass)
    assert "renew_identity" in result["menu_options"]

    flow = await start_repair_fix_flow(
        repairs_client, DOMAIN, certificate_expiring_issue_id(entry.entry_id)
    )
    with patch(
        "custom_components.ieee_2030_5.repairs.generate_identity",
        return_value=first,
    ):
        flow = await process_repair_fix_flow(
            repairs_client, flow["flow_id"], json={"next_step_id": "generate_identity"}
        )
    assert flow["reason"] == "renewal_pending"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "renew_identity"}
    )
    with patch(
        "custom_components.ieee_2030_5.config_flow.generate_identity",
        return_value=second,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "generate_identity"}
        )
    pending = lfdi_of(entry.data.get(CONF_PENDING_IDENTITY))
    assert pending == first.lfdi
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "renewal_already_pending"


async def test_a_confirmed_discard_keeps_a_renewal_staged_meanwhile(
    hass: HomeAssistant,
    meters: Callable[[Identity], None],
    repairs_client: TestClient,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The discard was confirmed for an expired renewal, not for its successor."""
    current = generate_identity(valid_days=30)
    now = dt_util.utcnow()
    freezer.move_to(now - timedelta(days=2))
    expired = generate_identity(valid_days=1)
    freezer.move_to(now)
    entry = meter_entry(current, **{CONF_PENDING_IDENTITY: expired.to_dict()})
    await setup_integration(hass, entry)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "discard_renewal"}
    )
    assert result["type"] is FlowResultType.FORM

    new = generate_identity()
    meters(new)
    flow = await start_repair_fix_flow(
        repairs_client, DOMAIN, certificate_expiring_issue_id(entry.entry_id)
    )
    with patch(
        "custom_components.ieee_2030_5.repairs.generate_identity",
        return_value=new,
    ):
        flow = await process_repair_fix_flow(
            repairs_client, flow["flow_id"], json={"next_step_id": "generate_identity"}
        )
    assert flow["reason"] == "renewal_pending"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "renewal_changed"
    pending = lfdi_of(entry.data.get(CONF_PENDING_IDENTITY))
    assert pending == new.lfdi
