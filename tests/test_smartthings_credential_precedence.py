"""An OAuth entry must use the live token, not its frozen snapshot (#319).

A reporter had Power on method = SmartThings, an OAuth link the UI showed as
healthy, and Art Mode failing to wake the TV from fully off — but only after
the first day. Two debug logs carried no SmartThings command at all.

The cause is that CONF_API_KEY is not a user-supplied PAT on an OAuth entry:
the OAuth flow writes token["access_token"] into it, so it is a SNAPSHOT from
when the entry was created or reconfigured. CONF_OAUTH_TOKEN is the live
credential, kept refreshed by api_key_callback; Samsung's access tokens expire
in about a day. Reading the snapshot first meant the frozen value was used
forever after the first rotation, every cloud call failed on it, and a cold
power-on set to SmartThings found no usable client and did nothing.

The live OAuth token now wins. The stored key stays the fallback for an entry
whose OAuth token carries no access_token, and a superseded snapshot is noted
at debug — not warned about, since the flow itself writes it and every OAuth
entry has one.

media_player.py needs Home Assistant to import, so the selection block is
extracted from the real source and executed here: the test fails if that code
changes shape.
"""

from pathlib import Path
import unittest

MEDIA_PLAYER = (
    Path(__file__).parents[1]
    / "custom_components"
    / "samsungtv_smart"
    / "media_player.py"
).read_text()

OAUTH = "oauth"
PAT_FIELD = "api_key"
TOKEN_FIELD = "oauth_token"


class _Log:
    def __init__(self):
        self.warnings = []
        self.debugs = []

    def warning(self, msg, *args):
        self.warnings.append(msg % args if args else msg)

    def debug(self, msg, *args):
        self.debugs.append(msg % args if args else msg)


def _select(*, pat, oauth_token, auth_method=OAUTH):
    """Run the module's own credential selection and report what it chose."""
    start = MEDIA_PLAYER.index("        self._st_api_key = config.get(CONF_API_KEY)")
    block = MEDIA_PLAYER[start:]
    block = block[: block.index("        if self._st_api_key and device_id:")]
    body = "\n".join(line[8:] for line in block.split("\n"))
    body = (
        body.replace("config.get(CONF_API_KEY)", f"config.get({PAT_FIELD!r})")
        .replace("config.get(CONF_DEVICE_ID)", "config.get('device_id')")
        .replace("config.get(CONF_OAUTH_TOKEN)", f"config.get({TOKEN_FIELD!r})")
        .replace("auth_method == AUTH_METHOD_OAUTH", f"auth_method == {OAUTH!r}")
        .replace("self._st_api_key", "store['key']")
        .replace("self._log", "log")
    )
    log = _Log()
    namespace = {
        "config": {PAT_FIELD: pat, TOKEN_FIELD: oauth_token, "device_id": "dev-1"},
        "auth_method": auth_method,
        "store": {},
        "log": log,
    }
    exec(compile(body, "st_credential_selection", "exec"), namespace)
    return namespace["store"].get("key"), log


class TheLiveTokenWinsOverTheSnapshotTest(unittest.TestCase):
    def test_the_live_token_is_used_when_the_stored_snapshot_is_stale(self):
        # The reported setup: the snapshot froze a day ago, the token rotated.
        key, log = _select(pat="old-snapshot", oauth_token={"access_token": "fresh"})

        self.assertEqual(key, "fresh")

    def test_a_superseded_snapshot_is_noted_at_debug_not_warned_about(self):
        # Every OAuth entry carries a stored key, because the flow writes the
        # access token into it. A refresh making it differ is routine, so it
        # must not raise a warning telling the user to clear a field the
        # integration fills itself.
        _, log = _select(pat="old-snapshot", oauth_token={"access_token": "fresh"})

        self.assertEqual(log.warnings, [])
        self.assertTrue(
            any("older OAuth snapshot" in line for line in log.debugs), log.debugs
        )

    def test_an_oauth_token_alone_is_used_without_any_warning(self):
        key, log = _select(pat=None, oauth_token={"access_token": "fresh"})

        self.assertEqual(key, "fresh")
        self.assertEqual(log.warnings, [])

    def test_a_snapshot_still_equal_to_the_live_token_is_silent(self):
        # Just after authorising, the stored key and the live token match.
        key, log = _select(pat="fresh", oauth_token={"access_token": "fresh"})

        self.assertEqual(key, "fresh")
        self.assertEqual(log.warnings, [])
        self.assertFalse(any("older OAuth snapshot" in line for line in log.debugs))


class TheStoredKeyRemainsTheFallbackTest(unittest.TestCase):
    def test_a_pat_is_used_when_oauth_carries_no_access_token(self):
        key, _ = _select(pat="only-pat", oauth_token={"refresh_token": "r"})

        self.assertEqual(key, "only-pat")

    def test_a_pat_is_used_when_there_is_no_oauth_token_at_all(self):
        for token in (None, {}, "not-a-dict"):
            with self.subTest(token=token):
                key, _ = _select(pat="only-pat", oauth_token=token)
                self.assertEqual(key, "only-pat")

    def test_falling_back_to_the_pat_says_so(self):
        _, log = _select(pat="only-pat", oauth_token=None)

        self.assertTrue(
            any(
                "falling back to the stored SmartThings key" in line
                for line in log.debugs
            ),
            log.debugs,
        )

    def test_neither_credential_leaves_nothing_selected(self):
        key, _ = _select(pat=None, oauth_token=None)

        self.assertFalse(key)


class ANonOAuthEntryIsUntouchedTest(unittest.TestCase):
    def test_a_pat_entry_still_uses_its_pat(self):
        # auth_method is not OAuth: the OAuth branch must not run at all.
        key, log = _select(
            pat="the-pat",
            oauth_token={"access_token": "ignored"},
            auth_method="token",
        )

        self.assertEqual(key, "the-pat")
        self.assertEqual(log.warnings, [])


if __name__ == "__main__":
    unittest.main()
