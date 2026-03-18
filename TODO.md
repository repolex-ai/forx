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
- Parser produces manifest.json in storage repo root
- Accumulates across parses (append new commits)
- Lists all files produced, dependencies with resolved GitHub repos
- Foundation for index site and lexq download
