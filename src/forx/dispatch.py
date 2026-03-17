"""Dispatch and monitor GitHub Actions workflow runs."""

import json
import subprocess
import time


WORKFLOW_FILE = "parse.yml"
WORKFLOW_REPO = "repolex-ai/forx"
MAX_CONCURRENT = 20
POLL_INTERVAL = 15  # seconds


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
    # Dispatch the workflow
    gh_run([
        "workflow", "run", WORKFLOW_FILE,
        "--repo", WORKFLOW_REPO,
        "-f", f"repo={repo}",
        "-f", f"tag={tag}",
        "-f", f"storage_repo={storage_repo}",
    ])

    # gh workflow run doesn't return the run ID, so we need to find it
    # Wait a moment for GitHub to register the run
    time.sleep(2)

    # Get the most recent run for this workflow
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


def get_run_status(run_id: str) -> dict:
    """Get the status of a workflow run."""
    result = gh_run([
        "run", "view", run_id,
        "--repo", WORKFLOW_REPO,
        "--json", "status,conclusion,databaseId",
    ])
    return json.loads(result.stdout)


def get_active_runs() -> list[dict]:
    """Get all currently active (queued/in_progress) runs."""
    result = gh_run([
        "run", "list",
        "--repo", WORKFLOW_REPO,
        "--workflow", WORKFLOW_FILE,
        "--status", "in_progress",
        "--json", "databaseId,status,createdAt",
        "--limit", "50",
    ])
    in_progress = json.loads(result.stdout)

    result = gh_run([
        "run", "list",
        "--repo", WORKFLOW_REPO,
        "--workflow", WORKFLOW_FILE,
        "--status", "queued",
        "--json", "databaseId,status,createdAt",
        "--limit", "50",
    ])
    queued = json.loads(result.stdout)

    return in_progress + queued


def check_completed_runs(run_ids: list[str]) -> list[tuple[str, str, str]]:
    """
    Check which runs from the given IDs have completed.
    Returns [(run_id, conclusion, conclusion_detail), ...] for completed runs.
    conclusion is 'success', 'failure', 'cancelled', etc.
    """
    completed = []
    for run_id in run_ids:
        try:
            status = get_run_status(run_id)
            if status["status"] == "completed":
                conclusion = status.get("conclusion", "unknown")
                completed.append((run_id, conclusion, ""))
        except Exception as e:
            # If we can't check, assume still running
            completed.append((run_id, "error", str(e)))
    return completed


def get_run_logs(run_id: str) -> str:
    """Get failed job logs for a run."""
    result = gh_run(
        ["run", "view", run_id, "--repo", WORKFLOW_REPO, "--log-failed"],
        check=False,
    )
    return result.stdout[-2000:] if result.stdout else result.stderr[-2000:]
