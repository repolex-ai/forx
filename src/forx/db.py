"""SQLite database for tracking repos, tags, and parse jobs."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB_PATH = Path.home() / ".forx" / "forx.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
    id INTEGER PRIMARY KEY,
    org TEXT NOT NULL,
    name TEXT NOT NULL,
    full_name TEXT NOT NULL UNIQUE,  -- org/name
    storage_repo TEXT NOT NULL,       -- repolex-forx/org--name
    language TEXT,
    added_at TEXT NOT NULL,
    UNIQUE(org, name)
);

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY,
    repo_id INTEGER NOT NULL REFERENCES repos(id),
    version TEXT NOT NULL,     -- clean version: 1.0.0
    git_tag TEXT NOT NULL,     -- original git tag: v1.0.0
    commit_sha TEXT,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending, dispatched, parsing, complete, failed
    workflow_run_id TEXT,      -- GitHub Actions run ID
    dispatched_at TEXT,
    completed_at TEXT,
    error TEXT,
    UNIQUE(repo_id, version)
);

CREATE INDEX IF NOT EXISTS idx_tags_status ON tags(status);
CREATE INDEX IF NOT EXISTS idx_tags_repo_status ON tags(repo_id, status);
"""


def get_db(db_path: Path | None = None) -> sqlite3.Connection:
    """Get a database connection, creating schema if needed."""
    path = db_path or DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


def now() -> str:
    """UTC timestamp string."""
    return datetime.now(timezone.utc).isoformat()


def add_repo(conn: sqlite3.Connection, full_name: str) -> int:
    """Add a repo to track. Returns repo id."""
    org, name = full_name.split("/", 1)
    storage_repo = f"repolex-forx/{full_name.replace('/', '--')}"

    conn.execute(
        """INSERT OR IGNORE INTO repos (org, name, full_name, storage_repo, added_at)
           VALUES (?, ?, ?, ?, ?)""",
        (org, name, full_name, storage_repo, now()),
    )
    conn.commit()

    row = conn.execute("SELECT id FROM repos WHERE full_name = ?", (full_name,)).fetchone()
    return row["id"]


def add_tags(conn: sqlite3.Connection, repo_id: int, tags: list[tuple[str, str]]):
    """Add discovered tags for a repo. tags = [(version, git_tag), ...]"""
    conn.executemany(
        """INSERT OR IGNORE INTO tags (repo_id, version, git_tag, status)
           VALUES (?, ?, ?, 'pending')""",
        [(repo_id, version, git_tag) for version, git_tag in tags],
    )
    conn.commit()


def get_pending_tags(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    """Get next tags to parse, round-robin across repos."""
    return conn.execute(
        """SELECT t.id, t.version, t.git_tag, t.commit_sha,
                  r.full_name, r.storage_repo, r.org, r.name
           FROM tags t
           JOIN repos r ON t.repo_id = r.id
           WHERE t.status = 'pending'
           ORDER BY r.id, t.id
           LIMIT ?""",
        (limit,),
    ).fetchall()


def get_dispatched_tags(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Get tags currently dispatched/parsing."""
    return conn.execute(
        """SELECT t.id, t.version, t.git_tag, t.workflow_run_id, t.dispatched_at,
                  r.full_name, r.storage_repo
           FROM tags t
           JOIN repos r ON t.repo_id = r.id
           WHERE t.status IN ('dispatched', 'parsing')
           ORDER BY t.dispatched_at""",
    ).fetchall()


def mark_dispatched(conn: sqlite3.Connection, tag_id: int, run_id: str):
    """Mark a tag as dispatched to GitHub Actions."""
    conn.execute(
        "UPDATE tags SET status = 'dispatched', workflow_run_id = ?, dispatched_at = ? WHERE id = ?",
        (run_id, now(), tag_id),
    )
    conn.commit()


def mark_complete(conn: sqlite3.Connection, tag_id: int, commit_sha: str | None = None):
    """Mark a tag as successfully parsed."""
    conn.execute(
        "UPDATE tags SET status = 'complete', completed_at = ?, commit_sha = COALESCE(?, commit_sha) WHERE id = ?",
        (now(), commit_sha, tag_id),
    )
    conn.commit()


def mark_failed(conn: sqlite3.Connection, tag_id: int, error: str = ""):
    """Mark a tag as failed."""
    conn.execute(
        "UPDATE tags SET status = 'failed', completed_at = ?, error = ? WHERE id = ?",
        (now(), error, tag_id),
    )
    conn.commit()


def reset_stale_dispatched(conn: sqlite3.Connection, minutes: int = 60):
    """Reset dispatched tags that have been stuck for too long."""
    cutoff = datetime.now(timezone.utc).isoformat()
    rows = conn.execute(
        """UPDATE tags SET status = 'pending', workflow_run_id = NULL, dispatched_at = NULL
           WHERE status = 'dispatched'
           AND dispatched_at < datetime(?, '-' || ? || ' minutes')
           RETURNING id""",
        (cutoff, minutes),
    ).fetchall()
    conn.commit()
    return len(rows)


def get_stats(conn: sqlite3.Connection) -> dict:
    """Get summary stats."""
    row = conn.execute(
        """SELECT
            COUNT(DISTINCT r.id) as total_repos,
            COUNT(t.id) as total_tags,
            SUM(CASE WHEN t.status = 'pending' THEN 1 ELSE 0 END) as pending,
            SUM(CASE WHEN t.status = 'dispatched' THEN 1 ELSE 0 END) as dispatched,
            SUM(CASE WHEN t.status = 'complete' THEN 1 ELSE 0 END) as complete,
            SUM(CASE WHEN t.status = 'failed' THEN 1 ELSE 0 END) as failed
           FROM repos r
           LEFT JOIN tags t ON t.repo_id = r.id"""
    ).fetchone()
    return dict(row)
