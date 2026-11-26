"""Command-line SQLite backup and restore utilities.

Examples:
    python -m media_bot.db.backup backup --database bot.sqlite3 --destination bot-backup.sqlite3
    python -m media_bot.db.backup restore --backup bot-backup.sqlite3 --database bot.sqlite3 --confirm-restore
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from media_bot.db.database import Database, DatabaseError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Back up or restore bot metadata in SQLite")
    commands = parser.add_subparsers(dest="command", required=True)

    backup = commands.add_parser("backup", help="create a consistent SQLite backup")
    backup.add_argument("--database", type=Path, required=True, help="live database path")
    backup.add_argument(
        "--destination",
        type=Path,
        required=True,
        help="new backup path; must not exist",
    )

    restore = commands.add_parser("restore", help="replace a database from a validated backup")
    restore.add_argument("--backup", type=Path, required=True, help="backup database path")
    restore.add_argument("--database", type=Path, required=True, help="destination database path")
    restore.add_argument(
        "--confirm-restore",
        action="store_true",
        help="required acknowledgement that the destination will be replaced",
    )
    return parser


async def _run(args: argparse.Namespace) -> Path:
    if args.command == "backup":
        return await Database(args.database).backup_to(args.destination)
    if not args.confirm_restore:
        raise ValueError("restore requires --confirm-restore because it replaces the destination")
    return await Database.restore_from(args.backup, args.database)


def main() -> None:
    args = _parser().parse_args()
    try:
        result = asyncio.run(_run(args))
    except (DatabaseError, FileExistsError, OSError, ValueError) as exc:
        print(f"Database {args.command} failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    print(f"Database {args.command} completed: {result}")


if __name__ == "__main__":
    main()
