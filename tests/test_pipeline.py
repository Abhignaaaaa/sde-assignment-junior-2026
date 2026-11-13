"""Integration tests for pipeline: lead merging, event overrides, idempotency, and incremental equivalence."""

import csv
import json
import os
import shutil
import tempfile
import pytest

from solution.db import get_db_connection
from solution.ingest import ingest_drop
from solution.report import generate_reports

SCHEMAS_DIR = os.path.abspath("assignment-a-data/schemas")


@pytest.fixture
def temp_workspace():
    tmpdir = tempfile.mkdtemp()
    yield tmpdir
    shutil.rmtree(tmpdir, ignore_errors=True)


def test_lead_merging_and_defaults(temp_workspace):
    db_path = os.path.join(temp_workspace, "test.db")
    drop_dir = os.path.join(temp_workspace, "drop_1")
    leads_dir = os.path.join(drop_dir, "leads", "motor_renewal")
    os.makedirs(leads_dir, exist_ok=True)

    # Batch 1
    batch_1 = os.path.join(leads_dir, "batch_001.csv")
    with open(batch_1, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["customer_name", "phone", "policy_number", "premium_amount", "preferred_language"])
        w.writerow(["Aarav Sharma", "9876543210", "POL100", "5000", ""])  # Language null -> default "en"

    # Batch 2: same lead, different batch_id, updated amount and language
    batch_2 = os.path.join(leads_dir, "batch_002.csv")
    with open(batch_2, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["customer_name", "phone", "policy_number", "premium_amount", "preferred_language"])
        w.writerow(["Aarav Sharma", "9876543210", "POL100", "7500", "hi"])  # Valid row: amount & lang updated

    ingest_drop(drop_dir, db_path, SCHEMAS_DIR)

    conn = get_db_connection(db_path)
    cursor = conn.execute("SELECT * FROM leads")
    leads = cursor.fetchall()
    assert len(leads) == 1

    lead = leads[0]
    # batch_id must permanently be batch_001
    assert lead["batch_id"] == "batch_001"
    # preferred_language updated to hi
    assert lead["preferred_language"] == "hi"
    # premium_amount updated to 7500
    extra = json.loads(lead["extra_data_json"])
    assert extra["premium_amount"] == "7500.00"

    # Verify duplicate merged count in stats
    stats_c = conn.execute("SELECT SUM(duplicate_lead_rows_merged) as merged FROM ingestion_stats")
    assert stats_c.fetchone()["merged"] == 1
    conn.close()


def test_effective_disposition_and_override_without_phone(temp_workspace):
    db_path = os.path.join(temp_workspace, "test.db")
    drop_dir = os.path.join(temp_workspace, "drop_1")
    leads_dir = os.path.join(drop_dir, "leads", "motor_renewal")
    events_dir = os.path.join(drop_dir, "events")
    os.makedirs(leads_dir, exist_ok=True)
    os.makedirs(events_dir, exist_ok=True)

    # Lead
    with open(os.path.join(leads_dir, "batch_001.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["customer_name", "phone", "policy_number"])
        w.writerow(["Aarav Sharma", "9876543210", "POL100"])

    # Events for call_1:
    # 1. call.initiated
    # 2. call.connected
    # 3. disposition.set -> callback_requested (non-terminal, non-success)
    # 4. disposition.override -> renewed (terminal, success, NO PHONE)
    events = [
        {"event_id": "ev1", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "c1",
         "occurred_at": "2026-10-01 10:00:00", "phone": "9876543210", "payload": {}},
        {"event_id": "ev2", "event_type": "call.connected", "workflow_id": "motor_renewal", "call_id": "c1",
         "occurred_at": "2026-10-01 10:00:05", "phone": "9876543210", "payload": {}},
        {"event_id": "ev3", "event_type": "disposition.set", "workflow_id": "motor_renewal", "call_id": "c1",
         "occurred_at": "2026-10-01 10:02:00", "phone": "9876543210", "payload": {"disposition_code": "callback_requested"}},
        {"event_id": "ev4", "event_type": "disposition.override", "workflow_id": "motor_renewal", "call_id": "c1",
         "occurred_at": "2026-10-01 12:00:00", "payload": {"disposition_code": "renewed", "reason": "qa"}}
    ]

    with open(os.path.join(events_dir, "events_0001.jsonl"), "w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")

    ingest_drop(drop_dir, db_path, SCHEMAS_DIR)

    reports_dir = os.path.join(temp_workspace, "reports")
    generate_reports(reports_dir, db_path, SCHEMAS_DIR)

    # Verify lead_report.csv
    with open(os.path.join(reports_dir, "lead_report.csv"), "r", encoding="utf-8") as f:
        reader = list(csv.DictReader(f))
        assert len(reader) == 1
        lr = reader[0]
        assert lr["current_disposition"] == "renewed"
        assert lr["bucket"] == "completed"
        assert lr["ever_success"] == "true"
        assert lr["total_calls"] == "1"
        assert lr["connected_calls"] == "1"

    # Verify daily_overview.csv
    with open(os.path.join(reports_dir, "daily_overview.csv"), "r", encoding="utf-8") as f:
        reader = list(csv.DictReader(f))
        assert len(reader) == 1
        d = reader[0]
        assert d["total_calls"] == "1"
        assert d["connected_calls"] == "1"
        assert d["connectivity_rate"] == "1.0000"
        assert d["success_leads"] == "1"


def test_orphan_resolution_across_drops(temp_workspace):
    db_path = os.path.join(temp_workspace, "test.db")
    drop_1 = os.path.join(temp_workspace, "drop_1")
    drop_2 = os.path.join(temp_workspace, "drop_2")

    events_1 = os.path.join(drop_1, "events")
    leads_2 = os.path.join(drop_2, "leads", "motor_renewal")
    os.makedirs(events_1, exist_ok=True)
    os.makedirs(leads_2, exist_ok=True)

    # Event arrives in drop 1 for a phone not yet uploaded
    ev = {"event_id": "ev1", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "c1",
          "occurred_at": "2026-10-01 10:00:00", "phone": "9876543210", "payload": {}}
    with open(os.path.join(events_1, "events_0001.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps(ev) + "\n")

    ingest_drop(drop_1, db_path, SCHEMAS_DIR)

    # Reports after drop 1: event must be an orphan
    rep_1 = os.path.join(temp_workspace, "rep_1")
    generate_reports(rep_1, db_path, SCHEMAS_DIR)
    with open(os.path.join(rep_1, "data_quality.json"), "r", encoding="utf-8") as f:
        dq = json.load(f)
        assert dq["orphan_events"] == 1

    # Lead arrives in drop 2
    with open(os.path.join(leads_2, "batch_001.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["customer_name", "phone", "policy_number"])
        w.writerow(["Aarav Sharma", "9876543210", "POL100"])

    ingest_drop(drop_2, db_path, SCHEMAS_DIR)

    # Reports after drop 2: event must no longer be an orphan
    rep_2 = os.path.join(temp_workspace, "rep_2")
    generate_reports(rep_2, db_path, SCHEMAS_DIR)
    with open(os.path.join(rep_2, "data_quality.json"), "r", encoding="utf-8") as f:
        dq = json.load(f)
        assert dq["orphan_events"] == 0


def test_idempotent_reingest(temp_workspace):
    db_path = os.path.join(temp_workspace, "test.db")
    drop_dir = os.path.join(temp_workspace, "drop_1")
    leads_dir = os.path.join(drop_dir, "leads", "motor_renewal")
    events_dir = os.path.join(drop_dir, "events")
    os.makedirs(leads_dir, exist_ok=True)
    os.makedirs(events_dir, exist_ok=True)

    with open(os.path.join(leads_dir, "batch_001.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["customer_name", "phone", "policy_number"])
        w.writerow(["Aarav Sharma", "9876543210", "POL100"])
        w.writerow(["Bad Phone Lead", "5123456789", "POL101"])

    ev = {"event_id": "ev1", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "c1",
          "occurred_at": "2026-10-01 10:00:00", "phone": "9876543210", "payload": {}}
    with open(os.path.join(events_dir, "events_0001.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps(ev) + "\n")

    # Ingest run 1
    ingest_drop(drop_dir, db_path, SCHEMAS_DIR)
    rep_1 = os.path.join(temp_workspace, "rep_1")
    generate_reports(rep_1, db_path, SCHEMAS_DIR)

    # Re-run ingest on same drop
    ingest_drop(drop_dir, db_path, SCHEMAS_DIR)
    rep_2 = os.path.join(temp_workspace, "rep_2")
    generate_reports(rep_2, db_path, SCHEMAS_DIR)

    # Compare all 5 files byte-for-byte
    for fname in ["batch_summary.csv", "daily_overview.csv", "lead_report.csv", "rejections.csv", "data_quality.json"]:
        f1 = os.path.join(rep_1, fname)
        f2 = os.path.join(rep_2, fname)
        with open(f1, "rb") as a, open(f2, "rb") as b:
            assert a.read() == b.read(), f"File {fname} differed on idempotent re-run"


def test_bucket_logic(temp_workspace):
    db_path = os.path.join(temp_workspace, "test.db")
    drop_dir = os.path.join(temp_workspace, "drop_1")
    leads_dir = os.path.join(drop_dir, "leads", "motor_renewal")
    events_dir = os.path.join(drop_dir, "events")
    os.makedirs(leads_dir, exist_ok=True)
    os.makedirs(events_dir, exist_ok=True)

    # 4 leads:
    # L1: no calls -> yet_to_reach_out
    # L2: telephony failed call -> in_progress_telephony_failed
    # L3: connected call, no terminal disp -> in_progress_dialed
    # L4: completed terminal call -> completed
    with open(os.path.join(leads_dir, "batch_001.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["customer_name", "phone", "policy_number"])
        w.writerow(["Lead One", "9876543211", "POL001"])
        w.writerow(["Lead Two", "9876543212", "POL002"])
        w.writerow(["Lead Three", "9876543213", "POL003"])
        w.writerow(["Lead Four", "9876543214", "POL004"])

    events = [
        # Lead Two: telephony failed
        {"event_id": "e2_1", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "c2",
         "occurred_at": "2026-10-01 10:00:00", "phone": "9876543212", "payload": {}},
        {"event_id": "e2_2", "event_type": "call.telephony_failed", "workflow_id": "motor_renewal", "call_id": "c2",
         "occurred_at": "2026-10-01 10:00:02", "phone": "9876543212", "payload": {"reason": "sip_503"}},
        # Lead Three: dialed / connected, non-terminal disp
        {"event_id": "e3_1", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "c3",
         "occurred_at": "2026-10-01 11:00:00", "phone": "9876543213", "payload": {}},
        {"event_id": "e3_2", "event_type": "call.connected", "workflow_id": "motor_renewal", "call_id": "c3",
         "occurred_at": "2026-10-01 11:00:10", "phone": "9876543213", "payload": {}},
        {"event_id": "e3_3", "event_type": "disposition.set", "workflow_id": "motor_renewal", "call_id": "c3",
         "occurred_at": "2026-10-01 11:02:00", "phone": "9876543213", "payload": {"disposition_code": "callback_requested"}},
        # Lead Four: terminal disposition
        {"event_id": "e4_1", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "c4",
         "occurred_at": "2026-10-01 12:00:00", "phone": "9876543214", "payload": {}},
        {"event_id": "e4_2", "event_type": "call.connected", "workflow_id": "motor_renewal", "call_id": "c4",
         "occurred_at": "2026-10-01 12:00:05", "phone": "9876543214", "payload": {}},
        {"event_id": "e4_3", "event_type": "disposition.set", "workflow_id": "motor_renewal", "call_id": "c4",
         "occurred_at": "2026-10-01 12:05:00", "phone": "9876543214", "payload": {"disposition_code": "renewed"}},
    ]
    with open(os.path.join(events_dir, "events_0001.jsonl"), "w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")

    ingest_drop(drop_dir, db_path, SCHEMAS_DIR)
    rep_dir = os.path.join(temp_workspace, "reports")
    generate_reports(rep_dir, db_path, SCHEMAS_DIR)

    with open(os.path.join(rep_dir, "lead_report.csv"), "r", encoding="utf-8") as f:
        leads_by_ref = {row["external_ref"]: row for row in csv.DictReader(f)}

    assert leads_by_ref["POL001"]["bucket"] == "yet_to_reach_out"
    assert leads_by_ref["POL002"]["bucket"] == "in_progress_telephony_failed"
    assert leads_by_ref["POL003"]["bucket"] == "in_progress_dialed"
    assert leads_by_ref["POL004"]["bucket"] == "completed"


def test_incremental_vs_singleshot_equivalence(temp_workspace):
    # Create drop_1 and drop_2
    drop_1 = os.path.join(temp_workspace, "drop_1")
    drop_2 = os.path.join(temp_workspace, "drop_2")
    leads_1 = os.path.join(drop_1, "leads", "motor_renewal")
    leads_2 = os.path.join(drop_2, "leads", "motor_renewal")
    events_1 = os.path.join(drop_1, "events")
    events_2 = os.path.join(drop_2, "events")
    for d in (leads_1, leads_2, events_1, events_2):
        os.makedirs(d, exist_ok=True)

    # Lead 1 in drop 1, call in drop 1
    with open(os.path.join(leads_1, "batch_001.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["customer_name", "phone", "policy_number"])
        w.writerow(["Lead A", "9876543210", "POL_A"])

    ev1 = {"event_id": "ev1", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "c1",
           "occurred_at": "2026-10-01 10:00:00", "phone": "9876543210", "payload": {}}
    with open(os.path.join(events_1, "events_0001.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps(ev1) + "\n")

    # Lead 2 in drop 2, override for call 1 in drop 2
    with open(os.path.join(leads_2, "batch_002.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["customer_name", "phone", "policy_number"])
        w.writerow(["Lead B", "9876543211", "POL_B"])

    ev2 = {"event_id": "ev2", "event_type": "disposition.override", "workflow_id": "motor_renewal", "call_id": "c1",
           "occurred_at": "2026-10-02 12:00:00", "payload": {"disposition_code": "renewed", "reason": "qa"}}
    with open(os.path.join(events_2, "events_1001.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps(ev2) + "\n")

    # Pipeline A: Ingest drop_1 then drop_2
    db_a = os.path.join(temp_workspace, "ledger_seq.db")
    rep_a = os.path.join(temp_workspace, "rep_seq")
    ingest_drop(drop_1, db_a, SCHEMAS_DIR)
    ingest_drop(drop_2, db_a, SCHEMAS_DIR)
    generate_reports(rep_a, db_a, SCHEMAS_DIR)

    # Pipeline B: Ingest both drops into fresh DB
    db_b = os.path.join(temp_workspace, "ledger_all.db")
    rep_b = os.path.join(temp_workspace, "rep_all")
    ingest_drop(drop_1, db_b, SCHEMAS_DIR)
    ingest_drop(drop_2, db_b, SCHEMAS_DIR)
    generate_reports(rep_b, db_b, SCHEMAS_DIR)

    for fname in ["batch_summary.csv", "daily_overview.csv", "lead_report.csv", "rejections.csv", "data_quality.json"]:
        f1 = os.path.join(rep_a, fname)
        f2 = os.path.join(rep_b, fname)
        with open(f1, "rb") as fa, open(f2, "rb") as fb:
            assert fa.read() == fb.read(), f"File {fname} differed between sequential runs"

