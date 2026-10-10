#!/usr/bin/env python3
"""Paxton10 event log probe: checks the integration's event parser against a live server.

Signs in, makes the same event query the integration makes, and reports the response
shape and how many rows parse. Read-only: the query is a POST on the client's read list.

Privacy: strings are replaced by their length, except the .NET "$type" names. Numbers,
booleans, and list lengths are kept, so event ids, type ids, and door ids stay visible.

Usage, from the repository root (needs only aiohttp):
    python3 tools/event_probe.py --direct 192.0.2.10
    python3 tools/event_probe.py --remote abc123
Add --clipboard to read the password from the macOS clipboard.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import subprocess
import sys
import types
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp

# Load the integration's modules without its __init__.py, which needs Home Assistant.
_PKG = Path(__file__).resolve().parent.parent / "custom_components" / "paxton10"
_pkg = types.ModuleType("paxton10")
_pkg.__path__ = [str(_PKG)]
sys.modules["paxton10"] = _pkg

from paxton10.api import (
    DirectTransport,
    PaxtonAuthError,
    PaxtonClient,
    PaxtonError,
    RemoteTransport,
)
from paxton10.models import event_filter, parse_event
from paxton10.source import EVENTS_PATH


def shape(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return "…"
    if isinstance(value, dict):
        return {k: (v if k == "$type" else shape(v, depth + 1)) for k, v in value.items()}
    if isinstance(value, list):
        return [shape(v, depth + 1) for v in value[:5]] + ([f"… {len(value) - 5} more"] if len(value) > 5 else [])
    if isinstance(value, str):
        return f"<str {len(value)}>"
    return value


def read_password(from_clipboard: bool) -> str:
    if from_clipboard:
        return subprocess.run(["pbpaste"], capture_output=True, text=True, check=True).stdout.rstrip("\r\n")
    return getpass.getpass("Password (not shown): ")


async def run(args: argparse.Namespace) -> int:
    username = input("Paxton username (email): ").strip()
    password = read_password(args.clipboard)
    async with aiohttp.ClientSession() as session:
        transport = DirectTransport(session, args.direct) if args.direct else RemoteTransport(session, args.remote)
        client = PaxtonClient(transport)
        try:
            await client.sign_in(username, password)
        except PaxtonAuthError as err:
            print(f"Sign-in failed: {err}")
            await client.close()
            return 1
        except PaxtonError as err:
            print(f"Connection failed ({transport.name}): {err} [{type(err.__cause__).__name__}]")
            await client.close()
            return 1
        finally:
            del password

        version = await client.get("/api/v1/System/Software/Version")
        params = await client.get("/api/v2/System/Parameters/All")
        regional = (params.body or {}).get("RegionalSettings") or {} if isinstance(params.body, dict) else {}
        offset = int(regional.get("MinutesUtcOffset") or 0)
        resp = await client.post(EVENTS_PATH, event_filter(offset))
        await client.close()

    body = resp.body
    print(f"Signed in over {transport.name}. Server {version.body}. UTC offset {offset} min.")
    print(f"POST {EVENTS_PATH}: HTTP {resp.status}, body type {type(body).__name__}")
    report: dict[str, Any] = {
        "route": transport.name,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": resp.status,
        "body_type": type(body).__name__,
    }
    if isinstance(body, dict):
        print(f"Top-level keys: {sorted(body)}")
        report["top_level"] = {k: shape(v) if not isinstance(v, list) else f"list[{len(v)}]" for k, v in body.items()}
    rows = body.get("Result") if isinstance(body, dict) else body if isinstance(body, list) else None
    if isinstance(rows, list):
        parsed = [parse_event(r, False) if isinstance(r, dict) else None for r in rows]
        ok = [p for p in parsed if p]
        print(f"Rows: {len(rows)}. Parsed by the integration: {len(ok)}.")
        if rows and isinstance(rows[0], dict):
            print(f"Row keys: {sorted(rows[0])}")
        if ok:
            print(f"Newest event id: {max(p.event_id for p in ok)}")
            print(f"Event type ids: {dict(Counter(p.event_type_id for p in ok).most_common())}")
        # Whose events and which doors the account can see, by Paxton's numeric ids only (no names).
        users = Counter(
            r["UserData"]["UserId"] if isinstance(r.get("UserData"), dict) and isinstance(r["UserData"].get("UserId"), int)
            else "no user"
            for r in rows if isinstance(r, dict)
        )
        doors = Counter(p.door_ids[0] if p.door_ids else "no door" for p in ok)
        print(f"Users on this page: {len(users) - ('no user' in users)} different user ids. Rows per user id: {dict(users.most_common())}")
        print(f"Doors on this page: {len(doors) - ('no door' in doors)} different doors. Rows per door id: {dict(doors.most_common())}")
        report["rows_per_user_id"] = {str(k): v for k, v in users.items()}
        report["rows_per_door_id"] = {str(k): v for k, v in doors.items()}
        report["rows"] = len(rows)
        report["parsed"] = len(ok)
        report["sample_rows"] = [shape(r) for r in rows[:5]]
    else:
        print("No Result list found. See the report for the body shape.")
        report["body_shape"] = shape(body)

    out = Path(args.out or f"probe-events-{transport.name}.json")
    out.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"Report: {out}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    route = parser.add_mutually_exclusive_group(required=True)
    route.add_argument("--direct", metavar="HOST", help="server address on the site network")
    route.add_argument("--remote", metavar="REMOTE_ID", help="remote ID from the paxton10remote.com address")
    parser.add_argument("--clipboard", action="store_true", help="read the password from the macOS clipboard")
    parser.add_argument("--out", help="report path (default probe-events-<route>.json)")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
