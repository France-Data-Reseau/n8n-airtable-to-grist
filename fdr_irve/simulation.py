"""Full read-only comparison. This module has no remote write operation."""

import os
import re
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Any, TypeGuard
from urllib.parse import quote, urlencode

from .approved import compile_mapping
from .conversions import RowError, convert, equal_cell
from .discovery import Config, DiscoveryError, JsonClient, discover, object_list, object_value


def read_airtable(client: JsonClient, config: Config, mapping: dict[str, Any]) -> list[dict[str, Any]]:
    parameters = [
        ("pageSize", "100"),
        ("returnFieldsByFieldId", "true"),
        *[("fields[]", field["airtable_field_id"]) for field in mapping["fields"]],
        ("fields[]", mapping["modified_field_id"]),
    ]
    records: list[dict[str, Any]] = []
    seen_offsets: set[str] = set()
    offset = None
    while True:
        query = [*parameters, *([("offset", offset)] if offset is not None else [])]
        path = f"{quote(config.base_id, safe='')}/{quote(config.airtable_table_id, safe='')}"
        payload = client.get(f"{path}?{urlencode(query)}")
        records.extend(object_list(payload.get("records"), "Airtable records"))
        if "offset" not in payload:
            return records
        offset = payload["offset"]
        if not isinstance(offset, str) or not offset or offset in seen_offsets:
            raise DiscoveryError("Pagination Airtable invalide ou répétée.")
        seen_offsets.add(offset)
        time.sleep(0.21)


def read_grist(client: JsonClient, config: Config, table_id: str) -> list[dict[str, Any]]:
    path = f"api/docs/{quote(config.document_id, safe='')}/tables/{quote(table_id, safe='')}/records"
    # Without a limit Grist returns all records; the Airtable offset protocol does not apply.
    return object_list(client.get(path).get("records"), "Grist records")


def record_id(value: Any) -> TypeGuard[str]:
    return isinstance(value, str) and re.fullmatch(r"rec[A-Za-z0-9]+", value) is not None


def index_grist(
    records: list[dict[str, Any]], key_column: str, table_id: str
) -> tuple[dict[str, list[int]], dict[int, dict[str, Any]], list[dict[str, Any]]]:
    index: dict[str, list[int]] = {}
    by_id: dict[int, dict[str, Any]] = {}
    anomalies = []
    for record in records:
        row_id = record.get("id")
        if isinstance(row_id, bool) or not isinstance(row_id, int) or row_id <= 0 or row_id in by_id:
            raise DiscoveryError("Identifiant de ligne Grist absent, invalide ou répété.")
        fields = object_value(record.get("fields"), "Grist record fields")
        by_id[row_id] = record
        key = fields.get(key_column)
        if not record_id(key):
            anomalies.append({
                "table_id": table_id,
                "grist_record_id": row_id,
                "code": "missing_key" if key is None or key == "" else "invalid_key",
            })
            continue
        index.setdefault(key, []).append(row_id)
    for key, ids in index.items():
        if len(ids) > 1:
            anomalies.append({
                "table_id": table_id,
                "airtable_record_id": key,
                "grist_record_ids": ids,
                "code": "duplicate_key",
            })
    return index, by_id, anomalies


def plan(
    mapping: dict[str, Any],
    source: list[dict[str, Any]],
    target: list[dict[str, Any]],
    linked: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    target_index, target_by_id, anomalies = index_grist(
        target, mapping["record_key"]["target_column"], mapping["target"]["table_id"]
    )
    reference_indexes = {}
    for field in mapping["fields"]:
        if field["conversion"] == "reference_list":
            lookup = field["lookup"]
            lookup_key = (lookup["grist_table_id"], lookup["grist_column_id"])
            if lookup_key not in reference_indexes:
                index, _, issues = index_grist(
                    linked[lookup_key[0]], lookup_key[1], lookup_key[0]
                )
                reference_indexes[lookup_key] = index
                anomalies.extend(issues)
    for record in source:
        if not record_id(record.get("id")):
            raise DiscoveryError("Identifiant de record Airtable absent ou invalide.")
        object_value(record.get("fields"), "Airtable record fields")
    source_counts = Counter(record["id"] for record in source)
    creates, updates, errors = [], [], []
    unchanged = 0
    missing_modified = 0
    for record in source:
        key = record["id"]
        fields = record["fields"]
        if mapping["modified_field_id"] not in fields:
            missing_modified += 1
        if source_counts[key] != 1 or len(target_index.get(key, [])) > 1:
            errors.append({"airtable_record_id": key, "code": "ambiguous_record_key"})
            continue
        matches = target_index.get(key, [])
        current = target_by_id[matches[0]] if matches else None
        values: dict[str, Any] = {}
        before: dict[str, Any] = {}
        row_errors = []
        for field in mapping["fields"]:
            source_id = field["airtable_field_id"]
            if source_id not in fields:
                continue
            column_id = field["grist_column_id"]
            references = None
            if field["conversion"] == "reference_list":
                lookup = field["lookup"]
                references = reference_indexes[(lookup["grist_table_id"], lookup["grist_column_id"])]
            try:
                value = convert(field, fields[source_id], references)
                if current is not None:
                    current_errors = object_value(current.get("errors", {}), "Grist record errors")
                    if column_id in current_errors or column_id not in current["fields"]:
                        raise RowError("Colonne Grist inaccessible, absente ou en erreur.")
                    old = current["fields"][column_id]
                    if equal_cell(old, value, field["grist_type"]):
                        continue
                    before[column_id] = old
                values[column_id] = value
            except RowError as exc:
                row_errors.append({"airtable_field_id": source_id, "message": str(exc)})
        if row_errors:
            errors.append({
                "airtable_record_id": key,
                "code": "invalid_row",
                "fields": row_errors,
            })
        elif current is None:
            creates.append({
                "airtable_record_id": key,
                "fields": {mapping["record_key"]["target_column"]: key, **values},
            })
        elif values:
            updates.append({
                "airtable_record_id": key,
                "id": current["id"],
                "fields": values,
                "before": before,
            })
        else:
            unchanged += 1
    retry_ids = sorted({error["airtable_record_id"] for error in errors})
    return {
        "mode": "read_only_full_simulation",
        "remote_writes": 0,
        "cursor_advanced": False,
        "summary": {
            "airtable_records": len(source),
            "grist_records": len(target),
            "mapped_fields": len(mapping["fields"]),
            "creates": len(creates),
            "updates": len(updates),
            "unchanged": unchanged,
            "skipped": len(errors),
            "changed_cells": sum(len(item["fields"]) for item in updates),
            "grist_key_anomalies": len(anomalies),
            "missing_modified_timestamp": missing_modified,
        },
        "creates": creates,
        "updates": updates,
        "errors": errors,
        "grist_key_anomalies": anomalies,
        "retry_airtable_ids": retry_ids,
        "warnings": [
            "Lecture séquentielle, sans instantané transactionnel entre les deux bases.",
            "Une clé vide dans Grist n'est pas rapprochée automatiquement et reste inchangée.",
            "Les erreurs de colonnes calculées exclues ne sont pas auditées par cette simulation.",
            "Le suivi incrémental et la persistance des reprises n8n ne sont pas encore exécutés.",
        ],
    }


def simulate(
    config: Config,
    schema: dict[str, Any],
    proposal: dict[str, Any],
    decisions: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    mapping = compile_mapping(config, schema, proposal, decisions)
    started_at = datetime.now(timezone.utc).isoformat()
    live_schema = discover(config)
    live_mapping = compile_mapping(config, live_schema, proposal, decisions)
    if mapping != live_mapping:
        raise DiscoveryError("Le schéma a changé depuis la validation ; nouvelle revue requise.")
    airtable = JsonClient("https://api.airtable.com/v0", os.environ.get("AIRTABLE_TOKEN", ""), "Airtable")
    grist = JsonClient(config.grist_url, os.environ.get("GRIST_TOKEN", ""), "Grist")
    source = read_airtable(airtable, config, mapping)
    target = read_grist(grist, config, config.grist_table_id)
    linked = {}
    for field in mapping["fields"]:
        if field["conversion"] == "reference_list":
            table_id = field["lookup"]["grist_table_id"]
            if table_id not in linked:
                linked[table_id] = read_grist(grist, config, table_id)
    report = plan(mapping, source, target, linked)
    report["started_at"] = started_at
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["schema_fingerprint"] = mapping["schema_fingerprint"]
    return mapping, report
