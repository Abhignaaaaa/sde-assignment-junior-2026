"""Unit tests for phone, date, currency, enum, and row normalization."""

import pytest
from solution.normalizers import (
    is_blank_row,
    normalize_date,
    normalize_decimal,
    normalize_enum,
    normalize_phone,
    validate_and_normalize_row,
)
from solution.schemas import FieldSchema, WorkflowSchema, normalize_header


def test_normalize_header():
    assert normalize_header(" Insured_Name ") == "insured name"
    assert normalize_header("Mobile__No") == "mobile no"
    assert normalize_header("POLICY_NUMBER") == "policy number"
    assert normalize_header("  Order    Date ") == "order date"


def test_normalize_phone():
    # Valid formats
    valid_cases = [
        ("9876543210", "+919876543210"),
        ("+91 98765 43210", "+919876543210"),
        ("09876543210", "+919876543210"),
        ("91-98765-43210", "+919876543210"),
        ("(+91) 9876543210", "+919876543210"),
        ("+919876543210", "+919876543210"),
        ("6123456789", "+916123456789"),
        ("7123456789", "+917123456789"),
        ("8123456789", "+918123456789"),
        ("9123456789", "+919123456789"),
    ]
    for raw, expected in valid_cases:
        res, is_valid = normalize_phone(raw)
        assert is_valid is True, f"Failed for {raw}"
        assert res == expected

    # Invalid formats
    invalid_cases = [
        "123456789",          # 9 digits
        "5123456789",         # starts with 5
        "1234567890",         # starts with 1
        "022-2345-6789",      # landline
        "+1 415 555 0134",    # US number
        "N/A",                # non-digits
        "",                   # empty
        "   ",                # spaces
        "91987654321",        # 11 digits starting with 91 (not 12)
    ]
    for raw in invalid_cases:
        res, is_valid = normalize_phone(raw)
        assert is_valid is False, f"Expected invalid for {raw}"
        assert res is None


def test_normalize_date():
    # Valid dates
    valid_dates = [
        ("2026-10-14", "2026-10-14"),
        ("14/10/2026", "2026-10-14"),
        ("14-10-2026", "2026-10-14"),
        ("01-Dec-2026", "2026-12-01"),
        ("1-dec-2026", "2026-12-01"),
        ("15-FEB-2026", "2026-02-15"),
    ]
    for raw, expected in valid_dates:
        res, is_valid = normalize_date(raw)
        assert is_valid is True, f"Failed for {raw}"
        assert res == expected

    # Invalid dates
    invalid_dates = [
        "31/02/2026",       # impossible Feb date
        "2026-13-01",       # month 13
        "soon",
        "TBD",
        "N/A",
        "",
        "32-01-2026",
    ]
    for raw in invalid_dates:
        res, is_valid = normalize_date(raw)
        assert is_valid is False, f"Expected invalid for {raw}"
        assert res is None


def test_normalize_decimal():
    valid_amounts = [
        ("12345.67", "12345.67"),
        ("₹12,345", "12345.00"),
        ("Rs. 12345", "12345.00"),
        ("Rs 12345.5", "12345.50"),
        ("INR 12345.1", "12345.10"),
        ("0", "0.00"),
        ("12,34,567.89", "1234567.89"),
    ]
    for raw, expected in valid_amounts:
        res, is_valid = normalize_decimal(raw)
        assert is_valid is True, f"Failed for {raw}"
        assert res == expected

    invalid_amounts = [
        "-500",
        "N/A",
        "-",
        "twelve thousand",
        "",
    ]
    for raw in invalid_amounts:
        res, is_valid = normalize_decimal(raw)
        assert is_valid is False, f"Expected invalid for {raw}"
        assert res is None


def test_normalize_enum():
    allowed = ["en", "hi", "ta", "kn", "te", "mr"]
    assert normalize_enum("hi", allowed) == ("hi", True)
    assert normalize_enum("HI", allowed) == ("hi", True)
    assert normalize_enum(" ta ", allowed) == ("ta", True)
    assert normalize_enum("Hindi", allowed) == (None, False)
    assert normalize_enum("english", allowed) == (None, False)
    assert normalize_enum("", allowed) == (None, False)


def test_blank_row():
    assert is_blank_row([]) is True
    assert is_blank_row(["", "  ", None, ""]) is True
    assert is_blank_row(["", "a", ""]) is False


def test_validate_and_normalize_row():
    schema = WorkflowSchema(
        workflow_id="motor_renewal",
        description="test",
        fields=[
            FieldSchema(name="customer_name", type="string", required=True, aliases=["name"]),
            FieldSchema(name="phone", type="phone", required=True, aliases=["mobile"]),
            FieldSchema(name="policy_number", type="string", required=True, reference=True, aliases=["policy"]),
            FieldSchema(name="policy_expiry", type="date", required=False, aliases=["expiry"]),
            FieldSchema(name="premium_amount", type="decimal", required=False),
            FieldSchema(name="preferred_language", type="enum", required=False, values=["en", "hi"], default="en"),
        ],
    )

    # Valid row
    row = {
        "Name": "Aarav Sharma",
        "Mobile": "9876543210",
        "Policy": "POL123",
        "Expiry": "14/10/2026",
        "premium_amount": "₹5,000",
        "preferred_language": "HI",
        "Extra_Col": "ignored",
    }
    res = validate_and_normalize_row(row, schema)
    assert res.is_rejected is False
    assert res.values["customer_name"] == "Aarav Sharma"
    assert res.values["phone"] == "+919876543210"
    assert res.values["policy_number"] == "POL123"
    assert res.values["policy_expiry"] == "2026-10-14"
    assert res.values["premium_amount"] == "5000.00"
    assert res.values["preferred_language"] == "hi"
    assert res.field_warnings == 0

    # Missing required field
    row_no_name = dict(row, Name="")
    res = validate_and_normalize_row(row_no_name, schema)
    assert res.is_rejected is True
    assert res.rejection_reason == "missing_required:customer_name"

    # Invalid phone
    row_bad_phone = dict(row, Mobile="5123456789")
    res = validate_and_normalize_row(row_bad_phone, schema)
    assert res.is_rejected is True
    assert res.rejection_reason == "invalid_phone"

    # Missing required phone
    row_no_phone = dict(row, Mobile="")
    res = validate_and_normalize_row(row_no_phone, schema)
    assert res.is_rejected is True
    assert res.rejection_reason == "missing_required:phone"

    # Optional field invalid -> warning, not rejected
    row_bad_date = dict(row, Expiry="31/02/2026")
    res = validate_and_normalize_row(row_bad_date, schema)
    assert res.is_rejected is False
    assert res.values["policy_expiry"] is None
    assert res.field_warnings == 1
