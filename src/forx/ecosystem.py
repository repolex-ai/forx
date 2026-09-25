"""
Ecosystem and dependency graph management for forx.

Provides:
- Ecosystem and language inference for tracked repositories
- Dependency import from Oxigraph / SPARQL
- Direct SQLite export of ecosystem.json for repolex-www and repolex-viz
- Language backfilling for repos table
"""

import json
import os
import re
import sqlite3
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_CATALOG_PATH = Path.home() / ".rlex" / "catalog.json"
DEFAULT_SPARQL_URL = "http://localhost:7878/query"

# Default paths for ecosystem.json export
DEFAULT_WWW_OUTPUT = Path("/Volumes/f00/repos/repolex-ai/repolex-www/assets/data/ecosystem.json")
DEFAULT_VIZ_OUTPUT = Path("/Volumes/f00/repos/repolex-ai/repolex-viz/data/ecosystem.json")

ECO_MAP = {
    "CARGO": "Cargo",
    "NPM": "npm",
    "PYPI": "PyPI",
    "MAVEN": "Maven",
    "RUBYGEMS": "RubyGems",
    "GO": "Go",
}

ECOSYSTEM_TO_LANGUAGE = {
    "Cargo": "Rust",
    "PyPI": "Python",
    "Go": "Go",
    "Maven": "Java",
    "RubyGems": "Ruby",
}


def infer_ecosystem(repo_id: str, known_eco: str | None = None) -> str:
    """
    Infer the software ecosystem (Cargo, PyPI, npm, Go, Maven, RubyGems, or Other)
    for a given repository full_name (org/name).
    """
    if known_eco:
        return ECO_MAP.get(known_eco.upper(), known_eco)

    org, name = repo_id.lower().split("/", 1) if "/" in repo_id else ("", repo_id.lower())

    # Rust / Cargo
    if (
        any(k in name for k in ["-rs", "rust", "cargo", "tree-sitter", "parking_lot", "flate2"])
        or name.endswith(".rs")
        or org in [
            "rust-lang", "tokio-rs", "dtolnay", "hyperium", "serde-rs", "rayon-rs",
            "tower-rs", "rustcrypto", "actix", "chronotope", "burntsushi", "alexcrichton", "diesel-rs",
            "crossbeam-rs", "crossterm-rs", "eyre-rs", "ratatui", "repolex-ai", "rust-itertools",
            "rust-rdf", "zkat", "paritytech", "katharostech", "dryrust", "faern", "sebastienrousseau",
            "swc-project", "carllerche", "al8n", "amanieu", "rust-unofficial"
        ]
        or repo_id in ["JelteF/derive_more", "near/nearcore", "Tina-1300/crate"]
    ):
        return "Cargo"

    # Go
    if (
        any(k in name for k in ["golang", "go-", "-go"])
        or name.endswith("/go")
        or org in [
            "golang", "bytedance", "cloudwego", "gin-gonic", "gin-contrib", "go-playground",
            "modern-go", "json-iterator", "quic-go", "klauspost", "stretchr", "ugorji"
        ]
        or repo_id in [
            "bytedance/gopkg", "bytedance/sonic", "cloudwego/base64x", "anthropics/anthropic-sdk-go",
            "davecgh/go-spew", "goccy/go-yaml", "leodido/go-urn", "mattn/go-isatty", "pelletier/go-toml",
            "pmezard/go-difflib", "quic-go/quic-go", "quic-go/qpack", "twitchyliquid64/golang-asm",
            "kr/text", "gabriel-vasile/mimetype"
        ]
    ):
        return "Go"

    # Python / PyPI
    if (
        any(k in name for k in [
            "py", "django", "flask", "numpy", "pandas", "torch", "scikit", "scipy", "pytest", "sphinx",
            "jupyter", "starlette", "uvloop", "pydantic", "fastapi", "aiosignal", "multidict", "yarl",
            "celery", "airflow", "requests", "pip", "setuptools", "wheel", "twine", "hypothesis",
            "frozendict", "munch", "yolox", "prefect", "llama_index", "langchain", "coremltools",
            "asyncio", "httpx", "feedparser", "beautifulsoup", "pillow", "loguru", "gymnasium", "audioop"
        ])
        or name.endswith(".py")
        or org in [
            "pypa", "psf", "pallets", "pallets-eco", "django", "encode", "tiangolo", "huggingface", "run-llama",
            "langchain-ai", "prefecthq", "activestate", "human-signal", "humansignal", "anorov", "magicstack",
            "nousresearch", "aio-libs", "agronholm", "alexmojaki", "andialbrecht", "berkerpeksag",
            "boto", "cloudpipe", "dabeaz", "davidhalter", "deedy5", "erdewit", "explosion",
            "gorakhargosh", "hukkin", "ipython", "jaraco", "jquast", "keleshev", "kennethreitz",
            "kislyuk", "kvesteri", "lancedb", "laurent-laporte-pro", "lepture", "librosa", "lxml",
            "m-bain", "mkdocstrings", "openai", "pexpect", "pixeltable", "praw-dev", "psycopg",
            "pytest-dev", "python-cffi", "python-greenlet", "python-thread", "python-trio", "pytorch",
            "requests", "samuelcolvin", "sdispater", "sphinx-contrib", "sphinx-doc", "sqlalchemy",
            "tim-osterhus", "tkem", "tornadoweb", "willmcgugan", "wolever", "zopefoundation", "alinaschan",
            "delgan", "farama-foundation", "abstractumbra"
        ]
        or repo_id in [
            "alethiophile/qtoml", "alex/pretend", "florimondmanca/httpx-sse", "pedroburon/dotenv",
            "rbarrois/confutils", "rbarrois/fslib", "rbarrois/tdparser", "rbarrois/uconf",
            "seequent/properties", "testing-cabal/fixtures", "uiri/toml", "carpedm20/emoji",
            "cdgriffith/puremagic", "di/id", "malthe/chameleon", "materialsproject/fireworks",
            "orm011/pgserver", "NVIDIA/NeMo-Relay", "davidfraser/WSGIUtils",
            "apple/corenet", "apple/ml-ane-transformers", "apple/ml-stable-diffusion",
            "asimov-platform/llama-index-asimov"
        ]
    ):
        return "PyPI"

    # JS / TS / npm
    if (
        any(k in name for k in [
            "js", "ts", "webpack", "babel", "eslint", "prettier", "rollup", "vite", "react", "vue",
            "svelte", "express", "fastify", "postcss", "tailwind", "lodash", "xml-parser",
            "definitelytyped", "font-awesome", "angular", "next", "nuxt", "typescript",
            "browser-sync", "throat", "markdownlint", "istanbul", "chalker"
        ])
        or name.endswith(".js")
        or name.endswith(".ts")
        or org in [
            "browserify", "acornjs", "jquery", "expressjs", "trpc", "definitelytyped", "fortawesome",
            "microsoft", "vercel", "facebook", "sindresorhus", "1337programming", "fridus", "nmfr",
            "naturalintelligence", "axios", "bcomnes", "bower", "colinhacks", "component", "cspotcode",
            "cypress-io", "debug-js", "es-shims", "eslint", "evanw", "gruntjs", "jestjs", "jshttp",
            "ladjs", "lerna", "listr2", "ljharb", "markedjs", "micromatch", "mrmlnc", "netlify",
            "npm", "okonet", "open-cli-tools", "paulmillr", "pillarjs", "prettier", "remy", "rollup",
            "sass", "standard-schema", "testing-library", "thinkmill", "tinylibs", "typicode",
            "webpack-contrib", "webpack", "yargs", "afarkas", "alexbrazier", "alexgorbatchev",
            "actions", "chalker", "davidanson", "epiceric", "forbeslindesay", "coderpuppy", "cookpete",
            "browsersync"
        ]
        or repo_id in [
            "brianloveswords/buffer-crc32", "git-albertomarin/winpath", "gotwarlost/istanbul",
            "huafu/bs-logger", "inspect-js/hasOwn", "inspect-js/is-core-module", "intesso/connect-livereload",
            "isaacs/github-flavored-markdown", "jadejs/jade", "jaredhanson/utils-merge", "jharding/grunt-exec",
            "jmreidy/grunt-browserify", "kinnison/marked-yaml", "mattstyles/grunt-banner", "mccormicka/string-argv",
            "novemberborn/ignore-by-default", "onehealth/grunt-open", "senchalabs/connect", "snide/wyrm",
            "stylus/stylus", "substack/node-browserify", "sverweij/dependency-cruiser", "tanstack/intent",
            "tj/connect-redis", "tlvince/make-coverage-badge", "visionmedia/node-cookie-signature",
            "anthropics/claude-code", "anthropics/claude-code-action", "anthropics/claude-code-base-action"
        ]
    ):
        return "npm"

    # Java / JVM / Maven
    if (
        any(k in name for k in [
            "java", "jvm", "maven", "gradle", "spring", "jena", "jelly", "scala", "clojure", "kotlin"
        ])
        or org in [
            "apache", "jelly-rdf", "eclipse", "spring-projects", "quarkusio", "topquadrant",
            "antlr", "google", "junit-team", "qos-ch", "square", "twitter-archive", "hmrc"
        ]
        or repo_id in [
            "google/gson", "junit-team/junit4", "qos-ch/slf4j", "square/okhttp", "square/okio",
            "twitter-archive/diffy", "TopQuadrant/shacl", "apple/servicetalk", "hmrc/service-manager"
        ]
    ):
        return "Maven"

    # Ruby / RubyGems
    if (
        any(k in name for k in [
            "ruby", "gem", "rails", "bundler", "jekyll", "sinatra", "rake", "rubocop", "asciidoctor"
        ])
        or name.endswith(".rb")
        or org in [
            "ruby", "rubygems", "rails", "asciidoctor", "bblimke", "chriseppstein", "ioquatix",
            "jeremyevans", "lsegal", "macournoyer", "minitest", "rack", "socketry", "sorbet",
            "soutaro", "thoughtbot"
        ]
        or repo_id in ["asimov-platform/asimov-universe.rb", "asimov-platform/asimov.rb"]
    ):
        return "RubyGems"

    return "Other"


def infer_language(repo_id: str, ecosystem: str | None = None, current_lang: str | None = None) -> str | None:
    """
    Infer programming language for a repo given its full_name, ecosystem, or existing language.
    """
    if current_lang and current_lang.strip() and current_lang != "?":
        return current_lang.strip()

    eco = ecosystem or infer_ecosystem(repo_id)
    if eco in ECOSYSTEM_TO_LANGUAGE:
        return ECOSYSTEM_TO_LANGUAGE[eco]

    if eco == "npm":
        lower = repo_id.lower()
        if any(k in lower for k in ["-ts", "ts-", ".ts", "typescript"]):
            return "TypeScript"
        return "JavaScript"

    return None


def backfill_languages(conn: sqlite3.Connection, dry_run: bool = False) -> tuple[int, int]:
    """
    Backfill repos.language for all tracked repositories with missing language.
    Uses recorded dependencies ecosystem hints and ecosystem inference.

    Returns:
        (updated_count, total_unassigned_count)
    """
    # 1. Build map of known ecosystems from dependencies table
    known_eco: dict[str, str] = {}
    rows = conn.execute(
        """SELECT r.full_name as source, d.target_full_name as target, d.ecosystem
           FROM dependencies d
           JOIN repos r ON d.source_repo_id = r.id
           WHERE d.ecosystem IS NOT NULL AND d.ecosystem != ''"""
    ).fetchall()

    for r in rows:
        eco = r["ecosystem"]
        src = r["source"]
        tgt = r["target"]
        if src not in known_eco:
            known_eco[src] = eco
        if tgt not in known_eco:
            known_eco[tgt] = eco

    # 2. Get all repos needing language
    unassigned = conn.execute(
        "SELECT id, full_name, language FROM repos WHERE language IS NULL OR language = ''"
    ).fetchall()
    total_unassigned = len(unassigned)

    updated = 0
    to_update = []
    for r in unassigned:
        full_name = r["full_name"]
        eco = infer_ecosystem(full_name, known_eco.get(full_name))
        lang = infer_language(full_name, eco)
        if lang:
            to_update.append((lang, r["id"]))
            updated += 1

    if not dry_run and to_update:
        conn.executemany("UPDATE repos SET language = ? WHERE id = ?", to_update)
        conn.commit()

    return updated, total_unassigned


def load_catalog(catalog_path: str | Path | None = None) -> dict[str, dict]:
    """Load parsed stats from catalog.json if available."""
    path = Path(catalog_path) if catalog_path else DEFAULT_CATALOG_PATH
    if not path.exists():
        return {}

    try:
        with open(path) as f:
            cat = json.load(f)
    except Exception:
        return {}

    def commit_sort_key(c):
        tag = c.get("tag") or ""
        nums = tuple(int(x) for x in re.findall(r"\d+", tag))
        parsed_at = c.get("parsed_at") or ""
        return (nums, parsed_at)

    repos = {}
    for r in cat.get("repos", []):
        full_name = f"{r.get('org', '')}/{r.get('repo', '')}"
        parsed_commits = [c for c in r.get("commits", []) if c.get("status") == "parsed"]

        # Prioritize parsed commits with a dep graph
        commits_with_dep = [
            c for c in parsed_commits
            if any(g.get("graph_type") == "dep" for g in (c.get("graph_files") or []))
        ]

        if commits_with_dep:
            best_c = max(commits_with_dep, key=commit_sort_key)
            tag_has_deps = True
        elif parsed_commits:
            best_c = max(parsed_commits, key=commit_sort_key)
            tag_has_deps = False
        else:
            best_c = None
            tag_has_deps = False

        total_size = sum(
            g.get("size_bytes", 0)
            for c in parsed_commits
            for g in (c.get("graph_files") or [])
        )

        repos[full_name] = {
            "parsed_count": r.get("parsed_count", 0),
            "pending_count": r.get("pending_count", 0),
            "latest_tag": best_c.get("tag") if best_c else None,
            "tag_has_deps": tag_has_deps,
            "parsed_at": best_c.get("parsed_at") if best_c else None,
            "graph_size_bytes": total_size,
        }
    return repos


def import_sparql_dependencies(
    conn: sqlite3.Connection,
    sparql_url: str = DEFAULT_SPARQL_URL,
    timeout: int = 45,
) -> int:
    """
    Import dependency triples from an Oxigraph SPARQL store into forx.db.dependencies.
    Returns the count of dependency edges recorded.
    """
    query = """
    PREFIX repolex: <https://repolex.ai/ontology/repolex/>
    SELECT ?g ?pkg ?ecosystem ?depOrg ?depRepo WHERE {
      GRAPH ?g {
        ?dep a repolex:Dependency ;
             repolex:packageName ?pkg ;
             repolex:packageEcosystem ?ecosystem ;
             repolex:githubOrg ?depOrg ;
             repolex:githubRepo ?depRepo .
      }
      FILTER(CONTAINS(STR(?g), "/dep/"))
    }
    """
    params = urllib.parse.urlencode({"query": query})
    url = f"{sparql_url}?{params}"
    req = urllib.request.Request(url, headers={"Accept": "application/sparql-results+json"})

    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)

    bindings = data.get("results", {}).get("bindings", [])
    if not bindings:
        return 0

    from . import db

    batch = []
    for b in bindings:
        g = b.get("g", {}).get("value", "")
        m = re.search(r"/(?:data|r)/([^/]+/[^/]+)/dep/", g)
        if not m:
            continue
        source = m.group(1)
        dep_org = b.get("depOrg", {}).get("value", "").strip()
        dep_repo = b.get("depRepo", {}).get("value", "").strip()
        if not dep_org or not dep_repo:
            continue
        target = f"{dep_org}/{dep_repo}"
        pkg = b.get("pkg", {}).get("value", "")
        eco = b.get("ecosystem", {}).get("value", "")

        batch.append({
            "source_full_name": source,
            "target_full_name": target,
            "package_name": pkg,
            "ecosystem": eco,
        })

    return db.record_dependencies_batch(conn, batch)


def generate_ecosystem_payload(
    conn: sqlite3.Connection,
    catalog_path: str | Path | None = None,
    total_quads: int = 118650000,
    active_runners: int | None = None,
) -> dict:
    """
    Generate ecosystem graph dataset from SQLite and catalog.json.
    Matches the schema expected by repolex-www/assets/data/ecosystem.json.
    """
    # 1. Load repos & tag counts from SQLite
    cur = conn.cursor()
    cur.execute("""
        SELECT r.id, r.org, r.name, r.full_name, r.storage_repo, r.language,
               COUNT(t.id) as total_tags,
               SUM(CASE WHEN t.status = 'complete' THEN 1 ELSE 0 END) as complete_tags,
               SUM(CASE WHEN t.status = 'dispatched' THEN 1 ELSE 0 END) as in_progress_tags,
               SUM(CASE WHEN t.status = 'failed' THEN 1 ELSE 0 END) as failed_tags,
               SUM(CASE WHEN t.status = 'pending' THEN 1 ELSE 0 END) as pending_tags
        FROM repos r
        LEFT JOIN tags t ON r.id = t.repo_id
        GROUP BY r.id
    """)
    rows = cur.fetchall()

    cur.execute("""
        SELECT r.full_name, t.git_tag, t.completed_at
        FROM tags t
        JOIN repos r ON t.repo_id = r.id
        WHERE t.status = 'complete'
        ORDER BY t.id DESC
    """)
    latest_db_tags = {}
    for fname, git_tag, comp_at in cur.fetchall():
        if fname not in latest_db_tags:
            latest_db_tags[fname] = (git_tag, comp_at)

    db_repos = {}
    for row in rows:
        rid = row["id"]
        org = row["org"]
        name = row["name"]
        full_name = row["full_name"]
        storage = row["storage_repo"]
        lang = row["language"]
        total = row["total_tags"] or 0
        comp = row["complete_tags"] or 0
        inp = row["in_progress_tags"] or 0
        fail = row["failed_tags"] or 0
        pend = row["pending_tags"] or 0

        status = "discovered"
        if comp > 0 and inp == 0:
            status = "complete"
        elif inp > 0:
            status = "in_progress"
        elif comp == 0 and fail > 0:
            status = "failed"
        elif comp > 0 and inp > 0:
            status = "in_progress"

        ltag, lcomp_at = latest_db_tags.get(full_name, (None, None))

        db_repos[full_name] = {
            "id": full_name,
            "org": org,
            "name": name,
            "storage_repo": storage,
            "language": lang,
            "status": status,
            "complete_tags": comp,
            "in_progress_tags": inp,
            "failed_tags": fail,
            "pending_tags": pend,
            "total_tags": total,
            "latest_tag": ltag,
            "parsed_at": lcomp_at,
        }

    # 2. Load catalog stats
    cat_repos = load_catalog(catalog_path)

    # 3. Load dependency edges from dependencies table
    cur.execute("""
        SELECT r.full_name as source, d.target_full_name as target,
               d.package_name, d.ecosystem
        FROM dependencies d
        JOIN repos r ON d.source_repo_id = r.id
    """)
    raw_edges = cur.fetchall()

    edges = []
    seen_edges = set()
    repo_eco = {}

    for r in raw_edges:
        source = r["source"]
        target = r["target"]
        pkg = r["package_name"]
        eco = r["ecosystem"] or ""

        if eco:
            repo_eco[source] = eco
            if target not in repo_eco:
                repo_eco[target] = eco

        if source == target:
            continue
        pair = (source, target)
        if pair in seen_edges:
            continue
        seen_edges.add(pair)
        edges.append({
            "source": source,
            "target": target,
            "package": pkg,
            "ecosystem": eco,
        })

    # Active nodes: either parsed/running/failed, or connected to an edge
    edge_sources = {e["source"] for e in edges}
    edge_targets = {e["target"] for e in edges}
    edge_nodes = edge_sources | edge_targets

    active_ids = set()
    for repo_id, r in db_repos.items():
        if r["status"] in ["complete", "in_progress", "failed"] or repo_id in edge_nodes:
            active_ids.add(repo_id)
    for nid in edge_nodes:
        active_ids.add(nid)

    out_degree: dict[str, int] = {}
    in_degree: dict[str, int] = {}
    for e in edges:
        s, t = e["source"], e["target"]
        out_degree[s] = out_degree.get(s, 0) + 1
        in_degree[t] = in_degree.get(t, 0) + 1

    nodes = []
    status_counts = {"complete": 0, "in_progress": 0, "failed": 0, "discovered": 0}
    ecosystem_counts: dict[str, int] = {}

    for repo_id in sorted(active_ids):
        org, name = repo_id.split("/", 1) if "/" in repo_id else ("", repo_id)
        dbr = db_repos.get(repo_id, {})
        catr = cat_repos.get(repo_id, {})

        status = dbr.get("status")
        if not status:
            if catr.get("parsed_count", 0) > 0:
                status = "complete"
            else:
                status = "complete" if repo_id in edge_sources else "discovered"

        status_counts[status] = status_counts.get(status, 0) + 1

        raw_eco = repo_eco.get(repo_id)
        ecosystem = infer_ecosystem(repo_id, raw_eco)
        ecosystem_counts[ecosystem] = ecosystem_counts.get(ecosystem, 0) + 1

        storage = dbr.get("storage_repo") or f"repolex-forx/{org}--{name}"
        tag = catr.get("latest_tag") or dbr.get("latest_tag")
        tag_has_deps = catr.get("tag_has_deps")
        if tag_has_deps is None:
            tag_has_deps = (repo_id in edge_sources)
        parsed_at = catr.get("parsed_at") or dbr.get("parsed_at")
        graph_size = catr.get("graph_size_bytes", 0)

        nodes.append({
            "id": repo_id,
            "org": org,
            "name": name,
            "status": status,
            "ecosystem": ecosystem,
            "storage_repo": storage,
            "tag": tag,
            "tag_has_deps": bool(tag_has_deps),
            "parsed_at": parsed_at,
            "graph_size_bytes": graph_size,
            "out_degree": out_degree.get(repo_id, 0),
            "in_degree": in_degree.get(repo_id, 0),
            "total_tags": dbr.get("total_tags", 1),
            "complete_tags": dbr.get("complete_tags", 1 if status == "complete" else 0),
        })

    valid_node_ids = {n["id"] for n in nodes}
    valid_edges = [
        e for e in edges
        if e["source"] in valid_node_ids and e["target"] in valid_node_ids
    ]

    # Calculate active runners if not provided
    if active_runners is None:
        runners_row = conn.execute(
            "SELECT COUNT(*) as c FROM tags WHERE status = 'dispatched'"
        ).fetchone()
        active_runners = runners_row["c"] if runners_row else 0

    return {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total_repos": len(nodes),
            "total_edges": len(valid_edges),
            "total_quads": total_quads,
            "active_runners": active_runners,
            "status_counts": status_counts,
            "ecosystem_counts": ecosystem_counts,
        },
        "nodes": nodes,
        "links": valid_edges,
    }
