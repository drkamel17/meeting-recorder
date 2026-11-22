"""Security: encrypt/decrypt passwords using an app-specific key.

The key is intentionally machine-independent so the database can be moved to
another PC. Legacy machine-dependent keys are still supported for reading
existing entries.
"""
import base64
import hashlib
import os

# Portable application secret. This is obfuscation for local storage, not a
# replacement for authenticated access control.
_PORTABLE_SECRET = "ZoomAutoRecorder-2026"


def _get_key() -> bytes:
    """Return the portable application key."""
    return hashlib.sha256(_PORTABLE_SECRET.encode("utf-8")).digest()[:16]


def _get_legacy_key() -> bytes:
    """Return the previous machine-specific key for backward compatibility."""
    username = os.environ.get("USERNAME", "default")
    machine_id = os.environ.get("COMPUTERNAME", "default")
    return hashlib.sha256(f"{username}{machine_id}ZoomAutoRecorder".encode()).digest()[:16]


def _xor_bytes(data: bytes, key: bytes) -> bytes:
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))


def encrypt_password(plain: str) -> str:
    """Encrypt a password and return a base64 string."""
    if not plain:
        return ""
    encrypted = _xor_bytes(plain.encode("utf-8"), _get_key())
    return base64.b64encode(encrypted).decode("ascii")


def decrypt_password(encoded: str) -> str:
    """Decrypt a base64-encoded password.

    Tries the portable key first, then the legacy machine-specific key. If
    neither succeeds, returns the original encoded value unchanged.
    """
    if not encoded:
        return ""

    try:
        decoded = base64.b64decode(encoded.encode("ascii"), validate=True)
    except Exception:
        return encoded

    for key in (_get_key(), _get_legacy_key()):
        try:
            decrypted = _xor_bytes(decoded, key).decode("utf-8")
            return decrypted
        except UnicodeDecodeError:
            continue
    return encoded