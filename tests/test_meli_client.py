from __future__ import annotations

import unittest
from io import BytesIO
from unittest.mock import Mock
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

from scripts.meli_client import MeliApiError, MeliClient


class MeliClientTests(unittest.TestCase):
    def test_token_response_requires_rotated_refresh_token(self) -> None:
        with self.assertRaisesRegex(MeliApiError, "refresh_token rotado"):
            MeliClient._parse_token_response(
                {"access_token": "access", "expires_in": 10_800, "token_type": "bearer"}
            )

    def test_bulk_uses_current_endpoint_and_validates_shape(self) -> None:
        client = MeliClient(min_request_interval=0)
        client._request_json = Mock(  # type: ignore[method-assign]
            return_value=[
                {
                    "id": "MLM123456789",
                    "status_code": 200,
                    "body": {"id": "MLM123456789", "title": "Báscula"},
                }
            ]
        )
        result = client.get_items_bulk(["MLM123456789"], access_token="temporary")
        self.assertEqual(result["MLM123456789"]["title"], "Báscula")
        called_url = client._request_json.call_args.args[1]  # type: ignore[union-attr]
        self.assertIn("/items/bulk?", called_url)
        self.assertNotIn("/items?ids=", called_url)
        attributes = parse_qs(urlparse(called_url).query)["attributes"][0].split(",")
        self.assertIn("id", attributes)
        self.assertIn("status_code", attributes)
        self.assertIn("body.id", attributes)

    def test_bulk_fails_if_an_item_is_omitted(self) -> None:
        client = MeliClient(min_request_interval=0)
        client._request_json = Mock(return_value=[])  # type: ignore[method-assign]
        with self.assertRaisesRegex(MeliApiError, "omitió"):
            client.get_items_bulk(["MLM123456789"], access_token="temporary")

    def test_bulk_accepts_confirmed_id_inside_body_when_root_id_is_omitted(self) -> None:
        client = MeliClient(min_request_interval=0)
        client._request_json = Mock(  # type: ignore[method-assign]
            return_value=[
                {
                    "status_code": 200,
                    "body": {"id": "MLM123456789", "title": "Báscula"},
                }
            ]
        )
        result = client.get_items_bulk(["MLM123456789"], access_token="temporary")
        self.assertEqual(result["MLM123456789"]["title"], "Báscula")

    def test_bulk_accepts_confirmed_body_when_projection_omits_status(self) -> None:
        client = MeliClient(min_request_interval=0)
        client._request_json = Mock(  # type: ignore[method-assign]
            return_value=[
                {
                    "body": {"id": "MLM123456789", "title": "Báscula"},
                }
            ]
        )
        result = client.get_items_bulk(["MLM123456789"], access_token="temporary")
        self.assertEqual(result["MLM123456789"]["title"], "Báscula")

    def test_bulk_does_not_infer_success_for_unrequested_body_id(self) -> None:
        client = MeliClient(min_request_interval=0)
        client._request_json = Mock(  # type: ignore[method-assign]
            return_value=[
                {
                    "body": {"id": "MLM999999999", "title": "Otro"},
                }
            ]
        )
        with self.assertRaisesRegex(MeliApiError, "status_code=None"):
            client.get_items_bulk(["MLM123456789"], access_token="temporary")

    def test_bulk_accepts_legacy_code_field(self) -> None:
        client = MeliClient(min_request_interval=0)
        client._request_json = Mock(  # type: ignore[method-assign]
            return_value=[
                {
                    "code": 200,
                    "body": {"id": "MLM123456789", "title": "Báscula"},
                }
            ]
        )
        result = client.get_items_bulk(["MLM123456789"], access_token="temporary")
        self.assertEqual(result["MLM123456789"]["title"], "Báscula")

    def test_bulk_reports_status_for_error_entry_without_exposing_body(self) -> None:
        client = MeliClient(min_request_interval=0)
        client._request_json = Mock(  # type: ignore[method-assign]
            return_value=[
                {
                    "status_code": 403,
                    "body": {"message": "private diagnostic", "access_token": "never-log-this"},
                }
            ]
        )
        with self.assertRaises(MeliApiError) as captured:
            client.get_items_bulk(["MLM123456789"], access_token="temporary")
        self.assertEqual(captured.exception.status_code, 403)
        message = str(captured.exception)
        self.assertIn("MLM123456789", message)
        self.assertIn("status_code=403", message)
        self.assertNotIn("private diagnostic", message)
        self.assertNotIn("never-log-this", message)

    def test_sale_price_uses_current_price_resource(self) -> None:
        client = MeliClient(min_request_interval=0)
        client._request_json = Mock(return_value={"amount": 135, "currency_id": "MXN"})  # type: ignore[method-assign]
        result = client.get_sale_price("MLM123456789", access_token="temporary")
        self.assertEqual(result, {"amount": 135, "currency_id": "MXN"})
        called_url = client._request_json.call_args.args[1]  # type: ignore[union-attr]
        self.assertTrue(called_url.endswith("/items/MLM123456789/sale_price?context=channel_marketplace"))

    def test_429_is_retried_without_exposing_response_fields(self) -> None:
        calls = 0

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, size=-1):
                return b'{"ok": true}'

        def opener(request, *, timeout):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise HTTPError(
                    request.full_url,
                    429,
                    "Too Many Requests",
                    {"Retry-After": "0"},
                    BytesIO(b'{"message":"slow down","access_token":"never-log-this"}'),
                )
            return Response()

        client = MeliClient(
            max_retries=1,
            min_request_interval=0,
            sleep=lambda _: None,
            random_value=lambda: 0,
            opener=opener,
        )
        self.assertEqual(client._request_json("GET", "https://api.mercadolibre.com/test"), {"ok": True})
        self.assertEqual(calls, 2)


if __name__ == "__main__":
    unittest.main()
