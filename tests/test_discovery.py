import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.client import HTTPMessage, RemoteDisconnected
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

from fdr_irve.__main__ import main
from fdr_irve.discovery import Config, DiscoveryError, JsonClient, NoRedirect, discover


CONFIG = Config(
    base_id="appExample",
    airtable_table_id="tblExample",
    modified_field="updatedat",
    grist_url="https://grist.example.test",
    document_id="docExample",
    grist_table_id="Enquete",
    key_column="airtable_id",
)


def schema():
    return {
        "airtable": {
            "tables": [{
                "id": "tblExample",
                "fields": [{
                    "id": "fldUpdated",
                    "name": "updatedat",
                    "type": "lastModifiedTime",
                }],
            }],
        },
        "grist": {
            "tables": [{
                "id": "Enquete",
                "columns": [{
                    "id": "airtable_id",
                    "fields": {"type": "Text", "isFormula": False},
                }],
            }],
        },
    }


class ConfigTests(unittest.TestCase):
    def load(self, server_url="https://grist.example.test", table_id="Enquete"):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                '[airtable]\nbase_id="appExample"\ntable_id="tblExample"\n'
                'modified_field="updatedat"\n[grist]\n'
                f'server_url="{server_url}"\ndocument_id="docExample"\n'
                f'table_id="{table_id}"\nairtable_id_column="airtable_id"\n',
                encoding="utf-8",
            )
            return Config.load(path)

    def test_valid_config(self):
        self.assertEqual(self.load(), CONFIG)

    def test_url_can_include_installation_prefix(self):
        self.assertEqual(
            self.load("https://grist.example.test/prefix/").grist_url,
            "https://grist.example.test/prefix",
        )

    def test_reject_unsafe_urls(self):
        for url in (
            "http://grist.example.test",
            "https://token@grist.example.test",
            "https://grist.example.test?token=secret",
            "https://grist.example.test#fragment",
            "https://grist.example.test:invalid",
            "https://grist.example.test/a b",
        ):
            with self.subTest(url=url), self.assertRaises(DiscoveryError):
                self.load(url)

    def test_require_technical_table_id(self):
        with self.assertRaises(DiscoveryError):
            self.load(table_id="Enquête 2026")

    def test_missing_and_invalid_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.toml"
            with self.assertRaises(DiscoveryError):
                Config.load(path)
            path.write_text("[airtable", encoding="utf-8")
            with self.assertRaises(DiscoveryError):
                Config.load(path)


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.client = JsonClient("https://example.test", "synthetic-token", "Test")

    def test_authenticated_get_and_timeout(self):
        with patch.object(self.client.opener, "open", return_value=io.BytesIO(b'{"tables": []}')) as op:
            self.assertEqual(self.client.get("api/tables"), {"tables": []})
        request = op.call_args.args[0]
        self.assertEqual(request.full_url, "https://example.test/api/tables")
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.get_header("Authorization"), "Bearer synthetic-token")
        self.assertEqual(op.call_args.kwargs["timeout"], 30)

    def test_reject_invalid_tokens(self):
        for token in ("", " ", "token\ninjected", "two words"):
            with self.subTest(token=token), self.assertRaises(DiscoveryError):
                JsonClient("https://example.test", token, "Test")

    def test_http_errors_do_not_expose_response_body_or_credentials(self):
        for status in (301, 401, 403, 404, 429, 500):
            body = io.BytesIO(b"sensitive response body")
            error = HTTPError("https://example.test", status, "private message", HTTPMessage(), body)
            with self.subTest(status=status):
                with patch.object(self.client.opener, "open", side_effect=error):
                    with self.assertRaises(DiscoveryError) as caught:
                        self.client.get("tables")
                message = str(caught.exception)
                self.assertIn(f"HTTP {status}", message)
                self.assertNotIn("sensitive", message)
                self.assertNotIn("synthetic-token", message)
                self.assertTrue(body.closed)

    def test_network_errors_are_explicit(self):
        for error in (URLError("private detail"), TimeoutError(), RemoteDisconnected("closed")):
            with self.subTest(error=type(error)):
                with patch.object(self.client.opener, "open", side_effect=error):
                    with self.assertRaisesRegex(DiscoveryError, "connexion impossible"):
                        self.client.get("tables")

    def test_invalid_json_and_wrong_shape(self):
        for payload in (b"<html>not JSON</html>", b"[]", b"null", b"\xff"):
            with self.subTest(payload=payload):
                with patch.object(self.client.opener, "open", return_value=io.BytesIO(payload)):
                    with self.assertRaises(DiscoveryError):
                        self.client.get("tables")

    def test_redirect_is_never_followed(self):
        self.assertIsNone(NoRedirect().redirect_request(
            Request("https://example.test"), io.BytesIO(), 302, "",
            HTTPMessage(), "https://other.test",
        ))


class DiscoveryTests(unittest.TestCase):
    @patch.dict(os.environ, {"AIRTABLE_TOKEN": "fake-airtable", "GRIST_TOKEN": "fake-grist"})
    def test_reads_all_schemas_without_reading_records(self):
        responses = [
            {"tables": [{"id": "tblExample", "fields": []}]},
            {"tables": [{"id": "Enquete"}, {"id": "Related Table"}]},
            {"columns": [{"id": "airtable_id", "fields": {"type": "Text"}}]},
            {"columns": [{"id": "Name", "fields": {"type": "Text"}}]},
        ]
        with patch.object(JsonClient, "get", side_effect=responses) as get:
            result = discover(CONFIG)
        self.assertEqual(
            [call.args[0] for call in get.call_args_list],
            [
                "meta/bases/appExample/tables",
                "api/docs/docExample/tables",
                "api/docs/docExample/tables/Enquete/columns",
                "api/docs/docExample/tables/Related%20Table/columns",
            ],
        )
        self.assertEqual(len(result["grist"]["tables"]), 2)
        self.assertEqual(result["grist"]["tables"][1]["columns"][0]["id"], "Name")
        self.assertNotIn("fake-airtable", json.dumps(result))
        self.assertNotIn("fake-grist", json.dumps(result))

    @patch.dict(os.environ, {"AIRTABLE_TOKEN": "fake-airtable", "GRIST_TOKEN": ""})
    def test_missing_second_token_fails_before_network(self):
        with patch.object(JsonClient, "get") as get, self.assertRaises(DiscoveryError):
            discover(CONFIG)
        get.assert_not_called()

    @patch.dict(os.environ, {"AIRTABLE_TOKEN": "fake-airtable", "GRIST_TOKEN": "fake-grist"})
    def test_malformed_schema_is_not_silently_empty(self):
        with patch.object(JsonClient, "get", return_value={"error": "invalid"}):
            with self.assertRaises(DiscoveryError):
                discover(CONFIG)


class CommandTests(unittest.TestCase):
    def run_command(self, directory):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["discover", "--output-dir", str(directory)])
        return code, stdout.getvalue(), stderr.getvalue()

    @patch("fdr_irve.__main__.Config.load", return_value=CONFIG)
    @patch("fdr_irve.__main__.discover", side_effect=lambda config: schema())
    def test_writes_draft_and_preserves_existing_mapping(self, discover_mock, config_mock):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(self.run_command(directory)[0], 0)
            output = Path(directory) / "mapping.proposed.json"
            before = output.read_bytes()
            proposal = json.loads(before)
            self.assertFalse(proposal["approved"])
            self.assertEqual(proposal["status"], "draft")
            snapshot = json.loads((Path(directory) / "schema.json").read_text())
            self.assertIn("discovered_at", snapshot)
            self.assertEqual(self.run_command(directory)[0], 1)
            self.assertEqual(output.read_bytes(), before)
            discover_mock.assert_called_once()

    @patch("fdr_irve.__main__.Config.load", return_value=CONFIG)
    @patch("fdr_irve.__main__.discover")
    def test_blockers_are_saved_and_reported_with_nonzero_exit(self, discover_mock, config_mock):
        snapshot = schema()
        snapshot["airtable"]["tables"][0]["fields"] = []
        discover_mock.return_value = snapshot
        with tempfile.TemporaryDirectory() as directory:
            code, _, error = self.run_command(directory)
            self.assertEqual(code, 2)
            self.assertIn("BLOCAGE", error)
            self.assertTrue((Path(directory) / "mapping.proposed.json").exists())

    @patch("fdr_irve.__main__.Config.load", return_value=CONFIG)
    @patch("fdr_irve.__main__.discover", side_effect=DiscoveryError("Grist: HTTP 403."))
    def test_api_failure_leaves_no_output(self, discover_mock, config_mock):
        with tempfile.TemporaryDirectory() as directory:
            code, _, error = self.run_command(directory)
            self.assertEqual(code, 1)
            self.assertIn("403", error)
            self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
