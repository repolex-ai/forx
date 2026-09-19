import unittest
from unittest.mock import MagicMock, patch

from forx.orchestrate import fill_slots


class TestFillSlotsAstGate(unittest.TestCase):
    @patch("forx.db.mark_dispatched")
    @patch("forx.dispatch.dispatch_workflow", return_value="run_123")
    @patch("forx.dispatch.check_ast_chunks_exist")
    @patch("forx.db.get_pending_tags")
    def test_enrich_falls_back_to_ast_when_chunks_missing(
        self,
        mock_get_pending,
        mock_check_ast,
        mock_dispatch,
        mock_mark_dispatched,
    ):
        conn = MagicMock()
        mock_get_pending.return_value = [
            {
                "id": 10,
                "full_name": "org/repo",
                "storage_repo": "repolex-forx/org--repo",
                "git_tag": "v1.0.0",
                "commit_sha": "sha12345678",
                "next_action": "enrich",
                "iteration_count": 1,
            }
        ]
        # AST chunks are missing in storage
        mock_check_ast.return_value = False

        dispatched = fill_slots(conn, max_concurrent=1)
        self.assertEqual(dispatched, 1)

        mock_check_ast.assert_called_once_with("repolex-forx/org--repo", "sha12345678")
        # Should have fallen back to phase='ast'
        mock_dispatch.assert_called_once_with(
            repo="org/repo",
            tag="v1.0.0",
            storage_repo="repolex-forx/org--repo",
            phase="ast",
        )
        mock_mark_dispatched.assert_called_once_with(conn, 10, "run_123", phase="ast")

    @patch("forx.db.mark_dispatched")
    @patch("forx.dispatch.dispatch_workflow", return_value="run_124")
    @patch("forx.dispatch.check_ast_chunks_exist")
    @patch("forx.db.get_pending_tags")
    def test_enrich_proceeds_when_chunks_exist(
        self,
        mock_get_pending,
        mock_check_ast,
        mock_dispatch,
        mock_mark_dispatched,
    ):
        conn = MagicMock()
        mock_get_pending.return_value = [
            {
                "id": 11,
                "full_name": "org/repo",
                "storage_repo": "repolex-forx/org--repo",
                "git_tag": "v1.0.0",
                "commit_sha": "sha12345678",
                "next_action": "enrich",
                "iteration_count": 1,
            }
        ]
        # AST chunks exist
        mock_check_ast.return_value = True

        dispatched = fill_slots(conn, max_concurrent=1)
        self.assertEqual(dispatched, 1)

        mock_check_ast.assert_called_once_with("repolex-forx/org--repo", "sha12345678")
        # Should proceed with phase='enrich'
        mock_dispatch.assert_called_once_with(
            repo="org/repo",
            tag="v1.0.0",
            storage_repo="repolex-forx/org--repo",
            phase="enrich",
        )
        mock_mark_dispatched.assert_called_once_with(conn, 11, "run_124", phase="enrich")


if __name__ == "__main__":
    unittest.main()
