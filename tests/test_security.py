from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SecurityTests(unittest.TestCase):
    def test_public_seed_has_no_secret_keys(self) -> None:
        payload = json.loads((ROOT / "public/data/products.json").read_text(encoding="utf-8"))
        serialized = json.dumps(payload).lower()
        for forbidden in ("access_token", "refresh_token", "client_secret", "code_verifier"):
            self.assertNotIn(forbidden, serialized)

    def test_dangerous_local_files_are_ignored(self) -> None:
        patterns = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        for expected in (".env", ".local/", "*token*.json", "credentials.json"):
            self.assertIn(expected, patterns)

    def test_workflow_uses_minimum_repository_permission(self) -> None:
        workflow = (ROOT / ".github/workflows/meli-sync.yml").read_text(encoding="utf-8")
        self.assertIn("permissions:\n  contents: write", workflow)
        self.assertNotIn("pull-requests: write", workflow)
        self.assertNotIn("actions: write", workflow)
        self.assertNotIn('echo "$MELI_CLIENT_SECRET"', workflow)
        self.assertNotIn('echo "$MELI_REFRESH_TOKEN"', workflow)


if __name__ == "__main__":
    unittest.main()
