import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from fdr_irve.__main__ import main
from fdr_irve.approved import compile_mapping
from fdr_irve.conversions import RowError, convert, equal_cell, timestamp
from fdr_irve.discovery import DiscoveryError, JsonClient
from fdr_irve.mapping import build_mapping
from fdr_irve.simulation import index_grist, plan, read_airtable, simulate
from test_discovery import CONFIG, schema


def fixture():
    snapshot = schema()
    snapshot["airtable"]["tables"][0]["fields"].extend([
        {"id": "fldName", "name": "Name", "type": "singleLineText"},
        {"id": "fldAmount", "name": "Amount", "type": "percent"},
        {
            "id": "fldAODE", "name": "AODE", "type": "multipleRecordLinks",
            "options": {"linkedTableId": "tblAODE"},
        },
    ])
    snapshot["grist"]["tables"][0]["columns"].extend([
        {"id": "Name", "fields": {"type": "Text", "isFormula": False}},
        {"id": "Amount", "fields": {"type": "Numeric", "isFormula": False}},
        {"id": "AODE", "fields": {"type": "RefList:AODE", "isFormula": False}},
    ])
    snapshot["grist"]["tables"].append({
        "id": "AODE", "columns": [
            {"id": "Airtable_Id", "fields": {"type": "Text", "isFormula": False}},
        ],
    })
    proposal = build_mapping(CONFIG, snapshot)
    decisions = {
        "approved": True,
        "authorization": "read_only_simulation",
        "other_suggested_fields": "approved",
        "expected_suggested_fields": 2,
        "calculated_fields": {
            "approved": True, "write": False,
            "preserve_existing_formulas": [], "exclude_without_modification": [],
        },
        "field_decisions": [{
            "airtable_field_id": "fldAODE",
            "grist_column_id": "AODE",
            "approved": True,
            "conversion": "reference_list",
            "lookup": {
                "grist_table_id": "AODE", "grist_column_id": "Airtable_Id",
                "write_linked_table": False,
                "on_missing_or_duplicate": "report_skip_and_retry",
            },
        }],
    }
    return snapshot, proposal, decisions


def airtable_record(key="recOne", **fields):
    return {"id": key, "fields": fields}


def grist_record(row_id=1, key="recOne", **fields):
    return {"id": row_id, "fields": {"airtable_id": key, **fields}}


class ApprovedTests(unittest.TestCase):
    def test_compiles_only_reviewed_writable_fields(self):
        snapshot, proposal, decisions = fixture()
        mapping = compile_mapping(CONFIG, snapshot, proposal, decisions)
        self.assertEqual(len(mapping["fields"]), 3)
        self.assertEqual(mapping["fields"][-1]["conversion"], "reference_list")
        self.assertEqual(mapping["authorization"], "read_only_simulation")
        self.assertEqual(len(mapping["schema_fingerprint"]), 64)

    def test_unapproved_or_mismatched_scope_is_rejected(self):
        for change in (
            {"approved": False},
            {"authorization": "write"},
            {"other_suggested_fields": "pending"},
            {"expected_suggested_fields": 3},
        ):
            snapshot, proposal, decisions = fixture()
            decisions.update(change)
            with self.subTest(change=change), self.assertRaises(DiscoveryError):
                compile_mapping(CONFIG, snapshot, proposal, decisions)

    def test_stale_proposal_rejected(self):
        snapshot, proposal, decisions = fixture()
        proposal["source"]["table_id"] = "tblOld"
        with self.assertRaises(DiscoveryError):
            compile_mapping(CONFIG, snapshot, proposal, decisions)

    def test_changed_type_rejected(self):
        snapshot, proposal, decisions = fixture()
        snapshot["grist"]["tables"][0]["columns"][-1]["fields"]["type"] = "Text"
        with self.assertRaises(DiscoveryError):
            compile_mapping(CONFIG, snapshot, proposal, decisions)

    def test_invalid_or_unreviewed_lookup_rejected(self):
        for change in (
            {"write_linked_table": True},
            {"grist_column_id": "Unknown"},
            {"grist_table_id": "Wrong"},
        ):
            snapshot, proposal, decisions = fixture()
            decisions["field_decisions"][0]["lookup"].update(change)
            with self.subTest(change=change), self.assertRaises(DiscoveryError):
                compile_mapping(CONFIG, snapshot, proposal, decisions)


class ConversionTests(unittest.TestCase):
    def field(self, kind, **extra):
        return {"conversion": kind, **extra}

    def test_markdown_preserved_verbatim(self):
        self.assertEqual(convert(self.field("text"), "**A**\n- B"), "**A**\n- B")

    def test_numbers_are_not_scaled_or_rounded(self):
        for number in (0, 0.25, 12.345, -4):
            self.assertEqual(convert(self.field("number"), number), number)
        for number in (True, "12", float("nan"), float("inf"), 10 ** 400):
            with self.subTest(number=str(number)[:20]), self.assertRaises(RowError):
                convert(self.field("number"), number)
        with self.assertRaises(RowError):
            convert(self.field("integer_checked"), 1.1)

    def test_explicit_empty_clears_according_to_type(self):
        for empty in (None, "", []):
            for kind, expected in (
                ("text", ""), ("choice", ""), ("number", None), ("date", None),
                ("datetime", None), ("choice_list", None), ("reference_list", None),
                ("boolean", False),
            ):
                with self.subTest(empty=empty, kind=kind):
                    self.assertEqual(convert(self.field(kind), empty), expected)
        self.assertIs(convert(self.field("boolean"), False), False)
        with self.assertRaises(RowError):
            convert(self.field("boolean"), 0)

    def test_choices_have_grist_list_encoding_and_reject_unknowns(self):
        field = self.field("choice_list", choices=["A", "B"])
        self.assertEqual(convert(field, ["B", "A"]), ["L", "B", "A"])
        for value in (["C"], ["A", "A"], "A", [1]):
            with self.subTest(value=value), self.assertRaises(RowError):
                convert(field, value)
        with self.assertRaises(RowError):
            convert(self.field("choice", choices=["A"]), "B")

    def test_reference_list_resolves_row_ids_not_airtable_ids(self):
        field = self.field("reference_list")
        self.assertEqual(convert(field, ["recA"], {"recA": [17]}), ["L", 17])
        for index in ({}, {"recA": [1, 2]}, None):
            with self.subTest(index=index), self.assertRaises(RowError):
                convert(field, ["recA"], index)

    def test_dates_are_utc_midnight_and_datetimes_preserve_instant(self):
        self.assertEqual(convert(self.field("date"), "1970-01-02"), 86400)
        self.assertEqual(
            timestamp("2026-07-01T10:00:00+02:00", "Europe/Paris"),
            timestamp("2026-07-01T08:00:00Z", "Europe/Paris"),
        )
        self.assertEqual(
            timestamp("2026-07-01T10:00:00", "Europe/Paris"),
            datetime(2026, 7, 1, 8, tzinfo=timezone.utc).timestamp(),
        )
        self.assertEqual(
            timestamp("2026-01-01T10:00:00", "Europe/Paris"),
            datetime(2026, 1, 1, 9, tzinfo=timezone.utc).timestamp(),
        )
        for invalid in ("2026-02-30", "2026-01-01T10:00:00Z"):
            with self.subTest(value=invalid), self.assertRaises(RowError):
                convert(self.field("date"), invalid)

    def test_ambiguous_and_nonexistent_naive_times_are_reported(self):
        for value in ("2026-03-29T02:30:00", "2026-10-25T02:30:00", "2026-01-01", "bad"):
            with self.subTest(value=value), self.assertRaises(RowError):
                timestamp(value, "Europe/Paris")

    def test_comparison_does_not_confuse_boolean_and_number(self):
        self.assertFalse(equal_cell(False, 0, "Numeric"))
        self.assertTrue(equal_cell(["L"], None, "RefList:AODE"))
        self.assertFalse(equal_cell(0, None, "Numeric"))
        for before in (["C"], ["E", "Error"], ["P"]):
            with self.subTest(before=before), self.assertRaises(RowError):
                equal_cell(before, 12, "Numeric")

    def test_unknown_conversion_is_not_silently_treated_as_empty(self):
        with self.assertRaises(RowError):
            convert(self.field(None), None)


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.snapshot, self.proposal, self.decisions = fixture()
        self.mapping = compile_mapping(CONFIG, self.snapshot, self.proposal, self.decisions)

    def run_plan(self, source, target=None, linked=None):
        return plan(self.mapping, source, target or [], linked or {"AODE": []})

    def test_update_differences_only_preserves_unmapped_and_absent_fields(self):
        report = self.run_plan(
            [airtable_record(fldName="new")],
            [grist_record(Name="old", Amount=12, AODE=["L", 4], Notes="manual")],
        )
        self.assertEqual(report["updates"][0]["fields"], {"Name": "new"})
        self.assertEqual(report["updates"][0]["before"], {"Name": "old"})
        self.assertEqual(report["summary"]["changed_cells"], 1)
        self.assertEqual(report["remote_writes"], 0)
        self.assertFalse(report["cursor_advanced"])

    def test_create_includes_key_and_only_present_source_values(self):
        report = self.run_plan([airtable_record(fldAmount=0.25)])
        self.assertEqual(report["creates"][0]["fields"], {"airtable_id": "recOne", "Amount": 0.25})

    def test_explicit_null_and_zero_are_written(self):
        report = self.run_plan(
            [airtable_record(fldName=None, fldAmount=0)],
            [grist_record(Name="old", Amount=12)],
        )
        self.assertEqual(report["updates"][0]["fields"], {"Name": "", "Amount": 0})

    def test_identical_row_has_no_update(self):
        report = self.run_plan(
            [airtable_record(fldName="same", fldAmount=0)],
            [grist_record(Name="same", Amount=0)],
        )
        self.assertEqual(report["summary"]["unchanged"], 1)
        self.assertEqual(report["updates"], [])

    def test_one_conversion_error_skips_entire_row_but_other_rows_continue(self):
        report = self.run_plan(
            [
                airtable_record(fldName="valid", fldAmount="invalid"),
                airtable_record("recOther", fldAmount=3),
            ],
            [grist_record(Name="old", Amount=12)],
        )
        self.assertEqual(report["updates"], [])
        self.assertEqual(report["summary"]["creates"], 1)
        self.assertEqual(report["retry_airtable_ids"], ["recOne"])

    def test_duplicate_grist_key_never_creates_or_updates(self):
        report = self.run_plan(
            [airtable_record(fldName="new")],
            [grist_record(1), grist_record(2)],
        )
        self.assertEqual(report["summary"]["skipped"], 1)
        self.assertEqual(report["creates"], [])
        self.assertEqual(report["updates"], [])
        self.assertEqual(report["grist_key_anomalies"][0]["code"], "duplicate_key")

    def test_missing_grist_key_is_reported_and_left_untouched(self):
        report = self.run_plan([], [grist_record(key="")])
        self.assertEqual(report["summary"]["grist_key_anomalies"], 1)
        self.assertEqual(report["creates"], [])
        self.assertEqual(report["updates"], [])

    def test_missing_reference_skips_whole_enquiry(self):
        report = self.run_plan([airtable_record(fldName="new", fldAODE=["recA"])])
        self.assertEqual(report["summary"]["skipped"], 1)
        self.assertEqual(report["creates"], [])

    def test_duplicate_reference_is_never_chosen_arbitrarily(self):
        report = self.run_plan(
            [airtable_record(fldAODE=["recA"])],
            linked={"AODE": [
                {"id": 17, "fields": {"Airtable_Id": "recA"}},
                {"id": 18, "fields": {"Airtable_Id": "recA"}},
            ]},
        )
        self.assertEqual(report["summary"]["skipped"], 1)

    def test_reference_resolves_existing_grist_row_id(self):
        report = self.run_plan(
            [airtable_record(fldAODE=["recA"])],
            linked={"AODE": [{"id": 17, "fields": {"Airtable_Id": "recA"}}]},
        )
        self.assertEqual(report["creates"][0]["fields"]["AODE"], ["L", 17])

    def test_unmapped_formula_errors_do_not_block_but_mapped_errors_do(self):
        target = grist_record(Name="old")
        target["errors"] = {"Computed": "error"}
        self.assertEqual(
            self.run_plan([airtable_record(fldName="new")], [target])["summary"]["updates"], 1
        )
        target["errors"] = {"Name": "error"}
        self.assertEqual(
            self.run_plan([airtable_record(fldName="new")], [target])["summary"]["skipped"], 1
        )

    def test_missing_target_field_is_not_silently_overwritten(self):
        report = self.run_plan([airtable_record(fldName="new")], [grist_record()])
        self.assertEqual(report["summary"]["skipped"], 1)

    def test_invalid_record_shapes_fail_explicitly(self):
        with self.assertRaises(DiscoveryError):
            self.run_plan([{"fields": {}}])
        with self.assertRaises(DiscoveryError):
            index_grist([{"id": True, "fields": {}}], "airtable_id", "Enquete")

    def test_duplicate_source_records_are_reported(self):
        report = self.run_plan([airtable_record(fldName="A"), airtable_record(fldName="B")])
        self.assertEqual(report["creates"], [])
        self.assertEqual(report["summary"]["skipped"], 2)

    def test_plan_is_idempotent_after_hypothetical_in_memory_application(self):
        source = [
            airtable_record(fldName="new", fldAmount=0.25, fldAODE=["recA"]),
            airtable_record("recOther", fldName="created"),
        ]
        target = [grist_record(Name="old", Amount=0, AODE=None, Notes="manual")]
        linked = {"AODE": [{"id": 17, "fields": {"Airtable_Id": "recA"}}]}
        first = self.run_plan(source, target, linked)
        for update in first["updates"]:
            next(row for row in target if row["id"] == update["id"])["fields"].update(update["fields"])
        for index, create in enumerate(first["creates"], start=2):
            target.append({"id": index, "fields": create["fields"]})
        second = self.run_plan(source, target, linked)
        self.assertEqual(second["creates"], [])
        self.assertEqual(second["updates"], [])
        self.assertEqual(second["errors"], [])
        self.assertEqual(second["summary"]["unchanged"], 2)
        self.assertEqual(target[0]["fields"]["Notes"], "manual")


class ReadTests(unittest.TestCase):
    def setUp(self):
        snapshot, proposal, decisions = fixture()
        self.mapping = compile_mapping(CONFIG, snapshot, proposal, decisions)

    @patch("fdr_irve.simulation.time.sleep")
    def test_airtable_pagination_uses_ids_and_does_not_drop_pages(self, sleep):
        client = JsonClient("https://example.test", "synthetic-token", "Test")
        with patch.object(client, "get", side_effect=[
            {"records": [airtable_record()], "offset": "next & special"},
            {"records": [airtable_record("recTwo")]},
        ]) as get:
            result = read_airtable(client, CONFIG, self.mapping)
        self.assertEqual(len(result), 2)
        query = parse_qs(urlsplit(get.call_args_list[1].args[0]).query)
        self.assertEqual(query["offset"], ["next & special"])
        self.assertEqual(query["returnFieldsByFieldId"], ["true"])
        self.assertEqual(set(query["fields[]"]), {"fldName", "fldAmount", "fldAODE", "fldUpdated"})
        sleep.assert_called_once_with(0.21)

    @patch("fdr_irve.simulation.time.sleep")
    def test_repeated_offset_fails_instead_of_looping(self, sleep):
        client = JsonClient("https://example.test", "synthetic-token", "Test")
        with patch.object(client, "get", return_value={"records": [], "offset": "same"}):
            with self.assertRaisesRegex(DiscoveryError, "Pagination"):
                read_airtable(client, CONFIG, self.mapping)

    @patch.dict(os.environ, {"AIRTABLE_TOKEN": "synthetic-a", "GRIST_TOKEN": "synthetic-g"})
    def test_end_to_end_read_only_calls(self):
        snapshot, proposal, decisions = fixture()
        with patch("fdr_irve.simulation.discover", return_value=snapshot):
            with patch.object(JsonClient, "get", side_effect=[
                {"records": [airtable_record(fldName="private-test-value")]},
                {"records": []},
                {"records": []},
            ]) as get:
                mapping, report = simulate(CONFIG, snapshot, proposal, decisions)
        self.assertEqual(len(get.call_args_list), 3)
        self.assertEqual(report["summary"]["creates"], 1)
        self.assertTrue(get.call_args_list[1].args[0].endswith("/Enquete/records"))
        self.assertTrue(get.call_args_list[2].args[0].endswith("/AODE/records"))
        self.assertEqual(mapping["authorization"], "read_only_simulation")

    def test_schema_drift_blocks_before_reading_any_records(self):
        snapshot, proposal, decisions = fixture()
        live = copy.deepcopy(snapshot)
        live["grist"]["tables"][0]["columns"][1]["fields"]["widgetOptions"] = '{"decimals":2}'
        with patch("fdr_irve.simulation.discover", return_value=live):
            with patch.object(JsonClient, "get") as get, self.assertRaises(DiscoveryError):
                simulate(CONFIG, snapshot, proposal, decisions)
        get.assert_not_called()

    @patch.dict(os.environ, {"AIRTABLE_TOKEN": "synthetic-a", "GRIST_TOKEN": "synthetic-g"})
    def test_cli_writes_private_report_and_never_prints_values(self):
        snapshot, proposal, decisions = fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            decisions["reviewed_schema"] = "schema.json"
            for name, value in (
                ("schema.json", snapshot), ("proposal.json", proposal), ("decisions.json", decisions),
            ):
                (root / name).write_text(json.dumps(value))
            args = [
                "simulate", "--proposal", str(root / "proposal.json"),
                "--decisions", str(root / "decisions.json"), "--output-dir", str(root / "out"),
            ]
            stdout = io.StringIO()
            with patch("fdr_irve.__main__.Config.load", return_value=CONFIG):
                with patch("fdr_irve.simulation.discover", return_value=snapshot):
                    with patch.object(JsonClient, "get", side_effect=[
                        {"records": [airtable_record(fldName="private-test-value")]},
                        {"records": []}, {"records": []},
                    ]):
                        with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                            self.assertEqual(main(args), 0)
            self.assertNotIn("private-test-value", stdout.getvalue())
            output = root / "out" / "simulation.json"
            self.assertIn("private-test-value", output.read_text())
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            with patch("fdr_irve.__main__.simulate") as runner:
                with redirect_stderr(io.StringIO()):
                    self.assertEqual(main(args), 1)
                runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
