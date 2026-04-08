"""Dispatch and monitor GitHub Actions workflow runs."""

import json
import subprocess
import time
import urllib.request
import urllib.error


from .db import PARSER_VERSION

WORKFLOW_FILE = "parse.yml"
WORKFLOW_REPO = "repolex-ai/forx"
MAX_CONCURRENT = 20
POLL_INTERVAL = 120  # seconds between manifest checks (no API rate limit concern)
STALE_TIMEOUT = 25200  # 7 hours per phase — Option B: each iteration gets a fresh 6hr Actions wall

# Option B safety cap: max iterations per tag before forx gives up.
# Each iteration is one phase dispatch (parse, ast, enrich, combine).
# Big enrich phases (e.g. jena) might take ~15 iterations. 50 is a generous safety net.
MAX_ITERATIONS_PER_TAG = 50

# Valid next_action values from .next-action.json (parser-side contract)
VALID_PHASES = {"parse", "ast", "enrich", "combine"}
TERMINAL_ACTION = "done"


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


def dispatch_workflow(
    repo: str,
    tag: str,
    storage_repo: str,
    phase: str = "parse",
    parser_ref: str | None = None,
) -> str:
    """
    Dispatch a parse workflow run for a specific phase.

    Phases: 'parse' | 'ast' | 'enrich' | 'combine'.
    Returns the workflow run ID.
    """
    if phase not in VALID_PHASES:
        raise ValueError(f"Invalid phase {phase!r}. Must be one of {sorted(VALID_PHASES)}")

    ref = parser_ref or PARSER_VERSION

    gh_run([
        "workflow", "run", WORKFLOW_FILE,
        "--repo", WORKFLOW_REPO,
        "-f", f"repo={repo}",
        "-f", f"tag={tag}",
        "-f", f"storage_repo={storage_repo}",
        "-f", f"parser_ref={ref}",
        "-f", f"phase={phase}",
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

    raise RuntimeError(f"Could not find workflow run after dispatch for {repo}@{tag} (phase={phase})")


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
    Check if a tag has been parsed by looking at repo-manifest.jsonld or manifest.json.
    Returns 'success' if the tag appears as parsed after dispatched_at.
    """
    # Try repo-manifest.jsonld first (new JSON-LD format)
    repo_manifest = fetch_json(storage_repo, "repo-manifest.jsonld")
    if repo_manifest is not None:
        for commit in repo_manifest.get("repolex:trackedCommit", []):
            tag_name = commit.get("git:tagName", "")
            status = commit.get("repolex:parseStatus", "")
            parsed_at = commit.get("repolex:parsedAt", "")
            if tag_name == git_tag and status == "parsed" and parsed_at >= dispatched_at:
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


# ============================================================================
# Option B: phase-driven dispatch helpers
# ============================================================================


def read_next_action(storage_repo: str) -> dict | None:
    """
    Read aggregate/.next-action.json from a storage repo.

    Returns parsed JSON dict with at least:
      - next_action: 'parse' | 'ast' | 'enrich' | 'combine' | 'done'
      - phase_completed: descriptor of what just finished
      - ts: ISO timestamp

    Returns None if the file doesn't exist (parser bug or fresh tag).
    """
    return fetch_json(storage_repo, "aggregate/.next-action.json")


def check_workflow_run_status(run_id: str) -> tuple[str, str] | None:
    """
    Check status of a workflow run via gh API.

    Returns (status, conclusion) tuple where:
      - status: 'queued' | 'in_progress' | 'completed' | ...
      - conclusion: 'success' | 'failure' | 'cancelled' | '' (empty if not completed)

    Returns None if the run can't be found.
    """
    result = gh_run(
        [
            "api",
            f"repos/{WORKFLOW_REPO}/actions/runs/{run_id}",
            "--jq",
            "{status: .status, conclusion: .conclusion}",
        ],
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        data = json.loads(result.stdout)
        return data.get("status", ""), data.get("conclusion", "") or ""
    except json.JSONDecodeError:
        return None
