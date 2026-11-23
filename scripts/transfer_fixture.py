#!/usr/bin/env python3
"""Create or verify exact-size transfer fixtures without contacting Telegram."""

from __future__ import annotations

import argparse
import hashlib
import re
import secrets
import shutil
import sys
from pathlib import Path

CHUNK_BYTES = 1024 * 1024
MAX_FIXTURE_BYTES = 4_000_000_000
LARGE_FIXTURE_BYTES = 500_000_000
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def create_fixture(path: Path, size_bytes: int) -> str:
    """Write a non-sensitive random fixture and return its SHA-256 hex digest."""
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int):
        raise ValueError("size must be an integer number of bytes")
    if not 0 < size_bytes <= MAX_FIXTURE_BYTES:
        raise ValueError(f"size must be between 1 and {MAX_FIXTURE_BYTES} bytes")
    sidecar = Path(f"{path}.sha256")
    if path.exists() or sidecar.exists():
        raise FileExistsError("output or checksum sidecar already exists")
    if not path.parent.is_dir():
        raise FileNotFoundError("output directory does not exist")
    if shutil.disk_usage(path.parent).free < size_bytes:
        raise OSError("insufficient free space for requested fixture")

    digest = hashlib.sha256()
    remaining = size_bytes
    created_file = False
    created_sidecar = False
    try:
        with path.open("xb") as output:
            created_file = True
            while remaining:
                block = secrets.token_bytes(min(CHUNK_BYTES, remaining))
                output.write(block)
                digest.update(block)
                remaining -= len(block)
            output.flush()
        checksum = digest.hexdigest()
        with sidecar.open("x", encoding="ascii") as manifest:
            created_sidecar = True
            manifest.write(f"{checksum}  {path.name}\n")
        return checksum
    except Exception:
        if created_file:
            path.unlink(missing_ok=True)
        if created_sidecar:
            sidecar.unlink(missing_ok=True)
        raise


def verify_fixture(path: Path, expected_size_bytes: int, expected_sha256: str) -> tuple[int, str]:
    """Stream a fixture and verify its exact size and SHA-256 digest."""
    if isinstance(expected_size_bytes, bool) or not isinstance(expected_size_bytes, int):
        raise ValueError("expected size must be an integer number of bytes")
    if expected_size_bytes < 0:
        raise ValueError("expected size cannot be negative")
    if not SHA256_RE.fullmatch(expected_sha256):
        raise ValueError("expected SHA-256 must be 64 lowercase hexadecimal characters")

    digest = hashlib.sha256()
    observed_size = 0
    with path.open("rb") as source:
        while block := source.read(CHUNK_BYTES):
            observed_size += len(block)
            digest.update(block)
    observed_digest = digest.hexdigest()
    if observed_size != expected_size_bytes:
        raise ValueError(
            f"size mismatch: expected {expected_size_bytes}, observed {observed_size}"
        )
    if observed_digest != expected_sha256:
        raise ValueError("SHA-256 mismatch")
    return observed_size, observed_digest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare/check transfer fixtures offline; this tool never calls Telegram."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="create an exact-size random fixture")
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--size-bytes", type=int, required=True)
    create.add_argument(
        "--confirm-large",
        action="store_true",
        help=f"required when size exceeds {LARGE_FIXTURE_BYTES} bytes",
    )
    verify = commands.add_parser("verify", help="verify a downloaded fixture")
    verify.add_argument("--input", type=Path, required=True)
    verify.add_argument("--expected-size-bytes", type=int, required=True)
    verify.add_argument("--expected-sha256", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            if args.size_bytes > LARGE_FIXTURE_BYTES and not args.confirm_large:
                raise ValueError(
                    "fixtures larger than 500 MB require the explicit --confirm-large flag"
                )
            checksum = create_fixture(args.output, args.size_bytes)
            print(f"created_bytes={args.size_bytes}")
            print(f"sha256={checksum}")
            print(f"checksum_file={args.output}.sha256")
            return 0
        size, checksum = verify_fixture(
            args.input, args.expected_size_bytes, args.expected_sha256
        )
        print(f"verified_bytes={size}")
        print(f"sha256={checksum}")
        return 0
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
