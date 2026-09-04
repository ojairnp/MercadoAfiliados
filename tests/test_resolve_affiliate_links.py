from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.resolve_affiliate_links import (
    LinkResolutionError,
    extract_mlm_id,
    extract_product_id_from_html,
    resolve_affiliate_url,
    resolve_config,
    _write_validated_config,
)
from scripts.sync_products import load_config


class ResolveAffiliateLinksTests(unittest.TestCase):
    def test_extracts_common_and_encoded_mlm_formats(self) -> None:
        cases = {
            "https://articulo.mercadolibre.com.mx/MLM-123456789-producto-_JM": "MLM123456789",
            "https://www.mercadolibre.com.mx/p/MLM987654321": "MLM987654321",
            "https://meli.la/redirect?url=https%3A%2F%2Fexample%2Fp%2FMLM765432109": "MLM765432109",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(extract_mlm_id(url), expected)

    def test_existing_id_is_preserved_without_network_request(self) -> None:
        def unexpected_fetch(url: str, timeout: float):
            raise AssertionError("No debe consultar la red cuando ya existe un ID válido")

        entries = [
            {
                "id": "MLM123456789",
                "affiliate_url": "https://mercado.li/existing",
                "category": "fitness",
                "enabled": True,
            }
        ]
        self.assertEqual(resolve_config(entries, fetcher=unexpected_fetch), entries)

    def test_follows_only_explicit_official_redirects(self) -> None:
        responses = {
            "https://meli.la/abc": (
                302,
                {"Location": "https://click.mercadolibre.com.mx/track"},
                "https://meli.la/abc",
                "",
            ),
            "https://click.mercadolibre.com.mx/track": (
                302,
                {"Location": "/MLM-123456789-producto-_JM"},
                "https://click.mercadolibre.com.mx/track",
                "",
            ),
        }
        calls: list[str] = []

        def fetch(url: str, timeout: float):
            calls.append(url)
            return responses[url]

        self.assertEqual(resolve_affiliate_url("https://meli.la/abc", fetcher=fetch), "MLM123456789")
        self.assertEqual(
            calls,
            ["https://meli.la/abc", "https://click.mercadolibre.com.mx/track"],
        )

    def test_rejects_redirect_to_non_official_domain_before_requesting_it(self) -> None:
        calls: list[str] = []

        def fetch(url: str, timeout: float):
            calls.append(url)
            return 302, {"Location": "https://attacker.example/MLM123456789"}, url, ""

        with self.assertRaisesRegex(LinkResolutionError, "dominio oficial"):
            resolve_affiliate_url("https://meli.la/abc", fetcher=fetch)
        self.assertEqual(calls, ["https://meli.la/abc"])

    def test_rejects_lookalike_hosts_credentials_and_nonstandard_ports(self) -> None:
        unsafe_urls = (
            "https://meli.la.attacker.example/abc",
            "https://meli.la@attacker.example/abc",
            "https://meli.la:8443/abc",
            "http://meli.la/abc",
        )
        for url in unsafe_urls:
            with self.subTest(url=url), self.assertRaises(LinkResolutionError):
                resolve_affiliate_url(url, fetcher=lambda *_: self.fail("No debe consultar la red"))

    def test_detects_redirect_cycles(self) -> None:
        def fetch(url: str, timeout: float):
            destination = "https://mercado.li/b" if url.endswith("/a") else "https://meli.la/a"
            return 302, {"Location": destination}, url, ""

        with self.assertRaisesRegex(LinkResolutionError, "ciclo"):
            resolve_affiliate_url("https://meli.la/a", fetcher=fetch)

    def test_resolved_output_passes_the_sync_validator(self) -> None:
        entries = [
            {
                "affiliate_url": "https://meli.la/abc",
                "category": "fitness",
                "enabled": True,
            }
        ]

        def fetch(url: str, timeout: float):
            return 302, {"Location": "https://articulo.mercadolibre.com.mx/MLM-123456789-x"}, url, ""

        resolved = resolve_config(entries, fetcher=fetch)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, "resolved.json")
            _write_validated_config(output, resolved)
            loaded = load_config(output)
            self.assertEqual(loaded[0].id, "MLM123456789")
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))[0]["id"], "MLM123456789")

    def test_duplicate_resolved_ids_are_rejected(self) -> None:
        entries = [
            {"affiliate_url": "https://meli.la/a", "category": "fitness", "enabled": True},
            {"affiliate_url": "https://meli.la/b", "category": "fitness", "enabled": True},
        ]

        def fetch(url: str, timeout: float):
            return 302, {"Location": "https://www.mercadolibre.com.mx/p/MLM123456789"}, url, ""

        resolved = resolve_config(entries, fetcher=fetch)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "duplicado"):
                _write_validated_config(Path(directory, "resolved.json"), resolved)

    def test_extracts_featured_product_from_official_social_landing(self) -> None:
        html = """
        <html><body>
          <a href="/recomendacion/MLM999999999">Otro producto</a>
          <a href="https://articulo.mercadolibre.com.mx/MLM-3041581530-producto-_JM">
            Ir a producto
          </a>
        </body></html>
        """
        self.assertEqual(
            extract_product_id_from_html(
                html,
                base_url="https://www.mercadolibre.com.mx/social/creator",
            ),
            "MLM3041581530",
        )

    def test_resolves_product_from_social_landing_body(self) -> None:
        def fetch(url: str, timeout: float):
            if url == "https://meli.la/social":
                return 302, {"Location": "https://www.mercadolibre.com.mx/social/creator"}, url, ""
            return (
                200,
                {"Content-Type": "text/html; charset=utf-8"},
                url,
                '<a aria-label="Ir a producto" href="/MLM-3041581530-item-_JM"></a>',
            )

        self.assertEqual(
            resolve_affiliate_url("https://meli.la/social", fetcher=fetch),
            "MLM3041581530",
        )


if __name__ == "__main__":
    unittest.main()
