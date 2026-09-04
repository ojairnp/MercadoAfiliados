"""Resuelve enlaces afiliados cortos sin abandonar dominios oficiales de Mercado Libre."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from collections.abc import Callable, Mapping
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urljoin, urlparse, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.sync_products import (  # noqa: E402
    ITEM_ID_PATTERN,
    MAX_CONFIG_BYTES,
    MAX_PRODUCTS,
    load_config,
)


OFFICIAL_HOSTS = ("meli.la", "mercado.li", "mercadolibre.com.mx", "mercadolibre.com")
SHORT_LINK_HOSTS = frozenset({"meli.la", "mercado.li"})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
MLM_IN_URL_PATTERN = re.compile(r"(?i)(?<![a-z0-9])MLM[-_]?([0-9]{6,20})(?![0-9])")
MAX_URL_LENGTH = 4096
MAX_HTML_BYTES = 4_000_000


class LinkResolutionError(ValueError):
    """Indica que un enlace no pudo resolverse de forma segura."""


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


_NO_REDIRECT_OPENER = build_opener(_NoRedirectHandler())
FetchResult = tuple[int, Mapping[str, str], str, str]
Fetcher = Callable[[str, float], FetchResult]


class _ProductLinkParser(HTMLParser):
    """Recolecta enlaces identificados por la página como el producto principal."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.product_links: list[str] = []
        self._anchor_href: str | None = None
        self._anchor_label = ""
        self._anchor_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a" or self._anchor_href is not None:
            return
        attributes = {name.lower(): value for name, value in attrs}
        href = attributes.get("href")
        if isinstance(href, str):
            self._anchor_href = href
            self._anchor_label = " ".join(
                value or ""
                for value in (attributes.get("aria-label"), attributes.get("title"))
            )
            self._anchor_text = []

    def handle_data(self, data: str) -> None:
        if self._anchor_href is not None:
            self._anchor_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or self._anchor_href is None:
            return
        description = " ".join((self._anchor_label, " ".join(self._anchor_text)))
        normalized = " ".join(description.lower().split())
        if "ir a producto" in normalized:
            self.product_links.append(self._anchor_href)
        self._anchor_href = None
        self._anchor_label = ""
        self._anchor_text = []


class _ProductCardParser(HTMLParser):
    """Extrae únicamente tarjetas de producto renderizadas por Mercado Libre."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cards: list[dict[str, Any]] = []
        self._card: dict[str, Any] | None = None
        self._card_div_depth = 0
        self._price_div_depth: int | None = None
        self._capture_title = False
        self._capture_fraction = False
        self._capture_cents = False

    @staticmethod
    def _classes(attributes: dict[str, str | None]) -> set[str]:
        return set((attributes.get("class") or "").split())

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name.lower(): value for name, value in attrs}
        classes = self._classes(attributes)
        tag = tag.lower()

        if tag == "div":
            if self._card is None and "poly-card" in classes:
                self._card = {"title_parts": [], "fraction_parts": [], "cents_parts": []}
                self._card_div_depth = 1
            elif self._card is not None:
                self._card_div_depth += 1
            if self._card is not None and "poly-price__current" in classes:
                self._price_div_depth = self._card_div_depth

        if self._card is None:
            return
        if tag == "img" and "poly-component__picture" in classes:
            self._card["image"] = attributes.get("src")
        elif tag == "a" and "poly-component__title" in classes:
            self._card["href"] = attributes.get("href")
            self._capture_title = True
        elif self._price_div_depth is not None:
            if attributes.get("data-andes-money-amount-fraction") == "true":
                self._capture_fraction = True
            elif attributes.get("data-andes-money-amount-cents") == "true":
                self._capture_cents = True

    def handle_data(self, data: str) -> None:
        if self._card is None:
            return
        if self._capture_title:
            self._card["title_parts"].append(data)
        if self._capture_fraction:
            self._card["fraction_parts"].append(data)
        if self._capture_cents:
            self._card["cents_parts"].append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._card is None:
            return
        if tag == "a":
            self._capture_title = False
        elif tag == "span":
            self._capture_fraction = False
            self._capture_cents = False
        elif tag == "div":
            if self._price_div_depth == self._card_div_depth:
                self._price_div_depth = None
            self._card_div_depth -= 1
            if self._card_div_depth == 0:
                self.cards.append(self._card)
                self._card = None


def _validated_image_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not (host == "mlstatic.com" or host.endswith(".mlstatic.com")):
        return None
    return value


def extract_product_snapshot_from_html(
    html: str,
    *,
    base_url: str,
    item_id: str,
) -> dict[str, Any] | None:
    """Obtiene la ficha pública de la tarjeta afiliada que coincide con el ID resuelto."""

    parser = _ProductCardParser()
    try:
        parser.feed(html)
        parser.close()
    except (ValueError, RecursionError):
        return None
    for card in parser.cards:
        href = card.get("href")
        if not isinstance(href, str):
            continue
        try:
            absolute = validate_official_url(urljoin(base_url, href))
        except LinkResolutionError:
            continue
        if extract_mlm_id(absolute) != item_id:
            continue
        title = " ".join("".join(card.get("title_parts", [])).split())[:200]
        fraction = "".join(character for character in "".join(card.get("fraction_parts", [])) if character.isdigit())
        cents = "".join(character for character in "".join(card.get("cents_parts", [])) if character.isdigit())
        image = _validated_image_url(card.get("image"))
        if not title or not fraction or image is None:
            return None
        cents_value = int((cents or "0")[:2].ljust(2, "0"))
        amount: int | float = int(fraction)
        if cents_value:
            amount += cents_value / 100
        parsed = urlparse(absolute)
        permalink = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        return {
            "title": title,
            "price": amount,
            "currency": "MXN",
            "image": image,
            "permalink": permalink,
            "available": True,
            "status": "active",
            "condition": None,
            "seller_id": None,
            "official_store_id": None,
        }
    return None


def _normalized_host(url: str) -> str:
    parsed = urlparse(url)
    return (parsed.hostname or "").lower().rstrip(".")


def _is_official_host(host: str) -> bool:
    return any(host == domain or host.endswith(f".{domain}") for domain in OFFICIAL_HOSTS)


def validate_official_url(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_URL_LENGTH:
        raise LinkResolutionError("El enlace afiliado no es una URL válida")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise LinkResolutionError("El enlace afiliado contiene caracteres de control")
    parsed = urlparse(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise LinkResolutionError("El enlace afiliado contiene un puerto inválido") from exc
    host = _normalized_host(value)
    if (
        parsed.scheme.lower() != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or not _is_official_host(host)
    ):
        raise LinkResolutionError(
            "El enlace y cada redirección deben usar HTTPS en un dominio oficial de Mercado Libre"
        )
    return value


def extract_mlm_id(value: str) -> str | None:
    """Extrae y normaliza MLM123 o MLM-123 incluso si aparece codificado en la URL."""

    candidate = value
    for _ in range(3):
        match = MLM_IN_URL_PATTERN.search(candidate)
        if match:
            item_id = f"MLM{match.group(1)}"
            if ITEM_ID_PATTERN.fullmatch(item_id):
                return item_id
        decoded = unquote(candidate)
        if decoded == candidate:
            break
        candidate = decoded
    return None


def extract_product_id_from_html(html: str, *, base_url: str) -> str | None:
    """Extrae el producto principal de una landing oficial, no de recomendaciones genéricas."""

    parser = _ProductLinkParser()
    try:
        parser.feed(html)
        parser.close()
    except (ValueError, RecursionError):
        return None
    for href in parser.product_links:
        try:
            absolute = validate_official_url(urljoin(base_url, href))
        except LinkResolutionError:
            continue
        item_id = extract_mlm_id(absolute)
        if item_id:
            return item_id
    return None


def _fetch_once(url: str, timeout: float) -> FetchResult:
    request = Request(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
            "User-Agent": "MercadoAfiliados-LinkResolver/1.0",
        },
        method="GET",
    )
    try:
        response = _NO_REDIRECT_OPENER.open(request, timeout=timeout)
    except HTTPError as exc:
        try:
            if exc.code in REDIRECT_STATUSES:
                return exc.code, exc.headers, exc.geturl(), ""
            raise LinkResolutionError(f"Mercado Libre respondió con HTTP {exc.code}") from exc
        finally:
            exc.close()
    except (URLError, TimeoutError, OSError) as exc:
        raise LinkResolutionError("No se pudo consultar el enlace afiliado") from exc

    with response:
        status = response.getcode()
        response_url = response.geturl()
        content_type = response.headers.get("Content-Type", "")
        body = ""
        if 200 <= status < 300 and "text/html" in content_type.lower():
            raw = response.read(MAX_HTML_BYTES + 1)
            if len(raw) > MAX_HTML_BYTES:
                raise LinkResolutionError("La página oficial excede el tamaño máximo permitido")
            charset = response.headers.get_content_charset() or "utf-8"
            body = raw.decode(charset, errors="replace")
        return status, response.headers, response_url, body


def resolve_affiliate_product(
    affiliate_url: str,
    *,
    timeout: float = 10.0,
    max_redirects: int = 10,
    fetcher: Fetcher = _fetch_once,
) -> tuple[str, dict[str, Any] | None]:
    """Devuelve el ID y, cuando existe, la ficha pública de la tarjeta afiliada."""

    current = validate_official_url(affiliate_url)
    direct_id = extract_mlm_id(current)
    if direct_id:
        return direct_id, None
    if _normalized_host(current) not in SHORT_LINK_HOSTS:
        raise LinkResolutionError(
            "Un producto sin id debe usar un enlace corto https://meli.la/ o https://mercado.li/"
        )
    if timeout <= 0 or timeout > 60:
        raise LinkResolutionError("El timeout debe ser mayor que 0 y no exceder 60 segundos")
    if max_redirects < 1 or max_redirects > 20:
        raise LinkResolutionError("max_redirects debe estar entre 1 y 20")

    visited: set[str] = set()
    for redirect_count in range(max_redirects + 1):
        if current in visited:
            raise LinkResolutionError("El enlace afiliado contiene un ciclo de redirecciones")
        visited.add(current)

        status, headers, response_url, body = fetcher(current, timeout)
        response_url = validate_official_url(response_url)
        item_id = extract_mlm_id(response_url)
        if item_id:
            return item_id, None

        if status in REDIRECT_STATUSES:
            if redirect_count >= max_redirects:
                raise LinkResolutionError("El enlace afiliado excedió el máximo de redirecciones")
            location = headers.get("Location")
            if not isinstance(location, str) or not location.strip():
                raise LinkResolutionError("Mercado Libre devolvió una redirección sin destino")
            current = validate_official_url(urljoin(response_url, location.strip()))
            item_id = extract_mlm_id(current)
            if item_id:
                return item_id, None
            continue

        if 200 <= status < 300:
            item_id = extract_product_id_from_html(body, base_url=response_url)
            if item_id:
                snapshot = extract_product_snapshot_from_html(
                    body,
                    base_url=response_url,
                    item_id=item_id,
                )
                return item_id, snapshot
            raise LinkResolutionError("El destino oficial no contiene un ID de producto MLM")
        raise LinkResolutionError(f"Mercado Libre respondió con HTTP {status}")

    raise LinkResolutionError("No se pudo resolver el enlace afiliado")


def resolve_affiliate_url(
    affiliate_url: str,
    *,
    timeout: float = 10.0,
    max_redirects: int = 10,
    fetcher: Fetcher = _fetch_once,
) -> str:
    """Compatibilidad: devuelve solamente el ID MLM normalizado."""

    item_id, _snapshot = resolve_affiliate_product(
        affiliate_url,
        timeout=timeout,
        max_redirects=max_redirects,
        fetcher=fetcher,
    )
    return item_id


def _read_config(path: Path) -> list[dict[str, Any]]:
    try:
        if path.stat().st_size > MAX_CONFIG_BYTES:
            raise ValueError("config/products.json excede el tamaño máximo permitido")
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"No existe el archivo de configuración: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON inválido en {path}: línea {exc.lineno}, columna {exc.colno}") from exc
    if not isinstance(raw, list):
        raise ValueError("config/products.json debe contener una lista")
    if len(raw) > MAX_PRODUCTS:
        raise ValueError(f"Se permiten como máximo {MAX_PRODUCTS} productos")
    if not all(isinstance(entry, dict) for entry in raw):
        raise ValueError("Cada producto debe ser un objeto JSON")
    return raw


def resolve_config(
    entries: list[dict[str, Any]],
    *,
    timeout: float = 10.0,
    max_redirects: int = 10,
    fetcher: Fetcher = _fetch_once,
) -> list[dict[str, Any]]:
    resolved: list[dict[str, Any]] = []
    cache: dict[str, tuple[str, dict[str, Any] | None]] = {}
    for index, entry in enumerate(entries):
        copied = dict(entry)
        existing_id = copied.get("id")
        if existing_id is not None:
            if not isinstance(existing_id, str) or not ITEM_ID_PATTERN.fullmatch(existing_id):
                raise ValueError(f"products[{index}].id debe tener el formato MLM seguido de 6 a 20 dígitos")
        else:
            affiliate_url = copied.get("affiliate_url")
            if not isinstance(affiliate_url, str):
                raise ValueError(f"products[{index}].affiliate_url es obligatorio")
            if affiliate_url not in cache:
                cache[affiliate_url] = resolve_affiliate_product(
                    affiliate_url,
                    timeout=timeout,
                    max_redirects=max_redirects,
                    fetcher=fetcher,
                )
            item_id, snapshot = cache[affiliate_url]
            copied["id"] = item_id
            if snapshot is not None:
                copied["snapshot"] = snapshot
        resolved.append(copied)
    return resolved


def _write_validated_config(path: Path, entries: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(entries, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        # Reutiliza exactamente la validación que consumirá sync_products.py.
        load_config(temporary)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def run(args: argparse.Namespace) -> int:
    entries = _read_config(Path(args.config))
    resolved = resolve_config(
        entries,
        timeout=args.timeout,
        max_redirects=args.max_redirects,
    )
    _write_validated_config(Path(args.output), resolved)
    print(f"Resolución correcta: {len(resolved)} producto(s) con ID validado.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Resuelve enlaces afiliados cortos a IDs MLM")
    parser.add_argument("--config", default="config/products.json")
    parser.add_argument("--output", required=True)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--max-redirects", type=int, default=10)
    return parser


def main() -> int:
    try:
        return run(build_parser().parse_args())
    except (ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
