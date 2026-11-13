"""Unit tests for PII hashing, masking, and AES-GCM encryption."""

import pytest
from solution.pii import compute_lead_key, decrypt_field, encrypt_field, mask_phone


def test_compute_lead_key_determinism():
    wf = "motor_renewal"
    phone = "+919876543210"
    k1 = compute_lead_key(wf, phone)
    k2 = compute_lead_key(wf, phone)
    assert k1 == k2
    assert len(k1) == 64  # SHA-256 hex string

    # Different workflow -> different key
    k_other_wf = compute_lead_key("policy_feedback", phone)
    assert k1 != k_other_wf

    # Different phone -> different key
    k_other_phone = compute_lead_key(wf, "+919876543211")
    assert k1 != k_other_phone


def test_mask_phone():
    assert mask_phone("+919876543210") == "+91******3210"
    assert mask_phone("+917718750036") == "+91******0036"
    assert mask_phone("1234") == "+91******1234"


def test_encryption_roundtrip():
    text = "Aarav Sharma"
    encrypted = encrypt_field(text)
    assert encrypted != text
    assert isinstance(encrypted, str)

    decrypted = decrypt_field(encrypted)
    assert decrypted == text

    # Different runs produce different ciphertexts (random nonce)
    encrypted2 = encrypt_field(text)
    assert encrypted != encrypted2
    assert decrypt_field(encrypted2) == text


def test_encryption_none():
    assert encrypt_field(None) is None
    assert decrypt_field(None) is None
