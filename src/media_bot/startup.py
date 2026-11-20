"""Local startup prerequisite checks."""

from __future__ import annotations

import tempfile
from pathlib import Path


class StartupPrerequisiteError(RuntimeError):
    """Raised when local filesystem prerequisites are not met."""


def prepare_state_directory(database_path: Path) -> None:
    """Create the database parent directory and verify it is writable."""
    if database_path.exists() and not database_path.is_file():
        raise StartupPrerequisiteError("DB_PATH exists but is not a file")

    state_directory = database_path.parent
    try:
        state_directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=".telegram-storage-bot-write-test-",
            dir=state_directory,
            delete=True,
        ) as test_file:
            test_file.write(b"ok")
            test_file.flush()
    except OSError as exc:
        raise StartupPrerequisiteError(
            "The database directory could not be created or is not writable"
        ) from exc
