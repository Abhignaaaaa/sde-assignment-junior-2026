"""Report generation pipeline matching the exact output contract."""

from __future__ import annotations

import csv
import json
import os
import sqlite3
from dataclasses import dataclass
from typing import Dict, List, Optional, Set

from solution.db import get_db_connection
from solution.schemas import SchemaRegistry


@dataclass
class CallInfo:
    call_id: str
    workflow_id: str
    lead_key: str
    call_date: str
    initiated_at: str
    initiated_epoch: float
    is_connected: bool
    is_telephony_failed: bool
    effective_disposition: Optional[str]
    is_terminal: bool
    is_success: bool


def generate_reports(
    out_dir: str,
    db_path: str,
    schemas_dir: str,
):
    """Generate all 5 report files into out_dir."""
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    registry = SchemaRegistry(schemas_dir)
    conn = get_db_connection(db_path)

    # 1. Determine linked calls and resolve lead_keys
    # For every call_id, find the associated lead_key (if any event for that call has a valid lead_key that exists in leads)
    call_leads_cursor = conn.execute(
        """
        SELECT il.workflow_id, il.call_id, il.lead_key
        FROM interaction_ledger il
        JOIN leads l ON il.lead_key = l.lead_key
        WHERE il.call_id != '' AND il.lead_key IS NOT NULL
        GROUP BY il.call_id
        """
    )
    call_to_lead: Dict[str, str] = {row["call_id"]: row["lead_key"] for row in call_leads_cursor.fetchall()}
    linked_call_ids: Set[str] = set(call_to_lead.keys())

    # 2. Count orphan events
    # An event is an orphan if its call_id is not in linked_call_ids
    # (or if call_id is empty and its lead_key is not in leads)
    orphan_cursor = conn.execute(
        """
        SELECT COUNT(*) as cnt
        FROM interaction_ledger
        WHERE call_id NOT IN (
            SELECT DISTINCT il.call_id
            FROM interaction_ledger il
            JOIN leads l ON il.lead_key = l.lead_key
            WHERE il.call_id != '' AND il.lead_key IS NOT NULL
        ) OR (call_id = '' AND (lead_key IS NULL OR lead_key NOT IN (SELECT lead_key FROM leads)))
        """
    )
    orphan_events_count = orphan_cursor.fetchone()["cnt"]

    # 3. Retrieve all events for linked calls to reconstruct calls
    # Order by call_id, occurred_at_epoch ASC, rowid ASC
    events_cursor = conn.execute(
        """
        SELECT rowid, event_id, event_type, workflow_id, call_id,
               occurred_at, occurred_at_epoch, lead_key,
               disposition_code, is_unknown_disposition
        FROM interaction_ledger
        WHERE call_id IN (
            SELECT DISTINCT il.call_id
            FROM interaction_ledger il
            JOIN leads l ON il.lead_key = l.lead_key
            WHERE il.call_id != '' AND il.lead_key IS NOT NULL
        )
        ORDER BY call_id, occurred_at_epoch ASC, rowid ASC
        """
    )

    # Group events by call_id
    events_by_call: Dict[str, list] = {}
    for row in events_cursor.fetchall():
        cid = row["call_id"]
        if cid not in events_by_call:
            events_by_call[cid] = []
        events_by_call[cid].append(row)

    # 4. Construct CallInfo for valid calls (must have call.initiated)
    all_calls: List[CallInfo] = []
    calls_by_lead: Dict[str, List[CallInfo]] = {}

    for cid, evs in events_by_call.items():
        initiated_ev = None
        for ev in evs:
            if ev["event_type"] == "call.initiated":
                initiated_ev = ev
                break

        if not initiated_ev:
            # Not counted as a call per spec
            continue

        wf_id = initiated_ev["workflow_id"]
        lead_k = call_to_lead[cid]
        call_date = initiated_ev["occurred_at"][:10]  # IST YYYY-MM-DD
        init_at = initiated_ev["occurred_at"]
        init_epoch = initiated_ev["occurred_at_epoch"]

        is_connected = any(ev["event_type"] == "call.connected" for ev in evs)
        is_telephony_failed = any(ev["event_type"] == "call.telephony_failed" for ev in evs)

        # Effective disposition:
        # Latest disposition.override (tie-break by ingestion order rowid),
        # else latest disposition.set, ignoring unknown dispositions.
        effective_disp = None
        overrides = [
            ev for ev in evs
            if ev["event_type"] == "disposition.override" and ev["is_unknown_disposition"] == 0 and ev["disposition_code"]
        ]
        if overrides:
            # Sort by occurred_at_epoch, then rowid
            overrides.sort(key=lambda x: (x["occurred_at_epoch"], x["rowid"]))
            effective_disp = overrides[-1]["disposition_code"]
        else:
            sets = [
                ev for ev in evs
                if ev["event_type"] == "disposition.set" and ev["is_unknown_disposition"] == 0 and ev["disposition_code"]
            ]
            if sets:
                sets.sort(key=lambda x: (x["occurred_at_epoch"], x["rowid"]))
                effective_disp = sets[-1]["disposition_code"]

        is_term = False
        is_succ = False
        if effective_disp:
            disp_info = registry.get_disposition(wf_id, effective_disp)
            if disp_info:
                is_term = disp_info.is_terminal
                is_succ = disp_info.is_success

        call_obj = CallInfo(
            call_id=cid,
            workflow_id=wf_id,
            lead_key=lead_k,
            call_date=call_date,
            initiated_at=init_at,
            initiated_epoch=init_epoch,
            is_connected=is_connected,
            is_telephony_failed=is_telephony_failed,
            effective_disposition=effective_disp,
            is_terminal=is_term,
            is_success=is_succ,
        )
        all_calls.append(call_obj)
        if lead_k not in calls_by_lead:
            calls_by_lead[lead_k] = []
        calls_by_lead[lead_k].append(call_obj)

    # 5. Load all leads
    leads_cursor = conn.execute(
        """
        SELECT lead_key, workflow_id, batch_id, external_ref, phone_masked
        FROM leads
        """
    )
    leads_list = [dict(r) for r in leads_cursor.fetchall()]

    # Compute lead metrics and buckets
    # Bucket rules:
    # 1. yet_to_reach_out: no calls
    # 2. completed: any call's effective disposition is terminal
    # 3. in_progress_telephony_failed: every call has call.telephony_failed
    # 4. in_progress_dialed: everything else
    lead_reports = []
    lead_buckets: Dict[str, str] = {}

    for lead in leads_list:
        lk = lead["lead_key"]
        l_calls = calls_by_lead.get(lk, [])

        if len(l_calls) == 0:
            bucket = "yet_to_reach_out"
        elif any(c.is_terminal for c in l_calls):
            bucket = "completed"
        elif all(c.is_telephony_failed for c in l_calls):
            bucket = "in_progress_telephony_failed"
        else:
            bucket = "in_progress_dialed"

        lead_buckets[lk] = bucket

        total_calls = len(l_calls)
        connected_calls = sum(1 for c in l_calls if c.is_connected)

        if total_calls > 0:
            # Sort calls descending by initiated_epoch, then initiated_at
            sorted_calls = sorted(l_calls, key=lambda c: (c.initiated_epoch, c.initiated_at), reverse=True)
            last_call_at = sorted_calls[0].initiated_at

            # current_disposition: effective disposition of most recent call that has one
            curr_disp = ""
            for c in sorted_calls:
                if c.effective_disposition:
                    curr_disp = c.effective_disposition
                    break
        else:
            last_call_at = ""
            curr_disp = ""

        ever_success = any(c.is_success for c in l_calls)

        lead_reports.append({
            "workflow_id": lead["workflow_id"],
            "external_ref": lead["external_ref"],
            "batch_id": lead["batch_id"],
            "phone_masked": lead["phone_masked"],
            "bucket": bucket,
            "total_calls": total_calls,
            "connected_calls": connected_calls,
            "last_call_at": last_call_at,
            "current_disposition": curr_disp,
            "ever_success": "true" if ever_success else "false",
        })

    # --- Write File 1: batch_summary.csv ---
    # Group leads by (workflow_id, batch_id)
    batch_map: Dict[tuple[str, str], dict] = {}
    for lead in leads_list:
        key = (lead["workflow_id"], lead["batch_id"])
        if key not in batch_map:
            batch_map[key] = {
                "workflow_id": lead["workflow_id"],
                "batch_id": lead["batch_id"],
                "total_leads": 0,
                "yet_to_reach_out": 0,
                "in_progress_telephony_failed": 0,
                "in_progress_dialed": 0,
                "completed": 0,
            }
        bucket = lead_buckets[lead["lead_key"]]
        batch_map[key]["total_leads"] += 1
        batch_map[key][bucket] += 1

    batch_rows = sorted(batch_map.values(), key=lambda r: (r["workflow_id"], r["batch_id"]))
    with open(os.path.join(out_dir, "batch_summary.csv"), "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "workflow_id",
                "batch_id",
                "total_leads",
                "yet_to_reach_out",
                "in_progress_telephony_failed",
                "in_progress_dialed",
                "completed",
            ],
        )
        writer.writeheader()
        for row in batch_rows:
            writer.writerow(row)

    # --- Write File 2: daily_overview.csv ---
    # Group calls by (workflow_id, call_date)
    daily_map: Dict[tuple[str, str], dict] = {}
    for call in all_calls:
        key = (call.workflow_id, call.call_date)
        if key not in daily_map:
            daily_map[key] = {
                "workflow_id": call.workflow_id,
                "date": call.call_date,
                "total_calls": 0,
                "connected_calls": 0,
                "success_leads_set": set(),
            }
        daily_map[key]["total_calls"] += 1
        if call.is_connected:
            daily_map[key]["connected_calls"] += 1
        if call.is_success:
            daily_map[key]["success_leads_set"].add(call.lead_key)

    daily_rows = []
    for (wf, dt), item in daily_map.items():
        tot = item["total_calls"]
        conn_cnt = item["connected_calls"]
        conn_rate = f"{(conn_cnt / tot):.4f}" if tot > 0 else "0.0000"
        daily_rows.append({
            "workflow_id": wf,
            "date": dt,
            "total_calls": tot,
            "connected_calls": conn_cnt,
            "connectivity_rate": conn_rate,
            "success_leads": len(item["success_leads_set"]),
        })

    daily_rows.sort(key=lambda r: (r["workflow_id"], r["date"]))
    with open(os.path.join(out_dir, "daily_overview.csv"), "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "workflow_id",
                "date",
                "total_calls",
                "connected_calls",
                "connectivity_rate",
                "success_leads",
            ],
        )
        writer.writeheader()
        for row in daily_rows:
            writer.writerow(row)

    # --- Write File 3: lead_report.csv ---
    lead_reports.sort(key=lambda r: (r["workflow_id"], r["external_ref"]))
    with open(os.path.join(out_dir, "lead_report.csv"), "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "workflow_id",
                "external_ref",
                "batch_id",
                "phone_masked",
                "bucket",
                "total_calls",
                "connected_calls",
                "last_call_at",
                "current_disposition",
                "ever_success",
            ],
        )
        writer.writeheader()
        for row in lead_reports:
            writer.writerow(row)

    # --- Write File 4: rejections.csv ---
    rej_cursor = conn.execute(
        """
        SELECT workflow_id, batch_id, row_number, reason
        FROM rejections
        ORDER BY workflow_id ASC, batch_id ASC, row_number ASC
        """
    )
    with open(os.path.join(out_dir, "rejections.csv"), "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["workflow_id", "batch_id", "row_number", "reason"],
        )
        writer.writeheader()
        for row in rej_cursor.fetchall():
            writer.writerow(dict(row))

    # --- Write File 5: data_quality.json ---
    stats_cursor = conn.execute(
        """
        SELECT
            COALESCE(SUM(lead_rows_total), 0) as lead_rows_total,
            COALESCE(SUM(lead_rows_rejected), 0) as lead_rows_rejected,
            COALESCE(SUM(duplicate_lead_rows_merged), 0) as duplicate_lead_rows_merged,
            COALESCE(SUM(field_warnings), 0) as field_warnings,
            COALESCE(SUM(events_total), 0) as events_total,
            COALESCE(SUM(malformed_events), 0) as malformed_events,
            COALESCE(SUM(duplicate_events_dropped), 0) as duplicate_events_dropped,
            COALESCE(SUM(unknown_disposition_events), 0) as unknown_disposition_events
        FROM ingestion_stats
        """
    )
    s_row = stats_cursor.fetchone()

    # Rejections by reason
    reasons_cursor = conn.execute(
        """
        SELECT reason, COUNT(*) as cnt
        FROM rejections
        GROUP BY reason
        ORDER BY reason ASC
        """
    )
    rejections_by_reason = {r["reason"]: r["cnt"] for r in reasons_cursor.fetchall()}

    dq_data = {
        "lead_rows_total": s_row["lead_rows_total"],
        "lead_rows_rejected": s_row["lead_rows_rejected"],
        "rejections_by_reason": rejections_by_reason,
        "duplicate_lead_rows_merged": s_row["duplicate_lead_rows_merged"],
        "field_warnings": s_row["field_warnings"],
        "events_total": s_row["events_total"],
        "malformed_events": s_row["malformed_events"],
        "duplicate_events_dropped": s_row["duplicate_events_dropped"],
        "orphan_events": orphan_events_count,
        "unknown_disposition_events": s_row["unknown_disposition_events"],
    }

    with open(os.path.join(out_dir, "data_quality.json"), "w", encoding="utf-8") as f:
        json.dump(dq_data, f, indent=2)

    conn.close()
