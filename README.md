# IEEE 2030.5 Smart Meter for Home Assistant (Xcel Energy Itron Riva)

[![Open your Home Assistant instance and open this repository in the Marketplace.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=ismell&repository=ha-ieee-2030-5&category=integration)

Read your electric utility meter, such as Xcel Energy's Itron Riva smart meter, directly over your local network,
using the [IEEE 2030.5](https://standards.ieee.org/ieee/2030.5/11216/) protocol (also known as Smart Energy Profile
2.0). The integration shows how much power your home is drawing from or sending to the grid right now, along with the
meter's own energy totals, so you can use the meter as the grid source of the energy dashboard.

Some utilities let customers connect their smart meter to their home Wi-Fi and register devices that may read it. Xcel
Energy, for example, offers this for its Itron Riva meters: see
[Setting up an Xcel Energy meter](https://github.com/ismell/ha-ieee-2030-5/blob/main/docs/xcel.md). Readings come straight from the
meter: no cloud service or utility account access is involved after registration.

This is a custom integration, distributed through the Home Assistant Marketplace (HACS before Home Assistant 2026.11).
It is not affiliated with or endorsed by Xcel Energy, Itron, or the IEEE.

## Supported devices

- Itron Gen5 Riva meters on Xcel Energy's network (firmware 3.2.50 has been tested).

Other meters that serve the IEEE 2030.5 metering function set over TLS should work too: the
[aio2030dot5](https://github.com/ismell/aio2030dot5) library this integration uses is also tested against an
independent, open-source IEEE 2030.5 server. Automatic discovery recognizes Itron meters only; add other meters
manually.

## Prerequisites

1. **Connect the meter to your network.** For Xcel Energy, follow
   [Setting up an Xcel Energy meter](https://github.com/ismell/ha-ieee-2030-5/blob/main/docs/xcel.md): turn on Launchpad, connect the meter to your
   Wi-Fi, and wait until Xcel finishes the meter setup. Until then, the meter can already be on your network but still
   refuses connections. (Xcel's page can still show the setup as **Pending** after the meter is ready.)
2. **Make sure Home Assistant can reach the meter** on TCP port 8081. If the meter is on a separate network (for
   example, an IoT VLAN), allow traffic from Home Assistant to the meter, and forward mDNS between the networks if you
   want automatic discovery.
3. **Stop anything else that reads the meter.** The meter serves a single client at a time. Stop any other app,
   add-on, or device that reads it locally before you set up this integration.

You also need a **client certificate registered with your utility**: the meter only talks to clients whose certificate
the utility has registered. You don't need one in advance. During setup, Home Assistant can generate one and show you
its **LFDI** (a fingerprint of 40 hexadecimal digits) to register; you can also import a certificate you have already
registered. Registration can take a few days.

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

### More than one meter

If you have more than one meter, for example a separate meter for solar production, add each one on its own: each
meter is a separate entry in Home Assistant with its own certificate, and each certificate is registered with the
utility for its meter.

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
3. Register the new certificate's LFDI with your utility (for Xcel Energy, under
   [Meters and devices](https://my.xcelenergy.com/MyAccount/s/meters-and-devices/manage-meters-and-devices)). Until the meter accepts it, Home Assistant keeps using the current certificate, and a
   repair issue shows the new LFDI.
4. Home Assistant checks the new certificate every 15 minutes, using one extra connection to the meter, and switches to
   it on its own once the meter accepts it. To check right away, open the repair issue and select **Submit**.

If the utility retires the old certificate before the new one is registered, readings pause, but Home Assistant does
not ask you to reauthenticate: it keeps checking the new certificate every 15 minutes and switches as soon as the meter
accepts it.

Only one renewal can wait at a time. To replace it, select **Reconfigure**, then **Discard the pending certificate**,
and confirm: this deletes its private key, so a registration you made for its LFDI can no longer be used.

## Supported functionality

### Sensors

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

### Repairs

The integration raises these repair issues under **Settings** > **System** > **Repairs**:

- **Register the meter's client certificate**: the meter does not accept the certificate yet. Shows the LFDI to
  register; select **Submit** to check again.
- **The meter's client certificate expires soon**: 60 days before expiry. Starts a [renewal](#renewing-the-certificate).
- **Register the meter's renewed client certificate**: a renewed certificate is waiting to be registered. Shows its
  LFDI.

### Reconfiguration

Select **Reconfigure** on the integration (under **Settings** > **Devices & services**) to:

- **Change the meter's address**: when its IP address or port changed. Home Assistant checks that it is the same meter.
- **Renew the client certificate**: see [Renewing the certificate](#renewing-the-certificate).
- **Discard the pending certificate**: when a renewal is waiting.

### Diagnostics

When you report a problem, download diagnostics from the integration's **⋮** menu under **Settings** > **Devices &
services**. They contain the meter's description, its latest readings, connection counters, and the state of the
reading checks. The certificate, private key, meter and certificate identifiers (LFDI, SFDI), serial number, and
address are removed.

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

### Recover the meter when its network freezes (UniFi)

Itron Riva meters have a firmware problem: every so often the meter stops answering on your network for hours (14 to
44 hours have been seen) while it stays connected to the Wi-Fi, so the sensors become unavailable. Forcing the meter
off the Wi-Fi restarts its network: it rejoins on its own, and the integration reads it again within a minute (12 and
34 seconds after the reconnect in the two cases seen so far).

If your Wi-Fi is UniFi, this automation does that for you. It waits until the meter has been unavailable for 10 minutes,
asks UniFi to reconnect it, and tries again every 30 minutes while it stays down. It needs:

- The [UniFi Network](https://www.home-assistant.io/integrations/unifi/) integration, set up with a UniFi account that
  may manage clients (for example, the Site Admin role), and the meter showing up in it as a tracked client.
- The meter's UniFi device in the `device_id` below. The easiest way to get it: create the automation in the UI, add
  the action **UniFi Network: Reconnect wireless client**, and pick the meter as the device.

```yaml
alias: "Recover the meter when its network freezes"
description: >-
  The meter's network can freeze while it stays on the Wi-Fi. Reconnecting it
  to the Wi-Fi brings it back within a minute.
mode: single
max_exceeded: silent
triggers:
  - trigger: state
    entity_id: sensor.electric_meter_power
    to: unavailable
    for:
      minutes: 10
  - trigger: time_pattern
    minutes: /30
conditions:
  - condition: state
    entity_id: sensor.electric_meter_power
    state: unavailable
    for:
      minutes: 10
  - alias: "At most one reconnect every 25 minutes"
    condition: template
    value_template: >-
      {% set last = this.attributes.last_triggered %}
      {{ last is none or (now() - last).total_seconds() > 1500 }}
actions:
  - action: unifi.reconnect_client
    data:
      device_id: YOUR_METER_UNIFI_DEVICE_ID
    continue_on_error: true
  - alias: "Give the meter 3 minutes to rejoin and answer a poll"
    wait_template: "{{ not is_state('sensor.electric_meter_power', 'unavailable') }}"
    timeout:
      minutes: 3
    continue_on_timeout: true
  - if:
      - condition: template
        value_template: "{{ wait.completed }}"
    then:
      - action: logbook.log
        data:
          name: Electric meter
          entity_id: sensor.electric_meter_power
          message: "recovered after a Wi-Fi reconnect"
    else:
      - action: notify.send_message
        target:
          entity_id: notify.my_device
        data:
          message: >-
            The electric meter is still unavailable after a Wi-Fi reconnect.
            Retrying every 30 minutes. If it stays down, send its Wi-Fi
            settings again from the utility's page.
```

Without UniFi, use your access point's own way to disconnect a client, if it has one in Home Assistant, or send the
meter's Wi-Fi settings again from the utility's page (for Xcel Energy,
[Meters and devices](https://my.xcelenergy.com/MyAccount/s/meters-and-devices/manage-meters-and-devices)).

Replace the entity IDs, the device ID and the `notify` target with the ones in your installation.

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

### Setup says "Failed to connect"

Home Assistant could not open a connection to the meter. Check, in this order:

1. The meter's setup is finished. Until then, the meter refuses connections even though it is on your network. For
   Xcel Energy, [Meters and devices](https://my.xcelenergy.com/MyAccount/s/meters-and-devices/manage-meters-and-devices)
   shows the meter setup as **Pending** meanwhile, but it can still show that for a while after the meter is ready.
2. Home Assistant can reach the meter's address on port 8081 (see [Prerequisites](#prerequisites)).
3. Nothing else is reading the meter.

### The meter is not discovered

Itron meters announce themselves with mDNS (`_smartenergy._tcp`); other meters are added manually. If Home Assistant
and the meter are on different networks, forward mDNS between them (for example, with an mDNS repeater on your router),
or add the integration manually with the meter's IP address. Your router's list of connected devices shows the meter's
address.

### The meter does not accept the certificate yet

After you register a new certificate, it can take a few days before the meter accepts it. The repair issue shows the
LFDI the utility needs. Check that the registered LFDI matches it exactly (for Xcel Energy, under
[Meters and devices](https://my.xcelenergy.com/MyAccount/s/meters-and-devices/manage-meters-and-devices)).

### The client certificate expired

When the certificate expires, or the utility removes its registration, Home Assistant asks you to reauthenticate,
unless a [renewed certificate](#renewing-the-certificate) is waiting: then it keeps checking that one instead. For Xcel
Energy, [Meters and devices](https://my.xcelenergy.com/MyAccount/s/meters-and-devices/manage-meters-and-devices) shows whether the certificate is still registered.

When reauthenticating, choose **Try the current certificate again** if you have just registered it again, or
**Generate a new certificate** and register its LFDI, or import a certificate you have already registered. Readings
stop until the meter accepts a certificate; [renewing in time](#renewing-the-certificate) avoids that.

### The meter is busy or stops responding

- Make sure nothing else is reading the meter.
- Check that the meter is still connected to your Wi-Fi. For Xcel Energy, the
  [Meters and devices](https://my.xcelenergy.com/MyAccount/s/meters-and-devices/manage-meters-and-devices) page shows
  its status.
- An Itron meter can stop answering for hours while it stays connected to the Wi-Fi. Forcing it to reconnect brings
  it back within a minute: disconnect it from your access point, or send its Wi-Fi settings again from the utility's
  page. With UniFi, an automation can do this for you: see
  [Recover the meter when its network freezes](#recover-the-meter-when-its-network-freezes-unifi).

### The meter moved to a new address

Home Assistant finds an Itron meter again through discovery. For other meters, or if discovery does not work on your
network, select **Reconfigure** on the integration, then **Change the meter's address**. Home Assistant checks that it
is the same meter.

### Reporting a problem

Open an issue on [GitHub](https://github.com/ismell/ha-ieee-2030-5/issues), and attach:

1. **Diagnostics** (see [Diagnostics](#diagnostics)).
2. **Debug logs**: on the integration's page under **Settings** > **Devices & services**, select **⋮** >
   **Enable debug logging**, wait for the problem to happen again, then select **Disable debug logging**: the log
   downloads. Logs show only the last digits of the meter's identifiers. If you set the meter up by its host name
   rather than its IP address, check the log for it before sharing.

## Removing the integration

Remove it under **Settings** > **Devices & services**: select the integration, then **⋮** > **Delete**. Removing it
also deletes the stored client certificate and its private key, any pending renewal, its repair issues, and the cached
description of the meter. To remove the files too, uninstall it from the Marketplace. If you no longer read the meter
locally, you can also remove the device registration with your utility.

## Development

```sh
uv run --group dev pytest
```

The tests run against Home Assistant through
[pytest-homeassistant-custom-component](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component).

## Authorship and license

Written by Raul E Rangel with [Claude](https://claude.com/claude-code) (Anthropic) as a co-author; commits it helped
write carry a `Co-Authored-By: Claude` trailer. Licensed under the Apache License 2.0.
