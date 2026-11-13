"""Configuration, timezone definitions, and default paths."""

import hashlib
import os
from datetime import timedelta, timezone

# Standard IST timezone (+05:30)
IST = timezone(timedelta(hours=5, minutes=30))

# Default paths
DEFAULT_DB_PATH = os.path.join("state", "ledger.db")
DEFAULT_SCHEMAS_DIR = os.path.join("assignment-a-data", "schemas")

# Master secret key for HMAC and encryption
_RAW_SECRET = os.environ.get(
    "PII_SECRET_KEY", "agentic-universe-take-home-2026-pii-secret-key"
).encode("utf-8")
MASTER_KEY = hashlib.sha256(_RAW_SECRET).digest()  # Exactly 32 bytes
