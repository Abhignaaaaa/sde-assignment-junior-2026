"""Data normalization and validation for lead CSV rows and types."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from solution.schemas import FieldSchema, WorkflowSchema, normalize_header


@dataclass
class RowValidationResult:
    is_rejected: bool
    rejection_reason: Optional[str] = None
    values: Dict[str, Any] = None
    field_warnings: int = 0


def is_blank_row(row_cells: list[str]) -> bool:
    """Check if all cells in a row are empty or whitespace."""
    return all(cell is None or str(cell).strip() == "" for cell in row_cells)


def normalize_phone(val: str) -> Tuple[Optional[str], bool]:
    """
    Validate and normalize Indian phone number.
    Strip non-digits. If 12 digits starting with 91, drop 91.
    If 11 digits starting with 0, drop 0.
    Valid iff 10 digits starting with 6, 7, 8, or 9.
    Returns (canonical_phone, is_valid).
    """
    digits = re.sub(r"\D", "", val.strip())
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]

    if len(digits) == 10 and digits[0] in "6789":
        return f"+91{digits}", True
    return None, False


_MONTHS = {
    "jan": "Jan", "feb": "Feb", "mar": "Mar", "apr": "Apr",
    "may": "May", "jun": "Jun", "jul": "Jul", "aug": "Aug",
    "sep": "Sep", "oct": "Oct", "nov": "Nov", "dec": "Dec",
}


def normalize_date(val: str) -> Tuple[Optional[str], bool]:
    """
    Validate and normalize date.
    Accepts: YYYY-MM-DD, DD/MM/YYYY, DD-MM-YYYY, DD-Mon-YYYY.
    Dates are day-first. Output canonical YYYY-MM-DD.
    """
    s = val.strip()
    if not s:
        return None, False

    # Try YYYY-MM-DD
    if re.match(r"^\d{4}-\d{1,2}-\d{1,2}$", s):
        try:
            dt = datetime.strptime(s, "%Y-%m-%d")
            return dt.strftime("%Y-%m-%d"), True
        except ValueError:
            return None, False

    # Try DD/MM/YYYY
    if re.match(r"^\d{1,2}/\d{1,2}/\d{4}$", s):
        try:
            dt = datetime.strptime(s, "%d/%m/%Y")
            return dt.strftime("%Y-%m-%d"), True
        except ValueError:
            return None, False

    # Try DD-MM-YYYY
    if re.match(r"^\d{1,2}-\d{1,2}-\d{4}$", s):
        try:
            dt = datetime.strptime(s, "%d-%m-%Y")
            return dt.strftime("%Y-%m-%d"), True
        except ValueError:
            return None, False

    # Try DD-Mon-YYYY (case-insensitive month)
    m = re.match(r"^(\d{1,2})-([a-zA-Z]{3})-(\d{4})$", s)
    if m:
        day, mon_str, year = m.groups()
        mon_key = mon_str.lower()
        if mon_key in _MONTHS:
            mon_title = _MONTHS[mon_key]
            norm_s = f"{day}-{mon_title}-{year}"
            try:
                dt = datetime.strptime(norm_s, "%d-%b-%Y")
                return dt.strftime("%Y-%m-%d"), True
            except ValueError:
                return None, False

    return None, False


_CURRENCY_PREFIX_RE = re.compile(
    r"^(?:₹|rs\.?|inr)\s*", re.IGNORECASE
)


def normalize_decimal(val: str) -> Tuple[Optional[str], bool]:
    """
    Validate and normalize decimal amount.
    Optionally prefixed by ₹, Rs, Rs., or INR (case-insensitive).
    Commas are thousands separators. Must be >= 0. Round to 2 places.
    """
    s = val.strip()
    if not s:
        return None, False

    # Remove optional currency prefix
    s = _CURRENCY_PREFIX_RE.sub("", s).strip()
    # Remove commas
    s = s.replace(",", "")

    # Must be valid float
    try:
        num = float(s)
    except ValueError:
        return None, False

    if num < 0:
        return None, False

    return f"{round(num, 2):.2f}", True


def normalize_enum(val: str, allowed_values: list[str]) -> Tuple[Optional[str], bool]:
    """Validate and normalize enum against allowed values, storing lowercase."""
    s = val.strip().lower()
    if not s:
        return None, False
    allowed_lower = [v.lower() for v in allowed_values]
    if s in allowed_lower:
        return s, True
    return None, False


def validate_and_normalize_row(
    raw_row: Dict[str, str], schema: WorkflowSchema
) -> RowValidationResult:
    """
    Map raw headers to schema fields, validate and normalize according to schema order.
    Returns RowValidationResult.
    """
    # 1. Map raw headers to schema fields
    mapped_inputs: Dict[str, str] = {}
    for raw_header, raw_val in raw_row.items():
        if raw_header is None:
            continue
        norm_h = normalize_header(raw_header)
        if norm_h in schema.header_map:
            field_name = schema.header_map[norm_h]
            val = str(raw_val).strip() if raw_val is not None else ""
            mapped_inputs[field_name] = val

    # 2. Walk fields in schema order
    normalized_values: Dict[str, Any] = {}
    warnings = 0

    for f in schema.fields:
        raw_val = mapped_inputs.get(f.name, "")
        is_empty = raw_val == ""

        if f.required:
            if is_empty:
                return RowValidationResult(
                    is_rejected=True,
                    rejection_reason=f"missing_required:{f.name}",
                )

            # Field is present; validate type
            if f.type == "phone":
                canon_phone, is_valid = normalize_phone(raw_val)
                if not is_valid:
                    return RowValidationResult(
                        is_rejected=True,
                        rejection_reason="invalid_phone",
                    )
                normalized_values[f.name] = canon_phone
            elif f.type == "string":
                normalized_values[f.name] = raw_val
            elif f.type == "date":
                canon_date, is_valid = normalize_date(raw_val)
                if not is_valid:
                    # Required date invalid
                    return RowValidationResult(
                        is_rejected=True,
                        rejection_reason=f"missing_required:{f.name}",
                    )
                normalized_values[f.name] = canon_date
            elif f.type == "decimal":
                canon_dec, is_valid = normalize_decimal(raw_val)
                if not is_valid:
                    return RowValidationResult(
                        is_rejected=True,
                        rejection_reason=f"missing_required:{f.name}",
                    )
                normalized_values[f.name] = canon_dec
            elif f.type == "enum":
                canon_enum, is_valid = normalize_enum(raw_val, f.values)
                if not is_valid:
                    return RowValidationResult(
                        is_rejected=True,
                        rejection_reason=f"missing_required:{f.name}",
                    )
                normalized_values[f.name] = canon_enum
            else:
                normalized_values[f.name] = raw_val

        else:
            # Optional field
            if is_empty:
                normalized_values[f.name] = None
            else:
                if f.type == "string":
                    normalized_values[f.name] = raw_val
                elif f.type == "phone":
                    canon_phone, is_valid = normalize_phone(raw_val)
                    if is_valid:
                        normalized_values[f.name] = canon_phone
                    else:
                        normalized_values[f.name] = None
                        warnings += 1
                elif f.type == "date":
                    canon_date, is_valid = normalize_date(raw_val)
                    if is_valid:
                        normalized_values[f.name] = canon_date
                    else:
                        normalized_values[f.name] = None
                        warnings += 1
                elif f.type == "decimal":
                    canon_dec, is_valid = normalize_decimal(raw_val)
                    if is_valid:
                        normalized_values[f.name] = canon_dec
                    else:
                        normalized_values[f.name] = None
                        warnings += 1
                elif f.type == "enum":
                    canon_enum, is_valid = normalize_enum(raw_val, f.values)
                    if is_valid:
                        normalized_values[f.name] = canon_enum
                    else:
                        normalized_values[f.name] = None
                        warnings += 1
                else:
                    normalized_values[f.name] = raw_val

    return RowValidationResult(
        is_rejected=False,
        values=normalized_values,
        field_warnings=warnings,
    )
