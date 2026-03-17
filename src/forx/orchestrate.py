"""Main orchestration loop: keep ~20 parse jobs running at all times."""

import time

from rich.console import Console
from rich.table import Table

from . import db, dispatch

console = Console()


def fill_slots(conn, max_concurrent: int = dispatch.MAX_CONCURRENT):
    """Dispatch jobs to fill available slots."""
    # How many are currently running?
    active = db.get_dispatched_tags(conn)
    available = max_concurrent - len(active)

    if available <= 0:
        return 0

    # Get next batch of pending tags
    pending = db.get_pending_tags(conn, limit=available)

    if not pending:
        return 0

    dispatched = 0
    for tag_row in pending:
        try:
            console.print(
                f"  [cyan]Dispatching[/] {tag_row['full_name']}@{tag_row['git_tag']}...",
                end=" ",
            )
            run_id = dispatch.dispatch_workflow(
                repo=tag_row["full_name"],
                tag=tag_row["git_tag"],
                storage_repo=tag_row["storage_repo"],
            )
            db.mark_dispatched(conn, tag_row["id"], run_id)
            console.print(f"[green]run {run_id}[/]")
            dispatched += 1
        except Exception as e:
            console.print(f"[red]failed: {e}[/]")
            db.mark_failed(conn, tag_row["id"], str(e))

    return dispatched


def check_running(conn) -> tuple[int, int]:
    """Check status of all dispatched jobs. Returns (completed, failed)."""
    active = db.get_dispatched_tags(conn)
    if not active:
        return 0, 0

    run_ids = [row["workflow_run_id"] for row in active]
    run_id_to_tag = {row["workflow_run_id"]: row for row in active}

    completed_count = 0
    failed_count = 0

    results = dispatch.check_completed_runs(run_ids)
    for run_id, conclusion, detail in results:
        tag_row = run_id_to_tag[run_id]
        if conclusion == "success":
            db.mark_complete(conn, tag_row["id"])
            console.print(
                f"  [green]✓[/] {tag_row['full_name']}@{tag_row['git_tag']}"
            )
            completed_count += 1
        elif conclusion in ("failure", "error", "cancelled"):
            error_msg = detail or conclusion
            if conclusion == "failure":
                try:
                    logs = dispatch.get_run_logs(run_id)
                    if logs:
                        error_msg = logs[-500:]
                except Exception:
                    pass
            db.mark_failed(conn, tag_row["id"], error_msg)
            console.print(
                f"  [red]✗[/] {tag_row['full_name']}@{tag_row['git_tag']} ({conclusion})"
            )
            failed_count += 1

    return completed_count, failed_count


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
    Keeps slots filled and monitors until everything is done.
    """
    console.print("[bold]Starting forx orchestrator[/]")
    print_status(conn)

    # Reset any stale dispatched jobs
    reset_count = db.reset_stale_dispatched(conn)
    if reset_count:
        console.print(f"[yellow]Reset {reset_count} stale dispatched jobs[/]")

    while True:
        # Check completed runs
        completed, failed = check_running(conn)
        if completed or failed:
            console.print(f"  [dim]Batch: {completed} complete, {failed} failed[/]")

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
