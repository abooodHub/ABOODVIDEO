"""Argon2id Password Hashing and Cryptographic Verification."""
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError

# Production-grade Argon2id configuration
_hasher = PasswordHasher(
    time_cost=2,
    memory_cost=65536,  # 64 MiB RAM
    parallelism=2,
    hash_len=32,
    salt_len=16,
)


def hash_password(password: str) -> str:
    """Hash a raw password using Argon2id."""
    return _hasher.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a raw password against an Argon2id hash."""
    try:
        return _hasher.verify(hashed_password, plain_password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(hashed_password: str) -> bool:
    """Check if the stored hash requires rehashing due to parameter updates."""
    try:
        return _hasher.check_needs_rehash(hashed_password)
    except Exception:
        return True
