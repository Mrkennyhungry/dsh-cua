import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dsh_desktop import knowledge
from dsh_desktop.knowledge import _redact_secrets


class RedactSecretsTests(unittest.TestCase):
    def assertMasked(self, text, secret):
        redacted = _redact_secrets(text)
        self.assertNotIn(secret, redacted, redacted)
        self.assertIn("***", redacted)

    def test_key_value_credentials_are_masked(self):
        self.assertMasked("DEEPSEEK_API_KEY=abc123def456", "abc123def456")
        self.assertMasked("CONSOLE_ADMIN_PASSWORD: Hunter2!x", "Hunter2!x")
        self.assertMasked("db_passwd = s3cretValue", "s3cretValue")
        self.assertMasked("登录密码：Pa55word9", "Pa55word9")

    def test_json_credentials_are_masked(self):
        self.assertMasked('{"password": "p@ssw0rd-1", "user": "alice"}', "p@ssw0rd-1")
        self.assertMasked("{'access_token': 'tok_0123456789'}", "tok_0123456789")

    def test_authorization_headers_are_masked(self):
        self.assertMasked("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload", "eyJhbGciOiJIUzI1NiJ9")
        self.assertMasked("curl -H 'Authorization: Basic dXNlcjpwYXNzd29yZA=='", "dXNlcjpwYXNzd29yZA==")

    def test_bare_key_literals_are_masked(self):
        self.assertMasked("key is sk-abcdefghijklmnopqrstuvwx", "sk-abcdefghijklmnopqrstuvwx")
        self.assertMasked("ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345", "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345")

    def test_ordinary_text_is_kept(self):
        text = "打开记事本，输入 hello，然后读回窗口标题 user=alice"
        self.assertEqual(_redact_secrets(text), text)
        self.assertEqual(_redact_secrets(""), "")
        self.assertEqual(_redact_secrets(None), "")


class WrittenRecordTests(unittest.TestCase):
    """What matters is what reaches the disk, on every path that writes a record."""

    TYPED = "TypedPassw0rd"
    FAILURE = "FailureT0ken123456"
    FROM_MODEL = "ModelEch0ed"
    STEPS = [
        {"name": "desktop_click", "args": {"x": 1, "y": 2}, "ok": True},
        {"name": "desktop_type_text", "args": {"text": f"password={TYPED}"}, "ok": True},
        {"name": "web_click", "args": {"selector": "#go"}, "ok": False,
         "error": f"Authorization: Bearer {FAILURE}"},
    ]

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        patcher = mock.patch.dict(os.environ, {"DSH_DESKTOP_HOME": temp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def records(self):
        return [path for path in (self.home / "knowledge").rglob("*.md")]

    def assertNoSecretOnDisk(self):
        records = self.records()
        self.assertTrue(records, "a record should have been written")
        for path in records:
            text = path.read_text(encoding="utf-8") + path.name
            for secret in (self.TYPED, self.FAILURE, self.FROM_MODEL):
                self.assertNotIn(secret, text, path.name)

    def test_raw_record_masks_step_arguments_and_errors(self):
        for outcome in ("succeeded", "failed", "unknown"):
            with self.subTest(outcome=outcome):
                knowledge.record_run("s-1", f"{outcome} task", self.STEPS,
                                     success=outcome == "succeeded", outcome=outcome)
                self.assertNoSecretOnDisk()

    def test_review_masks_model_text_and_is_always_a_draft(self):
        block = json.dumps([{"title": f"pwd={self.FROM_MODEL}", "trigger": f"token={self.FROM_MODEL}",
                             "steps": [f"password={self.FROM_MODEL}"], "solution": f"secret={self.FROM_MODEL}"}])
        review = f"passwd={self.FROM_MODEL}\n```json\n{block}\n```"
        with mock.patch.object(knowledge, "_call_review_llm", return_value=review):
            for outcome in ("succeeded", "failed", "unknown"):
                with self.subTest(outcome=outcome):
                    result = knowledge.review_run("s-2", f"{outcome} task", self.STEPS,
                                                  success=outcome == "succeeded", outcome=outcome,
                                                  llm=("http://unused", "key", "model"))
                    self.assertEqual(result.get("mode"), "llm")
                    self.assertNoSecretOnDisk()
        self.assertTrue(all(path.name.startswith("draft_") for path in self.records()))


if __name__ == "__main__":
    unittest.main()
