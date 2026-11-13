# AI Usage Disclosure

## Tools used
- Google Antigravity / Gemini 3.8 Flash (Pair programming assistant)

## What I used them for
- Generating initial boilerplate patterns for `argparse` CLI subcommands (`ingest` and `report`).
- Generating regex templates for Indian phone numbers, multiple date patterns (e.g. `DD-Mon-YYYY`), and currency prefixes (`₹`, `Rs.`, `INR`).
- Scaffolding the `pytest` test suite structure in `tests/test_pipeline.py` and `tests/test_normalizers.py`.

## What I wrote or decided myself
- **Database Architecture & Ledger Model:** Designed the SQLite schema with WAL mode, indexing strategy, and the append-only `interaction_ledger` table ensuring raw events are never updated or deleted.
- **PII Protection Scheme:** Selected the deterministic keyed HMAC-SHA256 design (`lead_key`) to enable joins across leads and events without plaintext phone storage, combined with AES-256-GCM encryption at rest.
- **Out-of-Order & Cross-Drop Event Linking:** Designed the dynamic call-to-lead resolution mechanism that allows supervisor overrides (which arrive without phone numbers) and events arriving prior to lead ingestion to resolve cleanly upon report generation without retroactive mutation.
- **Tie-Breaking & Effective Disposition Logic:** Formulated the prioritization hierarchy: latest `disposition.override` by occurred timestamp with ingestion sequence tie-break, falling back to latest `disposition.set`, while ignoring unknown catalogue codes.
- **Data Quality Invariants:** Enforced strict mathematical consistency across `lead_rows_total = lead_rows_rejected + unique_leads + duplicate_lead_rows_merged`.

## Where the AI was wrong or unhelpful
- In the initial SQLite schema draft for `interaction_ledger`, the assistant attempted to define two primary key columns (`event_id TEXT PRIMARY KEY` and `seq INTEGER PRIMARY KEY AUTOINCREMENT`), which is invalid in SQLite syntax (`OperationalError: table has more than one primary key`). I corrected this by making `event_id` the sole primary key and using SQLite's native `rowid` for ingestion sequence tie-breaking.
- In the test suite for duplicate merging, the assistant initially omitted `customer_name` in a duplicate batch row, which caused the row to be rejected as `missing_required:customer_name` rather than merged. I identified the cause and provided valid required fields so non-null optional field overwrites could be tested correctly.

## How I verified AI-generated code
- **Automated Tests:** Executed 17 comprehensive unit and integration tests using `pytest` covering phone parsing, date formats, currency stripping, header aliasing, PII hashing/encryption, override resolution, and bucket state transitions.
- **End-to-End Idempotency & Incremental Verification:** Generated test data using `generate_data.py`, verified re-runs were identical with zero state drift, and validated that sequential ingestion (`drop_1` then `drop_2`) matched fresh single-shot ingestion byte-for-byte via `git diff --no-index`.
- **Large-Scale Performance Benchmark:** Benchmarked ingestion and reporting on `--scale large` (~140,000 leads, ~630,000 events) to verify stability and sub-2-minute execution.
- **Code Review:** Reviewed every file line by line to verify strict adherence to Python 3.11+ type annotations, error handling, and security constraints.
