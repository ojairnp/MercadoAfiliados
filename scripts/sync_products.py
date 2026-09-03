"""Sincroniza productos configurados y genera un JSON público seguro."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.meli_client import MeliApiError, MeliClient  # noqa: E402


ITEM_ID_PATTERN = re.compile(r"^MLM\d{6,20}$")
CATEGORY_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
PROHIBITED_CATEGORY_TERMS = {"suplementos", "suplemento", "proteina", "creatina", "electrolitos"}
AFFILIATE_HOSTS = ("mercadolibre.com.mx", "mercadolibre.com", "mercado.li", "meli.la")
MAX_PRODUCTS = 200
MAX_CONFIG_BYTES = 1_000_000
PUBLIC_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ProductConfig:
    id: str
    affiliate_url: str
    category: str
    enabled: bool


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise ValueError(f"Falta la configuración requerida: {name}")
    return value.strip()


def _is_host_or_subdomain(host: str, allowed: Iterable[str]) -> bool:
    normalized = host.lower().rstrip(".")
    return any(normalized == domain or normalized.endswith(f".{domain}") for domain in allowed)


def _validated_https_url(value: Any, *, allowed_hosts: Iterable[str] | None = None) -> str | None:
    if not isinstance(value, str) or len(value) > 2048:
        return None
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return None
    if allowed_hosts is not None and not _is_host_or_subdomain(parsed.hostname, allowed_hosts):
        return None
    return value


def _validate_affiliate_url(value: Any) -> str:
    url = _validated_https_url(value, allowed_hosts=AFFILIATE_HOSTS)
    if url is None:
        raise ValueError(
            "affiliate_url debe ser HTTPS y pertenecer a un dominio oficial de Mercado Libre/Mercado Li"
        )
    return url


def _category_is_prohibited(category: str) -> bool:
    return any(term in category.split("-") for term in PROHIBITED_CATEGORY_TERMS)


def load_config(path: Path) -> list[ProductConfig]:
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

    products: list[ProductConfig] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        label = f"products[{index}]"
        if not isinstance(entry, dict):
            raise ValueError(f"{label} debe ser un objeto")
        unknown = set(entry) - {"id", "affiliate_url", "category", "enabled"}
        if unknown:
            raise ValueError(f"{label} contiene campos no permitidos: {', '.join(sorted(unknown))}")
        item_id = entry.get("id")
        if not isinstance(item_id, str) or not ITEM_ID_PATTERN.fullmatch(item_id):
            raise ValueError(f"{label}.id debe tener el formato MLM seguido de 6 a 20 dígitos")
        if item_id in seen:
            raise ValueError(f"ID duplicado en configuración: {item_id}")
        seen.add(item_id)
        category = entry.get("category")
        if not isinstance(category, str) or not CATEGORY_PATTERN.fullmatch(category):
            raise ValueError(f"{label}.category debe ser un slug en minúsculas, por ejemplo basculas")
        if _category_is_prohibited(category):
            raise ValueError(
                f"{label}.category está excluida actualmente del programa de afiliados de México: {category}"
            )
        enabled = entry.get("enabled")
        if not isinstance(enabled, bool):
            raise ValueError(f"{label}.enabled debe ser true o false")
        products.append(
            ProductConfig(
                id=item_id,
                affiliate_url=_validate_affiliate_url(entry.get("affiliate_url")),
                category=category,
                enabled=enabled,
            )
        )
    return products


def load_existing_output(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": PUBLIC_SCHEMA_VERSION, "generated_at": None, "products": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": PUBLIC_SCHEMA_VERSION, "generated_at": None, "products": []}
    if not isinstance(payload, dict) or not isinstance(payload.get("products"), list):
        return {"schema_version": PUBLIC_SCHEMA_VERSION, "generated_at": None, "products": []}
    return payload


def validate_sync_config(configs: list[ProductConfig]) -> None:
    if not any(product.enabled for product in configs):
        raise ValueError("No hay productos habilitados en config/products.json")


def _clean_text(value: Any, *, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())[:maximum]
    return cleaned or None


def _image_url(item: dict[str, Any]) -> str | None:
    pictures = item.get("pictures")
    if isinstance(pictures, list):
        for picture in pictures:
            if not isinstance(picture, dict):
                continue
            url = _validated_https_url(picture.get("secure_url"), allowed_hosts=("mlstatic.com",))
            if url:
                return url
    return _validated_https_url(item.get("thumbnail"), allowed_hosts=("mlstatic.com",))


def _public_product(
    config: ProductConfig,
    item: dict[str, Any],
    price: dict[str, Any],
    *,
    timestamp: str,
) -> dict[str, Any]:
    if item.get("id") != config.id:
        raise MeliApiError(f"La API devolvió un id distinto al solicitado para {config.id}")
    if item.get("site_id") != "MLM":
        raise MeliApiError(f"El artículo {config.id} no pertenece a Mercado Libre México")
    title = _clean_text(item.get("title"), maximum=200)
    status = _clean_text(item.get("status"), maximum=40)
    currency = price.get("currency_id")
    if title is None or status is None:
        raise MeliApiError(f"El artículo {config.id} no contiene título o estado válido")
    if currency != "MXN":
        raise MeliApiError(f"El artículo {config.id} no usa moneda MXN")
    quantity = item.get("available_quantity")
    available = status == "active" and isinstance(quantity, int) and not isinstance(quantity, bool) and quantity > 0
    permalink = _validated_https_url(item.get("permalink"), allowed_hosts=("mercadolibre.com.mx",))
    if permalink is None:
        raise MeliApiError(f"El artículo {config.id} no contiene un permalink HTTPS válido")
    seller_id = item.get("seller_id")
    if not isinstance(seller_id, (int, str)) or isinstance(seller_id, bool):
        seller_id = None
    official_store_id = item.get("official_store_id")
    if not isinstance(official_store_id, (int, str)) or isinstance(official_store_id, bool):
        official_store_id = None
    return {
        "id": config.id,
        "title": title,
        "price": price["amount"],
        "currency": currency,
        "image": _image_url(item),
        "permalink": permalink,
        "affiliate_url": config.affiliate_url,
        "category": config.category,
        "available": available,
        "status": status,
        "condition": _clean_text(item.get("condition"), maximum=30),
        "seller_id": seller_id,
        "official_store_id": official_store_id,
        "updated_at": timestamp,
    }


def _without_timestamp(product: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in product.items() if key != "updated_at"}


def synchronize(
    configs: list[ProductConfig],
    *,
    client: MeliClient,
    access_token: str,
    existing: dict[str, Any],
    timestamp: str,
) -> dict[str, Any]:
    enabled = [product for product in configs if product.enabled]
    existing_products = {
        product["id"]: product
        for product in existing.get("products", [])
        if isinstance(product, dict) and isinstance(product.get("id"), str)
    }
    items: dict[str, dict[str, Any]] = {}
    ids = [product.id for product in enabled]
    for offset in range(0, len(ids), 20):
        items.update(client.get_items_bulk(ids[offset : offset + 20], access_token=access_token))

    generated: list[dict[str, Any]] = []
    for config in enabled:
        price = client.get_sale_price(config.id, access_token=access_token)
        product = _public_product(config, items[config.id], price, timestamp=timestamp)
        old = existing_products.get(config.id)
        if isinstance(old, dict) and _without_timestamp(old) == _without_timestamp(product):
            previous_timestamp = old.get("updated_at")
            if isinstance(previous_timestamp, str):
                product["updated_at"] = previous_timestamp
        generated.append(product)

    previous_list = existing.get("products")
    changed = not isinstance(previous_list, list) or previous_list != generated
    previous_generated_at = existing.get("generated_at")
    return {
        "schema_version": PUBLIC_SCHEMA_VERSION,
        "generated_at": timestamp if changed else previous_generated_at,
        "products": generated,
    }


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=False)
            stream.write("\n")
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _atomic_write_secret(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(value)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def run(args: argparse.Namespace) -> int:
    configs = load_config(Path(args.config))
    validate_sync_config(configs)
    if args.validate_only:
        print("Configuración válida: hay al menos un producto habilitado.")
        return 0
    if not args.next_refresh_token_file:
        raise ValueError("Falta --next-refresh-token-file; es obligatorio para no perder la rotación OAuth")
    output_path = Path(args.output)
    next_token_path = Path(args.next_refresh_token_file)
    client_id = _required_env("MELI_CLIENT_ID")
    client_secret = _required_env("MELI_CLIENT_SECRET")
    refresh_token = _required_env("MELI_REFRESH_TOKEN")
    client = MeliClient(timeout=args.timeout, max_retries=args.retries)

    tokens = client.refresh_access_token(
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token,
    )
    # Debe ocurrir inmediatamente: el token anterior ya quedó invalidado.
    _atomic_write_secret(next_token_path, tokens.refresh_token)

    timestamp = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    payload = synchronize(
        configs,
        client=client,
        access_token=tokens.access_token,
        existing=load_existing_output(output_path),
        timestamp=timestamp,
    )
    _atomic_write_json(output_path, payload)
    print(f"Sincronización correcta: {len(payload['products'])} producto(s) público(s).")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Genera public/data/products.json desde Mercado Libre")
    parser.add_argument("--config", default="config/products.json")
    parser.add_argument("--output", default="public/data/products.json")
    parser.add_argument("--next-refresh-token-file")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--retries", type=int, default=4)
    return parser


def main() -> int:
    try:
        return run(build_parser().parse_args())
    except (ValueError, OSError, MeliApiError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
