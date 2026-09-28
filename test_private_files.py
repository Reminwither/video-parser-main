"""Ownership checks for Gradio file URLs and video state."""

import asyncio
import unittest
from unittest.mock import patch

import auth_middleware


class PrivateFileTests(unittest.TestCase):
    def test_private_output_ownership(self):
        url = "/gradio_api/file=/tmp/gradio/abc/vp-u12-report_asr.txt"
        with patch.object(auth_middleware.auth_db, "uploaded_file_access", return_value=None):
            self.assertTrue(auth_middleware._file_access_allowed(url, 12))
            self.assertFalse(auth_middleware._file_access_allowed(url, 13))
            self.assertFalse(auth_middleware._file_access_allowed(
                "/gradio_api/file=/tmp/gradio/abc/reports/old_asr.txt", 12
            ))

    def test_raw_upload_ownership(self):
        url = "/gradio_api/file=/tmp/gradio/abc/original.mp4"
        with patch.object(auth_middleware.auth_db, "uploaded_file_access", return_value=False):
            self.assertFalse(auth_middleware._file_access_allowed(url, 13))
        with patch.object(auth_middleware.auth_db, "uploaded_file_access", return_value=True):
            self.assertTrue(auth_middleware._file_access_allowed(url, 12))

    def test_upload_response_registers_owner(self):
        async def downstream(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b'["/tmp/gradio/abc/original.mp4"]'})

        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "method": "POST", "path": "/gradio_api/upload", "headers": []}
        with patch.object(auth_middleware.auth_db, "get_session_user", return_value={"id": 12}), \
             patch.object(auth_middleware, "_get_cookie", return_value="token"), \
             patch.object(auth_middleware, "_upload_response_paths", return_value=["/tmp/gradio/abc/original.mp4"]), \
             patch.object(auth_middleware.auth_db, "register_uploaded_files") as register, \
             patch.object(auth_middleware.auth_db, "consume_daily_quotas", return_value=True):
            asyncio.run(auth_middleware.SessionAuthMiddleware(downstream)(scope, receive, send))
        register.assert_called_once_with(12, ["/tmp/gradio/abc/original.mp4"])
        self.assertEqual(sent[0]["status"], 200)


if __name__ == "__main__":
    unittest.main()
