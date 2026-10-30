import copy
import json
import subprocess
import unittest
import test_n8n

from fdr_irve.conversions import convert, timestamp
from fdr_irve.n8n_runtime import finalize, prepare_plan, summary, validate_schema
from fdr_irve.n8n_workflow import javascript_code
from test_n8n import (
    ROOT, WorkflowHarness, context_fixture, execute_js,
)
from test_simulation import airtable_record, grist_record


RUNTIME = (ROOT / "fdr_irve" / "n8n_runtime.js").read_text()


def call_js(expression, value):
    return execute_js(
        RUNTIME + f"\nreturn {expression};", [{"json": copy.deepcopy(value)}],
    )


class JavaScriptWorkflowTests(test_n8n.WorkflowTests):
    def harness(self, mode="simulation"):
        return WorkflowHarness(mode, language="javascript")

    def test_javascript_export_has_no_python_nodes_or_imports(self):
        workflow = json.loads(
            (ROOT / "n8n" / "airtable-to-grist.javascript.workflow.json").read_text()
        )
        self.assertFalse(workflow["active"])
        for node in workflow["nodes"]:
            parameters = node["parameters"]
            self.assertNotIn("pythonCode", parameters)
            self.assertNotEqual(parameters.get("language"), "pythonNative")
            if node["type"] == "n8n-nodes-base.code":
                self.assertIn("jsCode", parameters)
                self.assertNotIn("require(", parameters["jsCode"])
                self.assertNotIn("import ", parameters["jsCode"])
        entries = {
            "Preparer execution": "prepare", "Valider schemas": "validate_schema",
            "Plan": "prepare_plan", "Finaliser etat": "finalize", "Bilan": "summary",
        }
        for node in workflow["nodes"]:
            if node["name"] in entries:
                self.assertEqual(
                    node["parameters"]["jsCode"], javascript_code(entries[node["name"]]),
                )


class JavaScriptParityTests(unittest.TestCase):
    def test_dates_offsets_and_naive_timezones_match_python(self):
        for value in (
            "2026-07-01T10:00:00+02:00", "2026-07-01T08:00:00Z",
            "2026-01-01T10:00:00", "2026-07-01T10:00:00",
            "2026-10-25T02:30:00+02:00", "2026-10-25T02:30:00+01:00",
            "2026-01-01T10:00:00.123456Z",
        ):
            with self.subTest(value=value):
                result = call_js("timestamp($input.first().json.value, 'Europe/Paris')", {"value": value})
                self.assertAlmostEqual(result, timestamp(value, "Europe/Paris"), places=5)

    def test_invalid_and_dst_dates_are_rejected_without_silent_normalization(self):
        for value in (
            "2026-03-29T02:30:00", "2026-10-25T02:30:00",
            "2026-02-30T12:00:00Z", "2026-01-01T24:00:00Z",
            "2026-01-01T10:00:00+25:00", "2026-01-01", "bad",
        ):
            with self.subTest(value=value), self.assertRaises(subprocess.CalledProcessError):
                call_js("timestamp($input.first().json.value, 'Europe/Paris')", {"value": value})

    def test_value_conversions_match_python(self):
        cases = [
            ({"conversion": "text"}, "**A**\n- B"),
            ({"conversion": "number"}, 0),
            ({"conversion": "number"}, 0.25),
            ({"conversion": "boolean"}, False),
            ({"conversion": "choice", "choices": ["A"]}, "A"),
            ({"conversion": "choice_list", "choices": ["A", "B"]}, ["B", "A"]),
            ({"conversion": "date"}, "1970-01-02"),
            ({"conversion": "date"}, "2024-02-29"),
        ]
        for empty in (None, "", []):
            for kind in ("text", "choice", "boolean", "number", "choice_list", "reference_list", "date"):
                cases.append(({"conversion": kind}, empty))
        for field, value in cases:
            with self.subTest(field=field, value=value):
                js = call_js(
                    "convert($input.first().json.field, $input.first().json.value)",
                    {"field": field, "value": value},
                )
                self.assertEqual(js, convert(field, value))

    def test_conversion_errors_never_turn_into_success(self):
        for field, value in (
            ({"conversion": "number"}, True),
            ({"conversion": "number"}, "12"),
            ({"conversion": "number"}, 9007199254740992),
            ({"conversion": "integer_checked"}, 1.1),
            ({"conversion": "boolean"}, 0),
            ({"conversion": "choice", "choices": ["A"]}, "B"),
            ({"conversion": "choice_list", "choices": ["A"]}, ["A", "A"]),
            ({"conversion": "date"}, "2026-02-30"),
            ({"conversion": None}, None),
        ):
            with self.subTest(field=field, value=value):
                with self.assertRaises(subprocess.CalledProcessError):
                    call_js(
                        "convert($input.first().json.field, $input.first().json.value)",
                        {"field": field, "value": value},
                    )

    def test_full_plan_finalization_and_summary_match_python(self):
        harness = WorkflowHarness("write", language="javascript")
        harness.source = [
            airtable_record(fldName="new", fldAmount=0.25, fldAODE=["recA"]),
            airtable_record("recOther", fldName="create"),
        ]
        harness.linked = [{"id": 17, "fields": {"Airtable_Id": "recA"}}]
        harness.run()
        js_context = harness.outputs["Assembler donnees"][0]["json"]
        expected = prepare_plan(copy.deepcopy(js_context))
        actual = harness.outputs["Plan"][0]["json"]
        for key in ("operations",):
            self.assertEqual(actual[key], expected[key])
        self.assertEqual(actual["report"]["summary"], expected["report"]["summary"])
        self.assertEqual(
            json.loads(actual["checkpoint_payload"]), json.loads(expected["checkpoint_payload"])
        )
        verification = harness.outputs["Assembler verification"][0]["json"]
        expected_final = finalize(copy.deepcopy(verification))
        actual_final = harness.outputs["Finaliser etat"][0]["json"]
        self.assertEqual(
            json.loads(actual_final["final_payload"]), json.loads(expected_final["final_payload"])
        )
        self.assertEqual(summary(expected_final), harness.outputs["Bilan"][0]["json"])

    def test_schema_equality_is_independent_of_json_object_key_order(self):
        schema, workflow, context = context_fixture("javascript")
        context = execute_js(javascript_code("prepare"), [{"json": context}])[0]["json"]
        context["source_schema"] = schema["airtable"]
        context["target_schemas"] = {
            table["id"]: {"columns": table["columns"]} for table in schema["grist"]["tables"]
        }
        expected = validate_schema(copy.deepcopy(context))
        actual = execute_js(javascript_code("validate_schema"), [{"json": context}])[0]["json"]
        self.assertEqual(actual, expected)
        context["target_schemas"]["Enquete"]["columns"][1]["fields"]["isFormula"] = True
        with self.assertRaises(subprocess.CalledProcessError):
            execute_js(javascript_code("validate_schema"), [{"json": context}])

    def test_row_errors_skip_whole_row_and_references_are_checked(self):
        for values in (
            {"fldName": "new", "fldAmount": "invalid"},
            {"fldName": "new", "fldAODE": ["recMissing"]},
        ):
            harness = WorkflowHarness("write", language="javascript")
            harness.source = [airtable_record(**values), airtable_record("recOther", fldName="valid")]
            result = harness.run()
            self.assertEqual(result["summary"]["skipped"], 1)
            self.assertEqual(harness.puts, ["recOther"])
            self.assertTrue(harness.error)
            self.assertEqual(json.loads(harness.state["payload"])["retry_ids"], ["recOne"])


if __name__ == "__main__":
    unittest.main()
