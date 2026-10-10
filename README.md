# Paxton10 for Home Assistant

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=urbozz&repository=paxton10-ha&category=integration)

This custom integration connects Home Assistant to a Paxton10 access control server. It shows the status of the door controllers and entry panels, fires an event for each door event in the Paxton log, and, if you turn it on, adds an **Open** button for each door, gate, and barrier.

It uses the Paxton10 web app's internal API. Paxton doesn't document or support that API, so a Paxton software upgrade can break the integration. It was built against Paxton10 4.11 SR1 (`4.11.9753.20528`).

## Prerequisites

- Home Assistant 2026.10.0 or later.
- A network path from Home Assistant to the Paxton10 server (Direct), or Paxton remote access turned on for the site (Remote).
- A dedicated Paxton10 account for Home Assistant, so the Paxton event log shows which actions came from Home Assistant. Build and test with an administrator account first, then move to an account with only the permissions it needs, using **Reconfigure**: see [Paxton account permissions](#paxton-account-permissions).

## Install

### HACS

1. Click the **Open in HACS** badge at the top of this page. It opens this repository in your Home Assistant's HACS.

   Alternatively, in HACS, open the menu, then click **Custom repositories**. Add this repository's URL with the type **Integration**.
2. Click **Download**, then restart Home Assistant.

HACS offers published releases only, not the latest code on `main`.

### Manual

1. Download `paxton10.zip` from the latest [release](https://github.com/urbozz/paxton10-ha/releases/latest).
2. In your Home Assistant configuration folder, create the folder `custom_components/paxton10`, and unzip `paxton10.zip` into it. The zip holds the integration's files with no folders around them, so `manifest.json` ends up at `custom_components/paxton10/manifest.json`.
3. Restart Home Assistant.

## Set up

1. In Home Assistant, go to **Settings > Devices & services**.
2. Click **Add integration**, then search for **Paxton10**.
3. In **Connection**, choose **Direct** or **Remote**.
4. Enter the server details and the Paxton account on one screen:
   - Direct: in **Server address**, the server's IP address or host name, for example `192.0.2.10`.
   - Remote: in **Remote ID**, the ID from the site's remote access address, for example `abc123`. You can paste the whole address, such as `https://abc123.paxton10remote.com`.
   - The Paxton account's username (email address) and password.

   The integration signs in straight away. If it can't, the error and its cause appear on the same screen, so you can fix the address and try again.
5. Check the server name, software version, doors, and devices found.
6. Choose whether to turn on **Allow door control** and **Include user names in events**. Both are off by default. You can change them later in **Configure**.
7. Click **Submit**.

Home Assistant stores only the SHA-1 password hash that the Paxton10 sign-in expects, never the password. That hash is enough to sign in to this Paxton10 server, so treat Home Assistant backups as sensitive.

### Installation parameters

| Parameter | Description |
|---|---|
| Connection | **Direct** connects to the server over HTTPS. The server's self-signed certificate isn't checked. **Remote** goes through Paxton's remote access relay at `p10remote.com`. Use Direct when Home Assistant can reach the server: it's faster, and it keeps all traffic on your network. Use Remote when it can't, or as the fallback route. Every Remote request goes through Paxton's relay, so the integration reads less often on Remote: see [How data updates](#how-data-updates). |
| Server address (Direct) | The server's IP address or host name, with an optional port. A pasted URL is reduced to its host. |
| Remote ID (Remote) | The site's remote ID. A pasted `paxton10remote.com` address is reduced to its ID. |
| Username | The Paxton10 account's email address. |
| Password | The Paxton10 account's password. Only its hash is stored. |
| Allow door control | See [Options](#options). |
| Include user names in events | See [Options](#options). |

To change the connection or the Paxton account later, open the integration and click **Reconfigure**. You can switch to another account on the same site without deleting the integration, so entity names, areas, and dashboards are kept. Enter the new account's password; to change only the address, leave the password blank to keep the current one. Reconfigure refuses a server that belongs to a different site. If the password changes, Home Assistant asks you to sign in again.

## Paxton account permissions

Paxton10 controls what an account can do with two kinds of permission. Building permissions say which doors the user can open. Software permissions say what the user can see and change in the Paxton10 software, set per group as Full, Read, or Events.

| What the integration does | What the Paxton account needs |
|---|---|
| Door state, the Lock sensor, and door events | Events or Read permission on the doors. |
| The event log | The **Reports** software permission. Without it, the integration logs a warning and fires no door events. Everything else keeps working. |
| Controllers, entry panels, and the summary | Permission to see them in Paxton10. The exact setting hasn't been mapped yet. Without it, those entities aren't created. |
| **Open** buttons | Building permission to each door: the same access the user would need to open it with a credential. Without it, the release fails. |

The integration never changes Paxton's configuration, so it doesn't need Full permission.

### A login that sees only one person's activity

Each user can have their own Home Assistant, signed in with their own Paxton account, that shows only their own door events. Paxton10 4.11 filters events by the person's software permission, both in the event log and on the live feed.

Paxton's software permissions select groups of people, not individuals. So first, add the person to a group of their own: a person can belong to several groups, and the new group doesn't change their door access unless you add it to a building permission.

Then create a software permission for them, apply it to their own person record, and give it these software elements. To move an existing installation to that person's account, use **Reconfigure**.

| Software element | Read | Events | Why |
|---|---|---|---|
| Reports | Yes | | The event log. |
| Devices, or the device groups the person uses | Yes | | The doors, their names and state, and opening them. Without Read, the person's events show only a reader's serial number. |
| The device groups the person uses | | Yes | Adds which door each of their events happened at. It doesn't add other people's events. |
| People: the person's own group | | Yes | Their events, and no one else's. |

Leave everything else unticked, including Software events and Hardware events.

With this set-up, tested on 4.11:

- Door events, from the event log and the live feed, are only that person's own, including their opens from software. Other people's events, exit button presses, and intercom calls don't appear.
- The Lock sensors still show every unlock and relock of the doors the account can see, without saying who. A door's real lock state isn't personal data.
- **Open** works on the doors the person's building permissions allow.
- The server's summary sensors (Active users and the others) are site-wide figures.
- Each of the integration's sign-ins appears in Paxton's event log under that person's name.

## Options

Open the integration and click **Configure**.

| Option | Default | Description |
|---|---|---|
| Allow door control | Off | Adds an **Open** button for each door, gate, and barrier (Paxton calls them access points). When off, Home Assistant creates no buttons and the client refuses every write. |
| Use a fallback route | Off | If the configured route fails, try the other one. Home Assistant raises a repair issue while it uses the fallback. Every hour it tests the main route on a separate connection, and switches back only once that connection signs in. |
| Fallback address or remote ID | Empty | The address or remote ID for the fallback route. Required when the fallback is on. |
| Include user names in events | Off | Adds the user's name to door events. User names are personal data. |
| Include credential names in events | Off | Adds `credential_name` to access events: the name the credential was given in Paxton, as it was entered. Paxton has no credential type field, so the integration doesn't report one. |

Changing an option reloads the integration.

How often the integration reads from Paxton isn't an option: Home Assistant doesn't let integrations offer polling intervals. The intervals are fixed, and chosen to keep traffic low. See [How data updates](#how-data-updates).

## Supported devices

| Paxton10 item | Home Assistant device |
|---|---|
| Server | One device with the system summary and software version. |
| Door controller (GEN1 and GEN2) | One device per controller, named after the door it drives, connected via the server. |
| Door, gate, or barrier | One device per door, connected via the controller that drives it. A door no controller drives is connected via the server. |
| Entry panel | One device per panel, named after the door it serves. A panel that serves no door keeps its Paxton name, or is called **Entry panel** and its ID if Paxton only has its serial number. |

The integration doesn't suggest areas. Paxton door names usually include the floor already, and Home Assistant adds the area to new entity IDs, so a suggested area would repeat it. Assign areas yourself in **Settings > Areas, labels & zones**.

Contacts, push buttons, break glass units, and other inputs in the device tree aren't added. Cameras, users, credentials, and temporary PINs aren't supported.

## Entities

| Device | Entity | Notes |
|---|---|---|
| Server | Active users, Total users, Unacknowledged alarms, Offline devices | From `System/Summary`. Created only if the account can read it. Active users is Paxton's own count, which Paxton can leave hours out of date: see [Known limitations](#known-limitations). |
| Server | Total devices, Software version | Diagnostic. |
| Door | Open (button) | Only when **Allow door control** is on. Releases the door for its door open time, the same as opening it from the Paxton10 software, then it relocks. There's no command to keep a door unlocked or to lock it: see [Known limitations](#known-limitations). If Paxton accepts the request but the door doesn't unlock within 5 seconds, the press fails with an error. |
| Door | Event (event) | Named after the door, for example `event.main_entrance_door`. See [Events](#events). |
| Door | Lock (binary sensor) | On while the door is unlocked: for its door open time after a credential, exit button, or **Open** press, while a time profile or toggle operating mode keeps it unlocked, while a fire alarm releases it, or while it's held open. Off while locked. The `door_state` attribute gives Paxton's state: `locked`, `unlocked`, `forced_or_left_open`, `offline`, or `online`. Unavailable while Paxton reports the door offline, and unknown while it reports `online`, which says the door is connected but not whether it's locked. Created only if the server and account can read door state. |
| Door | Forced or left open (binary sensor) | **Untested.** Disabled by default. On while Paxton reports the door forced or left open. Needs a door contact fitted and wired to the controller. Built from the Paxton10 web app's door states, but no live site has reported this state yet, so how and when it clears is unconfirmed. Turn it on per door in the entity settings, and report what you see. |
| Controller and entry panel | Connectivity (binary sensor) | Diagnostic. On while the device is connected to the server, including while it runs on battery, updates its firmware, or refreshes. Off while it's offline, rebooting, or reinstating. |
| Controller and entry panel | Status | Diagnostic. `online`, `online_on_battery`, `updating`, `offline`, `refreshing`, `reinstating`, or `rebooting`, as the web app shows them. `online_on_battery` means the controller has lost mains power. |
| Controller and entry panel | Firmware | Diagnostic. |
| Controller and entry panel | IP address | Diagnostic. Disabled by default. |
| Controller | Battery | Diagnostic. `good`, `low`, `critical`, or `not_connected` (no battery fitted). Matches the web app's battery icon, so a fitted battery that reports no charge shows its charging state as `low` (charging) or `critical` (discharging). |
| Controller | Battery state | Diagnostic. `charging` or `discharging`. |
| Controller | Power supply | Diagnostic. `external` (mains) or `failure` (running on battery). |

The battery and power supply states use the codes from the Paxton10 web app. A code the web app calls unknown shows as unknown.

Every press of an **Open** button is logged at info level with the door's name, and appears in the Paxton event log as opened by software.

## Events

The integration fires a `paxton10_event` event on the Home Assistant event bus for each new entry in the Paxton event log, and triggers the matching door's event entity.

| Field | Description |
|---|---|
| `event_type` | See [Event types](#event-types). `other` for a type the integration doesn't know, with Paxton's number in `event_type_id`. |
| `reader` | `entry` or `exit`: which reader an `access_permitted` event came from. `null` for other events. |
| `event_type_id` | Paxton's numeric event type. |
| `event_id` | Paxton's event ID, a 24-character string. It doesn't sort by time. |
| `entity_id`, `device_id` | The door's event entity and device in Home Assistant. Use these in automations. Both are `null` for events that aren't about a known door. |
| `door_entity_id`, `door_name` | The door's numeric ID and name in Paxton. Both are `null` for events that aren't about a known door. |
| `time` | When the event happened, in ISO 8601 format. |
| `user_name` | Only when **Include user names in events** is on. For intercom events, the user who was called. |
| `credential_name` | Only when **Include credential names in events** is on. The name the credential was given in Paxton. Never the credential's number. |
| `config_entry_id` | The integration entry the event came from. |

### Event types

| `event_type` | Paxton type | What happened |
|---|---|---|
| `access_permitted` | 5 | A credential opened the door. `reader` says whether it was the entry or exit reader. |
| `exit_request` | 6 | Someone pressed the exit button. |
| `opened_by_software` | 7 | An operator, Home Assistant, or an intercom release opened the door from software. |
| `unknown_credential` | 1 | Access denied: the credential isn't known. |
| `lost_credential` | 2 | Access denied: the credential is marked lost. |
| `no_permission` | 4 | Access denied: the user has no permission for this door at this time. |
| `access_not_made` | 3 | Access was granted, but the door wasn't opened. |
| `intercom_unlocked` | 140 | The called user unlocked the door from their intercom. |
| `intercom_not_unlocked` | 141 | The called user declined to unlock the door. The called user's name isn't extracted yet for this type. |
| `call_made` | 145 | Someone called a user from the entry panel. |
| `call_not_answered` | 142 | The called user didn't answer. |
| `unlocked`, `relocked` | 8, 9 | A time profile unlocked or relocked the door. |
| `toggled_open`, `toggled_closed` | 17, 18 | The door was toggled unlocked or locked again, by a credential in toggle operating mode or by a trigger and action. |
| `left_open` | 10 | The door was left open. |
| `closed` | 11 | The door closed. |
| `forced` | 16 | The door was forced open. |
| `operator_logged_on` | 700 | Someone signed in to the Paxton10 software, including the integration itself when it renews its sign-in token, about every 12 hours. Its sign-in at start-up doesn't fire, because it happens before the integration starts listening. Not about a door. |
| `operator_logged_off` | 701 | Someone signed out of the Paxton10 software. Not about a door. |
| `operator_logged_on_remotely` | 708 | Someone signed in to the Paxton10 software through remote access. Not about a door. |

The numbers come from the Paxton10 4.11 web app, except the intercom types, which were read from a live event log. Types 2, 3, 4, 8 to 11, 16, 17, 18, and 708 haven't been seen live yet, so their descriptions follow the web app's names for them.

Events that aren't about a door, such as the sign-in types, fire `paxton10_event` with `entity_id` and `device_id` set to `null`, and don't trigger a door's event entity. They still appear in **Logbook**, for example "Paxton10 logged a sign-in by Alex Smith".

Events from before Home Assistant started are never replayed.

Each event also appears in **Logbook**, and in the door's **Activity** on its device page, for example "Front Door logged access permitted by Alex Smith (credential: Keyfob 12)". The user's name appears only when **Include user names in events** is on, and the credential's name only when **Include credential names in events** is on.

## How data updates

- **Events:** live, on Direct and Remote. The integration subscribes to the server's live event feed, the same one the Paxton10 web app uses, and fires each event as it arrives. On Direct that's usually within a second. With **Include user names in events** on, an event whose live message names the user by ID only first waits for one event log read to get the name. On Paxton10 4.11, fob and card events arrive with the name, and software opens don't. If that read fails, the event still fires, without the name. Every 5 minutes the integration also reads the event log, in case the feed missed one.
- **Events while the live feed is down:** every 10 seconds, the integration reads the newest 50 entries in the event log and fires the ones it hasn't seen. It tries the live feed again with backoff, up to every 5 minutes.
- **Door lock state:** live on the same feed, and also read every 30 seconds. A poll never overwrites a newer live update.
- **Summary:** every 30 seconds. The figures are Paxton's own, and Paxton doesn't always keep them up to date: see [Known limitations](#known-limitations).
- **Controllers and entry panels** (connectivity, status, battery, power supply, firmware): the full list is large, about 57 KB per controller, so it's read only when needed. That's straight away when the summary's offline device or unacknowledged alarm count changes, or when a controller reports a hardware event (such as a restart, a reinstate, going offline or online, a power failure, or a low battery), and otherwise every 10 minutes (30 minutes on Remote). The hourly layout check reads the list too, and counts as one of these reads. A restart or reinstate shows within about a second, through its hardware event. Going offline should log a hardware event too, but that hasn't been seen on a live site yet; failing that, it shows once Paxton's offline device count changes, or at the next controller read. A change that moves neither count and logs no event, such as a firmware version, can take up to 10 minutes (30 on Remote) to show.
- **Layout:** every hour, the integration reads the device tree and the controller list again. It adds new doors and devices, and removes devices that are no longer on the server.

If the device read or an event log read fails, every entity becomes unavailable and Home Assistant logs it once. Entities stay unavailable until the read that failed succeeds again. A success on the other read doesn't hide the failure. Each read retries with backoff up to 5 minutes. Tokens last 12 hours. The integration signs in again by itself when a token expires. If the server rejects the stored credentials, polling stops and Home Assistant asks you to sign in again, so a changed password can't lock the account.

An event never fires twice, even when it arrives both live and from the event log. A live feed failure on its own doesn't make entities unavailable, because the event log covers the gap. Home Assistant logs at info level when the live feed connects and when it falls back to polling. Diagnostics show which is in use as `event_source`: `live` or `polling`.

On Direct, the live feed is an ASP.NET SignalR 2 long poll at `/signalr` on the server. On Remote, the same events and door states arrive through Paxton's remote access relay, on the connection the integration already uses for everything else, so Remote needs no extra network access.

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

Open a gate from a dashboard button or an automation (needs **Allow door control**):

```yaml
actions:
  - action: button.press
    target:
      entity_id: button.vehicle_gate_open
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

Alert when a controller loses mains power and runs on its battery:

```yaml
triggers:
  - trigger: state
    entity_id: sensor.main_entrance_door_controller_status
    to: online_on_battery
actions:
  - action: notify.notify
    data:
      message: Main entrance door controller has lost mains power
```

Entity IDs depend on your areas and names. Check them in **Settings > Devices & services > Entities**.

## Use cases

- Alerts for forced doors, doors left open, and offline controllers.
- Releasing the gate for a visitor or a delivery from Home Assistant.
- A dashboard of controller status and unacknowledged alarms.
- Recording door activity alongside other building systems.

## Known limitations

- The API is undocumented and can change with any Paxton upgrade. Paxton publishes integration documentation for Net2 only, not Paxton10.
- Paxton10 has no software command to keep a door unlocked or to lock it. Its software open is a timed release, which is what the **Open** button sends. A door stays unlocked only through Paxton10's own configuration: toggle operating mode, a time profile, triggers and actions, or a fire alarm. That's why the integration has an **Open** button and a Lock sensor, not a lock entity.
- Paxton10 doesn't open a door from software while an intruder alarm covering it is armed, or while the door is in lockdown, unless the account is exempt from the lockdown. Paxton accepts the request anyway, so the integration waits up to 5 seconds for the door to unlock, and fails the press if it doesn't. Without door state, it can't check, and the press always succeeds.
- Lockdown events (lockdown activated or deactivated, doors entering or leaving lockdown) arrive as `other`.
- While the live feed is down, events are polled. A door event can then take up to 10 seconds to appear.
- The Lock sensor shows the lock state, not whether the door is physically open. Paxton only reports forced or left open with a door contact fitted, and the integration then shows it as unlocked, with `door_state` set to `forced_or_left_open`.
- The **Forced or left open** sensor is untested on a live site. The forced and left open door events (`forced`, `left_open`) are separate: they come from Paxton's events, live or from the log, and don't depend on this sensor.
- While polling, if more than 50 events happen between two polls, only the newest 50 are fired.
- Controller status and the summary are polled, not pushed. Paxton accepts the live feed's controller status and battery subscriptions but sent nothing during a live controller restart and reinstate, so the integration doesn't use them. The hardware events that trigger an early device refresh come from the Paxton10 web app's event list. Only type 12, which Paxton logs when a controller restarts or is reinstated, has been seen on a live site.
- The Connectivity mapping comes from the Paxton10 web app's status list. Online and refreshing have been seen live. Offline, on battery, updating, rebooting, and reinstating haven't yet.
- The controller list is large (about 57 KB per controller) because Paxton includes every input and output. That's why it's read every 10 minutes (30 on Remote), or when something changes, rather than every 30 seconds. Traffic scales with the number of controllers: about 8 MB a day per controller on Direct, and about 3 MB a day per controller on Remote. Before version 0.7.0, which read it every 30 seconds, it was about 165 MB a day per controller. The rest adds about 25 MB a day, depending on how busy the doors are. On Remote, all of it goes through Paxton's relay, which Paxton doesn't support for this use and could limit.
- **Active users** can be hours out of date. Paxton resets it at midnight UTC, and recalculates it only when a new session starts: when someone signs in to the Paxton10 web app, or when Home Assistant starts or reloads the integration. It doesn't change each time someone uses a credential, and reading it more often doesn't help. Signing in again on a schedule would refresh it, but every sign-in adds an "operator logged on" entry to Paxton's event log, so the integration doesn't. Use door events for anything that needs to be timely. The offline device count comes from the same summary. A controller that restarts or is reinstated also logs a hardware event, which triggers a controller read straight away; going offline is expected to as well, but that hasn't been seen on a live site yet.
- Lists are read 100 items at a time, and only the first 100 are read: the controllers, the entry panels, and each group in the device tree. A site with more than 100 of any of these would be missing the rest. The largest site tested has 13 doors.
- The server isn't discovered automatically. Enter its address yourself.

## Safety rules

These rules are enforced in the client (`api.py`) and are covered by the tests:

- The client never sends `DELETE`. `DELETE /api/v2/Events/All` wipes the server's whole event log.
- The client sends `POST` or `PUT` only to paths on its allowlist. The only write on that list is door release (`/api/v2/System/ActivateAppliances`), and it's blocked unless **Allow door control** is on.
- The integration never calls endpoints that look like reads but change configuration, such as `POST /api/v2/device/states`, or `POST` and `PUT` on users, time constraints, dashboards, or permissions.
- Tokens, passwords, and hashes are never logged. Diagnostics redact credentials, addresses, serial numbers, the site ID, user names, and credential names.
- If the account isn't allowed to read something (HTTP 403 or 405, or 404 for door state), the integration skips those entities instead of failing. A 401 means the sign-in was rejected: the integration signs in again once, and if that's rejected too, Home Assistant asks you to sign in again.

## Troubleshooting

| Symptom | What to check |
|---|---|
| **Can't connect** during setup | For Direct, check that Home Assistant can reach the server on port 443. For Remote, check that remote access is on in Paxton10. |
| **The username or password is wrong** | Sign in to the Paxton10 web UI with the same account. |
| **Open** fails: the door didn't unlock within 5 seconds | Check whether an intruder alarm covering the door is armed, whether the door is in lockdown, and whether the door is online. Also check that the account has building permission to the door. |
| Fewer entities than expected | The account may lack permission for devices or the summary. Try an administrator account to compare. |
| No door events, and the log says the account can't read the event log | The Paxton account lacks the **Reports** permission, which the event log needs. Everything else keeps working without it. Grant the permission in Paxton10, then reload the integration. |
| No door events | Download diagnostics (open the integration, click the three dots, then **Download diagnostics**) and check `event_log_forbidden` and `event_source`. `event_log_forbidden: true` means the account lacks the **Reports** permission (see the row above). If the event poll itself is failing, every entity is unavailable and the log says why; turn on debug logging for `custom_components.paxton10` for more detail. `last_event_id` isn't a health check: it's `null` while the event log is empty or unreadable, and it can be set by the start-up read alone. |
| **Paxton10 is using the fallback route** repair | The main route failed. Check the network path. The repair clears itself once the main route works again. |

For more answers, see the [FAQ](https://github.com/urbozz/paxton10-ha/wiki/FAQ) in the wiki.

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

```bash
.venv/bin/ruff check .
```

CI runs the same checks: ruff, mypy, and the tests, plus Home Assistant's hassfest and the HACS validation.

Before a release, test on a real system: add the integration over Direct and then over Remote, confirm the entities, and test door release on one door with someone watching it.

## Licence

MIT. See `LICENSE`.

This project isn't affiliated with or endorsed by Paxton Access Ltd. Paxton and Paxton10 are trademarks of their owner.
