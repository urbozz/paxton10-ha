"""Device polling: cheap reads every interval, the large controller list only when needed."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.paxton10 import source as source_mod
from custom_components.paxton10.const import (
    DEFAULT_DEVICE_FULL_INTERVAL,
    OPT_DEVICE_FULL_INTERVAL,
)
from custom_components.paxton10.source import PollingSource

from .conftest import FakeServer, controller, event
from .test_init import coordinator, setup, source
from .test_live import fast_sleep  # noqa: F401  (fixture)

CONTROLLERS = "/api/v1/Devices/1/false?page=0&pageSize=100"
SUMMARY = "/api/v1/System/Summary"


def reads(server: FakeServer, path: str) -> int:
    return sum(1 for c in server.calls if c[2] == path)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> dict[str, float]:
    now = {"t": 1000.0}
    monkeypatch.setattr(source_mod, "_monotonic", lambda: now["t"])
    return now


async def stopped(hass: HomeAssistant, options: dict[str, Any] | None = None) -> PollingSource:
    entry = await setup(hass, options)
    src = source(entry)
    await src.async_stop()  # drive polls by hand
    return src


async def test_cheap_poll_skips_the_controller_list(
    hass: HomeAssistant, server: FakeServer, clock: dict[str, float]
) -> None:
    src = await stopped(hass)
    assert src._device_full_interval == DEFAULT_DEVICE_FULL_INTERVAL
    before, summaries = reads(server, CONTROLLERS), reads(server, SUMMARY)
    await src.poll_devices()
    assert reads(server, CONTROLLERS) == before  # nothing changed, not due
    assert reads(server, SUMMARY) == summaries + 1
    # Once the full refresh interval has passed, the list is read again.
    clock["t"] += DEFAULT_DEVICE_FULL_INTERVAL
    await src.poll_devices()
    assert reads(server, CONTROLLERS) == before + 1
    await src.poll_devices()
    assert reads(server, CONTROLLERS) == before + 1


async def test_summary_change_reads_the_list_at_once(
    hass: HomeAssistant, server: FakeServer, clock: dict[str, float], monkeypatch: pytest.MonkeyPatch
) -> None:
    src = await stopped(hass)
    before = reads(server, CONTROLLERS)
    server.controllers = [controller(4001, 2001, status=4)]
    real = source_mod.read_summary

    async def offline_one(conn: Any) -> dict[str, int]:
        summary = await real(conn)
        return {**summary, "offline_devices": 1}

    monkeypatch.setattr(source_mod, "read_summary", offline_one)
    coord = coordinator(hass.config_entries.async_entries("paxton10")[0])
    src._callback = coord._async_handle_update  # stopping the source unhooked it
    await src.poll_devices()
    assert reads(server, CONTROLLERS) == before + 1
    await hass.async_block_till_done()
    assert coord.data.devices[4001].online is False
    # The same count again doesn't read it again.
    await src.poll_devices()
    assert reads(server, CONTROLLERS) == before + 1


async def test_hardware_event_wakes_the_device_loop(
    hass: HomeAssistant, server: FakeServer, clock: dict[str, float]
) -> None:
    """A power failure or controller offline event reads the list now, not at the next full refresh."""
    src = await stopped(hass)
    before = reads(server, CONTROLLERS)
    server.events.append(event(101, 21, door=0))  # power failure: not about a door
    await src.poll_events()
    assert src._devices_requested and src._device_wake.is_set()
    await src.poll_devices()
    assert reads(server, CONTROLLERS) == before + 1
    assert not src._devices_requested
    # A door event doesn't.
    server.events.append(event(102, 5))
    await src.poll_events()
    assert not src._devices_requested


async def test_without_the_summary_every_poll_reads_the_list(
    hass: HomeAssistant, server: FakeServer, clock: dict[str, float]
) -> None:
    src = await stopped(hass)
    src._site.can_read_summary = False  # nothing else would show a change
    before = reads(server, CONTROLLERS)
    await src.poll_devices()
    await src.poll_devices()
    assert reads(server, CONTROLLERS) == before + 2


async def test_full_interval_option(hass: HomeAssistant, server: FakeServer) -> None:
    src = await stopped(hass, {OPT_DEVICE_FULL_INTERVAL: 120})
    assert src._device_full_interval == 120


async def test_nap_wakes_early(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []

    async def long_sleep(delay: float) -> None:
        slept.append(delay)
        await asyncio.sleep(3600)

    monkeypatch.setattr(source_mod, "_sleep", long_sleep)
    wake = asyncio.Event()
    nap = asyncio.ensure_future(source_mod._nap(30, wake))
    await asyncio.sleep(0)
    wake.set()
    await asyncio.wait_for(nap, 1)  # returned without the hour-long sleep
    assert slept == [30] and not wake.is_set()

    plain: list[float] = []

    async def quick(delay: float) -> None:
        plain.append(delay)

    monkeypatch.setattr(source_mod, "_sleep", quick)
    await source_mod._nap(5, None)
    assert plain == [5]


@pytest.mark.parametrize("trigger", ["count_change", "hardware_event"])
async def test_failed_list_read_keeps_the_trigger_and_the_cheap_data(
    hass: HomeAssistant, server: FakeServer, clock: dict[str, float], monkeypatch: pytest.MonkeyPatch, trigger: str
) -> None:
    """Regression (v0.7.0 review): a failed list read used up its trigger and dropped the summary and door states."""
    from custom_components.paxton10.api import PaxtonError

    src = await stopped(hass)
    coord = coordinator(hass.config_entries.async_entries("paxton10")[0])
    received: list[Any] = []

    async def cb(update: Any) -> None:
        received.append(update)
        await coord._async_handle_update(update)

    src._callback = cb
    real_summary, real_devices = source_mod.read_summary, source_mod.read_devices
    if trigger == "count_change":

        async def offline_one(conn: Any) -> dict[str, int]:
            return {**(await real_summary(conn)), "offline_devices": 1}

        monkeypatch.setattr(source_mod, "read_summary", offline_one)
    else:
        src.request_device_refresh()

    async def timeout(conn: Any) -> Any:
        raise PaxtonError("GET devices: no reply within 20 s")

    monkeypatch.setattr(source_mod, "read_devices", timeout)
    server.door_states[2001] = "1"
    with pytest.raises(PaxtonError):
        await src.poll_devices()
    # The summary and door states were handed on, and the trigger is still pending.
    assert received[-1].kind == "status" and received[-1].summary and received[-1].door_states == {2001: 1, 2002: 1}
    assert src._devices_requested
    await hass.async_block_till_done()
    assert coord.data.door_states[2001] == 1

    # The run loop reports the device failure; a second failed retry doesn't flicker entities back.
    await coord._async_handle_update(source_mod.SourceUpdate("devices", error=PaxtonError("timeout")))
    assert not coord.last_update_success
    with pytest.raises(PaxtonError):
        await src.poll_devices()
    assert not coord.last_update_success

    # The next poll retries straight away (no 10-minute wait) and recovers.
    monkeypatch.setattr(source_mod, "read_devices", real_devices)
    await src.poll_devices()
    assert not src._devices_requested
    assert received[-1].kind == "devices" and received[-1].devices
    assert coord.last_update_success


async def test_request_during_a_list_read_is_kept(
    hass: HomeAssistant, server: FakeServer, clock: dict[str, float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review (v0.7.2): a hardware event during the list read used to be wiped once the read finished."""
    src = await stopped(hass)
    real = source_mod.read_devices

    async def read_and_get_asked_again(conn: Any) -> Any:
        src.request_device_refresh()  # a hardware event lands while this read is in flight
        return await real(conn)

    monkeypatch.setattr(source_mod, "read_devices", read_and_get_asked_again)
    before = reads(server, CONTROLLERS)
    await src.poll_devices(full=True)
    assert reads(server, CONTROLLERS) == before + 1
    assert src._devices_requested  # survived the read
    monkeypatch.setattr(source_mod, "read_devices", real)
    await src.poll_devices()
    assert reads(server, CONTROLLERS) == before + 2
    assert not src._devices_requested
