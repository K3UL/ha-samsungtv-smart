"""The art-mode poll must not run faster than it is scheduled (#319).

Each refresh costs two serialized IP Control round-trips (powerControl, then
artModeControl), and they share one host gate with the user-initiated writes.
On a 2022 Frame woken by an external IR blaster the poll ran far beyond its
own schedule:

    10:23:51 -> 10:24:37   90 refreshes, one every 0.51 s
    before and after       a clean 5 s, exactly the timer

So it is not a duplicated timer, and no art transition fired in that window
(the hook that refreshes eagerly logs its event, and none appears). The
trigger is still unidentified. These tests pin the bound placed on it: one
refresh at a time, and a periodic one that follows another too closely is
dropped — which caps the cost whatever is asking, and logs each skip so the
next capture shows the real rate.

The two callers that exist for immediacy keep it: the art-transition hook
(#326 — the switch must follow a transition in ~1 s, not 5) and the eager
read at startup pass force=True.
"""

import sys
import time
from types import ModuleType
from unittest.mock import MagicMock

if "pysmartthings" not in sys.modules:
    pysmartthings = ModuleType("pysmartthings")

    class _Names:
        def __getattr__(self, name):
            return name

    pysmartthings.Attribute = _Names()
    pysmartthings.Capability = _Names()
    pysmartthings.Command = _Names()
    pysmartthings.SmartThings = object
    sys.modules["pysmartthings"] = pysmartthings

import asyncio  # noqa: E402

from custom_components.samsungtv_smart.media_player import (  # noqa: E402
    IP_ART_MODE_MIN_INTERVAL,
    SamsungTVDevice,
)


def _device(*, last_refresh_ago=None, hold=0.0):
    """A paired TV whose actual read is replaced by a counter."""
    device = object.__new__(SamsungTVDevice)
    device._host = "192.168.1.125"
    device._log = MagicMock()
    device._ip_art_mode_refreshing = False
    device._ip_art_mode_refresh_again = False
    device._ip_art_mode_last_refresh = (
        0.0 if last_refresh_ago is None else time.monotonic() - last_refresh_ago
    )
    device.reads = []

    async def _once():
        device.reads.append(time.monotonic())
        if hold:
            await asyncio.sleep(hold)

    device._refresh_ip_art_mode_once = _once
    return device


# --- the periodic poll is bounded -----------------------------------------


async def test_a_periodic_refresh_too_soon_after_the_last_is_dropped():
    device = _device(last_refresh_ago=0.5)

    await device._refresh_ip_art_mode()

    assert device.reads == []
    said = " ".join(str(c.args[0]) for c in device._log.debug.call_args_list)
    assert "skipped" in said


async def test_a_periodic_refresh_at_the_normal_cadence_runs():
    device = _device(last_refresh_ago=IP_ART_MODE_MIN_INTERVAL + 1)

    await device._refresh_ip_art_mode()

    assert len(device.reads) == 1


async def test_a_burst_of_periodic_refreshes_collapses_to_one():
    # 20 calls in a tight loop, as the #319 capture shows arriving.
    device = _device(last_refresh_ago=IP_ART_MODE_MIN_INTERVAL + 1)

    for _ in range(20):
        await device._refresh_ip_art_mode()

    assert len(device.reads) == 1


# --- only one at a time ---------------------------------------------------


async def test_overlapping_refreshes_do_not_both_run():
    device = _device(last_refresh_ago=IP_ART_MODE_MIN_INTERVAL + 1, hold=0.05)

    first = asyncio.create_task(device._refresh_ip_art_mode())
    await asyncio.sleep(0)  # let it take the in-flight flag
    second = asyncio.create_task(device._refresh_ip_art_mode())
    await asyncio.gather(first, second)

    assert len(device.reads) == 1


async def test_the_guard_is_released_even_if_the_read_raises():
    device = _device(last_refresh_ago=IP_ART_MODE_MIN_INTERVAL + 1)

    async def _boom():
        raise RuntimeError("transport failure")

    device._refresh_ip_art_mode_once = _boom
    with __import__("pytest").raises(RuntimeError):
        await device._refresh_ip_art_mode()

    assert device._ip_art_mode_refreshing is False
    assert device._ip_art_mode_refresh_again is False


# --- immediacy is preserved for the callers that need it ------------------


async def test_a_forced_refresh_ignores_the_interval():
    # The art-transition hook: the switch must follow within ~1 s (#326).
    device = _device(last_refresh_ago=0.1)

    await device._refresh_ip_art_mode(force=True)

    assert len(device.reads) == 1


async def test_a_forced_request_during_a_read_is_replayed_after_it():
    # The in-flight reading may predate the transition, so it cannot answer
    # it: a second read must follow.
    device = _device(last_refresh_ago=IP_ART_MODE_MIN_INTERVAL + 1, hold=0.05)

    first = asyncio.create_task(device._refresh_ip_art_mode())
    await asyncio.sleep(0)
    forced = asyncio.create_task(device._refresh_ip_art_mode(force=True))
    await asyncio.gather(first, forced)

    assert len(device.reads) == 2


async def test_a_periodic_request_during_a_read_is_not_replayed():
    # Only forced callers earn the catch-up pass; a dropped poll just waits
    # for its next tick.
    device = _device(last_refresh_ago=IP_ART_MODE_MIN_INTERVAL + 1, hold=0.05)

    first = asyncio.create_task(device._refresh_ip_art_mode())
    await asyncio.sleep(0)
    periodic = asyncio.create_task(device._refresh_ip_art_mode())
    await asyncio.gather(first, periodic)

    assert len(device.reads) == 1


async def test_the_timestamp_advances_so_the_next_poll_is_bounded_too():
    device = _device(last_refresh_ago=IP_ART_MODE_MIN_INTERVAL + 1)

    await device._refresh_ip_art_mode()
    await device._refresh_ip_art_mode()

    assert len(device.reads) == 1
