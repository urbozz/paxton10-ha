# Paxton10 for Home Assistant

This custom integration connects Home Assistant to a Paxton10 access control server. It shows the status of the door controllers and entry panels, fires an event for each door event in the Paxton log, and, if you turn it on, adds an **Open** button for each door and the car park gate.

It uses the Paxton10 web app's internal API. Paxton doesn't document or support that API, so a Paxton software upgrade can break the integration. It was built against Paxton10 4.11 SR1 (`4.11.9753.20528`).

## Prerequisites

- Home Assistant 2026.10 or later.
- A network path from Home Assistant to the Paxton10 server (Direct), or Paxton remote access turned on for the site (Remote).
- A dedicated Paxton10 account for Home Assistant, so the Paxton event log shows which actions came from Home Assistant. Build and test with an administrator account first, then move to an account with only the permissions it needs.

## Install

### Manual

1. Copy `custom_components/paxton10` into the `custom_components` folder in your Home Assistant configuration folder.
2. Restart Home Assistant.

### HACS

1. In HACS, open the menu, then click **Custom repositories**.
2. Add this repository's URL with the type **Integration**.
3. Install **Paxton10**, then restart Home Assistant.

## Set up

1. In Home Assistant, go to **Settings > Devices & services**.
2. Click **Add integration**, then search for **Paxton10**.
3. In **Connection**, choose **Direct** or **Remote**.
4. Enter the server address or remote ID:
   - Direct: the server's IP address or host name, for example `192.0.2.10`.
   - Remote: the remote ID from the `paxton10remote.com` address, for example `abc123`.
5. Enter the Paxton account's username (email address) and password. The integration signs in straight away and shows any error on the same screen.
6. Check the server name, software version, and the number of doors and devices found, then click **Submit**.

Home Assistant stores only the SHA-1 password hash that the Paxton10 sign-in expects, never the password. That hash is enough to sign in to this Paxton10 server, so treat Home Assistant backups as sensitive.

### Installation parameters

| Parameter | Description |
|---|---|
| Connection | **Direct** connects to the server over HTTPS. The server's self-signed certificate isn't checked. **Remote** goes through Paxton's remote access relay at `p10remote.com`. Direct is faster. |
| Server address or remote ID | The server's address for Direct, or the site's remote ID for Remote. |
| Username | The Paxton10 account's email address. |
| Password | The Paxton10 account's password. Only its hash is stored. |

To change the connection later, open the integration and click **Reconfigure**. Reconfigure refuses a server that belongs to a different site. If the password changes, Home Assistant asks you to sign in again.

## Options

Open the integration and click **Configure**.

| Option | Default | Description |
|---|---|---|
| Allow door control | Off | Adds an **Open** button for each door and the gate. When off, Home Assistant creates no buttons and the client refuses every write. |
| Device status interval | 30 s | How often to read controllers, entry panels, and the system summary. Minimum 10 s. |
| Event interval | 10 s | How often to read the event log. Minimum 5 s. |
| Use a fallback route | Off | If the configured route fails, try the other one. Home Assistant raises a repair issue while it uses the fallback. Every hour it tests the main route on a separate connection, and switches back only once that connection signs in. |
| Fallback address or remote ID | Empty | The address or remote ID for the fallback route. Required when the fallback is on. |
| Include user names in events | Off | Adds the user's name to door events. These are residents' names, which is personal data. |

Changing an option reloads the integration.

## Supported devices

| Paxton10 item | Home Assistant device |
|---|---|
| Server | One device with the system summary and software version. |
| Door, gate, or barrier | One device per door, with the group from the Paxton device tree (for example **Ground Floor**) as its suggested area. |
| Door controller (GEN1 and GEN2) | One device per controller, named after the door it drives. |
| Entry panel | One device per panel. |

Contacts, push buttons, break glass units, and other inputs in the device tree aren't added. Cameras, users, credentials, and temporary PINs aren't supported.

## Entities

| Device | Entity | Notes |
|---|---|---|
| Server | Active users, Total users, Unacknowledged alarms, Offline devices | From `System/Summary`. Created only if the account can read it. |
| Server | Total devices, Software version | Diagnostic. |
| Door | Open (button) | Only when **Allow door control** is on. The door opens for its configured open time, then relocks. There's no lock command. |
| Door | Door event (event) | See [Events](#events). |
| Controller and entry panel | Connectivity (binary sensor) | Diagnostic. |
| Controller and entry panel | Firmware | Diagnostic. |
| Controller and entry panel | IP address, Last contact | Diagnostic. Disabled by default. Last contact was weeks old on every device in testing while the devices were online, so don't use it as a health check. |
| Controller | Battery charge (raw), Battery state (raw), PSU power state (raw) | Diagnostic. The raw numbers from Paxton. What each value means isn't confirmed yet. |

Every press of an **Open** button is logged at info level with the door's name, and appears in the Paxton event log as opened by software.

## Events

The integration fires a `paxton10_event` event on the Home Assistant event bus for each new entry in the Paxton event log, and triggers the matching door's **Door event** entity.

| Field | Description |
|---|---|
| `event_type` | `access_permitted`, `opened_by_software`, `unlocked`, `relocked`, `left_open`, `closed`, `forced`, or `other`. |
| `event_type_id` | Paxton's numeric event type. |
| `event_id` | Paxton's event ID. |
| `door_entity_id`, `door_name` | The Paxton door. Both are `null` for events that aren't about a known door. |
| `time` | When the event happened, in ISO 8601 format. |
| `user_name` | Only when **Include user names in events** is on. |
| `config_entry_id` | The integration entry the event came from. |

Events from before Home Assistant started are never replayed.

## How data updates

- **Events:** every event interval, the integration reads the newest 50 entries in the event log and fires the ones it hasn't seen.
- **Device status and summary:** every device status interval.
- **Layout:** every hour, the integration reads the device tree again. It adds new doors and devices, and removes devices that are no longer on the server.

If the device read or the event read fails, every entity becomes unavailable and Home Assistant logs it once. Entities stay unavailable until the read that failed succeeds again. A success on the other read doesn't hide the failure. Each read retries with backoff up to 5 minutes. Tokens last 12 hours. The integration signs in again by itself when a token expires. If the server rejects the stored credentials, polling stops and Home Assistant asks you to sign in again, so a changed password can't lock the account.

Paxton10 also pushes live updates over a SignalR hub. This version doesn't use it yet. `source.py` puts all fetching behind an `UpdateSource` interface, so a live source can replace polling later without changing any entity.

## Examples

Notify when a door is forced:

```yaml
triggers:
  - trigger: event
    event_type: paxton10_event
    event_data:
      event_type: forced
actions:
  - action: notify.notify
    data:
      message: "{{ trigger.event.data.door_name }} was forced open"
```

Open the car park gate from a dashboard button or an automation (needs **Allow door control**):

```yaml
actions:
  - action: button.press
    target:
      entity_id: button.car_park_vehicle_gate_open
```

Alert when a controller goes offline:

```yaml
triggers:
  - trigger: state
    entity_id: binary_sensor.main_entrance_door_controller_connectivity
    to: "off"
    for: "00:05:00"
actions:
  - action: notify.notify
    data:
      message: Main entrance door controller is offline
```

Entity IDs depend on your areas and names. Check them in **Settings > Devices & services > Entities**.

## Use cases

- Alerts for forced doors, doors left open, and offline controllers.
- Releasing the gate for a visitor or a delivery from Home Assistant.
- A dashboard of controller status and unacknowledged alarms.
- Recording door activity alongside other building systems.

## Known limitations

- The API is undocumented and can change with any Paxton upgrade.
- Updates are polled. A door event can take up to the event interval to appear.
- If more than 50 events happen between two polls, only the newest 50 are fired.
- The event poll's filter body and the user name field come from the web app's code. They haven't been checked against a live response yet. If events don't arrive, see [Troubleshooting](#troubleshooting).
- Connectivity assumes status `1` means online. That held for every device during testing, when the server reported no offline devices. Other values are treated as offline until they're checked against the web UI.
- The controller list is large (about 57 KB per controller) because Paxton includes every input and output. With 10 controllers at the default 30 s interval, that's about 1.6 GB a day on the local network. Raise the device status interval if that matters.
- The server isn't discovered automatically. Enter its address yourself.
- When the integration switches from the fallback route back to the main route, a call already in flight on the fallback connection can fail. The next poll uses the main route.

## Safety rules

These rules are enforced in the client (`api.py`) and are covered by the tests:

- The client never sends `DELETE`. `DELETE /api/v2/Events/All` wipes the server's whole event log.
- The client sends `POST` or `PUT` only to paths on its allowlist. The only write on that list is door release (`/api/v2/System/ActivateAppliances`), and it's blocked unless **Allow door control** is on.
- The integration never calls endpoints that look like reads but change configuration, such as `POST /api/v2/device/states`, or `POST` and `PUT` on users, time constraints, dashboards, or permissions.
- Tokens, passwords, and hashes are never logged. Diagnostics redact credentials, addresses, serial numbers, the site ID, and user names.
- If the account isn't allowed to read something (HTTP 401, 403, or 405), the integration skips those entities instead of failing.

## Troubleshooting

| Symptom | What to check |
|---|---|
| **Can't connect** during setup | For Direct, check that Home Assistant can reach the server on port 443. For Remote, check that remote access is on in Paxton10. |
| **The username or password is wrong** | Sign in to the Paxton10 web UI with the same account. |
| Fewer entities than expected | The account may lack permission for devices or the summary. Try an administrator account to compare. |
| No door events | Download diagnostics (open the integration, click the three dots, then **Download diagnostics**) and check `last_event_id`. If it stays at `null` or `0`, the event poll is failing. Turn on debug logging for `custom_components.paxton10` and look for event poll errors. |
| **Paxton10 is using the fallback route** repair | The main route failed. Check the network path. The repair clears itself once the main route works again. |

Debug logging:

```yaml
logger:
  logs:
    custom_components.paxton10: debug
```

## Remove

1. In Home Assistant, go to **Settings > Devices & services > Paxton10**.
2. Click the three dots, then **Delete**.
3. Optional: delete `custom_components/paxton10` and restart Home Assistant.
4. Optional: delete or disable the Home Assistant account in Paxton10.

## Development

The tests use `pytest-homeassistant-custom-component` and a fake Paxton server under the real client, so the allowlist runs in every test. They never contact a real system.

```bash
uv venv -p 3.14 .venv && uv pip install -p .venv/bin/python -r requirements_test.txt
```

```bash
.venv/bin/python -m pytest --cov
```

```bash
.venv/bin/mypy --strict custom_components/paxton10
```

Before a release, test on a real system: add the integration over Direct and then over Remote, confirm the entities, and test door release on one door with someone watching it.

## Licence

MIT. See `LICENSE`.

This project isn't affiliated with or endorsed by Paxton Access Ltd. Paxton and Paxton10 are trademarks of their owner.
