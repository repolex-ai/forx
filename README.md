# forx

> **Orchestrator for the `repolex-forx` parsing pipeline.** Parse every open-source repository into structured RDF knowledge graphs at scale.

`forx` is a command-line orchestrator that discovers repositories and version tags across GitHub, coordinates remote GitHub Actions workers, manages local queue state in SQLite, and builds the public `forx-index` knowledge graph catalog.

---

## Architecture & How It Works

Instead of relying on throttled cron schedules, `forx` coordinates local state with remote execution:

```
Local Machine (forx CLI)                             GitHub Actions Runners
────────────────────────                             ──────────────────────
1. forx add org/repo
   → Discovers semver tags via Git API
   → Stores in ~/.forx/forx.db
                                                    
2. forx run
   → Selects pending tags (1 per storage repo)
   → Triggers workflows via workflow_dispatch ────►  parse.yml runner:
   → Monitors execution & polls status                1. Check out storage repo
   → Auto-dispatches next batch (up to 20 parallel)   2. Clone source at tag
                                                      3. repolex parse + aggregate
                                                      4. Commit & push RDF to storage repo
3. forx index
   → Pulls manifests into forx-index
   → Generates catalog for rlex / lexq query tools
```

### Key Design Decisions

1. **One Active Job per Storage Repo**: Repolex reuses content-addressed blob graphs between tags. Running tags sequentially per repository guarantees that subsequent tag parses build on existing cached blobs, cutting parse times dramatically and preventing git push conflicts.
2. **Local Orchestration, Remote Execution**: All scheduling, prioritization, and retry logic run locally on your machine via a local SQLite database (`~/.forx/forx.db`), completely bypassing GitHub cron throttling.
3. **Smart Version Selection**: By default, `forx` selects the latest patch per major.minor release series, avoiding redundant parsing of intermediate patch versions while ensuring full API lifecycle coverage.

---

## Installation

### Prerequisites
- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/) (recommended) or `pip`
- GitHub CLI (`gh`) authenticated with repository write access to the `repolex-forx` organization.

### Installing with `uv`

```bash
# In the repository directory
cd /path/to/repolex-ai/forx
uv tool install .

# Or install in editable mode for development
uv pip install -e .
```

Verify installation:

```bash
forx --help
```

---

## CLI Usage Reference

### Adding Repositories

```bash
# Add specific repositories and discover their release tags
forx add pallets/click Textualize/rich encode/httpx

# Add a repository without releases (parses HEAD on default branch)
forx add --head TopQuadrant/shacl-js

# Batch-add all repositories from an entire GitHub organization
forx add-org pallets --min-stars 50
```

### Running the Orchestration Pipeline

```bash
# Start the dispatch loop (runs continuously, keeping up to 20 workers active)
forx run
```

### Monitoring & Managing Queues

```bash
# Display summary table of all tracked repos, pending, running, done, and failed parses
forx status

# List all tracked repos, their assigned storage repos, and tag statuses
forx list-repos

# Bump high-priority repositories to the front of the queue
forx prioritize apache/jena Jelly-RDF/jelly-jvm --level 20

# Reset failed tags for a specific repo or across all repos
forx retry pallets/click
forx retry --all
```

### Dependency Spider & Crawling

```bash
# Spider all completed repositories: inspects manifests for library dependencies
# and automatically queues new upstream/downstream repos
forx crawl
```

### Parser Upgrades & Reparsing

```bash
# Check how many tags were parsed with older versions of repolex
forx reparse --dry-run

# Invalidate and re-queue tags parsed with older parser versions
forx reparse
```

### Index Synchronization

```bash
# Pull repo-manifest.jsonld files from storage repos and compile forx-index
forx index

# Compile index locally without pushing to GitHub
forx index --no-push
```

---

## Storage Layout in `repolex-forx`

Each parsed open-source repository receives a dedicated storage repository under the `repolex-forx` GitHub organization:
- `pallets/click` &rarr; `repolex-forx/pallets--click`
- `Textualize/rich` &rarr; `repolex-forx/Textualize--rich`

Inside each storage repository, Repolex writes structured RDF graphs under `files/{org}/{repo}/`:

```text
files/{org}/{repo}/
├── blob/                  # Content-addressed per-file RDF graphs (SHA-named)
├── aggregate/
│   └── ast/               # Aggregate AST graphs per commit
├── branch/                # Branch reference graphs
├── commit/                # Commit metadata graphs
├── tag/                   # Version tag graphs
└── filetree/              # Repository filesystem hierarchy graphs
```

---

## Ecosystem Integration

- **[`repolex-parser-py`](https://github.com/repolex-ai/repolex-parser-py)**: The underlying Python parser executed by GitHub Actions runners to produce the RDF graphs.
- **[`forx-index`](https://github.com/repolex-forx/forx-index)**: The central catalog compiled by `forx index`.
- **[`rlex`](https://github.com/repolex-ai/rlex)** & **[`lexq`](https://github.com/repolex-ai/lexq)**: High-speed query clients used by human developers and autonomous agents to query the parsed knowledge graphs via SPARQL.

---

## License

MIT License. Developed for the [Repolex](https://repolex.ai) ecosystem.
