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

    def test_workflow_resolves_ids_before_syncing(self) -> None:
        workflow = (ROOT / ".github/workflows/meli-sync.yml").read_text(encoding="utf-8")
        resolver_position = workflow.index("python scripts/resolve_affiliate_links.py")
        sync_position = workflow.index("- name: Sincronizar productos")
        self.assertLess(resolver_position, sync_position)
        self.assertIn('--config "${RUNNER_TEMP}/products-resolved.json"', workflow)

    def test_workflow_reads_existing_github_secrets(self) -> None:
        workflow = (ROOT / ".github/workflows/meli-sync.yml").read_text(encoding="utf-8")
        self.assertIn("MELI_CLIENT_ID: ${{ secrets.MELI_CLIENT_ID }}", workflow)
        self.assertIn("MELI_REDIRECT_URI: ${{ secrets.MELI_REDIRECT_URI }}", workflow)
        self.assertNotIn("MELI_CLIENT_ID: ${{ vars.MELI_CLIENT_ID }}", workflow)
        self.assertNotIn("MELI_REDIRECT_URI: ${{ vars.MELI_REDIRECT_URI }}", workflow)

    def test_source_config_contains_no_credentials(self) -> None:
        config = (ROOT / "config/products.json").read_text(encoding="utf-8").lower()
        for forbidden in ("access_token", "refresh_token", "client_secret", "authorization"):
            self.assertNotIn(forbidden, config)


if __name__ == "__main__":
    unittest.main()
