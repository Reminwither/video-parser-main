"""Focused checks for provider usage counters and request wrappers."""

import os
import sqlite3
import tempfile
import threading
import unittest
import wave
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import auth_db
import tencent_flash_asr
import video_analysis


class ResourceUsageTests(unittest.TestCase):
    def test_db_aggregates_units_without_request_content(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(auth_db, "DB_PATH", str(Path(directory) / "auth.db")), \
             patch.object(auth_db, "_DB_LOCAL", threading.local()), \
             patch.object(auth_db, "seed_admin_if_empty"):
            auth_db.init_db()
            auth_db.record_resource_usage("model-api", "vision", "test-model", input_tokens=13, output_tokens=5)
            auth_db.record_resource_usage("model-api", "vision", "test-model", input_tokens=7, output_tokens=2)
            auth_db.record_resource_usage("tencent-asr", "flash-transcribe", "16k_zh", submitted_audio_ms=1250)
            with closing(sqlite3.connect(auth_db.DB_PATH)) as check:
                row = check.execute(
                    "SELECT calls, input_tokens, output_tokens FROM resource_usage "
                    "WHERE provider='model-api' AND operation='vision'"
                ).fetchone()
                self.assertEqual(row, (2, 20, 7))
                audio = check.execute(
                    "SELECT submitted_audio_ms FROM resource_usage WHERE provider='tencent-asr'"
                ).fetchone()
                self.assertEqual(audio, (1250,))
            auth_db._DB_LOCAL.conn.close()

    def test_model_wrapper_records_reported_tokens(self):
        response = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=21, completion_tokens=9))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_: response)))
        with patch.object(video_analysis, "record_resource_usage") as record:
            self.assertIs(video_analysis._model_completion(client, "vision", model="test-model"), response)
        record.assert_called_once_with("model-api", "vision", "test-model", input_tokens=21, output_tokens=9)

    def test_flash_records_submitted_wav_duration(self):
        with tempfile.TemporaryDirectory() as directory:
            audio_path = str(Path(directory) / "audio.wav")
            with wave.open(audio_path, "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(16000)
                audio.writeframes(b"\0\0" * 16000)
            response = SimpleNamespace(raise_for_status=lambda: None,
                                       json=lambda: {"code": 0, "flash_result": []})
            env = {"TENCENT_ASR_APP_ID": "123", "TENCENT_ASR_SECRET_ID": "id",
                   "TENCENT_ASR_SECRET_KEY": "key"}
            with patch.dict(os.environ, env), \
                 patch.object(tencent_flash_asr.requests, "post", return_value=response), \
                 patch.object(tencent_flash_asr, "record_resource_usage") as record:
                self.assertEqual(tencent_flash_asr.transcribe(audio_path), [])
            self.assertEqual(record.call_args.kwargs["submitted_audio_ms"], 1000)


if __name__ == "__main__":
    unittest.main()
