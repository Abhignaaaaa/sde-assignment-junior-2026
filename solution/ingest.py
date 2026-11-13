"""Lead and event ingestion pipeline with deduplication, PII protection, and idempotency."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from solution.config import IST
from solution.db import get_db_connection, init_db
from solution.normalizers import (
    is_blank_row,
    normalize_phone,
    validate_and_normalize_row,
)
from solution.pii import compute_lead_key, encrypt_field, mask_phone
from solution.schemas import SchemaRegistry, WorkflowSchema


def compute_file_hash(filepath: str) -> str:
    """Compute SHA-256 hash of a file."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def parse_event_timestamp(ts_str: str) -> Optional[Tuple[datetime, str, float]]:
    """
    Parse an event timestamp string into IST datetime, ISO string, and epoch float.
    Handles:
      - UTC (...Z)
      - Explicit offset (... +05:30)
      - Naive (YYYY-MM-DD HH:MM:SS) -> treated as IST
    """
    s = ts_str.strip()
    if not s:
        return None

    try:
        if s.endswith("Z"):
            dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
            dt_ist = dt.astimezone(IST)
        elif "+" in s or (s.count("-") > 2 and "T" in s):
            dt = datetime.fromisoformat(s)
            dt_ist = dt.astimezone(IST)
        else:
            # Naive timestamp: treat as IST
            if "T" in s:
                dt = datetime.fromisoformat(s)
            else:
                dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
            dt_ist = dt.replace(tzinfo=IST)

        iso_str = dt_ist.isoformat()
        return dt_ist, iso_str, dt_ist.timestamp()
    except Exception:
        return None


def ingest_lead_file(
    conn: sqlite3.Connection,
    filepath: str,
    file_rel_key: str,
    file_hash: str,
    workflow_id: str,
    schema: WorkflowSchema,
    leads_cache: Dict[str, dict],
    new_leads_to_insert: List[dict],
    leads_to_update: List[dict],
) -> dict:
    """Ingest a single lead CSV file."""
    batch_id = Path(filepath).stem
    stats = {
        "lead_rows_total": 0,
        "lead_rows_rejected": 0,
        "duplicate_lead_rows_merged": 0,
        "field_warnings": 0,
    }

    rejections_to_insert = []
    row_num = 0

    with open(filepath, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        try:
            raw_header = next(reader)
        except StopIteration:
            return stats

        headers = [h.strip() for h in raw_header]

        for row in reader:
            if is_blank_row(row):
                continue

            row_num += 1
            stats["lead_rows_total"] += 1

            raw_dict = {}
            for idx, val in enumerate(row):
                if idx < len(headers):
                    raw_dict[headers[idx]] = val

            val_res = validate_and_normalize_row(raw_dict, schema)

            if val_res.is_rejected:
                stats["lead_rows_rejected"] += 1
                rejections_to_insert.append(
                    (
                        workflow_id,
                        batch_id,
                        row_num,
                        val_res.rejection_reason,
                        file_rel_key,
                    )
                )
            else:
                canon_phone = val_res.values["phone"]
                lead_key = compute_lead_key(workflow_id, canon_phone)
                stats["field_warnings"] += val_res.field_warnings

                if lead_key in leads_cache:
                    # Duplicate row: merge
                    stats["duplicate_lead_rows_merged"] += 1
                    cached = leads_cache[lead_key]

                    # Overwrite only non-null fields
                    for field in schema.fields:
                        new_val = val_res.values.get(field.name)
                        if new_val is not None:
                            if field.name == "customer_name":
                                cached["customer_name_enc"] = encrypt_field(new_val)
                            elif field.name == "preferred_language":
                                cached["preferred_language"] = new_val
                            elif field.reference:
                                cached["external_ref"] = str(new_val)
                            elif field.name != "phone":
                                extra = json.loads(cached.get("extra_data_json") or "{}")
                                extra[field.name] = new_val
                                cached["extra_data_json"] = json.dumps(extra)

                    cached["updated_at"] = datetime.now(IST).isoformat()
                    leads_to_update.append(cached)

                else:
                    # New lead: apply defaults to still null fields
                    for field in schema.fields:
                        if val_res.values.get(field.name) is None and field.default is not None:
                            val_res.values[field.name] = field.default

                    now_str = datetime.now(IST).isoformat()
                    extra_data = {}
                    for field in schema.fields:
                        if field.name not in ("customer_name", "phone", "preferred_language") and not field.reference:
                            extra_data[field.name] = val_res.values.get(field.name)

                    ref_val = str(val_res.values.get(schema.reference_field, ""))
                    lead_record = {
                        "lead_key": lead_key,
                        "workflow_id": workflow_id,
                        "batch_id": batch_id,
                        "external_ref": ref_val,
                        "phone_masked": mask_phone(canon_phone),
                        "customer_name_enc": encrypt_field(val_res.values.get("customer_name")),
                        "phone_enc": encrypt_field(canon_phone),
                        "preferred_language": val_res.values.get("preferred_language"),
                        "extra_data_json": json.dumps(extra_data),
                        "created_at": now_str,
                        "updated_at": now_str,
                    }
                    leads_cache[lead_key] = lead_record
                    new_leads_to_insert.append(lead_record)

    # Insert rejections
    if rejections_to_insert:
        conn.executemany(
            """
            INSERT INTO rejections (workflow_id, batch_id, row_number, reason, file_path)
            VALUES (?, ?, ?, ?, ?)
            """,
            rejections_to_insert,
        )

    # Record file stats and ingestion
    conn.execute(
        """
        INSERT INTO ingestion_stats (
            file_path, lead_rows_total, lead_rows_rejected,
            duplicate_lead_rows_merged, field_warnings
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            file_rel_key,
            stats["lead_rows_total"],
            stats["lead_rows_rejected"],
            stats["duplicate_lead_rows_merged"],
            stats["field_warnings"],
        ),
    )

    conn.execute(
        """
        INSERT INTO ingested_files (file_path, file_hash, file_type, ingested_at)
        VALUES (?, ?, 'lead', ?)
        """,
        (file_rel_key, file_hash, datetime.now(IST).isoformat()),
    )

    return stats


def ingest_event_file(
    conn: sqlite3.Connection,
    filepath: str,
    file_rel_key: str,
    file_hash: str,
    registry: SchemaRegistry,
    seen_event_ids: Set[str],
) -> dict:
    """Ingest a single event JSONL file."""
    stats = {
        "events_total": 0,
        "malformed_events": 0,
        "duplicate_events_dropped": 0,
        "unknown_disposition_events": 0,
    }

    events_to_insert = []

    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line_str = line.strip()
            if not line_str:
                continue

            stats["events_total"] += 1

            # 1. Malformed check
            try:
                data = json.loads(line_str)
            except Exception:
                stats["malformed_events"] += 1
                continue

            if not isinstance(data, dict):
                stats["malformed_events"] += 1
                continue

            event_id = data.get("event_id")
            if not event_id or not isinstance(event_id, str):
                stats["malformed_events"] += 1
                continue

            occurred_at_raw = data.get("occurred_at")
            if not occurred_at_raw or not isinstance(occurred_at_raw, str):
                stats["malformed_events"] += 1
                continue

            parsed_ts = parse_event_timestamp(occurred_at_raw)
            if parsed_ts is None:
                stats["malformed_events"] += 1
                continue

            dt_ist, dt_ist_str, epoch = parsed_ts

            # 2. Duplicate check
            if event_id in seen_event_ids:
                stats["duplicate_events_dropped"] += 1
                continue

            seen_event_ids.add(event_id)

            workflow_id = data.get("workflow_id", "")
            call_id = data.get("call_id", "")
            event_type = data.get("event_type", "")
            payload = data.get("payload", {})
            if not isinstance(payload, dict):
                payload = {}

            # 3. Phone & lead_key
            raw_phone = data.get("phone")
            lead_key = None
            if raw_phone and isinstance(raw_phone, str):
                canon_phone, is_valid_phone = normalize_phone(raw_phone)
                if is_valid_phone:
                    lead_key = compute_lead_key(workflow_id, canon_phone)

            # 4. Unknown disposition check
            disp_code = None
            is_unknown = 0
            if event_type.startswith("disposition."):
                raw_code = payload.get("disposition_code")
                if raw_code and isinstance(raw_code, str):
                    if registry.is_known_disposition(workflow_id, raw_code):
                        disp_code = raw_code
                    else:
                        stats["unknown_disposition_events"] += 1
                        is_unknown = 1
                else:
                    stats["unknown_disposition_events"] += 1
                    is_unknown = 1

            events_to_insert.append(
                (
                    event_id,
                    event_type,
                    workflow_id,
                    call_id,
                    dt_ist_str,
                    epoch,
                    lead_key,
                    json.dumps(payload),
                    disp_code,
                    is_unknown,
                )
            )

    if events_to_insert:
        conn.executemany(
            """
            INSERT INTO interaction_ledger (
                event_id, event_type, workflow_id, call_id,
                occurred_at, occurred_at_epoch, lead_key,
                payload_json, disposition_code, is_unknown_disposition
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            events_to_insert,
        )

    # Record stats and ingestion
    conn.execute(
        """
        INSERT INTO ingestion_stats (
            file_path, events_total, malformed_events,
            duplicate_events_dropped, unknown_disposition_events
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            file_rel_key,
            stats["events_total"],
            stats["malformed_events"],
            stats["duplicate_events_dropped"],
            stats["unknown_disposition_events"],
        ),
    )

    conn.execute(
        """
        INSERT INTO ingested_files (file_path, file_hash, file_type, ingested_at)
        VALUES (?, ?, 'event', ?)
        """,
        (file_rel_key, file_hash, datetime.now(IST).isoformat()),
    )

    return stats


def ingest_drop(
    drop_dir: str,
    db_path: str,
    schemas_dir: str,
) -> dict:
    """Ingest a drop directory containing leads and events."""
    drop_dir = os.path.abspath(drop_dir)
    if not os.path.exists(drop_dir):
        raise FileNotFoundError(f"Drop directory not found: {drop_dir}")

    drop_name = os.path.basename(os.path.normpath(drop_dir))
    registry = SchemaRegistry(schemas_dir)
    conn = get_db_connection(db_path)
    init_db(conn)

    # Load already ingested files
    cursor = conn.execute("SELECT file_path, file_hash FROM ingested_files")
    ingested_files_map = {row["file_path"]: row["file_hash"] for row in cursor.fetchall()}

    # Load existing event IDs
    cursor = conn.execute("SELECT event_id FROM interaction_ledger")
    seen_event_ids: Set[str] = {row["event_id"] for row in cursor.fetchall()}

    # Load existing leads into memory cache for fast deduplication
    leads_cache: Dict[str, dict] = {}
    cursor = conn.execute(
        """
        SELECT lead_key, workflow_id, batch_id, external_ref, phone_masked,
               customer_name_enc, phone_enc, preferred_language, extra_data_json,
               created_at, updated_at
        FROM leads
        """
    )
    for row in cursor.fetchall():
        leads_cache[row["lead_key"]] = dict(row)

    # Discover lead files
    leads_base = os.path.join(drop_dir, "leads")
    lead_files: List[Tuple[str, str, str]] = []  # (rel_key, full_path, workflow_id)
    if os.path.exists(leads_base):
        for wf_dir in sorted(os.listdir(leads_base)):
            wf_path = os.path.join(leads_base, wf_dir)
            if os.path.isdir(wf_path):
                for fname in sorted(os.listdir(wf_path)):
                    if fname.endswith(".csv"):
                        full_p = os.path.join(wf_path, fname)
                        rel_k = f"{drop_name}/leads/{wf_dir}/{fname}"
                        lead_files.append((rel_k, full_p, wf_dir))

    # Discover event files
    events_base = os.path.join(drop_dir, "events")
    event_files: List[Tuple[str, str]] = []  # (rel_key, full_path)
    if os.path.exists(events_base):
        for fname in sorted(os.listdir(events_base)):
            if fname.endswith(".jsonl"):
                full_p = os.path.join(events_base, fname)
                rel_k = f"{drop_name}/events/{fname}"
                event_files.append((rel_k, full_p))

    # Process leads
    new_leads_to_insert: List[dict] = []
    leads_to_update: List[dict] = []

    for rel_k, full_p, wf_id in lead_files:
        fhash = compute_file_hash(full_p)
        if rel_k in ingested_files_map:
            # File already ingested with same name
            if ingested_files_map[rel_k] == fhash:
                continue

        wf_schema = registry.get_workflow(wf_id)
        if not wf_schema:
            continue

        with conn:
            ingest_lead_file(
                conn=conn,
                filepath=full_p,
                file_rel_key=rel_k,
                file_hash=fhash,
                workflow_id=wf_id,
                schema=wf_schema,
                leads_cache=leads_cache,
                new_leads_to_insert=new_leads_to_insert,
                leads_to_update=leads_to_update,
            )

    # Flush new leads and updates to DB
    with conn:
        if new_leads_to_insert:
            conn.executemany(
                """
                INSERT INTO leads (
                    lead_key, workflow_id, batch_id, external_ref,
                    phone_masked, customer_name_enc, phone_enc,
                    preferred_language, extra_data_json, created_at, updated_at
                ) VALUES (
                    :lead_key, :workflow_id, :batch_id, :external_ref,
                    :phone_masked, :customer_name_enc, :phone_enc,
                    :preferred_language, :extra_data_json, :created_at, :updated_at
                )
                """,
                new_leads_to_insert,
            )
            new_leads_to_insert.clear()

        if leads_to_update:
            conn.executemany(
                """
                UPDATE leads SET
                    customer_name_enc = :customer_name_enc,
                    preferred_language = :preferred_language,
                    external_ref = :external_ref,
                    extra_data_json = :extra_data_json,
                    updated_at = :updated_at
                WHERE lead_key = :lead_key
                """,
                leads_to_update,
            )
            leads_to_update.clear()

    # Process events
    for rel_k, full_p in event_files:
        fhash = compute_file_hash(full_p)
        if rel_k in ingested_files_map:
            if ingested_files_map[rel_k] == fhash:
                continue

        with conn:
            ingest_event_file(
                conn=conn,
                filepath=full_p,
                file_rel_key=rel_k,
                file_hash=fhash,
                registry=registry,
                seen_event_ids=seen_event_ids,
            )

    conn.close()
    return {"status": "success", "drop": drop_name}
