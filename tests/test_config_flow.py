"""Tests for the IEEE 2030.5 Smart Meter config flow."""

from datetime import timedelta
from ipaddress import ip_address
from typing import Any
from unittest.mock import AsyncMock

from aio2030dot5 import (
    Identity,
    MeterAuthError,
    MeterConnectionError,
    PeerInfo,
    format_lfdi,
    generate_identity,
)
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import (
    SOURCE_IGNORE,
    SOURCE_USER,
    SOURCE_ZEROCONF,
    ConfigFlowResult,
)
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ieee_2030_5.const import (
    CONF_CERTIFICATE,
    CONF_DCAP_PATH,
    CONF_IDENTITY,
    CONF_METER_LFDI,
    CONF_METER_SFDI,
    CONF_PENDING_IDENTITY,
    CONF_PRIVATE_KEY,
    CONF_REGISTERED,
    DOMAIN,
    UTILITY_PLACEHOLDERS,
)

from . import (
    BROKEN_IDENTITY,
    MOCK_HOST,
    MOCK_LFDI,
    MOCK_PORT,
    MOCK_SFDI,
    meter_entry,
    peer,
)

pytestmark = pytest.mark.usefixtures("mock_setup_entry")

OTHER_LFDI = MOCK_LFDI[:-1] + "0"  # same SFDI, another certificate


def zeroconf_info(
    host: str = MOCK_HOST,
    name: str = f"itrondnsserver-{MOCK_SFDI}._smartenergy._tcp.local.",
    properties: dict[str, str] | None = None,
) -> ZeroconfServiceInfo:
    """What zeroconf reports for an Itron meter."""
    return ZeroconfServiceInfo(
        ip_address=ip_address(host),
        ip_addresses=[ip_address(host)],
        hostname=f"itrondnsserver-{MOCK_SFDI}.local.",
        name=name,
        port=MOCK_PORT,
        type="_smartenergy._tcp.local.",
        properties=properties
        if properties is not None
        else {"txtvers": "1", "dcap": "/dcap", "level": "-S0", "https": "8081"},
    )


async def _to_identity_menu(hass: HomeAssistant) -> str:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: MOCK_HOST, CONF_PORT: MOCK_PORT}
    )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "identity"
    return result["flow_id"]


@pytest.mark.usefixtures("mock_meter")
async def test_user_import_registered_identity(
    hass: HomeAssistant, mock_meter_create: AsyncMock, identity: Identity
) -> None:
    """A certificate the utility already registered: the entry is ready."""
    flow_id = await _to_identity_menu(hass)
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "import_identity"}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {CONF_CERTIFICATE: identity.cert_pem, CONF_PRIVATE_KEY: identity.key_pem},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == f"Electric meter {MOCK_SFDI}"
    assert result["result"].unique_id == MOCK_SFDI
    assert result["description"] is None
    assert result["data"] == {
        CONF_HOST: MOCK_HOST,
        CONF_PORT: MOCK_PORT,
        CONF_DCAP_PATH: "/dcap",
        CONF_METER_SFDI: MOCK_SFDI,
        CONF_METER_LFDI: MOCK_LFDI,
        CONF_IDENTITY: identity.to_dict(),
        CONF_REGISTERED: True,
    }
    assert mock_meter_create.await_args.args[0] == MOCK_HOST


async def test_user_generate_identity_to_register(
    hass: HomeAssistant, mock_meter: AsyncMock
) -> None:
    """A new certificate: the entry is kept at once, with the LFDI to register."""
    flow_id = await _to_identity_menu(hass)
    mock_meter.probe_tls.return_value = peer(accepted=False)
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "generate_identity"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["description"] == "register_certificate"
    lfdi = result["description_placeholders"]["lfdi"]
    # As Xcel Energy's portal accepts it: eight groups of five.
    assert [len(group) for group in lfdi.split("-")] == [5] * 8
    assert result["data"][CONF_REGISTERED] is False
    stored = Identity.from_dict(result["data"][CONF_IDENTITY])
    assert stored.lfdi == lfdi.replace("-", "")


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        pytest.param(MeterConnectionError("down"), "cannot_connect", id="unreachable"),
        pytest.param(MeterAuthError("refused"), "cannot_identify", id="unproven"),
        pytest.param(TimeoutError, "cannot_connect", id="slot-never-free"),
    ],
)
async def test_user_errors_recover(
    hass: HomeAssistant, mock_meter: AsyncMock, error: Exception, reason: str
) -> None:
    """Identifying the meter fails, then works."""
    mock_meter.probe_tls.side_effect = error
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: MOCK_HOST, CONF_PORT: MOCK_PORT}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": reason}
    mock_meter.probe_tls.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: MOCK_HOST, CONF_PORT: MOCK_PORT}
    )
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "generate_identity"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOST] == MOCK_HOST


@pytest.mark.usefixtures("mock_meter")
async def test_user_already_configured(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """The same meter (by its certificate) at a new address updates the entry."""
    mock_config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "192.0.2.20", CONF_PORT: MOCK_PORT}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert mock_config_entry.data[CONF_HOST] == "192.0.2.20"


async def test_user_same_sfdi_other_certificate(
    hass: HomeAssistant, mock_meter: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """A device claiming the configured meter's SFDI does not take its entry over."""
    mock_config_entry.add_to_hass(hass)
    mock_meter.probe_tls.return_value = peer(lfdi=OTHER_LFDI)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "192.0.2.20", CONF_PORT: MOCK_PORT}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_device"
    assert mock_config_entry.data[CONF_HOST] == MOCK_HOST


@pytest.mark.usefixtures("mock_meter")
async def test_import_invalid_identity_then_valid(
    hass: HomeAssistant, identity: Identity
) -> None:
    """Problems with an imported certificate are shown; a good one goes through."""
    flow_id = await _to_identity_menu(hass)
    await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "import_identity"}
    )
    other = generate_identity()
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {CONF_CERTIFICATE: identity.cert_pem, CONF_PRIVATE_KEY: other.key_pem},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_identity"}
    assert "does not match" in result["description_placeholders"]["problems"]
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {CONF_CERTIFICATE: identity.cert_pem, CONF_PRIVATE_KEY: identity.key_pem},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        pytest.param(MeterConnectionError("down"), "cannot_connect", id="unreachable"),
        pytest.param(MeterAuthError("refused"), "cannot_identify", id="unproven"),
    ],
)
async def test_connect_errors_can_be_retried(
    hass: HomeAssistant, mock_meter: AsyncMock, error: Exception, reason: str
) -> None:
    """The certificate is kept while the meter is retried."""
    flow_id = await _to_identity_menu(hass)
    mock_meter.probe_tls.side_effect = error
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "generate_identity"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "connect"
    assert result["errors"] == {"base": reason}
    mock_meter.probe_tls.side_effect = None
    result = await hass.config_entries.flow.async_configure(flow_id, {})
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.parametrize(
    "probe",
    [
        pytest.param(peer(sfdi="123456789014"), id="another-sfdi"),
        pytest.param(peer(lfdi=OTHER_LFDI), id="same-sfdi-other-certificate"),
    ],
)
async def test_connect_to_another_device(
    hass: HomeAssistant, mock_meter: AsyncMock, probe: object
) -> None:
    """The device answering now is not the one identified first."""
    flow_id = await _to_identity_menu(hass)
    mock_meter.probe_tls.return_value = probe
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "generate_identity"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_device"


async def test_zeroconf_new_meter(
    hass: HomeAssistant, mock_meter: AsyncMock, mock_meter_create: AsyncMock
) -> None:
    """A discovered meter is confirmed, then set up like a manual one."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_ZEROCONF},
        data=zeroconf_info(properties={"txtvers": "1", "dcap": "/sep2/dcap"}),
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "discovery_confirm"
    assert result["description_placeholders"] == {"sfdi": MOCK_SFDI, "host": MOCK_HOST}
    mock_meter.probe_tls.assert_not_called()  # the SFDI came from the mDNS name
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "generate_identity"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_DCAP_PATH] == "/sep2/dcap"
    assert result["data"][CONF_HOST] == MOCK_HOST
    assert mock_meter_create.await_args.kwargs["dcap_path"] == "/sep2/dcap"


@pytest.mark.parametrize(
    "info",
    [
        pytest.param(zeroconf_info(properties={"txtvers": "2"}), id="unknown-txtvers"),
        pytest.param(
            zeroconf_info(name="meter._smartenergy._tcp.local."), id="no-sfdi"
        ),
    ],
)
@pytest.mark.usefixtures("mock_meter")
async def test_zeroconf_not_a_meter(
    hass: HomeAssistant, info: ZeroconfServiceInfo
) -> None:
    """Adverts that cannot be used are ignored."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=info
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_ieee_2030_5"


async def test_zeroconf_configured_same_address(
    hass: HomeAssistant, mock_meter: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """The configured meter, where it was: nothing to do, nothing probed."""
    mock_config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=zeroconf_info()
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    mock_meter.probe_tls.assert_not_called()


async def test_zeroconf_ignored_meter(
    hass: HomeAssistant, mock_meter: AsyncMock
) -> None:
    """A meter the user chose to ignore stays ignored."""
    MockConfigEntry(
        domain=DOMAIN, unique_id=MOCK_SFDI, source=SOURCE_IGNORE, data={}
    ).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=zeroconf_info("192.0.2.77")
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    mock_meter.probe_tls.assert_not_called()


@pytest.mark.parametrize(
    ("answer", "error", "host"),
    [
        pytest.param(peer(), None, "192.0.2.77", id="the-meter-moved"),
        pytest.param(
            peer(lfdi=OTHER_LFDI), None, MOCK_HOST, id="an-impostor-claims-its-sfdi"
        ),
        pytest.param(None, MeterConnectionError("down"), MOCK_HOST, id="unverifiable"),
    ],
)
async def test_zeroconf_configured_new_address(
    hass: HomeAssistant,
    mock_meter: AsyncMock,
    mock_meter_create: AsyncMock,
    mock_config_entry: MockConfigEntry,
    answer: PeerInfo | None,
    error: Exception | None,
    host: str,
) -> None:
    """A new address is adopted only once the device there proves to be the meter."""
    mock_config_entry.add_to_hass(hass)
    mock_meter.probe_tls.return_value = answer
    mock_meter.probe_tls.side_effect = error
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=zeroconf_info("192.0.2.77")
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert mock_meter_create.await_args.args[0] == "192.0.2.77"  # probed there
    assert mock_config_entry.data[CONF_HOST] == host


async def _reauth_menu(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "reauth_confirm"
    assert result["description_placeholders"] == UTILITY_PLACEHOLDERS  # the portal link
    return result["flow_id"]


@pytest.mark.usefixtures("mock_meter")
async def test_reauth_retry_accepted(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """The current certificate works again (re-registered)."""
    mock_config_entry.add_to_hass(hass)
    flow_id = await _reauth_menu(hass, mock_config_entry)
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "reauth_retry"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_REGISTERED] is True


async def test_reauth_new_identity_to_register(
    hass: HomeAssistant, mock_meter: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """A new certificate replaces the old and waits for registration."""
    mock_config_entry.add_to_hass(hass)
    old = mock_config_entry.data[CONF_IDENTITY]
    flow_id = await _reauth_menu(hass, mock_config_entry)
    mock_meter.probe_tls.return_value = peer(accepted=False)
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "generate_identity"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "register_certificate"
    assert "lfdi" in result["description_placeholders"]
    assert mock_config_entry.data[CONF_REGISTERED] is False
    assert mock_config_entry.data[CONF_IDENTITY] != old


async def test_reauth_another_meter(
    hass: HomeAssistant, mock_meter: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """A device with the same SFDI but another certificate is not the meter."""
    mock_config_entry.add_to_hass(hass)
    flow_id = await _reauth_menu(hass, mock_config_entry)
    mock_meter.probe_tls.return_value = peer(lfdi=OTHER_LFDI)
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "reauth_retry"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_device"


@pytest.mark.usefixtures("mock_meter")
async def test_reauth_retry_invalid_identity(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """A stored certificate that cannot be loaded offers nothing to retry."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        data={
            **mock_config_entry.data,
            CONF_IDENTITY: BROKEN_IDENTITY,
        },
    )
    flow_id = await _reauth_menu(hass, mock_config_entry)
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "reauth_retry"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "invalid_identity"


async def test_reauth_retry_expired_identity(
    hass: HomeAssistant,
    mock_meter: AsyncMock,
    mock_config_entry: MockConfigEntry,
    identity: Identity,
    freezer: FrozenDateTimeFactory,
) -> None:
    """An expired certificate cannot be retried: a new one is needed."""
    mock_config_entry.add_to_hass(hass)
    flow_id = await _reauth_menu(hass, mock_config_entry)
    freezer.move_to(identity.not_after + timedelta(days=1))
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "reauth_retry"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "certificate_expired"
    mock_meter.probe_tls.assert_not_called()


async def _reconfigure(
    hass: HomeAssistant, entry: MockConfigEntry, option: str
) -> ConfigFlowResult:
    """Start reconfiguring and pick a menu option."""
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.MENU
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": option}
    )


@pytest.mark.usefixtures("mock_meter")
async def test_reconfigure(
    hass: HomeAssistant,
    mock_meter_create: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The meter's address changes, verified by its certificate."""
    mock_config_entry.add_to_hass(hass)
    result = await _reconfigure(hass, mock_config_entry, "address")
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "192.0.2.50", CONF_PORT: 8443}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert mock_config_entry.data[CONF_HOST] == "192.0.2.50"
    assert mock_config_entry.data[CONF_PORT] == 8443
    assert mock_meter_create.await_args.args[0] == "192.0.2.50"


@pytest.mark.parametrize(
    "probe",
    [
        pytest.param(peer(sfdi="123456789014"), id="another-sfdi"),
        pytest.param(peer(lfdi=OTHER_LFDI), id="same-sfdi-other-certificate"),
    ],
)
async def test_reconfigure_wrong_device(
    hass: HomeAssistant,
    mock_meter: AsyncMock,
    mock_config_entry: MockConfigEntry,
    probe: object,
) -> None:
    """Pointing the entry at another meter is refused."""
    mock_config_entry.add_to_hass(hass)
    mock_meter.probe_tls.return_value = probe
    result = await _reconfigure(hass, mock_config_entry, "address")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "192.0.2.50", CONF_PORT: MOCK_PORT}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_device"
    assert mock_config_entry.data[CONF_HOST] == MOCK_HOST


@pytest.mark.usefixtures("mock_meter")
async def test_reconfigure_invalid_identity(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """A broken stored certificate is not reported as a connection problem."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        data={
            **mock_config_entry.data,
            CONF_IDENTITY: BROKEN_IDENTITY,
        },
    )
    result = await _reconfigure(hass, mock_config_entry, "address")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "192.0.2.50", CONF_PORT: MOCK_PORT}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "invalid_identity"


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        pytest.param(MeterConnectionError("down"), "cannot_connect", id="unreachable"),
        pytest.param(MeterAuthError("refused"), "cannot_identify", id="unproven"),
    ],
)
async def test_reconfigure_errors_recover(
    hass: HomeAssistant,
    mock_meter: AsyncMock,
    mock_config_entry: MockConfigEntry,
    error: Exception,
    reason: str,
) -> None:
    """Errors are shown on the form, and the flow can still finish."""
    mock_config_entry.add_to_hass(hass)
    mock_meter.probe_tls.side_effect = error
    result = await _reconfigure(hass, mock_config_entry, "address")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "192.0.2.50", CONF_PORT: MOCK_PORT}
    )
    assert result["errors"] == {"base": reason}
    mock_meter.probe_tls.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "192.0.2.50", CONF_PORT: MOCK_PORT}
    )
    assert result["reason"] == "reconfigure_successful"


async def test_user_flow_while_the_meter_is_discovered(
    hass: HomeAssistant, mock_meter: AsyncMock
) -> None:
    """Setting the meter up by hand works while its discovery waits to be confirmed."""
    discovered = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=zeroconf_info()
    )
    assert discovered["type"] is FlowResultType.FORM
    flow_id = await _to_identity_menu(hass)
    mock_meter.probe_tls.return_value = peer(accepted=False)
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "generate_identity"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == MOCK_SFDI
    # The discovery of the same meter is done with.
    assert not hass.config_entries.flow.async_progress()


@pytest.mark.usefixtures("mock_meter")
async def test_user_flow_for_an_ignored_meter(hass: HomeAssistant) -> None:
    """A meter the user ignored can be set up by hand; the ignored entry is not changed."""
    ignored = MockConfigEntry(
        domain=DOMAIN, unique_id=MOCK_SFDI, source=SOURCE_IGNORE, data={}
    )
    ignored.add_to_hass(hass)
    flow_id = await _to_identity_menu(hass)
    assert ignored.data == {}
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "generate_identity"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entries = hass.config_entries.async_entries(DOMAIN, include_ignore=True)
    assert [e.source for e in entries] == [SOURCE_USER]


# Generated now; expired once a test moves two days ahead.
SHORT_LIVED = generate_identity(valid_days=1)


@pytest.mark.parametrize(
    ("renewal", "options"),
    [
        pytest.param({}, ["address", "renew_identity"], id="none"),
        pytest.param(
            {CONF_PENDING_IDENTITY: generate_identity().to_dict()},
            ["address", "discard_renewal"],
            id="pending",
        ),
        pytest.param(
            {CONF_PENDING_IDENTITY: SHORT_LIVED.to_dict()},
            ["address", "renew_identity", "discard_renewal"],
            id="expired",
        ),
        pytest.param(
            {CONF_PENDING_IDENTITY: BROKEN_IDENTITY},
            ["address", "renew_identity", "discard_renewal"],
            id="broken",
        ),
    ],
)
async def test_reconfigure_menu(
    hass: HomeAssistant,
    identity: Identity,
    freezer: FrozenDateTimeFactory,
    renewal: dict[str, Any],
    options: list[str],
) -> None:
    """One renewal at a time; one that cannot be used can be replaced or dropped."""
    freezer.tick(timedelta(days=2))
    entry = meter_entry(identity, **renewal)
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == options


async def test_discarding_an_expired_renewal_shows_its_lfdi(
    hass: HomeAssistant, identity: Identity, freezer: FrozenDateTimeFactory
) -> None:
    """The confirmation names the certificate that is about to be deleted."""
    freezer.tick(timedelta(days=2))
    entry = meter_entry(identity, **{CONF_PENDING_IDENTITY: SHORT_LIVED.to_dict()})
    entry.add_to_hass(hass)
    result = await _reconfigure(hass, entry, "discard_renewal")
    assert result["type"] is FlowResultType.FORM
    assert result["description_placeholders"]["lfdi"] == format_lfdi(SHORT_LIVED.lfdi)
