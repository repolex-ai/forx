"""Spider: crawl parsed repo manifests and queue their dependencies."""

import json
import urllib.request
import urllib.error

from rich.console import Console

from . import db, discover

console = Console()


def _fetch_json(url: str) -> dict | None:
    """Fetch JSON from a URL."""
    try:
        req = urllib.request.Request(url)
        req.add_header("Cache-Control", "no-cache")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def fetch_manifest(storage_repo: str) -> dict | None:
    """Fetch manifest from a storage repo. Tries new format first, falls back to legacy."""
    base = f"https://raw.githubusercontent.com/{storage_repo}/main"

    # Try new format: index.json → latest per-commit manifest
    index = _fetch_json(f"{base}/index.json")
    if index and index.get("versions"):
        latest_commit = index["versions"][0].get("commit", "")
        if latest_commit:
            per_commit = _fetch_json(f"{base}/manifests/{latest_commit}.json")
            if per_commit:
                # Wrap in legacy format so get_dependencies_from_manifest works
                return {"versions": [per_commit]}

    # Fall back to legacy manifest.json
    return _fetch_json(f"{base}/manifest.json")


def get_dependencies_from_manifest(manifest: dict) -> list[dict]:
    """Extract all unique dependencies across all versions."""
    seen = set()
    deps = []
    for version in manifest.get("versions", []):
        for dep in version.get("dependencies", []):
            org = dep.get("githubOrg", "")
            repo = dep.get("githubRepo", "")
            if org and repo:
                full_name = f"{org}/{repo}"
                if full_name not in seen:
                    seen.add(full_name)
                    deps.append({"full_name": full_name, "package": dep.get("packageName", "")})
    return deps


def spider_repo(conn, full_name: str, storage_repo: str) -> list[str]:
    """
    Spider a single repo: fetch its manifest, find dependencies,
    add new ones to the DB. Returns list of newly added repos.
    """
    manifest = fetch_manifest(storage_repo)
    if manifest is None:
        return []

    deps = get_dependencies_from_manifest(manifest)
    if not deps:
        return []

    added = []
    for dep in deps:
        dep_name = dep["full_name"]

        # Skip self-references
        if dep_name == full_name:
            continue

        # Check if already tracked
        existing = conn.execute(
            "SELECT id FROM repos WHERE full_name = ?", (dep_name,)
        ).fetchone()

        if existing:
            continue

        # Try to discover tags
        try:
            tags = discover.discover_repo(dep_name)
        except Exception:
            console.print(f"    [yellow]Could not access {dep_name}[/]")
            continue

        if not tags:
            console.print(f"    [dim]{dep_name} - no tags, skipping[/]")
            continue

        # Add to DB
        repo_id = db.add_repo(conn, dep_name)
        db.add_tags(conn, repo_id, tags)
        added.append(dep_name)
        console.print(f"    [green]+ {dep_name}[/] ({len(tags)} tags)")

    return added


def spider_all(conn) -> int:
    """
    Spider all parsed repos: fetch manifests, discover dependency repos,
    add them to the queue. Returns total new repos added.
    """
    repos = conn.execute(
        "SELECT full_name, storage_repo FROM repos"
    ).fetchall()

    total_added = []

    for repo in repos:
        console.print(f"[cyan]Spidering {repo['full_name']}...[/]")
        added = spider_repo(conn, repo["full_name"], repo["storage_repo"])
        total_added.extend(added)

    if total_added:
        console.print(f"\n[bold green]Added {len(total_added)} new repos from dependencies:[/]")
        for name in total_added:
            console.print(f"  {name}")
    else:
        console.print("\n[dim]No new dependencies found[/]")

    return len(total_added)
