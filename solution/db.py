"""Database connection, schema DDL, and database operations."""

from __future__ import annotations

import os
import sqlite3
from typing import Optional


def get_db_connection(db_path: str) -> sqlite3.Connection:
    """Create or connect to SQLite database with WAL mode enabled."""
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA temp_store = MEMORY;")
    return conn


def init_db(conn: sqlite3.Connection):
    """Initialize database tables and indexes."""
    with conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS ingested_files (
                file_path TEXT PRIMARY KEY,
                file_hash TEXT NOT NULL,
                file_type TEXT NOT NULL,
                ingested_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ingestion_stats (
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

            CREATE TABLE IF NOT EXISTS rejections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workflow_id TEXT NOT NULL,
                batch_id TEXT NOT NULL,
                row_number INTEGER NOT NULL,
                reason TEXT NOT NULL,
                file_path TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_rejections_sort
            ON rejections(workflow_id, batch_id, row_number);

            CREATE TABLE IF NOT EXISTS leads (
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

            CREATE INDEX IF NOT EXISTS idx_leads_wf_ref
            ON leads(workflow_id, external_ref);

            CREATE INDEX IF NOT EXISTS idx_leads_batch
            ON leads(workflow_id, batch_id);

            CREATE TABLE IF NOT EXISTS interaction_ledger (
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

            CREATE INDEX IF NOT EXISTS idx_ledger_call
            ON interaction_ledger(call_id);

            CREATE INDEX IF NOT EXISTS idx_ledger_wf_call
            ON interaction_ledger(workflow_id, call_id);

            CREATE INDEX IF NOT EXISTS idx_ledger_lead_key
            ON interaction_ledger(lead_key);

            CREATE INDEX IF NOT EXISTS idx_ledger_etype
            ON interaction_ledger(event_type);
            """
        )
