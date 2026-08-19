"""Rebuild structured ``body_text`` for historical HTML emails.

Run from ``backend/``:
    python scripts/rebuild_body_text.py --dry-run
    python scripts/rebuild_body_text.py

The script is idempotent: it only updates rows whose rebuilt text differs from
the current value, so re-running it after completion changes nothing.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.engine import make_url  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.services.ingest import _html_to_text  # noqa: E402


def _connect(database_url: str, dry_run: bool) -> sqlite3.Connection:
    url = make_url(database_url)
    if url.drivername not in ("sqlite", "sqlite+pysqlite"):
        raise ValueError(f"Only SQLite databases are supported, got: {url.drivername}")
    db_path = Path(url.database)
    if db_path.name in ("", ":memory:"):
        raise ValueError("The rebuild script requires a file-backed SQLite database")
    if dry_run:
        uri = f"file:{db_path.resolve()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    else:
        conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Rebuild email.body_text from stored body_html."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the number of rows that would change without writing",
    )
    args = parser.parse_args(argv)

    conn = _connect(get_settings().database_url, args.dry_run)
    try:
        rows = conn.execute(
            "SELECT id, body_html, body_text FROM emails "
            "WHERE body_html IS NOT NULL AND TRIM(body_html) <> ''"
        ).fetchall()
        pending = []
        for row in rows:
            rebuilt = _html_to_text(row["body_html"])
            if rebuilt != (row["body_text"] or ""):
                pending.append((rebuilt, row["id"]))
        if args.dry_run:
            print(f"Dry run: would update {len(pending)} of {len(rows)} email(s).")
            return 0
        conn.executemany("UPDATE emails SET body_text = ? WHERE id = ?", pending)
        conn.commit()
        print(f"Updated {len(pending)} email(s).")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
