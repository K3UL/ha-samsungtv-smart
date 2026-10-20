"""A user-initiated write must not queue behind the background polls (#319).

Every entity talks to the TV through one serialized channel, because the
JSON-RPC server on port 1516 resets overlapping connections. That lock was
FIFO, so an Art Mode write landed behind whatever polling was already queued.

Measured on a 2022 Frame woken by an external IR blaster, where the wake is
invisible to Home Assistant and every poller rushes the TV at once:

    10:23:29.172  Art Mode ON attempt 1/3      (asyncio.timeout(10) each)
    10:23:39.172  Timeout on attempt 1/3
    10:23:51.174  Timeout on attempt 2/3
    10:24:05.177  Timeout on attempt 3/3  -> "Failed ... after 3 attempts"

    10:23:45.042  Finished fetching IP Control state ... in 28 s
    10:23:57.793  Finished fetching IP Control video ... in 14.501 seconds

36.005 s of blocked script, and no write ever reached the TV: none of the
branches below the pre-write panel check logged anything, so `_set_artmode`
never got past its first statement. The coordinators' own refreshes show the
queue was 14-36 s deep while each attempt allowed 10 s.

Two changes, tested here:

  * priority_requests() marks a user-initiated operation, and calls inside it
    overtake the queued polls on the host gate — waiting only for the request
    already in flight, which CMD_TIMEOUT bounds.
  * the pre-write panel check gets its own short budget and times out into
    "panel unknown", which writes, instead of consuming the write's budget.
"""

import asyncio
from pathlib import Path
import sys
from types import ModuleType
import unittest

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

from custom_components.samsungtv_smart.api.ipcontrol import (  # noqa: E402
    _PRIORITY_REQUEST,
    _HostGate,
    priority_requests,
)

SWITCH = (
    Path(__file__).parents[1] / "custom_components" / "samsungtv_smart" / "switch.py"
).read_text()


async def _run(gate, label, order, *, priority=False, hold=0.0):
    async with gate.acquire(priority=priority):
        order.append(label)
        if hold:
            await asyncio.sleep(hold)


# --- the gate lets a user-initiated call overtake the queue ----------------


async def test_a_priority_call_goes_before_the_queued_polls():
    gate = _HostGate()
    order = []

    # One poll in flight, three more queued behind it.
    inflight = asyncio.create_task(_run(gate, "inflight", order, hold=0.05))
    await asyncio.sleep(0)  # let it take the gate
    queued = [asyncio.create_task(_run(gate, f"poll{i}", order)) for i in range(3)]
    await asyncio.sleep(0)

    write = asyncio.create_task(_run(gate, "write", order, priority=True))
    await asyncio.gather(inflight, write, *queued)

    # It cannot pre-empt the request already on the wire, but it must not wait
    # for the three behind it.
    assert order[0] == "inflight"
    assert order[1] == "write", order


async def test_the_polls_still_run_after_the_write():
    gate = _HostGate()
    order = []

    queued = [asyncio.create_task(_run(gate, f"poll{i}", order)) for i in range(3)]
    await asyncio.sleep(0)
    write = asyncio.create_task(_run(gate, "write", order, priority=True))
    await asyncio.gather(write, *queued)

    assert sorted(order) == ["poll0", "poll1", "poll2", "write"]


async def test_two_writes_both_go_before_the_polls():
    gate = _HostGate()
    order = []

    inflight = asyncio.create_task(_run(gate, "inflight", order, hold=0.05))
    await asyncio.sleep(0)  # let it take the gate, so the rest really queue
    queued = [asyncio.create_task(_run(gate, f"poll{i}", order)) for i in range(2)]
    await asyncio.sleep(0)
    writes = [
        asyncio.create_task(_run(gate, f"write{i}", order, priority=True))
        for i in range(2)
    ]
    await asyncio.gather(inflight, *writes, *queued)

    assert order == ["inflight", "write0", "write1", "poll0", "poll1"], order


async def test_calls_are_still_serialized_one_at_a_time():
    # The whole point of the gate: the TV resets overlapping connections.
    gate = _HostGate()
    concurrent = 0
    peak = 0

    async def call(priority):
        nonlocal concurrent, peak
        async with gate.acquire(priority=priority):
            concurrent += 1
            peak = max(peak, concurrent)
            await asyncio.sleep(0.01)
            concurrent -= 1

    await asyncio.gather(*(call(i % 2 == 0) for i in range(6)))

    assert peak == 1


async def test_background_order_is_unchanged_without_any_write():
    gate = _HostGate()
    order = []

    tasks = [asyncio.create_task(_run(gate, f"poll{i}", order)) for i in range(4)]
    await asyncio.gather(*tasks)

    assert order == ["poll0", "poll1", "poll2", "poll3"]


async def test_the_gate_is_reusable_after_a_write_finishes():
    # The idle event must be set again, or every later poll would hang.
    gate = _HostGate()
    order = []

    await _run(gate, "write", order, priority=True)
    await _run(gate, "poll", order)

    assert order == ["write", "poll"]


# --- priority_requests marks the operation, not the single call ------------


class PriorityContextTest(unittest.TestCase):
    def test_the_flag_is_off_by_default(self):
        self.assertFalse(_PRIORITY_REQUEST.get())

    def test_everything_inside_the_block_is_priority(self):
        with priority_requests():
            self.assertTrue(_PRIORITY_REQUEST.get())

    def test_the_flag_is_restored_afterwards(self):
        with priority_requests():
            pass
        self.assertFalse(_PRIORITY_REQUEST.get())

    def test_the_flag_is_restored_even_on_an_error(self):
        with self.assertRaises(RuntimeError):
            with priority_requests():
                raise RuntimeError("write failed")
        self.assertFalse(_PRIORITY_REQUEST.get())


# --- the write path claims that priority, and bounds its panel check -------


class WritePathTest(unittest.TestCase):
    """Pinned against the source: both call sites, and the capped check."""

    def test_both_art_mode_writes_are_marked_user_initiated(self):
        self.assertEqual(SWITCH.count("with priority_requests():"), 2)
        for call in (
            "with priority_requests():\n                        result = "
            "await self._set_artmode(\n                            True,",
            "with priority_requests():\n                        result = "
            "await self._set_artmode(False, since=since)",
        ):
            self.assertIn(call, SWITCH)

    def test_the_panel_check_cannot_eat_the_write_budget(self):
        block = SWITCH[SWITCH.index("    async def _set_artmode") :]
        block = block[: block.index("guard.check(turn_on)")]
        self.assertIn("asyncio.timeout(PANEL_CHECK_TIMEOUT)", block)
        self.assertIn("except asyncio.TimeoutError:", block)
        # Timing out must mean "unknown", which writes — not "already set".
        self.assertIn("panel = None", block)

    def test_the_panel_budget_is_shorter_than_the_attempt_budget(self):
        # The attempt allows 10 s; the guard against a redundant write must
        # leave room for the write itself.
        self.assertIn("PANEL_CHECK_TIMEOUT = 3.0", SWITCH)
        self.assertIn("asyncio.timeout(10)", SWITCH)
