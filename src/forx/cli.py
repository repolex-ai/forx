"""forx CLI - orchestrate repolex-forx parsing."""

import json
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from . import db, discover, dispatch, ecosystem, index, orchestrate, spider

console = Console()


@click.group()
@click.option("--db", "db_path", default=None, help="Path to sqlite database")
@click.pass_context
def cli(ctx, db_path):
    """forx - Parse every open source repo into RDF."""
    ctx.ensure_object(dict)
    conn = db.get_db(db_path)
    ctx.call_on_close(conn.close)
    ctx.obj["conn"] = conn


@cli.command()
@click.argument("repos", nargs=-1, required=True)
@click.option("--head", is_flag=True, help="Parse HEAD instead of tags (for repos without releases)")
@click.option("--priority", "-p", "-P", default=0, show_default=True, type=int, help="Priority level for repo (higher = parsed sooner)")
@click.pass_context
def add(ctx, repos, head, priority):
    """Add repos to parse. Discovers tags automatically.

    Examples:
        forx add TopQuadrant/shacl
        forx add pallets/click encode/httpx Textualize/rich
        forx add --head TopQuadrant/shacl-js
        forx add tim-osterhus/millrace --priority 1200
    """
    conn = ctx.obj["conn"]

    for repo in repos:
        if "/" not in repo:
            console.print(f"[red]Invalid repo format: {repo} (expected org/name)[/]")
            continue

        console.print(f"[cyan]Adding {repo}...[/]")

        inferred_lang = ecosystem.infer_language(repo)
        repo_id = db.add_repo(conn, repo, head_only=head, priority=priority, language=inferred_lang)
        if priority > 0:
            conn.execute("UPDATE repos SET priority = ? WHERE id = ? AND priority < ?", (priority, repo_id, priority))
            conn.commit()

        if head:
            default_branch = discover.get_default_branch(repo)
            db.add_tags(conn, repo_id, [default_branch])
            console.print(f"  [green]Added as HEAD-only ({default_branch}) priority={priority}[/]")
            continue

        # Discover all tags
        console.print("  Discovering tags...", end=" ")
        try:
            tags = discover.discover_repo(repo)
        except Exception as e:
            console.print(f"[red]failed: {e}[/]")
            continue

        if not tags:
            default_branch = discover.get_default_branch(repo)
            conn.execute("UPDATE repos SET head_only = 1 WHERE id = ?", (repo_id,))
            db.add_tags(conn, repo_id, [default_branch])
            conn.commit()
            console.print(f"[yellow]no tags found, added HEAD ({default_branch}) priority={priority}[/]")
            continue

        console.print(f"[green]{len(tags)} tags (priority={priority})[/]")

        # Add tags to DB
        db.add_tags(conn, repo_id, tags)

        # Show what we found
        for tag in tags:
            console.print(f"    {tag}")


@cli.command()
@click.argument("org")
@click.option("--priority", "-p", "-P", default=500, show_default=True, type=int, help="Priority level for discovered repos (default 500)")
@click.option("--include-forks", is_flag=True, help="Include forked repos")
@click.option("--min-stars", default=0, help="Only add repos with at least this many stars")
@click.option("--update-priority", is_flag=True, help="Update priority for already-tracked repos")
@click.pass_context
def add_org(ctx, org, priority, include_forks, min_stars, update_priority):
    """Add all repos from a GitHub org or user.

    Discovers all non-archived repos and their tags. If a repo has no tags,
    falls back to its default branch HEAD.

    Examples:
        forx add-org pallets
        forx add-org asimov-platform --priority 800
        forx add-org NousResearch --min-stars 10
        forx add-org someuser --include-forks
    """
    conn = ctx.obj["conn"]

    console.print(f"[bold]Listing repos for {org}...[/]")
    try:
        repos = discover.list_org_repos(org, include_forks=include_forks)
    except Exception as e:
        console.print(f"[red]Failed: {e}[/]")
        return

    if min_stars:
        repos = [r for r in repos if r.get("stars", 0) >= min_stars]

    console.print(f"Found {len(repos)} repos\n")

    added = 0
    for repo_info in repos:
        full_name = repo_info["full_name"]
        lang = repo_info.get("language") or "?"
        stars = repo_info.get("stars", 0)

        # Check if already tracked
        existing = conn.execute(
            "SELECT id, priority, head_only FROM repos WHERE full_name = ?", (full_name,)
        ).fetchone()
        if existing:
            # Check if it was stranded without any tags
            tag_count = conn.execute(
                "SELECT COUNT(*) as count FROM tags WHERE repo_id = ?", (existing["id"],)
            ).fetchone()["count"]
            if tag_count == 0:
                default_branch = repo_info.get("default_branch") or discover.get_default_branch(full_name)
                conn.execute(
                    "UPDATE repos SET head_only = 1, priority = ? WHERE id = ?",
                    (priority, existing["id"]),
                )
                db.add_tags(conn, existing["id"], [default_branch])
                conn.commit()
                console.print(
                    f"  [cyan]{full_name}[/] ({lang}, {stars}★)... [green]rescued tagless HEAD ({default_branch}) priority={priority}[/]"
                )
                added += 1
                continue

            if update_priority or existing["priority"] != priority:
                conn.execute(
                    "UPDATE repos SET priority = ? WHERE id = ?",
                    (priority, existing["id"]),
                )
                conn.commit()
                console.print(
                    f"  [dim]{full_name} ({lang}, {stars}★) - already tracked, updated priority {existing['priority']} -> {priority}[/]"
                )
            else:
                console.print(f"  [dim]{full_name} ({lang}, {stars}★) - already tracked[/]")

            # Backfill language on existing repo if not set
            if lang != "?":
                conn.execute(
                    "UPDATE repos SET language = ? WHERE id = ? AND language IS NULL",
                    (lang, existing["id"]),
                )
                conn.commit()
            continue

        console.print(f"  [cyan]{full_name}[/] ({lang}, {stars}★)...", end=" ")

        try:
            tags = discover.discover_repo(full_name)
        except Exception as e:
            console.print(f"[red]error: {e}[/]")
            continue

        lang_to_save = lang if lang != "?" else None
        if tags:
            repo_id = db.add_repo(conn, full_name, priority=priority, language=lang_to_save)
            db.add_tags(conn, repo_id, tags)
            console.print(f"[green]{len(tags)} tags (priority={priority})[/]")
            added += 1
        else:
            default_branch = repo_info.get("default_branch") or discover.get_default_branch(full_name)
            repo_id = db.add_repo(conn, full_name, head_only=True, priority=priority, language=lang_to_save)
            db.add_tags(conn, repo_id, [default_branch])
            console.print(f"[green]added HEAD ({default_branch}) priority={priority}[/]")
            added += 1

    console.print(f"\n[bold green]Added {added} repos from {org}[/]")


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
@click.option("--spider-every", default=30, show_default=True, help="Run dep spider every N poll cycles (0 to disable)")
@click.pass_context
def run(ctx, max_concurrent, poll_interval, spider_every):
    """Start the orchestrator. Dispatches and monitors parse jobs."""
    conn = ctx.obj["conn"]
    orchestrate.run_loop(conn, max_concurrent, poll_interval, spider_every)


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


@cli.command(name="index")
@click.option("--push/--no-push", default=True, help="Push changes to forx-index repo")
@click.pass_context
def index_cmd(ctx, push):
    """Sync parsed repo manifests into the forx-index repo.

    Pulls repo-manifest.jsonld and commit manifests from each
    storage repo and writes them to the local forx-index clone.

    Examples:
        forx index
        forx index --no-push
    """
    conn = ctx.obj["conn"]

    console.print("[bold]Syncing forx-index...[/]")
    try:
        index_path = index.get_index_path()
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/]")
        return

    updated = index.sync_all(conn, index_path)
    console.print(f"\n[bold]{updated} repos updated[/]")

    if push and updated > 0:
        index.push_index(index_path, message=f"Sync {updated} repos")

    # Update the org profile README with latest parsed repos
    if push:
        index.update_profile_readme(index_path)


@cli.command()
@click.argument("repos", nargs=-1, required=True)
@click.option("--level", default=10, show_default=True, help="Priority level (higher = sooner)")
@click.pass_context
def prioritize(ctx, repos, level):
    """Bump repos to the front of the parse queue.

    Example: forx prioritize apache/jena Jelly-RDF/jelly-jvm
    """
    conn = ctx.obj["conn"]
    for repo in repos:
        row = conn.execute("SELECT id FROM repos WHERE full_name = ?", (repo,)).fetchone()
        if not row:
            click.echo(f"  [not found] {repo}")
            continue
        conn.execute("UPDATE repos SET priority = ? WHERE id = ?", (level, row["id"]))
        click.echo(f"  [priority={level}] {repo}")
    conn.commit()


@cli.command()
@click.pass_context
def crawl(ctx):
    """Spider all parsed repos and queue their dependencies.

    Fetches manifest.json from each storage repo, finds resolved
    dependencies, and adds new repos to the parse queue.
    """
    conn = ctx.obj["conn"]
    spider.spider_all(conn)


@cli.command()
@click.argument("repo", required=False)
@click.option("--all-pending", is_flag=True, help="Re-discover all repos with any pending tags")
@click.option("--zero-only", is_flag=True, help="Only re-discover repos with zero complete tags")
@click.pass_context
def rediscover(ctx, repo, all_pending, zero_only):
    """Re-run tag discovery for a repo to backfill discovery_order.

    Drops all pending (not complete, not dispatched, not failed) tag rows
    for the repo and re-inserts them via discover.get_git_tags. Complete/failed
    tags are preserved. Newly inserted rows get proper discovery_order values
    so get_pending_tags can pick the latest tag correctly.

    Examples:
        forx rediscover psf/black
        forx rediscover --all-pending
        forx rediscover --zero-only
    """
    conn = ctx.obj["conn"]

    if repo:
        targets = [repo]
    elif all_pending:
        targets = [
            r["full_name"] for r in conn.execute(
                """SELECT DISTINCT r.full_name FROM repos r
                   JOIN tags t ON t.repo_id = r.id
                   WHERE t.status = 'pending'
                   ORDER BY r.full_name"""
            ).fetchall()
        ]
    elif zero_only:
        targets = [
            r["full_name"] for r in conn.execute(
                """SELECT r.full_name FROM repos r
                   WHERE (SELECT COUNT(*) FROM tags WHERE repo_id = r.id AND status = 'complete') = 0
                     AND (SELECT COUNT(*) FROM tags WHERE repo_id = r.id AND status = 'pending') > 0
                   ORDER BY r.full_name"""
            ).fetchall()
        ]
    else:
        console.print("[red]Specify a repo, --all-pending, or --zero-only[/]")
        return

    console.print(f"[bold]Re-discovering {len(targets)} repo(s)...[/]")
    total_added = 0
    for i, full_name in enumerate(targets, 1):
        row = conn.execute("SELECT id FROM repos WHERE full_name = ?", (full_name,)).fetchone()
        if not row:
            console.print(f"  [yellow]{i}/{len(targets)} {full_name} not tracked[/]")
            continue
        repo_id = row["id"]

        try:
            tags = discover.discover_repo(full_name)
        except Exception as e:
            console.print(f"  [red]{i}/{len(targets)} {full_name} failed: {e}[/]")
            continue

        if not tags:
            console.print(f"  [dim]{i}/{len(targets)} {full_name} no tags[/]")
            continue

        # Drop only pending tag rows; preserve complete/failed/dispatched
        conn.execute(
            "DELETE FROM tags WHERE repo_id = ? AND status = 'pending'",
            (repo_id,),
        )
        conn.commit()

        # Re-insert — add_tags populates discovery_order from list index
        db.add_tags(conn, repo_id, tags)

        pending_after = conn.execute(
            "SELECT COUNT(*) FROM tags WHERE repo_id = ? AND status = 'pending'",
            (repo_id,),
        ).fetchone()[0]
        console.print(f"  [green]{i}/{len(targets)} {full_name}[/] [dim]({pending_after} pending)[/]")
        total_added += pending_after

    console.print(f"\n[bold green]Re-discovered {len(targets)} repos, {total_added} pending tags total[/]")


@cli.command("export-ecosystem")
@click.option("-o", "--output", "output_paths", multiple=True, help="Output path(s) for ecosystem.json")
@click.option("--catalog", "catalog_path", default=None, help="Path to catalog.json (default ~/.rlex/catalog.json)")
@click.option("--quads", default=118650000, type=int, show_default=True, help="Total quads override for meta")
@click.option("--allow-empty", is_flag=True, help="Allow writing dataset with zero edges without error")
@click.option("--stdout", is_flag=True, help="Print generated JSON to stdout instead of files")
@click.pass_context
def export_ecosystem_cmd(ctx, output_paths, catalog_path, quads, allow_empty, stdout):
    """Generate ecosystem.json directly from SQLite for repolex-www and repolex-viz."""
    conn = ctx.obj["conn"]

    payload = ecosystem.generate_ecosystem_payload(
        conn,
        catalog_path=catalog_path,
        total_quads=quads,
    )

    total_edges = payload["meta"]["total_edges"]
    total_repos = payload["meta"]["total_repos"]

    if total_edges == 0 and not allow_empty:
        # Check if Oxigraph is available to pull edges first
        console.print("[yellow]0 dependency edges found in forx.db.[/]")
        console.print("[cyan]Attempting to bootstrap edges from Oxigraph (http://localhost:7878/query)...[/]")
        try:
            imported = ecosystem.import_sparql_dependencies(conn)
            if imported > 0:
                console.print(f"[green]Successfully imported {imported} edges from Oxigraph![/]")
                # Re-generate payload with the new edges
                payload = ecosystem.generate_ecosystem_payload(
                    conn,
                    catalog_path=catalog_path,
                    total_quads=quads,
                )
                total_edges = payload["meta"]["total_edges"]
                total_repos = payload["meta"]["total_repos"]
        except Exception as e:
            console.print(f"[dim yellow]Could not contact Oxigraph: {e}[/]")

    if total_edges == 0 and not allow_empty:
        console.print("[red]Error: 0 dependency edges found and no cached/imported links available.[/]")
        console.print("Pass --allow-empty to explicitly export an empty graph or run 'forx import-oxigraph-deps'.")
        raise click.Abort()

    json_str = json.dumps(payload, indent=2)

    if stdout:
        click.echo(json_str)
        return

    # Determine output destinations
    destinations = []
    if output_paths:
        destinations = [Path(p) for p in output_paths]
    else:
        # Default standard paths
        default_candidates = [
            ecosystem.DEFAULT_WWW_OUTPUT,
            ecosystem.DEFAULT_VIZ_OUTPUT,
            Path.home() / "repos" / "repolex-ai" / "repolex-viz" / "data" / "ecosystem.json",
        ]
        # Keep unique resolved paths that exist or whose parents exist
        seen = set()
        for cand in default_candidates:
            try:
                resolved = cand.resolve()
            except Exception:
                resolved = cand
            if str(resolved) not in seen and (cand.parent.exists() or cand.exists()):
                seen.add(str(resolved))
                destinations.append(cand)

        if not destinations:
            destinations = [Path("ecosystem.json")]

    for dest in destinations:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json_str)
        console.print(f"[green]Wrote {dest} ({total_repos} nodes, {total_edges} edges)[/]")


@cli.command("backfill-languages")
@click.option("--dry-run", is_flag=True, help="Print inferred languages without updating database")
@click.pass_context
def backfill_languages_cmd(ctx, dry_run):
    """Backfill repos.language for all tracked repositories with missing language."""
    conn = ctx.obj["conn"]
    console.print("[cyan]Backfilling missing repo languages...[/]")
    updated, total = ecosystem.backfill_languages(conn, dry_run=dry_run)
    mode = "[yellow](dry-run)[/]" if dry_run else ""
    console.print(f"[bold green]Successfully assigned language to {updated}/{total} repos {mode}[/]")


@cli.command("import-oxigraph-deps")
@click.option("--url", default="http://localhost:7878/query", show_default=True, help="Oxigraph SPARQL query endpoint")
@click.pass_context
def import_oxigraph_deps_cmd(ctx, url):
    """Import dependency triples from Oxigraph SPARQL store into forx.db."""
    conn = ctx.obj["conn"]
    console.print(f"[cyan]Querying Oxigraph at {url}...[/]")
    try:
        count = ecosystem.import_sparql_dependencies(conn, sparql_url=url)
        console.print(f"[bold green]Successfully imported {count} dependency edges into forx.db![/]")
    except Exception as e:
        console.print(f"[red]Failed to import from Oxigraph: {e}[/]")
        raise click.Abort()

