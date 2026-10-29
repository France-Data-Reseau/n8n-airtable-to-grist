import unittest

from fdr_irve.discovery import DiscoveryError
from fdr_irve.mapping import build_mapping, conversion
from test_discovery import CONFIG, schema


class MappingTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = schema()
        self.fields = self.snapshot["airtable"]["tables"][0]["fields"]
        self.columns = self.snapshot["grist"]["tables"][0]["columns"]

    def add_field(self, name, field_type="singleLineText", **extra):
        self.fields.append({
            "id": f"fld{len(self.fields)}", "name": name, "type": field_type, **extra,
        })

    def add_column(self, column_id, label=None, column_type="Text", **extra):
        self.columns.append({
            "id": column_id,
            "fields": {
                "label": label or column_id,
                "type": column_type,
                "isFormula": False,
                **extra,
            },
        })

    def build(self):
        return build_mapping(CONFIG, self.snapshot)

    def test_normalized_name_suggestion_is_never_approved(self):
        self.add_field("État du projet")
        self.add_column("Etat_du_projet")
        result = self.build()
        field = result["fields"][-1]
        self.assertEqual(field["grist_column_id"], "Etat_du_projet")
        self.assertEqual(field["status"], "suggested")
        self.assertEqual(field["conversion"], "text")
        self.assertFalse(field["approved"])
        self.assertFalse(result["record_key"]["approved"])
        self.assertFalse(result["approved"])
        self.assertEqual(result["blockers"], [])

    def test_matches_visible_label(self):
        self.add_field("Statut")
        self.add_column("Col_A", label="Statut")
        self.assertEqual(self.build()["fields"][-1]["grist_column_id"], "Col_A")

    def test_comparison_operators_distinguish_business_fields(self):
        self.add_field("Nb PdR DC<50 kW 2023", "number")
        self.add_field("Nb PdR DC=>50 kW 2023", "number")
        self.add_column(
            "Nb_PdR_DC_50_kW_2023",
            label="Nb PdR DC<50 kW 2023",
            column_type="Numeric",
        )
        self.add_column(
            "Nb_PdR_DC_50_kW_2023_2",
            label="Nb PdR DC=>50 kW 2023",
            column_type="Numeric",
        )
        fields = self.build()["fields"][-2:]
        self.assertEqual([field["status"] for field in fields], ["suggested", "suggested"])
        self.assertEqual(
            [field["grist_column_id"] for field in fields],
            ["Nb_PdR_DC_50_kW_2023", "Nb_PdR_DC_50_kW_2023_2"],
        )

    def test_ambiguous_names_have_no_selected_target(self):
        self.add_field("Nom")
        self.add_column("Nom")
        self.add_column("Nom_2", label="Nom")
        field = self.build()["fields"][-1]
        self.assertEqual(field["status"], "ambiguous")
        self.assertIsNone(field["grist_column_id"])
        self.assertEqual(field["candidates"], ["Nom", "Nom_2"])

    def test_normalized_source_collisions_are_not_silently_accepted(self):
        self.add_field("État")
        self.add_field("Etat")
        self.add_column("Etat")
        fields = self.build()["fields"][-2:]
        self.assertTrue(all(field["status"] == "ambiguous" for field in fields))
        self.assertTrue(all(field["conversion"] is None for field in fields))

    def test_reserved_record_key_cannot_be_mapped_as_a_business_field(self):
        self.add_field("airtable_id")
        self.assertEqual(self.build()["fields"][-1]["status"], "unmatched")

    def test_formula_columns_are_not_writable(self):
        self.add_field("Nom")
        self.add_column("Nom", isFormula=True, formula="$Other")
        field = self.build()["fields"][-1]
        self.assertEqual(field["status"], "read_only")
        self.assertIsNone(field["conversion"])

    def test_missing_writability_metadata_is_not_assumed_safe(self):
        self.add_field("Nom")
        self.add_column("Nom")
        del self.columns[-1]["fields"]["isFormula"]
        self.assertEqual(self.build()["fields"][-1]["status"], "read_only")

    def test_unknown_types_require_configuration(self):
        self.add_field("Calcul", "formula", options={"result": {"type": "number"}})
        self.add_column("Calcul", column_type="Numeric")
        self.assertEqual(self.build()["fields"][-1]["status"], "requires_configuration")

    def test_linked_records_require_explicit_lookup_and_cardinality(self):
        self.add_field("Sites", "multipleRecordLinks", options={"linkedTableId": "tblSites"})
        self.add_column("Sites", column_type="RefList:Sites")
        field = self.build()["fields"][-1]
        self.assertEqual(field["status"], "requires_configuration")
        self.assertIsNone(field["conversion"])
        self.assertEqual(field["reference"]["airtable_linked_table_id"], "tblSites")
        self.assertEqual(field["reference"]["grist_linked_table_id"], "Sites")
        self.assertIsNone(field["reference"]["lookup_column"])
        self.assertIsNone(field["reference"]["cardinality"])

    def test_missing_target_table_is_explicit(self):
        self.snapshot["grist"]["tables"] = []
        with self.assertRaisesRegex(DiscoveryError, "table cible absente"):
            self.build()

    def test_missing_key_and_invalid_modification_field_are_blockers(self):
        self.columns.clear()
        self.fields[0]["type"] = "dateTime"
        self.assertEqual(len(self.build()["blockers"]), 2)

    def test_key_must_be_text_and_writable(self):
        for metadata in (
            {"type": "Int", "isFormula": False},
            {"type": "Text", "isFormula": True},
            {"type": "Text"},
        ):
            with self.subTest(metadata=metadata):
                self.columns[0]["fields"] = metadata
                self.assertTrue(self.build()["blockers"])

    def test_empty_normalized_names_do_not_match(self):
        self.add_field("---")
        self.add_column("Punctuation", label="!!!")
        self.assertEqual(self.build()["fields"][-1]["status"], "unmatched")

    def test_preserves_unmapped_targets_and_agreed_rules(self):
        self.add_column("Notes_manuelles")
        result = self.build()
        self.assertEqual(result["unmapped_grist_columns"], ["Notes_manuelles"])
        self.assertEqual(result["sync"]["missing_field"], "ignore")
        self.assertEqual(result["sync"]["explicit_empty"], "clear_if_type_allows")
        self.assertFalse(result["sync"]["delete"])
        self.assertEqual(result["sync"]["interval_minutes"], 30)
        self.assertEqual(result["sync"]["modified_field"], "updatedat")
        self.assertEqual(result["sync"]["row_error"], "report_skip_and_retry")

    def test_expected_conversion_shapes(self):
        for source, target, expected in (
            ("singleLineText", "Text", "text"),
            ("number", "Numeric", "number"),
            ("number", "Int", "integer_checked"),
            ("checkbox", "Bool", "boolean"),
            ("date", "Date", "date"),
            ("dateTime", "DateTime:Europe/Paris", "datetime"),
            ("lastModifiedTime", "DateTime:UTC", "datetime"),
            ("singleSelect", "Choice", "choice"),
            ("multipleSelects", "ChoiceList", "choice_list"),
            ("multipleRecordLinks", "RefList:Sites", None),
            ("multipleSelects", "Text", None),
            ("dateTime", "Date", None),
        ):
            with self.subTest(source=source, target=target):
                self.assertEqual(conversion(source, target), expected)


if __name__ == "__main__":
    unittest.main()
