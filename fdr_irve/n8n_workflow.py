"""Generate a credential-free n8n export from the reviewed mapping."""

import ast
import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from .discovery import DiscoveryError
from .n8n_runtime import schema_contract


def python_code(entry: str) -> str:
    definitions = {}
    for filename in ("discovery.py", "conversions.py", "simulation.py", "n8n_runtime.py"):
        source = (Path(__file__).parent / filename).read_text(encoding="utf-8")
        for node in ast.parse(source).body:
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                definitions[node.name] = node
    needed: set[str] = set()

    def collect(name: str) -> None:
        if name in needed or name not in definitions:
            return
        needed.add(name)
        for child in ast.walk(definitions[name]):
            if isinstance(child, ast.Name):
                collect(child.id)

    collect(entry)
    if entry not in needed:
        raise DiscoveryError("Étape Python n8n inconnue.")
    parts = [
        "import json\nimport math\nimport re\nfrom collections import Counter\n"
        "from datetime import date, datetime, timedelta, timezone\n"
        "from typing import Any, TypeGuard\nfrom zoneinfo import ZoneInfo, ZoneInfoNotFoundError"
    ]
    for name in sorted(needed):
        if name in {"DiscoveryError", "RowError"}:
            # Native Python denies __build_class__; use built-in exception markers.
            parts.append(f"{name} = {'RuntimeError' if name == 'DiscoveryError' else 'ValueError'}")
        else:
            parts.append(ast.unparse(definitions[name]))
    parts.append(f'return [{{"json": {entry}(_items[0]["json"])}}]')
    return "\n\n".join(parts) + "\n"


def build_workflow(mapping: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    if mapping.get("approved") is not True:
        raise DiscoveryError("Mapping approuvé requis pour l'export n8n.")
    lookups = {
        field["lookup"]["grist_table_id"]
        for field in mapping["fields"] if field["conversion"] == "reference_list"
    }
    if lookups != {"AODE"}:
        raise DiscoveryError("Ce workflow couvre uniquement la référence AODE validée.")
    fingerprint = hashlib.sha256(
        json.dumps(mapping, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    state_key = ":".join([
        mapping["source"]["base_id"], mapping["source"]["table_id"],
        mapping["target"]["document_id"], mapping["target"]["table_id"],
    ])
    settings = {
        "mode": "simulation",
        "write_confirmation": "",
        "serialized_executions_confirmed": False,
        "modified_field_coverage_confirmed": False,
        "force_full_scan": False,
        "overlap_seconds": 120,
        "state_table_id": "REPLACE_WITH_STATE_TABLE_ID",
        "state_key": state_key,
        "mapping_fingerprint": fingerprint,
        "mapping": mapping,
        "schema_contract": schema_contract(mapping, schema),
    }
    nodes: list[dict[str, Any]] = []
    connections: dict[str, Any] = {}

    def node(name: str, kind: str, version: float, parameters: dict[str, Any], **extra) -> str:
        index = len(nodes)
        nodes.append({
            "id": str(uuid5(NAMESPACE_URL, f"fdr-irve/{name}")),
            "name": name,
            "type": f"n8n-nodes-base.{kind}",
            "typeVersion": version,
            "position": [(index % 8) * 260, (index // 8) * 320],
            "parameters": parameters,
            **extra,
        })
        return name

    def connect(start: str, end: str, output: int = 0) -> None:
        main = connections.setdefault(start, {"main": []})["main"]
        while len(main) <= output:
            main.append([])
        main[output].append({"node": end, "type": "main", "index": 0})

    def js(name: str, code: str) -> str:
        return node(name, "code", 2, {"mode": "runOnceForAllItems", "jsCode": code})

    def py(name: str, entry: str) -> str:
        return node(name, "code", 2, {
            "mode": "runOnceForAllItems", "language": "pythonNative", "pythonCode": python_code(entry),
        })

    def bridge(name: str, previous: str, expression: str) -> str:
        return js(
            name,
            f"return [{{json: {{...$('{previous}').first().json, {expression}}}}}];",
        )

    def condition(name: str, expression: str) -> str:
        return node(name, "if", 2.2, {
            "conditions": {
                "options": {
                    "caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2,
                },
                "conditions": [{
                    "id": str(uuid5(NAMESPACE_URL, name)),
                    "leftValue": expression,
                    "rightValue": True,
                    "operator": {"type": "boolean", "operation": "true", "singleValue": True},
                }],
                "combinator": "and",
            },
            "options": {},
        })

    def http(name: str, url: str, service: str, write: bool = False) -> str:
        result = node(name, "httpRequest", 4.2, {
            "method": "PUT" if write else "GET",
            "url": url,
            "authentication": "genericCredentialType",
            "genericAuthType": "httpHeaderAuth",
            "options": {
                "timeout": 30000,
                "redirect": {"redirect": {"followRedirects": False}},
                "response": {"response": {
                    "responseFormat": "text" if write else "json",
                    "fullResponse": write,
                    "neverError": write,
                }},
            },
        }, notes=f"Choisir le credential Header Auth {service} : Authorization = Bearer <token>.",
            notesInFlow=True)
        if write:
            nodes[-1].update(onError="continueRegularOutput")
            nodes[-1]["parameters"].update(
                sendBody=True, contentType="json", specifyBody="json", jsonBody="={{ $json.body }}",
            )
            nodes[-1]["parameters"]["options"]["batching"] = {
                "batch": {"batchSize": 1, "batchInterval": 250}
            }
        else:
            nodes[-1].update(retryOnFail=True, maxTries=3, waitBetweenTries=2000)
        return result

    def state_node(name: str, operation: str, payload: str | None = None) -> str:
        parameters: dict[str, Any] = {
            "resource": "row",
            "operation": operation,
            "dataTableId": {
                "__rl": True, "mode": "id",
                "value": "={{ $('Configuration').first().json.state_table_id }}",
            },
            "matchType": "allConditions",
            "filters": {"conditions": [{
                "keyName": "sync_key" if operation == "get" else "id",
                "condition": "eq",
                "keyValue": (
                    "={{ $('Configuration').first().json.state_key }}" if operation == "get"
                    else "={{ $('Plan').first().json.state_row_id }}"
                ),
            }]},
        }
        if operation == "get":
            parameters["returnAll"] = True
        else:
            parameters["columns"] = {
                "mappingMode": "defineBelow",
                "value": {"payload": payload},
                "matchingColumns": [],
                "schema": [{
                    "id": "payload", "displayName": "payload", "required": False,
                    "defaultMatch": False, "display": True, "type": "string",
                    "canBeUsedToMatch": True,
                }],
                "attemptToConvertTypes": False,
                "convertFieldsToString": False,
            }
            parameters["options"] = {}
        return node(name, "dataTable", 1, parameters, alwaysOutputData=True)

    def saved_state(name: str, previous: str, key: str) -> str:
        return js(name, f"""
const context = $('{previous}').first().json;
const rows = $input.all().map(item => item.json);
if (rows.length !== 1 || String(rows[0].id) !== String(context.state_row_id) ||
    rows[0].payload !== context.{key}) {{
  throw new Error('Etat non sauvegarde : execution interrompue, curseur non confirme.');
}}
return [{{json: context}}];
""".strip())

    manual = node("Manuel", "manualTrigger", 1, {})
    schedule = node("Toutes les 30 minutes", "scheduleTrigger", 1.2, {
        "rule": {"interval": [{"field": "minutes", "minutesInterval": 30}]},
    })
    config = js("Configuration", (
        "const config = " + json.dumps(settings, ensure_ascii=False, indent=2) + ";\n"
        "if (config.state_table_id === 'REPLACE_WITH_STATE_TABLE_ID') {\n"
        "  throw new Error('Renseigner state_table_id apres creation de la Data Table.');\n"
        "}\nreturn [{json: config}];"
    ))
    get_state = state_node("Lire etat", "get")
    state_context = js("Assembler etat", (
        "return [{json: {config: $('Configuration').first().json,"
        " state_rows: $input.all().map(item => item.json)}}];"
    ))
    prepare_node = py("Preparer execution", "prepare")
    airtable_base = "https://api.airtable.com/v0/" + mapping["source"]["base_id"]
    grist_base = (
        mapping["target"]["server_url"] + "/api/docs/" + mapping["target"]["document_id"]
        + "/tables/"
    )
    source_schema = http(
        "Schema Airtable",
        "https://api.airtable.com/v0/meta/bases/" + mapping["source"]["base_id"] + "/tables",
        "Airtable",
    )
    source_bridge = bridge(
        "Conserver schema Airtable", prepare_node, "source_schema: $input.first().json",
    )
    target_schema = http(
        "Schema Grist enquete", grist_base + mapping["target"]["table_id"] + "/columns", "Grist",
    )
    target_bridge = bridge(
        "Conserver schema enquete", source_bridge,
        "target_schemas: {" + json.dumps(mapping["target"]["table_id"]) + ": $input.first().json}",
    )
    linked_schema = http("Schema Grist AODE", grist_base + "AODE/columns", "Grist")
    schema_bridge = bridge(
        "Assembler schemas", target_bridge,
        f"target_schemas: {{...$('{target_bridge}').first().json.target_schemas, "
        "AODE: $input.first().json}",
    )
    validate = py("Valider schemas", "validate_schema")
    source_records = http(
        "Lire Airtable",
        "={{ (() => { const m = $json.config.mapping; "
        "const q = [['pageSize', '100'], ['returnFieldsByFieldId', 'true'],"
        " ...m.fields.map(f => ['fields[]', f.airtable_field_id]),"
        " ['fields[]', m.modified_field_id]];"
        " if ($json.filter_formula) q.push(['filterByFormula', $json.filter_formula]);"
        " const url = " + json.dumps(airtable_base + "/" + mapping["source"]["table_id"])
        + " + '?' + q.map(([k,v]) => encodeURIComponent(k) + '=' + encodeURIComponent(v)).join('&');"
        " if (url.length > 15000) throw new Error('Requete trop longue : ne pas perdre les reprises.');"
        " return url; })() }}",
        "Airtable",
    )
    nodes[-1]["parameters"]["options"]["pagination"] = {"pagination": {
        "paginationMode": "updateAParameterInEachRequest",
        "parameters": {"parameters": [{
            "type": "qs", "name": "offset", "value": "={{ $response.body.offset }}",
        }]},
        "paginationCompleteWhen": "other",
        "completeExpression": "={{ !$response.body.offset }}",
        "limitPagesFetched": True,
        "maxRequests": 1000,
        "requestInterval": 250,
    }}
    source_data_bridge = bridge(
        "Conserver pages Airtable", validate, "airtable_pages: $input.all().map(item => item.json)",
    )
    target_records = http(
        "Lire Grist enquete", grist_base + mapping["target"]["table_id"] + "/records", "Grist",
    )
    target_data_bridge = bridge(
        "Conserver enquete", source_data_bridge, "target_records: $input.first().json",
    )
    linked_records = http("Lire Grist AODE", grist_base + "AODE/records", "Grist")
    data_bridge = bridge(
        "Assembler donnees", target_data_bridge,
        "linked_records: {AODE: $input.first().json}",
    )
    plan_node = py("Plan", "prepare_plan")
    dry_gate = condition("Simulation ?", "={{ $json.config.mode === 'simulation' }}")
    checkpoint = state_node("Checkpoint avant ecriture", "update", "={{ $json.checkpoint_payload }}")
    checkpoint_checked = saved_state(
        "Verifier checkpoint", plan_node, "checkpoint_payload",
    )
    operations_gate = condition("Operations ?", "={{ $json.operations.length > 0 }}")
    expand = js("Operations", (
        "return $input.first().json.operations.map((op, index) => "
        "({json: {...op, operation_index: index}, pairedItem: {item: 0}}));"
    ))
    write_url = grist_base + mapping["target"]["table_id"] + "/records"
    write = http(
        "Ecrire Grist",
        "={{ " + json.dumps(write_url)
        + " + '?noparse=true&onmany=none&allow_empty_require=false&noadd='"
        " + ($json.action === 'update' ? 'true' : 'false')"
        " + '&noupdate=' + ($json.action === 'create' ? 'true' : 'false') }}",
        "Grist", write=True,
    )
    results = js("Assembler resultats", """
const operations = $('Operations').all();
const responses = $input.all();
if (operations.length !== responses.length) {
  throw new Error('Reponses incompletes : checkpoint de reprise conserve.');
}
const write_results = responses.map((item, index) => {
  const pair = Array.isArray(item.pairedItem) ? item.pairedItem[0] : item.pairedItem;
  const operationIndex = typeof pair === 'number' ? pair : pair?.item;
  if (operationIndex !== index) {
    throw new Error('Ordre des reponses inconnu : checkpoint de reprise conserve.');
  }
  return {
    airtable_record_id: operations[index].json.airtable_record_id,
    status_code: Number.isInteger(item.json.statusCode) ? item.json.statusCode : null
  };
});
return [{json: {...$('Plan').first().json, write_results}}];
""".strip())
    no_operations = bridge("Aucune operation", plan_node, "write_results: []")
    verify = http(
        "Relire Grist apres ecriture", write_url, "Grist",
    )
    verification_bridge = js("Assembler verification", """
const base = $('Plan').first().json;
const write_results = $('Operations ?').first().json.operations.length > 0
  ? $('Assembler resultats').first().json.write_results : [];
return [{json: {...base, write_results, verification_records: $input.first().json}}];
""".strip())
    finalize_node = py("Finaliser etat", "finalize")
    save_final = state_node("Sauver curseur et reprises", "update", "={{ $json.final_payload }}")
    final_checked = saved_state("Verifier etat final", finalize_node, "final_payload")
    summary_node = py("Bilan", "summary")
    anomaly_gate = condition("Anomalies ?", "={{ $json.has_anomalies }}")
    stop = node("Signaler les anomalies", "stopAndError", 1, {
        "errorType": "errorMessage",
        "errorMessage": "={{ 'Synchronisation avec anomalies : ' + JSON.stringify($json.summary) }}",
    })
    success = node("Termine", "noOp", 1, {})
    chain = [
        config, get_state, state_context, prepare_node, source_schema, source_bridge,
        target_schema, target_bridge, linked_schema, schema_bridge, validate,
        source_records, source_data_bridge, target_records, target_data_bridge,
        linked_records, data_bridge, plan_node, dry_gate,
    ]
    connect(manual, config)
    connect(schedule, config)
    for start, end in zip(chain, chain[1:]):
        connect(start, end)
    connect(dry_gate, summary_node, 0)
    connect(dry_gate, checkpoint, 1)
    connect(checkpoint, checkpoint_checked)
    connect(checkpoint_checked, operations_gate)
    connect(operations_gate, expand, 0)
    connect(operations_gate, no_operations, 1)
    connect(expand, write)
    connect(write, results)
    connect(results, verify)
    connect(no_operations, verify)
    for start, end in zip(
        [verify, verification_bridge, finalize_node, save_final, final_checked, summary_node],
        [verification_bridge, finalize_node, save_final, final_checked, summary_node, anomaly_gate],
    ):
        connect(start, end)
    connect(anomaly_gate, stop, 0)
    connect(anomaly_gate, success, 1)
    return {
        "name": "IRVE - Airtable vers Grist (simulation par defaut)",
        "active": False,
        "nodes": nodes,
        "connections": connections,
        "settings": {
            "executionOrder": "v1",
            "timezone": "Europe/Paris",
            "executionTimeout": 1200,
            "saveManualExecutions": False,
            "saveDataSuccessExecution": "none",
            "saveDataErrorExecution": "none",
            "saveExecutionProgress": False,
        },
        "pinData": {},
    }
