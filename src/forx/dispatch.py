"""Dispatch and monitor GitHub Actions workflow runs."""

import json
import subprocess
import time
import urllib.request
import urllib.error


WORKFLOW_FILE = "parse.yml"
WORKFLOW_REPO = "repolex-ai/forx"
MAX_CONCURRENT = 20
POLL_INTERVAL = 120  # seconds between manifest checks (no API rate limit concern)
STALE_TIMEOUT = 18000  # 5 hours - big repos like pixeltable can take a long time


def gh_run(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """Run a gh CLI command."""
    result = subprocess.run(
        ["gh"] + args,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {result.stderr.strip()}")
    return result


def dispatch_workflow(repo: str, tag: str, storage_repo: str) -> str:
    """
    Dispatch a parse workflow run.
    Returns the workflow run ID.
    """
    gh_run([
        "workflow", "run", WORKFLOW_FILE,
        "--repo", WORKFLOW_REPO,
        "-f", f"repo={repo}",
        "-f", f"tag={tag}",
        "-f", f"storage_repo={storage_repo}",
    ])

    # gh workflow run doesn't return the run ID, so we need to find it
    time.sleep(2)

    result = gh_run([
        "run", "list",
        "--repo", WORKFLOW_REPO,
        "--workflow", WORKFLOW_FILE,
        "--limit", "1",
        "--json", "databaseId,status,event",
    ])

    runs = json.loads(result.stdout)
    if runs:
        return str(runs[0]["databaseId"])

    raise RuntimeError(f"Could not find workflow run after dispatch for {repo}@{tag}")


def fetch_json(storage_repo: str, path: str) -> dict | None:
    """
    Fetch a JSON file from a storage repo via raw HTTP (no API auth needed).
    Returns parsed JSON or None if not found.
    """
    url = f"https://raw.githubusercontent.com/{storage_repo}/main/{path}"
    try:
        req = urllib.request.Request(url)
        req.add_header("Cache-Control", "no-cache")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError:
        return None
    except Exception:
        return None


def check_manifest_for_tag(storage_repo: str, git_tag: str, dispatched_at: str) -> str | None:
    """
    Check if a tag has been parsed by looking at index.json or manifest.json.
    Returns 'success' if the tag appears with a parsed_at after dispatched_at.
    """
    # Try index.json first (new format), fall back to manifest.json (legacy)
    index = fetch_json(storage_repo, "index.json")
    if index is not None:
        for version in index.get("versions", []):
            if version.get("tag") == git_tag:
                parsed_at = version.get("parsed_at", "")
                if parsed_at >= dispatched_at:
                    return "success"
        return None

    # Fallback to legacy manifest.json
    manifest = fetch_json(storage_repo, "manifest.json")
    if manifest is None:
        return None

    for version in manifest.get("versions", []):
        if version.get("tag") == git_tag:
            parsed_at = version.get("parsed_at", "")
            if parsed_at >= dispatched_at:
                return "success"

    return None


def get_run_logs(run_id: str) -> str:
    """Get failed job logs for a run."""
    result = gh_run(
        ["run", "view", run_id, "--repo", WORKFLOW_REPO, "--log-failed"],
        check=False,
    )
    return result.stdout[-2000:] if result.stdout else result.stderr[-2000:]
