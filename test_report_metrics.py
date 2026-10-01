"""Export delivery and actionable report feedback, without production data."""
import asyncio
import hashlib
import sqlite3
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import auth_db
import auth_middleware
import auth_pages
from admin import app as admin_app


class ReportMetricsTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db_patch = patch.object(auth_db, "_db", return_value=self.db)
        self.db_patch.start()
        with patch.object(auth_db, "seed_admin_if_empty"):
            auth_db.init_db()
        self.day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        for uid in (1, 2, 99):
            self.db.execute(
                "INSERT INTO users(id,username,password_hash,is_admin,created_at) VALUES (?,?,?,?,?)",
                (uid, f"user{uid}", "unused", int(uid == 99), self.day),
            )
        self.db.commit()
        self.name = "vp-u1-example_1727000000_123456abcdef.md"
        self.key = hashlib.sha256(self.name.encode()).hexdigest()

    def tearDown(self):
        self.db_patch.stop()
        self.db.close()

    def read_connection(self):
        # A separate connection lets the real admin queries close their readers.
        reader = sqlite3.connect(":memory:")
        self.db.backup(reader)
        return reader

    def test_legacy_feedback_migration_is_idempotent_and_preserves_ratings(self):
        self.db.execute("DROP TABLE report_feedback")
        self.db.execute("CREATE TABLE report_feedback(report_key TEXT,user_id INTEGER,rating INTEGER,updated_at TEXT,PRIMARY KEY(user_id,report_key))")
        self.db.execute("INSERT INTO report_feedback VALUES (?,1,-1,?)", (self.key, self.day))
        self.db.commit()
        with patch.object(auth_db, "seed_admin_if_empty"):
            auth_db.init_db()
            auth_db.init_db()
        self.assertEqual(auth_db.get_report_feedback_details(1, self.name), {"rating": -1, "reason": ""})

    def test_downloads_deduplicate_daily_and_keep_only_hashes(self):
        for _ in range(3):
            auth_db.record_artifact_export(1, self.name)
        auth_db.record_artifact_export(1, self.name.replace(".md", "_asr.txt"))
        self.assertEqual([tuple(r) for r in self.db.execute("SELECT action,count FROM user_activity ORDER BY action")],
                         [("asr_export", 1), ("report_export", 1)])
        rows = [tuple(r) for r in self.db.execute("SELECT * FROM artifact_exports")]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(len(row[2]) == 64 for row in rows))
        self.assertNotIn(self.name, repr(rows))

    def test_other_owner_and_non_export_files_are_not_counted(self):
        for name in (self.name.replace("vp-u1-", "vp-u2-"), "video.mp4", "vp-u1-unverified.md", "../" + self.name):
            auth_db.record_artifact_export(1, name)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM artifact_exports").fetchone()[0], 0)

    def test_activity_failure_rolls_back_dedup_marker_so_retry_can_succeed(self):
        self.db.execute("DROP TABLE user_activity")
        self.db.commit()
        with self.assertLogs("auth_db", level="ERROR"):
            auth_db.record_artifact_export(1, self.name)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM artifact_exports").fetchone()[0], 0)

    def test_delete_account_cascades_export_and_feedback_records(self):
        auth_db.record_artifact_export(1, self.name)
        auth_db.set_report_feedback(1, self.name, -1, "asr")
        auth_db.record_artifact_export(2, self.name.replace("vp-u1-", "vp-u2-"))
        self.db.execute("DELETE FROM users WHERE id=1")
        self.db.commit()
        for table in ("artifact_exports", "report_feedback", "user_activity"):
            self.assertEqual(self.db.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id=1").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM artifact_exports WHERE user_id=2").fetchone()[0], 1)

    def test_new_utc_day_counts_again_and_prunes_old_detail(self):
        old = (datetime.now(timezone.utc) - timedelta(days=91)).strftime("%Y-%m-%d")
        self.db.execute("INSERT INTO artifact_exports VALUES (?,1,?,'report')", (old, self.key))
        self.db.commit()
        auth_db.record_artifact_export(1, self.name)
        tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
        with patch.object(auth_db, "datetime") as clock:
            clock.now.return_value = tomorrow
            auth_db.record_artifact_export(1, self.name)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM artifact_exports").fetchone()[0], 2)

    def test_feedback_is_optional_updatable_and_positive_clears_problem(self):
        self.assertTrue(auth_db.set_report_feedback(1, self.name, -1))
        self.assertTrue(auth_db.set_report_feedback(1, self.name, -1, "evidence"))
        self.assertEqual(auth_db.get_report_feedback_details(1, self.name), {"rating": -1, "reason": "evidence"})
        self.assertTrue(auth_db.set_report_feedback(1, self.name, 1, "evidence"))
        self.assertEqual(auth_db.get_report_feedback_details(1, self.name), {"rating": 1, "reason": ""})
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM report_feedback").fetchone()[0], 1)
        self.assertIsNone(auth_db.get_report_feedback(2, self.name))

    def test_invalid_feedback_does_not_overwrite_existing_choice(self):
        auth_db.set_report_feedback(1, self.name, -1, "timeline")
        self.assertFalse(auth_db.set_report_feedback(1, self.name, 5, "asr"))
        self.assertFalse(auth_db.set_report_feedback(1, self.name, -1, "<script>"))
        self.assertEqual(auth_db.get_report_feedback_details(1, self.name), {"rating": -1, "reason": "timeline"})

    def test_preview_escapes_content_and_restores_reason_choice(self):
        page = auth_pages.account_file_preview_page(self.name, "报告", "<script>x</script>", False,
                                                    "token", -1, "asr")
        self.assertIn('&lt;script&gt;x&lt;/script&gt;', page)
        self.assertIn('value="asr" selected', page)
        self.assertIn('语音转写不准确', page)
        self.assertNotIn('feedback-reason', auth_pages.account_file_preview_page(
            self.name.replace(".md", "_asr.txt"), "转写", "test", False, "token"))

    def test_admin_counts_distinct_files_and_matches_feedback_to_same_file(self):
        auth_db.record_artifact_export(1, self.name)
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        self.db.execute("INSERT INTO artifact_exports VALUES (?,1,?,'report')", (yesterday, self.key))
        self.db.commit()
        other = self.name.replace("vp-u1-", "vp-u2-")
        admin = self.name.replace("vp-u1-", "vp-u99-")
        for uid, name in ((2, other), (99, admin)):
            auth_db.record_artifact_export(uid, name)
        auth_db.set_report_feedback(1, self.name, 1)
        auth_db.set_report_feedback(2, other + "different", 1)
        auth_db.set_report_feedback(99, admin, 1)
        with patch.object(admin_app, "ro_conn", side_effect=self.read_connection):
            result = admin_app.get_export_stats()
        self.assertTrue(result["ready"])
        self.assertEqual((result["report_users"], result["report_files"], result["useful_users"]), (2, 2, 1))
        auth_db.set_report_feedback(1, self.name, -1, "actionable")
        with patch.object(admin_app, "ro_conn", side_effect=self.read_connection):
            self.assertEqual(admin_app.get_export_stats()["useful_users"], 0)
            feedback = admin_app.get_feedback_stats()
        self.assertEqual(feedback["reasons"], [{"label": "建议不够具体", "count": 1}])

    def test_admin_tolerates_old_schema_and_waits_for_exports(self):
        self.db.execute("DROP TABLE artifact_exports")
        self.db.execute("ALTER TABLE report_feedback DROP COLUMN reason")
        self.db.commit()
        with patch.object(admin_app, "ro_conn", side_effect=self.read_connection):
            self.assertFalse(admin_app.get_export_stats()["ready"])
            feedback = admin_app.get_feedback_stats()
        self.assertTrue(feedback["ready"])
        self.assertFalse(feedback["reasons_ready"])

    def test_admin_excludes_old_downloads_and_counts_asr_separately(self):
        old = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
        self.db.execute("INSERT INTO artifact_exports VALUES (?,1,?,'report')", (old, self.key))
        self.db.commit()
        auth_db.set_report_feedback(1, self.name, 1)
        auth_db.record_artifact_export(1, self.name.replace(".md", "_asr.srt"))
        auth_db.record_artifact_export(99, self.name.replace("vp-u1-", "vp-u99-").replace(".md", "_asr.srt"))
        with patch.object(admin_app, "ro_conn", side_effect=self.read_connection):
            result = admin_app.get_export_stats()
        self.assertEqual((result["report_users"], result["useful_users"], result["asr_users"], result["asr_files"]), (0, 0, 1, 1))

    def test_real_admin_dashboard_renders_feedback_and_export_sections(self):
        auth_db.record_artifact_export(1, self.name)
        auth_db.set_report_feedback(1, self.name, -1, "timeline")
        with admin_app.app.test_client() as client:
            with client.session_transaction() as session:
                session["admin_user"] = "user99"
            with patch.object(admin_app, "ro_conn", side_effect=self.read_connection):
                response = client.get("/", base_url="http://localhost/admin/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("报告与转写导出", html)
        self.assertIn("时间点不准确：1 份", html)


class ExportDeliveryTests(unittest.TestCase):
    name = "vp-u1-example_1727000000_123456abcdef.md"

    def request(self, path=None, method="GET", status=200, *, fail_send=False, unfinished=False, user=True):
        scope = {"type": "http", "method": method, "path": path or "/account/files/" + self.name,
                 "headers": [(b"cookie", b"vp_session=token")]}
        sent = []

        async def downstream(scope, receive, send):
            await send({"type": "http.response.start", "status": status, "headers": []})
            await send({"type": "http.response.body", "body": b"part", "more_body": True})
            if not unfinished:
                await send({"type": "http.response.body", "body": b"end", "more_body": False})

        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(message):
            if fail_send and message["type"] == "http.response.body" and not message.get("more_body", False):
                raise ConnectionError("client disconnected")
            sent.append(message)

        with patch.object(auth_db, "get_session_user", return_value={"id": 1} if user else None), \
             patch.object(auth_db, "record_artifact_export") as record:
            if fail_send:
                with self.assertRaises(ConnectionError):
                    asyncio.run(auth_middleware.SessionAuthMiddleware(downstream)(scope, receive, send))
            else:
                asyncio.run(auth_middleware.SessionAuthMiddleware(downstream)(scope, receive, send))
        return record, sent

    def test_both_download_entries_record_only_after_full_response(self):
        for path in ("/account/files/" + self.name, "/gradio_api/file=/tmp/gradio/x/" + self.name,
                     "/file=E:%5Ccache%5C" + self.name):
            with self.subTest(path=path):
                record, sent = self.request(path)
                record.assert_called_once_with(1, self.name)
                self.assertEqual(b"".join(m.get("body", b"") for m in sent), b"partend")

    def test_preview_head_partial_missing_and_unauthorized_do_not_count(self):
        cases = [dict(path="/account/files/" + self.name + "/preview"), dict(method="HEAD"),
                 dict(status=206), dict(status=304), dict(status=404), dict(user=False),
                 dict(path="/account/files/" + self.name.replace("vp-u1-", "vp-u2-")),
                 dict(path="/gradio_api/file=/tmp/" + self.name.replace("vp-u1-", "vp-u2-"))]
        for options in cases:
            with self.subTest(options=options):
                record, _ = self.request(**options)
                record.assert_not_called()

    def test_incomplete_or_disconnected_transfer_does_not_count(self):
        for options in (dict(unfinished=True), dict(fail_send=True)):
            with self.subTest(options=options):
                record, _ = self.request(**options)
                record.assert_not_called()


if __name__ == "__main__":
    unittest.main()
