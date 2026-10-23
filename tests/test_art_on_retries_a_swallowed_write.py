"""A just-woken Frame swallows the first art-mode write; one attempt is not enough.

Measured on a QE55LS03D, both halves on the same morning.

At 07:00 an automation asked for Art Mode on a TV that was off. The
media_player path powered it on, read the art channel successfully, wrote
once, and gave up:

    05:00:00.267  Frame Art: TV is off, turning it on first...
    05:00:11.688  Frame Art: TV answers after the power-on
    05:00:11.688  Sent 'get_artmode_status'
    05:00:11.786  answered in 98 ms -> "Art Mode is OFF, activating it..."
    05:00:11.787  Sent 'set_artmode_status'
    05:00:16.788  Timeout waiting for the reply (5.001 s)
    05:00:16.788  ERROR Frame Art: Failed to activate Art Mode

No art_mode_changed broadcast followed, so that write really was dropped.
Six minutes later the Art Mode switch -- which retries -- was asked the same
thing and got there:

    05:07:01.214  set_artmode_status   (attempt 1)
    05:07:06.215  timed out            <- swallowed, same as at 07:00
    05:07:09.434  set_artmode_status   (attempt 2)
    05:07:14.435  timed out
    05:07:15.607  {'event': 'wakeup'}
    05:07:15.835  {'event': 'art_mode_changed', 'status': 'on'}   <- +6.40 s
    05:07:17.489  reply to attempt 2                              <- +8.05 s
    05:07:25.866  Art Mode turned ON

Two separate facts, and the single attempt lost to both: the first write is
dropped, and the second is answered past the 5 s request budget, by a
broadcast before the reply. set_artmode already races reply against
broadcast, but its race ends with that budget.
"""

import asyncio
import sys
from types import ModuleType, SimpleNamespace
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

from custom_components.samsungtv_smart import media_player as mp  # noqa: E402


class _Clock:
    """Fake monotonic clock; a patched asyncio.sleep is what advances it."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


class _ArtApi:
    """The art channel. ``writes`` says what each successive write does.

    "ok"        -> set_artmode returns True
    "dropped"   -> returns False and nothing ever follows
    "late:<s>"  -> returns False, then the art_mode_changed broadcast lands
                   <s> seconds later, as the real TV did at +6.40 s
    "timeout"   -> set_artmode itself never returns in time
    """

    def __init__(self, writes, clock):
        self._writes = list(writes)
        self._clock = clock
        self.calls = 0
        self.art_mode_broadcast_count = 0
        self._broadcast_at = None

    async def set_artmode(self, turn_on):
        assert turn_on is True
        script = (
            self._writes[self.calls] if self.calls < len(self._writes) else "dropped"
        )
        self.calls += 1
        if script == "ok":
            self.art_mode_broadcast_count += 1
            return True
        if script == "timeout":
            await asyncio.sleep(30)
            return False
        if script.startswith("late:"):
            self._broadcast_at = self._clock.monotonic() + float(script.split(":")[1])
        return False

    def art_mode_broadcast_since(self, mark, turn_on):
        if self._broadcast_at is None or self._clock.monotonic() < self._broadcast_at:
            return False
        return turn_on is True and mark <= self.art_mode_broadcast_count


def _device(writes, clock):
    device = object.__new__(mp.SamsungTVDevice)
    device._log = MagicMock()
    device._art_api = _ArtApi(writes, clock)
    return device


def _run(device, clock, *, after_power_on):
    """Drive the helper with sleeps that advance the fake clock instantly."""
    real_sleep = asyncio.sleep
    slept = []

    async def _fake_sleep(seconds, *args, **kwargs):
        slept.append(seconds)
        clock.now += seconds
        await real_sleep(0)

    real_module_time = mp.time
    asyncio.sleep = _fake_sleep
    mp.time = SimpleNamespace(monotonic=clock.monotonic)
    try:
        result = asyncio.run(device._write_art_mode_on(after_power_on))
    finally:
        asyncio.sleep = real_sleep
        mp.time = real_module_time
    return result, slept


# --- the 07:00 failure, and what should happen instead ---------------------


def test_a_dropped_first_write_is_retried_after_a_power_on():
    # The regression this exists for: at 07:00 the code wrote once and
    # reported a failure the TV would have honoured on the next try.
    clock = _Clock()
    device = _device(["dropped", "ok"], clock)

    result, _ = _run(device, clock, after_power_on=True)

    assert result is True
    assert device._art_api.calls == 2


def test_a_late_broadcast_counts_as_the_write_having_taken():
    # The 05:07 case: the write returns false at 5 s, the TV broadcasts at
    # +6.40 s. That is an applied write, not a failed one.
    clock = _Clock()
    device = _device(["late:6.4"], clock)

    result, _ = _run(device, clock, after_power_on=False)

    assert result is True
    # What matters is that the broadcast is what settled it, not the reply.
    assert any(
        "broadcast Art Mode ON" in str(c) for c in device._log.info.call_args_list
    )


def test_the_whole_07_07_sequence_now_succeeds_on_the_media_player_path():
    # First write swallowed, second answered only by a late broadcast --
    # exactly what the switch lived through and this path did not survive.
    clock = _Clock()
    device = _device(["dropped", "late:6.4"], clock)

    result, _ = _run(device, clock, after_power_on=True)

    assert result is True
    assert device._art_api.calls >= 2, "one write is what failed at 07:00"


def test_a_first_write_that_simply_works_costs_nothing_extra():
    clock = _Clock()
    device = _device(["ok"], clock)

    result, slept = _run(device, clock, after_power_on=True)

    assert result is True
    assert device._art_api.calls == 1
    assert slept == [], "a TV that answers must not be made to wait"


# --- the bounds ------------------------------------------------------------


def test_a_cold_start_gets_more_attempts_than_a_warm_one():
    cold_clock, warm_clock = _Clock(), _Clock()
    cold = _device(["dropped"] * 9, cold_clock)
    warm = _device(["dropped"] * 9, warm_clock)

    assert _run(cold, cold_clock, after_power_on=True)[0] is False
    assert _run(warm, warm_clock, after_power_on=False)[0] is False

    assert cold._art_api.calls == mp.ART_ON_ATTEMPTS_AFTER_POWER_ON
    assert warm._art_api.calls == mp.ART_ON_ATTEMPTS
    assert cold._art_api.calls > warm._art_api.calls


def test_a_tv_that_never_answers_still_reports_failure():
    clock = _Clock()
    device = _device(["dropped"] * 9, clock)

    result, _ = _run(device, clock, after_power_on=True)

    assert result is False


def test_each_attempt_listens_for_the_broadcast_before_giving_up():
    clock = _Clock()
    device = _device(["dropped"] * 9, clock)

    _run(device, clock, after_power_on=False)

    waited = clock.now - 1000.0
    # Between attempts the retry delay is the listening window; the last one
    # waits the full grace.
    expected = (
        mp.ART_ON_RETRY_DELAY * (mp.ART_ON_ATTEMPTS - 1) + mp.ART_ON_BROADCAST_GRACE
    )
    assert waited >= expected


def test_a_write_that_hangs_does_not_abort_the_remaining_attempts():
    # asyncio.timeout(10) raising must cost that attempt, not the operation.
    clock = _Clock()
    device = _device(["timeout", "ok"], clock)

    result, _ = _run(device, clock, after_power_on=True)

    assert result is True
    assert device._art_api.calls == 2


def test_the_broadcast_mark_is_taken_once_before_the_first_write():
    # A broadcast answering write 1 that lands while write 2 is in flight is
    # still a confirmation. Re-reading the mark each attempt would lose it.
    clock = _Clock()
    device = _device(["late:9.0", "dropped"], clock)

    result, _ = _run(device, clock, after_power_on=True)

    assert result is True


def test_the_helper_is_what_the_cold_start_path_calls():
    source = __import__("pathlib").Path(mp.__file__).read_text()
    start = source.index("    async def _ensure_art_mode_ready")
    block = source[start : source.index("\n    async def _write_art_mode_on", start)]
    assert "await self._write_art_mode_on(woken)" in block
    assert (
        "await self._art_api.set_artmode(True)" not in block
    ), "the single unretried write is what this fix removes"


def test_the_grace_clears_the_latencies_that_were_measured():
    # 6.40 s to the broadcast, 8.05 s to the reply. A grace under either is
    # a grace that misses the thing it is there to catch.
    assert mp.ART_ON_BROADCAST_GRACE > 8.05
