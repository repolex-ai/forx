"""Unit tests for ecosystem and dependency graph features."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from forx import cli, db, ecosystem


class TestEcosystemInference(unittest.TestCase):
    def test_infer_ecosystem_known(self):
        self.assertEqual(ecosystem.infer_ecosystem("org/repo", "CARGO"), "Cargo")
        self.assertEqual(ecosystem.infer_ecosystem("org/repo", "npm"), "npm")
        self.assertEqual(ecosystem.infer_ecosystem("org/repo", "PYPI"), "PyPI")
        self.assertEqual(ecosystem.infer_ecosystem("org/repo", "Go"), "Go")
        self.assertEqual(ecosystem.infer_ecosystem("org/repo", "maven"), "Maven")
        self.assertEqual(ecosystem.infer_ecosystem("org/repo", "RubyGems"), "RubyGems")

    def test_infer_ecosystem_heuristics(self):
        # Rust / Cargo
        self.assertEqual(ecosystem.infer_ecosystem("tokio-rs/tokio"), "Cargo")
        self.assertEqual(ecosystem.infer_ecosystem("rust-lang/flate2-rs"), "Cargo")
        self.assertEqual(ecosystem.infer_ecosystem("Amanieu/parking_lot"), "Cargo")
        self.assertEqual(ecosystem.infer_ecosystem("swc-project/swc"), "Cargo")

        # Python / PyPI
        self.assertEqual(ecosystem.infer_ecosystem("pallets/flask"), "PyPI")
        self.assertEqual(ecosystem.infer_ecosystem("delgan/loguru"), "PyPI")
        self.assertEqual(ecosystem.infer_ecosystem("farama-foundation/gymnasium"), "PyPI")
        self.assertEqual(ecosystem.infer_ecosystem("tim-osterhus/millrace"), "PyPI")

        # Go
        self.assertEqual(ecosystem.infer_ecosystem("golang/go"), "Go")
        self.assertEqual(ecosystem.infer_ecosystem("bytedance/gopkg"), "Go")

        # npm / JS / TS
        self.assertEqual(ecosystem.infer_ecosystem("expressjs/express"), "npm")
        self.assertEqual(ecosystem.infer_ecosystem("browsersync/browser-sync"), "npm")
        self.assertEqual(ecosystem.infer_ecosystem("forbeslindesay/throat"), "npm")

        # Maven
        self.assertEqual(ecosystem.infer_ecosystem("apache/jena"), "Maven")
        self.assertEqual(ecosystem.infer_ecosystem("google/gson"), "Maven")

        # RubyGems
        self.assertEqual(ecosystem.infer_ecosystem("rails/rails"), "RubyGems")

        # Fallback
        self.assertEqual(ecosystem.infer_ecosystem("unknown-org/unknown-repo-xyz"), "Other")

    def test_infer_language(self):
        # Explicit language preserved
        self.assertEqual(ecosystem.infer_language("org/repo", current_lang="Python"), "Python")
        self.assertEqual(ecosystem.infer_language("org/repo", current_lang="Rust"), "Rust")

        # Ecosystem to language
        self.assertEqual(ecosystem.infer_language("org/repo", ecosystem="Cargo"), "Rust")
        self.assertEqual(ecosystem.infer_language("org/repo", ecosystem="PyPI"), "Python")
        self.assertEqual(ecosystem.infer_language("org/repo", ecosystem="Go"), "Go")
        self.assertEqual(ecosystem.infer_language("org/repo", ecosystem="Maven"), "Java")
        self.assertEqual(ecosystem.infer_language("org/repo", ecosystem="RubyGems"), "Ruby")

        # npm TypeScript vs JavaScript
        self.assertEqual(ecosystem.infer_language("microsoft/typescript", ecosystem="npm"), "TypeScript")
        self.assertEqual(ecosystem.infer_language("org/foo-ts", ecosystem="npm"), "TypeScript")
        self.assertEqual(ecosystem.infer_language("org/express", ecosystem="npm"), "JavaScript")


class TestDependenciesDatabase(unittest.TestCase):
    def setUp(self):
        self.conn = db.get_db(":memory:")
        self.src_id = db.add_repo(self.conn, "pallets/flask", language="Python")

    def tearDown(self):
        self.conn.close()

    def test_record_dependency_unresolved_target(self):
        dep_id = db.record_dependency(
            self.conn,
            source_repo_id=self.src_id,
            target_full_name="pallets/werkzeug",
            package_name="werkzeug",
            ecosystem="PyPI",
        )
        self.assertIsInstance(dep_id, int)

        deps = db.get_dependencies(self.conn, source_repo_id=self.src_id)
        self.assertEqual(len(deps), 1)
        self.assertEqual(deps[0]["source_full_name"], "pallets/flask")
        self.assertEqual(deps[0]["target_full_name"], "pallets/werkzeug")
        self.assertEqual(deps[0]["package_name"], "werkzeug")
        self.assertEqual(deps[0]["ecosystem"], "PyPI")
        self.assertIsNone(deps[0]["target_repo_id"])

        # When target repo is added later, target_repo_id should resolve automatically
        tgt_id = db.add_repo(self.conn, "pallets/werkzeug")
        deps_after = db.get_dependencies(self.conn, source_repo_id=self.src_id)
        self.assertEqual(deps_after[0]["target_repo_id"], tgt_id)

    def test_record_dependency_unique_upsert(self):
        db.record_dependency(
            self.conn,
            source_repo_id=self.src_id,
            target_full_name="pallets/click",
            package_name="click",
            ecosystem=None,
        )
        # Duplicate with ecosystem should update without error
        db.record_dependency(
            self.conn,
            source_repo_id=self.src_id,
            target_full_name="pallets/click",
            package_name="click",
            ecosystem="PyPI",
        )
        deps = db.get_dependencies(self.conn, source_repo_id=self.src_id)
        self.assertEqual(len(deps), 1)
        self.assertEqual(deps[0]["ecosystem"], "PyPI")

    def test_record_dependencies_batch(self):
        batch = [
            {
                "source_full_name": "pallets/flask",
                "target_full_name": "pallets/jinja",
                "package_name": "jinja2",
                "ecosystem": "PyPI",
            },
            {
                "source_full_name": "pallets/flask",
                "target_full_name": "pallets/itsdangerous",
                "package_name": "itsdangerous",
                "ecosystem": "PyPI",
            },
        ]
        inserted = db.record_dependencies_batch(self.conn, batch)
        self.assertEqual(inserted, 2)
        deps = db.get_dependencies(self.conn, source_repo_id=self.src_id)
        self.assertEqual(len(deps), 2)

    def test_backfill_languages(self):
        # Add repos without language
        r1 = db.add_repo(self.conn, "tokio-rs/tokio")
        r2 = db.add_repo(self.conn, "psf/requests")
        self.conn.execute("UPDATE repos SET language = NULL WHERE id IN (?, ?)", (r1, r2))
        self.conn.commit()

        # Dry run does not commit changes
        updated, total = ecosystem.backfill_languages(self.conn, dry_run=True)
        self.assertEqual(updated, 2)
        self.assertEqual(total, 2)
        row = self.conn.execute("SELECT language FROM repos WHERE id = ?", (r1,)).fetchone()
        self.assertIsNone(row["language"])

        # Actual run updates database
        updated, total = ecosystem.backfill_languages(self.conn, dry_run=False)
        self.assertEqual(updated, 2)
        r1_lang = self.conn.execute("SELECT language FROM repos WHERE id = ?", (r1,)).fetchone()["language"]
        r2_lang = self.conn.execute("SELECT language FROM repos WHERE id = ?", (r2,)).fetchone()["language"]
        self.assertEqual(r1_lang, "Rust")
        self.assertEqual(r2_lang, "Python")


class TestGenerateEcosystemPayload(unittest.TestCase):
    def setUp(self):
        self.conn = db.get_db(":memory:")
        self.r1 = db.add_repo(self.conn, "org/repo-a", language="Python")
        self.r2 = db.add_repo(self.conn, "org/repo-b", language="Python")
        self.r3 = db.add_repo(self.conn, "org/repo-c", language="Rust")  # pure discovered node

        # repo-a is parsed complete
        db.add_tags(self.conn, self.r1, ["v1.0.0"])
        tag1 = self.conn.execute("SELECT id FROM tags WHERE repo_id = ?", (self.r1,)).fetchone()["id"]
        db.mark_complete(self.conn, tag1)

        # repo-b is in progress (dispatched)
        db.add_tags(self.conn, self.r2, ["v1.0.0"])
        tag2 = self.conn.execute("SELECT id FROM tags WHERE repo_id = ?", (self.r2,)).fetchone()["id"]
        db.mark_dispatched(self.conn, tag2, "run_100")

        # Dependency edge from repo-a to repo-b
        db.record_dependency(
            self.conn,
            source_repo_id=self.r1,
            target_full_name="org/repo-b",
            package_name="b",
            ecosystem="PyPI",
        )

    def tearDown(self):
        self.conn.close()

    def test_payload_structure(self):
        payload = ecosystem.generate_ecosystem_payload(self.conn, total_quads=500000)
        self.assertIn("meta", payload)
        self.assertIn("nodes", payload)
        self.assertIn("links", payload)

        meta = payload["meta"]
        self.assertEqual(meta["total_quads"], 500000)
        self.assertEqual(meta["active_runners"], 1)  # 1 dispatched tag
        self.assertEqual(meta["total_edges"], 1)

        # Node check: repo-a and repo-b are included; repo-c is omitted (no edges, no complete/dispatched tags)
        node_ids = {n["id"] for n in payload["nodes"]}
        self.assertIn("org/repo-a", node_ids)
        self.assertIn("org/repo-b", node_ids)
        self.assertNotIn("org/repo-c", node_ids)

        # Degree check
        node_a = next(n for n in payload["nodes"] if n["id"] == "org/repo-a")
        self.assertEqual(node_a["out_degree"], 1)
        self.assertEqual(node_a["in_degree"], 0)
        self.assertEqual(node_a["status"], "complete")

        node_b = next(n for n in payload["nodes"] if n["id"] == "org/repo-b")
        self.assertEqual(node_b["out_degree"], 0)
        self.assertEqual(node_b["in_degree"], 1)
        self.assertEqual(node_b["status"], "in_progress")

        # Link check
        self.assertEqual(len(payload["links"]), 1)
        link = payload["links"][0]
        self.assertEqual(link["source"], "org/repo-a")
        self.assertEqual(link["target"], "org/repo-b")
        self.assertEqual(link["package"], "b")
        self.assertEqual(link["ecosystem"], "PyPI")


class TestCliEcosystemCommands(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.conn = db.get_db(str(self.db_path))

        self.r1 = db.add_repo(self.conn, "demo/app", language="Python")
        self.r2 = db.add_repo(self.conn, "demo/lib", language="Python")
        db.add_tags(self.conn, self.r1, ["v1.0.0"])
        tag1 = self.conn.execute("SELECT id FROM tags WHERE repo_id = ?", (self.r1,)).fetchone()["id"]
        db.mark_complete(self.conn, tag1)

        db.record_dependency(
            self.conn,
            source_repo_id=self.r1,
            target_full_name="demo/lib",
            package_name="lib",
            ecosystem="PyPI",
        )
        self.conn.close()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_export_ecosystem_stdout(self):
        runner = CliRunner()
        result = runner.invoke(
            cli.cli,
            ["--db", str(self.db_path), "export-ecosystem", "--stdout"],
        )
        self.assertEqual(result.exit_code, 0)
        data = json.loads(result.output)
        self.assertEqual(data["meta"]["total_edges"], 1)
        self.assertEqual(len(data["nodes"]), 2)

    def test_export_ecosystem_to_file(self):
        out_file = Path(self.temp_dir.name) / "ecosystem.json"
        runner = CliRunner()
        result = runner.invoke(
            cli.cli,
            ["--db", str(self.db_path), "export-ecosystem", "-o", str(out_file)],
        )
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(out_file.exists())
        with open(out_file) as f:
            data = json.load(f)
        self.assertEqual(data["meta"]["total_edges"], 1)

    def test_backfill_languages_cli(self):
        runner = CliRunner()
        result = runner.invoke(
            cli.cli,
            ["--db", str(self.db_path), "backfill-languages"],
        )
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Successfully assigned language", result.output)

    @patch("urllib.request.urlopen")
    def test_import_oxigraph_deps_cli(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "results": {
                "bindings": [
                    {
                        "g": {"value": "https://repolex.ai/r/source/repo/dep/12345"},
                        "depOrg": {"value": "target"},
                        "depRepo": {"value": "dep"},
                        "pkg": {"value": "mypkg"},
                        "ecosystem": {"value": "PYPI"},
                    }
                ]
            }
        }).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_response

        runner = CliRunner()
        result = runner.invoke(
            cli.cli,
            ["--db", str(self.db_path), "import-oxigraph-deps"],
        )
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Successfully imported 1 dependency edges", result.output)


class TestSparqlImport(unittest.TestCase):
    @patch("urllib.request.urlopen")
    def test_import_sparql_dependencies(self, mock_urlopen):
        conn = db.get_db(":memory:")
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "results": {
                "bindings": [
                    {
                        "g": {"value": "https://repolex.ai/r/palletsprojects/flask/dep/abcdef123456"},
                        "depOrg": {"value": "palletsprojects"},
                        "depRepo": {"value": "werkzeug"},
                        "pkg": {"value": "werkzeug"},
                        "ecosystem": {"value": "PYPI"},
                    }
                ]
            }
        }).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_response

        count = ecosystem.import_sparql_dependencies(conn, sparql_url="http://mock:7878/query")
        self.assertEqual(count, 1)

        deps = db.get_dependencies(conn)
        self.assertEqual(len(deps), 1)
        self.assertEqual(deps[0]["source_full_name"], "palletsprojects/flask")
        self.assertEqual(deps[0]["target_full_name"], "palletsprojects/werkzeug")
        self.assertEqual(deps[0]["package_name"], "werkzeug")
        self.assertEqual(deps[0]["ecosystem"], "PYPI")
        conn.close()


class TestSpiderEdgeRecording(unittest.TestCase):
    def setUp(self):
        self.conn = db.get_db(":memory:")
        self.repo_id = db.add_repo(self.conn, "test-org/main-repo")

    def tearDown(self):
        self.conn.close()

    @patch("forx.spider.get_dependencies_from_dep_graph")
    def test_spider_repo_records_edge(self, mock_get_deps):
        mock_get_deps.return_value = [
            {"full_name": "test-org/dep-repo", "package": "dep-pkg", "ecosystem": "Cargo"}
        ]
        with patch("forx.discover.discover_repo") as mock_disc:
            mock_disc.return_value = ["v1.0.0"]
            from forx import spider
            added = spider.spider_repo(self.conn, "test-org/main-repo", "repolex-forx/test-org--main-repo")
            self.assertEqual(added, ["test-org/dep-repo"])

            # Verify edge was saved
            deps = db.get_dependencies(self.conn, source_repo_id=self.repo_id)
            self.assertEqual(len(deps), 1)
            self.assertEqual(deps[0]["target_full_name"], "test-org/dep-repo")
            self.assertEqual(deps[0]["package_name"], "dep-pkg")
            self.assertEqual(deps[0]["ecosystem"], "Cargo")


if __name__ == "__main__":
    unittest.main()
