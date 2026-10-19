# Setting up an Xcel Energy meter

Xcel Energy's **Launchpad** service connects your smart meter to your home Wi-Fi so that devices you authorize can read
it locally. This guide walks through turning it on in Xcel's My Account and getting the meter into Home Assistant. Xcel
can change its pages at any time; if a step looks different, please
[open an issue](https://github.com/ismell/ha-ieee-2030-5/issues).

This integration is not affiliated with or endorsed by Xcel Energy.

## Before you start

- **An Xcel Energy smart meter.** The integration is tested with the Itron Gen5 Riva.
- **An Xcel Energy online account** (My Account) for the billing account the meter belongs to.
- **Your Wi-Fi name and password.** The meter connects on 2.4 GHz: both meters this was tested with joined the 2.4 GHz
  band. If your 2.4 GHz and 5 GHz networks have different names, use the 2.4 GHz one.
- **One meter per billing account.** Launchpad handles one meter per billing account. If your home has a second meter,
  for example a separate solar production meter, it needs a billing account of its own. Customers have done this by
  opening a second billing account under the same login and having the second meter moved to it.

## 1. Turn on Launchpad

1. Sign in to My Account, and open
   [Manage Billing Account](https://my.xcelenergy.com/MyAccount/s/profile/manage-billing-account) for the billing
   account of the meter.
2. Select the **Authorized Devices** tab, then **Edit Connected Devices**.

   ![Manage Billing Account, Authorized Devices tab, with the Edit Connected Devices button](images/xcel/authorized-devices.png)

3. Accept the terms and conditions.

## 2. Connect the meter to your Wi-Fi

1. Open [Meters and devices](https://my.xcelenergy.com/MyAccount/s/meters-and-devices?wifistatus). The **Xcel Energy
   Launchpad** card shows **Pending**. Select **Manage**.

   ![The Xcel Energy Launchpad card, Pending, with a Manage link](images/xcel/launchpad-pending.png)

2. On **Connected Devices**, select **Edit**, and enter your Wi-Fi name and password.

   ![Connected Devices: Wi-Fi Connectivity Not Connected, Meter Setup Pending, with an Edit link](images/xcel/connected-devices.png)

3. Wait for **Wi-Fi Connectivity** to show **Connected**. Xcel asks you not to register a device (an LFDI) before it
   does.

The status on Xcel's page can lag behind: it showed **Not Connected** for a meter that had been on the Wi-Fi without a
break for twelve hours, until the page was refreshed. Refresh it, and use your router's list of connected devices as
the reliable check. The meter shows up there as an
Itron device. Note its IP address: you need it if Home Assistant does not discover the meter.

## 3. Wait for the meter setup

Once the meter is on your Wi-Fi, the page looks like this:

![Connected Devices: Wi-Fi Connectivity Connected to the home network, Meter Setup Pending](images/xcel/connected-pending.png)

**Meter Setup** shows **Pending** until Xcel finishes setting the meter up, which can take days; customers have reported
weeks. Until the meter is ready, it can already be on your Wi-Fi but refuses connections, so Home Assistant cannot set
it up yet ("Failed to connect").

Don't rely on the page alone: a meter can be ready while the page still shows **Pending**. One meter started
answering, and Home Assistant discovered it, about twelve hours after it joined the Wi-Fi, with the page still showing
**Pending**. When Home Assistant shows the meter under **Settings** > **Devices & services** as discovered, it is
ready: go on to the next step.

## 4. Add the meter to Home Assistant

1. Make sure Home Assistant can reach the meter on TCP port 8081, and that nothing else is reading it: see
   [Prerequisites](../README.md#prerequisites).
2. [Install the integration](../README.md#installation). The meter is usually discovered on its own; otherwise add the
   integration with the meter's IP address.
3. Choose **Generate a new certificate**. Home Assistant shows the certificate's **LFDI**, for example
   `431ED-86C1F-202CD-D3FEB-3EF8D-CB9F4-457F0-EC47D`.
4. Register that LFDI with Xcel. Open [Meters and devices](https://my.xcelenergy.com/MyAccount/s/meters-and-devices?wifistatus), select **Manage** on the Xcel Energy
   Launchpad card, and on **Connected Devices**, under **My Devices**, select **Add a Device**.

   ![Connected Devices with the Add a Device button under My Devices](images/xcel/my-devices.png)

5. Fill in the form, and select **Next**:
   - **LFDI**: the LFDI Home Assistant shows, in groups of five as shown, or without the dashes.
   - **Nickname**: any name, for example "Home Assistant".
   - **Manufacturer of Device**: for example "Home Assistant".
   - **Device Type**: **In-home Display** works.

   ![The Add Device form: LFDI, Nickname, Manufacturer of Device and Device Type](images/xcel/add-device.png)

6. Xcel confirms the request. It says that if the meter has not finished its setup ("licensing"), using the device can
   take up to 5 business days, and that it emails you once the meter is licensed.

   ![Xcel's confirmation that it received the request to add an authorized device](images/xcel/add-device-success.png)

7. Meanwhile, a repair issue in Home Assistant shows the LFDI, and Home Assistant keeps checking. Once the meter accepts
   the certificate, the sensors appear: for one meter that took minutes, while Xcel's page still showed the meter
   setup as **Pending**.

If you already registered a certificate for another tool, choose **Import a registered certificate** instead and paste
the certificate and its private key. Stop the other tool first: the meter serves one client at a time.

Then set up the [energy dashboard](../README.md#energy-dashboard).

## Keeping it working

- **Renew the certificate every three years.** Home Assistant raises a repair issue 60 days before it expires and
  keeps using the old certificate until Xcel has registered the new one. See
  [Renewing the certificate](../README.md#renewing-the-certificate).
- **If the meter goes silent for hours** while it still shows on your Wi-Fi, reconnecting it to the Wi-Fi brings it
  back. See [Recover the meter when its network freezes](../README.md#recover-the-meter-when-its-network-freezes-unifi).
- **If you change your Wi-Fi name or password,** update it with **Edit** on the Connected Devices page.

## A second meter

Each meter is its own entry in Home Assistant, with its own certificate registered on its own billing account's
Meters and devices page. Repeat steps 1 to 4 for it, with its billing account selected.

Both meters' devices are called **Electric meter**, so the second one's entity IDs get a `_2` suffix. Rename its device
(for example, to "Solar meter") on its device page, and let Home Assistant rename the entity IDs to match.

### A solar production meter

A production meter measures what your solar panels produce. On the production meter this was tested with:

- **Energy imported** is the solar production. The meter's own history shows it at zero overnight, rising from
  about 7:00, and peaking around noon.
- **Power** is what the panels produce right now. At night it shows a few watts below zero: the inverter draws a
  little power while idle, and **Energy exported** counts it.

In the energy dashboard, add the production meter's **Energy imported** under **Solar panels**, and keep the main
meter's **Energy imported** and **Energy exported** as grid consumption and return to grid.
