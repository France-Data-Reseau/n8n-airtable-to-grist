"""Pure steps embedded in native Python Code nodes by the workflow exporter."""

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from .conversions import RowError, equal_cell, timestamp
from .discovery import DiscoveryError, object_list, object_value
from .simulation import index_grist, plan, record_id


def schema_contract(mapping: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    source_id = mapping["source"]["table_id"]
    sources = [table for table in schema["airtable"]["tables"] if table["id"] == source_id]
    if len(sources) != 1:
        raise DiscoveryError("La table Airtable du mapping est absente ou ambiguë.")
    wanted_source = {
        mapping["modified_field_id"],
        *[field["airtable_field_id"] for field in mapping["fields"]],
    }
    source_fields = {
        field["id"]: field for field in sources[0]["fields"] if field["id"] in wanted_source
    }
    if set(source_fields) != wanted_source:
        raise DiscoveryError("Un champ Airtable du mapping est absent.")
    target_id = mapping["target"]["table_id"]
    wanted_columns: dict[str, set[str]] = {
        target_id: {
            mapping["record_key"]["target_column"],
            *[field["grist_column_id"] for field in mapping["fields"]],
        }
    }
    for field in mapping["fields"]:
        if field["conversion"] == "reference_list":
            lookup = field["lookup"]
            wanted_columns.setdefault(lookup["grist_table_id"], set()).add(lookup["grist_column_id"])
    targets = {}
    for table_id, column_ids in wanted_columns.items():
        tables = [table for table in schema["grist"]["tables"] if table["id"] == table_id]
        if len(tables) != 1:
            raise DiscoveryError("Une table Grist du mapping est absente ou ambiguë.")
        columns = {
            column["id"]: column for column in tables[0]["columns"] if column["id"] in column_ids
        }
        if set(columns) != column_ids:
            raise DiscoveryError("Une colonne Grist du mapping est absente.")
        targets[table_id] = columns
    return {"airtable_fields": source_fields, "grist_columns": targets}


def prepare(context: dict[str, Any]) -> dict[str, Any]:
    config = object_value(context.get("config"), "config")
    mode = config.get("mode")
    if mode not in {"simulation", "write"}:
        raise DiscoveryError("Mode inconnu : simulation ou write requis.")
    if mode == "write" and (
        config.get("write_confirmation") != "WRITE_AIRTABLE_TO_GRIST"
        or config.get("serialized_executions_confirmed") is not True
        or config.get("modified_field_coverage_confirmed") is not True
    ):
        raise DiscoveryError(
            "Écriture refusée : confirmation, sérialisation et couverture de updatedat requises."
        )
    if not isinstance(config.get("force_full_scan"), bool):
        raise DiscoveryError("force_full_scan doit être un booléen.")
    overlap = config.get("overlap_seconds")
    if isinstance(overlap, bool) or not isinstance(overlap, int) or overlap < 120:
        raise DiscoveryError("La fenêtre de recouvrement doit être d'au moins 120 secondes.")
    rows = object_list(context.get("state_rows"), "state_rows")
    rows = [row for row in rows if row]
    if len(rows) != 1 or rows[0].get("sync_key") != config["state_key"]:
        raise DiscoveryError("Créer exactement une ligne d'état pour cette synchronisation.")
    row = rows[0]
    if not isinstance(row.get("id"), (str, int)) or isinstance(row.get("id"), bool) or not row["id"]:
        raise DiscoveryError("Identifiant de ligne d'état invalide.")
    try:
        state = object_value(json.loads(row["payload"]), "state.payload")
    except (ValueError, TypeError, KeyError) as exc:
        raise DiscoveryError("L'état n8n doit être un document JSON valide.") from exc
    cursor = state.get("cursor")
    retries = state.get("retry_ids")
    if (
        state.get("version") != 1
        or isinstance(state.get("version"), bool)
        or "cursor" not in state
        or not isinstance(retries, list)
        or not all(record_id(item) for item in retries)
        or len(set(retries)) != len(retries)
    ):
        raise DiscoveryError("Version ou liste de reprise de l'état invalide.")
    fingerprint = state.get("mapping_fingerprint")
    bootstrap = cursor is None and retries == [] and fingerprint == ""
    if not bootstrap and fingerprint != config["mapping_fingerprint"]:
        raise DiscoveryError("Le mapping a changé : réinitialisation contrôlée de l'état requise.")
    started_at = context.get("started_at") or datetime.now(timezone.utc).isoformat()
    start_seconds = timestamp(started_at, "UTC")
    formula = None
    if cursor is not None:
        if not isinstance(cursor, str) or not cursor.endswith(("+00:00", "Z")):
            raise DiscoveryError("Le curseur doit être un horodatage UTC explicite.")
        cursor_seconds = timestamp(cursor, "UTC")
        if cursor_seconds > start_seconds:
            raise DiscoveryError("Le curseur est dans le futur ; vérifier l'horloge et l'état.")
        cutoff = datetime.fromtimestamp(cursor_seconds, timezone.utc) - timedelta(seconds=overlap)
        cutoff_iso = cutoff.isoformat()
        modified_id = config["mapping"]["modified_field_id"]
        terms = [
            f"IS_AFTER({{{modified_id}}}, DATETIME_PARSE('{cutoff_iso}'))",
            f"IS_AFTER(CREATED_TIME(), DATETIME_PARSE('{cutoff_iso}'))",
            *[f"RECORD_ID()='{item}'" for item in retries],
        ]
        if not config["force_full_scan"]:
            formula = "OR(" + ",".join(terms) + ")"
    result = {
        **context,
        "state_row_id": row["id"],
        "state": state,
        "started_at": started_at,
        "filter_formula": formula,
        "read_mode": "incremental" if formula else "full",
    }
    result.pop("state_rows", None)
    return result


def validate_schema(context: dict[str, Any]) -> dict[str, Any]:
    config = context["config"]
    source = object_value(context.get("source_schema"), "source_schema")
    tables = object_list(source.get("tables"), "Airtable tables")
    target_schemas = object_value(context.get("target_schemas"), "target_schemas")
    targets = [
        {
            "id": table_id,
            "columns": object_list(payload.get("columns"), "Grist columns"),
        }
        for table_id, payload in target_schemas.items()
    ]
    live = {"airtable": {"tables": tables}, "grist": {"tables": targets}}
    if schema_contract(config["mapping"], live) != config["schema_contract"]:
        raise DiscoveryError("Le schéma a changé depuis la validation ; nouvelle revue requise.")
    result = dict(context)
    result.pop("source_schema", None)
    result.pop("target_schemas", None)
    return result


def prepare_plan(context: dict[str, Any]) -> dict[str, Any]:
    pages = object_list(context.get("airtable_pages"), "airtable_pages")
    if not pages or pages[-1].get("offset") is not None:
        raise DiscoveryError("Pagination Airtable incomplète ; aucune écriture autorisée.")
    source = []
    seen_offsets = set()
    for page in pages:
        offset = page.get("offset")
        if offset is not None:
            if not isinstance(offset, str) or not offset or offset in seen_offsets:
                raise DiscoveryError("Pagination Airtable invalide ou répétée.")
            seen_offsets.add(offset)
        source.extend(object_list(page.get("records"), "Airtable records"))
    mapping = context["config"]["mapping"]
    target = object_list(context["target_records"].get("records"), "Grist records")
    linked = {
        table_id: object_list(payload.get("records"), "Grist linked records")
        for table_id, payload in context["linked_records"].items()
    }
    report = plan(mapping, source, target, linked)
    missing_retries = sorted(set(context["state"]["retry_ids"]) - {row["id"] for row in source})
    for key in missing_retries:
        report["errors"].append({"airtable_record_id": key, "code": "retry_record_not_returned"})
    report["retry_airtable_ids"] = sorted({
        *report["retry_airtable_ids"], *missing_retries,
    })
    report["warnings"] = [
        "Les colonnes calculées exclues et la table AODE ne sont jamais écrites.",
        "Les champs absents ne sont pas effacés ; aucune suppression n'est effectuée.",
        "Lecture séquentielle sans instantané transactionnel entre les deux bases.",
    ]
    operations = []
    for action, rows in (("create", report["creates"]), ("update", report["updates"])):
        for row in rows:
            values = dict(row["fields"])
            values.pop(mapping["record_key"]["target_column"], None)
            operations.append({
                "action": action,
                "airtable_record_id": row["airtable_record_id"],
                "fields": values,
                "body": {"records": [{
                    "require": {mapping["record_key"]["target_column"]: row["airtable_record_id"]},
                    "fields": values,
                }]},
            })
    pending = sorted({
        *context["state"]["retry_ids"],
        *report["retry_airtable_ids"],
        *[operation["airtable_record_id"] for operation in operations],
    })
    checkpoint = {
        "version": 1,
        "mapping_fingerprint": context["config"]["mapping_fingerprint"],
        "cursor": context["state"]["cursor"],
        "retry_ids": pending,
        "pending_since": context["started_at"],
    }
    return {
        key: value for key, value in {
            **context,
            "report": report,
            "operations": operations,
            "checkpoint_payload": json.dumps(checkpoint, ensure_ascii=False),
        }.items()
        if key not in {"airtable_pages", "target_records", "linked_records"}
    }


def finalize(context: dict[str, Any]) -> dict[str, Any]:
    if context["config"]["mode"] != "write":
        raise DiscoveryError("La simulation ne doit pas passer par la finalisation d'écriture.")
    report = context["report"]
    operations = context["operations"]
    results = object_list(context.get("write_results"), "write_results")
    if len(operations) != len(results):
        raise DiscoveryError("Résultats d'écriture incomplets ; conserver le checkpoint de reprise.")
    mapping = context["config"]["mapping"]
    target = object_list(context["verification_records"].get("records"), "verification_records")
    index, by_id, anomalies = index_grist(
        target, mapping["record_key"]["target_column"], mapping["target"]["table_id"]
    )
    fields = {field["grist_column_id"]: field for field in mapping["fields"]}
    verified = 0
    write_errors = []
    for operation, response in zip(operations, results):
        key = operation["airtable_record_id"]
        status = response.get("status_code")
        if (
            response.get("airtable_record_id") != key
            or isinstance(status, bool)
            or not isinstance(status, int)
            or not 200 <= status < 300
        ):
            write_errors.append({
                "airtable_record_id": key, "code": "write_http_error", "status_code": status,
            })
            continue
        matches = index.get(key, [])
        if len(matches) != 1:
            write_errors.append({"airtable_record_id": key, "code": "write_key_not_unique"})
            continue
        current = by_id[matches[0]]
        try:
            for column_id, expected in operation["fields"].items():
                if (
                    column_id in current.get("errors", {})
                    or column_id not in current["fields"]
                    or not equal_cell(
                        current["fields"][column_id], expected, fields[column_id]["grist_type"]
                    )
                ):
                    raise RowError("La valeur enregistrée ne correspond pas au plan.")
        except RowError:
            write_errors.append({"airtable_record_id": key, "code": "write_verification_failed"})
        else:
            verified += 1
    retries = sorted({
        *report["retry_airtable_ids"],
        *[error["airtable_record_id"] for error in write_errors],
    })
    report["write_errors"] = write_errors
    report["summary"].update(
        verified_writes=verified,
        write_failures=len(write_errors),
        pending_retries=len(retries),
        grist_key_anomalies=len(anomalies) + sum(
            1 for issue in report["grist_key_anomalies"]
            if issue["table_id"] != mapping["target"]["table_id"]
        ),
    )
    final_state = {
        "version": 1,
        "mapping_fingerprint": context["config"]["mapping_fingerprint"],
        "cursor": context["started_at"],
        "retry_ids": retries,
        "last_summary": report["summary"],
    }
    return {**context, "final_payload": json.dumps(final_state, ensure_ascii=False)}


def summary(context: dict[str, Any]) -> dict[str, Any]:
    report = context["report"]
    counts = dict(report["summary"])
    counts["pending_retries"] = (
        counts.get("pending_retries", len(report["retry_airtable_ids"]))
    )
    has_anomalies = bool(
        report["errors"]
        or report["grist_key_anomalies"]
        or report.get("write_errors")
        or counts.get("grist_key_anomalies")
    )
    return {
        "mode": context["config"]["mode"],
        "read_mode": context["read_mode"],
        "summary": counts,
        "has_anomalies": has_anomalies,
        "cursor_advanced": context["config"]["mode"] == "write",
        "warnings": report["warnings"],
    }
