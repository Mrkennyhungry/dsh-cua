import unittest

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


if __name__ == "__main__":
    unittest.main()
