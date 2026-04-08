"""Main orchestration loop: keep ~20 parse jobs running at all times.

Option B: phase-driven dispatch.
Each tag goes through phases: parse → ast → enrich (loop) → combine → done.
The parser writes aggregate/.next-action.json after each phase, and forx reads
it to decide what to dispatch next. Each phase gets its own fresh 6hr Actions
wall (vs the old "all phases in one runner" model).

State machine per tag:
  - status='pending', next_action=NULL → fresh tag, dispatch phase=parse
  - status='pending', next_action='ast'/'enrich'/'combine'/'parse' → re-dispatch that phase
  - status='dispatched' → wait for workflow run to complete
  - status='complete' → all phases done, manifest verified
  - status='failed' → unrecoverable error or hit iteration cap
"""

import time
from datetime import datetime, timezone

from rich.console import Console
from rich.table import Table

from . import db, dispatch, index

console = Console()


def _next_phase_for_tag(tag_row) -> str:
    """
    Determine which phase to dispatch for a pending tag.

    Fresh tags (no next_action recorded) start with 'parse'.
    Tags mid-pipeline use the next_action recorded by the parser.
    """
    next_action = tag_row["next_action"] if "next_action" in tag_row.keys() else None
    if next_action and next_action in dispatch.VALID_PHASES:
        return next_action
    # Fresh tag or invalid stored action — start from the beginning
    return "parse"


def fill_slots(conn, max_concurrent: int = dispatch.MAX_CONCURRENT):
    """Dispatch jobs to fill available slots."""
    active = db.get_dispatched_tags(conn)
    available = max_concurrent - len(active)

    if available <= 0:
        return 0

    pending = db.get_pending_tags(conn, limit=available)

    if not pending:
        return 0

    dispatched = 0
    for tag_row in pending:
        # Safety cap on iterations per tag
        iter_count = tag_row["iteration_count"] if "iteration_count" in tag_row.keys() else 0
        if iter_count >= dispatch.MAX_ITERATIONS_PER_TAG:
            console.print(
                f"  [red]✗[/] {tag_row['full_name']}@{tag_row['git_tag']} hit iteration cap ({iter_count})"
            )
            db.mark_failed(conn, tag_row["id"], f"Hit iteration cap: {iter_count}")
            continue

        phase = _next_phase_for_tag(tag_row)

        try:
            console.print(
                f"  [cyan]Dispatching[/] {tag_row['full_name']}@{tag_row['git_tag']} "
                f"[dim](phase={phase}, iter={iter_count + 1})[/]...",
                end=" ",
            )
            run_id = dispatch.dispatch_workflow(
                repo=tag_row["full_name"],
                tag=tag_row["git_tag"],
                storage_repo=tag_row["storage_repo"],
                phase=phase,
            )
            db.mark_dispatched(conn, tag_row["id"], run_id, phase=phase)
            console.print(f"[green]run {run_id}[/]")
            dispatched += 1
        except Exception as e:
            console.print(f"[red]failed: {e}[/]")
            db.mark_failed(conn, tag_row["id"], str(e))

    return dispatched


def check_running(conn) -> tuple[int, int]:
    """
    Check status of dispatched jobs.

    Option B flow per dispatched tag:
      1. Check workflow run conclusion via gh API
      2. If still running → skip
      3. If completed successfully → read aggregate/.next-action.json
         - next_action='done' → verify manifest, mark complete
         - next_action in {parse,ast,enrich,combine} → reset to pending,
           orchestrator will re-dispatch with the new phase next round
         - missing/invalid → parser bug, mark failed
      4. If completed with failure/cancelled → mark failed

    Returns (completed_count, failed_count) where 'completed' means
    fully done (next_action=done verified), not just one phase.
    """
    active = db.get_dispatched_tags(conn)
    if not active:
        return 0, 0

    completed_count = 0
    failed_count = 0
    now = datetime.now(timezone.utc)

    for tag_row in active:
        storage_repo = tag_row["storage_repo"]
        git_tag = tag_row["git_tag"]
        full_name = tag_row["full_name"]
        run_id = tag_row["workflow_run_id"]
        current_phase = tag_row["current_phase"] if "current_phase" in tag_row.keys() else None
        dispatched_at = tag_row["dispatched_at"]

        # 1. Check workflow run status
        if not run_id:
            # No run id (shouldn't happen) — fall back to stale check
            if _is_stale(dispatched_at, now):
                db.mark_failed(conn, tag_row["id"], "No run_id and stale")
                failed_count += 1
            continue

        run_status = dispatch.check_workflow_run_status(run_id)
        if run_status is None:
            # Couldn't reach gh API — try again next poll
            continue

        status, conclusion = run_status

        if status != "completed":
            # Still running. Check stale.
            if _is_stale(dispatched_at, now):
                db.mark_failed(
                    conn,
                    tag_row["id"],
                    f"Stale: phase={current_phase} run={run_id} status={status}",
                )
                console.print(
                    f"  [red]✗[/] {full_name}@{git_tag} [dim](phase={current_phase}, stale)[/]"
                )
                failed_count += 1
            continue

        # 2. Run completed — handle outcome
        if conclusion not in {"success", "skipped"}:
            db.mark_failed(
                conn,
                tag_row["id"],
                f"Workflow {run_id} (phase={current_phase}) ended: {conclusion}",
            )
            console.print(
                f"  [red]✗[/] {full_name}@{git_tag} [dim](phase={current_phase}, {conclusion})[/]"
            )
            failed_count += 1
            continue

        # 3. Workflow succeeded — read .next-action.json
        next_action_data = dispatch.read_next_action(storage_repo)
        if next_action_data is None:
            db.mark_failed(
                conn,
                tag_row["id"],
                f"Workflow {run_id} (phase={current_phase}) succeeded but .next-action.json missing",
            )
            console.print(
                f"  [red]✗[/] {full_name}@{git_tag} [dim](missing .next-action.json after phase={current_phase})[/]"
            )
            failed_count += 1
            continue

        next_action = next_action_data.get("next_action", "")
        phase_completed = next_action_data.get("phase_completed", "")

        if next_action == dispatch.TERMINAL_ACTION:
            # Verify with manifest as defense in depth
            manifest_result = dispatch.check_manifest_for_tag(storage_repo, git_tag, dispatched_at)
            if manifest_result == "success":
                db.mark_complete(conn, tag_row["id"])
                console.print(
                    f"  [green]✓[/] {full_name}@{git_tag} "
                    f"[dim](done after phase={phase_completed})[/]"
                )
                completed_count += 1
            else:
                # next_action says done but manifest doesn't agree — flag loudly
                db.mark_failed(
                    conn,
                    tag_row["id"],
                    f"next_action=done but manifest not parsed (phase={phase_completed})",
                )
                console.print(
                    f"  [red]✗[/] {full_name}@{git_tag} "
                    f"[dim](next_action=done but manifest mismatch)[/]"
                )
                failed_count += 1

        elif next_action in dispatch.VALID_PHASES:
            # Mid-pipeline — record the next action and reset to pending
            # so fill_slots will dispatch the next phase next round.
            db.update_next_action(conn, tag_row["id"], next_action)
            db.reset_to_pending(conn, tag_row["id"])
            console.print(
                f"  [yellow]→[/] {full_name}@{git_tag} "
                f"[dim](phase {phase_completed} done, next: {next_action})[/]"
            )

        else:
            db.mark_failed(
                conn,
                tag_row["id"],
                f"Invalid next_action {next_action!r} from .next-action.json (phase={phase_completed})",
            )
            console.print(
                f"  [red]✗[/] {full_name}@{git_tag} "
                f"[dim](invalid next_action: {next_action!r})[/]"
            )
            failed_count += 1

    return completed_count, failed_count


def _is_stale(dispatched_at: str | None, now: datetime) -> bool:
    """Check if a dispatched job has been running longer than STALE_TIMEOUT."""
    if not dispatched_at:
        return False
    try:
        dispatched_time = datetime.fromisoformat(dispatched_at)
    except (ValueError, TypeError):
        return False
    elapsed = (now - dispatched_time).total_seconds()
    return elapsed > dispatch.STALE_TIMEOUT


def print_status(conn):
    """Print current status summary."""
    stats = db.get_stats(conn)

    table = Table(title="forx status", show_header=False, border_style="dim")
    table.add_column(style="bold")
    table.add_column(justify="right")

    table.add_row("Repos", str(stats["total_repos"]))
    table.add_row("Total tags", str(stats["total_tags"]))
    table.add_row("[dim]Pending[/]", str(stats["pending"]))
    table.add_row("[cyan]Dispatched[/]", str(stats["dispatched"]))
    table.add_row("[green]Complete[/]", str(stats["complete"]))
    table.add_row("[red]Failed[/]", str(stats["failed"]))

    console.print(table)


def run_loop(conn, max_concurrent: int = dispatch.MAX_CONCURRENT, poll_interval: int = dispatch.POLL_INTERVAL):
    """
    Main orchestration loop.
    Keeps slots filled and monitors via manifest checks (no API polling).
    """
    console.print("[bold]Starting forx orchestrator[/]")
    print_status(conn)

    # Reset any stale dispatched jobs
    reset_count = db.reset_stale_dispatched(conn)
    if reset_count:
        console.print(f"[yellow]Reset {reset_count} stale dispatched jobs[/]")

    while True:
        # Check completed runs via manifest
        completed, failed = check_running(conn)
        if completed or failed:
            console.print(f"  [dim]Batch: {completed} complete, {failed} failed[/]")

            # Sync index and update profile when parses complete
            if completed > 0:
                try:
                    index_path = index.get_index_path()
                    updated = index.sync_all(conn, index_path)
                    if updated:
                        index.push_index(index_path, message=f"Sync {updated} repos")
                        index.update_profile_readme(index_path)
                except Exception as e:
                    console.print(f"  [dim yellow]Index sync: {e}[/]")

        # Fill available slots
        dispatched = fill_slots(conn, max_concurrent)
        if dispatched:
            console.print(f"  [dim]Dispatched {dispatched} new jobs[/]")

        # Check if we're done
        stats = db.get_stats(conn)
        if stats["pending"] == 0 and stats["dispatched"] == 0:
            console.print("\n[bold green]All done![/]")
            print_status(conn)
            break

        # Status line
        console.print(
            f"  [dim]{stats['dispatched']} running, {stats['pending']} pending, "
            f"{stats['complete']} complete[/]",
        )

        time.sleep(poll_interval)
