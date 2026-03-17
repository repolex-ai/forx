"""forx CLI - orchestrate repolex-forx parsing."""

import click
from rich.console import Console
from rich.table import Table

from . import db, discover, dispatch, orchestrate

console = Console()


@click.group()
@click.pass_context
def cli(ctx):
    """forx - Parse every open source repo into RDF."""
    ctx.ensure_object(dict)
    ctx.obj["conn"] = db.get_db()


@cli.command()
@click.argument("repos", nargs=-1, required=True)
@click.pass_context
def add(ctx, repos):
    """Add repos to parse. Discovers version tags automatically.

    Examples:
        forx add TopQuadrant/shacl
        forx add pallets/click encode/httpx Textualize/rich
    """
    conn = ctx.obj["conn"]

    for repo in repos:
        if "/" not in repo:
            console.print(f"[red]Invalid repo format: {repo} (expected org/name)[/]")
            continue

        console.print(f"[cyan]Adding {repo}...[/]")

        # Add repo to DB
        repo_id = db.add_repo(conn, repo)

        # Discover tags
        console.print(f"  Discovering tags...", end=" ")
        try:
            tags = discover.discover_repo(repo)
        except Exception as e:
            console.print(f"[red]failed: {e}[/]")
            continue

        if not tags:
            console.print("[yellow]no semver tags found[/]")
            continue

        console.print(f"[green]{len(tags)} versions[/]")

        # Add tags to DB
        db.add_tags(conn, repo_id, tags)

        # Show what we found
        for version, git_tag in tags:
            suffix = f" ({git_tag})" if git_tag != version else ""
            console.print(f"    {version}{suffix}")


@cli.command()
@click.option("--max-concurrent", "-j", default=dispatch.MAX_CONCURRENT, help="Max parallel jobs")
@click.option("--poll-interval", "-p", default=dispatch.POLL_INTERVAL, help="Seconds between polls")
@click.pass_context
def run(ctx, max_concurrent, poll_interval):
    """Start the orchestrator. Dispatches and monitors parse jobs."""
    conn = ctx.obj["conn"]
    orchestrate.run_loop(conn, max_concurrent, poll_interval)


@cli.command()
@click.pass_context
def status(ctx):
    """Show current parsing status."""
    conn = ctx.obj["conn"]
    orchestrate.print_status(conn)

    # Show per-repo breakdown
    rows = conn.execute(
        """SELECT r.full_name,
                  COUNT(t.id) as total,
                  SUM(CASE WHEN t.status = 'pending' THEN 1 ELSE 0 END) as pending,
                  SUM(CASE WHEN t.status = 'dispatched' THEN 1 ELSE 0 END) as dispatched,
                  SUM(CASE WHEN t.status = 'complete' THEN 1 ELSE 0 END) as complete,
                  SUM(CASE WHEN t.status = 'failed' THEN 1 ELSE 0 END) as failed
           FROM repos r
           LEFT JOIN tags t ON t.repo_id = r.id
           GROUP BY r.id
           ORDER BY r.full_name"""
    ).fetchall()

    if not rows:
        console.print("[dim]No repos added yet. Use: forx add org/repo[/]")
        return

    table = Table(title="Repos")
    table.add_column("Repo")
    table.add_column("Total", justify="right")
    table.add_column("Pending", justify="right")
    table.add_column("Running", justify="right")
    table.add_column("Done", justify="right")
    table.add_column("Failed", justify="right")

    for row in rows:
        table.add_row(
            row["full_name"],
            str(row["total"]),
            str(row["pending"]),
            str(row["dispatched"]),
            str(row["complete"]),
            str(row["failed"]),
        )

    console.print(table)


@cli.command()
@click.argument("repo", required=False)
@click.option("--all", "reset_all", is_flag=True, help="Reset all failed tags")
@click.pass_context
def retry(ctx, repo, reset_all):
    """Reset failed tags back to pending for retry.

    Examples:
        forx retry TopQuadrant/shacl
        forx retry --all
    """
    conn = ctx.obj["conn"]

    if reset_all:
        rows = conn.execute(
            "UPDATE tags SET status = 'pending', error = NULL, workflow_run_id = NULL WHERE status = 'failed' RETURNING id"
        ).fetchall()
        conn.commit()
        console.print(f"[green]Reset {len(rows)} failed tags to pending[/]")
    elif repo:
        rows = conn.execute(
            """UPDATE tags SET status = 'pending', error = NULL, workflow_run_id = NULL
               WHERE status = 'failed' AND repo_id = (SELECT id FROM repos WHERE full_name = ?)
               RETURNING id""",
            (repo,),
        ).fetchall()
        conn.commit()
        console.print(f"[green]Reset {len(rows)} failed tags for {repo}[/]")
    else:
        console.print("[red]Specify a repo or use --all[/]")


@cli.command()
@click.pass_context
def list_repos(ctx):
    """List all tracked repos and their tags."""
    conn = ctx.obj["conn"]

    repos = conn.execute("SELECT * FROM repos ORDER BY full_name").fetchall()

    if not repos:
        console.print("[dim]No repos. Use: forx add org/repo[/]")
        return

    for repo in repos:
        tags = conn.execute(
            "SELECT version, git_tag, status FROM tags WHERE repo_id = ? ORDER BY version",
            (repo["id"],),
        ).fetchall()

        console.print(f"\n[bold]{repo['full_name']}[/] → {repo['storage_repo']}")
        for tag in tags:
            status_style = {
                "pending": "dim",
                "dispatched": "cyan",
                "complete": "green",
                "failed": "red",
            }.get(tag["status"], "")
            marker = {
                "pending": "○",
                "dispatched": "◐",
                "complete": "●",
                "failed": "✗",
            }.get(tag["status"], "?")
            console.print(f"  [{status_style}]{marker} {tag['version']}[/]")
