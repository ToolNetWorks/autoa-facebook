import json
import tempfile
import unittest
from pathlib import Path

from services.logging import (JsonlEventLogger, new_run_id, redact_sensitive,
                              RunLogger)


class LoggingTests(unittest.TestCase):
    def test_new_run_id_unique(self):
        self.assertNotEqual(new_run_id(), new_run_id())
        self.assertEqual(len(new_run_id()), 32)

    def test_redact_sensitive_dict(self):
        value = {"token": "secret123", "message": "Authorization: Bearer abc",
                 "nested": {"password": "pw", "safe": "ok"},
                 "list": [{"session": "sess"}]}
        redacted = redact_sensitive(value)
        self.assertEqual(redacted["token"], "[REDACTED]")
        self.assertIn("Bearer [REDACTED]", redacted["message"])
        self.assertEqual(redacted["nested"]["password"], "[REDACTED]")
        self.assertEqual(redacted["nested"]["safe"], "ok")
        self.assertEqual(redacted["list"][0]["session"], "[REDACTED]")

    def test_redact_cookie(self):
        self.assertEqual(redact_sensitive("c_user=123456; xs=abc"), "c_user=[REDACTED]; xs=[REDACTED]")

    def test_jsonl_logger_writes_and_redacts(self):
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "events.jsonl"
            logger = JsonlEventLogger(log_path, run_id="run-1")
            logger.emit("task_finished", status="failed",
                        token="top-secret", session_path="/private/session.json",
                        message="Authorization: Bearer super-secret")
            logger.close()
            lines = log_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            payload = json.loads(lines[0])
            self.assertEqual(payload["run_id"], "run-1")
            self.assertEqual(payload["event"], "task_finished")
            self.assertEqual(payload["token"], "[REDACTED]")
            self.assertEqual(payload["session_path"], "[REDACTED]")
            self.assertIn("Bearer [REDACTED]", payload["message"])
            self.assertNotIn("top-secret", lines[0])
            self.assertNotIn("super-secret", lines[0])
            self.assertIn("ts", payload)

    def test_run_logger(self):
        with tempfile.TemporaryDirectory() as td:
            logger = RunLogger(td, run_id="run-x", name="posting")
            logger.emit("post_started", job_id=1)
            logger.close()
            self.assertTrue((Path(td) / "posting.jsonl").exists())
