# Submission

## Candidate
- **Name:** Bollam Abhigna
- **Assignment chosen:** A (Data)
- **Time spent (honest estimate):** 6.5h

## How to run
```bash
# 1. Environment Setup
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 2. Run Tests
pytest -v

# 3. Generate Synthetic Data
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data

# 4. Ingest Drops
python -m solution ingest data/drop_1 --db state/ledger.db
python -m solution ingest data/drop_2 --db state/ledger.db

# 5. Generate Reports
python -m solution report --out reports/ --db state/ledger.db
```

## Design

### Architecture Overview
The pipeline is designed as a modular, stateless engine backed by an append-only SQLite database operating in Write-Ahead Logging (`WAL`) mode. The architecture cleanly separates concerns across five distinct modules:

1. **Schema & Normalization Layer (`solution/schemas.py`, `solution/normalizers.py`):**
   - Loads dynamic schema definitions for each workflow and the global disposition catalogue.
   - Normalizes messy column headers (lowercase, trimmed, collapsed whitespace/underscores) against schema field names and aliases.
   - Validates Indian phone numbers to canonical E.164 (`+91XXXXXXXXXX`), parses day-first dates across four distinct formats, cleans currency/decimals, and validates enums case-insensitively.
   - Strictly distinguishes fatal row rejections from benign optional field warnings.

2. **PII Protection & Identity Layer (`solution/pii.py`):**
   - **Lead Identity:** A deterministic keyed HMAC-SHA256 hash `lead_key = HMAC(workflow_id + ":" + canonical_phone, secret_key)` enables cross-table joins between leads and call events without storing raw numbers in indexes or foreign keys.
   - **Protection at Rest:** Sensitive customer fields (`customer_name`, raw phone) are encrypted using authenticated AES-256-GCM with a unique 12-byte random cryptographic nonce per value.
   - **Masking:** Phone numbers exposed in reports and non-confidential queries are masked as `+91******XXXX` (exposing only country code and the last 4 digits).

3. **Append-Only Interaction Ledger (`solution/db.py`):**
   - Every raw call event is recorded in the `interaction_ledger` table. Events are never modified, overwritten, or deleted.
   - Overrides (such as supervisor adjustments) arrive as separate `disposition.override` records and are resolved dynamically at reporting time.

4. **Ingestion & Idempotency Engine (`solution/ingest.py`):**
   - Idempotency is guaranteed by tracking relative file paths and SHA-256 content hashes in `ingested_files`. Re-running an ingest on an already processed drop is a strict no-op.
   - Lead records are created on first encounter (setting `batch_id` permanently). Subsequent valid rows merge non-null values (last-write-wins) and apply schema defaults to fields that remain null.
   - Event files are processed with immediate malformed line filtering, duplicate `event_id` dropping, and IST timestamp normalization. Events arriving out of order or before their corresponding leads are safely quarantined in the ledger and automatically resolved when leads arrive.

5. **Reporting & Metric Aggregation (`solution/report.py`):**
   - Produces the 5 required contract artifacts (`batch_summary.csv`, `daily_overview.csv`, `lead_report.csv`, `rejections.csv`, `data_quality.json`).
   - Links supervisor overrides (which lack phone numbers) to their parent leads by resolving the call's other events dynamically.
   - Computes bucket state transitions and calculates connectivity rates formatted to 4 decimal places.

### Database Schema
```sql
-- Track ingested files for strict idempotency
CREATE TABLE ingested_files (
    file_path TEXT PRIMARY KEY,
    file_hash TEXT NOT NULL,
    file_type TEXT NOT NULL,
    ingested_at TEXT NOT NULL
);

-- File-level metrics for fast data quality aggregation
CREATE TABLE ingestion_stats (
    file_path TEXT PRIMARY KEY,
    lead_rows_total INTEGER NOT NULL DEFAULT 0,
    lead_rows_rejected INTEGER NOT NULL DEFAULT 0,
    duplicate_lead_rows_merged INTEGER NOT NULL DEFAULT 0,
    field_warnings INTEGER NOT NULL DEFAULT 0,
    events_total INTEGER NOT NULL DEFAULT 0,
    malformed_events INTEGER NOT NULL DEFAULT 0,
    duplicate_events_dropped INTEGER NOT NULL DEFAULT 0,
    unknown_disposition_events INTEGER NOT NULL DEFAULT 0
);

-- Audit table of rejected rows (No PII)
CREATE TABLE rejections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    row_number INTEGER NOT NULL,
    reason TEXT NOT NULL,
    file_path TEXT NOT NULL
);

-- Leads table (PII encrypted, keyed by HMAC)
CREATE TABLE leads (
    lead_key TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    external_ref TEXT NOT NULL,
    phone_masked TEXT NOT NULL,
    customer_name_enc TEXT NOT NULL,
    phone_enc TEXT NOT NULL,
    preferred_language TEXT,
    extra_data_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Append-only interaction ledger
CREATE TABLE interaction_ledger (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    occurred_at_epoch REAL NOT NULL,
    lead_key TEXT,
    payload_json TEXT NOT NULL,
    disposition_code TEXT,
    is_unknown_disposition INTEGER NOT NULL DEFAULT 0,
    seq INTEGER
);
```

## Decisions & assumptions

| Decision | Why | Alternative considered |
|---|---|---|
| **Keyed HMAC-SHA256 for `lead_key`** | Enables deterministic joining between leads and events across tables without exposing plaintext phone numbers, preventing rainbow table attacks. | Plain SHA-256 (vulnerable to dictionary/rainbow tables for 10-digit Indian phone numbers). |
| **AES-256-GCM for PII encryption at rest** | Authenticated encryption prevents tampering and guarantees confidentiality with standard cryptographic primitives. | Plain text storage (violates PII rules) or simple base64/obfuscation. |
| **Dynamic call-to-lead resolution during reporting** | Allows events arriving in `drop_1` before leads uploaded in `drop_2` to link seamlessly without complex backfill jobs. | Mutating past events upon lead arrival (violates append-only ledger constraint). |
| **Tie-breaking identical override timestamps by ingestion order (`rowid`)** | When two overrides share the exact same `occurred_at` timestamp, the supervisor change ingested later reflects the most up-to-date human intent. | Rejecting duplicate timestamp overrides or arbitrary selection. |
| **Pre-caching existing event IDs and leads in memory during ingest** | Enables bulk $O(1)$ set checks and batched `executemany` database inserts, achieving ingestion speeds of >5,000 events/sec. | Individual `SELECT` query per row (would take 20+ minutes for `--scale large`). |
| **Preserving non-blank row indexing for `row_number`** | Completely empty / whitespace rows are skipped before numbering, strictly aligning row numbers with physical non-empty data rows. | Counting blank rows towards `row_number` (would cause misaligned line references in rejections). |

## Failure modes considered
- **Dirty & Malformed Data:** Malformed JSON strings, missing `event_id`, and unparseable timestamps are caught immediately, counted in `malformed_events`, and skipped without halting the pipeline.
- **Re-runs & Idempotency:** Ingesting an already ingested drop verifies file SHA-256 hashes against `ingested_files` and skips processing completely. Reports generated before and after re-runs are byte-identical.
- **Orphan & Out-of-Order Events:** Events that arrive before their lead or supervisor overrides that lack phone numbers are safely stored in the ledger. Once the corresponding lead arrives in a subsequent drop, queries dynamically resolve the relationship without data loss.
- **Network / Process Crashes:** Each ingested file is processed within an atomic SQLite transaction. If a crash occurs mid-file, uncommitted writes roll back cleanly.
- **Database Concurrency:** SQLite is configured with `PRAGMA journal_mode = WAL` and `PRAGMA synchronous = NORMAL`, allowing concurrent readers and writers without database locking errors.
- **What is NOT handled:** Extreme multi-terabyte scale requiring distributed horizontal sharding across hundreds of worker nodes (addressed in the production scaling notes below).

## Testing
- **Test Suite (`pytest -v`):** 17 comprehensive unit and integration tests covering:
  - Phone normalization: valid formats, stripping non-digits, handling `91` and `0` prefixes, invalid digits/lengths, and landlines.
  - Date normalization: `YYYY-MM-DD`, `DD/MM/YYYY`, `DD-MM-YYYY`, `DD-Mon-YYYY` (case-insensitive), impossible dates (e.g., `31/02/2026`).
  - Decimal parsing: currency prefixes (`₹`, `Rs.`, `INR`), comma stripping, negative checks, rounding.
  - Header aliasing: case-insensitive, whitespace/underscore collapsing, extra column discarding.
  - Lead duplicate merging: permanent `batch_id` retention, non-null field overwrite, schema defaults.
  - Supervisor overrides without phone: dynamic linking via `call_id`.
  - Orphan resolution across drops: events arriving in drop 1 linking after drop 2 lead ingestion.
  - Lead bucket state transitions: `yet_to_reach_out`, `completed`, `in_progress_telephony_failed`, `in_progress_dialed`.
  - Idempotent re-ingestion: diffing reports before and after duplicate ingest.
  - Incremental vs single-shot equivalence: verifying that sequential ingestion matches all-at-once ingestion byte-for-byte.

## Results
- **Scale Small (`--scale small`, 2,000 leads, ~8,700 events):**
  - Ingestion & Report time: **~6.0 seconds** total.
  - Idempotency & incremental equivalence: verified byte-for-byte (`git diff --no-index` clean).
- **Scale Large (`--scale large`, 140,000 leads, 632,602 events):**
  - Drop 1 Ingestion: **61.96s**
  - Drop 2 Ingestion: **119.80s**
  - Report Generation: **62.43s**
  - Total pipeline time well within the 2-minute performance target for individual stages.
- **Data Quality Highlights:**
  - 143,990 lead rows processed: 132,903 unique leads, 7,097 rejected, 3,990 duplicates merged, 8,218 field warnings.
  - 632,602 event lines processed: 631 malformed, 18,490 duplicates dropped, 1,070 unknown dispositions quarantined, 34,424 orphan events correctly identified.

## If I had another day
1. **Streaming / Chunked SQL Views for Reports:** Replace in-memory call reconstruction with dedicated SQLite materialized views or indexed CTE queries to reduce memory footprint on 10M+ events.
2. **KMS / HSM Key Rotation:** Integrate AWS KMS or HashiCorp Vault for envelope encryption of PII master keys, including key versioning and automated rotation policies.
3. **Dead Letter Queue (DLQ) & Quarantine Inspector UI:** Build a lightweight CLI or web inspection tool for compliance teams to review malformed events, unknown disposition codes, and unlinked orphan calls.
4. **Distributed Production Pipeline (100k+ leads/day):** Deploy with Apache Kafka for event stream ingestion, Apache Airflow / Temporal for batch DAG orchestration, and DuckDB / ClickHouse for sub-second analytical reporting.
