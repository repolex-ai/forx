import unittest
from unittest.mock import MagicMock, patch

from forx.dispatch import check_ast_chunks_exist, check_filetree_exists, check_manifest_for_tag


class TestCheckManifestForTag(unittest.TestCase):
    @patch("forx.dispatch.fetch_json")
    def test_direct_tag_match_in_repo_manifest(self, mock_fetch):
        mock_fetch.return_value = {
            "@id": "https://repolex.ai/r/org/repo",
            "repolex:trackedCommit": [
                {
                    "git:tagName": "v1.0.0",
                    "git:hexsha": "sha111",
                    "repolex:parseStatus": "parsed",
                }
            ],
        }
        res = check_manifest_for_tag("repolex-forx/org--repo", "v1.0.0", commit_sha="sha111")
        self.assertEqual(res, "success")

    @patch("forx.dispatch.fetch_json")
    def test_commit_sha_aliasing_match_in_repo_manifest(self, mock_fetch):
        # fireworks case: tag v2.0.3 points to sha111, but repo-manifest recorded v2.0.2 with sha111
        mock_fetch.return_value = {
            "@id": "https://repolex.ai/r/org/repo",
            "repolex:trackedCommit": [
                {
                    "git:tagName": "v2.0.2",
                    "git:hexsha": "sha111",
                    "repolex:parseStatus": "parsed",
                }
            ],
        }
        # Checking tag v2.0.3 with commit_sha sha111 should succeed via sha match
        res = check_manifest_for_tag("repolex-forx/org--repo", "v2.0.3", commit_sha="sha111")
        self.assertEqual(res, "success")

    @patch("forx.dispatch.fetch_json")
    def test_single_tracked_commit_dict(self, mock_fetch):
        mock_fetch.return_value = {
            "@id": "https://repolex.ai/r/org/repo",
            "repolex:trackedCommit": {
                "git:tagName": "v1.0.0",
                "git:hexsha": "sha111",
                "repolex:parseStatus": "parsed",
            },
        }
        res = check_manifest_for_tag("repolex-forx/org--repo", "v1.0.0")
        self.assertEqual(res, "success")

    @patch("forx.dispatch.fetch_json")
    def test_fallback_to_individual_commit_manifest(self, mock_fetch):
        def fake_fetch(storage_repo, path):
            if path == "repo-manifest.jsonld":
                return {
                    "@id": "https://repolex.ai/r/org/repo",
                    "repolex:trackedCommit": [],
                }
            if path == "manifests/commit-manifest-sha222.jsonld":
                return {
                    "git:hexsha": "sha222",
                    "repolex:parseStatus": "parsed",
                }
            return None

        mock_fetch.side_effect = fake_fetch
        res = check_manifest_for_tag("repolex-forx/org--repo", "v2.0.0", commit_sha="sha222")
        self.assertEqual(res, "success")

    @patch("forx.dispatch.fetch_json")
    def test_fallback_to_legacy_manifest(self, mock_fetch):
        def fake_fetch(storage_repo, path):
            if path == "manifest.json":
                return {
                    "versions": [
                        {"tag": "v1.0.0", "sha": "sha333"},
                    ]
                }
            return None

        mock_fetch.side_effect = fake_fetch
        res = check_manifest_for_tag("repolex-forx/org--repo", "v1.0.0")
        self.assertEqual(res, "success")

        # Also verify sha match on legacy manifest
        res_sha = check_manifest_for_tag("repolex-forx/org--repo", "v1.0.1", commit_sha="sha333")
        self.assertEqual(res_sha, "success")

    @patch("forx.dispatch.fetch_json", return_value=None)
    def test_missing_manifest_returns_none(self, mock_fetch):
        res = check_manifest_for_tag("repolex-forx/org--repo", "v1.0.0", commit_sha="sha444")
        self.assertIsNone(res)


class TestCheckAstChunksExist(unittest.TestCase):
    @patch("forx.dispatch.gh_run")
    def test_ast_chunks_exist_via_gh_api(self, mock_gh):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = '[{"name": "chunk-001.nq.gz", "type": "file"}]'
        mock_gh.return_value = mock_proc

        self.assertTrue(check_ast_chunks_exist("repolex-forx/org--repo", "sha12345"))

    @patch("urllib.request.urlopen")
    @patch("forx.dispatch.gh_run")
    def test_ast_chunks_exist_via_http_head_fallback(self, mock_gh, mock_urlopen):
        # gh api fails
        mock_gh.side_effect = Exception("gh not available")

        # http head succeeds
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        self.assertTrue(check_ast_chunks_exist("repolex-forx/org--repo", "sha12345"))

    @patch("urllib.request.urlopen")
    @patch("forx.dispatch.gh_run")
    def test_ast_chunks_missing_returns_false(self, mock_gh, mock_urlopen):
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stdout = ""
        mock_gh.return_value = mock_proc

        mock_urlopen.side_effect = Exception("404 Not Found")

        self.assertFalse(check_ast_chunks_exist("repolex-forx/org--repo", "sha12345"))

    def test_empty_commit_sha_returns_false(self):
        self.assertFalse(check_ast_chunks_exist("repolex-forx/org--repo", ""))


class TestCheckFiletreeExists(unittest.TestCase):
    @patch("forx.dispatch.gh_run")
    def test_filetree_exists_via_gh_api(self, mock_gh):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = '{"name": "sha12345.nq.gz", "type": "file"}'
        mock_gh.return_value = mock_proc

        self.assertTrue(check_filetree_exists("repolex-forx/org--repo", "sha12345"))

    @patch("urllib.request.urlopen")
    @patch("forx.dispatch.gh_run")
    def test_filetree_exists_via_http_head_fallback(self, mock_gh, mock_urlopen):
        mock_gh.side_effect = Exception("gh not available")

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        self.assertTrue(check_filetree_exists("repolex-forx/org--repo", "sha12345"))

    @patch("urllib.request.urlopen")
    @patch("forx.dispatch.gh_run")
    def test_filetree_missing_returns_false(self, mock_gh, mock_urlopen):
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stdout = ""
        mock_gh.return_value = mock_proc

        mock_urlopen.side_effect = Exception("404 Not Found")

        self.assertFalse(check_filetree_exists("repolex-forx/org--repo", "sha12345"))

    def test_empty_commit_sha_returns_false(self):
        self.assertFalse(check_filetree_exists("repolex-forx/org--repo", ""))


if __name__ == "__main__":
    unittest.main()
