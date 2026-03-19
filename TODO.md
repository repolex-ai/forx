# TODO

## forx nuke
- `forx nuke pallets/flask` - delete storage repo, reset all tags to pending
- `forx nuke --all` - delete all storage repos, reset entire DB
- Useful during test/iteration phase, eventually gate behind confirmation

## forx update-index
- Auto-generate index.json + README for repolex-forx/.github and repolex-forx/index
- Read from SQLite DB, cross-reference with storage repo manifests
- Update the parsed repos table on the org profile

## GitHub Pages site (repolex-forx/index)
- Static HTML generated from manifests
- Per-repo pages showing versions, commits, files
- Clickable dependency graph (links to other parsed repos)
- Landing page with searchable repo list
- Target: something shareable on HN

## HEAD-only parsing
- Wire up HEAD parsing in orchestrator for repos without tags
- 7-day cooldown between parses
- `forx parse org/repo` to force immediate re-parse

## Manifest file (parser-side)
- Parser produces manifest.json in storage repo root ✅
- Accumulates across parses (append new commits) ✅
- Lists all files produced, dependencies with resolved GitHub repos ✅
- Foundation for index site and lexq download

## Parser version tracking
- Parser writes its version to manifest.json (e.g., `parser_version: "0.3.0"`)
- forx can compare manifest parser_version vs current parser version
- Auto-requeue repos parsed with old parser versions
- Enables automated re-parsing when parser improves (new ontology, better LSP, etc.)

## Dependency spider
- Read dependencies from manifest.json in storage repos
- Auto-add dependency repos to forx queue
- `forx spider` command to crawl all parsed repos and queue their deps
- Recursive: parsed deps reveal their own deps
- Track dependency edges in forx DB for the index site

## Dev org for testing (repolex-forx-dev)
- `repolex-forx` = production, uses pinned parser releases
- `repolex-forx-dev` = testing, pulls parser from HEAD
- `forx add --dev` / `forx run --dev` switches target org
- Workflow needs two install paths: release tag vs HEAD
- Test parser changes on a few repos in dev before promoting
- Broken parser commits never touch production data

## Large repo handling
- pixeltable and sqlalchemy hit 6-hour GitHub Actions timeout on aggregate
- Streaming AST aggregation helps but may not be enough for very large repos
- Consider: chunked aggregate output, skip aggregate for repos over N blobs
- Parser-side: investigate memory usage during LSP enrichment on large codebases
