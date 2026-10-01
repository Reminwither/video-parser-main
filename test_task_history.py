"""Private task history and restart handling, using isolated SQLite only."""
import ast
import hashlib
import json
from html.parser import HTMLParser
import unittest
from pathlib import Path
from unittest.mock import patch
import auth_db
import auth_pages
from admin import app as admin_app
import test_report_metrics as fixtures


class TaskHistoryTests(unittest.TestCase):
    def setUp(self):
        fixtures.ReportMetricsTests.setUp(self)
        self.tasks = []
        self.context_token = auth_db._RESOURCE_TASK.set(None)

    def tearDown(self):
        for task in reversed(self.tasks):
            auth_db.end_processing_task(task, succeeded=False)
        auth_db._RESOURCE_TASK.reset(self.context_token)
        fixtures.ReportMetricsTests.tearDown(self)

    def start(self, uid=1):
        task = auth_db.begin_processing_task(uid, "analysis", "抖音")
        self.tasks.append(task)
        return task

    def row(self, task):
        return self.db.execute("SELECT * FROM processing_tasks WHERE id=?", (task.task_id,)).fetchone()

    def snapshot(self, files=(), uid=1):
        tree = ast.parse(Path(__file__).with_name("api.py").read_text(encoding="utf-8"))
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_account_task_snapshot")
        ns = dict(auth_db=auth_db, hashlib=hashlib, _account_report_files=lambda owner: list(files) if owner == uid else [])
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "api.py", "exec"), ns)
        return ns[fn.name](uid)

    def test_progress_monotonic_clamped_and_only_fixed_phases(self):
        task = self.start()
        auth_db.update_processing_progress(task, .8, "analysis")
        auth_db.update_processing_progress(task, .2, "analysis")
        auth_db.update_processing_progress(task, .9, "private provider message")
        self.assertEqual((self.row(task)["percent"], self.row(task)["phase"]), (80, "analysis"))
        auth_db.update_processing_progress(task, 2, "saving")
        self.assertEqual(self.row(task)["percent"], 99)
        auth_db.end_processing_task(task, succeeded=True)
        auth_db.update_processing_progress(task, .1, "evidence")
        self.assertEqual((self.row(task)["percent"], self.row(task)["phase"]), (100, "complete"))

    def test_output_keys_are_hashed_deduplicated_and_owned(self):
        task = self.start()
        own = self.name.replace(".md", "_asr.txt")
        auth_db.end_processing_task(task, succeeded=True, outputs=(self.name, self.name, own, self.name.replace("vp-u1-", "vp-u2-"), "unknown.md"))
        keys = json.loads(self.row(task)["output_keys"])
        self.assertEqual(keys, [self.key, hashlib.sha256(own.encode()).hexdigest()])
        self.assertNotIn(self.name, repr(tuple(self.row(task))))
        self.assertIsNone(auth_db._RESOURCE_TASK.get())

    def test_failed_outputs_and_corrupt_json_never_create_links(self):
        task = self.start()
        auth_db.end_processing_task(task, succeeded=False, outputs=(self.name,))
        self.assertEqual(auth_db.list_processing_tasks(1)[0]["output_keys"], [])
        for value in ("{bad", "null", "123", '["../../private",5]', '"secret"'):
            self.db.execute("UPDATE processing_tasks SET output_keys=? WHERE id=?", (value, task.task_id))
            self.db.commit()
            self.assertEqual(auth_db.list_processing_tasks(1)[0]["output_keys"], [])

    def test_private_history_reads_do_not_consume_quota_or_expose_runtime(self):
        one = self.start()
        auth_db.end_processing_task(one, succeeded=True)
        two = self.start(2)
        auth_db.end_processing_task(two, succeeded=True)
        for _ in range(3):
            result = auth_db.list_processing_tasks(1)
            self.assertEqual([r["id"] for r in result], [one.task_id])
            self.assertNotIn("runtime_id", result[0])
            self.assertNotIn("user_id", result[0])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM daily_quota").fetchone()[0], 0)

    def test_only_last_thirty_started_tasks_are_returned(self):
        for i in range(33):
            task = self.start()
            auth_db.end_processing_task(task, succeeded=True)
            self.db.execute("UPDATE processing_tasks SET started_at=? WHERE id=?", (f"2026-10-01T00:00:{i:02d}Z", task.task_id))
        self.db.commit()
        tasks = auth_db.list_processing_tasks(1)
        self.assertEqual(len(tasks), 30)
        self.assertTrue(tasks[0]["started_at"].endswith("32Z"))
        self.assertTrue(tasks[-1]["started_at"].endswith("03Z"))

    def test_restart_marks_old_running_tasks_only_and_init_is_read_safe(self):
        old = self.start()
        auth_db.record_resource_usage("model-api", "vision", "test-model", input_tokens=10, output_tokens=3)
        current = self.start(2)
        completed = self.start()
        auth_db.end_processing_task(completed, succeeded=True)
        self.db.execute("UPDATE processing_tasks SET runtime_id='' WHERE id=?", (old.task_id,))
        self.db.commit()
        with patch.object(auth_db, "seed_admin_if_empty"):
            auth_db.init_db()
        self.assertEqual(self.row(old)["outcome"], "running")
        self.assertEqual(auth_db.recover_interrupted_processing_tasks(), 1)
        self.assertEqual((self.row(old)["outcome"], self.row(old)["phase"]), ("failed", "interrupted"))
        self.assertIsNotNone(self.row(old)["finished_at"])
        self.assertEqual(self.row(current)["outcome"], "running")
        self.assertEqual(self.row(completed)["outcome"], "success")
        self.assertEqual(auth_db.recover_interrupted_processing_tasks(), 0)
        self.assertEqual(self.db.execute("SELECT input_tokens FROM task_resource_usage WHERE task_id=?", (old.task_id,)).fetchone()[0], 10)

    def test_admin_interruptions_keep_units_but_exclude_unknown_duration(self):
        old = self.start()
        auth_db.record_resource_usage("model-api", "vision", "test-model", input_tokens=100, output_tokens=10)
        self.db.execute("UPDATE processing_tasks SET runtime_id='old' WHERE id=?", (old.task_id,))
        self.db.commit()
        auth_db.recover_interrupted_processing_tasks()
        auth_db.end_processing_task(old, succeeded=False)
        failed = self.start()
        auth_db.end_processing_task(failed, succeeded=False)
        self.db.execute("UPDATE processing_tasks SET duration_ms=6000 WHERE id=?", (failed.task_id,))
        self.db.commit()
        with patch.object(admin_app, "ro_conn", side_effect=lambda: fixtures.ReportMetricsTests.read_connection(self)):
            stats = admin_app.get_task_resource_stats()
        self.assertEqual(stats["interrupted"], 1)
        row = stats["rows"][0]
        self.assertEqual((row["tasks"], row["avg_seconds"], row["avg_input"]), (2, 6.0, 50))

    def test_upgrade_preserves_existing_tasks_and_admin_reads_legacy_schema(self):
        task = self.start()
        auth_db.end_processing_task(task, succeeded=True)
        for column in ("runtime_id", "phase", "percent", "output_keys"):
            self.db.execute(f"ALTER TABLE processing_tasks DROP COLUMN {column}")
        self.db.commit()
        with patch.object(admin_app, "ro_conn", side_effect=lambda: fixtures.ReportMetricsTests.read_connection(self)):
            self.assertTrue(admin_app.get_task_resource_stats()["ready"])
        with patch.object(auth_db, "seed_admin_if_empty"):
            auth_db.init_db()
            auth_db.init_db()
        self.assertEqual(self.row(task)["outcome"], "success")
        self.assertEqual(self.snapshot()["tasks"][0]["message"], "处理完成")

    def test_snapshot_links_only_existing_owned_files_and_escapes_html(self):
        task = self.start()
        auth_db.end_processing_task(task, succeeded=True, outputs=(self.name,))
        self.assertEqual(self.snapshot()["tasks"][0]["files"], [])
        file = {"name": self.name, "label": "分析报告 · <script>bad</script>"}
        snapshot = self.snapshot([file])
        self.assertEqual(snapshot["tasks"][0]["files"], [file])
        self.assertNotIn("output_keys", snapshot["tasks"][0])
        self.assertEqual(self.snapshot([file], uid=2)["tasks"], [])
        self.assertNotIn("<script>bad</script>", auth_pages.processing_task_cards(snapshot["tasks"]))
        self.assertIn("下载分析报告", auth_pages.processing_task_cards(snapshot["tasks"]))

    def test_workspace_history_opens_new_page_and_preserves_processing_workbench(self):
        tree = ast.parse(Path(__file__).with_name("app.py").read_text(encoding="utf-8"))
        markup = next(n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str) and 'class="vp-report-history"' in n.value)
        attrs = []
        class LinkParser(HTMLParser):
            def handle_starttag(self, tag, attributes):
                if tag == "a": attrs.append(dict(attributes))
        LinkParser().feed(markup)
        self.assertEqual(attrs[0]["href"], "/account/files")
        self.assertEqual(attrs[0]["target"], "_blank")
        self.assertIn("noopener", attrs[0]["rel"])
        self.assertIn("新页面", markup)


if __name__ == "__main__": unittest.main()
