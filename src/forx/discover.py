"""Discover tags and repos from GitHub."""

import json
import subprocess


def get_git_tags(repo_full_name: str) -> list[str]:
    """Get all tags from a GitHub repo using git ls-remote (no clone needed)."""
    result = subprocess.run(
        ["git", "ls-remote", "--tags", f"https://github.com/{repo_full_name}.git"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to list tags for {repo_full_name}: {result.stderr.strip()}")

    tags = []
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        # Format: <sha>\trefs/tags/<tagname>
        # Skip ^{} dereferenced tags
        ref = line.split("\t", 1)[1]
        if ref.endswith("^{}"):
            continue
        tag = ref.removeprefix("refs/tags/")
        tags.append(tag)

    return tags


def get_head_sha(repo_full_name: str) -> str:
    """Get the HEAD commit SHA for a repo's default branch."""
    result = subprocess.run(
        ["git", "ls-remote", f"https://github.com/{repo_full_name}.git", "HEAD"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to get HEAD for {repo_full_name}: {result.stderr.strip()}")

    line = result.stdout.strip().split("\n")[0]
    return line.split("\t")[0]


def discover_repo(repo_full_name: str) -> list[str]:
    """
    Discover all tags for a repo. Returns them as-is, no filtering.
    The git tag is what we checkout, no transformation needed.
    """
    return get_git_tags(repo_full_name)


def list_org_repos(org: str, include_forks: bool = False) -> list[dict]:
    """
    List all repos in a GitHub org/user using gh CLI.
    Returns list of {full_name, language, stars, fork} dicts.
    """
    args = [
        "gh", "api", "--paginate",
        f"/orgs/{org}/repos",
        "--jq", ".[] | {full_name: .full_name, language: .language, stars: .stargazers_count, fork: .fork, archived: .archived}",
    ]
    result = subprocess.run(args, capture_output=True, text=True, timeout=60)

    if result.returncode != 0:
        # Try as user instead of org
        args[3] = f"/users/{org}/repos"
        result = subprocess.run(args, capture_output=True, text=True, timeout=60)

    if result.returncode != 0:
        raise RuntimeError(f"Failed to list repos for {org}: {result.stderr.strip()}")

    repos = []
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        try:
            repo = json.loads(line)
            if repo.get("archived"):
                continue
            if not include_forks and repo.get("fork"):
                continue
            repos.append(repo)
        except json.JSONDecodeError:
            continue

    return repos
