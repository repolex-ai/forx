import json
import subprocess
import unittest
from unittest.mock import patch, MagicMock

from forx import discover


class TestDiscover(unittest.TestCase):
    @patch("subprocess.run")
    def test_get_default_branch_gh_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="master\n", stderr="")
        branch = discover.get_default_branch("test-org/test-repo")
        self.assertEqual(branch, "master")

    @patch("subprocess.run")
    def test_get_default_branch_git_ls_remote_fallback(self, mock_run):
        # First call (gh) fails, second call (git ls-remote) succeeds
        mock_gh = MagicMock(returncode=1, stdout="", stderr="error")
        mock_git = MagicMock(
            returncode=0,
            stdout="ref: refs/heads/develop\tHEAD\nabcdef123456\tHEAD\n",
            stderr="",
        )
        mock_run.side_effect = [mock_gh, mock_git]

        branch = discover.get_default_branch("test-org/test-repo")
        self.assertEqual(branch, "develop")

    @patch("subprocess.run")
    def test_get_default_branch_default_main_on_error(self, mock_run):
        mock_run.side_effect = Exception("network down")
        branch = discover.get_default_branch("test-org/test-repo")
        self.assertEqual(branch, "main")

    @patch("subprocess.run")
    def test_list_org_repos_includes_default_branch(self, mock_run):
        payload = [
            json.dumps({"full_name": "org/repo1", "language": "Python", "stars": 10, "fork": False, "archived": False, "default_branch": "master"}),
            json.dumps({"full_name": "org/repo2", "language": "Rust", "stars": 5, "fork": False, "archived": False, "default_branch": "main"}),
            json.dumps({"full_name": "org/repo-archived", "language": "Python", "stars": 2, "fork": False, "archived": True, "default_branch": "main"}),
            json.dumps({"full_name": "org/repo-fork", "language": "Go", "stars": 1, "fork": True, "archived": False, "default_branch": "main"}),
        ]
        mock_run.return_value = MagicMock(returncode=0, stdout="\n".join(payload), stderr="")

        repos = discover.list_org_repos("org", include_forks=False)
        self.assertEqual(len(repos), 2)
        self.assertEqual(repos[0]["full_name"], "org/repo1")
        self.assertEqual(repos[0]["default_branch"], "master")
        self.assertEqual(repos[1]["full_name"], "org/repo2")
        self.assertEqual(repos[1]["default_branch"], "main")


if __name__ == "__main__":
    unittest.main()
