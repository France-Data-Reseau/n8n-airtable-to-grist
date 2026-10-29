"""Convert present Airtable values to the compact Grist cell format."""

import math
import re
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class RowError(Exception):
    """A row must be reported and retried, without a partial update."""


def timestamp(value: Any, naive_timezone: str) -> float:
    if not isinstance(value, str) or not re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", value):
        raise RowError("Date et heure ISO attendues.")
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            zone = ZoneInfo(naive_timezone)
            candidates = {
                candidate.timestamp()
                for fold in (0, 1)
                for candidate in (parsed.replace(tzinfo=zone, fold=fold),)
                if candidate.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) == parsed
            }
            if len(candidates) != 1:
                raise RowError("Heure locale ambiguë ou inexistante au changement d'heure.")
            return candidates.pop()
        return parsed.timestamp()
    except (ValueError, OverflowError, ZoneInfoNotFoundError) as exc:
        raise RowError("Date, heure ou fuseau invalide.") from exc


def convert(
    field: dict[str, Any],
    value: Any,
    references: dict[str, list[int]] | None = None,
) -> Any:
    kind = field["conversion"]
    if kind not in {
        "text", "number", "integer_checked", "boolean", "choice",
        "choice_list", "reference_list", "date", "datetime",
    }:
        raise RowError("Conversion non prise en charge.")
    if value is None or value == "" or value == []:
        if kind in {"text", "choice"}:
            return ""
        if kind == "boolean":
            return False
        return None
    if kind == "text":
        if not isinstance(value, str):
            raise RowError("Texte attendu.")
        return value
    if kind in {"number", "integer_checked"}:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RowError("Nombre attendu, sans conversion implicite depuis du texte.")
        try:
            finite = math.isfinite(value)
        except OverflowError as exc:
            raise RowError("Nombre hors limites.") from exc
        if not finite:
            raise RowError("Nombre non fini.")
        if kind == "integer_checked" and int(value) != value:
            raise RowError("Entier attendu ; aucun arrondi automatique.")
        return value
    if kind == "boolean":
        if not isinstance(value, bool):
            raise RowError("Booléen attendu.")
        return value
    if kind == "choice":
        if not isinstance(value, str) or value not in field["choices"]:
            raise RowError("Choix non autorisé dans le mapping validé.")
        return value
    if kind in {"choice_list", "reference_list"}:
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise RowError("Liste de textes ou d'identifiants attendue.")
        if len(set(value)) != len(value):
            raise RowError("Valeurs en doublon dans la liste source.")
        if kind == "choice_list":
            if any(item not in field["choices"] for item in value):
                raise RowError("Choix non autorisé dans le mapping validé.")
            return ["L", *value]
        if references is None:
            raise RowError("Index des références absent.")
        ids = []
        for item in value:
            matches = references.get(item, [])
            if len(matches) != 1:
                raise RowError("Référence liée absente ou en doublon dans Grist.")
            ids.append(matches[0])
        return ["L", *ids]
    if kind == "date":
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise RowError("Date ISO sans heure attendue.")
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise RowError("Date invalide.") from exc
        return datetime(parsed.year, parsed.month, parsed.day, tzinfo=timezone.utc).timestamp()
    if kind == "datetime":
        return timestamp(value, field["naive_timezone"])
    raise RowError("Conversion non prise en charge.")


def equal_cell(before: Any, after: Any, grist_type: str) -> bool:
    if isinstance(before, list) and before and before[0] != "L":
        raise RowError("Valeur Grist masquée, en erreur ou non calculée.")
    if grist_type.split(":", 1)[0] in {"ChoiceList", "RefList"}:
        if before == ["L"]:
            before = None
    if isinstance(before, bool) != isinstance(after, bool):
        return False
    return before == after
