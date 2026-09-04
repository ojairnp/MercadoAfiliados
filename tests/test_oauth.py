from __future__ import annotations

import base64
import hashlib
import unittest

from scripts.meli_oauth import _pkce_pair, _validate_redirect_uri


class OAuthTests(unittest.TestCase):
    def test_pkce_challenge_is_s256(self) -> None:
        verifier, challenge = _pkce_pair()
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        self.assertEqual(challenge, expected.decode("ascii").rstrip("="))
        self.assertGreaterEqual(len(verifier), 43)

    def test_redirect_uri_requires_https(self) -> None:
        with self.assertRaises(ValueError):
            _validate_redirect_uri("http://example.com/callback")


if __name__ == "__main__":
    unittest.main()
