# forx

Orchestrator for the repolex-forx parsing pipeline. Discovers version tags from open source repos, dispatches parallel GitHub Actions to parse them into RDF, and tracks progress.

## Install

```bash
uv tool install .
```

## Usage

```bash
# Add repos to parse
forx add TopQuadrant/shacl pallets/click Textualize/rich

# Start the orchestrator (dispatches up to 20 parallel parse jobs)
forx run

# Check status
forx status

# See all repos and tags
forx list-repos

# Retry failed parses
forx retry --all
```
