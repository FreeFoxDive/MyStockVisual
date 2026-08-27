"""logger 密钥/密码脱敏单测。"""
from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path

VISUAL = Path(__file__).resolve().parents[1]
if str(VISUAL) not in sys.path:
    sys.path.insert(0, str(VISUAL))

from logger import (  # noqa: E402
    mask_secret,
    redact_message,
    sanitize_error,
    _RedactFilter,
)


class TestMaskSecret(unittest.TestCase):
    def test_short_all_stars(self):
        self.assertEqual(mask_secret("abc"), "***")
        self.assertEqual(mask_secret("1234567"), "***")

    def test_head_tail(self):
        s = "abcdefghijklmnop"
        self.assertEqual(mask_secret(s), "abcd***mnop")

    def test_none_empty(self):
        self.assertEqual(mask_secret(None), "[REDACTED]")
        self.assertEqual(mask_secret(""), "[REDACTED]")


class TestRedactMessage(unittest.TestCase):
    def test_password_redacted(self):
        out = redact_message("login password=hunter2ok")
        self.assertIn("[REDACTED]", out)
        self.assertNotIn("hunter2ok", out)

    def test_api_key_masked(self):
        key = "ABCDEFGH12345678XYZ"
        out = redact_message(f"AF_API_KEY={key} loaded")
        self.assertNotIn(key, out)
        self.assertIn("ABCD***", out)
        self.assertIn("8XYZ", out)

    def test_url_access_token(self):
        tok = "tok_abcdefghijklmnop"
        url = f"https://oapi.dingtalk.com/robot/send?access_token={tok}&timestamp=1"
        out = redact_message(f"推送失败: {url}")
        self.assertNotIn(tok, out)
        self.assertIn("access_token=", out)
        self.assertIn("***", out)

    def test_mairui_path_licence(self):
        lic = "ABCDEF0123456789abcdef01"
        url = f"https://api.mairuiapi.com/jj/lskx/510300/d/{lic}"
        out = redact_message(f"麦蕊失败 {url}")
        self.assertNotIn(lic, out)
        self.assertIn("ABCD***", out)

    def test_plain_text_unchanged(self):
        msg = "持仓监控线程已启动"
        self.assertEqual(redact_message(msg), msg)


class TestSanitizeError(unittest.TestCase):
    def test_rate_limit(self):
        self.assertEqual(sanitize_error("Rate limit exceeded"), "请求过于频繁，请稍后重试")

    def test_token_keyword(self):
        self.assertEqual(sanitize_error("invalid api key"), "服务暂不可用，请稍后重试")

    def test_generic_redacted(self):
        key = "ABCDEFGH12345678XYZ"
        out = sanitize_error(f"upstream failed api_key={key}")
        # keyword path → generic unavailable
        self.assertEqual(out, "服务暂不可用，请稍后重试")


class TestRedactFilter(unittest.TestCase):
    def test_filter_mutates_record(self):
        f = _RedactFilter()
        rec = logging.LogRecord(
            name="t", level=logging.INFO, pathname="", lineno=0,
            msg="token=abcdefghijklmnop", args=(), exc_info=None,
        )
        self.assertTrue(f.filter(rec))
        self.assertNotIn("abcdefghijklmnop", rec.getMessage())
        self.assertIn("***", rec.getMessage())


if __name__ == "__main__":
    unittest.main()
