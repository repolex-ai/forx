"""Main orchestration loop: keep ~20 parse jobs running at all times."""

import time
from datetime import datetime, timezone

from rich.console import Console
from rich.table import Table

from . import db, dispatch, index

console = Console()


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
    """
    Check status of dispatched jobs by fetching manifests via HTTP.
    No GitHub API calls - just raw file fetches from public repos.
    Returns (completed, failed).
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
        dispatched_at = tag_row["dispatched_at"]

        # Check manifest for this tag
        result = dispatch.check_manifest_for_tag(storage_repo, git_tag, dispatched_at)

        if result == "success":
            db.mark_complete(conn, tag_row["id"])
            console.print(
                f"  [green]✓[/] {tag_row['full_name']}@{git_tag}"
            )
            completed_count += 1
        else:
            # Check if it's been too long (stale)
            if dispatched_at:
                dispatched_time = datetime.fromisoformat(dispatched_at)
                elapsed = (now - dispatched_time).total_seconds()
                if elapsed > dispatch.STALE_TIMEOUT:
                    db.mark_failed(conn, tag_row["id"], f"No manifest update after {int(elapsed)}s")
                    console.print(
                        f"  [red]✗[/] {tag_row['full_name']}@{git_tag} (timeout)"
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
