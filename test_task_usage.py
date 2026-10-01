"""Task attribution with isolated SQLite and no external model requests."""
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch
import auth_db
from admin import app as admin_app
import test_report_metrics as report_fixtures


class TaskUsageTests(unittest.TestCase):
    def setUp(self):
        report_fixtures.ReportMetricsTests.setUp(self)
        self.context_token = auth_db._RESOURCE_TASK.set(None)

    def tearDown(self):
        auth_db._RESOURCE_TASK.reset(self.context_token)
        report_fixtures.ReportMetricsTests.tearDown(self)

    reader = report_fixtures.ReportMetricsTests.read_connection

    def usage(self, **kwargs):
        auth_db.record_resource_usage("model-api", "vision", "test-model", **kwargs)

    def stats(self):
        with patch.object(admin_app, "ro_conn", side_effect=self.reader):
            return admin_app.get_task_resource_stats()

    def test_success_and_idempotent_finish_restore_context(self):
        task = auth_db.begin_processing_task(1, "analysis", "private URL and title")
        self.usage(input_tokens=100, output_tokens=20)
        auth_db.end_processing_task(task, succeeded=True)
        auth_db.end_processing_task(task, succeeded=False)
        self.assertIsNone(auth_db._RESOURCE_TASK.get())
        row = self.db.execute("SELECT * FROM processing_tasks").fetchone()
        self.assertEqual((row["outcome"], row["platform"]), ("success", "其他"))
        self.assertIsNotNone(row["finished_at"])
        self.assertNotIn("private URL", repr(tuple(row)))
        self.assertEqual(self.stats()["rows"][0]["avg_input"], 100)

    def test_nested_tasks_attribute_to_owner_and_restore_parent(self):
        first = auth_db.begin_processing_task(1, "analysis", "抖音")
        self.usage(input_tokens=10, output_tokens=1)
        second = auth_db.begin_processing_task(2, "transcribe", "上传")
        self.usage(input_tokens=50, output_tokens=2)
        auth_db.end_processing_task(second, succeeded=False)
        self.usage(input_tokens=20, output_tokens=3)
        auth_db.end_processing_task(first, succeeded=True)
        rows = {r["task_id"]: (r["calls"], r["input_tokens"]) for r in self.db.execute("SELECT * FROM task_resource_usage")}
        self.assertEqual(rows, {first.task_id: (2, 30), second.task_id: (1, 50)})
        self.assertEqual(tuple(self.db.execute("SELECT calls,input_tokens FROM resource_usage").fetchone()), (3, 80))

    def test_failed_missing_and_audio_units_remain_distinct(self):
        task = auth_db.begin_processing_task(1, "analysis", "上传")
        self.usage(failed=True)
        self.usage()
        auth_db.record_resource_usage("tencent-asr", "flash-transcribe", "16k_zh", submitted_audio_ms=60000, failed=True)
        auth_db.end_processing_task(task, succeeded=False)
        row = self.stats()["rows"][0]
        self.assertEqual((row["outcome"], row["model_failed"], row["missing"], row["asr_failed"], row["avg_audio_minutes"]), ("失败", 1, 1, 1, 1))

    def test_cache_zero_and_multiple_calls_do_not_multiply_task_denominator(self):
        task = auth_db.begin_processing_task(1, "analysis", "抖音")
        self.usage(input_tokens=60, output_tokens=10)
        auth_db.record_resource_usage("model-api", "synthesis", "test-model", input_tokens=40, output_tokens=30)
        auth_db.end_processing_task(task, succeeded=True)
        cached = auth_db.begin_processing_task(1, "analysis", "抖音")
        auth_db.end_processing_task(cached, succeeded=True)
        row = self.stats()["rows"][0]
        self.assertEqual((row["tasks"], row["avg_input"], row["avg_output"], row["avg_calls"]), (2, 50, 20, 1))

    def test_delete_inflight_account_keeps_global_units_and_removes_private_rows(self):
        task = auth_db.begin_processing_task(1, "analysis", "抖音")
        self.usage(input_tokens=10, output_tokens=1)
        self.db.execute("DELETE FROM users WHERE id=1")
        self.db.commit()
        self.usage(input_tokens=20, output_tokens=2)
        auth_db.end_processing_task(task, succeeded=True)
        self.assertEqual(tuple(self.db.execute("SELECT calls,input_tokens FROM resource_usage").fetchone()), (2, 30))
        for table in ("processing_tasks", "task_resource_usage"):
            self.assertEqual(self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_telemetry_error_rolls_back_without_raising_into_processing(self):
        task = auth_db.begin_processing_task(1, "analysis", "抖音")
        self.db.execute("DROP TABLE task_resource_usage")
        self.db.commit()
        with self.assertLogs("auth_db", level="ERROR"):
            self.usage(input_tokens=5, output_tokens=2)
        self.assertFalse(self.db.in_transaction)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM resource_usage").fetchone()[0], 0)
        auth_db.end_processing_task(task, succeeded=False)
        self.assertIsNone(auth_db._RESOURCE_TASK.get())

    def test_old_and_admin_tasks_excluded_with_stale_running_separate(self):
        for uid in (1, 99):
            task = auth_db.begin_processing_task(uid, "analysis", "抖音")
            self.usage(input_tokens=100, output_tokens=10)
            auth_db.end_processing_task(task, succeeded=True)
            if uid == 1:
                old = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
                self.db.execute("UPDATE processing_tasks SET day=? WHERE id=?", (old, task.task_id))
                self.db.commit()
        task = auth_db.begin_processing_task(1, "analysis", "抖音")
        self.usage(input_tokens=12, output_tokens=3)
        earlier = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        self.db.execute("UPDATE processing_tasks SET started_at=? WHERE id=?", (earlier, task.task_id))
        self.db.commit()
        result = self.stats()
        self.assertEqual((result["stale"], len(result["rows"])), (1, 1))
        self.assertEqual((result["rows"][0]["outcome"], result["rows"][0]["avg_seconds"]), ("尚未结束", None))
        auth_db.end_processing_task(task, succeeded=False)

    def test_retention_cascades_and_old_schema_is_tolerated(self):
        task = auth_db.begin_processing_task(1, "analysis", "上传")
        self.usage(input_tokens=20, output_tokens=3)
        auth_db.end_processing_task(task, succeeded=True)
        self.db.execute("UPDATE processing_tasks SET day='2000-01-01'")
        self.db.commit()
        task = auth_db.begin_processing_task(1, "transcribe", "上传")
        auth_db.end_processing_task(task, succeeded=True)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM task_resource_usage").fetchone()[0], 0)
        self.db.execute("DROP TABLE task_resource_usage")
        self.db.commit()
        self.assertFalse(self.stats()["ready"])


class ThreadAttributionTests(unittest.TestCase):
    def test_simultaneous_workers_are_isolated(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(auth_db, "DB_PATH", str(Path(tmp) / "auth.db")), \
             patch.object(auth_db, "_DB_LOCAL", threading.local()), patch.object(auth_db, "seed_admin_if_empty"):
            auth_db.init_db()
            for uid in (1, 2):
                auth_db._db().execute("INSERT INTO users(id,username,password_hash,created_at) VALUES (?,?,?,?)", (uid, str(uid), "unused", auth_db._now_iso()))
            auth_db._db().commit()
            barrier, errors = threading.Barrier(2), []
            def worker(uid):
                try:
                    task = auth_db.begin_processing_task(uid, "analysis", "上传")
                    barrier.wait(timeout=5)
                    auth_db.record_resource_usage("model-api", "vision", "test-model", input_tokens=uid * 100, output_tokens=uid)
                    auth_db.end_processing_task(task, succeeded=True)
                    self.assertIsNone(auth_db._RESOURCE_TASK.get())
                except Exception as error:
                    errors.append(error)
                finally:
                    if getattr(auth_db._DB_LOCAL, "conn", None): auth_db._DB_LOCAL.conn.close()
            threads = [threading.Thread(target=worker, args=(uid,)) for uid in (1, 2)]
            for thread in threads: thread.start()
            for thread in threads:
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            rows = auth_db._db().execute("SELECT t.user_id,r.input_tokens FROM processing_tasks t JOIN task_resource_usage r ON r.task_id=t.id ORDER BY t.user_id").fetchall()
            self.assertEqual([tuple(r) for r in rows], [(1, 100), (2, 200)])
            auth_db._DB_LOCAL.conn.close()


if __name__ == "__main__": unittest.main()
