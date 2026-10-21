"""A local-only 0.97 Frame must not lose Art Mode to PowerState or a blip (#326).

A 2019 Frame (QE65LS03R, art API 0.97) paired locally has neither IP Control
nor SmartThings, so the art WebSocket channel is the *only* source of truth —
and that channel only ever pushes `art_mode_changed`; its request/response
reads never answer. Three consequences were measured on 8.12.0:

1. Entering Art Mode flapped off: the TV broadcast art_mode_changed='on' at
   10:42:37.634 and a REST device-info poll read PowerState='standby' 285 ms
   later, which `_art_mode_is_on()` took as "art is off". The IP Control
   branches already let a fresh broadcast outrank a trailing snapshot; the
   PowerState branch did not, and a local-only set falls straight through to
   it. A 0.97 Frame reports 'standby' *while* displaying art.

2. The wedge breaker fired on read timeouts: last broadcast 10:49:30, third
   timeout 10:52:45 — 195 s, just past ART_WS_EVENT_LIVENESS_WINDOW (180 s).
   Reconnecting cannot make those reads answer, and the backoff drops the
   broadcasts the switch depends on. Such a channel now gets the much longer
   ART_WS_BROADCAST_ONLY_LIVENESS_WINDOW; a truly dead app still trips.

3. The reconnect blanked the state: the receive loop dropped the cached
   art_mode, and with no independent source the attribute vanished and the
   sensor went unknown — one second before the channel came back. The value is
   now retained across a drop for ART_WS_STATE_RETENTION, then expires so a
   real power-off is not pinned "on".
"""

import sys
import time
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

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
    ART_WS_BROADCAST_ONLY_LIVENESS_WINDOW,
    ART_WS_EVENT_LIVENESS_WINDOW,
    ART_WS_STATE_RETENTION,
    ART_WS_TIMEOUT_TRIP,
    SamsungTVAsyncArt,
)
from custom_components.samsungtv_smart.api.samsungws import ArtModeStatus  # noqa: E402
from custom_components.samsungtv_smart.const import (  # noqa: E402
    DATA_ART_API,
    DEFAULT_APP,
    DOMAIN,
)
from custom_components.samsungtv_smart.media_player import (  # noqa: E402
    ART_BROADCAST_GRACE,
    SamsungTVDevice,
)
from homeassistant.components.media_player import MediaPlayerState  # noqa: E402

ENTRY_ID = "entry-90"


# --- 1. PowerState='standby' must not outrank a fresh broadcast -------------


def _local_only_device(*, power_state, broadcast, art_mode=None):
    """A 2019 Frame with no IP Control and no SmartThings."""
    device = object.__new__(SamsungTVDevice)
    device._entry_id = ENTRY_ID
    device._state = MediaPlayerState.OFF  # a Frame showing art reports OFF
    device._running_app = DEFAULT_APP
    device._ip_art_mode = None  # not paired
    device._st = None  # local only
    device._ws = SimpleNamespace(artmode_status=ArtModeStatus.Unsupported)
    # No IP Control coordinator data -> _ip_control_panel_art_cached() is None.
    device._get_ip_control_state_coordinator = lambda: SimpleNamespace(data=None)
    device._device_info = {"device": {"PowerState": power_state}}
    device._device_info_at = time.monotonic()
    art_api = SimpleNamespace(art_mode_last_broadcast=broadcast, art_mode=art_mode)
    device.hass = SimpleNamespace(data={DOMAIN: {ENTRY_ID: {DATA_ART_API: art_api}}})
    return device


def test_standby_loses_to_a_fresh_art_broadcast():
    # The #326 flap: broadcast 'on', then PowerState='standby' 285 ms later.
    device = _local_only_device(
        power_state="standby", broadcast=(True, time.monotonic())
    )

    assert device._art_mode_is_on() is True


def test_standby_wins_once_the_broadcast_is_old():
    # Past the grace the reading is the authority again, so a genuine power-off
    # is not pinned "on" by a broadcast from before it.
    stale = time.monotonic() - (ART_BROADCAST_GRACE + 5)
    device = _local_only_device(power_state="standby", broadcast=(True, stale))

    assert device._art_mode_is_on() is False


def test_standby_without_any_broadcast_is_still_off():
    device = _local_only_device(power_state="standby", broadcast=None)

    assert device._art_mode_is_on() is False


def test_a_broadcast_saying_off_does_not_turn_art_on():
    # The override reports what the TV announced, not simply "on".
    device = _local_only_device(
        power_state="standby", broadcast=(False, time.monotonic())
    )

    assert device._art_mode_is_on() is False


# --- 2. The breaker must not tear down a broadcast-only channel -------------


def _art_client(*, answered_read, last_event_age):
    art = object.__new__(SamsungTVAsyncArt)
    art._log = MagicMock()
    art._upload_in_progress = False
    art._timeout_streak = ART_WS_TIMEOUT_TRIP - 1  # the next timeout trips
    art._connected = True
    art._ws = SimpleNamespace(closed=False)
    art._answered_request_since_connect = answered_read
    art._last_event_at = (
        None if last_event_age is None else time.monotonic() - last_event_age
    )
    # Only read past the guard, when the breaker actually fires.
    art._got_response_since_connect = answered_read
    art._unproductive_cycles = 0
    art._recurring_wedges = 0
    art.sole_art_source = False
    art._last_wedge_at = None
    art._request_cooldown_until = 0.0
    art._proven_port = 8001
    art._port = 8001
    art._force_close_task = None
    art._force_close_ws = AsyncMock()
    return art


def _wedged(art):
    """True when the breaker declared the channel wedged."""
    return any(
        "looks wedged" in str(call.args[0]) for call in art._log.warning.call_args_list
    )


async def test_broadcast_only_channel_is_not_reconnected_on_read_timeouts():
    # The measured gap: 195 s of silence — past the normal window, well inside
    # the broadcast-only one. Reads never answer on this model by design.
    age = ART_WS_EVENT_LIVENESS_WINDOW + 15
    assert age < ART_WS_BROADCAST_ONLY_LIVENESS_WINDOW
    art = _art_client(answered_read=False, last_event_age=age)

    art._note_request_timeout()

    assert not _wedged(art)
    assert art._timeout_streak == 0  # the streak is forgiven, not carried
    art._force_close_ws.assert_not_called()


async def test_a_channel_whose_reads_work_still_trips_at_the_normal_window():
    # Same silence, but this channel has answered reads — so timing out now is
    # a real fault and the existing behaviour must be unchanged.
    art = _art_client(
        answered_read=True, last_event_age=ART_WS_EVENT_LIVENESS_WINDOW + 15
    )

    art._note_request_timeout()

    assert _wedged(art)


async def test_a_silent_broadcast_only_channel_still_trips_eventually():
    # A genuinely dead app pushes nothing at all; it must still recover.
    art = _art_client(
        answered_read=False,
        last_event_age=ART_WS_BROADCAST_ONLY_LIVENESS_WINDOW + 10,
    )

    art._note_request_timeout()

    assert _wedged(art)


# --- 3. A channel blip must not blank the only known Art Mode state ---------


def _connected_art(art_mode):
    art = object.__new__(SamsungTVAsyncArt)
    art._art_mode = art_mode
    art._art_mode_retained_until = None
    art.art_mode_last_broadcast = (art_mode, time.monotonic())
    return art


def _drop_channel(art):
    """What the receive loop does when the socket goes away."""
    if art._art_mode is None:
        art.art_mode_last_broadcast = None
    else:
        art._art_mode_retained_until = time.monotonic() + ART_WS_STATE_RETENTION


def test_art_mode_survives_a_brief_channel_drop():
    art = _connected_art(True)

    _drop_channel(art)

    # The 1 s reconnect must not blank the switch and sensor.
    assert art.art_mode is True
    assert art.art_mode_last_broadcast is not None


def test_retained_art_mode_expires_so_a_power_off_is_not_pinned_on():
    art = _connected_art(True)
    _drop_channel(art)

    # Pretend the channel stayed down past the retention.
    art._art_mode_retained_until = time.monotonic() - 1

    assert art.art_mode is None
    # The broadcast goes with it: it described a channel long gone.
    assert art.art_mode_last_broadcast is None


def test_a_fresh_value_is_not_on_borrowed_time():
    art = _connected_art(True)
    _drop_channel(art)
    assert art._art_mode_retained_until is not None

    # A reconnect delivers a real event again.
    art.art_mode = False

    assert art._art_mode_retained_until is None
    assert art.art_mode is False


def test_a_drop_with_nothing_cached_clears_the_broadcast():
    art = _connected_art(None)

    _drop_channel(art)

    assert art.art_mode is None
    assert art.art_mode_last_broadcast is None
