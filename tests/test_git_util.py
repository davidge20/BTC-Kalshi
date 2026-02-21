import os
import tempfile
import unittest
from unittest.mock import patch

from kalshi_edge.util.git import best_effort_git_commit, find_repo_root


class TestGitHelpers(unittest.TestCase):
    def test_find_repo_root_walks_up(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = os.path.join(td, "repo")
            nested = os.path.join(repo, "a", "b", "c")
            os.makedirs(nested, exist_ok=True)
            os.makedirs(os.path.join(repo, ".git"), exist_ok=True)

            root = find_repo_root(nested)
            self.assertEqual(root, repo)

    def test_best_effort_git_commit_returns_hash_when_git_works(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = os.path.join(td, "repo")
            nested = os.path.join(repo, "subdir")
            os.makedirs(nested, exist_ok=True)
            os.makedirs(os.path.join(repo, ".git"), exist_ok=True)

            with patch("subprocess.check_output", return_value="deadbeef\n") as p:
                got = best_effort_git_commit(start_paths=[nested])
                self.assertEqual(got, "deadbeef")
                self.assertTrue(p.called)
                _args, kwargs = p.call_args
                self.assertEqual(kwargs.get("cwd"), repo)

    def test_best_effort_git_commit_none_without_repo(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            got = best_effort_git_commit(start_paths=[td])
            self.assertIsNone(got)


if __name__ == "__main__":
    unittest.main()

