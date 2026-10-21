"""A channel that is the only art-mode source must not be backed off for minutes (#337).

A 2019 Frame (QE65LS03RAUXXH) that runs no IP Control server at all — ports
1516 and 1515 both refuse — with SmartThings not configured. The art
WebSocket channel is the only thing that can report Art Mode.

The wedge breaker trips on read timeouts, and the recovery cooldown doubles
on each recurrence inside ART_WS_WEDGE_RECURRENCE_WINDOW (900 s):

    20:14:04  wedged, forcing reconnect -> backing off for 60s
    20:15:35  wedged ...                -> backing off for 120s
    20:18:06  wedged ...                -> backing off for 240s
    20:22:23  wedged ...                -> backing off for 240s
    20:26:38  wedged ...                -> backing off for 240s

Each new wedge lands just after the previous cooldown expired (+91 s, +151 s,
+257 s), so the reconnects were succeeding and the TV was reachable
throughout. The channel is shut for the whole cooldown, every
art_mode_changed sent in that window is lost, and the reporter measured a
transition taking about five minutes to appear.

Spacing retries out assumes something else reports art mode meanwhile. Here
there is nothing, so the escalation is not applied — the breaker still trips
and still reconnects, keeping the recovery it exists for (#153), but the
blind window stays at the base cooldown.
"""

import sys
import time
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

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

from custom_components.samsungtv_smart.api.art import (  # noqa: E402
    ART_WS_RECOVERY_BACKOFF_MAX_DOUBLINGS,
    ART_WS_RECOVERY_COOLDOWN,
    ART_WS_TIMEOUT_TRIP,
    SamsungTVAsyncArt,
)


def _channel(*, sole_art_source, recurring_wedges=0, unproductive_cycles=0):
    """An art client with an open socket whose reads have just timed out."""
    art = object.__new__(SamsungTVAsyncArt)
    art._log = MagicMock()
    art._host = "192.168.0.85"
    art._port = 8001
    art._proven_port = 8001
    art._upload_in_progress = False
    art._connected = True
    art._ws = SimpleNamespace(closed=False, close=MagicMock())
    art._timeout_streak = ART_WS_TIMEOUT_TRIP - 1
    art._last_event_at = None  # silent: a Frame idling in art pushes nothing
    art._answered_request_since_connect = True
    art._got_response_since_connect = False
    art._unproductive_cycles = unproductive_cycles
    art._recurring_wedges = recurring_wedges
    # A recurrence is only counted when the previous wedge was inside
    # ART_WS_WEDGE_RECURRENCE_WINDOW; without this the trip resets the
    # counter and the preset above would be silently discarded.
    art._last_wedge_at = time.monotonic() - 60 if recurring_wedges else None
    art._request_cooldown_until = 0.0
    art.sole_art_source = sole_art_source
    art._pending_requests = {}
    art._force_close_task = None
    # The real one tears the socket down through the receive loop; here we
    # only care that the breaker asked for it.
    art._force_close_ws = AsyncMock()
    return art


def _cooldown(art):
    """The cooldown the breaker just set, in seconds from now."""
    return art._request_cooldown_until - time.monotonic()


def _trip(art):
    art._note_request_timeout()


# --- the sole source keeps the base cooldown ------------------------------


@pytest.mark.parametrize("recurrences", [1, 2, ART_WS_RECOVERY_BACKOFF_MAX_DOUBLINGS])
async def test_the_only_art_source_is_never_backed_off_past_the_base(recurrences):
    art = _channel(sole_art_source=True, recurring_wedges=recurrences)

    _trip(art)

    assert _cooldown(art) == pytest.approx(ART_WS_RECOVERY_COOLDOWN, abs=1.0)


async def test_the_reporters_third_wedge_drops_from_240s_to_the_base():
    # 20:18:06 in the capture: two recurrences already counted.
    art = _channel(sole_art_source=True, recurring_wedges=2)

    _trip(art)

    assert _cooldown(art) == pytest.approx(30.0, abs=1.0)


async def test_an_unproductive_cycle_is_capped_too():
    # The other escalation path: a connection that never answered.
    art = _channel(sole_art_source=True, unproductive_cycles=3)

    _trip(art)

    assert _cooldown(art) == pytest.approx(ART_WS_RECOVERY_COOLDOWN, abs=1.0)


async def test_the_breaker_still_trips_and_closes_the_socket():
    # Recovery must survive the change: this is still the #153 dead-app path.
    art = _channel(sole_art_source=True, recurring_wedges=2)

    _trip(art)

    assert art._force_close_task is not None
    art._force_close_task.cancel()
    assert art._timeout_streak == 0


async def test_the_cap_is_logged_so_a_capture_shows_it():
    art = _channel(sole_art_source=True, recurring_wedges=3)

    _trip(art)

    said = " ".join(str(c.args[0]) for c in art._log.debug.call_args_list)
    assert "only art-mode source" in said


# --- a TV with another source still gets the escalation -------------------


async def test_a_tv_with_another_source_still_escalates():
    # Two recurrences already counted, and the trip makes it three: the full
    # 8x escalation, exactly what the reporter saw before the change.
    art = _channel(sole_art_source=False, recurring_wedges=2)

    _trip(art)

    assert _cooldown(art) == pytest.approx(ART_WS_RECOVERY_COOLDOWN * 8, abs=1.0)


async def test_the_escalation_is_still_bounded_by_the_max_doublings():
    art = _channel(
        sole_art_source=False, recurring_wedges=ART_WS_RECOVERY_BACKOFF_MAX_DOUBLINGS
    )

    _trip(art)

    ceiling = ART_WS_RECOVERY_COOLDOWN * (2**ART_WS_RECOVERY_BACKOFF_MAX_DOUBLINGS)
    assert _cooldown(art) == pytest.approx(ceiling, abs=1.0)


async def test_a_first_wedge_on_a_sole_source_is_the_base_cooldown():
    art = _channel(sole_art_source=True)

    _trip(art)

    assert _cooldown(art) == pytest.approx(ART_WS_RECOVERY_COOLDOWN, abs=1.0)


async def test_a_first_wedge_elsewhere_still_doubles_once():
    # The connection answered nothing, so it counts as one unproductive cycle
    # even on a first wedge — the 60 s that opens the reporter's sequence.
    art = _channel(sole_art_source=False)

    _trip(art)

    assert _cooldown(art) == pytest.approx(ART_WS_RECOVERY_COOLDOWN * 2, abs=1.0)


# --- the message no longer asserts the TV is away -------------------------


async def test_the_warning_does_not_claim_the_tv_is_away():
    # The breaker returns early when the socket is closed, so at this point it
    # is open: "away" was a guess stated as fact.
    art = _channel(sole_art_source=False, recurring_wedges=1)

    _trip(art)

    said = " ".join(str(c.args[0]) for c in art._log.warning.call_args_list)
    assert "the TV or network is likely away" not in said
    assert "no request answered on port" in said
