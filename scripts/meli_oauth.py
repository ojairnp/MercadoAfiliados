"""Asistente local de OAuth 2.0 + PKCE para Mercado Libre México.

Este archivo no debe ejecutarse dentro de GitHub Actions. El intercambio inicial
requiere interacción humana y nunca persiste el access token.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import tempfile
from datetime import UTC, datetime
from getpass import getpass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.meli_client import MeliApiError, MeliClient  # noqa: E402


AUTHORIZATION_URL = "https://auth.mercadolibre.com.mx/authorization"
DEFAULT_SESSION_PATH = Path(".local/meli_oauth_session.json")


def _required(value: str | None, name: str) -> str:
    if value is None or not value.strip():
        raise ValueError(f"Falta {name}")
    return value.strip()


def _validate_redirect_uri(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("MELI_REDIRECT_URI debe ser una URL HTTPS válida y sin credenciales")
    return value


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64).rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def _atomic_private_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def start(args: argparse.Namespace) -> int:
    client_id = _required(args.client_id or os.getenv("MELI_CLIENT_ID"), "MELI_CLIENT_ID")
    redirect_uri = _validate_redirect_uri(
        _required(args.redirect_uri or os.getenv("MELI_REDIRECT_URI"), "MELI_REDIRECT_URI")
    )
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(32)
    session_path = Path(args.session_file)
    _atomic_private_json(
        session_path,
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
            "state": state,
            "created_at": datetime.now(UTC).isoformat(),
        },
    )
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    print("Sesión PKCE creada en un archivo local ignorado por Git.")
    print("Abre esta URL y autoriza con la cuenta principal de Mercado Libre:\n")
    print(f"{AUTHORIZATION_URL}?{query}")
    return 0


def _load_session(path: Path) -> dict[str, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError("No existe la sesión PKCE. Ejecuta primero el comando start.") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("El archivo de sesión PKCE contiene JSON inválido") from exc
    required = ("client_id", "redirect_uri", "code_verifier", "state")
    if not isinstance(payload, dict) or any(not isinstance(payload.get(key), str) for key in required):
        raise ValueError("La sesión PKCE está incompleta")
    return {key: payload[key] for key in required}


def exchange(args: argparse.Namespace) -> int:
    session_path = Path(args.session_file)
    session = _load_session(session_path)
    client_secret = _required(os.getenv("MELI_CLIENT_SECRET"), "MELI_CLIENT_SECRET")
    code = _required(getpass("Pega el parámetro code de la URL: "), "authorization code")
    returned_state = _required(getpass("Pega el parámetro state de la URL: "), "state")
    if not hmac.compare_digest(returned_state, session["state"]):
        raise ValueError("El state recibido no coincide. No se realizará el intercambio OAuth.")

    client = MeliClient()
    tokens = client.exchange_authorization_code(
        client_id=session["client_id"],
        client_secret=client_secret,
        redirect_uri=session["redirect_uri"],
        code=code,
        code_verifier=session["code_verifier"],
    )
    session_path.unlink(missing_ok=True)
    print(f"Autorización correcta. Access token temporal válido por {tokens.expires_in} segundos.")
    print("El access token no se guardó ni se mostrará.")
    confirmation = input("¿Mostrar ahora el refresh token para copiarlo a GitHub Secrets? [s/N]: ").strip().lower()
    if confirmation == "s":
        print("\nMELI_REFRESH_TOKEN (cópialo y limpia la terminal después):")
        print(tokens.refresh_token)
    else:
        print("Refresh token no mostrado. Tendrás que repetir la autorización para obtenerlo.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OAuth 2.0 + PKCE para Mercado Libre México")
    subparsers = parser.add_subparsers(dest="command", required=True)

    start_parser = subparsers.add_parser("start", help="Genera PKCE y la URL de autorización")
    start_parser.add_argument("--client-id")
    start_parser.add_argument("--redirect-uri")
    start_parser.add_argument("--session-file", default=str(DEFAULT_SESSION_PATH))
    start_parser.set_defaults(handler=start)

    exchange_parser = subparsers.add_parser("exchange", help="Intercambia el authorization code")
    exchange_parser.add_argument("--session-file", default=str(DEFAULT_SESSION_PATH))
    exchange_parser.set_defaults(handler=exchange)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return int(args.handler(args))
    except (ValueError, MeliApiError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
