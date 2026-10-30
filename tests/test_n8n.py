import copy
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from fdr_irve.__main__ import main
from fdr_irve.approved import compile_mapping
from fdr_irve.discovery import DiscoveryError
from fdr_irve.n8n_runtime import (
    finalize, prepare, prepare_plan, summary, validate_schema,
)
from fdr_irve.n8n_workflow import build_workflow, python_code
from test_simulation import CONFIG, airtable_record, fixture, grist_record


ROOT = Path(__file__).resolve().parents[1]


def settings(workflow):
    source = next(node for node in workflow["nodes"] if node["name"] == "Configuration")
    return json.JSONDecoder().raw_decode(source["parameters"]["jsCode"].split("const config = ", 1)[1])[0]


def execute_python(source, value):
    namespace: dict[str, Any] = {}
    body = "def run(_items):\n" + "\n".join("    " + line for line in source.splitlines())
    exec(compile(body, "<n8n-python>", "exec"), namespace)
    return namespace["run"]([{"json": copy.deepcopy(value)}])


def execute_js(source, items, outputs=None, expression=False) -> Any:
    process = subprocess.run(
        ["node", str(ROOT / "tests" / "n8n_js_harness.mjs")],
        input=json.dumps({
            "source": source, "items": items, "outputs": outputs or {},
            "mode": "expression" if expression else "code",
        }),
        text=True, capture_output=True, check=True, timeout=10,
    )
    return json.loads(process.stdout)


def context_fixture(language="python"):
    schema, proposal, decisions = fixture()
    mapping = compile_mapping(CONFIG, schema, proposal, decisions)
    workflow = build_workflow(mapping, schema, language=language)
    config = settings(workflow)
    state = {"version": 1, "cursor": None, "retry_ids": [], "mapping_fingerprint": ""}
    context = {
        "config": config,
        "state_rows": [{"id": 1, "sync_key": config["state_key"], "payload": json.dumps(state)}],
        "started_at": "2026-10-29T15:00:00+00:00",
    }
    return schema, workflow, context


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.schema, self.workflow, self.context = context_fixture()

    def test_initial_run_is_full_and_simulation_has_no_cursor_side_effect(self):
        prepared = prepare(copy.deepcopy(self.context))
        self.assertIsNone(prepared["filter_formula"])
        self.assertEqual(prepared["read_mode"], "full")
        self.assertIsNone(prepared["state"]["cursor"])

    def test_write_mode_needs_both_explicit_gates(self):
        for confirmation, serialized in (("", False), ("WRITE_AIRTABLE_TO_GRIST", False), ("", True)):
            context = copy.deepcopy(self.context)
            context["config"].update(
                mode="write", write_confirmation=confirmation,
                serialized_executions_confirmed=serialized,
            )
            with self.subTest(confirmation=confirmation, serialized=serialized):
                with self.assertRaisesRegex(DiscoveryError, "Écriture refusée"):
                    prepare(context)

    def test_write_mode_requires_modified_field_coverage(self):
        context = copy.deepcopy(self.context)
        context["config"].update(
            mode="write", write_confirmation="WRITE_AIRTABLE_TO_GRIST",
            serialized_executions_confirmed=True, modified_field_coverage_confirmed=False,
        )
        with self.assertRaisesRegex(DiscoveryError, "couverture"):
            prepare(context)
        context["config"]["modified_field_coverage_confirmed"] = True
        self.assertEqual(prepare(context)["config"]["mode"], "write")

    def test_incremental_query_includes_overlap_creation_and_unchanged_retries(self):
        state = {
            "version": 1, "cursor": "2026-10-29T14:30:00+00:00",
            "retry_ids": ["recFailed"],
            "mapping_fingerprint": self.context["config"]["mapping_fingerprint"],
        }
        self.context["state_rows"][0]["payload"] = json.dumps(state)
        formula = prepare(self.context)["filter_formula"]
        self.assertIn("2026-10-29T14:28:00+00:00", formula)
        self.assertIn("{fldUpdated}", formula)
        self.assertIn("CREATED_TIME()", formula)
        self.assertIn("RECORD_ID()='recFailed'", formula)

    def test_force_full_scan_does_not_discard_pending_retries(self):
        context = copy.deepcopy(self.context)
        context["config"]["force_full_scan"] = True
        state = {
            "version": 1, "cursor": "2026-10-29T14:30:00Z", "retry_ids": ["recFailed"],
            "mapping_fingerprint": context["config"]["mapping_fingerprint"],
        }
        context["state_rows"][0]["payload"] = json.dumps(state)
        prepared = prepare(context)
        self.assertIsNone(prepared["filter_formula"])
        self.assertEqual(prepared["state"]["retry_ids"], ["recFailed"])

    def test_invalid_missing_duplicate_or_stale_state_blocks(self):
        for rows in (
            [],
            self.context["state_rows"] * 2,
            [{"id": 1, "sync_key": "wrong", "payload": "{}"}],
            [{"id": 1, "sync_key": self.context["config"]["state_key"], "payload": "bad json"}],
            [{
                "id": 1, "sync_key": self.context["config"]["state_key"],
                "payload": json.dumps({
                    "version": 1, "cursor": None, "retry_ids": [], "mapping_fingerprint": "old",
                }),
            }],
        ):
            context = copy.deepcopy(self.context)
            context["state_rows"] = rows
            with self.subTest(rows=rows), self.assertRaises(DiscoveryError):
                prepare(context)

    def test_future_and_naive_cursor_block(self):
        for cursor in ("2099-01-01T00:00:00Z", "2026-10-29T14:30:00"):
            context = copy.deepcopy(self.context)
            context["state_rows"][0]["payload"] = json.dumps({
                "version": 1, "cursor": cursor, "retry_ids": [],
                "mapping_fingerprint": context["config"]["mapping_fingerprint"],
            })
            with self.subTest(cursor=cursor), self.assertRaises(DiscoveryError):
                prepare(context)

    def live_context(self):
        context = prepare(copy.deepcopy(self.context))
        context["source_schema"] = self.schema["airtable"]
        context["target_schemas"] = {
            table["id"]: {"columns": table["columns"]} for table in self.schema["grist"]["tables"]
        }
        return context

    def test_schema_drift_blocks_before_planning(self):
        context = self.live_context()
        context["target_schemas"]["Enquete"]["columns"][1]["fields"]["isFormula"] = True
        with self.assertRaisesRegex(DiscoveryError, "schéma a changé"):
            validate_schema(context)

    def test_incomplete_or_duplicate_pagination_blocks(self):
        for pages in (
            [],
            [{"records": [], "offset": "more"}],
            [{"records": [], "offset": "same"}, {"records": [], "offset": "same"}, {"records": []}],
        ):
            context = validate_schema(self.live_context())
            context.update(
                airtable_pages=pages, target_records={"records": []},
                linked_records={"AODE": {"records": []}},
            )
            with self.subTest(pages=pages), self.assertRaises(DiscoveryError):
                prepare_plan(context)

    def planned(self):
        context = validate_schema(self.live_context())
        context["config"].update(
            mode="write", write_confirmation="WRITE_AIRTABLE_TO_GRIST",
            serialized_executions_confirmed=True,
        )
        context.update(
            airtable_pages=[{"records": [airtable_record(fldName="new")]}],
            target_records={"records": [grist_record(Name="old")]},
            linked_records={"AODE": {"records": []}},
        )
        return prepare_plan(context)

    def test_checkpoint_retains_old_cursor_and_all_planned_keys(self):
        context = self.planned()
        state = json.loads(context["checkpoint_payload"])
        self.assertIsNone(state["cursor"])
        self.assertEqual(state["retry_ids"], ["recOne"])
        self.assertEqual(context["operations"][0]["body"], {
            "records": [{"require": {"airtable_id": "recOne"}, "fields": {"Name": "new"}}],
        })

    def test_verification_mismatch_keeps_record_in_retry_queue(self):
        context = self.planned()
        context["write_results"] = [{"airtable_record_id": "recOne", "status_code": 200}]
        context["verification_records"] = {"records": [grist_record(Name="wrong")]}
        final = finalize(context)
        state = json.loads(final["final_payload"])
        self.assertEqual(state["retry_ids"], ["recOne"])
        self.assertEqual(state["cursor"], context["started_at"])
        self.assertTrue(summary(final)["has_anomalies"])

    def test_no_result_means_no_cursor_advancement(self):
        context = self.planned()
        context["write_results"] = []
        with self.assertRaisesRegex(DiscoveryError, "incomplets"):
            finalize(context)

    def test_http_failure_even_if_target_matches_is_retried(self):
        context = self.planned()
        context["write_results"] = [{"airtable_record_id": "recOne", "status_code": 429}]
        context["verification_records"] = {"records": [grist_record(Name="new")]}
        final = finalize(context)
        self.assertEqual(json.loads(final["final_payload"])["retry_ids"], ["recOne"])

    def test_duplicate_after_write_is_detected_even_with_http_success(self):
        context = self.planned()
        context["write_results"] = [{"airtable_record_id": "recOne", "status_code": 200}]
        context["verification_records"] = {"records": [
            grist_record(1, Name="new"), grist_record(2, Name="new"),
        ]}
        final = finalize(context)
        self.assertEqual(json.loads(final["final_payload"])["retry_ids"], ["recOne"])
        self.assertEqual(final["report"]["write_errors"][0]["code"], "write_key_not_unique")

    def test_retry_not_returned_is_never_silently_discarded(self):
        context = self.planned()
        context["state"]["retry_ids"] = ["recDisappeared"]
        context.update(
            airtable_pages=[{"records": []}], target_records={"records": []},
            linked_records={"AODE": {"records": []}},
        )
        planned = prepare_plan(context)
        self.assertIn("recDisappeared", planned["report"]["retry_airtable_ids"])
        self.assertTrue(summary(planned)["has_anomalies"])

    def test_embedded_python_uses_shared_semantics_without_custom_classes(self):
        for entry in ("prepare", "validate_schema", "prepare_plan", "finalize", "summary"):
            code = python_code(entry)
            self.assertNotIn("class DiscoveryError", code)
            self.assertNotIn("class RowError", code)
            self.assertNotIn("from .", code)
        embedded = execute_python(python_code("prepare"), self.context)[0]["json"]
        self.assertEqual(embedded, prepare(copy.deepcopy(self.context)))


class WorkflowHarness:
    """Execute the actual generated graph with synthetic HTTP/Data Table responses."""

    def __init__(self, mode="simulation", language="python"):
        self.schema, self.workflow, context = context_fixture(language=language)
        self.state = copy.deepcopy(context["state_rows"][0])
        config = context["config"]
        config.update(
            state_table_id="stateExample", mode=mode,
            write_confirmation="WRITE_AIRTABLE_TO_GRIST" if mode == "write" else "",
            serialized_executions_confirmed=mode == "write",
            modified_field_coverage_confirmed=mode == "write",
        )
        config_node = next(node for node in self.workflow["nodes"] if node["name"] == "Configuration")
        original = settings(self.workflow)
        config_node["parameters"]["jsCode"] = config_node["parameters"]["jsCode"].replace(
            json.dumps(original, ensure_ascii=False, indent=2),
            json.dumps(config, ensure_ascii=False, indent=2),
        )
        self.source = [airtable_record(fldName="new")]
        self.target = [grist_record(Name="old", Amount=0, AODE=None)]
        self.linked = []
        self.fail_keys = set()
        self.interrupt_after_write = False
        self.checkpoint_failure = False
        self.state_writes = []
        self.puts = []
        self.outputs: dict[str, list[dict[str, Any]]] = {}
        self.error = False
        self.nodes = {node["name"]: node for node in self.workflow["nodes"]}

    def expression(self, expression: Any, items: list[dict[str, Any]]) -> Any:
        if not isinstance(expression, str) or not expression.startswith("={{"):
            return expression
        return execute_js(expression, items, self.outputs, expression=True)

    def http(self, parameters, items):
        if parameters["method"] == "GET":
            url = self.expression(parameters["url"], items)
            if "/meta/bases/" in url:
                body = copy.deepcopy(self.schema["airtable"])
            elif url.endswith("/columns"):
                table_id = url.split("/")[-2]
                table = next(t for t in self.schema["grist"]["tables"] if t["id"] == table_id)
                body = {"columns": copy.deepcopy(table["columns"])}
            elif url.endswith("/AODE/records"):
                body = {"records": copy.deepcopy(self.linked)}
            elif "/api/docs/" in url:
                body = {"records": copy.deepcopy(self.target)}
            else:
                body = {"records": copy.deepcopy(self.source)}
            return [{"json": body, "pairedItem": {"item": 0}}]
        responses = []
        for index, item in enumerate(items):
            operation = item["json"]
            state = json.loads(self.state["payload"])
            assert operation["airtable_record_id"] in state["retry_ids"]
            self.puts.append(operation["airtable_record_id"])
            if operation["airtable_record_id"] in self.fail_keys:
                responses.append({"json": {"statusCode": 429}, "pairedItem": {"item": index}})
                continue
            url = self.expression(parameters["url"], [item])
            query = parse_qs(urlsplit(url).query)
            assert query["onmany"] == ["none"] and query["noparse"] == ["true"]
            assert query["allow_empty_require"] == ["false"]
            body = self.expression(parameters["jsonBody"], [item])
            record = body["records"][0]
            key = record["require"]["airtable_id"]
            matches = [row for row in self.target if row["fields"].get("airtable_id") == key]
            if len(matches) == 1 and query["noupdate"] == ["false"]:
                matches[0]["fields"].update(record["fields"])
            elif not matches and query["noadd"] == ["false"]:
                self.target.append({
                    "id": max([row["id"] for row in self.target], default=0) + 1,
                    "fields": {"airtable_id": key, **record["fields"]},
                })
            if self.interrupt_after_write:
                raise RuntimeError("Synthetic interruption after remote success")
            responses.append({"json": {"statusCode": 200}, "pairedItem": {"item": index}})
        return responses

    def run(self):
        self.outputs = {}
        self.error = False
        name = "Manuel"
        items = [{"json": {}}]
        for _ in range(80):
            node = self.nodes[name]
            parameters = node["parameters"]
            kind = node["type"].split(".")[-1]
            output_index = 0
            if kind == "code":
                items = (
                    execute_python(parameters["pythonCode"], items[0]["json"])
                    if parameters.get("language") == "pythonNative"
                    else execute_js(parameters["jsCode"], items, self.outputs)
                )
            elif kind == "dataTable":
                if parameters["operation"] == "get":
                    items = [{"json": copy.deepcopy(self.state)}]
                else:
                    if self.checkpoint_failure:
                        raise RuntimeError("Synthetic state storage failure")
                    payload = self.expression(parameters["columns"]["value"]["payload"], items)
                    self.state["payload"] = payload
                    self.state_writes.append(json.loads(payload))
                    items = [{"json": copy.deepcopy(self.state)}]
            elif kind == "httpRequest":
                items = self.http(parameters, items)
            elif kind == "if":
                expression = parameters["conditions"]["conditions"][0]["leftValue"]
                output_index = 0 if self.expression(expression, items) else 1
            elif kind == "stopAndError":
                self.error = True
                self.outputs[name] = items
                break
            self.outputs[name] = copy.deepcopy(items)
            outputs = self.workflow["connections"].get(name, {}).get("main", [])
            if output_index >= len(outputs) or not outputs[output_index]:
                break
            name = outputs[output_index][0]["node"]
        else:
            raise AssertionError("Synthetic workflow did not terminate")
        if "Bilan" not in self.outputs:
            raise AssertionError("Synthetic workflow did not produce its summary")
        return self.outputs["Bilan"][0]["json"]


class WorkflowTests(unittest.TestCase):
    def harness(self, mode="simulation"):
        return WorkflowHarness(mode)

    def test_export_is_inactive_has_no_credentials_or_delete_and_is_deterministic(self):
        schema, workflow, _ = context_fixture()
        self.assertFalse(workflow["active"])
        self.assertEqual(settings(workflow)["mode"], "simulation")
        self.assertEqual(settings(workflow)["write_confirmation"], "")
        self.assertFalse(settings(workflow)["serialized_executions_confirmed"])
        self.assertFalse(settings(workflow)["modified_field_coverage_confirmed"])
        self.assertNotIn('"credentials"', json.dumps(workflow))
        self.assertNotIn('"DELETE"', json.dumps(workflow))
        for node in workflow["nodes"]:
            self.assertNotIn("pinData", node)
        snapshot, proposal, decisions = fixture()
        self.assertEqual(
            build_workflow(compile_mapping(CONFIG, snapshot, proposal, decisions), schema), workflow,
        )
        names = {node["name"] for node in workflow["nodes"]}
        for name, edges in workflow["connections"].items():
            self.assertIn(name, names)
            for output in edges["main"]:
                for edge in output:
                    self.assertIn(edge["node"], names)

    def test_export_command_is_offline_and_requires_explicit_overwrite(self):
        snapshot, proposal, decisions = fixture()
        decisions["reviewed_schema"] = "schema.json"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, value in (
                ("schema.json", snapshot), ("proposal.json", proposal),
                ("decisions.json", decisions),
            ):
                (root / name).write_text(json.dumps(value))
            args = [
                "export-n8n", "--proposal", str(root / "proposal.json"),
                "--decisions", str(root / "decisions.json"),
                "--output-dir", str(root / "out"),
            ]
            with patch("fdr_irve.__main__.Config.load", return_value=CONFIG):
                with patch("fdr_irve.discovery.JsonClient.get") as network:
                    with redirect_stdout(StringIO()):
                        self.assertEqual(main(args), 0)
                    network.assert_not_called()
                    output = root / "out" / "airtable-to-grist.workflow.json"
                    content = output.read_bytes()
                    with redirect_stderr(StringIO()):
                        self.assertEqual(main(args), 1)
                    self.assertEqual(output.read_bytes(), content)
                    with redirect_stdout(StringIO()):
                        self.assertEqual(main([*args, "--overwrite"]), 0)
                    self.assertEqual(output.read_bytes(), content)

    def test_default_configuration_requires_state_table_setup(self):
        _, workflow, _ = context_fixture()
        config = next(node for node in workflow["nodes"] if node["name"] == "Configuration")
        with self.assertRaises(subprocess.CalledProcessError):
            execute_js(config["parameters"]["jsCode"], [{"json": {}}])

    def test_simulation_graph_never_writes_data_or_state(self):
        harness = self.harness()
        before = copy.deepcopy(harness.target)
        result = harness.run()
        self.assertEqual(result["summary"]["updates"], 1)
        self.assertFalse(result["cursor_advanced"])
        self.assertEqual(harness.puts, [])
        self.assertEqual(harness.state_writes, [])
        self.assertEqual(harness.target, before)

    def test_write_graph_checkpoints_verifies_and_advances_cursor(self):
        harness = self.harness("write")
        harness.source.append(airtable_record("recOther", fldName="created"))
        result = harness.run()
        self.assertEqual(result["summary"]["verified_writes"], 2)
        self.assertEqual(harness.state_writes[0]["retry_ids"], ["recOne", "recOther"])
        self.assertIsNone(harness.state_writes[0]["cursor"])
        self.assertEqual(harness.state_writes[-1]["retry_ids"], [])
        self.assertTrue(harness.state_writes[-1]["cursor"])
        self.assertEqual(len(harness.target), 2)
        self.assertFalse(harness.error)

    def test_empty_incremental_run_still_commits_cursor(self):
        harness = self.harness("write")
        harness.source = []
        result = harness.run()
        self.assertEqual(harness.puts, [])
        self.assertEqual(result["summary"]["verified_writes"], 0)
        self.assertEqual(len(harness.state_writes), 2)
        self.assertTrue(json.loads(harness.state["payload"])["cursor"])

    def test_partial_http_failure_is_persisted_and_signaled_then_retried(self):
        harness = self.harness("write")
        harness.source.append(airtable_record("recOther", fldName="created"))
        harness.fail_keys = {"recOne"}
        first = harness.run()
        self.assertTrue(harness.error)
        self.assertEqual(first["summary"]["write_failures"], 1)
        self.assertEqual(first["summary"]["verified_writes"], 1)
        self.assertEqual(json.loads(harness.state["payload"])["retry_ids"], ["recOne"])
        harness.source = [airtable_record(fldName="new")]
        harness.fail_keys = set()
        second = harness.run()
        self.assertIn(
            "RECORD_ID()='recOne'",
            harness.outputs["Preparer execution"][0]["json"]["filter_formula"],
        )
        self.assertEqual(second["summary"]["verified_writes"], 1)
        self.assertEqual(json.loads(harness.state["payload"])["retry_ids"], [])

    def test_interruption_after_creation_replays_without_duplicate(self):
        harness = self.harness("write")
        harness.target = []
        harness.interrupt_after_write = True
        with self.assertRaisesRegex(RuntimeError, "interruption"):
            harness.run()
        self.assertEqual(len(harness.target), 1)
        self.assertEqual(json.loads(harness.state["payload"])["retry_ids"], ["recOne"])
        self.assertIsNone(json.loads(harness.state["payload"])["cursor"])
        harness.interrupt_after_write = False
        result = harness.run()
        self.assertEqual(len(harness.target), 1)
        self.assertEqual(result["summary"]["unchanged"], 1)
        self.assertEqual(json.loads(harness.state["payload"])["retry_ids"], [])

    def test_checkpoint_failure_prevents_all_grist_writes(self):
        harness = self.harness("write")
        harness.checkpoint_failure = True
        with self.assertRaisesRegex(RuntimeError, "storage"):
            harness.run()
        self.assertEqual(harness.puts, [])

    def test_actual_export_uses_exact_reviewed_perimeter(self):
        mapping = json.loads((ROOT / "n8n" / "airtable-to-grist.workflow.json").read_text())
        config = settings(mapping)
        self.assertEqual(len(config["mapping"]["fields"]), 121)
        self.assertEqual(config["mapping"]["record_key"]["target_column"], "Airtable_Id")
        columns = {field["grist_column_id"] for field in config["mapping"]["fields"]}
        self.assertFalse(columns & {
            "AODE_Amenageur", "Sigle", "Panel", "Siren", "Type", "Departement",
            "Region", "Repondant", "Declencheur_envoi",
        })
        expected_code = {
            "Preparer execution": "prepare",
            "Valider schemas": "validate_schema",
            "Plan": "prepare_plan",
            "Finaliser etat": "finalize",
            "Bilan": "summary",
        }
        for node in mapping["nodes"]:
            if node["name"] in expected_code:
                self.assertEqual(
                    node["parameters"]["pythonCode"], python_code(expected_code[node["name"]]),
                )


if __name__ == "__main__":
    unittest.main()
