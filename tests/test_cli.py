import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from forx import db
from forx.cli import cli


class TestCliAddOrg(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.conn = db.get_db(self.db_path)
        self.runner = CliRunner()

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    @patch("forx.discover.list_org_repos")
    @patch("forx.discover.discover_repo")
    def test_add_org_default_priority_and_tags(self, mock_discover_repo, mock_list_org_repos):
        mock_list_org_repos.return_value = [
            {"full_name": "test-org/tagged-repo", "language": "Python", "stars": 15, "default_branch": "main"}
        ]
        mock_discover_repo.return_value = ["v1.0.0", "v0.9.0"]

        result = self.runner.invoke(cli, ["--db", str(self.db_path), "add-org", "test-org"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("2 tags (priority=500)", result.output)

        row = self.conn.execute("SELECT priority, head_only FROM repos WHERE full_name = 'test-org/tagged-repo'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["priority"], 500)
        self.assertEqual(row["head_only"], 0)

        tags = self.conn.execute("SELECT git_tag, status FROM tags WHERE repo_id = (SELECT id FROM repos WHERE full_name = 'test-org/tagged-repo')").fetchall()
        self.assertEqual(len(tags), 2)
        self.assertEqual([t["git_tag"] for t in tags], ["v1.0.0", "v0.9.0"])

    @patch("forx.discover.list_org_repos")
    @patch("forx.discover.discover_repo")
    def test_add_org_tagless_head_fallback(self, mock_discover_repo, mock_list_org_repos):
        mock_list_org_repos.return_value = [
            {"full_name": "asimov-platform/asimov-cli", "language": "Rust", "stars": 42, "default_branch": "master"}
        ]
        mock_discover_repo.return_value = []  # No tags

        result = self.runner.invoke(cli, ["--db", str(self.db_path), "add-org", "asimov-platform", "--priority", "800"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("added HEAD (master) priority=800", result.output)

        row = self.conn.execute("SELECT priority, head_only FROM repos WHERE full_name = 'asimov-platform/asimov-cli'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["priority"], 800)
        self.assertEqual(row["head_only"], 1)

        tags = self.conn.execute("SELECT git_tag, status FROM tags WHERE repo_id = (SELECT id FROM repos WHERE full_name = 'asimov-platform/asimov-cli')").fetchall()
        self.assertEqual(len(tags), 1)
        self.assertEqual(tags[0]["git_tag"], "master")
        self.assertEqual(tags[0]["status"], "pending")

    @patch("forx.discover.list_org_repos")
    @patch("forx.discover.discover_repo")
    def test_add_org_update_priority_existing_repo(self, mock_discover_repo, mock_list_org_repos):
        # Insert repo previously at priority 0
        repo_id = db.add_repo(self.conn, "existing-org/repo", priority=0)
        db.add_tags(self.conn, repo_id, ["v1.0.0"])

        mock_list_org_repos.return_value = [
            {"full_name": "existing-org/repo", "language": "Python", "stars": 5, "default_branch": "main"}
        ]

        result = self.runner.invoke(cli, ["--db", str(self.db_path), "add-org", "existing-org", "-p", "800", "--update-priority"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("updated priority 0 -> 800", result.output)

        row = self.conn.execute("SELECT priority FROM repos WHERE id = ?", (repo_id,)).fetchone()
        self.assertEqual(row["priority"], 800)

    @patch("forx.discover.list_org_repos")
    @patch("forx.discover.discover_repo")
    def test_add_org_rescues_stranded_tagless_repo(self, mock_discover_repo, mock_list_org_repos):
        # Simulate a repo previously added with 0 tags (stranded)
        repo_id = db.add_repo(self.conn, "stranded-org/tagless", priority=0)

        mock_list_org_repos.return_value = [
            {"full_name": "stranded-org/tagless", "language": "Python", "stars": 3, "default_branch": "main"}
        ]

        result = self.runner.invoke(cli, ["--db", str(self.db_path), "add-org", "stranded-org", "-p", "800"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("rescued tagless HEAD (main) priority=800", result.output)

        row = self.conn.execute("SELECT priority, head_only FROM repos WHERE id = ?", (repo_id,)).fetchone()
        self.assertEqual(row["priority"], 800)
        self.assertEqual(row["head_only"], 1)

        tags = self.conn.execute("SELECT git_tag, status FROM tags WHERE repo_id = ?", (repo_id,)).fetchall()
        self.assertEqual(len(tags), 1)
        self.assertEqual(tags[0]["git_tag"], "main")
        self.assertEqual(tags[0]["status"], "pending")

    def test_priority_hierarchy_in_get_pending_tags(self):
        # 1. Background spider repo @ priority 0
        r0 = db.add_repo(self.conn, "spider/lib-a", priority=0)
        db.add_tags(self.conn, r0, ["v0.1.0"])

        # 2. Asimov repo @ priority 800 (tagless head fallback)
        r800 = db.add_repo(self.conn, "asimov-platform/asimov-sdk", head_only=True, priority=800)
        db.add_tags(self.conn, r800, ["master"])

        # 3. Hermes direct dep @ priority 1000
        r1000 = db.add_repo(self.conn, "hermes/direct-dep", priority=1000)
        db.add_tags(self.conn, r1000, ["v2.0.0"])

        # 4. Repolex core @ priority 2000
        r2000 = db.add_repo(self.conn, "repolex-ai/core", priority=2000)
        db.add_tags(self.conn, r2000, ["main"])

        pending = db.get_pending_tags(self.conn, limit=10)
        repo_names = [p["full_name"] for p in pending]

        # Verify ordering strictly follows priority DESC: 2000 -> 1000 -> 800 -> 0
        self.assertEqual(repo_names, [
            "repolex-ai/core",
            "hermes/direct-dep",
            "asimov-platform/asimov-sdk",
            "spider/lib-a",
        ])


if __name__ == "__main__":
    unittest.main()
