"""Focused checks for the independent transcription user flow."""

import asyncio
import os
import sqlite3
import tempfile
import unittest
import importlib
from types import SimpleNamespace
from unittest.mock import patch

import app
from auth_middleware import SessionAuthMiddleware, _requires_login_json
from video_analysis import TimedText


class StandaloneAsrTests(unittest.TestCase):
    def test_transcript_exports_without_running_ai_analysis(self):
        cues = [TimedText(0.0, 1.4, "你好，世界。", "asr")]
        with tempfile.TemporaryDirectory() as output_dir, \
             patch.dict(os.environ, {"ASR_MODEL_ID": "tencent-flash"}), \
             patch.object(app.client, "download_dir", output_dir), \
             patch.object(app, "_vp_session_from_request", return_value={"id": 1}), \
             patch.object(app, "_load_media_for_processing", return_value=("video.mp4", "", "")), \
             patch.object(app, "inspect_video", return_value=SimpleNamespace(audio_tracks=1)), \
             patch.object(app, "_allow_user_action", return_value=True), \
             patch.object(app, "transcribe_audio", return_value=(cues, "timestamped", None)), \
             patch.object(app, "analyze_video_evidence_first") as analyze, \
             patch.object(app.auth_db, "record_usage") as record:
            result = app.transcribe_video_only(
                {"platform": "抖音", "video_id": "sample"}, progress=lambda *args, **kwargs: None,
                request=object(),
            )
            self.assertEqual(len(result), 5)
            self.assertIn("你好，世界", result[0])
            self.assertIn("转写完成", result[1])
            self.assertFalse(result[2]["visible"])
            self.assertTrue(result[3]["visible"])
            self.assertTrue(result[4]["visible"])
            with open(result[3]["value"], encoding="utf-8") as text_file:
                self.assertIn("未人工校对", text_file.read())
            with open(result[4]["value"], encoding="utf-8") as srt_file:
                self.assertIn("00:00:00,000 --> 00:00:01,400", srt_file.read())
            analyze.assert_not_called()
            record.assert_any_call("transcribe", "抖音", "success", duration_ms=unittest.mock.ANY)

    def test_no_audio_does_not_consume_transcription_quota(self):
        with patch.dict(os.environ, {"ASR_MODEL_ID": "tencent-flash"}), \
             patch.object(app, "_vp_session_from_request", return_value={"id": 1}), \
             patch.object(app, "_load_media_for_processing", return_value=("video.mp4", "", "")), \
             patch.object(app, "inspect_video", return_value=SimpleNamespace(audio_tracks=0)), \
             patch.object(app, "_allow_user_action") as quota, \
             patch.object(app.auth_db, "record_usage"):
            result = app.transcribe_video_only(
                {"platform": "抖音", "video_id": "sample"}, progress=lambda *args, **kwargs: None,
                request=object(),
            )
            self.assertIn("没有音轨", result[1])
            quota.assert_not_called()

    def test_new_parse_clears_old_media_and_exports(self):
        with patch.object(app, "parse_video", return_value=("ok", "info", None, "url", "guide", {})):
            result = app.parse_video_for_ui("new url", "抖音")
        self.assertEqual(len(result), 13)
        self.assertEqual(result[8], app.REPORT_PLACEHOLDER)
        for index in (6, 7, 9, 10, 11):
            self.assertFalse(result[index]["visible"])
        self.assertIsNone(result[12]["value"])

    def test_owned_upload_enters_existing_media_workflow(self):
        with tempfile.TemporaryDirectory() as output_dir:
            upload_dir = os.path.join(output_dir, "gradio")
            cache_dir = os.path.join(output_dir, "cache")
            os.makedirs(upload_dir)
            os.makedirs(cache_dir)
            source = os.path.join(upload_dir, "owned.mp4")
            with open(source, "wb") as uploaded_file:
                uploaded_file.write(b"sample video")
            media = SimpleNamespace(width=1920, height=1080, duration=12.0)
            with patch("gradio.utils.get_upload_folder", return_value=upload_dir), \
                 patch.object(app.client, "cache_dir", cache_dir), \
                 patch.object(app, "_vp_session_from_request", return_value={"id": 1}), \
                 patch.object(app, "_allow_user_action", return_value=True), \
                 patch.object(app, "inspect_video", return_value=media), \
                 patch.object(app, "_media_disk_available", return_value=True), \
                 patch.object(app.shutil, "disk_usage", return_value=SimpleNamespace(free=10 * 1024**3)), \
                 patch.object(app.auth_db, "record_usage") as record:
                result = app.upload_owned_video(source, "视频号", request=object())
                self.assertEqual(len(result), 12)
                self.assertEqual(result[5]["platform"], "视频号")
                self.assertTrue(result[5]["_uploaded"])
                self.assertTrue(os.path.isfile(result[6]["value"]))
                self.assertEqual(result[6]["value"], result[7]["value"])
                self.assertEqual(app._load_media_for_processing({"id": 1}, result[5], None)[0], result[6]["value"])
                record.assert_any_call("upload", "视频号", "success")
            self.assertTrue(_requires_login_json("/gradio_api/upload"))

    def test_upload_rejects_server_file_outside_gradio_uploads(self):
        with tempfile.TemporaryDirectory() as output_dir:
            upload_dir = os.path.join(output_dir, "gradio")
            os.makedirs(upload_dir)
            source = os.path.join(output_dir, "other.mp4")
            with open(source, "wb") as file:
                file.write(b"sample")
            with patch("gradio.utils.get_upload_folder", return_value=upload_dir), \
                 patch.object(app, "_vp_session_from_request", return_value={"id": 1}), \
                 patch.object(app.auth_db, "record_usage"), \
                 patch.object(app, "inspect_video") as inspect:
                result = app.upload_owned_video(source, "视频号", request=object())
                self.assertIn("只接受通过页面上传", result[0])
                inspect.assert_not_called()

    def test_upload_http_requires_session_and_has_its_own_quota(self):
        messages = []

        async def downstream(scope, receive, send):
            raise AssertionError("upload request should be blocked")

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        middleware = SessionAuthMiddleware(downstream)
        scope = {"type": "http", "method": "POST", "path": "/gradio_api/upload", "headers": []}
        asyncio.run(middleware(scope, receive, send))
        self.assertEqual(messages[0]["status"], 401)

        messages.clear()
        scope["headers"] = [(b"cookie", b"vp_session=valid")]
        with patch("auth_middleware.auth_db.get_session_user", return_value={"id": 1}), \
             patch("auth_middleware.auth_db.consume_daily_quotas", return_value=False) as quota:
            asyncio.run(middleware(scope, receive, send))
            self.assertEqual(messages[0]["status"], 429)
            quota.assert_called_once()
            self.assertEqual(quota.call_args.args[0][0][1], "upload_http")

    def test_admin_counts_transcription_and_renders_dashboard(self):
        admin_app = importlib.import_module("admin.app")
        with tempfile.TemporaryDirectory() as output_dir:
            db_path = os.path.join(output_dir, "metrics.db")
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE usage_metrics (day TEXT, action TEXT, platform TEXT, outcome TEXT, reason TEXT, count INTEGER, duration_ms_total INTEGER)")
            conn.execute("INSERT INTO usage_metrics VALUES (date('now'), 'transcribe', '抖音', 'success', '', 2, 30000)")
            conn.execute("INSERT INTO usage_metrics VALUES (date('now'), 'upload', '视频号', 'success', '', 1, 5000)")
            conn.commit()
            conn.close()
            with patch.object(admin_app, "AUTH_DB_PATH", db_path):
                metrics = admin_app.get_usage_stats()
            self.assertEqual(metrics["transcribe"]["success"], 2)
            self.assertEqual(metrics["transcribe"]["avg_success_seconds"], 15.0)
            self.assertEqual(metrics["upload"]["success"], 1)
            with admin_app.app.test_request_context("/"):
                rendered = admin_app.render_template("dashboard.html", s={}, m=metrics, user="admin")
            self.assertIn("独立转写完成", rendered)
            self.assertIn("自有视频上传完成", rendered)


if __name__ == "__main__":
    unittest.main()
