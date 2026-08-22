"""Password hashing.

05_SECURITY.md §10.2: Argon2id only, with parameters checked against current
OWASP guidance at implementation time rather than copied from a stale constant.
No custom cryptography anywhere — this module is a thin wrapper over
`argon2-cffi`, which is the reference binding for the Argon2 reference
implementation.
"""

from __future__ import annotations

import hashlib
import hmac

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

# OWASP Password Storage Cheat Sheet, Argon2id configuration (checked 2026-08):
# m=19 MiB, t=2, p=1 is the recommended baseline. Memory cost dominates GPU
# resistance, so it is raised to 64 MiB here — the login path runs a handful of
# times a day on a server that is not otherwise busy (02_ARCHITECTURE.md §7.9),
# which makes the extra cost free in practice and expensive for an attacker.
_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=64 * 1024,  # KiB
    parallelism=1,
    hash_len=32,
    salt_len=16,
)

# Verified against at login when the submitted email matches no account, so that
# an unknown email costs the same wall-clock time as a wrong password. Without
# this, response timing alone enumerates valid accounts, which
# 01_REQUIREMENTS.md forbids explicitly.
_DUMMY_HASH = _hasher.hash("timing-equalisation-placeholder-not-a-credential")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Return whether the password matches. Never raises for a bad password."""
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def waste_time_like_a_verification() -> None:
    """Burn the same work a real verification would, for an unknown account."""
    verify_password("timing-equalisation-placeholder-not-a-credential", _DUMMY_HASH)


def needs_rehash(password_hash: str) -> bool:
    """True when a stored hash predates the current cost parameters.

    Called on successful login so that raising the parameters later upgrades
    accounts transparently instead of leaving old hashes weak forever.
    """
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        # An unparseable hash cannot be verified against, so the account is
        # already unusable; flagging it for rehash is the safe answer.
        return True


def hash_session_token(token: str) -> str:
    """SHA-256 of a session token.

    Not a password hash and deliberately not slow: the token is 256 bits of
    `secrets` output, so there is no dictionary to attack and no reason to pay
    Argon2's cost on every authenticated request. Hashing at all is what stops
    database read access from yielding usable session cookies.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokens_equal(a: str, b: str) -> bool:
    """Constant-time comparison for token material."""
    return hmac.compare_digest(a, b)
