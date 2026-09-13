# Action Network paper-signature importer

This dependency-free Python script records paper petition signatures in Action
Network. It performs a dry run unless `--commit` is explicitly supplied, checks
whether an existing person has already signed the petition, and disables the
petition autoresponse for every imported signature.

New people are subscribed to the Action Network email list, as required for
this paper-signature workflow. Existing activists retain their current
subscription status because the importer does not send a subscription status
in the API payload.

## Requirements

- Python 3.9 or newer
- An Action Network API key for the group sponsoring the petition
- The petition's Action Network UUID

Action Network API keys are managed under **Start Organizing > API & Sync**.
Keep the key private and do not put it in the CSV or source code.

## Prepare the CSV

Start with `signatures-template.csv`. Required columns are:

- `first_name`
- `last_name`
- `email`

Optional columns are:

- `address`
- `city`
- `state`
- `zip`
- `country`
- `signed_date` in `YYYY-MM-DD` format
- `comments`

Save the file as UTF-8 comma-separated CSV. One person must have one unique
email address. Action Network deduplicates people by email, so two signers who
share an email address cannot be represented as separate signatures.

The physical handwritten marks are not uploaded. Retain the original paper
sheets as the source records.

## Configure and run

Set the credentials for the current terminal session:

```bash
export ACTION_NETWORK_API_KEY="your-secret-api-key"
export ACTION_NETWORK_PETITION_ID="00000000-0000-0000-0000-000000000000"
```

Validate without contacting Action Network. `-n` and `--dry-run` are
equivalent; dry run is also the default when neither mode flag is supplied:

```bash
python3 import_paper_signatures.py signatures.csv -n
```

After reviewing the dry-run output, upload signatures:

```bash
python3 import_paper_signatures.py signatures.csv --commit
```

Committed imports create a timestamped file such as
`signatures.results.20260913T143000.csv` beside the input file. The report
contains masked email addresses and identifies imported, already-signed, and
failed rows. Failed rows cause a nonzero exit status. Existing results files
are never overwritten.

Use a different Action Network source code if desired:

```bash
python3 import_paper_signatures.py signatures.csv --source paper-door-to-door -n
```

Run the automated tests from this directory with:

```bash
python3 -m unittest -v
```
