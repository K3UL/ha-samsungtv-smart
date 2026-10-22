"""Guard against re-issuing art-mode writes that did not take.

Every path that puts a Frame into (or out of) Art Mode used to trust a derived
``art_mode_status`` and then write — ``artModeOn`` over IP Control, or
``set_artmode`` / ``KEY_POWER`` over the WebSocket — with no check of what the
panel actually shows, no read-back afterwards, and no memory of having just
done the same thing. A wrong reading therefore turned every periodic
automation into a periodic write against the TV, indefinitely: one
``artModeOn`` per run at a panel already showing art, or a full
power-toggle-and-back when the fallback path ran instead.

This module is the memory part. The panel check (``getTVStates.pictureMode``,
which is ``Ambient`` exactly while art is displayed and is independent of the
``artModeControl`` flag that can wedge on some firmware) lives with the
callers; they consult this guard right before writing and report the outcome
right after.

Only writes that were NOT verified to take are remembered: a write that was
read back as applied clears the record, so a user toggling art on, off with
the remote, and on again within a minute is never blocked. What is blocked
is the second unverified write of the same intent inside the cooldown — the
exact shape of the loop above — and blocking it raises, so a retry loop that
would otherwise thrash exits with one warning instead of N writes.

Pure Python, no Home Assistant imports: shared between the media player and
the Art Mode switch through the entry's ``hass.data`` dict, and unit-tested
without either.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import time

# Seconds during which a second, unverified write of the same intent is
# refused. Long enough that an automation firing every 30-60 s cannot turn
# into a write loop; short enough that a genuine retry after a transient
# failure is not held for long.
ART_MODE_WRITE_COOLDOWN = 60.0

# Seconds to let a source switch settle before art mode is written.
#
# Selecting an input sends key presses over the WebSocket, and the TV then runs
# an HDMI-CEC (Anynet+) negotiation with whatever sits on that input. A device
# that announces itself as the active source during that window is deciding
# what the panel shows at the same moment we are, and the two disagree. On a
# QE55LS03D an automation that selected an AVR's input and started playback on
# it with no gap left the TV unable to enter Art Mode at all -- not from Home
# Assistant, not from its own remote -- until it was power-cycled; the refusals
# outlived the automation that caused them by forty minutes, which is a wedged
# TV rather than a busy one.
#
# This only covers the half we can see: a switch WE sent. CEC traffic the TV
# exchanges with its own devices is invisible from here, so a gap in the
# automation itself is still worth having.
SOURCE_SWITCH_SETTLE = 15.0

# Key under which the per-entry guard is stored in hass.data[DOMAIN][entry_id].
DATA_ART_MODE_GUARD = "art_mode_write_guard"


class ArtModeWriteSuppressed(Exception):
    """A same-intent art-mode write was refused inside the cooldown.

    Carries the seconds since the write it repeats, for the caller's warning.
    """

    def __init__(self, turn_on: bool, since: float) -> None:
        self.turn_on = turn_on
        self.since = since
        intent = "on" if turn_on else "off"
        super().__init__(
            f"art mode '{intent}' was already written {since:.0f}s ago without "
            f"being confirmed; not writing it again within "
            f"{ART_MODE_WRITE_COOLDOWN:.0f}s"
        )


class ArtModeWriteGuard:
    """Remember unverified art-mode writes, per intent, for a cooldown."""

    def __init__(
        self,
        cooldown: float = ART_MODE_WRITE_COOLDOWN,
        clock: Callable[[], float] = time.monotonic,
        settle: float = SOURCE_SWITCH_SETTLE,
    ) -> None:
        self._cooldown = cooldown
        self._clock = clock
        self._settle = settle
        # intent (True = art on, False = art off) -> monotonic time of the last
        # write of that intent that was not verified to have taken.
        self._unverified: dict[bool, float] = {}
        # When we last switched the TV's input, or None if we never have.
        self._source_switch_at: float | None = None

    def check(self, turn_on: bool) -> None:
        """Raise ArtModeWriteSuppressed if this intent was just written in vain."""
        last = self._unverified.get(turn_on)
        if last is None:
            return
        since = self._clock() - last
        if since < self._cooldown:
            raise ArtModeWriteSuppressed(turn_on, since)
        # Cooldown elapsed: the earlier attempt is history, allow one more.
        del self._unverified[turn_on]

    def record_unverified(self, turn_on: bool) -> None:
        """A write was sent and either failed to take or could not be read back."""
        self._unverified[turn_on] = self._clock()

    def record_verified(self, turn_on: bool) -> None:
        """A write was read back as applied: nothing to hold against it."""
        self._unverified.pop(turn_on, None)

    def record_confirmed(self, turn_on: bool) -> None:
        """The TV was read back in this state AFTER a write of it.

        Clears both records: the write took, and an earlier unverified write
        of the other intent is not being repeated — the TV has moved since.
        Without this, a write that could not be confirmed (e.g. a redundant OFF
        to a TV already off) blocked a genuine OFF for the rest of the cooldown
        even after a confirmed ON in between (#290).

        Only for a read-back after a write. A pre-write "the panel already
        shows it" check must use record_verified: clearing the other intent
        there would let a no-op ON wipe the record of a failing OFF.
        """
        self._unverified.pop(turn_on, None)
        self._unverified.pop(not turn_on, None)

    def note_source_switch(self) -> None:
        """An input switch was just sent to the TV."""
        self._source_switch_at = self._clock()

    def source_switch_settling(self) -> float:
        """Seconds to wait before writing art mode is safe (0.0 if none).

        Deliberately not part of ``check``: this is "not yet", not "no". The
        write is still wanted, so the caller waits it out rather than failing
        an automation that did nothing wrong.
        """
        if self._source_switch_at is None:
            return 0.0
        remaining = self._settle - (self._clock() - self._source_switch_at)
        return max(0.0, remaining)

    def pending(self, turn_on: bool) -> float | None:
        """Seconds since the last unverified write of this intent, if any."""
        last = self._unverified.get(turn_on)
        return None if last is None else self._clock() - last


def guard_for(entry_store: dict) -> ArtModeWriteGuard:
    """Return the entry's shared guard, creating it on first use.

    ``entry_store`` is ``hass.data[DOMAIN][entry_id]`` — the same dict the
    shared art API lives in — so the switch and the media player, which both
    write art mode, share one memory of what was just written.
    """
    guard = entry_store.get(DATA_ART_MODE_GUARD)
    if guard is None:
        guard = ArtModeWriteGuard()
        entry_store[DATA_ART_MODE_GUARD] = guard
    return guard


async def settle_after_source_switch(guard: ArtModeWriteGuard, log, what: str) -> float:
    """Hold an art-mode write until a source switch we sent has settled.

    Returns the seconds actually waited, so a caller can report it. Separate
    from the guard itself to keep that class free of asyncio and testable as
    plain data.
    """
    remaining = guard.source_switch_settling()
    if remaining <= 0:
        return 0.0
    log.debug(
        "%s: an input switch was sent %.0fs ago; waiting %.1fs for the TV's "
        "HDMI-CEC negotiation to finish before writing art mode",
        what,
        SOURCE_SWITCH_SETTLE - remaining,
        remaining,
    )
    await asyncio.sleep(remaining)
    return remaining
