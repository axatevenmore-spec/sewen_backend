"""
Credential encryption helpers for third-party lead source integrations.

Uses Fernet symmetric encryption with a key derived from Django's SECRET_KEY.
"""
import base64
import hashlib
import json
import logging
from typing import Any, Dict

from cryptography.fernet import Fernet
from django.conf import settings

logger = logging.getLogger(__name__)


def _get_fernet() -> Fernet:
    secret = getattr(settings, "SECRET_KEY", "") or "default-insecure-key-must-be-replaced"
    key_bytes = hashlib.sha256(secret.encode("utf-8")).digest()
    b64_key = base64.urlsafe_b64encode(key_bytes)
    return Fernet(b64_key)


def encrypt_credentials(data: Any) -> str:
    """Encrypt credentials dictionary or string into a secure ciphertext string."""
    if data is None or data == "" or data == {}:
        return ""
    if isinstance(data, (dict, list)):
        payload_str = json.dumps(data)
    else:
        payload_str = str(data)
    fernet = _get_fernet()
    return fernet.encrypt(payload_str.encode("utf-8")).decode("utf-8")


def decrypt_credentials(ciphertext: str) -> Dict[str, Any]:
    """Decrypt a credentials ciphertext string into a dictionary."""
    if not ciphertext:
        return {}
    fernet = _get_fernet()
    try:
        decrypted_bytes = fernet.decrypt(ciphertext.encode("utf-8"))
        decrypted_str = decrypted_bytes.decode("utf-8")
        parsed = json.loads(decrypted_str)
        if isinstance(parsed, dict):
            return parsed
        return {"value": parsed}
    except Exception as exc:
        logger.error("Failed to decrypt integration credentials: %s", exc)
        return {}
