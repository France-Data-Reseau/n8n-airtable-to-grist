"""Conservative suggestions: nothing is automatically approved."""

import unicodedata
from typing import Any

from .discovery import Config, DiscoveryError, object_list, object_value, text_value


def normalized_name(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value).casefold()
    return "".join(
        character for character in decomposed
        if character.isalnum() or character in "<>="
    )


def find_table(tables: list[dict[str, Any]], table_id: str, service: str) -> dict[str, Any]:
    matches = [table for table in tables if table.get("id") == table_id]
    if len(matches) != 1:
        raise DiscoveryError(f"{service}: table cible absente ou ambiguë ({table_id}).")
    return matches[0]


def conversion(source_type: str, target_type: str) -> str | None:
    target_base = target_type.split(":", 1)[0]
    text_types = {"singleLineText", "multilineText", "richText", "email", "url", "phoneNumber"}
    if source_type in text_types and target_base == "Text":
        return "text"
    if source_type in {"number", "currency", "percent", "duration", "rating", "autoNumber"}:
        if target_base == "Numeric":
            return "number"
        if target_base == "Int":
            return "integer_checked"
    if source_type == "checkbox" and target_base == "Bool":
        return "boolean"
    if source_type == "singleSelect" and target_base == "Choice":
        return "choice"
    if source_type == "multipleSelects" and target_base == "ChoiceList":
        return "choice_list"
    if source_type == "date" and target_base == "Date":
        return "date"
    if source_type in {"dateTime", "lastModifiedTime", "createdTime"} and target_base == "DateTime":
        return "datetime"
    return None


def build_mapping(config: Config, schema: dict[str, Any]) -> dict[str, Any]:
    airtable_tables = object_list(
        object_value(schema.get("airtable"), "airtable").get("tables"), "airtable.tables"
    )
    grist_tables = object_list(
        object_value(schema.get("grist"), "grist").get("tables"), "grist.tables"
    )
    source = find_table(airtable_tables, config.airtable_table_id, "Airtable")
    target = find_table(grist_tables, config.grist_table_id, "Grist")
    fields = object_list(source.get("fields"), "Airtable fields")
    columns = object_list(target.get("columns"), "Grist columns")
    blockers: list[str] = []
    warnings = [
        f"Vérifier manuellement que {config.modified_field} surveille tous les champs utiles.",
        f"Les changements calculés ou issus de tables liées peuvent échapper à {config.modified_field}.",
        f"L'unicité et le remplissage de {config.key_column} seront vérifiés lors de la simulation.",
    ]
    modified = [field for field in fields if field.get("name") == config.modified_field]
    if len(modified) != 1 or modified[0].get("type") != "lastModifiedTime":
        blockers.append(
            f"Le champ {config.modified_field} doit être un champ Airtable lastModifiedTime unique."
        )
    keys = [column for column in columns if column.get("id") == config.key_column]
    if len(keys) != 1:
        blockers.append(f"Colonne Grist {config.key_column} absente ou ambiguë.")
    else:
        key_metadata = object_value(keys[0].get("fields"), "Grist key fields")
        if key_metadata.get("type") != "Text" or key_metadata.get("isFormula") is not False:
            blockers.append(f"La colonne {config.key_column} doit être de type Text et modifiable.")

    suggestions: list[dict[str, Any]] = []
    for field in fields:
        field_id = text_value(field.get("id"), "Airtable field id")
        name = text_value(field.get("name"), "Airtable field name")
        source_type = text_value(field.get("type"), "Airtable field type")
        name_key = normalized_name(name)
        matches = []
        for column in columns:
            column_id = text_value(column.get("id"), "Grist column id")
            metadata = object_value(column.get("fields"), "Grist column fields")
            label = metadata.get("label", column_id)
            label = text_value(label, "Grist column label")
            if column_id in {config.key_column, "manualSort", "id"}:
                continue
            if name_key and name_key in {normalized_name(column_id), normalized_name(label)}:
                matches.append(column)
        suggestion: dict[str, Any] = {
            "airtable_field_id": field_id,
            "airtable_field_name": name,
            "airtable_type": source_type,
            "grist_column_id": None,
            "grist_type": None,
            "candidates": [column["id"] for column in matches],
            "status": "unmatched" if not matches else "ambiguous",
            "approved": False,
            "conversion": None,
            "notes": [],
        }
        if len(matches) == 1:
            column = matches[0]
            metadata = object_value(column.get("fields"), "Grist column fields")
            target_type = text_value(metadata.get("type"), "Grist column type")
            suggestion.update(grist_column_id=column["id"], grist_type=target_type)
            proposed_conversion = conversion(source_type, target_type)
            writable = metadata.get("isFormula") is False
            suggestion["conversion"] = proposed_conversion if writable else None
            suggestion["status"] = (
                "read_only" if not writable
                else "suggested" if proposed_conversion else "requires_configuration"
            )
            if not writable:
                suggestion["notes"].append("Colonne calculée ou caractère modifiable non confirmé.")
            if metadata.get("formula"):
                suggestion["notes"].append("Examiner aussi la formule Grist et ses déclencheurs.")
            if source_type in {"singleSelect", "multipleSelects"}:
                suggestion["notes"].append("Vérifier les choix autorisés et leur correspondance.")
            if source_type == "multipleRecordLinks" or target_type.startswith(("Ref:", "RefList:")):
                options = object_value(field.get("options", {}), "Airtable field options")
                suggestion["reference"] = {
                    "airtable_linked_table_id": options.get("linkedTableId"),
                    "grist_linked_table_id": (
                        target_type.split(":", 1)[1] if ":" in target_type else None
                    ),
                    "lookup_column": None,
                    "cardinality": None,
                }
                suggestion["notes"].append(
                    "Définir la clé de rapprochement, la cardinalité et la conversion des références."
                )
        suggestions.append(suggestion)

    by_target: dict[str, list[dict[str, Any]]] = {}
    for suggestion in suggestions:
        if suggestion["grist_column_id"] is not None:
            by_target.setdefault(suggestion["grist_column_id"], []).append(suggestion)
    for column_id, duplicates in by_target.items():
        if len(duplicates) > 1:
            for suggestion in duplicates:
                suggestion["status"] = "ambiguous"
                suggestion["conversion"] = None
                suggestion["notes"].append(f"Plusieurs champs Airtable ciblent {column_id}.")

    return {
        "format_version": 1,
        "status": "draft",
        "approved": False,
        "source": {"base_id": config.base_id, "table_id": config.airtable_table_id},
        "target": {
            "server_url": config.grist_url,
            "document_id": config.document_id,
            "table_id": config.grist_table_id,
        },
        "record_key": {
            "source": "airtable_record_id",
            "target_column": config.key_column,
            "approved": False,
        },
        "sync": {
            "interval_minutes": 30,
            "timezone": "Europe/Paris",
            "modified_field": config.modified_field,
            "mode": "incremental_after_initial_full_sync",
            "missing_field": "ignore",
            "explicit_empty": "clear_if_type_allows",
            "delete": False,
            "row_error": "report_skip_and_retry",
            "state_storage": "n8n_data_table",
        },
        "blockers": blockers,
        "warnings": warnings,
        "fields": suggestions,
        "unmapped_grist_columns": [
            column["id"] for column in columns
            if column["id"] not in by_target
            and column["id"] not in {config.key_column, "manualSort", "id"}
        ],
    }
