"""PII protection module: keyed HMAC lead_key, masking, and AES-GCM encryption at rest."""

import base64
import hashlib
import hmac
import os
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from solution.config import MASTER_KEY

_AESGCM = AESGCM(MASTER_KEY)


def compute_lead_key(workflow_id: str, canonical_phone: str) -> str:
    """Compute deterministic keyed HMAC-SHA256 from (workflow_id, canonical_phone)."""
    msg = f"{workflow_id}:{canonical_phone}".encode("utf-8")
    return hmac.new(MASTER_KEY, msg, hashlib.sha256).hexdigest()


def mask_phone(canonical_phone: str) -> str:
    """Mask phone as +91******XXXX using the last 4 digits."""
    if len(canonical_phone) >= 4:
        return f"+91******{canonical_phone[-4:]}"
    return "+91******0000"


def encrypt_field(plaintext: str | None) -> str | None:
    """Encrypt a plaintext string using AES-256-GCM at rest."""
    if plaintext is None:
        return None
    nonce = os.urandom(12)
    ciphertext = _AESGCM.encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.b64encode(nonce + ciphertext).decode("ascii")


def decrypt_field(token: str | None) -> str | None:
    """Decrypt a base64-encoded AES-256-GCM token."""
    if token is None:
        return None
    data = base64.b64decode(token.encode("ascii"))
    nonce = data[:12]
    ciphertext = data[12:]
    return _AESGCM.decrypt(nonce, ciphertext, None).decode("utf-8")
