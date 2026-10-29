"""Configuration and authenticated GET requests for schema discovery."""

import json
import os
import re
import tomllib
from dataclasses import dataclass
from http.client import HTTPException
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class DiscoveryError(Exception):
    """An actionable configuration, API or schema error."""


def object_value(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DiscoveryError(f"{context}: objet JSON attendu.")
    return value


def object_list(value: Any, context: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise DiscoveryError(f"{context}: liste JSON attendue.")
    return [object_value(item, context) for item in value]


def text_value(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DiscoveryError(f"{context}: texte non vide attendu.")
    return value


def identifier(value: Any, context: str) -> str:
    result = text_value(value, context)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", result):
        raise DiscoveryError(f"{context}: identifiant technique invalide.")
    return result


@dataclass(frozen=True)
class Config:
    base_id: str
    airtable_table_id: str
    modified_field: str
    grist_url: str
    document_id: str
    grist_table_id: str
    key_column: str

    @classmethod
    def load(cls, path: Path) -> "Config":
        try:
            with path.open("rb") as stream:
                data = tomllib.load(stream)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise DiscoveryError("Configuration TOML absente, illisible ou invalide.") from exc
        airtable = object_value(data.get("airtable"), "[airtable]")
        grist = object_value(data.get("grist"), "[grist]")
        url = text_value(grist.get("server_url"), "grist.server_url").rstrip("/")
        try:
            parts = urlsplit(url)
            valid_port = parts.port
        except ValueError as exc:
            raise DiscoveryError("grist.server_url: URL invalide.") from exc
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or any(character.isspace() for character in url)
            or valid_port == 0
        ):
            raise DiscoveryError(
                "grist.server_url: URL HTTPS requise, sans credentials, query ni fragment."
            )
        return cls(
            base_id=identifier(airtable.get("base_id"), "airtable.base_id"),
            airtable_table_id=identifier(airtable.get("table_id"), "airtable.table_id"),
            modified_field=text_value(airtable.get("modified_field"), "airtable.modified_field"),
            grist_url=url,
            document_id=identifier(grist.get("document_id"), "grist.document_id"),
            grist_table_id=identifier(grist.get("table_id"), "grist.table_id"),
            key_column=identifier(grist.get("airtable_id_column"), "grist.airtable_id_column"),
        )


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a bearer token to a redirected destination.
        return None


class JsonClient:
    def __init__(self, base_url: str, token: str, service: str):
        if not token.strip() or any(character.isspace() for character in token):
            raise DiscoveryError(f"{service}: token absent ou invalide.")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.service = service
        self.opener = build_opener(NoRedirect())

    def get(self, path: str) -> dict[str, Any]:
        request = Request(
            f"{self.base_url}/{path.lstrip('/')}",
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
            method="GET",
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                payload = json.load(response)
        except HTTPError as exc:
            hints = {
                401: "Vérifier le token.",
                403: "Vérifier les permissions et l'accès à la base ou au document.",
                404: "Vérifier l'URL et les identifiants techniques.",
                429: "Limite API atteinte ; relancer la découverte plus tard.",
            }
            hint = hints.get(exc.code, "Vérifier le service avant de relancer.")
            if 300 <= exc.code < 400:
                hint = "Redirection refusée ; utiliser l'URL HTTPS directe du service."
            exc.close()
            raise DiscoveryError(f"{self.service}: HTTP {exc.code}. {hint}") from None
        except (URLError, TimeoutError, OSError, HTTPException) as exc:
            raise DiscoveryError(
                f"{self.service}: connexion impossible ou délai dépassé ; vérifier réseau et TLS."
            ) from exc
        except (ValueError, UnicodeError) as exc:
            raise DiscoveryError(f"{self.service}: réponse JSON invalide.") from exc
        return object_value(payload, self.service)


def discover(config: Config) -> dict[str, Any]:
    # Validate both secrets before making any request.
    airtable = JsonClient(
        "https://api.airtable.com/v0",
        os.environ.get("AIRTABLE_TOKEN", ""),
        "Airtable",
    )
    grist = JsonClient(config.grist_url, os.environ.get("GRIST_TOKEN", ""), "Grist")
    airtable_tables = object_list(
        airtable.get(f"meta/bases/{quote(config.base_id, safe='')}/tables").get("tables"),
        "Airtable tables",
    )
    document_path = f"api/docs/{quote(config.document_id, safe='')}"
    grist_tables = object_list(grist.get(f"{document_path}/tables").get("tables"), "Grist tables")
    for table in grist_tables:
        table_id = text_value(table.get("id"), "Grist table id")
        table["columns"] = object_list(
            grist.get(f"{document_path}/tables/{quote(table_id, safe='')}/columns").get("columns"),
            f"Grist columns ({table_id})",
        )
    return {"airtable": {"tables": airtable_tables}, "grist": {"tables": grist_tables}}
