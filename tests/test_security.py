"""Security, Argon2id, and Signed URL Tokens Unit Tests."""
import time
import pytest
from app.security.hashing import hash_password, verify_password
from app.security.tokens import (
    generate_secure_token,
    hash_token,
    generate_signed_url_token,
    verify_signed_url_token,
)


def test_argon2id_password_hashing():
    raw_pass = "SuperSecretPassword123!"
    hashed = hash_password(raw_pass)

    assert hashed.startswith("$argon2id$")
    assert verify_password(raw_pass, hashed) is True
    assert verify_password("WrongPassword!", hashed) is False


def test_token_generation_and_hashing():
    t1 = generate_secure_token()
    t2 = generate_secure_token()
    assert t1 != t2
    assert len(t1) >= 32

    h1 = hash_token(t1)
    h2 = hash_token(t1)
    assert h1 == h2
    assert len(h1) == 64  # SHA256 hex


def test_signed_url_tokens():
    video_id = "11111111-2222-3333-4444-555555555555"
    token = generate_signed_url_token(video_id=video_id, user_id="user123", quality="720p", ttl_seconds=10)

    # Valid token verification
    assert verify_signed_url_token(video_id=video_id, token=token, user_id="user123", quality="720p") is True

    # Tampered video_id
    assert verify_signed_url_token(video_id="tampered-video-id", token=token, user_id="user123", quality="720p") is False

    # Tampered user_id
    assert verify_signed_url_token(video_id=video_id, token=token, user_id="different_user", quality="720p") is False

    # Expired token
    expired_token = generate_signed_url_token(video_id=video_id, ttl_seconds=-5)
    assert verify_signed_url_token(video_id=video_id, token=expired_token) is False
