"""A partly-answering art channel must not loop at the base cooldown (#273).

Reported on a 43" LS03A: it woke into Art Mode, moved to an HDMI input ~40 s
later, and from ~2 min after that the art channel wedged 11 times in 64 min
(and 10 in 54 min the next morning), 4-8 min apart, with no backing-off line,
stopping on its own with no change on the TV.

Cause: the recovery backoff doubles only while the channel has never answered
since connecting. An art app polled on a TV showing an input answers some
requests and times out on the rest, so `_got_response_since_connect` is True,
`_unproductive_cycles` is reset on every wedge, the cooldown stays at its 30 s
base, and neither disposition branch logs anything.

Recurring wedges now escalate on their own, whatever the channel answered, and
say so. The two existing escalations are unchanged.

Also covered: a capability must not be latched to "unsupported" by a probe on a
channel that is merely busy (one 1 s timeout 12 s after a wedge-forced
reconnect latched both art get-capabilities false on a 65" LS03A, whose
same-model sibling reads true).

art.py needs aiohttp to import, so it is loaded with a stub.
"""

import importlib.util
from pathlib import Path
import sys
import types
import unittest

ROOT = Path(__file__).parents[1] / "custom_components" / "samsungtv_smart"


def _load_art():
    pkg_name = "_wedge_recurrence_api"
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(ROOT / "api")]
    sys.modules[pkg_name] = pkg
    stubbed = False
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        sys.modules["aiohttp"] = types.ModuleType("aiohttp")
        stubbed = True
    try:
        for name, path in (
            (f"{pkg_name}._image_prep", ROOT / "api" / "_image_prep.py"),
            (f"{pkg_name}.art", ROOT / "api" / "art.py"),
        ):
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
        return sys.modules[f"{pkg_name}.art"]
    finally:
        if stubbed:
            del sys.modules["aiohttp"]


art = _load_art()


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class _Channel(art.SamsungTVAsyncArt):
    """The real wedge handler; everything it touches is stubbed."""

    def __init__(self, clock):
        self._clock = clock
        self._log = _Log()
        self._upload_in_progress = False
        self._timeout_streak = 0
        self._connected = True
        self._ws = types.SimpleNamespace(closed=False)
        self._force_close_task = types.SimpleNamespace(done=lambda: False)
        self._request_cooldown_until = 0.0
        self._unproductive_cycles = 0
        self._recurring_wedges = 0
        self._last_wedge_at = None
        self._got_response_since_connect = True
        # No unsolicited event has been seen, so the event-liveness guard
        # (#315) never holds the breaker off and these cases exercise the
        # escalation itself. A channel that HAS answered a read is also not the
        # broadcast-only 0.97 Frame that gets the longer window (#326).
        self._last_event_at = None
        self._answered_request_since_connect = True
        self._proven_port = 8002
        self._port = 8002

    def wedge(self):
        """Three consecutive request timeouts, i.e. one wedge."""
        for _ in range(art.ART_WS_TIMEOUT_TRIP):
            self._note_request_timeout()

    @property
    def cooldown(self):
        return self._request_cooldown_until - self._clock()


class _Log:
    def __init__(self):
        self.lines = []

    def _log(self, level, msg, *args):
        self.lines.append((level, msg % args if args else msg))

    def debug(self, msg, *args):
        self._log("DEBUG", msg, *args)

    def info(self, msg, *args):
        self._log("INFO", msg, *args)

    def warning(self, msg, *args):
        self._log("WARNING", msg, *args)


class _Base(unittest.TestCase):
    def setUp(self):
        self.clock = _Clock()
        self.real_monotonic = art.time.monotonic
        art.time.monotonic = self.clock
        self.addCleanup(setattr, art.time, "monotonic", self.real_monotonic)
        self.ch = _Channel(self.clock)


class RecurringWedgesEscalateTest(_Base):
    def test_a_partly_answering_channel_stops_looping_at_the_base_cooldown(self):
        # The reported loop: the channel answers between wedges, so the old
        # rule reset the escalation every time and the cooldown never grew.
        cooldowns = []
        for _ in range(5):
            self.ch._got_response_since_connect = True  # it answered in between
            self.ch.wedge()
            cooldowns.append(round(self.ch.cooldown))
            self.clock.t += 300  # a wedge every 5 min, as measured

        base = art.ART_WS_RECOVERY_COOLDOWN
        cap = base * 2**art.ART_WS_RECOVERY_BACKOFF_MAX_DOUBLINGS
        self.assertEqual(cooldowns[0], base)
        self.assertEqual(cooldowns, [base, base * 2, base * 4, cap, cap])

    def test_it_says_so_instead_of_logging_nothing(self):
        self.ch.wedge()
        self.clock.t += 300
        self.ch.wedge()
        warnings = [m for level, m in self.ch._log.lines if level == "WARNING"]
        self.assertTrue(
            any("the channel answers but art requests" in m for m in warnings)
        )
        self.assertTrue(any("non-art input" in m for m in warnings))

    def test_a_quiet_window_clears_the_escalation(self):
        self.ch.wedge()
        self.clock.t += 300
        self.ch.wedge()
        self.assertEqual(round(self.ch.cooldown), art.ART_WS_RECOVERY_COOLDOWN * 2)
        self.clock.t += art.ART_WS_WEDGE_RECURRENCE_WINDOW + 1
        self.ch.wedge()
        self.assertEqual(round(self.ch.cooldown), art.ART_WS_RECOVERY_COOLDOWN)

    def test_an_isolated_wedge_still_gets_the_base_cooldown(self):
        self.ch.wedge()
        self.assertEqual(round(self.ch.cooldown), art.ART_WS_RECOVERY_COOLDOWN)

    def test_the_never_answered_escalation_is_unchanged(self):
        # #273's original case: a port that never answers keeps doubling, and
        # keeps its own message.
        self.ch._got_response_since_connect = False
        self.ch._proven_port = self.ch._port  # proven, so it is kept not swapped
        cooldowns = []
        for _ in range(3):
            self.ch.wedge()
            cooldowns.append(round(self.ch.cooldown))
            self.clock.t += 5000  # far apart: recurrence must not be what drives it
        # Unchanged from before this fix: the counter is incremented before
        # the cooldown is computed, so the first one already doubles.
        base = art.ART_WS_RECOVERY_COOLDOWN
        self.assertEqual(cooldowns, [base * 2, base * 4, base * 8])
        self.assertTrue(any("keeping the port" in m for _, m in self.ch._log.lines))

    def test_a_never_answered_unproven_port_still_flips(self):
        self.ch._got_response_since_connect = False
        self.ch._proven_port = None
        self.ch.wedge()
        self.assertEqual(self.ch._port, 8001)


class CapabilityProbeTest(_Base):
    def test_a_busy_channel_is_not_evidence_of_unsupported(self):
        # 12 s after a wedge-forced reconnect: still inside the cooldown.
        self.ch.wedge()
        self.clock.t += 12
        self.assertFalse(self.ch._capability_probe_is_trustworthy())

    def test_a_channel_that_never_answered_is_not_evidence_either(self):
        self.ch._got_response_since_connect = False
        self.assertFalse(self.ch._capability_probe_is_trustworthy())

    def test_a_settled_channel_that_answers_is_trusted(self):
        self.assertTrue(self.ch._capability_probe_is_trustworthy())

    def test_both_probes_are_gated(self):
        source = (ROOT / "api" / "art.py").read_text()
        self.assertEqual(source.count("_capability_probe_is_trustworthy()"), 2)
        for request in ("get_brightness", "get_color_temperature"):
            start = source.index(f'"request": "{request}"')
            block = source[
                start : source.index("return await self.get_artmode_settings", start)
            ]
            self.assertIn("_capability_probe_is_trustworthy()", block)


if __name__ == "__main__":
    unittest.main()
