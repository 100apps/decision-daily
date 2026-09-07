"""Publication recovery and evidence gates, using temporary repositories only."""
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / "scripts/run_daily.py"
spec = importlib.util.spec_from_file_location("run_daily", MODULE)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
DAY = date(2026, 9, 7)
REL = "daily/2026/09/2026-09-07.md"


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runtime = self.root / ".runtime"
        self.runtime.mkdir()
        self.patches = [patch.object(runner, "ROOT", self.root), patch.object(runner, "RUNTIME", self.runtime)]
        for p in self.patches:
            p.start()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(lambda: [p.stop() for p in reversed(self.patches)])
        self.git("init", "-b", "main")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        (self.root / ".gitignore").write_text(".runtime/\n")
        (self.root / "notes.txt").write_text("user notes\n")
        (self.root / "docs").mkdir()
        (self.root / "docs/index.html").write_text("old home")
        self.git("add", ".")
        self.git("commit", "-m", "initial")

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root, stderr=subprocess.DEVNULL).decode().strip()

    def fake_build(self, source, output):
        target = output / REL
        target.parent.mkdir(parents=True)
        target.write_bytes((source / "2026/09/2026-09-07.md").read_bytes())
        target.with_suffix(".html").write_text("new issue html")
        (output / "index.html").write_text("new home")

    def transaction(self):
        candidate = self.runtime / "issue.md"
        candidate.write_text("verified issue")
        with patch.object(runner, "build", side_effect=self.fake_build):
            return runner.stage_transaction(DAY, candidate, self.git("rev-parse", "HEAD"))

    def test_interrupted_archive_resumes_without_regenerating(self):
        stage, manifest = self.transaction()
        real_write = runner.atomic_bytes
        count = 0
        def fail_after_first(path, data):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("simulated interrupted copy")
            real_write(path, data)
        with patch.object(runner, "atomic_bytes", side_effect=fail_after_first):
            with self.assertRaises(OSError):
                runner.apply_transaction(stage, manifest)
        loaded_stage, loaded = runner.load_pending()
        runner.apply_transaction(loaded_stage, loaded)
        runner.check_transaction(loaded, applied=True)
        self.assertEqual((self.root / REL).read_text(), "verified issue")

    def test_user_edit_to_pending_file_is_preserved(self):
        stage, manifest = self.transaction()
        (self.root / "docs/index.html").write_text("user edit during recovery")
        with self.assertRaisesRegex(RuntimeError, "Concurrent edits"):
            runner.apply_transaction(stage, manifest)
        self.assertEqual((self.root / "docs/index.html").read_text(), "user edit during recovery")
        self.assertFalse((self.root / REL).exists())

    def test_unrelated_concurrent_user_change_is_not_staged(self):
        stage, manifest = self.transaction()
        (self.root / "notes.txt").write_text("new user notes")
        with self.assertRaisesRegex(RuntimeError, "Concurrent user changes"):
            runner.apply_transaction(stage, manifest)
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "")

    def test_head_change_during_build_stops_before_archiving(self):
        candidate = self.runtime / "issue.md"
        candidate.write_text("candidate")
        initial_head = self.git("rev-parse", "HEAD")
        def changing_build(source, output):
            self.fake_build(source, output)
            (self.root / "notes.txt").write_text("new committed user notes")
            self.git("add", "notes.txt")
            self.git("commit", "-m", "user change")
        with patch.object(runner, "build", side_effect=changing_build):
            with self.assertRaisesRegex(RuntimeError, "HEAD changed"):
                runner.stage_transaction(DAY, candidate, initial_head)
        self.assertFalse((self.root / REL).exists())
        self.assertFalse((self.runtime / "pending-publication.json").exists())

    def test_push_failure_reuses_commit_and_verifies_all_pages(self):
        stage, manifest = self.transaction()
        runner.apply_transaction(stage, manifest)
        real_git = runner.git
        def failed_push(*args):
            if args[0] == "push":
                raise RuntimeError("temporary network failure")
            return real_git(*args)
        with patch.object(runner, "git", side_effect=failed_push):
            with self.assertRaisesRegex(RuntimeError, "temporary network"):
                runner.publish(stage, manifest)
        committed_head = self.git("rev-parse", "HEAD")
        loaded_stage, loaded = runner.load_pending()
        runner.apply_transaction(loaded_stage, loaded)
        def successful_push(*args):
            return "" if args[0] == "push" else real_git(*args)
        with patch.object(runner, "git", side_effect=successful_push), patch.object(runner, "verify_pages", return_value="https://example.invalid/day.html") as verify:
            runner.publish(loaded_stage, loaded)
            verify.assert_called_once_with(DAY, stage / "docs")
        self.assertEqual(committed_head, self.git("rev-parse", "HEAD"))
        self.assertFalse((self.runtime / "pending-publication.json").exists())

    def test_commit_succeeded_before_manifest_save_is_recovered(self):
        stage, manifest = self.transaction()
        runner.apply_transaction(stage, manifest)
        self.git("add", "--", *manifest["files"])
        self.git("commit", "-m", "publication", "-m", "Decision-Daily-Transaction: " + manifest["id"])
        runner.apply_transaction(stage, manifest)
        self.assertEqual(manifest["commit"], self.git("rev-parse", "HEAD"))

    def test_staged_user_change_is_preserved_and_rejected(self):
        stage, manifest = self.transaction()
        path = self.root / "docs/index.html"
        path.write_text("user staged content")
        self.git("add", "docs/index.html")
        path.write_text("old home")
        with self.assertRaisesRegex(RuntimeError, "Concurrent staged"):
            runner.apply_transaction(stage, manifest)
        self.assertEqual(self.git("show", ":docs/index.html"), "user staged content")

    def test_same_day_existing_issue_has_no_new_commit(self):
        stage, manifest = self.transaction()
        runner.apply_transaction(stage, manifest)
        self.git("add", "--", *manifest["files"])
        self.git("commit", "-m", "published issue")
        (self.runtime / "pending-publication.json").unlink()
        head = self.git("rev-parse", "HEAD")
        with patch.object(runner, "build", side_effect=self.fake_build):
            second_stage, second = runner.stage_transaction(DAY, None, head)
        self.assertEqual(second["files"], {})
        runner.apply_transaction(second_stage, second)
        real_git = runner.git
        with patch.object(runner, "git", side_effect=lambda *args: "" if args[0] == "push" else real_git(*args)), patch.object(runner, "verify_pages", return_value="https://example.invalid/day.html"):
            runner.publish(second_stage, second)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertFalse((self.runtime / "pending-publication.json").exists())

    def test_markdown_match_without_updated_html_does_not_publish(self):
        stage, manifest = self.transaction()
        requested = []
        def open_url(request, **kwargs):
            relative = request.full_url.removeprefix(runner.CONFIG["base_url"] + "/")
            requested.append(relative)
            body = (stage / "docs" / relative).read_bytes()
            return io.BytesIO(b"stale" if relative.endswith(".html") else body)
        with patch("urllib.request.urlopen", side_effect=open_url):
            with self.assertRaisesRegex(RuntimeError, "reading page or homepage"):
                runner.verify_pages(DAY, stage / "docs", attempts=1, delay=0)
        self.assertEqual(len(requested), 3)

    def events(self, actions, completion=True):
        path = self.runtime / "events.jsonl"
        events = [{"type": "item.completed", "item": {"type": "web_search", "action": {"type": action}}} for action in actions]
        if completion:
            events.append({"type": "turn.completed"})
        path.write_text("\n".join(map(json.dumps, events)) + "\n")
        return path

    def test_no_search_or_no_page_action_is_rejected(self):
        for actions in ([], ["search"], ["open_page"]):
            with self.subTest(actions=actions), self.assertRaisesRegex(RuntimeError, "Research requires"):
                runner.verify_research_events(self.events(actions))
        result = runner.verify_research_events(self.events(["search", "other"]))
        self.assertEqual(result["opaque_non_search"], 1)
        self.assertIn("不能证明", result["limitation"])

    def test_generator_clears_only_inherited_session_identifiers(self):
        def fake_command(args, **kwargs):
            self.assertNotIn("CODEX_THREAD_ID", kwargs["env"])
            self.assertNotIn("CODEX_SESSION_ID", kwargs["env"])
            self.assertEqual(kwargs["env"]["KEEP_THIS_TEST_VALUE"], "kept")
            work = kwargs["cwd"]
            (work / "issue.md").write_text("candidate")
            for action in ("search", "open_page"):
                kwargs["stdout"].write(json.dumps({"type": "item.completed", "item": {"type": "web_search", "action": {"type": action}}}) + "\n")
            kwargs["stdout"].write('{"type":"turn.completed"}\n')
        def prepare(work, day):
            work.mkdir()
            return "prompt"
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "parent", "CODEX_SESSION_ID": "parent", "KEEP_THIS_TEST_VALUE": "kept"}), patch.object(runner, "prepare_workspace", side_effect=prepare), patch.object(runner, "command", side_effect=fake_command):
            self.assertTrue(runner.generate(self.runtime / "work", DAY).exists())


if __name__ == "__main__":
    unittest.main()
