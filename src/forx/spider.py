"""Spider: crawl parsed repo manifests and queue their dependencies.

Two paths for finding deps:

1. Manifest path (legacy): fetch repo-manifest.jsonld / manifest.json from
   the storage repo, look for repolex:dependencyRepo. The current parser
   doesn't write these fields, so this path finds almost nothing.

2. Source path (new, the workhorse): fetch package files (package.json,
   pyproject.toml, Cargo.toml, go.mod) directly from the SOURCE repo via
   GitHub API. Resolve declared package names to GitHub repos via npm /
   pypi / crates.io / direct-URL parsing. Queue the resolved repos.

Path 2 is a stopgap until the parser learns to write resolved deps to
its manifest at parse time (the proper fix).
"""

import base64
import json
import re
import subprocess
import urllib.request
import urllib.error

from rich.console import Console

from . import db, discover

console = Console()


# In-process resolution cache: package_name → github full_name (or None
# if known unresolvable). Avoids hammering registry APIs in a single run.
_resolution_cache: dict[tuple[str, str], str | None] = {}


def _fetch_json(url: str) -> dict | None:
    """Fetch JSON from a URL."""
    try:
        req = urllib.request.Request(url)
        req.add_header("Cache-Control", "no-cache")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def fetch_manifest(storage_repo: str) -> dict | None:
    """Fetch manifest from a storage repo. Tries new format first, falls back to legacy."""
    base = f"https://raw.githubusercontent.com/{storage_repo}/main"

    # Try new format: repo-manifest.jsonld → latest parsed commit manifest
    repo_manifest = _fetch_json(f"{base}/repo-manifest.jsonld")
    if repo_manifest:
        # Find latest parsed commit
        for commit in repo_manifest.get("repolex:trackedCommit", []):
            if commit.get("repolex:parseStatus") == "parsed":
                sha = commit.get("git:hexsha", "")
                if sha:
                    per_commit = _fetch_json(f"{base}/manifests/commit-manifest-{sha}.jsonld")
                    if per_commit:
                        return per_commit
        return None

    # Fall back to legacy manifest.json
    return _fetch_json(f"{base}/manifest.json")


def get_dependencies_from_manifest(manifest: dict) -> list[dict]:
    """Extract all unique dependencies from a manifest (handles both formats)."""
    seen = set()
    deps = []

    # New JSON-LD format: repolex:dependencyRepo array
    for dep in manifest.get("repolex:dependencyRepo", []):
        dep_id = dep.get("@id", "")
        # Extract org/repo from URI like https://repolex.ai/r/apache/jena
        if "/r/" in dep_id:
            full_name = dep_id.split("/r/", 1)[1]
            if "/" in full_name and full_name not in seen:
                seen.add(full_name)
                deps.append({"full_name": full_name, "package": dep.get("repolex:packageName", "")})

    # Legacy format: versions[].dependencies[]
    for version in manifest.get("versions", []):
        for dep in version.get("dependencies", []):
            org = dep.get("githubOrg", "")
            repo = dep.get("githubRepo", "")
            if org and repo:
                full_name = f"{org}/{repo}"
                if full_name not in seen:
                    seen.add(full_name)
                    deps.append({"full_name": full_name, "package": dep.get("packageName", "")})

    return deps


def spider_repo(conn, full_name: str, storage_repo: str) -> list[str]:
    """
    Spider a single repo: fetch its manifest, find dependencies,
    add new ones to the DB. Returns list of newly added repos.
    """
    manifest = fetch_manifest(storage_repo)
    if manifest is None:
        return []

    deps = get_dependencies_from_manifest(manifest)
    if not deps:
        return []

    added = []
    for dep in deps:
        dep_name = dep["full_name"]

        # Skip self-references
        if dep_name == full_name:
            continue

        # Check if already tracked
        existing = conn.execute(
            "SELECT id FROM repos WHERE full_name = ?", (dep_name,)
        ).fetchone()

        if existing:
            continue

        # Try to discover tags
        try:
            tags = discover.discover_repo(dep_name)
        except Exception:
            console.print(f"    [yellow]Could not access {dep_name}[/]")
            continue

        if not tags:
            console.print(f"    [dim]{dep_name} - no tags, skipping[/]")
            continue

        # Add to DB
        repo_id = db.add_repo(conn, dep_name)
        db.add_tags(conn, repo_id, tags)
        added.append(dep_name)
        console.print(f"    [green]+ {dep_name}[/] ({len(tags)} tags)")

    return added


def spider_all(conn) -> int:
    """
    Spider all parsed repos. For each:
      1. Try manifest path (repolex:dependencyRepo in storage repo manifest)
      2. Fall back to source path (package files in source repo)

    Only spiders repos that have at least one completed parse — no point
    trying to discover deps from a repo we haven't successfully parsed yet.
    Returns total new repos added.
    """
    repos = conn.execute(
        """SELECT DISTINCT r.full_name, r.storage_repo
           FROM repos r
           JOIN tags t ON t.repo_id = r.id
           WHERE t.status = 'complete'
           ORDER BY r.full_name"""
    ).fetchall()

    total_added = []

    for repo in repos:
        console.print(f"[cyan]Spidering {repo['full_name']}...[/]")

        # Manifest path (usually empty under current parser)
        added = spider_repo(conn, repo["full_name"], repo["storage_repo"])
        total_added.extend(added)

        # Source path (the workhorse)
        try:
            source_added = spider_repo_via_source(conn, repo["full_name"])
            total_added.extend(source_added)
        except Exception as e:
            console.print(f"  [dim yellow]source spider error: {e}[/]")

    if total_added:
        console.print(f"\n[bold green]Added {len(total_added)} new repos from dependencies:[/]")
        for name in total_added[:30]:
            console.print(f"  {name}")
        if len(total_added) > 30:
            console.print(f"  ... and {len(total_added) - 30} more")
    else:
        console.print("\n[dim]No new dependencies found[/]")

    return len(total_added)


# ============================================================================
# Source path: fetch package files directly from the source repo via gh API,
# parse declared deps, resolve to GitHub repos via package registries.
# ============================================================================


# Package files we know how to parse, in order of preference per ecosystem.
# Each entry: (path, ecosystem, parser function name).
PACKAGE_FILES = [
    ("pyproject.toml", "pypi", "_parse_pyproject_toml"),
    ("setup.py", "pypi", "_parse_setup_py"),
    ("requirements.txt", "pypi", "_parse_requirements_txt"),
    ("package.json", "npm", "_parse_package_json"),
    ("Cargo.toml", "crates", "_parse_cargo_toml"),
    ("go.mod", "go", "_parse_go_mod"),
]

# Don't try to resolve these — they're stdlib, build tooling, or wildly
# common false positives that would clog the queue.
DEP_BLOCKLIST: set[str] = {
    # python stdlib & ubiquitous
    "python", "pip", "setuptools", "wheel", "tomli", "typing_extensions",
    # js stdlib-ish
    "node", "fs", "path", "child_process", "crypto", "events", "stream",
    "util", "url", "http", "https", "os", "buffer", "process",
    # tooling
    "tsc", "typescript", "eslint", "prettier", "rollup", "webpack", "vite",
    "jest", "vitest", "mocha", "pytest",
    # known meta packages
    "@types", "babel-runtime",
}


def _gh_api_get(path: str) -> dict | list | None:
    """Make a gh api GET call. Returns parsed JSON or None on failure."""
    try:
        result = subprocess.run(
            ["gh", "api", path],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        return None


def fetch_source_file(source_repo: str, path: str) -> str | None:
    """
    Fetch a file's contents from a source GitHub repo via the contents API.
    Returns the decoded text, or None if the file doesn't exist.
    """
    data = _gh_api_get(f"repos/{source_repo}/contents/{path}")
    if not data or not isinstance(data, dict):
        return None
    encoded = data.get("content", "")
    if not encoded or data.get("encoding") != "base64":
        return None
    try:
        return base64.b64decode(encoded).decode("utf-8", errors="replace")
    except Exception:
        return None


def _parse_pyproject_toml(text: str) -> list[str]:
    """Extract direct dependency package names from pyproject.toml.
    Catches PEP 621 [project.dependencies] and Poetry [tool.poetry.dependencies].
    """
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore

    deps: list[str] = []
    try:
        data = tomllib.loads(text)
    except Exception:
        return deps

    # PEP 621 [project] dependencies = ["foo", "bar>=1.0", ...]
    proj = data.get("project", {})
    for entry in proj.get("dependencies", []) or []:
        name = _extract_pep508_name(entry)
        if name:
            deps.append(name)

    # Poetry [tool.poetry.dependencies]
    poetry = data.get("tool", {}).get("poetry", {})
    for name in (poetry.get("dependencies") or {}).keys():
        if name and name.lower() != "python":
            deps.append(name)

    return deps


def _extract_pep508_name(spec: str) -> str | None:
    """Pull the package name out of a PEP 508 dep string like 'foo>=1.0; python<3'."""
    spec = spec.strip()
    if not spec or spec.startswith("#"):
        return None
    # Stop at any of: extras [, version comparator, semicolon, space
    m = re.match(r"^([A-Za-z0-9_.\-]+)", spec)
    return m.group(1) if m else None


def _parse_setup_py(text: str) -> list[str]:
    """Extract install_requires from a setup.py via regex (no execution).
    Brittle but cheap. Misses dynamic constructions.
    """
    deps: list[str] = []
    m = re.search(r"install_requires\s*=\s*\[([^\]]*)\]", text, re.DOTALL)
    if not m:
        return deps
    block = m.group(1)
    for entry in re.findall(r"['\"]([^'\"]+)['\"]", block):
        name = _extract_pep508_name(entry)
        if name:
            deps.append(name)
    return deps


def _parse_requirements_txt(text: str) -> list[str]:
    """Extract package names from a requirements.txt file."""
    deps: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        name = _extract_pep508_name(line)
        if name:
            deps.append(name)
    return deps


def _parse_package_json(text: str) -> list[str]:
    """Extract direct dependencies from package.json."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    deps = list((data.get("dependencies") or {}).keys())
    # Skip devDependencies — too noisy for broad-and-wide. Can revisit.
    return deps


def _parse_cargo_toml(text: str) -> list[str]:
    """Extract direct dependencies from Cargo.toml."""
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore

    try:
        data = tomllib.loads(text)
    except Exception:
        return []
    return list((data.get("dependencies") or {}).keys())


def _parse_go_mod(text: str) -> list[str]:
    """Extract module paths from a go.mod file. Returns import paths,
    not package names — go.mod imports often look like github.com/foo/bar
    which we can map to repos directly without registry resolution."""
    deps: list[str] = []
    in_block = False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("require ("):
            in_block = True
            continue
        if in_block and line == ")":
            in_block = False
            continue
        if in_block:
            parts = line.split()
            if parts and "/" in parts[0]:
                deps.append(parts[0])
        elif line.startswith("require "):
            parts = line.split()
            if len(parts) >= 2 and "/" in parts[1]:
                deps.append(parts[1])
    return deps


def resolve_dep(name: str, ecosystem: str) -> str | None:
    """Resolve a package name to a GitHub full_name (org/repo).
    Returns None if unresolvable. Memoized for the process lifetime.
    """
    if not name:
        return None
    if name.lower() in DEP_BLOCKLIST:
        return None
    cache_key = (ecosystem, name.lower())
    if cache_key in _resolution_cache:
        return _resolution_cache[cache_key]

    resolved: str | None = None
    try:
        if ecosystem == "go":
            # Go module paths starting with github.com/foo/bar map directly
            if name.startswith("github.com/"):
                parts = name.removeprefix("github.com/").split("/")
                if len(parts) >= 2:
                    resolved = f"{parts[0]}/{parts[1]}"
        elif ecosystem == "pypi":
            resolved = _resolve_pypi(name)
        elif ecosystem == "npm":
            resolved = _resolve_npm(name)
        elif ecosystem == "crates":
            resolved = _resolve_crates(name)
    except Exception:
        resolved = None

    _resolution_cache[cache_key] = resolved
    return resolved


_GITHUB_URL_RE = re.compile(
    r"https?://(?:www\.)?github\.com/([A-Za-z0-9_.\-]+)/([A-Za-z0-9_.\-]+?)(?:\.git|/|$)"
)


def _extract_github_repo(url: str) -> str | None:
    """Pull org/repo out of a GitHub URL of any common shape."""
    if not url:
        return None
    m = _GITHUB_URL_RE.search(url.strip())
    if not m:
        return None
    return f"{m.group(1)}/{m.group(2).removesuffix('.git')}"


def _http_get_json(url: str, timeout: int = 10) -> dict | None:
    """GET a URL, return parsed JSON, or None on failure."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "forx-spider/0.1"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError, TimeoutError):
        return None


def _resolve_pypi(name: str) -> str | None:
    """Resolve a PyPI package to its GitHub repo via project_urls / home_page."""
    data = _http_get_json(f"https://pypi.org/pypi/{name}/json")
    if not data:
        return None
    info = data.get("info", {})
    # Try project_urls first
    urls = info.get("project_urls") or {}
    for key in ("Source", "Source Code", "Repository", "Code", "Homepage", "Home"):
        u = urls.get(key)
        repo = _extract_github_repo(u) if u else None
        if repo:
            return repo
    # Fall back to home_page
    return _extract_github_repo(info.get("home_page", "") or "")


def _resolve_npm(name: str) -> str | None:
    """Resolve an npm package to its GitHub repo via the registry."""
    # Encode scoped packages: @scope/name → @scope%2Fname
    encoded = name.replace("/", "%2F")
    data = _http_get_json(f"https://registry.npmjs.org/{encoded}")
    if not data:
        return None
    repo = data.get("repository")
    if isinstance(repo, dict):
        url = repo.get("url", "")
    elif isinstance(repo, str):
        url = repo
    else:
        url = ""
    return _extract_github_repo(url)


def _resolve_crates(name: str) -> str | None:
    """Resolve a crates.io crate to its GitHub repo."""
    data = _http_get_json(f"https://crates.io/api/v1/crates/{name}")
    if not data:
        return None
    crate = data.get("crate", {})
    return _extract_github_repo(crate.get("repository", "") or crate.get("homepage", ""))


def discover_deps_for_repo(source_repo: str) -> list[str]:
    """
    Walk known package files in a source repo, parse declared deps,
    resolve to GitHub repos. Returns deduped list of org/repo strings.
    """
    parsers = {
        "_parse_pyproject_toml": _parse_pyproject_toml,
        "_parse_setup_py": _parse_setup_py,
        "_parse_requirements_txt": _parse_requirements_txt,
        "_parse_package_json": _parse_package_json,
        "_parse_cargo_toml": _parse_cargo_toml,
        "_parse_go_mod": _parse_go_mod,
    }

    seen_resolved: set[str] = set()
    for path, ecosystem, parser_name in PACKAGE_FILES:
        text = fetch_source_file(source_repo, path)
        if not text:
            continue
        try:
            names = parsers[parser_name](text)
        except Exception:
            continue
        for name in names:
            resolved = resolve_dep(name, ecosystem)
            if resolved and resolved != source_repo and resolved not in seen_resolved:
                seen_resolved.add(resolved)
    return sorted(seen_resolved)


def spider_repo_via_source(conn, source_repo: str) -> list[str]:
    """
    Discover deps for a source repo by reading its package files directly.
    Adds resolved deps to the db (skipping any already tracked).
    Returns list of newly added repo full_names.
    """
    deps = discover_deps_for_repo(source_repo)
    if not deps:
        return []

    added: list[str] = []
    for dep_name in deps:
        existing = conn.execute(
            "SELECT id FROM repos WHERE full_name = ?", (dep_name,)
        ).fetchone()
        if existing:
            continue

        try:
            tags = discover.discover_repo(dep_name)
        except Exception:
            continue
        if not tags:
            continue

        repo_id = db.add_repo(conn, dep_name)
        db.add_tags(conn, repo_id, tags)
        added.append(dep_name)
        console.print(f"    [green]+ {dep_name}[/] [dim](src-deps from {source_repo})[/]")

    return added
