"""A Frame that announces real standby as art must not latch Art Mode on (#326).

The 2019 Frame (0.97, local only — no IP Control, no SmartThings) sends the
same event pair when it enters Art Mode and when it goes to real standby:
``go_to_standby`` followed ~56 ms later by ``art_mode_changed status='on'``.
The pair carries no information, so it cannot be used to tell the two apart.
What differs is what the TV does next: in Art Mode the panel is powered and
answers device info, and in real standby it drops off the network.

Retest on 8.12.5 showed the consequence. Real standby at 22:50:14:

    22:50:14  go_to_standby, then art_mode_changed status='on'
    22:50:1x  REST device info starts timing out on every poll
    ...       media_player off, but the Art Mode switch stays ON
    22:54:02  REST answers PowerState='standby' -> switch finally goes off

Two separate defects kept it on for nearly four minutes:

  * ``_art_broadcast_overrides`` compared the broadcast against the age of the
    *reading* only. ``_async_load_device_info`` stamps ``_device_info_at`` on
    success alone, so the frozen PowerState='standby' from 22:44:54 never aged
    past the grace window and the 22:50:14 broadcast outranked it for good.
    The window is now bounded in wall-clock terms as well.

  * With the last answered read saying PowerState='on' instead, that branch
    does not fire at all and the art channel's cached 'on' decides — which is
    the very value the standby announcement wrote. A TV silent on device info
    for ART_UNREACHABLE_WINDOW while we do not believe it on is off the
    network, and a panel showing artwork is not, so the cache no longer gets
    to assert 'on' there.
"""

import sys
import time
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

from custom_components.samsungtv_smart.api.samsungws import ArtModeStatus  # noqa: E402
from custom_components.samsungtv_smart.const import (  # noqa: E402
    DATA_ART_API,
    DEFAULT_APP,
    DOMAIN,
)
from custom_components.samsungtv_smart.media_player import (  # noqa: E402
    ART_BROADCAST_GRACE,
    ART_UNREACHABLE_WINDOW,
    SamsungTVDevice,
)
from homeassistant.components.media_player import MediaPlayerState  # noqa: E402

ENTRY_ID = "entry-90"


def _device(
    *,
    power_state="standby",
    device_info_at=0.0,
    broadcast=None,
    art_mode=True,
    state=MediaPlayerState.OFF,
):
    """The reported TV: 2019 Frame QE65LS03RAUXZG, no IP Control, no SmartThings.

    ``device_info_at`` is seconds ago, so 0 is "answered just now"; None means
    device info has never been answered at all.
    """
    device = object.__new__(SamsungTVDevice)
    device._entry_id = ENTRY_ID
    device._host = "192.168.1.90"
    device._log = MagicMock()
    device._state = state
    device._running_app = DEFAULT_APP
    device._ip_art_mode = None  # not paired
    device._get_ip_control_state_coordinator = lambda: SimpleNamespace(data=None)
    device._device_info = (
        {"device": {"PowerState": power_state}} if power_state is not None else {}
    )
    device._device_info_at = (
        None if device_info_at is None else time.monotonic() - device_info_at
    )
    device._st = None  # local only
    device._st_state_at = None
    device._ws = SimpleNamespace(artmode_status=ArtModeStatus.Unsupported)
    art_api = SimpleNamespace(art_mode=art_mode, art_mode_last_broadcast=broadcast)
    device.hass = SimpleNamespace(data={DOMAIN: {ENTRY_ID: {DATA_ART_API: art_api}}})
    return device


# --- F1: the override window is bounded in wall-clock terms too -------------


def test_entering_art_mode_still_beats_a_trailing_standby_reading():
    # #326 case A, the 8.12.1 fix: broadcast 'on' at 10:42:37.634, device info
    # answers PowerState='standby' 0.285 s later. The broadcast must still win.
    now = time.monotonic()
    device = _device(device_info_at=0.0, broadcast=(True, now - 0.285))

    assert device._art_mode_is_on() is True


def test_a_frozen_reading_stops_losing_once_the_broadcast_is_old():
    # #326 case B: the standby announcement is 25 s old and device info has
    # been timing out since, so its stamp is stuck 5 minutes back. The reading
    # is still older than the broadcast, but neither is fresh any more — the
    # broadcast must stop overriding instead of winning forever.
    now = time.monotonic()
    device = _device(
        device_info_at=300.0, broadcast=(True, now - (ART_BROADCAST_GRACE + 5))
    )

    assert device._art_mode_is_on() is False


def test_a_frozen_reading_still_loses_inside_the_grace_window():
    # The bound shortens the latch, it does not remove the override: a
    # broadcast heard 5 s ago is genuinely the newer word.
    now = time.monotonic()
    device = _device(device_info_at=300.0, broadcast=(True, now - 5))

    assert device._art_mode_is_on() is True


def test_a_fresh_reading_after_the_grace_window_is_the_authority():
    # Unchanged behaviour: the TV really did power off and the art channel
    # never witnessed it, so a current 'standby' outranks the old broadcast.
    now = time.monotonic()
    device = _device(
        device_info_at=0.0, broadcast=(True, now - (ART_BROADCAST_GRACE + 5))
    )

    assert device._art_mode_is_on() is False


def test_a_recent_broadcast_saying_off_is_reported_as_off():
    # The override reports what the TV announced, not simply "on".
    now = time.monotonic()
    device = _device(device_info_at=300.0, broadcast=(False, now - 5), art_mode=False)

    assert device._art_mode_is_on() is False


# --- F2: a TV silent on device info is not showing art ----------------------


def test_a_stale_cached_on_does_not_survive_an_unreachable_tv():
    # #326 case C: the last answered read said PowerState='on', so the standby
    # branch never fires and the art channel's cached 'on' — written by the
    # real-standby announcement — used to decide on its own.
    device = _device(
        power_state="on",
        device_info_at=ART_UNREACHABLE_WINDOW + 30,
        broadcast=None,
        art_mode=True,
    )

    assert device._art_mode_is_on() is False


def test_the_cache_still_decides_while_the_tv_answers():
    device = _device(power_state="on", device_info_at=5.0, art_mode=True)

    assert device._art_mode_is_on() is True


def test_a_brief_silence_does_not_unsee_art_mode():
    # Just inside the window: a few missed polls must not flip Art Mode off.
    device = _device(
        power_state="on", device_info_at=ART_UNREACHABLE_WINDOW - 5, art_mode=True
    )

    assert device._art_mode_is_on() is True


def test_a_tv_we_believe_is_on_is_never_called_unreachable():
    # While the TV is ON the probe is answering by definition; a stale stamp
    # there would mean something else entirely.
    device = _device(
        power_state="on",
        device_info_at=ART_UNREACHABLE_WINDOW + 30,
        art_mode=True,
        state=MediaPlayerState.ON,
    )

    assert device._art_mode_is_on() is True


def test_a_tv_that_never_answered_device_info_has_no_baseline():
    device = _device(power_state="on", device_info_at=None, art_mode=True)

    assert device._art_mode_is_on() is True


def test_a_tv_whose_device_info_carries_no_power_state_is_not_probed():
    # _check_status only force-reads device info for a TV reporting
    # PowerState, so on any other set the stamp ages for an unrelated reason.
    device = _device(
        power_state=None, device_info_at=ART_UNREACHABLE_WINDOW + 30, art_mode=True
    )

    assert device._art_mode_is_on() is True
