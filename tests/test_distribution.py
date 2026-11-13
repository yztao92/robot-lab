"""Exercise updates against real local Git remotes without touching the live service."""
import contextlib
import io
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import preflight
import update


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="robotlab-update-")
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.remote, self.author, self.client = [base / name for name in ("remote.git", "author", "client")]
        self.remote.mkdir()
        git(self.remote, "init", "--bare", "--initial-branch=main")
        self.author.mkdir()
        git(self.author, "init", "--initial-branch=main")
        git(self.author, "config", "user.name", "Test")
        git(self.author, "config", "user.email", "test@example.invalid")
        (self.author / "scripts").mkdir()
        (self.author / ".gitignore").write_text("restart-marker\n")
        (self.author / "version").write_text("1")
        (self.author / "scripts/preflight.py").write_text("raise SystemExit(0)\n")
        (self.author / "scripts/local_service.py").write_text(
            "from pathlib import Path\nimport sys\nassert sys.argv[1] == 'restart'\n"
            "Path(__file__).resolve().parents[1].joinpath('restart-marker').write_text('restarted')\n")
        git(self.author, "add", ".")
        git(self.author, "commit", "-m", "Initial")
        git(self.author, "remote", "add", "origin", str(self.remote))
        git(self.author, "push", "-u", "origin", "main")
        git(base, "clone", str(self.remote), str(self.client))

    def publish(self, path="version", text="2"):
        (self.author / path).write_text(text)
        git(self.author, "add", ".")
        git(self.author, "commit", "-m", "Update")
        git(self.author, "push")

    def test_fast_forward_runs_updated_validator_then_restarts(self):
        self.publish()
        update.update(self.client)
        self.assertEqual((self.client / "version").read_text(), "2")
        self.assertTrue((self.client / "restart-marker").exists())

    def test_manual_pull_still_restarts_when_already_up_to_date(self):
        self.publish()
        git(self.client, "pull", "--ff-only")
        update.update(self.client)
        self.assertTrue((self.client / "restart-marker").exists())

    def test_local_edits_are_preserved_without_pulling_or_restarting(self):
        self.publish()
        (self.client / "version").write_text("my edits")
        with self.assertRaisesRegex(RuntimeError, "本地改动"):
            update.update(self.client)
        self.assertEqual((self.client / "version").read_text(), "my edits")
        self.assertFalse((self.client / "restart-marker").exists())

    def test_untracked_files_are_preserved(self):
        (self.client / "notes.txt").write_text("personal")
        with self.assertRaisesRegex(RuntimeError, "本地改动"):
            update.update(self.client)
        self.assertEqual((self.client / "notes.txt").read_text(), "personal")

    def test_unreachable_remote_does_not_restart(self):
        git(self.client, "remote", "set-url", "origin", str(self.remote / "missing"))
        with self.assertRaises(subprocess.CalledProcessError):
            update.update(self.client)
        self.assertFalse((self.client / "restart-marker").exists())

    def test_diverged_history_does_not_merge_or_restart(self):
        self.publish()
        git(self.client, "config", "user.name", "Test")
        git(self.client, "config", "user.email", "test@example.invalid")
        (self.client / "version").write_text("local commit")
        git(self.client, "commit", "-am", "Local")
        before = git(self.client, "rev-parse", "HEAD")
        with self.assertRaises(subprocess.CalledProcessError):
            update.update(self.client)
        self.assertEqual(git(self.client, "rev-parse", "HEAD"), before)
        self.assertFalse((self.client / "restart-marker").exists())

    def test_updated_validator_failure_does_not_restart(self):
        self.publish("scripts/preflight.py", "raise SystemExit(1)\n")
        with self.assertRaises(subprocess.CalledProcessError):
            update.update(self.client)
        self.assertFalse((self.client / "restart-marker").exists())


class PreflightTests(unittest.TestCase):
    def test_checkout_has_all_media_without_parent_directory(self):
        with tempfile.TemporaryDirectory(prefix="robotlab-assets-") as folder:
            checkout = Path(folder) / "standalone"
            shutil.copytree(ROOT, checkout, ignore=shutil.ignore_patterns(".git", "output", "__pycache__"))
            self.assertEqual(preflight.check(checkout), 62)
            next((checkout / "media").glob("*.mp4")).unlink()
            with self.assertRaisesRegex(ValueError, "表情视频缺失"):
                preflight.check(checkout)

    @unittest.skipUnless(sys.platform == "darwin", "launchd entry is macOS-only")
    def test_restart_validation_failure_does_not_stop_live_service(self):
        import local_service
        with (mock.patch.object(sys, "argv", ["local_service.py", "restart"]),
              mock.patch.object(local_service, "check", side_effect=ValueError("资源缺失")),
              mock.patch.object(local_service, "stop") as stop,
              contextlib.redirect_stderr(io.StringIO())):
            self.assertEqual(local_service.main(), 1)
        stop.assert_not_called()


if __name__ == "__main__":
    unittest.main()
