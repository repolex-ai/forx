"""Manage the forx-index repo: pull manifests from storage repos into the local index."""

import json
import os
import subprocess
from pathlib import Path

from rich.console import Console

from .dispatch import fetch_json

console = Console()

DEFAULT_INDEX_PATH = Path.home() / "repos" / "repolex-forx" / "forx-index"


def get_index_path() -> Path:
    """Get the forx-index repo path."""
    path = Path(os.environ.get("FORX_INDEX_PATH", str(DEFAULT_INDEX_PATH)))
    if not path.exists():
        raise FileNotFoundError(f"forx-index repo not found at {path}. Clone it first.")
    return path


def sync_repo(full_name: str, storage_repo: str, index_path: Path) -> bool:
    """
    Sync a single repo's manifests from the storage repo into the index.
    Returns True if anything was updated.
    """
    org, name = full_name.split("/", 1)
    updated = False

    # Fetch repo-manifest.jsonld from storage repo
    repo_manifest = fetch_json(storage_repo, "repo-manifest.jsonld")
    if repo_manifest is None:
        return False

    # Write to index: repos/{org}/{repo}/repo-manifest.jsonld
    repo_dir = index_path / "repos" / org / name
    repo_dir.mkdir(parents=True, exist_ok=True)
    repo_file = repo_dir / "repo-manifest.jsonld"

    existing = None
    if repo_file.exists():
        with open(repo_file) as f:
            try:
                existing = json.load(f)
            except json.JSONDecodeError:
                existing = None

    if existing != repo_manifest:
        with open(repo_file, "w") as f:
            json.dump(repo_manifest, f, indent=2)
            f.write("\n")
        updated = True

    # Fetch per-commit manifests for parsed commits
    commits_dir = repo_dir / "commits"
    commits_dir.mkdir(parents=True, exist_ok=True)

    for commit in repo_manifest.get("repolex:trackedCommit", []):
        if commit.get("repolex:parseStatus") != "parsed":
            continue

        sha = commit.get("git:hexsha", "")
        if not sha:
            continue

        commit_file = commits_dir / f"commit-manifest-{sha}.jsonld"
        if commit_file.exists():
            continue

        commit_manifest = fetch_json(storage_repo, f"manifests/commit-manifest-{sha}.jsonld")
        if commit_manifest:
            with open(commit_file, "w") as f:
                json.dump(commit_manifest, f, indent=2)
                f.write("\n")
            updated = True

    return updated


def sync_all(conn, index_path: Path | None = None) -> int:  # noqa: conn used for DB query
    """
    Sync all parsed repos into the forx-index.
    Returns count of repos updated.
    """
    if index_path is None:
        index_path = get_index_path()

    # Get all tracked repos
    repos = conn.execute(
        """SELECT full_name, storage_repo FROM repos ORDER BY full_name"""
    ).fetchall()

    updated = 0
    for repo in repos:
        console.print(f"  [dim]Syncing {repo['full_name']}...[/]", end=" ")
        try:
            if sync_repo(repo["full_name"], repo["storage_repo"], index_path):
                console.print("[green]updated[/]")
                updated += 1
            else:
                console.print("[dim]unchanged[/]")
        except Exception as e:
            console.print(f"[red]error: {e}[/]")

    return updated


def push_index(index_path: Path | None = None, message: str = "Update index"):
    """Git add, commit, and push the forx-index repo."""
    if index_path is None:
        index_path = get_index_path()

    subprocess.run(
        ["git", "add", "-A"],
        cwd=index_path,
        check=True,
    )

    # Check if there are changes
    result = subprocess.run(
        ["git", "diff", "--staged", "--quiet"],
        cwd=index_path,
    )
    if result.returncode == 0:
        console.print("[dim]No changes to push[/]")
        return

    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=index_path,
        check=True,
    )
    subprocess.run(
        ["git", "push"],
        cwd=index_path,
        check=True,
    )
    console.print("[green]Pushed to forx-index[/]")
