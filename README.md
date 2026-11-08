# Xiaomi Scale HA — persistent household measurements

This is [fiam/xiaomi-scale-ha](https://github.com/fiam/xiaomi-scale-ha), a fork of
[ermarmon/xiaomi-scale-ha](https://github.com/ermarmon/xiaomi-scale-ha) based on
commit `7877ebcbbddc8b5df2881aa0678626d93d6eafee` (upstream 0.2.1).
The integration domain remains `xiaomi_scale_ha`, so existing entity/config entry
identities can be preserved when replacing the component.

This fork focuses on the native Home Assistant integration. The legacy MQTT
scripts and add-on folders remain inherited from upstream; the new workflow below
applies to `custom_components/xiaomi_scale_ha`.

## Features

- Xiaomi BLE weight and impedance decoding through HA's Bluetooth stack, including
  ESPHome-compatible proxies such as [fiam/btproxy](https://github.com/fiam/btproxy).
- Persistent records with IDs, reception timestamps, weight, raw impedance,
  ownership status and candidate responses.
- Automatic matching learns from human-confirmed readings (within 2 kg of the
  median of the last ten). One match assigns; overlapping matches ask the
  candidates; no match asks everyone. Optional fixed ranges override learning.
- Each phone receives **That's me** / **Not me**. The first valid claim wins.
  Old buttons cannot claim new records. Claims are bound to the configured HA user
  or phone, and buttons expire after 24 hours.
- Pending records survive restarts. All declines leave a record for manual review;
  a decline never automatically assigns it to another person.
- Profile forms in HA for height, date of birth, sex and optional weight ranges.
- Raw impedance retained before profile details are available. Saving a profile
  recalculates historical body-fat estimates using age at the measurement date.
- Weight-only readings have no body-fat estimate. Earlier records keep their own
  impedance and estimates; values are never carried across readings.
- A dashboard card with account-bound confirmation buttons, personal history first
  and expandable household measurements. Native HA charts show 14-day weight
  and body-fat trends; the card also shows the change since the previous reading.
- Separate weigh-ins of the same weight are preserved; packets from one physical
  weighing session are combined.

No Apple Health / HealthKit export is included. Body-fat values are estimates from
upstream's formula and require valid impedance plus the profile details.

## Installation

For a pinned manual install, download the release ZIP and checksum from this
fork's Releases page, verify the checksum, and extract the component into
`<HA config>/custom_components/xiaomi_scale_ha`. Restart HA and add **Xiaomi Scale HA**
under **Settings → Devices & services**.

This repository retains HACS metadata and can be added as a custom **Integration**
repository using `https://github.com/fiam/xiaomi-scale-ha`. Do not install both
forks at once: they use the same integration domain.

## Initial setup

Enter the scale MAC address and a list of people in Users JSON. The data below is
an example; replace the identifiers and notification service with your own:

```json
[
  {
    "NAME": "Person A",
    "NOTIFY_SERVICE": "notify.mobile_app_person_a_phone",
    "HA_USER_ID": "replace-with-the-persons-ha-user-id"
  },
  {
    "NAME": "Person B",
    "NOTIFY_SERVICE": "notify.mobile_app_person_b_phone",
    "HA_USER_ID": "replace-with-the-persons-ha-user-id"
  }
]
```

`NAME` is required and must be unique. Notification routing is optional, but
phone confirmation requires `HA_USER_ID` to identify the responding HA user.
`MOBILE_DEVICE_ID` is an optional fallback for events without a user context.
Use the mobile app's device identifier, not an unrelated device registry ID.
Do not publish your real configuration or records.

An empty list keeps readings pending without sending phone notifications.
The initial user list and phone associations are configured at setup; the current
options forms edit existing people rather than adding/removing them.

Under **Configure → Edit a person's profile**, select a person and set:

- Optional fixed matching range: inclusive minimum/maximum in kg.
- Height in cm, date of birth and male/female formula setting for body-fat estimates.

With no confirmed history, readings ask everyone. Each person’s confirmed readings
then seed automatic matching. Automatic assignments do not train the matching
range. Birthdate uses separate day/month/year fields so any year can be entered
directly. Under **Notifications and measurement settings**, configure registered
weight notifications and the default eight-second wait for impedance.

## Data and manual review

The store is `<HA config>/.storage/xiaomi_scale_ha_<entry_id>_records`.
Records are retained until explicitly deleted. The history sensor shows only the
last ten records per person; this does not truncate stored history.
Back up the configuration directory. Do not edit storage while HA is running.

Pending sensor attributes include measurement IDs and candidate/decline lists.
Manual `xiaomi_scale_ha.assign_pending` requires `user_name` and `measurement_id`;
`discard_pending` requires `measurement_id`. User-initiated manual changes require
an HA administrator. `send_pending_notification` resends unresolved requests.

Weight sensors expose their record attributes, including raw impedance when
available. Body-fat sensors expose `measured_at` and `measurement_id` for the latest
reading and become unknown when that reading has no estimate. Profile updates
recalculate the ledger and current sensor values; they do not rewrite historical
Home Assistant Recorder states.

## Dashboard

Add a JavaScript module resource under **Settings → Dashboards → Resources**:
`/xiaomi_scale_ha/scale-card.js?v=0.3.1`. The integration serves this file after HA
restarts. Use a panel view and this card (replace the sample entity names and IDs):

```yaml
type: custom:xiaomi-scale-card
pending_entity: sensor.xiaomi_scale_pending_measurement
history_entity: sensor.xiaomi_scale_history
people:
  - name: Person A
    user_id: replace-with-the-persons-ha-user-id
    weight_entity: sensor.xiaomi_scale_person_a_weight
    body_fat_entity: sensor.xiaomi_scale_person_a_body_fat
  - name: Person B
    user_id: replace-with-the-persons-ha-user-id
    weight_entity: sensor.xiaomi_scale_person_b_weight
    body_fat_entity: sensor.xiaomi_scale_person_b_body_fat
```

For multiple scales, also set `entry_id`. The card shows pending readings, the
signed-in person's measurements, then other household members. `That's me` and
`Not me` call `xiaomi_scale_ha.respond_pending`; the server derives identity from
the authenticated HA account, not the card configuration. Administrators can
assign older or unmatched readings through the expanded assignment control.
These cards organize existing HA sensor data; they are not per-person privacy
boundaries. Other members' data remains accessible through HA's normal entities.

## Upgrading from upstream

Back up HA first. This fork's persistent ledger uses a new `_records` storage key.
Existing upstream `_history` data is not automatically imported; it is left intact.
Existing user configuration is preserved, but mobile confirmation requires a user
or device binding as described above. Alexa actions are not supported by this fork.
Historical records without impedance cannot be backfilled with body-fat values.

## Tests

```sh
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

Parser and ledger tests run without HA. Runtime and config tests require HA and
are otherwise skipped. To run all tests with the pinned validation environment:

```sh
docker run --rm -v "$PWD:/work" -w /work --entrypoint python \
  ghcr.io/home-assistant/home-assistant:2026.10.4 \
  -m unittest discover -s tests -p 'test_*.py' -v
```

Tests use synthetic fixtures, fake storage and notification calls; they do not
contact devices or create real measurements. CI runs both test modes.

MIT licensed; original attribution is retained in `LICENSE` and the component.
