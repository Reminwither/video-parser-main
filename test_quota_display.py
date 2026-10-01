"""Read-only allowance display and UTC reset behavior, with an isolated database."""
import os
import sqlite3
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import auth_db
import auth_pages


class QuotaDisplayTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute("CREATE TABLE daily_quota(day TEXT, subject TEXT, action TEXT, count INTEGER, PRIMARY KEY(day, subject, action))")
        self.db_patch = patch.object(auth_db, "_db", return_value=self.db)
        self.db_patch.start()
        self.env_patch = patch.dict(os.environ, {}, clear=True)
        self.env_patch.start()
        self.day = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def tearDown(self):
        self.env_patch.stop()
        self.db_patch.stop()
        self.db.close()

    def count(self, subject, action, number, day=None):
        self.db.execute("INSERT INTO daily_quota VALUES (?, ?, ?, ?)", (day or self.day, subject, action, number))
        self.db.commit()

    def item(self, snapshot, action):
        return next(row for row in snapshot["items"] if row["action"] == action)

    def test_display_uses_current_account_and_configuration_without_charging(self):
        os.environ["DAILY_ANALYSIS_LIMIT"] = "4"
        self.count("user:1", "analysis", 1)
        self.count("user:2", "analysis", 99)
        before = [tuple(row) for row in self.db.execute("SELECT * FROM daily_quota")]
        for _ in range(3):
            item = self.item(auth_db.user_daily_quota(1), "analysis")
            self.assertEqual((item["remaining"], item["limit"]), (3, 4))
            self.assertNotIn("site_used", item)
        self.assertEqual([tuple(row) for row in self.db.execute("SELECT * FROM daily_quota")], before)
        page = auth_pages.account_page({"username": "tester"}, quotas=auth_db.user_daily_quota(1))
        self.assertIn("剩余 3 / 4 次", page)
        self.assertIn("08:00", page)

    def test_utc_day_resets_at_eight_in_china_even_before_eight_today(self):
        china = timezone(timedelta(hours=8))
        before = datetime(2026, 10, 1, 7, 59, tzinfo=china)
        after = datetime(2026, 10, 1, 8, 0, tzinfo=china)
        self.count("user:1", "analysis", 2, "2026-09-30")
        first = auth_db.user_daily_quota(1, before)
        self.assertEqual(self.item(first, "analysis")["remaining"], 0)
        self.assertEqual(first["reset_at"], "2026-10-01T00:00:00+00:00")
        self.assertEqual(first["reset_label"], "北京时间 10月01日 08:00")
        second = auth_db.user_daily_quota(1, after)
        self.assertEqual(self.item(second, "analysis")["remaining"], 2)
        self.assertEqual(second["reset_at"], "2026-10-02T00:00:00+00:00")

    def test_upload_allowance_handles_both_raw_receive_and_import_counters(self):
        self.count("user:1", "upload_http", 4)
        self.count("user:1", "upload", 2)
        self.count("site", "upload_http", 30)
        item = self.item(auth_db.user_daily_quota(1), "upload")
        self.assertEqual(item["remaining"], 1)
        self.assertFalse(item["site_available"])
        self.assertIn("本站今日上传新文件处理额度已满", auth_db.quota_notice(1, "upload_http"))

    def test_received_video_can_be_imported_after_file_receive_is_exhausted(self):
        self.count("user:1", "upload_http", 5)
        self.count("user:1", "upload", 2)
        self.count("site", "upload_http", 30)
        snapshot = auth_db.user_daily_quota(1)
        item = self.item(snapshot, "upload")
        stages = {stage["action"]: stage for stage in item["stages"]}
        self.assertEqual(item["remaining"], 0)
        self.assertFalse(item["site_available"])
        self.assertEqual(stages["upload"]["remaining"], 3)
        self.assertTrue(stages["upload"]["site_available"])
        page = auth_pages.account_page({"username": "tester"}, quotas=snapshot)
        self.assertIn("使用已上传视频：剩余 3 / 5 次", page)
        self.assertTrue(auth_db.consume_daily_quotas([("user:1", "upload", 5), ("site", "upload", 30)]))

    def test_import_rejection_uses_import_counter_when_receive_is_exhausted(self):
        self.count("user:1", "upload_http", 5)
        self.count("user:1", "upload", 2)
        self.count("site", "upload", 30)
        self.assertFalse(auth_db.consume_daily_quotas([("user:1", "upload", 5), ("site", "upload", 30)]))
        notice = auth_db.quota_notice(1, "upload")
        self.assertTrue(notice.startswith("本站今日使用已上传视频处理额度已满"))
        self.assertNotIn("今日使用已上传视频额度已用完", notice)

    def test_receive_rejection_uses_receive_counter_when_import_is_exhausted(self):
        self.count("user:1", "upload_http", 2)
        self.count("user:1", "upload", 5)
        self.count("site", "upload_http", 30)
        self.assertFalse(auth_db.consume_daily_quotas([("user:1", "upload_http", 5), ("site", "upload_http", 30)]))
        notice = auth_db.quota_notice(1, "upload_http")
        self.assertTrue(notice.startswith("本站今日上传新文件处理额度已满"))
        self.assertNotIn("今日上传新文件额度已用完", notice)

    def test_centralized_limits_preserve_defaults_and_upload_alias(self):
        expected = {"parse": (30, 300), "upload": (5, 30), "play": (10, 50),
                    "download": (10, 50), "transcribe": (5, 30), "analysis": (2, 10)}
        for action, limits in expected.items():
            self.assertEqual(auth_db.action_quota_limits(action), limits)
        os.environ["DAILY_UPLOAD_LIMIT"] = "7"
        os.environ["DAILY_SITE_UPLOAD_LIMIT"] = "40"
        self.assertEqual(auth_db.action_quota_limits("upload"), (7, 40))
        self.assertEqual(auth_db.action_quota_limits("upload_http"), (7, 40))

    def test_site_exhaustion_does_not_charge_user_or_claim_user_used_their_allowance(self):
        self.count("site", "analysis", 10)
        self.assertFalse(auth_db.consume_daily_quotas([("user:1", "analysis", 2), ("site", "analysis", 10)]))
        snapshot = auth_db.user_daily_quota(1)
        self.assertEqual(self.item(snapshot, "analysis")["remaining"], 2)
        self.assertIn("本站今日AI 分析处理额度已满", auth_db.quota_notice(1, "analysis"))
        self.count("user:2", "analysis", 2)
        self.assertTrue(auth_db.quota_notice(2, "analysis").startswith("今日AI 分析额度已用完"))


if __name__ == "__main__":
    unittest.main()
