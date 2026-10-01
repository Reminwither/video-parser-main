"""Run the shipped handlers with fake media/providers and isolated SQLite."""
import ast
import html
import logging
import os
import secrets
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import auth_db
import test_report_metrics as fixtures


class ProcessingAttributionTests(unittest.TestCase):
    setUp = fixtures.ReportMetricsTests.setUp
    tearDown = fixtures.ReportMetricsTests.tearDown

    def handlers(self, output):
        tree = ast.parse(Path(__file__).with_name("app.py").read_text(encoding="utf-8"))
        names = {"transcribe_video_only", "extract_video_content"}
        functions = ast.Module(body=[n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names], type_ignores=[])
        evidence = SimpleNamespace(frames=[1], subtitles=[], raw_transcript=[], transcript_status="asr_empty")
        def analyze(*args, **kwargs):
            auth_db.record_resource_usage("model-api", "vision", "test-model", input_tokens=40, output_tokens=8)
            return "Synthetic report", evidence
        def transcribe(*args, **kwargs):
            auth_db.record_resource_usage("tencent-asr", "flash-transcribe", "test-engine", submitted_audio_ms=6000)
            return [], "asr_empty", None
        ns = dict(auth_db=auth_db, os=os, html=html, logging=logging, secrets=secrets, tempfile=tempfile, time=time,
                  gr=SimpleNamespace(Progress=lambda: None, Request=object),
                  _vp_session_from_request=lambda _: {"id": 1},
                  _load_media_for_processing=lambda *a: ("fake.mp4", "", ""),
                  _allow_user_action=lambda *a: True, inspect_video=lambda _: SimpleNamespace(audio_tracks=1),
                  _ANALYSIS_SLOTS=threading.BoundedSemaphore(1), _TRANSCRIBE_SLOTS=threading.BoundedSemaphore(1),
                  _analysis_result=lambda *a: a, _asr_status_label=lambda x: x, REPORT_PLACEHOLDER="empty",
                  AI_ANALYSIS_ENABLED=True, OpenAI=lambda **kw: object(), QWEN_API_BASE_URL="", QWEN_API_KEY="",
                  QWEN_MODEL_ID="test-model", MAX_ANALYSIS_FRAMES=1, client=SimpleNamespace(download_dir=output),
                  analyze_video_evidence_first=analyze, transcribe_audio=transcribe)
        exec(compile(functions, "app.py", "exec"), ns)
        return ns

    def test_actual_report_handler_attributes_and_finishes_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            ns = self.handlers(tmp)
            result = ns["extract_video_content"](video_info={"platform": "抖音", "video_id": "sample"}, progress=lambda *a, **kw: None)
            self.assertTrue(Path(result[2]).is_file())
        row = self.db.execute("SELECT action,outcome FROM processing_tasks").fetchone()
        self.assertEqual(tuple(row), ("analysis", "success"))
        self.assertEqual(self.db.execute("SELECT input_tokens FROM task_resource_usage").fetchone()[0], 40)
        self.assertIsNone(auth_db._RESOURCE_TASK.get())

    def test_empty_asr_keeps_submitted_audio_on_failed_task_and_releases_slot(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"ASR_MODEL_ID": "test"}):
            ns = self.handlers(tmp)
            ns["transcribe_video_only"]({"platform": "自有视频", "video_id": "sample"}, progress=lambda *a, **kw: None)
            self.assertTrue(ns["_TRANSCRIBE_SLOTS"].acquire(blocking=False))
            ns["_TRANSCRIBE_SLOTS"].release()
        row = self.db.execute("SELECT action,outcome FROM processing_tasks").fetchone()
        self.assertEqual(tuple(row), ("transcribe", "failed"))
        self.assertEqual(self.db.execute("SELECT submitted_audio_ms FROM task_resource_usage").fetchone()[0], 6000)
        self.assertIsNone(auth_db._RESOURCE_TASK.get())

    def test_quota_rejection_creates_no_processing_or_provider_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            ns = self.handlers(tmp)
            ns["_allow_user_action"] = lambda *a: False
            ns["extract_video_content"](video_info={"platform": "抖音"}, progress=lambda *a, **kw: None)
            self.assertTrue(ns["_ANALYSIS_SLOTS"].acquire(blocking=False))
            ns["_ANALYSIS_SLOTS"].release()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM processing_tasks").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM resource_usage").fetchone()[0], 0)


if __name__ == "__main__": unittest.main()
