"""Discover tags from GitHub repos."""

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
