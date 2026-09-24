import unittest

from forx import db


class TestDbCommitShaTracking(unittest.TestCase):
    def setUp(self):
        self.conn = db.get_db(":memory:")
        self.repo_id = db.add_repo(self.conn, "test-org/test-repo")
        db.add_tags(self.conn, self.repo_id, ["v1.0.0", "v2.0.0"])

    def tearDown(self):
        self.conn.close()

    def test_update_next_action_saves_commit_sha(self):
        tag = self.conn.execute("SELECT id, commit_sha FROM tags WHERE git_tag = 'v1.0.0'").fetchone()
        tag_id = tag["id"]

        # Initially commit_sha is NULL
        self.assertIsNone(tag["commit_sha"])

        # Record next_action and commit_sha
        db.update_next_action(self.conn, tag_id, "enrich", commit_sha="abcdef123456")

        row = self.conn.execute(
            "SELECT next_action, commit_sha FROM tags WHERE id = ?", (tag_id,)
        ).fetchone()
        self.assertEqual(row["next_action"], "enrich")
        self.assertEqual(row["commit_sha"], "abcdef123456")

    def test_mark_complete_records_commit_sha(self):
        tag = self.conn.execute("SELECT id FROM tags WHERE git_tag = 'v2.0.0'").fetchone()
        tag_id = tag["id"]

        db.mark_complete(self.conn, tag_id, commit_sha="9876543210fe")

        row = self.conn.execute(
            "SELECT status, commit_sha FROM tags WHERE id = ?", (tag_id,)
        ).fetchone()
        self.assertEqual(row["status"], "complete")
        self.assertEqual(row["commit_sha"], "9876543210fe")

    def test_get_dispatched_tags_includes_commit_sha(self):
        tag = self.conn.execute("SELECT id FROM tags WHERE git_tag = 'v1.0.0'").fetchone()
        tag_id = tag["id"]

        db.mark_dispatched(self.conn, tag_id, "run_999", phase="ast")
        db.update_next_action(self.conn, tag_id, "enrich", commit_sha="sha_dispatched_1")

        dispatched = db.get_dispatched_tags(self.conn)
        self.assertEqual(len(dispatched), 1)
        self.assertEqual(dispatched[0]["commit_sha"], "sha_dispatched_1")

    def test_add_repo_priority(self):
        repo_id = db.add_repo(self.conn, "custom-org/high-priority", priority=800)
        row = self.conn.execute("SELECT priority, head_only FROM repos WHERE id = ?", (repo_id,)).fetchone()
        self.assertEqual(row["priority"], 800)
        self.assertEqual(row["head_only"], 0)

        head_repo_id = db.add_repo(self.conn, "custom-org/head-repo", head_only=True, priority=500)
        head_row = self.conn.execute("SELECT priority, head_only FROM repos WHERE id = ?", (head_repo_id,)).fetchone()
        self.assertEqual(head_row["priority"], 500)
        self.assertEqual(head_row["head_only"], 1)


if __name__ == "__main__":
    unittest.main()
