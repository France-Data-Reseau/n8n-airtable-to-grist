"""Compile explicit human decisions against the reviewed schema."""

import hashlib
import json
from pathlib import Path
from typing import Any

from .discovery import Config, DiscoveryError, object_list, object_value, text_value
from .mapping import build_mapping, find_table


def load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as stream:
            return object_value(json.load(stream), "Document JSON")
    except (ValueError, UnicodeError) as exc:
        raise DiscoveryError("Document JSON local invalide.") from exc


def compile_mapping(
    config: Config,
    schema: dict[str, Any],
    proposal: dict[str, Any],
    decisions: dict[str, Any],
) -> dict[str, Any]:
    if (
        decisions.get("approved") is not True
        or decisions.get("authorization") != "read_only_simulation"
        or decisions.get("other_suggested_fields") != "approved"
    ):
        raise DiscoveryError("Validation globale du mapping requise avant la simulation.")
    if build_mapping(config, schema) != proposal:
        raise DiscoveryError("La proposition ne correspond plus au schéma ou à la configuration.")
    if proposal["blockers"]:
        raise DiscoveryError("Le mapping comporte encore des prérequis bloquants.")
    suggested = [field for field in proposal["fields"] if field["status"] == "suggested"]
    if len(suggested) != decisions.get("expected_suggested_fields"):
        raise DiscoveryError("Le nombre de champs diffère du périmètre validé.")

    calculated = object_value(decisions.get("calculated_fields"), "calculated_fields")
    preserved = calculated.get("preserve_existing_formulas")
    excluded = calculated.get("exclude_without_modification")
    if not isinstance(preserved, list) or not isinstance(excluded, list):
        raise DiscoveryError("Décisions des colonnes calculées incomplètes.")
    reviewed_calculated = preserved + excluded
    expected_calculated = [
        field["grist_column_id"] for field in proposal["fields"] if field["status"] == "read_only"
    ]
    if (
        calculated.get("approved") is not True
        or calculated.get("write") is not False
        or not all(isinstance(item, str) for item in reviewed_calculated)
        or sorted(reviewed_calculated) != sorted(expected_calculated)
    ):
        raise DiscoveryError("Les exclusions des colonnes calculées doivent être confirmées.")

    source = find_table(schema["airtable"]["tables"], config.airtable_table_id, "Airtable")
    target = find_table(schema["grist"]["tables"], config.grist_table_id, "Grist")
    source_fields = {field["id"]: field for field in source["fields"]}
    columns = {column["id"]: column for column in target["columns"]}
    overrides = object_list(decisions.get("field_decisions"), "field_decisions")
    overrides_by_id = {entry.get("airtable_field_id"): entry for entry in overrides}
    if len(overrides_by_id) != len(overrides):
        raise DiscoveryError("Décisions de champ en doublon.")
    known_ids = {field["airtable_field_id"] for field in proposal["fields"]}
    if set(overrides_by_id) - known_ids:
        raise DiscoveryError("Une décision cible un champ inconnu.")

    result_fields = []
    contract: list[Any] = [columns[config.key_column]]
    for field in proposal["fields"]:
        source_id = field["airtable_field_id"]
        decision = overrides_by_id.get(source_id)
        if decision is not None and (
            decision.get("approved") is not True
            or decision.get("grist_column_id") != field["grist_column_id"]
        ):
            raise DiscoveryError("Une décision de champ est incohérente ou non validée.")
        if field["status"] != "suggested" and decision is None:
            if field["status"] in {"ambiguous", "requires_configuration"}:
                raise DiscoveryError("Une correspondance nécessite encore une décision.")
            continue
        if field["status"] not in {"suggested", "requires_configuration"}:
            if decision is not None:
                raise DiscoveryError("Une décision tenterait d'inclure un champ exclu.")
            continue
        source_field = source_fields[source_id]
        column = columns[field["grist_column_id"]]
        entry = {
            "airtable_field_id": source_id,
            "airtable_field_name": field["airtable_field_name"],
            "grist_column_id": column["id"],
            "airtable_type": field["airtable_type"],
            "grist_type": field["grist_type"],
            "conversion": field["conversion"],
            "approved": True,
        }
        if field["airtable_type"] == "richText":
            if decision is None or decision.get("preserve_markdown") is not True:
                raise DiscoveryError("Le traitement du texte enrichi doit être confirmé.")
            entry["preserve_markdown"] = True
        if field["conversion"] == "datetime":
            if decision is None or decision.get("preserve_instant") is not True:
                raise DiscoveryError("Le traitement du fuseau horaire doit être confirmé.")
            entry["naive_timezone"] = decision["naive_timezone"]
        if field["airtable_type"] in {"singleSelect", "multipleSelects"}:
            options = source_field.get("options", {})
            choices = [choice["name"] for choice in options.get("choices", [])]
            try:
                widget = json.loads(column["fields"].get("widgetOptions") or "{}")
            except ValueError as exc:
                raise DiscoveryError("Options Grist invalides pour une sélection.") from exc
            if set(choices) != set(object_value(widget, "widgetOptions").get("choices", [])):
                raise DiscoveryError("Les choix Airtable et Grist divergent ; nouvelle revue requise.")
            entry["choices"] = choices
        if field["airtable_type"] == "multipleRecordLinks":
            if decision is None or decision.get("conversion") != "reference_list":
                raise DiscoveryError("La résolution des références doit être validée.")
            lookup = object_value(decision.get("lookup"), "lookup")
            linked_table_id = text_value(lookup.get("grist_table_id"), "lookup.grist_table_id")
            if (
                field["grist_type"] != f"RefList:{linked_table_id}"
                or lookup.get("write_linked_table") is not False
                or lookup.get("on_missing_or_duplicate") != "report_skip_and_retry"
            ):
                raise DiscoveryError("La règle de référence ne correspond pas au périmètre validé.")
            linked_table = find_table(schema["grist"]["tables"], linked_table_id, "Grist")
            key = next(
                (item for item in linked_table["columns"]
                 if item["id"] == lookup.get("grist_column_id")),
                None,
            )
            if (
                key is None
                or key["fields"].get("type") != "Text"
                or key["fields"].get("isFormula") is not False
            ):
                raise DiscoveryError("La clé de référence liée doit être un champ Text modifiable.")
            contract.append(key)
            entry["conversion"] = "reference_list"
            entry["lookup"] = lookup
        if entry["conversion"] is None:
            raise DiscoveryError("Conversion encore non définie pour un champ retenu.")
        result_fields.append(entry)
        contract.append({"source": source_field, "target": column})
    modified = next(field for field in source["fields"] if field["name"] == config.modified_field)
    contract.append(modified)
    fingerprint = hashlib.sha256(
        json.dumps(contract, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return {
        "format_version": 1,
        "approved": True,
        "authorization": "read_only_simulation",
        "source": proposal["source"],
        "target": proposal["target"],
        "record_key": {**proposal["record_key"], "approved": True},
        "sync": proposal["sync"],
        "modified_field_id": modified["id"],
        "schema_fingerprint": fingerprint,
        "fields": result_fields,
        "calculated_fields": calculated,
    }
