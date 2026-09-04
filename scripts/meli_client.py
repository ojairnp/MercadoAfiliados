"""Cliente pequeño y defensivo para la API de Mercado Libre.

No registra cuerpos de autenticación ni valores de tokens.
"""

from __future__ import annotations

import json
import random
import socket
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


API_BASE_URL = "https://api.mercadolibre.com"
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_RESPONSE_BYTES = 5_000_000
ITEM_ATTRIBUTES = (
    "id",
    "site_id",
    "title",
    "category_id",
    "currency_id",
    "status",
    "available_quantity",
    "permalink",
    "thumbnail",
    "pictures",
    "seller_id",
    "official_store_id",
    "condition",
)


class MeliApiError(RuntimeError):
    """Error seguro de la API; nunca contiene credenciales."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class TokenResponse:
    access_token: str
    refresh_token: str
    expires_in: int
    token_type: str


class MeliClient:
    """Cliente HTTP con timeout, backoff exponencial y jitter."""

    def __init__(
        self,
        *,
        timeout: float = 15.0,
        max_retries: int = 4,
        min_request_interval: float = 0.25,
        sleep: Callable[[float], None] = time.sleep,
        random_value: Callable[[], float] = random.random,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout debe ser mayor que cero")
        if not 0 <= max_retries <= 8:
            raise ValueError("max_retries debe estar entre 0 y 8")
        self.timeout = timeout
        self.max_retries = max_retries
        self.min_request_interval = max(0.0, min_request_interval)
        self._sleep = sleep
        self._random = random_value
        self._opener = opener
        self._last_request_at = 0.0

    def _wait_for_rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        remaining = self.min_request_interval - elapsed
        if remaining > 0:
            self._sleep(remaining)

    def _backoff(self, attempt: int, retry_after: str | None = None) -> float:
        if retry_after:
            try:
                return min(max(float(retry_after), 0.0), 60.0)
            except ValueError:
                pass
        return min(2**attempt + self._random(), 30.0)

    @staticmethod
    def _safe_api_message(raw: bytes) -> str:
        """Extrae solo un mensaje breve; descarta cualquier otro campo."""
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return "respuesta no JSON"
        if isinstance(payload, dict):
            message = payload.get("message") or payload.get("error")
            if isinstance(message, str):
                return message[:240]
        return "respuesta de error sin detalle público"

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        access_token: str | None = None,
        form: dict[str, str] | None = None,
    ) -> Any:
        headers = {
            "Accept": "application/json",
            "User-Agent": "VeroBeth-MercadoAfiliados/1.0",
        }
        body: bytes | None = None
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        if form is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            body = urlencode(form).encode("utf-8")

        for attempt in range(self.max_retries + 1):
            self._wait_for_rate_limit()
            request = Request(url, data=body, headers=headers, method=method)
            try:
                with self._opener(request, timeout=self.timeout) as response:
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise MeliApiError("Mercado Libre devolvió una respuesta demasiado grande")
                self._last_request_at = time.monotonic()
                try:
                    return json.loads(raw.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise MeliApiError("Mercado Libre devolvió JSON inválido") from exc
            except HTTPError as exc:
                self._last_request_at = time.monotonic()
                retry_after = exc.headers.get("Retry-After") if exc.headers is not None else None
                try:
                    raw = exc.read(4096)
                finally:
                    exc.close()
                if exc.code in RETRYABLE_STATUS and attempt < self.max_retries:
                    self._sleep(self._backoff(attempt, retry_after))
                    continue
                detail = self._safe_api_message(raw)
                if exc.code == 401:
                    hint = "token inválido o vencido"
                elif exc.code == 403:
                    hint = "permiso insuficiente o acceso denegado"
                elif exc.code == 404:
                    hint = "recurso no encontrado"
                elif exc.code == 429:
                    hint = "límite de solicitudes excedido"
                else:
                    hint = "error HTTP"
                raise MeliApiError(
                    f"Mercado Libre: {hint} ({exc.code}): {detail}",
                    status_code=exc.code,
                ) from exc
            except (URLError, TimeoutError, socket.timeout) as exc:
                self._last_request_at = time.monotonic()
                if attempt < self.max_retries:
                    self._sleep(self._backoff(attempt))
                    continue
                raise MeliApiError("No fue posible conectar con Mercado Libre después de reintentos") from exc

        raise AssertionError("bucle de reintentos agotado de forma inesperada")

    @staticmethod
    def _parse_token_response(payload: Any) -> TokenResponse:
        if not isinstance(payload, dict):
            raise MeliApiError("La respuesta OAuth no es un objeto JSON")
        access_token = payload.get("access_token")
        refresh_token = payload.get("refresh_token")
        expires_in = payload.get("expires_in")
        token_type = payload.get("token_type", "bearer")
        if not isinstance(access_token, str) or not access_token:
            raise MeliApiError("La respuesta OAuth no contiene access_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise MeliApiError("La respuesta OAuth no contiene el refresh_token rotado")
        if not isinstance(expires_in, int) or expires_in <= 0:
            raise MeliApiError("La respuesta OAuth contiene expires_in inválido")
        if not isinstance(token_type, str):
            raise MeliApiError("La respuesta OAuth contiene token_type inválido")
        return TokenResponse(access_token, refresh_token, expires_in, token_type)

    def exchange_authorization_code(
        self,
        *,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        code: str,
        code_verifier: str,
    ) -> TokenResponse:
        payload = self._request_json(
            "POST",
            f"{API_BASE_URL}/oauth/token",
            form={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
            },
        )
        return self._parse_token_response(payload)

    def refresh_access_token(
        self,
        *,
        client_id: str,
        client_secret: str,
        refresh_token: str,
    ) -> TokenResponse:
        payload = self._request_json(
            "POST",
            f"{API_BASE_URL}/oauth/token",
            form={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
            },
        )
        return self._parse_token_response(payload)

    def get_items_bulk(self, item_ids: Iterable[str], *, access_token: str) -> dict[str, dict[str, Any]]:
        ids = list(item_ids)
        if not ids:
            return {}
        if len(ids) > 20:
            raise ValueError("get_items_bulk acepta como máximo 20 IDs por lote")
        attributes = ",".join(("id", "status_code", *(f"body.{name}" for name in ITEM_ATTRIBUTES)))
        query = urlencode({"ids": ",".join(ids), "attributes": attributes})
        try:
            payload = self._request_json(
                "GET",
                f"{API_BASE_URL}/items/bulk?{query}",
                access_token=access_token,
            )
        except MeliApiError as exc:
            if exc.status_code != 403:
                raise
            # Algunas políticas de Mercado Libre rechazan el recurso bulk aunque
            # el mismo token pueda consultar los artículos de forma individual.
            return {
                item_id: self.get_item(item_id, access_token=access_token)
                for item_id in ids
            }
        if not isinstance(payload, list):
            raise MeliApiError("La respuesta de /items/bulk no es una lista")
        results: dict[str, dict[str, Any]] = {}
        fallback_ids: list[str] = []
        requested_ids = set(ids)
        for index, entry in enumerate(payload):
            if not isinstance(entry, dict):
                raise MeliApiError("/items/bulk devolvió una entrada inválida")
            body = entry.get("body")
            root_id = entry.get("id")
            body_id = body.get("id") if isinstance(body, dict) else None
            item_id = root_id if isinstance(root_id, str) else body_id
            status_code = entry.get("status_code")
            if status_code is None:
                # Compatibilidad defensiva con la forma verbose anterior.
                status_code = entry.get("code")
            if (
                status_code is None
                and isinstance(body, dict)
                and isinstance(body_id, str)
                and body_id in requested_ids
                and (not isinstance(root_id, str) or root_id == body_id)
            ):
                # La proyección `attributes` puede omitir el metadato verbose.
                # Un body con ID solicitado y consistente confirma el éxito.
                status_code = 200
            if not isinstance(item_id, str) and index < len(ids):
                # Solo se usa para identificar de forma segura un error por posición;
                # nunca se acepta como resultado exitoso sin un ID confirmado.
                error_item_id = ids[index]
            else:
                error_item_id = item_id
            if status_code == 403:
                if not isinstance(error_item_id, str) or error_item_id not in requested_ids:
                    raise MeliApiError("/items/bulk devolvió un rechazo para un id no solicitado")
                fallback_ids.append(error_item_id)
                continue
            if status_code != 200 or not isinstance(body, dict):
                label = error_item_id if isinstance(error_item_id, str) else "desconocido"
                raise MeliApiError(
                    f"No se pudo consultar el artículo {label} (status_code={status_code})",
                    status_code=status_code if isinstance(status_code, int) else None,
                )
            if not isinstance(item_id, str):
                raise MeliApiError("/items/bulk devolvió una entrada sin id")
            if item_id not in requested_ids:
                raise MeliApiError("/items/bulk devolvió un id no solicitado")
            if isinstance(root_id, str) and isinstance(body_id, str) and root_id != body_id:
                raise MeliApiError(f"/items/bulk devolvió IDs inconsistentes para {item_id}")
            results[item_id] = body
        for item_id in fallback_ids:
            results[item_id] = self.get_item(item_id, access_token=access_token)
        missing = set(ids) - set(results)
        if missing:
            raise MeliApiError(f"/items/bulk omitió {len(missing)} artículo(s) solicitado(s)")
        return results

    def get_item(self, item_id: str, *, access_token: str) -> dict[str, Any]:
        """Consulta un artículo individual y confirma que no haya sustitución de ID."""

        query = urlencode({"attributes": ",".join(ITEM_ATTRIBUTES)})
        payload = self._request_json(
            "GET",
            f"{API_BASE_URL}/items/{quote(item_id, safe='')}?{query}",
            access_token=access_token,
        )
        if not isinstance(payload, dict):
            raise MeliApiError(f"Respuesta individual inválida para {item_id}")
        returned_id = payload.get("id")
        if returned_id != item_id:
            raise MeliApiError(f"La consulta individual devolvió un id distinto para {item_id}")
        return payload

    def get_sale_price(self, item_id: str, *, access_token: str) -> dict[str, Any]:
        payload = self._request_json(
            "GET",
            f"{API_BASE_URL}/items/{quote(item_id, safe='')}/sale_price?context=channel_marketplace",
            access_token=access_token,
        )
        if not isinstance(payload, dict):
            raise MeliApiError(f"Precio inválido para {item_id}")
        amount = payload.get("amount")
        currency = payload.get("currency_id")
        if not isinstance(amount, (int, float)) or isinstance(amount, bool) or amount < 0:
            raise MeliApiError(f"Precio ausente o inválido para {item_id}")
        if not isinstance(currency, str) or not currency:
            raise MeliApiError(f"Moneda ausente para {item_id}")
        return {"amount": amount, "currency_id": currency}
