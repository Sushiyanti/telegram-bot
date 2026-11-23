from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from transfer_fixture import create_fixture, main, verify_fixture  # noqa: E402


def test_fixture_creation_and_checksum_verification(tmp_path) -> None:
    fixture = tmp_path / "fixture.bin"

    checksum = create_fixture(fixture, 2049)

    assert fixture.stat().st_size == 2049
    assert len(checksum) == 64
    assert (tmp_path / "fixture.bin.sha256").read_text() == (
        f"{checksum}  fixture.bin\n"
    )
    size, verified_checksum = verify_fixture(fixture, 2049, checksum)
    assert size == 2049
    assert verified_checksum == checksum


def test_fixture_verification_rejects_size_or_digest_mismatch(tmp_path) -> None:
    fixture = tmp_path / "fixture.bin"
    fixture.write_bytes(b"small fixture")
    correct = hashlib.sha256(b"small fixture").hexdigest()

    with pytest.raises(ValueError, match="size mismatch"):
        verify_fixture(fixture, len(b"small fixture") + 1, correct)
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        verify_fixture(fixture, len(b"small fixture"), "0" * 64)


def test_fixture_creation_does_not_overwrite_existing_files(tmp_path) -> None:
    fixture = tmp_path / "existing.bin"
    fixture.write_bytes(b"keep me")

    with pytest.raises(FileExistsError):
        create_fixture(fixture, 10)

    assert fixture.read_bytes() == b"keep me"


def test_cli_requires_explicit_confirmation_for_large_fixture(tmp_path) -> None:
    fixture = tmp_path / "large.bin"

    result = main([
        "create",
        "--output",
        str(fixture),
        "--size-bytes",
        "500000001",
    ])

    assert result == 2
    assert not fixture.exists()
    assert not (tmp_path / "large.bin.sha256").exists()
