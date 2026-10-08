#!/usr/bin/env python3
"""Paxton10 live event capture: checks the integration's hub client against a live server.

Signs in over Direct, opens the SignalR long poll with the integration's own hub client
(hub.py), subscribes to live events, and prints every reply for a while. Open a door during
the capture so an event arrives.

Read-only: the only hub calls are SubscribeToLiveEvents and UnsubscribeFromLiveEvents, and
the client refuses any other method.

Privacy: strings are replaced by their length, except hub and method names, .NET "$type"
names, and EventTime (to measure delay). Numbers, booleans, and list lengths are kept.

Usage, from the repository root (needs only aiohttp):
    python3 tools/live_capture.py --direct 192.0.2.10
    python3 tools/live_capture.py --direct 192.0.2.10 --seconds 120
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

from paxton10.api import DirectTransport, PaxtonAuthError, PaxtonClient, PaxtonError
from paxton10.hub import (
    METHOD_SUBSCRIBE_EVENTS,
    METHOD_UNSUBSCRIBE_EVENTS,
    LongPollHub,
    event_rows,
)
from paxton10.models import live_event_filter, parse_event, parse_time

KEEP = {"$type", "H", "M", "EventTime", "Response"}


def shape(value: Any, depth: int = 0, key: str = "") -> Any:
    if depth > 8:
        return "…"
    if isinstance(value, dict):
        return {k: shape(v, depth + 1, k) for k, v in value.items()}
    if isinstance(value, list):
        return [shape(v, depth + 1) for v in value[:5]] + ([f"… {len(value) - 5} more"] if len(value) > 5 else [])
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


async def capture(
    args: argparse.Namespace, username: str, password: str, log: list[dict[str, Any]], events: list[dict[str, Any]]
) -> int:
    async with aiohttp.ClientSession() as session:
        transport = DirectTransport(session, args.direct)
        client = PaxtonClient(transport)
        try:
            await client.sign_in(username, password)
        except PaxtonAuthError as err:
            print(f"Sign-in failed: {err}")
            return 1
        except PaxtonError as err:
            print(f"Connection failed: {err}")
            return 1
        finally:
            del password
        params = await client.get("/api/v2/System/Parameters/All")
        regional = params.body.get("RegionalSettings") or {} if isinstance(params.body, dict) else {}
        offset = int(regional.get("MinutesUtcOffset") or 0)
        print(f"Signed in. UTC offset {offset} min. Opening the hub.\n")

        hub = RecordingHub(session, transport.base_url, lambda: client.token, log=log)
        try:
            await hub.connect()
            # Try the web app's live view filter first, then the site graphic's (every category listed).
            for all_categories in (False, True):
                label = "every category listed" if all_categories else "categories unrestricted"
                try:
                    result = await hub.invoke(METHOD_SUBSCRIBE_EVENTS, live_event_filter(offset, all_categories))
                except PaxtonError as err:
                    print(f"\nSubscribe with {label} failed: {err}")
                    continue
                print(f"\nSubscribed with {label} (result {shape(result)}).")
                break
            else:
                raise PaxtonError("every subscribe variant failed")
            print(f"Listening for {args.seconds} s. Open a door now.\n")
            deadline = time.monotonic() + args.seconds
            while time.monotonic() < deadline:
                try:
                    messages = await asyncio.wait_for(hub.poll(), timeout=max(1, deadline - time.monotonic()))
                except asyncio.TimeoutError:
                    break
                received = datetime.now(timezone.utc)
                for message in messages:
                    rows = event_rows(message)
                    print(f"  push {message.hub}.{message.method}: {len(rows)} event row(s)")
                    for row in rows:
                        parsed = parse_event(row, False)
                        when = parse_time(row.get("EventTime"))
                        delay = (received - when).total_seconds() if when else None
                        summary = {
                            "received": received.isoformat(timespec="milliseconds"),
                            "event_time": row.get("EventTime"),
                            "delay_s": round(delay, 2) if delay is not None else None,
                            "event_type_id": row.get("EventTypeId"),
                            "parsed_type": parsed.event_type if parsed else None,
                            "door_ids": list(parsed.door_ids) if parsed else None,
                            "row_keys": sorted(row),
                        }
                        events.append(summary)
                        print(f"    type {summary['event_type_id']} ({summary['parsed_type']}), doors "
                              f"{summary['door_ids']}, delay {summary['delay_s']} s")
        except PaxtonError as err:
            print(f"\nHub failed: {err}")
        finally:
            if hub.connected:
                try:
                    await hub.invoke(METHOD_UNSUBSCRIBE_EVENTS)
                except PaxtonError as err:
                    print(f"Unsubscribe failed: {err}")
            await hub.close()
            await client.close()

    return 0


def write_report(path: str | None, log: list[dict[str, Any]], events: list[dict[str, Any]]) -> None:
    out = Path(path or "probe-live-direct.json")
    out.write_text(json.dumps({"log": log, "events": events}, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"\n{len(events)} live event(s). Report: {out}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--direct", metavar="HOST", required=True, help="server address on the site network")
    parser.add_argument("--seconds", type=int, default=60, help="how long to listen (default 60)")
    parser.add_argument("--clipboard", action="store_true", help="read the password from the macOS clipboard")
    parser.add_argument("--out", help="report path (default probe-live-direct.json)")
    try:
        return asyncio.run(run(parser.parse_args()))
    except KeyboardInterrupt:
        # The hub client has already unsubscribed and aborted in run()'s finally.
        print("\nStopped.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
