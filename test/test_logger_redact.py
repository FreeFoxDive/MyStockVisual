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
    mask_value,
    redact_message,
    sanitize_error,
    _RedactFilter,
)


class TestMaskValue(unittest.TestCase):
    def test_short_all_stars(self):
        self.assertEqual(mask_value("abc"), "***")
        self.assertEqual(mask_value("1234567"), "***")

    def test_head_tail(self):
        s = "abcdefghijklmnop"
        self.assertEqual(mask_value(s), "abcd***mnop")

    def test_none_empty(self):
        self.assertEqual(mask_value(None), "[REDACTED]")
        self.assertEqual(mask_value(""), "[REDACTED]")


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

    def test_mairui_uuid_licence_any_endpoint(self):
        """licence 是 UUID, 出现在 hsindex/himk/hsstock 等路径末段, 也必须脱敏。"""
        lic = "D4C8639C-F318-41A2-9B89-E4AB053B4ADE"
        for url in (
            f"https://api.mairuiapi.com/hsindex/list/{lic}",
            f"https://api.mairuiapi.com/himk/roe/{lic}",
            f"https://api.mairuiapi.com/hsstock/instrument/000001.SZ/{lic}",
            f"https://api.mairuiapi.com/hsstock/announcement/000001/{lic}?lt=20",
        ):
            out = redact_message(f"HTTP 429: {url} (2514ms)")
            self.assertNotIn(lic, out, url)
            self.assertIn("D4C8***4ADE", out)

    def test_mairui_payload_dict_masked(self):
        """429 的 payload 可能回显 licence (键名 lid); dict repr 也要脱敏。"""
        lic = "D4C8639C-F318-41A2-9B89-E4AB053B4ADE"
        out = redact_message({"code": 103, "msg": "too many source ips", "lid": lic})
        self.assertNotIn(lic, out)
        self.assertIn("D4C8***4ADE", out)

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

    def test_mairui_429_is_rate_limit_not_auth(self):
        """麦蕊 429 消息含 api.mairuiapi.com, 不能因裸 'api' 被判成密钥错误。"""
        lic = "D4C8639C-F318-41A2-9B89-E4AB053B4ADE"
        msg = f"HTTP 429: https://api.mairuiapi.com/himk/roe/{lic} (2514ms)"
        self.assertEqual(sanitize_error(msg), "请求过于频繁，请稍后重试")

    def test_mairui_403_is_auth(self):
        msg = "鉴权失败 HTTP 403: 请检查 licence 是否有效 / 是否欠费限流"
        self.assertEqual(sanitize_error(msg), "服务暂不可用，请稍后重试")

    def test_404_with_429_stock_code_not_rate_limited(self):
        """404 消息里的 000429.SZ 不应被当成 HTTP 429。"""
        out = sanitize_error("HTTP 404: https://api.mairuiapi.com/hsstock/history/000429.SZ/d/n/KEY")
        self.assertNotEqual(out, "请求过于频繁，请稍后重试")


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
