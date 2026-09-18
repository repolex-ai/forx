import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from forx.index import sync_repo
from forx.orchestrate import check_running


class TestSyncRepo(unittest.TestCase):
    def test_sync_repo_lifecycle_statuses(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            index_path = Path(tmpdir)
            full_name = "test-org/test-repo"
            storage_repo = "repolex-forx/test-org--test-repo"

            repo_manifest = {
                "@id": "https://repolex.ai/r/test-org/test-repo",
                "repolex:trackedCommit": [
                    {
                        "git:hexsha": "sha_meta",
                        "repolex:parseStatus": "metadata_complete",
                    },
                    {
                        "git:hexsha": "sha_ast",
                        "repolex:parseStatus": "ast_complete",
                    },
                    {
                        "git:hexsha": "sha_parsed",
                        "repolex:parseStatus": "parsed",
                    },
                ],
            }

            def fake_fetch_json(s_repo, path):
                if path == "repo-manifest.jsonld":
                    return repo_manifest
                if path == "manifests/commit-manifest-sha_ast.jsonld":
                    return {"git:hexsha": "sha_ast", "repolex:parseStatus": "ast_complete"}
                if path == "manifests/commit-manifest-sha_parsed.jsonld":
                    return {"git:hexsha": "sha_parsed", "repolex:parseStatus": "parsed"}
                return None

            with patch("forx.index.fetch_json", side_effect=fake_fetch_json):
                updated = sync_repo(full_name, storage_repo, index_path)
                self.assertTrue(updated)

            repo_dir = index_path / "repos" / "test-org" / "test-repo"
            self.assertTrue((repo_dir / "repo-manifest.jsonld").exists())

            commits_dir = repo_dir / "commits"
            # metadata_complete should NOT have a commit manifest pulled
            self.assertFalse((commits_dir / "commit-manifest-sha_meta.jsonld").exists())
            # ast_complete and parsed MUST have commit manifests pulled
            self.assertTrue((commits_dir / "commit-manifest-sha_ast.jsonld").exists())
            self.assertTrue((commits_dir / "commit-manifest-sha_parsed.jsonld").exists())


class TestOrchestrateReconcilerSync(unittest.TestCase):
    @patch("forx.index.push_index")
    @patch("forx.index.sync_repo", return_value=True)
    @patch("forx.index.get_index_path")
    @patch("forx.dispatch.check_workflow_run_status", return_value=("completed", "success"))
    @patch("forx.db.get_dispatched_tags")
    @patch("forx.db.update_next_action")
    @patch("forx.db.reset_to_pending")
    def test_mid_pipeline_advancement_triggers_sync(
        self,
        mock_reset,
        mock_update_next,
        mock_get_tags,
        mock_run_status,
        mock_get_index_path,
        mock_sync_repo,
        mock_push_index,
    ):
        conn = MagicMock()
        mock_get_tags.return_value = [
            {
                "id": 1,
                "full_name": "test-org/test-repo",
                "storage_repo": "repolex-forx/test-org--test-repo",
                "git_tag": "v1.0.0",
                "workflow_run_id": "12345",
                "current_phase": "ast",
                "dispatched_at": "2026-09-18T00:00:00+00:00",
            }
        ]

        with patch("forx.dispatch.read_next_action", return_value={
            "next_action": "enrich",
            "phase_completed": "ast",
        }):
            completed, failed = check_running(conn)

            self.assertEqual(completed, 0)
            self.assertEqual(failed, 0)
            mock_update_next.assert_called_once_with(conn, 1, "enrich")
            mock_reset.assert_called_once_with(conn, 1)
            mock_sync_repo.assert_called_once()
            mock_push_index.assert_called_once()

    @patch("forx.index.update_profile_readme")
    @patch("forx.index.push_index")
    @patch("forx.index.sync_repo", return_value=True)
    @patch("forx.index.get_index_path")
    @patch("forx.dispatch.check_workflow_run_status", return_value=("completed", "success"))
    @patch("forx.dispatch.check_manifest_for_tag", return_value="success")
    @patch("forx.db.get_dispatched_tags")
    @patch("forx.db.mark_complete")
    def test_terminal_action_triggers_sync_and_readme(
        self,
        mock_mark_complete,
        mock_get_tags,
        mock_check_manifest,
        mock_run_status,
        mock_get_index_path,
        mock_sync_repo,
        mock_push_index,
        mock_update_readme,
    ):
        conn = MagicMock()
        mock_get_tags.return_value = [
            {
                "id": 2,
                "full_name": "test-org/test-repo",
                "storage_repo": "repolex-forx/test-org--test-repo",
                "git_tag": "v1.0.0",
                "workflow_run_id": "12345",
                "current_phase": "combine",
                "dispatched_at": "2026-09-18T00:00:00+00:00",
            }
        ]

        with patch("forx.dispatch.read_next_action", return_value={
            "next_action": "done",
            "phase_completed": "combine",
        }):
            completed, failed = check_running(conn)

            self.assertEqual(completed, 1)
            self.assertEqual(failed, 0)
            mock_mark_complete.assert_called_once_with(conn, 2)
            mock_sync_repo.assert_called_once()
            mock_push_index.assert_called_once()
            mock_update_readme.assert_called_once()


if __name__ == "__main__":
    unittest.main()
