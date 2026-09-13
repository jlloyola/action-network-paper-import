#!/usr/bin/env python3
"""Import paper petition signatures into Action Network from a CSV file.

The script is intentionally dependency-free. It performs a dry run unless
--commit is supplied, and it never enables Action Network's autoresponse.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence
from urllib import error, parse, request
from zoneinfo import ZoneInfo


API_ROOT = "https://actionnetwork.org/api/v2"
API_KEY_ENV = "ACTION_NETWORK_API_KEY"
PETITION_ID_ENV = "ACTION_NETWORK_PETITION_ID"
DEFAULT_SOURCE = "paper"
DEFAULT_TIMEZONE = "America/Chicago"

REQUIRED_COLUMNS = ("first_name", "last_name", "email")
OPTIONAL_COLUMNS = (
    "address",
    "city",
    "state",
    "zip",
    "country",
    "signed_date",
    "comments",
)
KNOWN_COLUMNS = set(REQUIRED_COLUMNS + OPTIONAL_COLUMNS)
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
ZIP_PATTERN = re.compile(r"^\d{5}(?:-\d{4})?$")


class ImportErrorBase(Exception):
    """Base class for expected importer errors."""


class CsvValidationError(ImportErrorBase):
    """Raised when the input CSV is not safe to import."""


class ApiError(ImportErrorBase):
    """Raised when Action Network returns an unsuccessful response."""


@dataclass(frozen=True)
class SignatureRow:
    row_number: int
    first_name: str
    last_name: str
    email: str
    address: str = ""
    city: str = ""
    state: str = ""
    zip_code: str = ""
    country: str = ""
    signed_date: str = ""
    comments: str = ""


@dataclass(frozen=True)
class ImportResult:
    row_number: int
    email: str
    status: str
    person_id: str = ""
    signature_id: str = ""
    message: str = ""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Import paper petition signatures from CSV into Action Network. "
            "Dry-run mode is the default."
        )
    )
    parser.add_argument("csv_file", type=Path, help="UTF-8 CSV file to import")
    parser.add_argument(
        "--petition-id",
        default=os.environ.get(PETITION_ID_ENV, ""),
        help=f"Action Network petition UUID (or set {PETITION_ID_ENV})",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="validate and preview without contacting Action Network (default)",
    )
    mode.add_argument(
        "--commit",
        action="store_true",
        help="upload validated signatures to Action Network",
    )
    parser.add_argument(
        "--source",
        default=DEFAULT_SOURCE,
        help=f"Action Network source code (default: {DEFAULT_SOURCE})",
    )
    parser.add_argument(
        "--timezone",
        default=DEFAULT_TIMEZONE,
        help=f"timezone used for signed_date (default: {DEFAULT_TIMEZONE})",
    )
    parser.add_argument(
        "--results",
        type=Path,
        help="results CSV path (default: timestamped file beside INPUT)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="HTTP timeout in seconds (default: 30)",
    )
    return parser


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def validate_petition_id(raw_value: str) -> str:
    value = clean(raw_value)
    if not value:
        raise CsvValidationError(
            f"A petition ID is required via --petition-id or {PETITION_ID_ENV}."
        )
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise CsvValidationError(
            "The petition ID must be an Action Network UUID, not the public petition slug."
        ) from exc


def validate_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (KeyError, ValueError) as exc:
        raise CsvValidationError(f"Unknown timezone: {name}") from exc


def load_rows(path: Path) -> list[SignatureRow]:
    if not path.is_file():
        raise CsvValidationError(f"CSV file not found: {path}")

    try:
        handle = path.open("r", encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise CsvValidationError(f"Unable to open CSV file: {exc}") from exc

    errors: list[str] = []
    rows: list[SignatureRow] = []
    seen_emails: dict[str, int] = {}

    with handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise CsvValidationError("CSV file is empty or has no header row.")

        headers = [clean(header).lower() for header in reader.fieldnames]
        missing = [column for column in REQUIRED_COLUMNS if column not in headers]
        duplicate_headers = sorted(
            {header for header in headers if headers.count(header) > 1}
        )
        if missing:
            errors.append("missing required columns: " + ", ".join(missing))
        if duplicate_headers:
            errors.append("duplicate columns: " + ", ".join(duplicate_headers))
        if errors:
            raise CsvValidationError("CSV header error: " + "; ".join(errors))

        reader.fieldnames = headers
        for row_number, raw in enumerate(reader, start=2):
            if None in raw:
                errors.append(
                    f"row {row_number}: too many values; check quoting around commas"
                )
                continue

            values = {key: clean(value) for key, value in raw.items() if key in KNOWN_COLUMNS}
            if not any(values.values()):
                continue

            first_name = values.get("first_name", "")
            last_name = values.get("last_name", "")
            email = values.get("email", "").lower()
            zip_code = values.get("zip", "")
            signed_date = values.get("signed_date", "")
            address_parts = (
                values.get("address", ""),
                values.get("city", ""),
                values.get("state", ""),
                values.get("country", ""),
            )

            if not first_name:
                errors.append(f"row {row_number}: first_name is required")
            if not last_name:
                errors.append(f"row {row_number}: last_name is required")
            if not EMAIL_PATTERN.fullmatch(email):
                errors.append(f"row {row_number}: invalid email address")
            if zip_code and not ZIP_PATTERN.fullmatch(zip_code):
                errors.append(f"row {row_number}: ZIP must be 12345 or 12345-6789")
            if any(address_parts) and not zip_code:
                errors.append(
                    f"row {row_number}: zip is required when address fields are present"
                )
            if values.get("state", "") and len(values["state"]) != 2:
                errors.append(f"row {row_number}: state must be a two-letter code")
            if values.get("country", "") and len(values["country"]) != 2:
                errors.append(f"row {row_number}: country must be a two-letter code")
            if signed_date:
                try:
                    date.fromisoformat(signed_date)
                except ValueError:
                    errors.append(f"row {row_number}: signed_date must be YYYY-MM-DD")

            if email in seen_emails:
                errors.append(
                    f"row {row_number}: duplicate email (first seen on row "
                    f"{seen_emails[email]})"
                )
            elif email:
                seen_emails[email] = row_number

            rows.append(
                SignatureRow(
                    row_number=row_number,
                    first_name=first_name,
                    last_name=last_name,
                    email=email,
                    address=values.get("address", ""),
                    city=values.get("city", ""),
                    state=values.get("state", ""),
                    zip_code=zip_code,
                    country=values.get("country", "").upper(),
                    signed_date=signed_date,
                    comments=values.get("comments", ""),
                )
            )

    if not rows and not errors:
        errors.append("CSV contains no signer rows")
    if errors:
        formatted = "\n".join(f"  - {message}" for message in errors)
        raise CsvValidationError(f"CSV validation failed:\n{formatted}")
    return rows


def signed_datetime(value: str, timezone: ZoneInfo) -> str:
    signed = date.fromisoformat(value)
    # Noon avoids ambiguous or nonexistent wall-clock times at DST transitions.
    local = datetime.combine(signed, datetime_time(hour=12), tzinfo=timezone)
    return local.isoformat()


def build_payload(
    row: SignatureRow, source: str, timezone: ZoneInfo
) -> dict[str, Any]:
    person: dict[str, Any] = {
        "given_name": row.first_name,
        "family_name": row.last_name,
        # No status is passed. Action Network subscribes new people while
        # preserving the status of people already on the list.
        "email_addresses": [{"address": row.email}],
    }

    if any((row.address, row.city, row.state, row.zip_code, row.country)):
        postal: dict[str, Any] = {}
        if row.address:
            postal["address_lines"] = [row.address]
        if row.city:
            postal["locality"] = row.city
        if row.state:
            postal["region"] = row.state.upper()
        if row.zip_code:
            postal["postal_code"] = row.zip_code
        if row.country:
            postal["country"] = row.country
        person["postal_addresses"] = [postal]

    payload: dict[str, Any] = {
        "person": person,
        "action_network:referrer_data": {"source": source},
        "triggers": {"autoresponse": {"enabled": False}},
    }
    if row.signed_date:
        payload["created_date"] = signed_datetime(row.signed_date, timezone)
    if row.comments:
        payload["comments"] = row.comments
    return payload


def native_identifier(resource: dict[str, Any]) -> str:
    for identifier in resource.get("identifiers", []):
        if identifier.startswith("action_network:"):
            return identifier.split(":", 1)[1]
    return ""


def mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    visible = local[:1]
    return f"{visible}{'*' * max(1, len(local) - 1)}@{domain}"


class ActionNetworkClient:
    def __init__(self, api_key: str, timeout: float = 30.0) -> None:
        self.api_key = api_key
        self.timeout = timeout

    def _request_json(
        self, method: str, url: str, payload: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {
            "Accept": "application/hal+json",
            "OSDI-API-Token": self.api_key,
        }
        if data is not None:
            headers["Content-Type"] = "application/json"

        last_error: Optional[Exception] = None
        for attempt in range(4):
            try:
                req = request.Request(url, data=data, headers=headers, method=method)
                with request.urlopen(req, timeout=self.timeout) as response:
                    body = response.read()
                    return json.loads(body) if body else {}
            except error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if exc.code == 429 or 500 <= exc.code < 600:
                    last_error = ApiError(f"HTTP {exc.code}: {body[:300]}")
                else:
                    raise ApiError(f"HTTP {exc.code}: {body[:500]}") from exc
            except (error.URLError, TimeoutError) as exc:
                last_error = exc

            if attempt < 3:
                time.sleep(2**attempt)

        raise ApiError(f"request failed after retries: {last_error}")

    def find_person(self, email: str) -> Optional[dict[str, Any]]:
        escaped = email.replace("'", "''")
        query = parse.urlencode({"filter": f"email_address eq '{escaped}'"})
        response = self._request_json("GET", f"{API_ROOT}/people/?{query}")
        people = response.get("_embedded", {}).get("osdi:people", [])
        if len(people) > 1:
            raise ApiError(f"Action Network returned multiple people for {email}")
        return people[0] if people else None

    def get_petition(self, petition_id: str) -> dict[str, Any]:
        return self._request_json("GET", f"{API_ROOT}/petitions/{petition_id}")

    def person_signed_petition(
        self, person: dict[str, Any], petition_id: str
    ) -> bool:
        href = person.get("_links", {}).get("osdi:signatures", {}).get("href")
        while href:
            response = self._request_json("GET", href)
            signatures = response.get("_embedded", {}).get("osdi:signatures", [])
            for signature in signatures:
                if signature.get("action_network:petition_id") == petition_id:
                    return True
                petition_href = (
                    signature.get("_links", {}).get("osdi:petition", {}).get("href", "")
                )
                if petition_href.rstrip("/").endswith(f"/{petition_id}"):
                    return True
            href = response.get("_links", {}).get("next", {}).get("href")
        return False

    def create_signature(
        self, petition_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        endpoint = f"{API_ROOT}/petitions/{petition_id}/signatures/"
        return self._request_json("POST", endpoint, payload)


def dry_run(rows: Sequence[SignatureRow], source: str, timezone: ZoneInfo) -> None:
    print(f"DRY RUN: validated {len(rows)} signature(s); no API calls were made.")
    print(f"Source code: {source}")
    for row in rows:
        payload = build_payload(row, source, timezone)
        details = [f"row {row.row_number}", mask_email(row.email)]
        if "created_date" in payload:
            details.append(row.signed_date)
        print("  " + " | ".join(details))
    print("Run again with --commit to upload these signatures.")


def commit_rows(
    rows: Sequence[SignatureRow],
    petition_id: str,
    source: str,
    timezone: ZoneInfo,
    client: ActionNetworkClient,
) -> list[ImportResult]:
    results: list[ImportResult] = []
    for index, row in enumerate(rows, start=1):
        masked = mask_email(row.email)
        print(f"[{index}/{len(rows)}] {masked}: checking...", flush=True)
        try:
            person = client.find_person(row.email)
            if person and client.person_signed_petition(person, petition_id):
                result = ImportResult(
                    row_number=row.row_number,
                    email=masked,
                    status="already_signed",
                    person_id=native_identifier(person),
                    message="Existing petition signature was preserved.",
                )
            else:
                response = client.create_signature(
                    petition_id, build_payload(row, source, timezone)
                )
                person_id = clean(response.get("action_network:person_id"))
                if not person_id and person:
                    person_id = native_identifier(person)
                result = ImportResult(
                    row_number=row.row_number,
                    email=masked,
                    status="imported",
                    person_id=person_id,
                    signature_id=native_identifier(response),
                )
        except ApiError as exc:
            result = ImportResult(
                row_number=row.row_number,
                email=masked,
                status="failed",
                message=str(exc),
            )
        print(f"    {result.status}")
        results.append(result)
    return results


def default_results_path(input_path: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    return input_path.with_name(f"{input_path.stem}.results.{timestamp}.csv")


def write_results(path: Path, results: Iterable[ImportResult]) -> None:
    fieldnames = [
        "row_number",
        "email",
        "status",
        "person_id",
        "signature_id",
        "message",
    ]
    if path.exists():
        raise CsvValidationError(
            f"Results file already exists; choose a new --results path: {path}"
        )
    try:
        with path.open("x", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for result in results:
                writer.writerow(
                    {field: getattr(result, field) for field in fieldnames}
                )
    except OSError as exc:
        raise CsvValidationError(f"Unable to write results file: {exc}") from exc


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        petition_id = validate_petition_id(args.petition_id)
        timezone = validate_timezone(args.timezone)
        rows = load_rows(args.csv_file)
        source = clean(args.source)
        if not source:
            raise CsvValidationError("--source cannot be blank")
        if args.timeout <= 0:
            raise CsvValidationError("--timeout must be greater than zero")

        # -n documents the user's intent, but dry run is also the safe default.
        if args.dry_run or not args.commit:
            dry_run(rows, source, timezone)
            return 0

        api_key = os.environ.get(API_KEY_ENV, "").strip()
        if not api_key:
            raise CsvValidationError(
                f"{API_KEY_ENV} must be set when using --commit."
            )

        client = ActionNetworkClient(api_key=api_key, timeout=args.timeout)
        petition = client.get_petition(petition_id)
        petition_title = clean(petition.get("title")) or petition_id
        print(f"Petition: {petition_title}")
        results = commit_rows(rows, petition_id, source, timezone, client)
        results_path = args.results or default_results_path(args.csv_file)
        write_results(results_path, results)

        imported = sum(result.status == "imported" for result in results)
        existing = sum(result.status == "already_signed" for result in results)
        failed = sum(result.status == "failed" for result in results)
        print(
            f"Finished: {imported} imported, {existing} already signed, "
            f"{failed} failed."
        )
        print(f"Results: {results_path}")
        return 1 if failed else 0
    except ImportErrorBase as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    sys.exit(main())
