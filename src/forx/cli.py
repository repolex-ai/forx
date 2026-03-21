"""forx CLI - orchestrate repolex-forx parsing."""

import click
from rich.console import Console
from rich.table import Table

from . import db, discover, dispatch, orchestrate, spider

console = Console()


@click.group()
@click.pass_context
def cli(ctx):
    """forx - Parse every open source repo into RDF."""
    ctx.ensure_object(dict)
    ctx.obj["conn"] = db.get_db()


@cli.command()
@click.argument("repos", nargs=-1, required=True)
@click.option("--head", is_flag=True, help="Parse HEAD instead of tags (for repos without releases)")
@click.pass_context
def add(ctx, repos, head):
    """Add repos to parse. Discovers tags automatically.

    Examples:
        forx add TopQuadrant/shacl
        forx add pallets/click encode/httpx Textualize/rich
        forx add --head TopQuadrant/shacl-js
    """
    conn = ctx.obj["conn"]

    for repo in repos:
        if "/" not in repo:
            console.print(f"[red]Invalid repo format: {repo} (expected org/name)[/]")
            continue

        console.print(f"[cyan]Adding {repo}...[/]")

        repo_id = db.add_repo(conn, repo, head_only=head)

        if head:
            console.print(f"  [green]Added as HEAD-only (will parse default branch)[/]")
            continue

        # Discover all tags
        console.print(f"  Discovering tags...", end=" ")
        try:
            tags = discover.discover_repo(repo)
        except Exception as e:
            console.print(f"[red]failed: {e}[/]")
            continue

        if not tags:
            console.print("[yellow]no tags found[/]")
            continue

        console.print(f"[green]{len(tags)} tags[/]")

        # Add tags to DB
        db.add_tags(conn, repo_id, tags)

        # Show what we found
        for tag in tags:
            console.print(f"    {tag}")


@cli.command()
@click.argument("repo")
@click.pass_context
def parse(ctx, repo):
    """Force an immediate parse of a repo (even if recently parsed).

    For HEAD-only repos, resets the cooldown so it gets picked up next run.
    For tagged repos, retries any failed tags.

    Examples:
        forx parse TopQuadrant/shacl-js
        forx parse certifi/python-certifi
    """
    conn = ctx.obj["conn"]

    row = conn.execute("SELECT * FROM repos WHERE full_name = ?", (repo,)).fetchone()
    if not row:
        console.print(f"[red]Repo {repo} not tracked. Use: forx add {repo}[/]")
        return

    if row["head_only"]:
        # Reset cooldown so it gets picked up
        conn.execute(
            "UPDATE repos SET last_head_parsed = NULL WHERE full_name = ?",
            (repo,),
        )
        conn.commit()
        console.print(f"[green]Reset HEAD parse cooldown for {repo} - will parse on next run[/]")
    else:
        # Reset failed tags to pending
        rows = conn.execute(
            """UPDATE tags SET status = 'pending', error = NULL, workflow_run_id = NULL
               WHERE status = 'failed' AND repo_id = ?
               RETURNING id""",
            (row["id"],),
        ).fetchall()
        conn.commit()
        console.print(f"[green]Reset {len(rows)} failed tags to pending for {repo}[/]")


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
        """SELECT r.full_name, r.head_only,
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
    table.add_column("Type", justify="center")
    table.add_column("Total", justify="right")
    table.add_column("Pending", justify="right")
    table.add_column("Running", justify="right")
    table.add_column("Done", justify="right")
    table.add_column("Failed", justify="right")

    for row in rows:
        repo_type = "HEAD" if row["head_only"] else "tags"
        table.add_row(
            row["full_name"],
            repo_type,
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
        if repo["head_only"]:
            last = repo["last_head_parsed"] or "never"
            console.print(f"\n[bold]{repo['full_name']}[/] → {repo['storage_repo']} [dim](HEAD, last: {last})[/]")
            continue

        tags = conn.execute(
            "SELECT git_tag, status FROM tags WHERE repo_id = ? ORDER BY id",
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
            console.print(f"  [{status_style}]{marker} {tag['git_tag']}[/]")


@cli.command()
@click.option("--dry-run", is_flag=True, help="Show what would be reset without doing it")
@click.pass_context
def reparse(ctx, dry_run):
    """Invalidate tags parsed with an older parser version.

    Resets completed tags back to pending if they were parsed with
    a different version than the current one. Use after upgrading
    the repolex parser.

    Examples:
        forx reparse
        forx reparse --dry-run
    """
    conn = ctx.obj["conn"]

    if dry_run:
        rows = conn.execute(
            """SELECT r.full_name, t.git_tag, t.parser_version
               FROM tags t JOIN repos r ON t.repo_id = r.id
               WHERE t.status = 'complete'
                 AND (t.parser_version IS NULL OR t.parser_version != ?)
               ORDER BY r.full_name, t.id""",
            (db.PARSER_VERSION,),
        ).fetchall()
        if rows:
            console.print(f"[yellow]Would reset {len(rows)} tags (current version: {db.PARSER_VERSION}):[/]")
            for row in rows[:20]:
                old_ver = row["parser_version"] or "unknown"
                console.print(f"  {row['full_name']}@{row['git_tag']} (was: {old_ver})")
            if len(rows) > 20:
                console.print(f"  ... and {len(rows) - 20} more")
        else:
            console.print(f"[green]All completed tags are on current version ({db.PARSER_VERSION})[/]")
    else:
        count = db.invalidate_old_parses(conn)
        if count:
            console.print(f"[green]Reset {count} tags to pending (current version: {db.PARSER_VERSION})[/]")
        else:
            console.print(f"[green]All completed tags are on current version ({db.PARSER_VERSION})[/]")


@cli.command()
@click.pass_context
def crawl(ctx):
    """Spider all parsed repos and queue their dependencies.

    Fetches manifest.json from each storage repo, finds resolved
    dependencies, and adds new repos to the parse queue.
    """
    conn = ctx.obj["conn"]
    spider.spider_all(conn)
