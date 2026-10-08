"""Time-based one-time passwords (RFC 6238), as authenticator apps make them, and recovery codes.

Codes are six digits for 30-second steps with HMAC-SHA1, which every authenticator app speaks.
A code is accepted one step early or late (clocks drift, people type slowly) and only for a step
later than the last one used, so a code that was seen once cannot be used again.
"""

import base64
import hashlib
import hmac
import secrets
import struct
from urllib.parse import quote, urlencode

STEP = 30
DIGITS = 6
WINDOW = 1  # steps accepted before and after now
RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no 0/o, 1/l/i


def new_secret() -> str:
    """A new random secret, in base32 as authenticator apps want it."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def secret_bytes(secret: str) -> bytes:
    """The raw key of a base32 secret."""
    text = secret.strip().replace(" ", "").upper()
    return base64.b32decode(text + "=" * (-len(text) % 8))


def totp(key: bytes, at: float, *, digits: int = DIGITS, step: int = STEP,
         algorithm: str = "sha1") -> str:  # fmt: skip
    """The code for time `at` (seconds since 1970)."""
    counter = struct.pack(">Q", int(at // step))
    digest = hmac.new(key, counter, algorithm).digest()
    offset = digest[-1] & 0x0F
    number = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(number % 10**digits).zfill(digits)


def verify(secret: str, code: str, now: float, last_step: int) -> int | None:
    """The step a code belongs to, if it is right, near now and newer than `last_step`."""
    typed = "".join(code.split())
    if len(typed) != DIGITS or not typed.isdigit():
        return None
    key, current = secret_bytes(secret), int(now // STEP)
    for step in range(current - WINDOW, current + WINDOW + 1):
        if step > last_step and hmac.compare_digest(totp(key, step * STEP), typed):
            return step
    return None


def otpauth_uri(secret: str, account: str, issuer: str) -> str:
    """The `otpauth://` link authenticator apps read (also shown as a QR code elsewhere)."""
    label = f"{quote(issuer, safe='')}:{quote(account, safe='')}"
    query = urlencode({"secret": secret, "issuer": issuer, "algorithm": "SHA1",
                       "digits": DIGITS, "period": STEP})  # fmt: skip
    return f"otpauth://totp/{label}?{query}"


def new_recovery_codes(count: int = 10) -> list[str]:
    """One-time codes for when the phone is gone, like `k7mq-x2pd`."""
    codes: set[str] = set()
    while len(codes) < count:
        raw = "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(8))
        codes.add(f"{raw[:4]}-{raw[4:]}")
    return sorted(codes)


def code_hash(code: str) -> str:
    """What is stored for a recovery code (case, spaces and dashes do not matter)."""
    normal = "".join(ch for ch in code.lower() if ch.isalnum())
    return hashlib.sha256(normal.encode("ascii", "ignore")).hexdigest()
