# About forx

## What it does

forx is the orchestrator for the repolex-forx parsing pipeline. It takes open source repos, discovers their version tags, and dispatches parallel GitHub Actions workflows to parse each version into RDF (N-Quads). The parsed data is stored in per-repo storage repos under the `repolex-forx` GitHub org.

## Why it exists

The previous system (`repolex-forx-tools`) used GitHub Actions cron schedules to trigger parsing one tag at a time. GitHub throttled the cron to ~1 run every 2 hours, making it impractical to parse hundreds of tags across dozens of repos. The manifest.json-based state tracking through git commits was fragile and prone to race conditions.

forx replaces all of that with:
- **Local SQLite database** for state tracking (no git-based coordination)
- **workflow_dispatch** triggers instead of cron (immediate, not throttled)
- **Parallel dispatch** of up to 20 GitHub Actions workers at once
- **One job per repo** constraint to enable blob cache reuse between tags

## How it works

```
You (local machine)                    GitHub Actions
─────────────────                      ──────────────
forx add org/repo
  → discovers semver tags
  → stores in ~/.forx/forx.db

forx run
  → picks one pending tag per repo
  → dispatches up to 20 workflows ──→  parse.yml workers:
  → polls for completion                 1. checkout storage repo
  → marks complete/failed                2. clone source at tag
  → dispatches next batch                3. repolex parse + aggregate
                                         4. commit & push to storage repo
```

## Key design decisions

**One job per storage repo at a time**: repolex checks for existing blob graphs and skips them. Running tags sequentially means each parse builds on the cached blobs from the previous tag, saving significant time. Concurrent jobs on the same repo would duplicate work and cause git push conflicts.

**Local orchestration, remote execution**: The CLI runs on your machine and manages state locally. The actual parsing happens on GitHub Actions runners. This avoids GitHub's cron throttling entirely while still leveraging free CI compute.

**Smart version selection**: Not every patch release is worth parsing. forx discovers all semver tags but selects only the latest patch per major.minor series (e.g., from 1.0.0, 1.0.1, 1.0.2, it picks 1.0.2). This keeps the workload manageable.

## Storage layout

Each repo gets a storage repo in the `repolex-forx` org:
- `TopQuadrant/shacl` → `repolex-forx/TopQuadrant--shacl`
- `Textualize/rich` → `repolex-forx/Textualize--rich`

Within each storage repo, repolex writes to `files/{org}/{repo}/`:
- `blob/` - per-file RDF graphs (named by content SHA)
- `aggregate/ast/` - per-commit AST graphs
- `branch/`, `commit/`, `tag/`, `filetree/` - repo structure graphs
