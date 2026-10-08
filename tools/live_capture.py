#!/usr/bin/env python3
"""Paxton10 live capture: checks the integration's hub client and status reads against a live server.

Signs in over Direct and discovers the site as the integration does. Then it:

1. Reads door state with POST /api/v1/Appliance/Connector/Status, as the web app does.
2. Opens the SignalR long poll with the integration's own hub client (hub.py) and subscribes
   to live events, door state, controller and entry panel status, and controller batteries.
3. Prints every push for a while, and saves a report.

Open a door during the capture, and leave one open long enough to raise its left-open alarm.

Read-only: the client only sends the hub's subscribe and unsubscribe methods, and the door
state read is a query. Both clients refuse anything else.

Privacy: strings are replaced by their length, except hub and method names, .NET "$type"
names, state and status codes, and EventTime (to measure delay). Numbers are kept.

Usage, from the repository root (needs only aiohttp):
    python3 tools/live_capture.py --direct 192.0.2.10
    python3 tools/live_capture.py --direct 192.0.2.10 --seconds 180
Add --clipboard to read the password from the macOS clipboard.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import subprocess
import sys
import time
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp

# Load the integration's modules without its __init__.py, which needs Home Assistant.
_PKG = Path(__file__).resolve().parent.parent / "custom_components" / "paxton10"
_pkg = types.ModuleType("paxton10")
_pkg.__path__ = [str(_PKG)]
sys.modules["paxton10"] = _pkg

from paxton10.api import PaxtonAuthError, PaxtonError, password_hash
from paxton10.connection import PaxtonConnection
from paxton10.const import ROUTE_DIRECT
from paxton10.discovery import discover_site
from paxton10.hub import (
    METHOD_SUBSCRIBE_BATTERY,
    METHOD_SUBSCRIBE_DEVICE_STATUS,
    METHOD_SUBSCRIBE_DOOR_STATE,
    METHOD_SUBSCRIBE_EVENTS,
    METHOD_UNSUBSCRIBE_BATTERY,
    METHOD_UNSUBSCRIBE_DEVICE_STATUS,
    METHOD_UNSUBSCRIBE_DOOR_STATE,
    METHOD_UNSUBSCRIBE_EVENTS,
    NOTIFY_DOOR_STATE,
    LongPollHub,
    event_rows,
)
from paxton10.models import KIND_CONTROLLER, live_event_filter, parse_event, parse_time

# Strings kept as they are: names, codes, and times. Everything else is replaced by its length.
KEEP = {"$type", "H", "M", "EventTime", "Response", "StateValue", "Status", "State", "Value"}


def shape(value: Any, depth: int = 0, key: str = "") -> Any:
    if depth > 8:
        return "…"
    if isinstance(value, dict):
        return {k: shape(v, depth + 1, k) for k, v in value.items()}
    if isinstance(value, list):
        return [shape(v, depth + 1) for v in value[:20]] + ([f"… {len(value) - 20} more"] if len(value) > 20 else [])
    if isinstance(value, str):
        return value if key in KEEP else f"<str {len(value)}>"
    return value


class RecordingHub(LongPollHub):
    """The integration's hub client, recording each reply."""

    def __init__(self, *args: Any, log: list[dict[str, Any]], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.log = log

    async def _request(self, method: str, path: str, query: dict[str, str], **kwargs: Any) -> Any:
        started = time.monotonic()
        reply = await super()._request(method, path, query, **kwargs)
        entry = {
            "at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "request": f"{method} /signalr/{path}",
            "ms": round((time.monotonic() - started) * 1000),
            "reply": shape(reply),
        }
        self.log.append(entry)
        print(f"{entry['at']}  {entry['request']:<26} {entry['ms']:>6} ms  {json.dumps(entry['reply'])[:300]}")
        return reply


def read_password(from_clipboard: bool) -> str:
    if from_clipboard:
        return subprocess.run(["pbpaste"], capture_output=True, text=True, check=True).stdout.rstrip("\r\n")
    return getpass.getpass("Password (not shown): ")


async def run(args: argparse.Namespace) -> int:
    username = input("Paxton username (email): ").strip()
    password = read_password(args.clipboard)
    log: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    try:
        return await capture(args, username, password, log, events)
    finally:
        # Also on Ctrl+C, so a capture stopped early still leaves its report.
        write_report(args.out, log, events)


# The web app's ApplianceState values for doors; other values belong to other appliance types.
DOOR_STATES = {1: "open_unlocked", 2: "locked", 3: "forced_or_left_open", 4: "offline", 5: "online"}
DOOR_STATE_PATH = "/api/v1/Appliance/Connector/Status"


def door_state_summary(rows: Any) -> list[str]:
    out = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        try:
            value = int(row.get("StateValue"))
        except (TypeError, ValueError):
            value = None
        out.append(f"{row.get('EntityId')}: {row.get('StateValue')!r} ({DOOR_STATES.get(value, 'not a door state')})")
    return out


async def capture(
    args: argparse.Namespace, username: str, password: str, log: list[dict[str, Any]], events: list[dict[str, Any]]
) -> int:
    async with aiohttp.ClientSession() as session:
        conn = PaxtonConnection(session, ROUTE_DIRECT, args.direct, username, password_hash(password))
        del password
        try:
            site = await discover_site(conn)
            target = await conn.hub_target()
        except PaxtonAuthError as err:
            print(f"Sign-in failed: {err}")
            await conn.close()
            return 1
        except PaxtonError as err:
            print(f"Connection failed: {err}")
            await conn.close()
            return 1
        assert target
        offset = site.server.utc_offset_minutes
        door_ids = sorted(site.doors)
        device_ids = sorted(site.devices)
        controller_ids = sorted(i for i, d in site.devices.items() if d.kind == KIND_CONTROLLER)
        print(f"Signed in. UTC offset {offset} min. {len(door_ids)} doors, {len(device_ids)} devices.\n")

        async def read_door_state(when: str) -> None:
            """Door state the way the web app loads it before subscribing."""
            try:
                states = await conn.post(DOOR_STATE_PATH, door_ids)
                log.append({"request": f"POST {DOOR_STATE_PATH} ({when})", "reply": shape(states)})
                print(f"POST {DOOR_STATE_PATH} ({when}): {json.dumps(shape(states))[:600]}")
                for line in door_state_summary(states):
                    print(f"  door {line}")
            except PaxtonError as err:
                log.append({"request": f"POST {DOOR_STATE_PATH} ({when})", "error": str(err)})
                print(f"POST {DOOR_STATE_PATH} ({when}) failed: {err}")
            print()

        await read_door_state("before")

        hub = RecordingHub(session, *target, log=log)
        subscribed: list[str] = []
        try:
            await hub.connect()
            # Try the web app's live view filter first, then the site graphic's (every category listed).
            for all_categories in (False, True):
                label = "every category listed" if all_categories else "categories unrestricted"
                try:
                    await hub.invoke(METHOD_SUBSCRIBE_EVENTS, live_event_filter(offset, all_categories))
                except PaxtonError as err:
                    print(f"Subscribe to events with {label} failed: {err}")
                    continue
                print(f"Subscribed to events with {label}.")
                subscribed.append(METHOD_UNSUBSCRIBE_EVENTS)
                break
            for subscribe, unsubscribe, ids, what in (
                (METHOD_SUBSCRIBE_DOOR_STATE, METHOD_UNSUBSCRIBE_DOOR_STATE, door_ids, "door state"),
                (METHOD_SUBSCRIBE_DEVICE_STATUS, METHOD_UNSUBSCRIBE_DEVICE_STATUS, device_ids, "device status"),
                (METHOD_SUBSCRIBE_BATTERY, METHOD_UNSUBSCRIBE_BATTERY, controller_ids, "battery"),
            ):
                try:
                    result = await hub.invoke(subscribe, ids)
                except PaxtonError as err:
                    print(f"Subscribe to {what} failed: {err}")
                    continue
                print(f"Subscribed to {what} for {len(ids)} ids (result {json.dumps(shape(result))[:200]}).")
                subscribed.append(unsubscribe)

            print(f"\nListening for {args.seconds} s. Open a door now, and leave one open until it alarms.\n")
            deadline = time.monotonic() + args.seconds
            while time.monotonic() < deadline:
                try:
                    messages = await asyncio.wait_for(hub.poll(), timeout=max(1, deadline - time.monotonic()))
                except asyncio.TimeoutError:
                    break
                received = datetime.now(timezone.utc)
                for message in messages:
                    rows = event_rows(message)
                    if not rows:
                        # Door state, device status, battery, or anything else: show it all, redacted.
                        print(f"  push {message.hub}.{message.method}: {json.dumps(shape(message.args))[:600]}")
                        if message.method.lower() == NOTIFY_DOOR_STATE.lower() and message.args:
                            for line in door_state_summary(message.args[0]):
                                print(f"    door {line}")
                        events.append({
                            "received": received.isoformat(timespec="milliseconds"),
                            "method": message.method,
                            "args": shape(message.args),
                        })
                        continue
                    print(f"  push {message.hub}.{message.method}: {len(rows)} event row(s)")
                    for row in rows:
                        parsed = parse_event(row, False)
                        when = parse_time(row.get("EventTime"))
                        delay = (received - when).total_seconds() if when else None
                        summary = {
                            "received": received.isoformat(timespec="milliseconds"),
                            "method": message.method,
                            "event_time": row.get("EventTime"),
                            "delay_s": round(delay, 2) if delay is not None else None,
                            "event_type_id": row.get("EventTypeId"),
                            "parsed_type": parsed.event_type if parsed else None,
                            "door_ids": list(parsed.door_ids) if parsed else None,
                            "user_data": shape(row.get("UserData")),
                        }
                        events.append(summary)
                        print(f"    type {summary['event_type_id']} ({summary['parsed_type']}), doors "
                              f"{summary['door_ids']}, delay {summary['delay_s']} s")
            print()
            await read_door_state("after")
        except PaxtonError as err:
            print(f"\nHub failed: {err}")
        finally:
            for unsubscribe in subscribed:
                if not hub.connected:
                    break
                try:
                    await hub.invoke(unsubscribe)
                except PaxtonError as err:
                    print(f"{unsubscribe} failed: {err}")
            await hub.close()
            await conn.close()

    return 0


def write_report(path: str | None, log: list[dict[str, Any]], events: list[dict[str, Any]]) -> None:
    out = Path(path or "probe-live-direct.json")
    try:
        out.write_text(json.dumps({"log": log, "events": events}, indent=1, ensure_ascii=False), encoding="utf-8")
    except OSError as err:
        # Don't hide the real outcome, such as a failed sign-in, behind a report error.
        print(f"\nCouldn't write the report to {out}: {err}")
        return
    print(f"\n{len(events)} live event(s). Report: {out}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--direct", metavar="HOST", required=True, help="server address on the site network")
    parser.add_argument("--seconds", type=int, default=180, help="how long to listen (default 180)")
    parser.add_argument("--clipboard", action="store_true", help="read the password from the macOS clipboard")
    parser.add_argument("--out", help="report path (default probe-live-direct.json)")
    try:
        return asyncio.run(run(parser.parse_args()))
    except KeyboardInterrupt:
        # Ctrl+C cancels the capture task, so capture()'s finally has already unsubscribed and
        # aborted the hub, and run()'s finally has written the report.
        print("\nStopped.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
