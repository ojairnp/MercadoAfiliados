from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from scripts.sync_products import ProductConfig, load_config, synchronize, validate_sync_config


class FakeClient:
    def get_items_bulk(self, item_ids: list[str], *, access_token: str) -> dict[str, dict[str, Any]]:
        self.access_token = access_token
        return {
            item_id: {
                "id": item_id,
                "site_id": "MLM",
                "title": "  Báscula   inteligente económica  ",
                "status": "active",
                "available_quantity": 1,
                "permalink": f"https://articulo.mercadolibre.com.mx/{item_id}",
                "pictures": [{"secure_url": "https://http2.mlstatic.com/product.jpg"}],
                "seller_id": 123,
                "official_store_id": None,
                "condition": "new",
            }
            for item_id in item_ids
        }

    def get_sale_price(self, item_id: str, *, access_token: str) -> dict[str, Any]:
        return {"amount": 135, "currency_id": "MXN"}


class SyncProductsTests(unittest.TestCase):
    def test_empty_configuration_fails_before_oauth(self) -> None:
        with self.assertRaisesRegex(ValueError, "No hay productos habilitados"):
            validate_sync_config([])

    def test_rejects_supplements_under_current_affiliate_rules(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "products.json")
            path.write_text(
                json.dumps(
                    [
                        {
                            "id": "MLM123456789",
                            "affiliate_url": "https://mercado.li/example",
                            "category": "creatina",
                            "enabled": True,
                        }
                    ]
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "excluida"):
                load_config(path)

    def test_output_preserves_affiliate_link_and_contains_no_secrets(self) -> None:
        config = ProductConfig(
            id="MLM123456789",
            affiliate_url="https://mercado.li/affiliate-example",
            category="basculas",
            enabled=True,
        )
        payload = synchronize(
            [config],
            client=FakeClient(),  # type: ignore[arg-type]
            access_token="must-not-appear",
            existing={"schema_version": 1, "generated_at": None, "products": []},
            timestamp="2026-09-03T18:00:00Z",
        )
        product = payload["products"][0]
        self.assertEqual(product["affiliate_url"], config.affiliate_url)
        self.assertEqual(product["price"], 135)
        self.assertTrue(product["available"])
        serialized = json.dumps(payload)
        for forbidden in ("must-not-appear", "access_token", "refresh_token", "client_secret"):
            self.assertNotIn(forbidden, serialized.lower())

    def test_unchanged_data_keeps_timestamps_stable(self) -> None:
        config = ProductConfig(
            id="MLM123456789",
            affiliate_url="https://mercado.li/affiliate-example",
            category="basculas",
            enabled=True,
        )
        first = synchronize(
            [config],
            client=FakeClient(),  # type: ignore[arg-type]
            access_token="temporary",
            existing={"schema_version": 1, "generated_at": None, "products": []},
            timestamp="2026-09-03T18:00:00Z",
        )
        second = synchronize(
            [config],
            client=FakeClient(),  # type: ignore[arg-type]
            access_token="temporary",
            existing=first,
            timestamp="2026-09-04T06:00:00Z",
        )
        self.assertEqual(first, second)

    def test_affiliate_snapshot_does_not_require_oauth_client(self) -> None:
        config = ProductConfig(
            id="MLM3041581530",
            affiliate_url="https://meli.la/example",
            category="fitness",
            enabled=True,
            snapshot={
                "title": "Kit de bandas elásticas",
                "price": 257.79,
                "currency": "MXN",
                "image": "https://http2.mlstatic.com/product.webp",
                "permalink": "https://articulo.mercadolibre.com.mx/MLM-3041581530-bandas-_JM",
                "available": True,
                "status": "active",
                "condition": None,
                "seller_id": None,
                "official_store_id": None,
            },
        )
        payload = synchronize(
            [config],
            client=None,
            access_token=None,
            existing={"schema_version": 1, "generated_at": None, "products": []},
            timestamp="2026-09-04T16:00:00Z",
        )
        product = payload["products"][0]
        self.assertEqual(product["price"], 257.79)
        self.assertEqual(product["affiliate_url"], "https://meli.la/example")


if __name__ == "__main__":
    unittest.main()
