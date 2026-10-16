"""Constants for the IEEE 2030.5 Smart Meter integration."""

from datetime import timedelta
import logging

from aio2030dot5 import Timeouts

DOMAIN = "ieee_2030_5"
LOGGER = logging.getLogger(__package__)

CONF_CERTIFICATE = "certificate"
CONF_DCAP_PATH = "dcap_path"
CONF_IDENTITY = "identity"
CONF_METER_LFDI = "meter_lfdi"
CONF_METER_SFDI = "meter_sfdi"
CONF_PRIVATE_KEY = "private_key"
# A renewed certificate waiting for the utility to register it.
CONF_PENDING_IDENTITY = "pending_identity"
CONF_REGISTERED = "registered"

DEFAULT_PORT = 8081
DEFAULT_DCAP_PATH = "/dcap"

# One connection a minute: instantaneous demand and the import/export
# registers. Everything else rides along every SLOW_INTERVAL. The meter
# serves a single connection at a time, so all reads share one batch.
SCAN_INTERVAL = timedelta(seconds=60)
SLOW_INTERVAL = timedelta(minutes=15)

# A call that holds the meter longer than this has failed. The deadline
# starts once the meter's single connection slot is ours.
POLL_TIMEOUTS = Timeouts(connect=10, handshake=20, response=20, batch=90)
# A config flow step waits for the slot (a poll may hold it) and then probes.
FLOW_TIMEOUTS = Timeouts(connect=10, handshake=20, response=20, batch=30)
FLOW_STEP_TIMEOUT = 150

# Where a utility's customers register a client's LFDI; shown as examples
# next to the LFDI.
UTILITY_PLACEHOLDERS = {
    "xcel_url": (
        "https://my.xcelenergy.com/MyAccount/s/meters-and-devices/"
        "manage-meters-and-devices"
    ),
}

TOU_DELIVERED = "energy_tou_summation_delivered"
TOU_RECEIVED = "energy_tou_summation_received"


def awaiting_registration_issue_id(entry_id: str) -> str:
    """The repair issue shown until the utility registers the certificate."""
    return f"awaiting_registration_{entry_id}"


def certificate_expiring_issue_id(entry_id: str) -> str:
    """The repair issue asking to renew a certificate that expires soon."""
    return f"certificate_expiring_{entry_id}"


def renewal_pending_issue_id(entry_id: str) -> str:
    """The repair issue shown until the utility registers a renewal."""
    return f"renewal_pending_{entry_id}"
