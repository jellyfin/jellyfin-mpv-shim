import unittest

from jellyfin_mpv_shim.clients import (
    VOLATILE_CREDENTIAL_KEYS,
    clean_credentials_for_save,
)


class CleanCredentialsForSaveTest(unittest.TestCase):
    def test_strips_connected(self):
        creds = [{"uuid": "abc", "connected": True}]
        cleaned = clean_credentials_for_save(creds)
        self.assertNotIn("connected", cleaned[0])

    def test_preserves_real_credentials(self):
        creds = [
            {
                "uuid": "abc",
                "username": "user",
                "address": "http://example:8096",
                "Id": "server-id",
                "AccessToken": "token",
                "connected": True,
            }
        ]
        cleaned = clean_credentials_for_save(creds)
        self.assertEqual(cleaned[0]["uuid"], "abc")
        self.assertEqual(cleaned[0]["username"], "user")
        self.assertEqual(cleaned[0]["address"], "http://example:8096")
        self.assertEqual(cleaned[0]["Id"], "server-id")
        self.assertEqual(cleaned[0]["AccessToken"], "token")

    def test_does_not_mutate_input(self):
        creds = [{"uuid": "abc", "connected": True}]
        clean_credentials_for_save(creds)
        # Live dicts (read by other threads) must be left intact.
        self.assertIn("connected", creds[0])
        self.assertTrue(creds[0]["connected"])

    def test_tolerates_missing_volatile_keys(self):
        # A credential that never got a runtime flag written must pass through.
        creds = [{"uuid": "abc", "username": "user"}]
        cleaned = clean_credentials_for_save(creds)
        self.assertEqual(cleaned, [{"uuid": "abc", "username": "user"}])

    def test_all_volatile_keys_stripped(self):
        server = {"uuid": "abc"}
        for key in VOLATILE_CREDENTIAL_KEYS:
            server[key] = "runtime-value"
        cleaned = clean_credentials_for_save([server])
        for key in VOLATILE_CREDENTIAL_KEYS:
            self.assertNotIn(key, cleaned[0])
        self.assertEqual(cleaned[0]["uuid"], "abc")

    def test_empty_list(self):
        self.assertEqual(clean_credentials_for_save([]), [])


if __name__ == "__main__":
    unittest.main()
