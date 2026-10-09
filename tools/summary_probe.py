#!/usr/bin/env python3
"""Paxton10 summary probe: does reading the web app's start-up context refresh the summary?

Paxton's System/Summary figures (Active users in particular) can stay unchanged for hours,
until someone opens them in the web app. When the web app starts, it reads
Context/Runtime/<user id>, which includes the summary. This probe checks whether that read
makes Paxton recalculate it:

1. Reads System/Summary.
2. Reads Context/Runtime/<user id> and its embedded summary.
3. Reads System/Summary again, straight away and after 30 seconds.

Read-only: every request is a GET. Only the summary counts are printed or saved.

Usage, from the repository root (needs only aiohttp):
    python3 tools/summary_probe.py --direct 192.0.2.10
    python3 tools/summary_probe.py --remote abc123 --user-id 59
Add --clipboard to read the password from the macOS clipboard. Without --user-id, the
probe looks up the user id the way the web app's sign-in page does.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import getpass
import json
import subprocess
import sys
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

from paxton10.api import (
    DirectTransport,
    PaxtonAuthError,
    PaxtonClient,
    PaxtonError,
    RemoteTransport,
)
from paxton10.models import parse_summary

SUMMARY = "/api/v1/System/Summary"
RUNTIME = "/api/v1/Context/Runtime/{user_id}"
VALIDATE_EMAIL = "/api/v1/System/Validate/Email"  # the web app's sign-in lookup: email in, UserId out
USER_ID_CLAIMS = ("userId", "UserId", "user_id", "nameid", "sub")


def read_password(from_clipboard: bool) -> str:
    if from_clipboard:
        return subprocess.run(["pbpaste"], capture_output=True, text=True, check=True).stdout.rstrip("\r\n")
    return getpass.getpass("Password (not shown): ")


def user_id_from_token(token: str | None) -> int | None:
    """The user id from a JWT bearer token's claims, if the token is a JWT and has one."""
    parts = (token or "").split(".")
    if len(parts) != 3:
        return None
    try:
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except ValueError:
        return None
    for key in USER_ID_CLAIMS:
        value = claims.get(key) if isinstance(claims, dict) else None
        if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
            return int(value)
    return None


async def user_id_from_email(client: PaxtonClient, email: str) -> int | None:
    """Ask Paxton for the account's user id, as the web app's sign-in page does.

    It's a read, but a POST, so the client's allowlist (which this probe otherwise keeps to)
    doesn't know it. It goes straight to the transport, for this one lookup only.
    """
    resp = await client.transport.send(
        "POST", VALIDATE_EMAIL, client.token, json.dumps(email), "application/json; charset=utf-8"
    )
    value = resp.body.get("UserId") if isinstance(resp.body, dict) else None
    return value if isinstance(value, int) and value > 0 else None


async def summary(client: PaxtonClient, label: str) -> dict[str, int]:
    resp = await client.get(SUMMARY)
    figures = parse_summary(resp.body)
    print(f"{label:<34} HTTP {resp.status}  {figures}")
    return figures


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

        user_id = args.user_id or user_id_from_token(client.token) or await user_id_from_email(client, username)
        if user_id is None:
            print("Couldn't find your Paxton user id in the sign-in token. Run again with --user-id <id>.")
            await client.close()
            return 1
        print(f"Signed in over {transport.name} as user {user_id}.\n")

        report: dict[str, Any] = {"route": transport.name, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        report["before"] = await summary(client, "1. System/Summary before")
        resp = await client.get(RUNTIME.format(user_id=user_id))
        runtime = resp.body if isinstance(resp.body, dict) else {}
        report["runtime_status"] = resp.status
        report["runtime"] = parse_summary(runtime.get("Summary"))
        print(f"{'2. Context/Runtime summary':<34} HTTP {resp.status}  {report['runtime']}")
        if not report["runtime"]:
            raw = runtime.get("Summary")
            print(f"   (no figures parsed; Summary is {type(raw).__name__}, top-level keys {sorted(runtime)[:12]})")
        report["after"] = await summary(client, "3. System/Summary straight after")
        await asyncio.sleep(30)
        report["after_30s"] = await summary(client, "4. System/Summary 30 s later")
        await client.close()

    changed = any(report[k] != report["before"] for k in ("runtime", "after", "after_30s"))
    print("\nThe summary changed after reading Context/Runtime." if changed else "\nNo change.")
    out = Path(args.out or f"probe-summary-{transport.name}.json")
    out.write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(f"Report: {out}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    route = parser.add_mutually_exclusive_group(required=True)
    route.add_argument("--direct", metavar="HOST", help="server address on the site network")
    route.add_argument("--remote", metavar="REMOTE_ID", help="remote ID from the paxton10remote.com address")
    parser.add_argument("--user-id", type=int, help="your Paxton user id, if the token doesn't carry it")
    parser.add_argument("--clipboard", action="store_true", help="read the password from the macOS clipboard")
    parser.add_argument("--out", help="report path (default probe-summary-<route>.json)")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
