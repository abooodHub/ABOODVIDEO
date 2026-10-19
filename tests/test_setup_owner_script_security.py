"""Regression checks for the manual owner-setup helper."""
from pathlib import Path


def test_setup_owner_script_requires_external_password() -> None:
    script = (Path(__file__).resolve().parents[1] / "scripts" / "setup_test_owner.py").read_text(
        encoding="utf-8"
    )

    assert "ABOODVIDEO_SETUP_OWNER_PASSWORD" in script
    assert 'hash_password("' not in script
    assert script.count("hash_password(password)") == 2
    assert "ready with password" not in script
