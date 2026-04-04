# Versioned Reparse & Repo Drop

**Author:** 4RX  
**Date:** 2026-04-04  
**Status:** Design proposal  
**Related:** forx `reparse` command, `PARSER_VERSION` in db.py, storage repo manifest format

## Problem

We have a single `PARSER_VERSION` pin that controls all reparse decisions. When bumped, `forx reparse` resets every completed tag to pending — full reparse of everything. But not all parser changes require full reparse:

| Change | Example | Blobs affected? | Enrichment affected? |
|---|---|---|---|
| New blob extractor | security scanner (db50ce0) | YES — blob .nq.gz changes | YES (cascades) |
| AST parser fix | tree-sitter grammar update | YES | YES (cascades) |
| LSP batch size | ecccffb (batch=10) | NO | YES |
| Aggregate memory fix | chunked bulk_load | NO | Maybe (same output, different perf) |
| New enrichment step | dataflow analysis | NO | YES |

Currently all of these trigger a full reparse. The security scanner change (db50ce0) adds triples to blob files, so blobs genuinely changed — full reparse is correct. But the memory fix (ecccffb) only changed how enrichment runs, not what it outputs — blobs are identical, and re-running blob parsing wastes hours.

### Second problem: dropping a repo

There's no `forx drop` command. When blobs are invalidated, we need to:
1. Clean storage repo data
2. Update manifests
3. Reset forx.db state
4. Re-sync forx-index

Currently this is manual and error-prone.

## Design

### Two version tracks

Split `PARSER_VERSION` into two independent version pins:

```python
# db.py
BLOB_VERSION = "v0.2.0"     # changes to blob-level output (AST, scanner, extractors)
ENRICH_VERSION = "v0.1.4"   # changes to enrichment (aggregate, LSP, dataflow)
```

**`BLOB_VERSION`** — bump when anything changes that affects individual blob `.nq.gz` files:
- Tree-sitter grammar updates
- New per-file extractors (security scanner)
- Changes to RDF output format for blobs
- Namespace/ontology changes that affect blob triples

**`ENRICH_VERSION`** — bump when anything changes that affects post-blob processing:
- Aggregate logic changes
- LSP resolution changes
- New enrichment steps (dataflow, metrics)
- Performance-only changes that don't affect output do NOT require a bump

### Tag status in forx.db

Add `blob_version` and `enrich_version` columns to the tags table:

```sql
ALTER TABLE tags ADD COLUMN blob_version TEXT;
ALTER TABLE tags ADD COLUMN enrich_version TEXT;
```

On completion, both are recorded:

```python
def mark_complete(conn, tag_id, commit_sha=None):
    conn.execute(
        """UPDATE tags SET status = 'complete', completed_at = ?,
           commit_sha = COALESCE(?, commit_sha),
           blob_version = ?, enrich_version = ?
           WHERE id = ?""",
        (now(), commit_sha, BLOB_VERSION, ENRICH_VERSION, tag_id),
    )
```

### Smart reparse

Replace the single `forx reparse` with version-aware invalidation:

```
forx reparse              # invalidate anything with stale blob OR enrich version
forx reparse --blobs      # only invalidate stale blob versions (full reparse)
forx reparse --enrich     # only invalidate stale enrich versions (keep blobs)
forx reparse --dry-run    # show what would be invalidated
```

Logic:

```python
def invalidate_old_parses(conn, blobs=True, enrich=True):
    conditions = []
    if blobs:
        conditions.append(
            "(blob_version IS NULL OR blob_version != ?)"
        )
    if enrich:
        conditions.append(
            "(enrich_version IS NULL OR enrich_version != ?)"
        )
    where = " OR ".join(conditions)
    # ...
```

When only `enrich_version` is stale:
- Tag status → `'enrich-pending'` (new status)
- Orchestrator dispatches enrich-only workflow (skips blob parsing)
- Blobs are preserved in storage repo

When `blob_version` is stale:
- Tag status → `'pending'` (full reparse)
- Storage repo blobs are cleaned (see "Repo Drop" below)

### New tag statuses

```
pending          → not yet parsed
dispatched       → workflow running
parsing          → blob phase in progress
enrich-pending   → blobs valid, enrichment needs re-running
enrich-dispatched → enrich-only workflow running
complete         → fully parsed and enriched
failed           → parse or enrich failed
```

### Workflow changes

Add an `enrich-only` mode to parse.yml that skips the Parse job and goes straight to Enrich:

```yaml
inputs:
  mode:
    description: 'Parse mode: full or enrich-only'
    required: false
    default: 'full'
```

The dispatch module checks mode:

```python
def dispatch_workflow(repo, tag, storage_repo, mode="full"):
    fields = [
        f"repo={repo}",
        f"tag={tag}",
        f"storage_repo={storage_repo}",
        f"parser_ref={BLOB_VERSION}",  # or ENRICH_VERSION for enrich-only
        f"mode={mode}",
    ]
    # ...
```

## Repo Drop

### `forx drop` command

```
forx drop org/repo              # drop all parsed data, reset to pending
forx drop org/repo --tag v1.0   # drop a specific tag only
forx drop org/repo --remove     # drop and remove repo from tracking entirely
forx drop --dry-run             # show what would be affected
```

### What drop does

#### Step 1: Update forx.db

```python
def drop_repo(conn, full_name, tag=None, remove=False):
    repo = conn.execute(
        "SELECT id, storage_repo FROM repos WHERE full_name = ?",
        (full_name,)
    ).fetchone()

    if tag:
        # Drop specific tag
        conn.execute(
            """UPDATE tags SET status = 'pending',
               blob_version = NULL, enrich_version = NULL,
               workflow_run_id = NULL, dispatched_at = NULL,
               completed_at = NULL, error = NULL
               WHERE repo_id = ? AND git_tag = ?""",
            (repo['id'], tag),
        )
    else:
        # Drop all tags
        conn.execute(
            """UPDATE tags SET status = 'pending',
               blob_version = NULL, enrich_version = NULL,
               workflow_run_id = NULL, dispatched_at = NULL,
               completed_at = NULL, error = NULL
               WHERE repo_id = ?""",
            (repo['id'],),
        )

    if remove:
        conn.execute("DELETE FROM tags WHERE repo_id = ?", (repo['id'],))
        conn.execute("DELETE FROM repos WHERE id = ?", (repo['id'],))

    conn.commit()
    return repo['storage_repo']
```

#### Step 2: Clean storage repo

For a full repo drop, remove parse output but preserve structure:

```
KEEP:
  repo-manifest.jsonld  (update parseStatus → "invalidated")
  index.json            (rebuild after drop)
  README.md             (regenerate on next parse)
  manifests/            (keep as history, update status)

DELETE:
  blob/                 (all blob .nq.gz files)
  aggregate/            (ast, lsp, dataflow, repolex subdirs)

KEEP (git metadata doesn't change):
  commit/
  branch/
  tag/
  filetree/
  dep/
```

Implementation via gh CLI:

```python
def clean_storage_repo(storage_repo, tag=None):
    """Remove parse output from storage repo."""
    # For tag-specific drop, only remove that tag's aggregate
    # For full drop, remove blob/ and aggregate/ directories
    # Update repo-manifest.jsonld with invalidated status
    pass
```

#### Step 3: Update repo-manifest.jsonld

Add `"invalidated"` as a valid `parseStatus`:

```json
{
    "@id": "https://repolex.ai/r/apache/jena/commit/1b767c88...",
    "git:hexsha": "1b767c88...",
    "repolex:parseStatus": "invalidated",
    "repolex:invalidatedAt": "2026-04-04T12:00:00Z",
    "repolex:invalidationReason": "blob_version_bump:v0.1.2→v0.2.0",
    "repolex:previouslyParsedAt": "2026-04-04T07:00:35Z",
    "repolex:previousBlobVersion": "v0.1.2",
    "repolex:previousEnrichVersion": "v0.1.3",
    "git:tagName": "jena-6.0.0"
}
```

This preserves history — we know the tag was once parsed, when, with what version, and why it was invalidated. The manifest is an audit log.

#### Step 4: Sync forx-index

After drop, run `forx index` to sync updated manifests to forx-index repo.

### Parse status lifecycle

```
pending → dispatched → complete → invalidated → pending → dispatched → ...
                                       ↑
                        (blob_version bump or manual drop)

complete → enrich-pending → enrich-dispatched → complete
                ↑
    (enrich_version bump only)
```

## Version tracking in manifests

Add version provenance to commit manifests:

```json
{
    "repolex:trackedCommit": [{
        "@id": "https://repolex.ai/r/apache/jena/commit/1b767c88...",
        "repolex:parseStatus": "parsed",
        "repolex:blobVersion": "v0.2.0",
        "repolex:enrichVersion": "v0.1.4",
        "repolex:parsedAt": "2026-04-04T...",
        "git:tagName": "jena-6.0.0"
    }]
}
```

This lets any consumer (lexq, rlex, SPARQL queries) know exactly which parser produced the data.

## Migration path

1. Add `blob_version` and `enrich_version` columns to tags table (nullable, backward compatible)
2. Backfill: set `blob_version = parser_version` and `enrich_version = parser_version` for all complete tags
3. Rename `PARSER_VERSION` → keep as alias that bumps both for backward compatibility
4. Add `enrich-pending` and `enrich-dispatched` statuses
5. Add `mode` input to parse.yml workflow
6. Implement `forx drop` command
7. Update `forx reparse` with `--blobs` / `--enrich` flags
8. Update manifest format with version provenance and `invalidated` status

Steps 1-3 are backward compatible and can ship immediately. Steps 4-8 can follow incrementally.

## Connection to incremental computation research

This design is the first concrete step toward the incremental pipeline vision from the April 2026 research scan:

- **Content-addressed blob skipping**: `blob_version` is a coarse proxy for "did the blob parser change?" Future: hash the parser code itself for exact change detection.
- **IncRML pattern**: invalidation metadata in manifests is a simplified version of LDES change events. Future: emit invalidation as Activity Streams events for downstream consumers.
- **Differential enrichment**: `enrich-only` mode is the first implementation of "keep valid data, only recompute what changed." Future: per-file enrichment skipping based on blob SHA overlap between tags.

## Open questions

1. **Should `forx drop` delete the storage repo entirely for `--remove`?** Or just empty it? Deleting repos via API is destructive and hard to undo.
2. **Blob garbage collection**: if we drop a tag, other tags may share the same blobs (content-addressed). Should we reference-count blobs? For now, dropping all blobs on a full repo drop is simplest.
3. **Notification**: should `forx drop` notify forx-index consumers? This is where LDES would help — publish an "invalidation" event that downstream subscribers can react to.
4. **Storage repo size**: after many reparse cycles, git history grows. Should we force-push a cleaned history, or accept the growth? Git's deduplication helps, but aggregate files are large.
