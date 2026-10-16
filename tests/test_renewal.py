"""Tests for renewing the client certificate while the current one works."""

from collections.abc import Callable
from datetime import timedelta
import logging
from typing import Any
from unittest.mock import AsyncMock, patch

from aio2030dot5 import (
    Identity,
    MeterAuthError,
    MeterConnectionError,
    PeerInfo,
    format_lfdi,
    generate_identity,
)
from aiohttp.test_utils import TestClient
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ieee_2030_5.const import (
    CONF_CERTIFICATE,
    CONF_IDENTITY,
    CONF_PENDING_IDENTITY,
    CONF_PRIVATE_KEY,
    CONF_REGISTERED,
    DOMAIN,
    awaiting_registration_issue_id,
    certificate_expiring_issue_id,
    renewal_pending_issue_id,
)

from . import (
    BROKEN_IDENTITY,
    MOCK_SFDI,
    POWER,
    SLOW_POLLS,
    discard_from_reconfigure,
    lfdi_of,
    meter_entry,
    peer,
    pending_entry,
    poll,
    renew_from_reconfigure,
    retry_setup,
    setup_integration,
)
from .repairs import process_repair_fix_flow, start_repair_fix_flow

OTHER_LFDI = "0" * 40
# The reminder to renew appears this long before the certificate expires.
RENEWAL_REMINDER = timedelta(days=60)


async def test_renewal_waits_for_registration_then_takes_over(
    hass: HomeAssistant,
    mock_meter: AsyncMock,
    renewed_meter: AsyncMock,
    meters: Callable[[Identity], None],
    mock_config_entry: MockConfigEntry,
    identity: Identity,
    issue_registry: ir.IssueRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The current certificate keeps the readings coming until the switch."""
    new = generate_identity()
    meters(new)
    await setup_integration(hass, mock_config_entry)

    result = await renew_from_reconfigure(hass, mock_config_entry, new)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "renewal_pending"
    assert result["description_placeholders"]["lfdi"] == format_lfdi(new.lfdi)
    assert mock_config_entry.data[CONF_IDENTITY] == identity.to_dict()
    assert mock_config_entry.data[CONF_PENDING_IDENTITY] == new.to_dict()
    assert mock_config_entry.state is ConfigEntryState.LOADED
    issue_id = renewal_pending_issue_id(mock_config_entry.entry_id)
    issue = issue_registry.async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.translation_placeholders["lfdi"] == format_lfdi(new.lfdi)

    # Not registered yet: polling goes on with the current certificate.
    reads = mock_meter.read.await_count
    await poll(hass, freezer, SLOW_POLLS)
    assert mock_meter.read.await_count == reads + SLOW_POLLS
    assert renewed_meter.probe_tls.await_count == 2  # staged, then a slow poll
    assert mock_config_entry.data[CONF_PENDING_IDENTITY] == new.to_dict()

    # Registered: the next slow poll switches.
    renewed_meter.probe_tls.return_value = peer()
    await poll(hass, freezer, SLOW_POLLS)
    assert mock_config_entry.data[CONF_IDENTITY] == new.to_dict()
    assert CONF_PENDING_IDENTITY not in mock_config_entry.data
    assert issue_registry.async_get_issue(DOMAIN, issue_id) is None
    assert mock_config_entry.state is ConfigEntryState.LOADED
    renewed_meter.read.assert_awaited()  # polling with the new certificate


async def test_an_accepted_certificate_takes_over_at_once(
    hass: HomeAssistant,
    renewed_meter: AsyncMock,
    meters: Callable[[Identity], None],
    mock_config_entry: MockConfigEntry,
    issue_registry: ir.IssueRegistry,
) -> None:
    """Importing a certificate that is registered already."""
    new = generate_identity()
    meters(new)
    renewed_meter.probe_tls.return_value = peer()
    await setup_integration(hass, mock_config_entry)
    result = await renew_from_reconfigure(hass, mock_config_entry, new)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "renewal_complete"
    await hass.async_block_till_done()
    assert mock_config_entry.data[CONF_IDENTITY] == new.to_dict()
    assert CONF_PENDING_IDENTITY not in mock_config_entry.data
    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert not issue_registry.issues


async def test_renewing_with_an_imported_certificate(
    hass: HomeAssistant,
    meters: Callable[[Identity], None],
    mock_config_entry: MockConfigEntry,
    identity: Identity,
) -> None:
    """An imported certificate is checked, and the current one is not a renewal."""
    new = generate_identity()
    meters(new)
    await setup_integration(hass, mock_config_entry)

    async def renew_with(cert: str, key: str) -> dict[str, Any]:
        result = await mock_config_entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "renew_identity"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "import_identity"}
        )
        return await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_CERTIFICATE: cert, CONF_PRIVATE_KEY: key}
        )

    result = await renew_with(identity.cert_pem, identity.key_pem)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "same_identity"
    assert CONF_PENDING_IDENTITY not in mock_config_entry.data

    result = await renew_with(new.cert_pem, new.key_pem)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "renewal_pending"
    assert mock_config_entry.data[CONF_PENDING_IDENTITY] == new.to_dict()


async def test_discarding_a_renewal(
    hass: HomeAssistant,
    meters: Callable[[Identity], None],
    identity: Identity,
    issue_registry: ir.IssueRegistry,
) -> None:
    """A pending certificate can be dropped again, once confirmed."""
    new = generate_identity()
    meters(new)
    entry = pending_entry(identity, new)
    await setup_integration(hass, entry)
    issue_id = renewal_pending_issue_id(entry.entry_id)
    assert issue_registry.async_get_issue(DOMAIN, issue_id) is not None

    result = await entry.start_reconfigure_flow(hass)
    assert result["menu_options"] == ["address", "discard_renewal"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "discard_renewal"}
    )
    # Nothing is lost until confirmed.
    assert result["type"] is FlowResultType.FORM
    assert result["description_placeholders"]["lfdi"] == format_lfdi(new.lfdi)
    assert entry.data[CONF_PENDING_IDENTITY] == new.to_dict()
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "renewal_discarded"
    assert CONF_PENDING_IDENTITY not in entry.data
    assert entry.data[CONF_IDENTITY] == identity.to_dict()
    assert issue_registry.async_get_issue(DOMAIN, issue_id) is None


async def test_refused_while_running_switches_to_a_registered_renewal(
    hass: HomeAssistant,
    mock_meter: AsyncMock,
    renewed_meter: AsyncMock,
    meters: Callable[[Identity], None],
    identity: Identity,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The utility retired the old certificate first: no reauthentication."""
    new = generate_identity()
    meters(new)
    entry = pending_entry(identity, new)
    await setup_integration(hass, entry)
    mock_meter.read.side_effect = MeterAuthError("refused")
    renewed_meter.probe_tls.return_value = peer()
    await poll(hass, freezer)
    assert not hass.config_entries.flow.async_progress()
    assert entry.data[CONF_IDENTITY] == new.to_dict()
    assert entry.state is ConfigEntryState.LOADED


async def test_refused_while_running_waits_for_the_renewal(
    hass: HomeAssistant,
    mock_meter: AsyncMock,
    renewed_meter: AsyncMock,
    meters: Callable[[Identity], None],
    identity: Identity,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The utility retired the old certificate before registering the renewal.

    Reauthentication would only lead the user away from the renewal (and,
    retrying the current certificate, strand it): Home Assistant keeps
    checking the renewal every 15 minutes instead, and switches to it.
    """
    new = generate_identity()
    meters(new)
    entry = pending_entry(identity, new)
    await setup_integration(hass, entry)
    mock_meter.read.side_effect = MeterAuthError("refused")
    await poll(hass, freezer)
    assert not hass.config_entries.flow.async_progress()
    assert hass.states.get(POWER).state == STATE_UNAVAILABLE
    assert (
        "Waiting for the utility to register the renewed one "
        f"({format_lfdi(new.lfdi)})" in caplog.text
    )
    assert renewed_meter.probe_tls.await_count == 1

    # Left alone for 15 minutes, then the renewal is checked again.
    reads = mock_meter.read.await_count
    await poll(hass, freezer, SLOW_POLLS - 1)
    assert mock_meter.read.await_count == reads
    await poll(hass, freezer, 2)
    assert mock_meter.read.await_count == reads + 1
    assert renewed_meter.probe_tls.await_count == 2
    assert not hass.config_entries.flow.async_progress()
    assert entry.data[CONF_PENDING_IDENTITY] == new.to_dict()

    # Registered: the next check switches.
    renewed_meter.probe_tls.return_value = peer()
    await poll(hass, freezer, SLOW_POLLS)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.data[CONF_IDENTITY] == new.to_dict()
    assert CONF_PENDING_IDENTITY not in entry.data
    assert entry.state is ConfigEntryState.LOADED
    assert float(hass.states.get(POWER).state) == 2439
    renewed_meter.read.assert_awaited()


async def test_refused_at_setup_waits_for_the_renewal(
    hass: HomeAssistant,
    mock_meter: AsyncMock,
    renewed_meter: AsyncMock,
    meters: Callable[[Identity], None],
    identity: Identity,
    issue_registry: ir.IssueRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Setup retries, for the renewal, instead of asking to reauthenticate."""
    new = generate_identity()
    meters(new)
    entry = pending_entry(identity, new)
    mock_meter.discover.side_effect = MeterAuthError("refused")
    await setup_integration(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert entry.error_reason_translation_key == "awaiting_renewal"
    assert entry.error_reason_translation_placeholders == {
        "lfdi": format_lfdi(new.lfdi)
    }
    assert not hass.config_entries.flow.async_progress()
    assert list(issue_registry.issues) == [
        (DOMAIN, renewal_pending_issue_id(entry.entry_id))
    ]

    renewed_meter.probe_tls.return_value = peer()
    await retry_setup(hass, freezer)  # switches
    await retry_setup(hass, freezer)  # and sets up with it
    assert entry.state is ConfigEntryState.LOADED
    assert entry.data[CONF_IDENTITY] == new.to_dict()
    assert not issue_registry.issues


async def test_refused_at_setup_switches_to_a_registered_renewal(
    hass: HomeAssistant,
    mock_meter: AsyncMock,
    renewed_meter: AsyncMock,
    meters: Callable[[Identity], None],
    identity: Identity,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Setup retries with the renewed certificate."""
    new = generate_identity()
    meters(new)
    entry = pending_entry(identity, new)
    mock_meter.discover.side_effect = MeterAuthError("refused")
    renewed_meter.probe_tls.return_value = peer()
    await setup_integration(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert entry.error_reason_translation_key == "certificate_renewed"
    assert entry.data[CONF_IDENTITY] == new.to_dict()
    await retry_setup(hass, freezer)
    assert entry.state is ConfigEntryState.LOADED


async def test_an_unregistered_entry_waits_for_its_renewal(
    hass: HomeAssistant,
    mock_meter: AsyncMock,
    renewed_meter: AsyncMock,
    meters: Callable[[Identity], None],
    identity: Identity,
    issue_registry: ir.IssueRegistry,
) -> None:
    """A reauthentication replaced a refused certificate, with a renewal pending.

    The renewal is the one the user registered last: it is tried, and its
    issue (not the replaced certificate's) tells what to register.
    """
    new = generate_identity()
    meters(new)
    entry = meter_entry(
        identity, **{CONF_PENDING_IDENTITY: new.to_dict(), CONF_REGISTERED: False}
    )
    mock_meter.discover.side_effect = MeterAuthError("refused")
    await setup_integration(hass, entry)
    assert entry.error_reason_translation_key == "awaiting_renewal"
    renewed_meter.probe_tls.assert_awaited_once()
    assert list(issue_registry.issues) == [
        (DOMAIN, renewal_pending_issue_id(entry.entry_id))
    ]


async def test_an_unregistered_entry_switches_to_its_renewal(
    hass: HomeAssistant,
    mock_meter: AsyncMock,
    renewed_meter: AsyncMock,
    meters: Callable[[Identity], None],
    identity: Identity,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Once the meter accepts the renewal, it takes over, registered."""
    new = generate_identity()
    meters(new)
    entry = meter_entry(
        identity, **{CONF_PENDING_IDENTITY: new.to_dict(), CONF_REGISTERED: False}
    )
    mock_meter.discover.side_effect = MeterAuthError("refused")
    renewed_meter.probe_tls.return_value = peer()
    await setup_integration(hass, entry)
    assert entry.error_reason_translation_key == "certificate_renewed"
    await retry_setup(hass, freezer)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.data[CONF_IDENTITY] == new.to_dict()
    assert entry.data[CONF_REGISTERED] is True


async def test_an_expired_certificate_gives_way_to_a_registered_renewal(
    hass: HomeAssistant,
    renewed_meter: AsyncMock,
    meters: Callable[[Identity], None],
    identity: Identity,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Expiry does not need reauthentication once the renewal is registered."""
    current = generate_identity()  # expires long before the test identity
    meters(identity)
    entry = pending_entry(current, identity)
    renewed_meter.probe_tls.return_value = peer()
    freezer.move_to(current.not_after + timedelta(days=1))
    await setup_integration(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.data[CONF_IDENTITY] == identity.to_dict()


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        pytest.param(None, MeterConnectionError("down"), id="unreachable"),
        pytest.param(peer(lfdi=OTHER_LFDI), None, id="another-meter"),
    ],
)
async def test_a_renewal_is_only_promoted_by_the_meter(
    hass: HomeAssistant,
    renewed_meter: AsyncMock,
    meters: Callable[[Identity], None],
    identity: Identity,
    freezer: FrozenDateTimeFactory,
    answer: PeerInfo | None,
    error: Exception | None,
) -> None:
    """An unreachable meter, or another device accepting it, changes nothing."""
    new = generate_identity()
    meters(new)
    entry = pending_entry(identity, new)
    await setup_integration(hass, entry)
    renewed_meter.probe_tls.return_value = answer
    renewed_meter.probe_tls.side_effect = error
    await poll(hass, freezer, SLOW_POLLS)
    renewed_meter.probe_tls.assert_awaited_once()
    assert entry.data[CONF_IDENTITY] == identity.to_dict()
    assert entry.data[CONF_PENDING_IDENTITY] == new.to_dict()
    assert entry.state is ConfigEntryState.LOADED


@pytest.mark.parametrize(
    ("answer", "error", "reason"),
    [
        pytest.param(
            None, MeterConnectionError("down"), "cannot_connect", id="unreachable"
        ),
        pytest.param(peer(lfdi=OTHER_LFDI), None, "wrong_device", id="another-meter"),
    ],
)
async def test_checking_a_renewal_the_meter_cannot_answer(
    hass: HomeAssistant,
    renewed_meter: AsyncMock,
    meters: Callable[[Identity], None],
    identity: Identity,
    repairs_client: TestClient,
    issue_registry: ir.IssueRegistry,
    answer: PeerInfo | None,
    error: Exception | None,
    reason: str,
) -> None:
    """The renewal's fix flow tells "no answer" from "still refused"."""
    new = generate_identity()
    meters(new)
    entry = pending_entry(identity, new)
    await setup_integration(hass, entry)
    renewed_meter.probe_tls.return_value = answer
    renewed_meter.probe_tls.side_effect = error
    issue_id = renewal_pending_issue_id(entry.entry_id)
    flow = await start_repair_fix_flow(repairs_client, DOMAIN, issue_id)
    flow = await process_repair_fix_flow(repairs_client, flow["flow_id"], json={})
    assert flow["type"] == "abort"
    assert flow["reason"] == reason
    assert issue_registry.async_get_issue(DOMAIN, issue_id) is not None
    assert entry.data[CONF_PENDING_IDENTITY] == new.to_dict()


async def test_renewing_from_the_expiry_reminder(
    hass: HomeAssistant,
    renewed_meter: AsyncMock,
    meters: Callable[[Identity], None],
    repairs_client: TestClient,
    issue_registry: ir.IssueRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The reminder appears in time, and its fix stages a renewal."""
    # Expires 5 minutes after the reminder window opens.
    start = dt_util.utcnow()
    freezer.move_to(start - timedelta(days=1) + timedelta(minutes=5))
    current = generate_identity(valid_days=RENEWAL_REMINDER.days + 1)
    freezer.move_to(start)
    entry = meter_entry(current)
    await setup_integration(hass, entry)
    expiring_id = certificate_expiring_issue_id(entry.entry_id)
    assert issue_registry.async_get_issue(DOMAIN, expiring_id) is None
    await poll(hass, freezer, SLOW_POLLS)  # now within the reminder window
    issue = issue_registry.async_get_issue(DOMAIN, expiring_id)
    assert issue is not None
    assert issue.is_fixable
    assert issue.translation_placeholders["expiry"] == (
        current.not_after.date().isoformat()
    )

    new = generate_identity()
    meters(new)
    flow = await start_repair_fix_flow(repairs_client, DOMAIN, expiring_id)
    assert flow["type"] == "menu"
    assert flow["description_placeholders"]["sfdi"] == MOCK_SFDI
    with patch(
        "custom_components.ieee_2030_5.repairs.generate_identity",
        return_value=new,
    ):
        flow = await process_repair_fix_flow(
            repairs_client, flow["flow_id"], json={"next_step_id": "generate_identity"}
        )
    assert flow["type"] == "abort"
    assert flow["reason"] == "renewal_pending"
    assert flow["description_placeholders"]["lfdi"] == format_lfdi(new.lfdi)
    assert issue_registry.async_get_issue(DOMAIN, expiring_id) is None
    pending_id = renewal_pending_issue_id(entry.entry_id)
    assert issue_registry.async_get_issue(DOMAIN, pending_id) is not None

    # Checked too early: the issue stays.
    flow = await start_repair_fix_flow(repairs_client, DOMAIN, pending_id)
    assert flow["step_id"] == "confirm"
    assert flow["description_placeholders"]["lfdi"] == format_lfdi(new.lfdi)
    flow = await process_repair_fix_flow(repairs_client, flow["flow_id"], json={})
    assert flow["type"] == "abort"
    assert flow["reason"] == "still_unregistered"
    assert issue_registry.async_get_issue(DOMAIN, pending_id) is not None

    # Registered.
    renewed_meter.probe_tls.return_value = peer()
    flow = await start_repair_fix_flow(repairs_client, DOMAIN, pending_id)
    flow = await process_repair_fix_flow(repairs_client, flow["flow_id"], json={})
    assert flow["type"] == "create_entry"
    await hass.async_block_till_done()
    assert entry.data[CONF_IDENTITY] == new.to_dict()
    assert entry.state is ConfigEntryState.LOADED
    assert not issue_registry.issues


async def test_importing_from_the_expiry_reminder(
    hass: HomeAssistant,
    renewed_meter: AsyncMock,
    meters: Callable[[Identity], None],
    repairs_client: TestClient,
    issue_registry: ir.IssueRegistry,
) -> None:
    """A registered certificate imported from the reminder takes over at once."""
    current = generate_identity(valid_days=30)
    entry = meter_entry(current)
    await setup_integration(hass, entry)
    expiring_id = certificate_expiring_issue_id(entry.entry_id)
    new = generate_identity()
    meters(new)
    renewed_meter.probe_tls.return_value = peer()

    async def import_from_reminder(cert: str, key: str) -> dict[str, Any]:
        flow = await start_repair_fix_flow(repairs_client, DOMAIN, expiring_id)
        flow = await process_repair_fix_flow(
            repairs_client, flow["flow_id"], json={"next_step_id": "import_identity"}
        )
        assert flow["step_id"] == "import_identity"
        return await process_repair_fix_flow(
            repairs_client,
            flow["flow_id"],
            json={CONF_CERTIFICATE: cert, CONF_PRIVATE_KEY: key},
        )

    flow = await import_from_reminder(current.cert_pem, current.key_pem)
    assert flow["type"] == "abort"
    assert flow["reason"] == "same_identity"

    flow = await import_from_reminder("not a certificate", new.key_pem)
    assert flow["type"] == "form"
    assert flow["errors"] == {"base": "invalid_identity"}
    flow = await process_repair_fix_flow(
        repairs_client,
        flow["flow_id"],
        json={CONF_CERTIFICATE: new.cert_pem, CONF_PRIVATE_KEY: new.key_pem},
    )
    assert flow["type"] == "create_entry"
    await hass.async_block_till_done()
    assert entry.data[CONF_IDENTITY] == new.to_dict()
    assert not issue_registry.issues


@pytest.mark.parametrize(
    ("issue_id", "translation_key"),
    [
        pytest.param(renewal_pending_issue_id("gone"), "renewal_pending", id="pending"),
        pytest.param(
            certificate_expiring_issue_id("gone"), "certificate_expiring", id="reminder"
        ),
    ],
)
@pytest.mark.parametrize(
    "data",
    [
        pytest.param({"entry_id": "gone"}, id="removed-entry"),
        pytest.param(None, id="no-data"),
    ],
)
async def test_renewal_fix_flows_without_their_entry(
    hass: HomeAssistant,
    repairs_client: TestClient,
    issue_id: str,
    translation_key: str,
    data: dict[str, str] | None,
) -> None:
    """A renewal issue left behind by its entry (or without data) just closes."""
    assert await async_setup_component(hass, DOMAIN, {})
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key=translation_key,
        data=data,
    )
    flow = await start_repair_fix_flow(repairs_client, DOMAIN, issue_id)
    assert flow["type"] == "create_entry"


@pytest.mark.parametrize(
    "data",
    [
        pytest.param({"entry_id": "gone"}, id="removed-entry"),
        pytest.param(None, id="no-data"),
    ],
)
async def test_registration_fix_flow_without_its_entry(
    hass: HomeAssistant,
    repairs_client: TestClient,
    data: dict[str, str] | None,
) -> None:
    """The registration issue left behind by its entry closes when checked."""
    assert await async_setup_component(hass, DOMAIN, {})
    issue_id = awaiting_registration_issue_id("gone")
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="awaiting_registration",
        data=data,
    )
    flow = await start_repair_fix_flow(repairs_client, DOMAIN, issue_id)
    assert flow["step_id"] == "confirm"
    flow = await process_repair_fix_flow(repairs_client, flow["flow_id"], json={})
    assert flow["type"] == "create_entry"


async def test_fix_flow_for_a_finished_renewal(
    hass: HomeAssistant,
    meters: Callable[[Identity], None],
    identity: Identity,
    repairs_client: TestClient,
) -> None:
    """The renewal's issue was opened, then the renewal was discarded."""
    new = generate_identity()
    meters(new)
    entry = pending_entry(identity, new)
    await setup_integration(hass, entry)
    pending_id = renewal_pending_issue_id(entry.entry_id)
    flow = await start_repair_fix_flow(repairs_client, DOMAIN, pending_id)
    assert flow["step_id"] == "confirm"
    result = await discard_from_reconfigure(hass, entry)
    assert result["reason"] == "renewal_discarded"
    flow = await process_repair_fix_flow(repairs_client, flow["flow_id"], json={})
    assert flow["type"] == "create_entry"


@pytest.mark.usefixtures("mock_meter")
async def test_reminder_flow_for_a_removed_entry(
    hass: HomeAssistant, repairs_client: TestClient
) -> None:
    """The entry is removed while the reminder's menu is open."""
    entry = meter_entry(generate_identity(valid_days=30))
    await setup_integration(hass, entry)
    expiring_id = certificate_expiring_issue_id(entry.entry_id)
    flow = await start_repair_fix_flow(repairs_client, DOMAIN, expiring_id)
    await hass.config_entries.async_remove(entry.entry_id)
    flow = await process_repair_fix_flow(
        repairs_client, flow["flow_id"], json={"next_step_id": "generate_identity"}
    )
    assert flow["type"] == "create_entry"


async def test_no_reminder_for_an_unregistered_certificate(
    hass: HomeAssistant, mock_meter: AsyncMock, issue_registry: ir.IssueRegistry
) -> None:
    """A certificate still waiting for registration has its own issue."""
    entry = meter_entry(generate_identity(valid_days=30), **{CONF_REGISTERED: False})
    mock_meter.discover.side_effect = MeterAuthError("refused")
    await setup_integration(hass, entry)
    assert list(issue_registry.issues) == [
        (DOMAIN, awaiting_registration_issue_id(entry.entry_id))
    ]


@pytest.mark.usefixtures("mock_meter")
async def test_a_broken_renewal_keeps_the_reminder(
    hass: HomeAssistant,
    issue_registry: ir.IssueRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A pending certificate that cannot be loaded is no renewal."""
    caplog.set_level(logging.DEBUG, logger="custom_components.ieee_2030_5")
    entry = meter_entry(
        generate_identity(valid_days=30), **{CONF_PENDING_IDENTITY: BROKEN_IDENTITY}
    )
    await setup_integration(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert list(issue_registry.issues) == [
        (DOMAIN, certificate_expiring_issue_id(entry.entry_id))
    ]
    assert "The pending client certificate cannot be used" in caplog.text


async def test_an_expired_renewal_gives_the_reminder_back(
    hass: HomeAssistant,
    renewed_meter: AsyncMock,
    meters: Callable[[Identity], None],
    issue_registry: ir.IssueRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A pending certificate that expired is no renewal either.

    It is never tried, and no longer asked to be registered; the current
    certificate's reminder is back, so it does not expire unannounced.
    """
    current = generate_identity(valid_days=40)
    short = generate_identity(valid_days=2)  # e.g. imported near its end
    meters(short)
    entry = pending_entry(current, short)
    await setup_integration(hass, entry)
    assert list(issue_registry.issues) == [
        (DOMAIN, renewal_pending_issue_id(entry.entry_id))
    ]
    freezer.move_to(short.not_after + timedelta(days=1))
    await poll(hass, freezer, SLOW_POLLS)
    renewed_meter.probe_tls.assert_not_awaited()
    assert list(issue_registry.issues) == [
        (DOMAIN, certificate_expiring_issue_id(entry.entry_id))
    ]
    assert entry.data[CONF_PENDING_IDENTITY] == short.to_dict()  # kept to discard


@pytest.mark.usefixtures("mock_meter")
async def test_the_reminder_goes_when_the_certificate_expires(
    hass: HomeAssistant,
    issue_registry: ir.IssueRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """An expired certificate is left to reauthentication, without the reminder."""
    current = generate_identity(valid_days=30)
    entry = meter_entry(current)
    await setup_integration(hass, entry)
    expiring_id = certificate_expiring_issue_id(entry.entry_id)
    assert issue_registry.async_get_issue(DOMAIN, expiring_id) is not None

    freezer.move_to(current.not_after + timedelta(hours=1))
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.error_reason_translation_key == "certificate_expired"
    assert issue_registry.async_get_issue(DOMAIN, expiring_id) is None


async def test_the_reminder_goes_with_a_replaced_certificate(
    hass: HomeAssistant,
    mock_meter: AsyncMock,
    issue_registry: ir.IssueRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Reauthentication replaces the certificate: its reminder goes with it.

    The new certificate awaits registration; the reminder would otherwise
    announce the replaced one's expiry, and stage a "renewal" for an entry
    that is not even registered.
    """
    current = generate_identity(valid_days=30)
    entry = meter_entry(current)
    await setup_integration(hass, entry)
    expiring_id = certificate_expiring_issue_id(entry.entry_id)
    assert issue_registry.async_get_issue(DOMAIN, expiring_id) is not None

    mock_meter.read.side_effect = MeterAuthError("refused")
    mock_meter.probe_tls.return_value = peer(accepted=False)
    await poll(hass, freezer)
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]
    new = generate_identity()
    with patch(
        "custom_components.ieee_2030_5.config_flow.generate_identity",
        return_value=new,
    ):
        result = await hass.config_entries.flow.async_configure(
            flows[0]["flow_id"], {"next_step_id": "generate_identity"}
        )
    assert result["reason"] == "register_certificate"
    await hass.async_block_till_done()
    assert lfdi_of(entry.data[CONF_IDENTITY]) == new.lfdi
    assert entry.error_reason_translation_key == "awaiting_registration"
    assert list(issue_registry.issues) == [
        (DOMAIN, awaiting_registration_issue_id(entry.entry_id))
    ]


async def test_renewing_a_broken_stored_certificate(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Reconfigure cannot renew what it cannot read."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        data={**mock_config_entry.data, CONF_IDENTITY: BROKEN_IDENTITY},
    )
    result = await mock_config_entry.start_reconfigure_flow(hass)
    assert result["menu_options"] == ["address", "renew_identity"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "renew_identity"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "invalid_identity"


async def test_a_renewal_outlives_a_broken_stored_certificate(
    hass: HomeAssistant,
    meters: Callable[[Identity], None],
    identity: Identity,
    repairs_client: TestClient,
    issue_registry: ir.IssueRegistry,
) -> None:
    """The stored certificate breaks while a renewal is pending.

    The renewal still shows its LFDI (the expiry is unknown); discarding it
    leaves no reminder for a certificate that cannot be read.
    """
    new = generate_identity()
    meters(new)
    entry = pending_entry(identity, new)
    await setup_integration(hass, entry)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_IDENTITY: BROKEN_IDENTITY}
    )
    pending_id = renewal_pending_issue_id(entry.entry_id)
    flow = await start_repair_fix_flow(repairs_client, DOMAIN, pending_id)
    assert flow["step_id"] == "confirm"
    assert flow["description_placeholders"]["lfdi"] == format_lfdi(new.lfdi)
    assert flow["description_placeholders"]["expiry"] == ""

    result = await discard_from_reconfigure(hass, entry)
    assert result["reason"] == "renewal_discarded"
    assert not issue_registry.issues


@pytest.mark.usefixtures("mock_meter")
async def test_reminder_fix_flow_with_a_broken_stored_certificate(
    hass: HomeAssistant, repairs_client: TestClient
) -> None:
    """The stored certificate breaks while its reminder is open."""
    entry = meter_entry(generate_identity(valid_days=30))
    await setup_integration(hass, entry)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_IDENTITY: BROKEN_IDENTITY}
    )
    expiring_id = certificate_expiring_issue_id(entry.entry_id)
    flow = await start_repair_fix_flow(repairs_client, DOMAIN, expiring_id)
    flow = await process_repair_fix_flow(
        repairs_client, flow["flow_id"], json={"next_step_id": "generate_identity"}
    )
    assert flow["type"] == "abort"
    assert flow["reason"] == "invalid_identity"
    assert CONF_PENDING_IDENTITY not in entry.data
