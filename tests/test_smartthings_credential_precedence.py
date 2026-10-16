"""With OAuth chosen, the OAuth token must win over a leftover PAT (#319).

A reporter had Power on method = SmartThings, an OAuth link the UI showed as
healthy, and Art Mode failing to wake the TV from fully off. Two debug logs
carried no SmartThings command at all. The credential selection explains it:
the API-key field (labelled "SmartThings Personal Access Token (PAT)") was
read first and the OAuth token used only when that field was empty, so a PAT
left over from an earlier setup — stale or revoked — took precedence over the
token the refresh callback actually keeps alive. SmartThings then had no
usable credential while everything looked fine.

The OAuth token now wins when OAuth is the chosen method, the PAT remains the
fallback when OAuth carries no access token, and holding both is reported
instead of silently resolved.

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


class OAuthWinsOverALeftoverPatTest(unittest.TestCase):
    def test_the_oauth_token_is_used_when_a_stale_pat_is_also_present(self):
        # The reported setup: OAuth re-authorised, an old PAT still in the box.
        key, log = _select(pat="stale-pat", oauth_token={"access_token": "fresh"})

        self.assertEqual(key, "fresh")

    def test_holding_both_is_reported_not_resolved_in_silence(self):
        _, log = _select(pat="stale-pat", oauth_token={"access_token": "fresh"})

        self.assertEqual(len(log.warnings), 1)
        warning = log.warnings[0]
        self.assertIn("OAuth token and a SmartThings PAT", warning)
        self.assertIn("using the OAuth token", warning)
        self.assertIn("Clear the PAT", warning)

    def test_an_oauth_token_alone_is_used_without_any_warning(self):
        key, log = _select(pat=None, oauth_token={"access_token": "fresh"})

        self.assertEqual(key, "fresh")
        self.assertEqual(log.warnings, [])

    def test_a_pat_matching_the_oauth_token_is_not_worth_a_warning(self):
        # An entry whose PAT field was filled from the same OAuth token is not
        # an ambiguity to complain about.
        key, log = _select(pat="fresh", oauth_token={"access_token": "fresh"})

        self.assertEqual(key, "fresh")
        self.assertEqual(log.warnings, [])


class ThePatRemainsTheFallbackTest(unittest.TestCase):
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
            any("falling back to the configured PAT" in line for line in log.debugs),
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
