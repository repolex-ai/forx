"""Discover version tags from GitHub repos."""

import re
import subprocess
from collections import defaultdict
from packaging.version import parse as parse_version, InvalidVersion


SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


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


def filter_semver(tags: list[str]) -> list[tuple[str, str]]:
    """
    Filter to semver tags. Returns [(clean_version, original_git_tag), ...].
    Handles v-prefixed and plain tags.
    """
    results = []
    for tag in tags:
        clean = tag.lstrip("v")
        if SEMVER_RE.match(clean):
            results.append((clean, tag))
    return results


def select_smart(tag_pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """
    Select latest patch per major.minor series.
    Input/output: [(clean_version, git_tag), ...]
    """
    by_major_minor: dict[tuple, list[tuple[str, str]]] = defaultdict(list)

    for clean_ver, git_tag in tag_pairs:
        try:
            v = parse_version(clean_ver)
            key = (v.major, v.minor)
            by_major_minor[key].append((clean_ver, git_tag))
        except InvalidVersion:
            continue

    result = []
    for pairs in by_major_minor.values():
        latest = max(pairs, key=lambda p: parse_version(p[0]))
        result.append(latest)

    return sorted(result, key=lambda p: parse_version(p[0]))


def discover_repo(repo_full_name: str) -> list[tuple[str, str]]:
    """
    Discover parseable version tags for a repo.
    Returns [(clean_version, git_tag), ...] sorted by version.
    """
    all_tags = get_git_tags(repo_full_name)
    semver = filter_semver(all_tags)

    if not semver:
        return []

    return select_smart(semver)
