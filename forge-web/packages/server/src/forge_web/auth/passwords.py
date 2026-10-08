"""Password hashing with argon2id, a few at a time so a login flood cannot exhaust memory."""

import asyncio

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_LENGTH = 10
MAX_LENGTH = 256
_hasher = PasswordHasher()  # argon2id with the library's current defaults
_slots = asyncio.Semaphore(4)
# Checked against when the account does not exist, so the answer takes as long either way.
_DUMMY = _hasher.hash("forge-web-dummy-password")


def password_problem(password: str) -> str | None:
    """Why a new password is not acceptable, or None."""
    if len(password) < MIN_LENGTH:
        return f"use at least {MIN_LENGTH} characters"
    if len(password) > MAX_LENGTH:
        return f"use at most {MAX_LENGTH} characters"
    if len(set(password)) < 4:
        return "use a less repetitive password"
    return None


async def hash_password(password: str) -> str:
    """An argon2id hash of the password."""
    async with _slots:
        return await asyncio.to_thread(_hasher.hash, password)


async def verify_password(stored: str | None, password: str) -> bool:
    """True if the password matches the stored hash (False for no hash, in the same time)."""

    def check() -> bool:
        try:
            return _hasher.verify(stored or _DUMMY, password) and stored is not None
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    async with _slots:
        return await asyncio.to_thread(check)
