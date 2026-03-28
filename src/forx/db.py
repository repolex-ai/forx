"""SQLite database for tracking repos, tags, and parse jobs."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB_PATH = Path.home() / ".forx" / "forx.db"

# The parser version used by the current workflow.
# Bump this when repolex-parser-py gets a new release that
# changes output format. Tags parsed with an older version
# can be invalidated with `forx reparse`.
PARSER_VERSION = "v0.0.9"

MIGRATIONS = [
    # v1: initial schema
    """
    CREATE TABLE IF NOT EXISTS repos (
        id INTEGER PRIMARY KEY,
        org TEXT NOT NULL,
        name TEXT NOT NULL,
        full_name TEXT NOT NULL UNIQUE,  -- org/name
        storage_repo TEXT NOT NULL,       -- repolex-forx/org--name
        language TEXT,
        head_only INTEGER NOT NULL DEFAULT 0,  -- 1 = no tags, parse HEAD
        last_head_parsed TEXT,                  -- when HEAD was last parsed
        head_sha TEXT,                          -- last parsed HEAD commit
        added_at TEXT NOT NULL,
        UNIQUE(org, name)
    );

    CREATE TABLE IF NOT EXISTS tags (
        id INTEGER PRIMARY KEY,
        repo_id INTEGER NOT NULL REFERENCES repos(id),
        git_tag TEXT NOT NULL,     -- the actual git tag (used for checkout)
        commit_sha TEXT,
        status TEXT NOT NULL DEFAULT 'pending',  -- pending, dispatched, parsing, complete, failed
        workflow_run_id TEXT,      -- GitHub Actions run ID
        dispatched_at TEXT,
        completed_at TEXT,
        error TEXT,
        UNIQUE(repo_id, git_tag)
    );

    CREATE INDEX IF NOT EXISTS idx_tags_status ON tags(status);
    CREATE INDEX IF NOT EXISTS idx_tags_repo_status ON tags(repo_id, status);
    """,
]


def get_db(db_path: Path | None = None) -> sqlite3.Connection:
    """Get a database connection, creating schema if needed."""
    path = db_path or DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    # Check if we need to migrate (fresh DB or old schema)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

    if "repos" not in tables:
        # Fresh DB - apply schema
        conn.executescript(MIGRATIONS[0])
    elif "version" not in {r[1] for r in conn.execute("PRAGMA table_info(tags)").fetchall()}:
        # Already on new schema (git_tag based, no version column)
        pass
    else:
        # Old schema with 'version' column - migrate
        _migrate_v0_to_v1(conn)

    # Add parser_version column if missing
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tags)").fetchall()}
    if "parser_version" not in cols:
        conn.execute("ALTER TABLE tags ADD COLUMN parser_version TEXT")
        conn.commit()

    return conn


def _migrate_v0_to_v1(conn: sqlite3.Connection):
    """Migrate from old schema (version+git_tag) to new (git_tag only)."""
    conn.executescript("""
        -- Add new columns to repos
        ALTER TABLE repos ADD COLUMN head_only INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE repos ADD COLUMN last_head_parsed TEXT;
        ALTER TABLE repos ADD COLUMN head_sha TEXT;

        -- Recreate tags table without version column
        CREATE TABLE tags_new (
            id INTEGER PRIMARY KEY,
            repo_id INTEGER NOT NULL REFERENCES repos(id),
            git_tag TEXT NOT NULL,
            commit_sha TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            workflow_run_id TEXT,
            dispatched_at TEXT,
            completed_at TEXT,
            error TEXT,
            UNIQUE(repo_id, git_tag)
        );

        INSERT OR IGNORE INTO tags_new (id, repo_id, git_tag, commit_sha, status, workflow_run_id, dispatched_at, completed_at, error)
            SELECT id, repo_id, git_tag, commit_sha, status, workflow_run_id, dispatched_at, completed_at, error
            FROM tags;

        DROP TABLE tags;
        ALTER TABLE tags_new RENAME TO tags;

        CREATE INDEX IF NOT EXISTS idx_tags_status ON tags(status);
        CREATE INDEX IF NOT EXISTS idx_tags_repo_status ON tags(repo_id, status);
    """)


def now() -> str:
    """UTC timestamp string."""
    return datetime.now(timezone.utc).isoformat()


def add_repo(conn: sqlite3.Connection, full_name: str, head_only: bool = False) -> int:
    """Add a repo to track. Returns repo id."""
    org, name = full_name.split("/", 1)
    storage_repo = f"repolex-forx/{full_name.replace('/', '--')}"

    conn.execute(
        """INSERT OR IGNORE INTO repos (org, name, full_name, storage_repo, head_only, added_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (org, name, full_name, storage_repo, int(head_only), now()),
    )
    conn.commit()

    row = conn.execute("SELECT id FROM repos WHERE full_name = ?", (full_name,)).fetchone()
    return row["id"]


def add_tags(conn: sqlite3.Connection, repo_id: int, git_tags: list[str]):
    """Add discovered tags for a repo."""
    conn.executemany(
        """INSERT OR IGNORE INTO tags (repo_id, git_tag, status)
           VALUES (?, ?, 'pending')""",
        [(repo_id, tag) for tag in git_tags],
    )
    conn.commit()


def get_pending_tags(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    """
    Get next tags to parse: one per repo, skipping repos that already
    have a dispatched job. This ensures sequential parsing within a repo
    (so each job builds on the previous one's blob cache) while still
    running up to `limit` repos in parallel.
    """
    return conn.execute(
        """SELECT t.id, t.git_tag, t.commit_sha,
                  r.full_name, r.storage_repo, r.org, r.name
           FROM tags t
           JOIN repos r ON t.repo_id = r.id
           WHERE t.status = 'pending'
             AND r.id NOT IN (
               SELECT DISTINCT repo_id FROM tags WHERE status IN ('dispatched', 'parsing')
             )
           GROUP BY r.id
           HAVING t.id = MIN(t.id)
           ORDER BY r.id
           LIMIT ?""",
        (limit,),
    ).fetchall()


def get_dispatched_tags(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Get tags currently dispatched/parsing."""
    return conn.execute(
        """SELECT t.id, t.git_tag, t.workflow_run_id, t.dispatched_at,
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
    """Mark a tag as successfully parsed with the current parser version."""
    conn.execute(
        "UPDATE tags SET status = 'complete', completed_at = ?, commit_sha = COALESCE(?, commit_sha), parser_version = ? WHERE id = ?",
        (now(), commit_sha, PARSER_VERSION, tag_id),
    )
    conn.commit()


def mark_failed(conn: sqlite3.Connection, tag_id: int, error: str = ""):
    """Mark a tag as failed."""
    conn.execute(
        "UPDATE tags SET status = 'failed', completed_at = ?, error = ? WHERE id = ?",
        (now(), error, tag_id),
    )
    conn.commit()


def mark_head_parsed(conn: sqlite3.Connection, repo_id: int, sha: str):
    """Mark a HEAD-only repo as parsed."""
    conn.execute(
        "UPDATE repos SET last_head_parsed = ?, head_sha = ? WHERE id = ?",
        (now(), sha, repo_id),
    )
    conn.commit()


def get_head_repos_needing_parse(conn: sqlite3.Connection, cooldown_days: int = 7) -> list[sqlite3.Row]:
    """Get HEAD-only repos that haven't been parsed recently."""
    return conn.execute(
        """SELECT id, full_name, storage_repo, head_sha, last_head_parsed
           FROM repos
           WHERE head_only = 1
             AND (last_head_parsed IS NULL
                  OR last_head_parsed < datetime('now', '-' || ? || ' days'))
           ORDER BY last_head_parsed NULLS FIRST""",
        (cooldown_days,),
    ).fetchall()


def invalidate_old_parses(conn: sqlite3.Connection) -> int:
    """Reset completed tags that were parsed with an older parser version."""
    rows = conn.execute(
        """UPDATE tags SET status = 'pending', workflow_run_id = NULL,
                          dispatched_at = NULL, completed_at = NULL, error = NULL
           WHERE status = 'complete'
             AND (parser_version IS NULL OR parser_version != ?)
           RETURNING id""",
        (PARSER_VERSION,),
    ).fetchall()
    conn.commit()
    return len(rows)


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
