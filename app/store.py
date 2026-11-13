"""Persistent baseline storage for notice-directory patrols.

The store is deliberately append-only: a URL that has once appeared in a
successful patrol remains in the baseline.  That makes page reshuffles and
cross-page duplicates harmless and guarantees that a notice is reported as
"new" at most once, even when several patrols run concurrently or the
container is restarted.

A failed patrol never writes to the notice baseline.  Only the status columns
are updated, so the next successful patrol compares against the last complete
baseline and can report everything that was missed in the meantime.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

DEFAULT_DB_PATH = os.getenv(
    "PATROL_DB_PATH",
    str(Path(__file__).resolve().parent.parent / "data" / "patrol.sqlite3"),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS patrol_directories (
    directory_key TEXT PRIMARY KEY,
    list_url TEXT NOT NULL,
    baseline_complete INTEGER NOT NULL DEFAULT 0,
    baseline_count INTEGER NOT NULL DEFAULT 0,
    first_patrol_at TEXT,
    last_patrol_at TEXT,
    last_success_at TEXT,
    last_status TEXT,
    last_failure_count INTEGER NOT NULL DEFAULT 0,
    last_new_count INTEGER NOT NULL DEFAULT 0,
    last_note TEXT,
    last_run_id TEXT
);

CREATE TABLE IF NOT EXISTS patrol_notices (
    directory_key TEXT NOT NULL,
    url_key TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (directory_key, url_key)
);

CREATE TABLE IF NOT EXISTS patrol_additions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    directory_key TEXT NOT NULL,
    url_key TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT,
    detected_at TEXT NOT NULL,
    run_id TEXT NOT NULL,
    UNIQUE (directory_key, url_key)
);

CREATE INDEX IF NOT EXISTS patrol_additions_recent
    ON patrol_additions (directory_key, id DESC);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class NoticeRow:
    url_key: str
    url: str
    title: str


@dataclass
class CommitResult:
    first_patrol: bool
    baseline_count: int
    new_notices: list[NoticeRow]


@dataclass
class PatrolState:
    exists: bool = False
    baseline_complete: bool = False
    baseline_count: int = 0
    first_patrol_at: str | None = None
    last_patrol_at: str | None = None
    last_success_at: str | None = None
    last_status: str | None = None
    last_failure_count: int = 0
    last_new_count: int = 0
    last_note: str | None = None


class PatrolStore:
    """SQLite-backed baseline, safe to share between concurrent requests."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = str(path or DEFAULT_DB_PATH)
        self._init_lock = threading.Lock()
        self._ensure_schema()

    # ------------------------------------------------------------------
    # Connection helpers
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _ensure_schema(self) -> None:
        with self._init_lock:
            with closing(self._connect()) as connection:
                connection.executescript(SCHEMA)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_state(self, directory_key: str) -> PatrolState:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM patrol_directories WHERE directory_key = ?",
                (directory_key,),
            ).fetchone()
        if row is None:
            return PatrolState()
        return PatrolState(
            exists=True,
            baseline_complete=bool(row["baseline_complete"]),
            baseline_count=int(row["baseline_count"] or 0),
            first_patrol_at=row["first_patrol_at"],
            last_patrol_at=row["last_patrol_at"],
            last_success_at=row["last_success_at"],
            last_status=row["last_status"],
            last_failure_count=int(row["last_failure_count"] or 0),
            last_new_count=int(row["last_new_count"] or 0),
            last_note=row["last_note"],
        )

    def recent_additions(self, directory_key: str, limit: int = 20) -> list[dict[str, str | None]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT url, title, detected_at
                FROM patrol_additions
                WHERE directory_key = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (directory_key, int(limit)),
            ).fetchall()
        return [
            {"url": row["url"], "title": row["title"], "detected_at": row["detected_at"]}
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def commit_success(
        self,
        directory_key: str,
        list_url: str,
        notices: Sequence[NoticeRow],
        run_id: str,
    ) -> CommitResult:
        """Atomically compare the patrol against the baseline.

        The first complete patrol only establishes the baseline.  Later
        patrols insert unseen URL keys and return exactly those rows as new.
        ``BEGIN IMMEDIATE`` serialises writers, so two patrols finishing at the
        same time cannot both report the same notice.
        """

        now = utcnow()
        unique: list[NoticeRow] = []
        seen_keys: set[str] = set()
        for notice in notices:
            if notice.url_key in seen_keys:
                continue
            seen_keys.add(notice.url_key)
            unique.append(notice)

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT baseline_complete FROM patrol_directories WHERE directory_key = ?",
                (directory_key,),
            ).fetchone()
            first_patrol = row is None or not bool(row["baseline_complete"])

            if first_patrol:
                connection.execute(
                    """
                    INSERT INTO patrol_directories (
                        directory_key, list_url, baseline_complete, baseline_count,
                        first_patrol_at, last_patrol_at, last_success_at, last_status,
                        last_failure_count, last_new_count, last_note, last_run_id
                    ) VALUES (?, ?, 1, ?, ?, ?, ?, 'success', 0, 0, NULL, ?)
                    ON CONFLICT(directory_key) DO UPDATE SET
                        list_url = excluded.list_url,
                        baseline_complete = 1,
                        baseline_count = excluded.baseline_count,
                        first_patrol_at = COALESCE(patrol_directories.first_patrol_at, excluded.first_patrol_at),
                        last_patrol_at = excluded.last_patrol_at,
                        last_success_at = excluded.last_success_at,
                        last_status = 'success',
                        last_failure_count = 0,
                        last_new_count = 0,
                        last_note = NULL,
                        last_run_id = excluded.last_run_id
                    """,
                    (directory_key, list_url, len(unique), now, now, now, run_id),
                )
                for notice in unique:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO patrol_notices (
                            directory_key, url_key, url, title, first_seen_at, last_seen_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (directory_key, notice.url_key, notice.url, notice.title, now, now),
                    )
                baseline_count = len(unique)
                new_rows: list[NoticeRow] = []
            else:
                existing_keys = {
                    item["url_key"]
                    for item in connection.execute(
                        "SELECT url_key FROM patrol_notices WHERE directory_key = ?",
                        (directory_key,),
                    ).fetchall()
                }
                new_rows = [notice for notice in unique if notice.url_key not in existing_keys]

                for notice in unique:
                    if notice.url_key in existing_keys:
                        connection.execute(
                            """
                            UPDATE patrol_notices
                            SET last_seen_at = ?,
                                title = CASE
                                    WHEN title IS NULL OR title = '' THEN ?
                                    ELSE title
                                END
                            WHERE directory_key = ? AND url_key = ?
                            """,
                            (now, notice.title, directory_key, notice.url_key),
                        )
                    else:
                        connection.execute(
                            """
                            INSERT OR IGNORE INTO patrol_notices (
                                directory_key, url_key, url, title, first_seen_at, last_seen_at
                            ) VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (directory_key, notice.url_key, notice.url, notice.title, now, now),
                        )

                for notice in new_rows:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO patrol_additions (
                            directory_key, url_key, url, title, detected_at, run_id
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (directory_key, notice.url_key, notice.url, notice.title, now, run_id),
                    )

                baseline_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM patrol_notices WHERE directory_key = ?",
                        (directory_key,),
                    ).fetchone()[0]
                )
                connection.execute(
                    """
                    UPDATE patrol_directories SET
                        list_url = ?,
                        baseline_count = ?,
                        last_patrol_at = ?,
                        last_success_at = ?,
                        last_status = 'success',
                        last_failure_count = 0,
                        last_new_count = ?,
                        last_note = NULL,
                        last_run_id = ?
                    WHERE directory_key = ?
                    """,
                    (
                        list_url,
                        baseline_count,
                        now,
                        now,
                        len(new_rows),
                        run_id,
                        directory_key,
                    ),
                )

            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

        return CommitResult(
            first_patrol=first_patrol,
            baseline_count=baseline_count,
            new_notices=new_rows,
        )

    def mark_failure(
        self,
        directory_key: str,
        list_url: str,
        failure_count: int,
        note: str,
        run_id: str,
    ) -> None:
        """Record a failed patrol without touching the baseline."""

        now = utcnow()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO patrol_directories (
                    directory_key, list_url, baseline_complete, baseline_count,
                    last_patrol_at, last_status, last_failure_count, last_new_count,
                    last_note, last_run_id
                ) VALUES (?, ?, 0, 0, ?, 'failed', ?, 0, ?, ?)
                ON CONFLICT(directory_key) DO UPDATE SET
                    list_url = excluded.list_url,
                    last_patrol_at = excluded.last_patrol_at,
                    last_status = 'failed',
                    last_failure_count = excluded.last_failure_count,
                    last_new_count = 0,
                    last_note = excluded.last_note,
                    last_run_id = excluded.last_run_id
                """,
                (directory_key, list_url, now, failure_count, note, run_id),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    # ------------------------------------------------------------------
    # Async wrappers used by the request path
    # ------------------------------------------------------------------

    async def get_state_async(self, directory_key: str) -> PatrolState:
        return await asyncio.to_thread(self.get_state, directory_key)

    async def recent_additions_async(
        self, directory_key: str, limit: int = 20
    ) -> list[dict[str, str | None]]:
        return await asyncio.to_thread(self.recent_additions, directory_key, limit)

    async def commit_success_async(
        self,
        directory_key: str,
        list_url: str,
        notices: Sequence[NoticeRow],
        run_id: str,
    ) -> CommitResult:
        return await asyncio.to_thread(
            self.commit_success, directory_key, list_url, notices, run_id
        )

    async def mark_failure_async(
        self,
        directory_key: str,
        list_url: str,
        failure_count: int,
        note: str,
        run_id: str,
    ) -> None:
        await asyncio.to_thread(
            self.mark_failure,
            directory_key,
            list_url,
            failure_count,
            note,
            run_id,
        )
