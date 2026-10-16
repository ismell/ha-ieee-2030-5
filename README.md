# IEEE 2030.5 Smart Meter for Home Assistant

[![Open your Home Assistant instance and open this repository in the Marketplace.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=ismell&repository=ha-ieee-2030-5&category=integration)

Read your electric utility meter directly over your local network, using the
[IEEE 2030.5](https://standards.ieee.org/ieee/2030.5/11216/) protocol (also known as Smart Energy Profile 2.0). The
integration shows how much power your home is drawing from or sending to the grid right now, along with the meter's own
energy totals, so you can use the meter as the grid source of the energy dashboard.

Some utilities let customers connect their smart meter to their home Wi-Fi and register devices that may read it. Xcel
Energy, for example, offers this for its Itron Riva meters. Readings come straight from the meter: no cloud service or
utility account access is involved after registration.

This is a custom integration, distributed through the Home Assistant Marketplace (HACS before Home Assistant 2026.11).
It is not affiliated with or endorsed by Xcel Energy, Itron, or the IEEE.

## Supported devices

- Itron Gen5 Riva meters on Xcel Energy's network (firmware 3.2.50 has been tested).

Other meters that serve the IEEE 2030.5 metering function set over TLS should work too: the
[aio2030dot5](https://github.com/ismell/aio2030dot5) library this integration uses is also tested against an
independent, open-source IEEE 2030.5 server. Automatic discovery recognizes Itron meters only; add other meters
manually.

## Prerequisites

- **The meter must be connected to your network.** For Xcel Energy, connect the meter to your Wi-Fi under
  [Meters and devices](https://my.xcelenergy.com/MyAccount/s/meters-and-devices/manage-meters-and-devices) in My Account.
- **A client certificate registered with your utility.** The meter only talks to clients whose certificate the utility
  has registered. Home Assistant can generate one during setup and shows you its **LFDI** (a fingerprint of 40
  hexadecimal digits) to register; you can also import a certificate you have already registered. Registration can take
  a few days.
- **Home Assistant must be able to reach the meter** on TCP port 8081. If the meter is on a separate network (for
  example, an IoT VLAN), allow traffic from Home Assistant to the meter, and forward mDNS between the networks if you
  want automatic discovery.
- **Nothing else may be reading the meter.** The meter serves a single client at a time. Stop any other app, add-on,
  or device that reads it locally before you set up this integration.

## Installation

1. Open this repository in the Marketplace with the button above (or, before Home Assistant 2026.11, in HACS:
   **HACS** > **⋮** > **Custom repositories**, add `https://github.com/ismell/ha-ieee-2030-5` as an **Integration**).
2. Install **IEEE 2030.5 Smart Meter**, and restart Home Assistant if asked.
3. An Itron meter advertises itself on your network, so it is usually discovered: you then only confirm the setup and
   choose a certificate. Otherwise, add it manually:

   [![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=ieee_2030_5)

### Configuration parameters

- **Host**: the hostname or IP address of the meter. Only needed when the meter is not discovered automatically.
- **Port**: the meter's HTTPS port. Itron meters use 8081.
- **Certificate**: when importing a certificate, the client certificate in PEM format.
- **Private key**: when importing a certificate, its unencrypted private key in PEM format.

### Registering a new certificate

If you choose **Generate a new certificate**, Home Assistant creates the integration right away and shows the
certificate's LFDI, for example `431ED-86C1F-202CD-D3FEB-3EF8D-CB9F4-457F0-EC47D`.

1. Register that LFDI with your utility, exactly as shown (Xcel Energy accepts it in groups of five, or without
   dashes). For Xcel Energy, add a device under
   [Meters and devices](https://my.xcelenergy.com/MyAccount/s/meters-and-devices/manage-meters-and-devices).
2. Until the utility has registered it, the meter refuses the connection. A repair issue under **Settings** >
   **System** > **Repairs** shows the LFDI again, and Home Assistant keeps checking on its own.
3. Once the meter accepts the certificate, the sensors appear and the repair issue disappears. To check right away,
   open the repair issue and select **Submit**.

The private key never leaves Home Assistant. It is stored with the integration's configuration and removed together
with it.

### Identifying the meter

The integration identifies the meter by its own certificate, not by its IP address, so it never shows readings from a
different device as if they came from your meter. If an Itron meter's address changes, Home Assistant finds it again
through discovery; for other meters, see [The meter moved to a new address](#the-meter-moved-to-a-new-address).

### Renewing the certificate

Client certificates are typically valid for three years, and registering a new one can take a few days. Renew in time,
and readings continue without a gap:

1. Sixty days before the certificate expires, a repair issue asks you to renew it. You can also start any time: select
   **Reconfigure** on the integration, then **Renew the client certificate**.
2. Generate a new certificate, or import one you have already registered.
3. Register the new certificate's LFDI with your utility. Until the meter accepts it, Home Assistant keeps using the
   current certificate, and a repair issue shows the new LFDI.
4. Home Assistant checks the new certificate every 15 minutes, using one extra connection to the meter, and switches to
   it on its own once the meter accepts it. To check right away, open the repair issue and select **Submit**.

If the utility retires the old certificate before the new one is registered, readings pause, but Home Assistant does
not ask you to reauthenticate: it keeps checking the new certificate every 15 minutes and switches as soon as the meter
accepts it.

Only one renewal can wait at a time. To replace it, select **Reconfigure**, then **Discard the pending certificate**,
and confirm: this deletes its private key, so a registration you made for its LFDI can no longer be used.

## Sensors

The integration creates a device for the meter with these sensors:

- **Power** (W): the power currently drawn from the grid. Negative while your home sends power to the grid, for
  example from solar panels.
- **Energy imported** (Wh): the meter's total of energy taken from the grid.
- **Energy exported** (Wh): the meter's total of energy sent to the grid.
- **Energy imported tier _N_** (Wh): if your meter keeps time-of-use registers, the energy imported in each pricing
  tier. Which tier is which period (for example, on-peak) is up to your utility. A tier that starts counting later,
  such as another season's rate, gets its sensor on its own.
- **Client certificate expiry** (diagnostic): when the client certificate expires.

These sensors are disabled by default. Enable them if your meter provides them and you want to use them:

- **Energy exported tier _N_** (Wh)
- **Reactive energy imported** and **Reactive energy exported** (varh)
- **Apparent energy imported** and **Apparent energy exported** (VAh)
- **Peak demand imported** and **Peak demand exported** (W)
- **Power factor**, or **Power factor phase A/B/C**
- **Clock offset** (diagnostic): how far the meter's clock is from Home Assistant's; positive when the meter's clock
  is ahead.

Sensors are created for the readings your meter provides, and for the time-of-use tiers that have counted energy. The
device also shows the meter's model, serial number, and hardware version when the meter reports them (Itron meters
leave them empty), and its firmware version.

### Long-term statistics from the meter's history

The meter keeps about 30 days of its own 15-minute energy readings. The integration imports them as hourly long-term
statistics, **Electric meter _SFDI_ energy imported** and **energy exported** (statistic IDs
`ieee_2030_5:<SFDI>_energy_imported` and `ieee_2030_5:<SFDI>_energy_exported`), and keeps them up to date. Because the
meter accounted for this energy itself, these statistics have no gaps even when Home Assistant could not reach the
meter. The first import of the stored history runs in the background after setup and can take about 20 minutes; the
sensors keep updating meanwhile.

You can show these statistics in a statistics graph card, or use them as the grid source of the energy dashboard. The
energy dashboard can only price them with a cost statistic, not with a fixed price or a price entity, so to track
costs, keep using the **Energy imported** and **Energy exported** sensors there.

### Energy dashboard

Use **Energy imported** as the grid consumption and **Energy exported** as the return to grid in the energy dashboard
settings. If your utility charges different prices per time-of-use period, and the meter's tiers follow the periods you
are billed for, you can instead add each **Energy imported tier _N_** sensor as a separate grid consumption, each with
its own price. Check this first: a meter's tier schedule can differ from the tariff the utility bills (for example,
after a tariff change), and the meter updates its tier registers only about once an hour.

## Examples

### Get reminded to renew the certificate

Home Assistant raises a repair issue 60 days before the certificate expires. If you prefer a notification on your
phone, use the **Client certificate expiry** sensor in an automation. This one reminds you 60 days ahead.

```yaml
alias: "Remind me to renew the meter certificate"
triggers:
  - trigger: time
    at:
      entity_id: sensor.electric_meter_client_certificate_expiry
      offset: "-1440:00:00"
actions:
  - action: notify.send_message
    target:
      entity_id: notify.my_device
    data:
      title: "Meter certificate expires in 60 days"
      message: >
        Renew the electric meter's certificate (Reconfigure > Renew the
        client certificate) and register its LFDI with your utility before
        {{ as_local(as_datetime(states('sensor.electric_meter_client_certificate_expiry'))).date() }}.
```

### Notify when the home starts sending power to the grid

```yaml
alias: "Exporting to the grid"
triggers:
  - trigger: numeric_state
    entity_id: sensor.electric_meter_power
    below: -500
    for:
      minutes: 5
actions:
  - action: notify.send_message
    target:
      entity_id: notify.my_device
    data:
      message: "Sending {{ states('sensor.electric_meter_power') | float | abs | round(0) }} W to the grid."
```

Replace the entity IDs and the `notify` target with the ones in your installation.

## Data updates

The integration polls the meter every 60 seconds for power and the imported and exported energy. All other readings
are updated every 15 minutes. Each update uses a single connection to the meter and resumes the previous TLS session,
which keeps the load on the meter low.

If an update fails, the sensors become unavailable until the next successful one; a reading that fails on its own
shows as unknown. When the meter reports that it is busy, Home Assistant leaves it alone for 5 minutes.

Readings the meter marks as questionable, and implausible jumps (such as a register that suddenly runs backwards), are
not shown: the sensor is unknown until the next good reading.

## Known limitations

- The meter serves one client at a time. While another app reads the meter, Home Assistant cannot, and the other way
  around.
- The meter does not report prices. Set up pricing in the energy dashboard.
- The meter does not report which time-of-use tier is active, nor which tier is which pricing period, and it updates
  its tier registers only every so often. Compare the tiers with your bill or tariff, and use your tariff's schedule
  (for example, in a template or schedule helper) to know the current period.
- The meter's history goes back about 30 days, so the imported statistics start then. Gaps in the sensors' own
  statistics are not filled; the imported statistics are the gap-free record.

## Troubleshooting

### The meter is not discovered

Itron meters announce themselves with mDNS (`_smartenergy._tcp`); other meters are added manually. If Home Assistant
and the meter are on different networks, forward mDNS between them (for example, with an mDNS repeater on your router),
or add the integration manually with the meter's IP address. Your router's list of connected devices shows the meter's
address.

### The meter does not accept the certificate yet

After you register a new certificate, it can take a few days before the meter accepts it. The repair issue shows the
LFDI the utility needs. Check that the registered LFDI matches it exactly.

### The client certificate expired

When the certificate expires, or the utility removes its registration, Home Assistant asks you to reauthenticate,
unless a [renewed certificate](#renewing-the-certificate) is waiting: then it keeps checking that one instead.

When reauthenticating, choose **Try the current certificate again** if you have just registered it again, or
**Generate a new certificate** and register its LFDI, or import a certificate you have already registered. Readings
stop until the meter accepts a certificate; [renewing in time](#renewing-the-certificate) avoids that.

### The meter is busy or stops responding

- Make sure nothing else is reading the meter.
- Check that the meter is still connected to your Wi-Fi. For Xcel Energy, the
  [Meters and devices](https://my.xcelenergy.com/MyAccount/s/meters-and-devices/manage-meters-and-devices) page shows
  its status.
- An Itron meter can stop answering for hours while it stays connected to the Wi-Fi. Forcing it to reconnect brings
  it back within seconds: disconnect it from your access point (for example, "Reconnect client" in UniFi), or send its
  Wi-Fi settings again from the utility's page.

### The meter moved to a new address

Home Assistant finds an Itron meter again through discovery. For other meters, or if discovery does not work on your
network, select **Reconfigure** on the integration, then **Change the meter's address**. Home Assistant checks that it
is the same meter.

## Removing the integration

Remove it under **Settings** > **Devices & services**: select the integration, then **⋮** > **Delete**. Removing it
also deletes the stored client certificate and its private key, any pending renewal, its repair issues, and the cached
description of the meter. To remove the files too, uninstall it from the Marketplace. If you no longer read the meter
locally, you can also remove the device registration with your utility.

## Development

```
uv run --group dev pytest
```

The tests run against Home Assistant through
[pytest-homeassistant-custom-component](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component).

## Authorship and license

Written by Raul E Rangel with [Claude](https://claude.com/claude-code) (Anthropic) as a co-author; commits it helped
write carry a `Co-Authored-By: Claude` trailer. Licensed under the Apache License 2.0.
