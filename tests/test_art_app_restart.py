"""Restarting the art app, for a Frame whose Art Mode no longer toggles.

A Frame can reach a state where nothing switches Art Mode any more: not Home
Assistant, not SmartThings, and not the power button on the TV's own remote.
The integration's symptom is the write going out and the panel never
following -- "artModeOn was accepted but the panel did not switch to Ambient"
on every retry, seven times in forty minutes on the reporter's QE55LS03D.
A power cycle clears it, which says the fault is the TV's art app, not ours.

The art API has no reset verb: of its 33 requests the only reset is
reset_brightness. But Art Mode is an ordinary Tizen app -- org.tizen.art-app,
built-in, launch mode single -- and the remote WebSocket exposes
ms.application.stop and ms.application.start, so the app can be cycled
without cutting the power.

This is a hypothesis built from the decompiled notes in
notes/QN55LS03FAFXZA/, not a recovery anyone has yet observed: a built-in app
is exactly the kind Tizen may decline to stop. So what is pinned here is that
the right verbs go out on the right channel, that each step is reported
separately, and that a refusal is not dressed up as a success.
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

from custom_components.samsungtv_smart.api.samsungws import SamsungTVWS  # noqa: E402
from custom_components.samsungtv_smart.const import ART_APP_ID  # noqa: E402
from custom_components.samsungtv_smart.media_player import SamsungTVDevice  # noqa: E402

# --- the TV side: which verb goes out, and on which channel ----------------


def _ws(*, control=True, connected=True, send_result=True):
    ws = object.__new__(SamsungTVWS)
    ws._log = MagicMock()
    ws._ws_control = object() if control else None
    ws._is_control_connected = connected
    ws.sent = []

    def _send(command, **kwargs):
        ws.sent.append((command, kwargs))
        return send_result

    ws._ws_send = _send
    return ws


def test_stop_sends_the_documented_stop_verb():
    ws = _ws()

    assert ws.stop_app(ART_APP_ID) is True

    command, _ = ws.sent[0]
    assert command["method"] == "ms.application.stop"
    assert command["params"] == {"id": ART_APP_ID}


def test_start_sends_the_documented_start_verb():
    ws = _ws()

    assert ws.start_app(ART_APP_ID) is True

    command, _ = ws.sent[0]
    assert command["method"] == "ms.application.start"
    assert command["params"] == {"id": ART_APP_ID}


def test_both_verbs_go_out_on_the_control_channel():
    # ed.apps.launch (the remote channel) has no stop verb at all, so these
    # must not fall back to it the way run_app does.
    ws = _ws()
    ws.stop_app(ART_APP_ID)
    ws.start_app(ART_APP_ID)

    for _, kwargs in ws.sent:
        assert kwargs["use_control"] is True
        assert kwargs["ws_socket"] is ws._ws_control


def test_nothing_is_sent_when_the_control_channel_is_missing():
    ws = _ws(control=False)

    assert ws.stop_app(ART_APP_ID) is False
    assert ws.sent == []
    assert ws._log.warning.called


def test_nothing_is_sent_before_the_control_channel_has_connected():
    # The socket object exists from the moment the thread starts; only
    # ms.channel.connect makes it usable.
    ws = _ws(connected=False)

    assert ws.start_app(ART_APP_ID) is False
    assert ws.sent == []


def test_start_does_not_depend_on_the_installed_app_list():
    # run_app picks its channel from _installed_app / _app_type, and a
    # built-in app is normally missing from both -- which would route the
    # relaunch to ed.apps.launch instead of ms.application.start.
    ws = _ws()
    assert not hasattr(ws, "_installed_app")
    assert not hasattr(ws, "_app_type")

    assert ws.start_app(ART_APP_ID) is True
    assert ws.sent[0][0]["method"] == "ms.application.start"


# --- the service side: stop, drop the socket, start ------------------------


class _ArtApi:
    def __init__(self):
        self.closed = 0

    async def close(self):
        self.closed += 1


def _device(*, frame=True, stop_ok=True, start_ok=True):
    device = object.__new__(SamsungTVDevice)
    device._log = MagicMock()
    device._frame_tv_supported = frame
    device.calls = []
    art_api = _ArtApi()
    device._art_api = art_api

    def _stop(app_id):
        device.calls.append(("stop", app_id, art_api.closed))
        return stop_ok

    def _start(app_id):
        device.calls.append(("start", app_id, art_api.closed))
        return start_ok

    device._ws = SimpleNamespace(stop_app=_stop, start_app=_start)

    async def _executor(func, *args):
        return func(*args)

    device.hass = SimpleNamespace(async_add_executor_job=_executor)
    device._store_art_result = lambda result: device.calls.append(("stored", result))
    # supported() is a REST read of :8001/api/v2/, independent of the art app,
    # so a wedged app does not make the service refuse itself.
    if not frame:
        device._art_api.supported = lambda: _false()
    return device


async def _false():
    return False


def _restart(device, **kwargs):
    return asyncio.run(device.async_art_restart_app(**kwargs))


def test_a_healthy_restart_stops_then_starts_the_art_app():
    device = _device()

    result = _restart(device, delay=0)

    assert [c[:2] for c in device.calls if c[0] in ("stop", "start")] == [
        ("stop", ART_APP_ID),
        ("start", ART_APP_ID),
    ]
    assert result["stopped"] is True
    assert result["started"] is True
    assert "error" not in result


def test_the_art_socket_is_dropped_between_the_stop_and_the_start():
    # The art channel belongs to the app we just stopped. Left open, the next
    # request waits out a dead socket's timeouts instead of reaching the new
    # instance.
    device = _device()

    _restart(device, delay=0)

    stop = next(c for c in device.calls if c[0] == "stop")
    start = next(c for c in device.calls if c[0] == "start")
    assert stop[2] == 0, "closed before the app was even stopped"
    assert start[2] == 1, "the stale art socket was still open at relaunch"


def test_a_refused_stop_is_reported_and_the_app_is_not_relaunched():
    # Relaunching an app that was never stopped would do nothing and read as
    # a success.
    device = _device(stop_ok=False)

    result = _restart(device, delay=0)

    assert result["stopped"] is False
    assert "error" in result
    assert not any(c[0] == "start" for c in device.calls)
    assert device._art_api.closed == 0


def test_a_stop_that_worked_and_a_start_that_did_not_says_both():
    # The worst outcome to report as a plain failure: the art app is down and
    # the user needs to know it is, not just that "something went wrong".
    device = _device(start_ok=False)

    result = _restart(device, delay=0)

    assert result["stopped"] is True
    assert result["started"] is False
    assert "error" in result


def test_the_result_names_the_app_and_the_delay_it_used():
    device = _device()

    result = _restart(device, delay=0)

    assert result["service"] == "art_restart_app"
    assert result["app_id"] == ART_APP_ID
    assert result["delay"] == 0


def test_a_non_frame_tv_is_refused_without_touching_the_tv():
    device = _device(frame=False)

    result = _restart(device, delay=0)

    assert result["error"] == "Frame TV not supported"
    assert device.calls == [("stored", result)]


def test_a_close_that_fails_still_relaunches_the_app():
    # An art app left stopped is worse than a stale socket, so nothing in the
    # cleanup step may swallow the relaunch.
    device = _device()

    async def _boom():
        raise RuntimeError("socket already gone")

    device._art_api.close = _boom

    result = _restart(device, delay=0)

    assert result["started"] is True
    assert "art_socket_close_failed" in result


def test_the_outcome_is_published_to_the_frame_art_sensor():
    # Every art service stores its result; a service whose whole purpose is
    # to report what the TV accepted must not be the exception.
    device = _device()

    result = _restart(device, delay=0)

    assert ("stored", result) in device.calls


# --- the service is reachable from an automation ---------------------------


def test_the_service_is_registered_and_documented():
    from pathlib import Path

    root = Path(__file__).parents[1] / "custom_components" / "samsungtv_smart"
    media_player = (root / "media_player.py").read_text()
    services = (root / "services.yaml").read_text()

    assert "SERVICE_ART_RESTART_APP," in media_player
    assert '"async_art_restart_app",' in media_player
    assert "\nart_restart_app:\n" in services
    # The caveat belongs where the user reads before calling it.
    assert "EXPERIMENTAL" in services
