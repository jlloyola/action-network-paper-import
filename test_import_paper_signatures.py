import contextlib
import csv
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import import_paper_signatures as importer


PETITION_ID = "9f837109-710d-442f-8a99-857a21f36d25"


class CsvTests(unittest.TestCase):
    def write_csv(self, text):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "signatures.csv"
        path.write_text(text, encoding="utf-8")
        return path

    def test_loads_valid_csv_and_normalizes_email(self):
        path = self.write_csv(
            "first_name,last_name,email,zip,signed_date\n"
            " Jane , Doe , JANE@EXAMPLE.COM ,78717,2026-09-12\n"
        )
        rows = importer.load_rows(path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].email, "jane@example.com")
        self.assertEqual(rows[0].row_number, 2)

    def test_duplicate_email_fails_before_import(self):
        path = self.write_csv(
            "first_name,last_name,email\n"
            "Jane,Doe,jane@example.com\n"
            "Janet,Doe,JANE@example.com\n"
        )
        with self.assertRaisesRegex(importer.CsvValidationError, "duplicate email"):
            importer.load_rows(path)

    def test_invalid_date_and_email_are_reported_together(self):
        path = self.write_csv(
            "first_name,last_name,email,signed_date\n"
            "Jane,Doe,not-an-email,09/12/2026\n"
        )
        with self.assertRaises(importer.CsvValidationError) as raised:
            importer.load_rows(path)
        message = str(raised.exception)
        self.assertIn("invalid email", message)
        self.assertIn("YYYY-MM-DD", message)

    def test_address_fields_require_zip(self):
        path = self.write_csv(
            "first_name,last_name,email,address,city,state,country\n"
            "Jane,Doe,jane@example.com,123 Main Street,Austin,TX,US\n"
        )
        with self.assertRaisesRegex(importer.CsvValidationError, "zip is required"):
            importer.load_rows(path)


class PayloadTests(unittest.TestCase):
    def test_payload_disables_autoresponse_and_marks_source(self):
        row = importer.SignatureRow(
            row_number=2,
            first_name="Jane",
            last_name="Doe",
            email="jane@example.com",
            zip_code="78717",
            signed_date="2026-09-12",
        )
        payload = importer.build_payload(
            row, "paper", ZoneInfo("America/Chicago")
        )
        self.assertFalse(payload["triggers"]["autoresponse"]["enabled"])
        self.assertEqual(payload["action_network:referrer_data"]["source"], "paper")
        self.assertEqual(
            payload["person"]["email_addresses"],
            [{"address": "jane@example.com"}],
        )
        self.assertNotIn("status", payload["person"]["email_addresses"][0])
        self.assertEqual(payload["created_date"], "2026-09-12T12:00:00-05:00")


class CommandLineTests(unittest.TestCase):
    def write_csv(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "signatures.csv"
        path.write_text(
            "first_name,last_name,email,zip,signed_date\n"
            "Jane,Doe,jane@example.com,78717,2026-09-12\n",
            encoding="utf-8",
        )
        return path

    def test_short_n_is_a_dry_run_and_never_constructs_client(self):
        path = self.write_csv()
        output = io.StringIO()
        with mock.patch.object(
            importer, "ActionNetworkClient"
        ) as client_constructor, contextlib.redirect_stdout(output):
            status = importer.main(
                [str(path), "--petition-id", PETITION_ID, "-n"]
            )
        self.assertEqual(status, 0)
        client_constructor.assert_not_called()
        self.assertIn("no API calls were made", output.getvalue())

    def test_no_mode_flag_is_also_a_dry_run(self):
        path = self.write_csv()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = importer.main([str(path), "--petition-id", PETITION_ID])
        self.assertEqual(status, 0)
        self.assertIn("DRY RUN", output.getvalue())

    def test_commit_requires_api_key(self):
        path = self.write_csv()
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                importer.main(
                    [str(path), "--petition-id", PETITION_ID, "--commit"]
                )
        self.assertEqual(raised.exception.code, 2)
        self.assertIn(importer.API_KEY_ENV, stderr.getvalue())


class CommitTests(unittest.TestCase):
    def test_existing_signature_is_skipped(self):
        row = importer.SignatureRow(
            row_number=2,
            first_name="Jane",
            last_name="Doe",
            email="jane@example.com",
        )
        person = {
            "identifiers": ["action_network:person-1"],
            "_links": {"osdi:signatures": {"href": "unused"}},
        }
        client = mock.Mock()
        client.find_person.return_value = person
        client.person_signed_petition.return_value = True

        with contextlib.redirect_stdout(io.StringIO()):
            results = importer.commit_rows(
                [row],
                PETITION_ID,
                "paper",
                ZoneInfo("America/Chicago"),
                client,
            )

        self.assertEqual(results[0].status, "already_signed")
        client.create_signature.assert_not_called()

    def test_results_writer_refuses_to_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "results.csv"
            path.write_text("existing", encoding="utf-8")
            with self.assertRaisesRegex(
                importer.CsvValidationError, "already exists"
            ):
                importer.write_results(path, [])


if __name__ == "__main__":
    unittest.main()
