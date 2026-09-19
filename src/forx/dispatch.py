"""Dispatch and monitor GitHub Actions workflow runs."""

import base64
import json
import subprocess
import time
import urllib.error
import urllib.request


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
    Fetch a JSON file from a storage repo.
    Tries `gh api` first to bypass Fastly CDN caching on raw.githubusercontent.com,
    falling back to raw HTTP if gh CLI is unavailable or fails.
    Returns parsed JSON or None if not found.
    """
    try:
        result = gh_run(
            ["api", f"repos/{storage_repo}/contents/{path}", "--jq", ".content"],
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            content = base64.b64decode(result.stdout.strip()).decode()
            return json.loads(content)
    except Exception:
        pass

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


def check_manifest_for_tag(
    storage_repo: str,
    git_tag: str,
    dispatched_at: str = "",
    commit_sha: str | None = None,
) -> str | None:
    """
    Check if a tag has been parsed by looking at repo-manifest.jsonld,
    individual commit manifest manifests/commit-manifest-{sha}.jsonld,
    or legacy manifest.json.
    Returns 'success' if the tag or commit appears as parsed.

    Supports commit SHA aliasing (forx#7): multiple tags frequently point to the
    exact same commit SHA (e.g. fireworks v2.0.2–v2.0.9 or release candidate tags).
    If the underlying commit is recorded as parsed, any tag pointing to that commit
    is verified.
    """
    del dispatched_at  # unused under Option B — see docstring
    # Try repo-manifest.jsonld first (new JSON-LD format)
    repo_manifest = fetch_json(storage_repo, "repo-manifest.jsonld")
    if repo_manifest is not None:
        commits = repo_manifest.get("repolex:trackedCommit", [])
        if isinstance(commits, dict):
            commits = [commits]
        for commit in commits:
            tag_name = commit.get("git:tagName", "")
            hexsha = commit.get("git:hexsha", "")
            status = commit.get("repolex:parseStatus", "")
            if status == "parsed":
                if tag_name == git_tag or (commit_sha and hexsha == commit_sha):
                    return "success"
        # If commit_sha is provided, also check if individual commit manifest exists and is parsed
        if commit_sha:
            cm = fetch_json(storage_repo, f"manifests/commit-manifest-{commit_sha}.jsonld")
            if cm is not None and cm.get("repolex:parseStatus") == "parsed":
                return "success"
        return None

    # Fallback to checking individual commit manifest directly
    if commit_sha:
        cm = fetch_json(storage_repo, f"manifests/commit-manifest-{commit_sha}.jsonld")
        if cm is not None and cm.get("repolex:parseStatus") == "parsed":
            return "success"

    # Fallback to legacy manifest.json
    manifest = fetch_json(storage_repo, "manifest.json")
    if manifest is None:
        return None

    for version in manifest.get("versions", []):
        tag_match = version.get("tag") == git_tag
        sha_match = bool(commit_sha and version.get("sha") == commit_sha)
        if tag_match or sha_match:
            return "success"

    return None


def check_ast_chunks_exist(storage_repo: str, commit_sha: str) -> bool:
    """
    Check if AST aggregate chunks exist for a commit in the storage repo.
    Prevents dispatching 'enrich' when AST chunks are missing (forx#7 item 2).
    """
    if not commit_sha:
        return False

    # Check directory contents via gh api
    try:
        result = gh_run(
            ["api", f"repos/{storage_repo}/contents/aggregate/ast/{commit_sha}"],
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            items = json.loads(result.stdout)
            if isinstance(items, list) and any(item.get("name", "").endswith(".nq.gz") for item in items):
                return True
    except Exception:
        pass

    # Direct raw probe fallback
    url = f"https://raw.githubusercontent.com/{storage_repo}/main/aggregate/ast/{commit_sha}/chunk-001.nq.gz"
    try:
        req = urllib.request.Request(url, method="HEAD")
        req.add_header("Cache-Control", "no-cache")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


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
    Uses fetch_json (which tries gh api first to avoid CDN caching).

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
