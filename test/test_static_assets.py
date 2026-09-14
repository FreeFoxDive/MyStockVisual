"""静态资源: 第三方库自托管 + 缓存分流 + CSP 第三方源清零。

不触网、不碰数据源, 只 GET 静态路径 / 读磁盘文件。
"""
from __future__ import annotations

import hashlib
import re
import sys
import tempfile
import unittest
from pathlib import Path

_VISUAL = Path(__file__).resolve().parents[1]
if str(_VISUAL) not in sys.path:
    sys.path.insert(0, str(_VISUAL))

STATIC_DIR = _VISUAL / "static"
VENDOR_DIR = STATIC_DIR / "vendor"
ECHARTS_NAME = "echarts-5.5.0.min.js"
VENDOR_URL = f"/vendor/{ECHARTS_NAME}"


class StaticAssetsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        import trades
        cls._orig_db = trades._db_path
        trades.init_db(Path(cls._tmpdir.name) / "static_assets.db")

        from app import create_app
        cls.client = create_app().test_client()

    @classmethod
    def tearDownClass(cls):
        import trades
        trades._db_path = cls._orig_db
        cls._tmpdir.cleanup()

    def _get(self, path):
        # 静态响应是文件包装: 不显式 close 会留下 ResourceWarning
        r = self.client.get(path)
        self.addCleanup(r.close)
        return r

    def test_vendored_echarts_immutable_and_matches_readme(self):
        r = self._get(VENDOR_URL)
        self.assertEqual(r.status_code, 200)
        self.assertIn("javascript", r.headers["Content-Type"])
        self.assertEqual(
            r.headers["Cache-Control"], "public, max-age=31536000, immutable"
        )

        # vendor/README.md 是来源台账: 字节数与 sha256 必须与磁盘文件一致
        ledger = (VENDOR_DIR / "README.md").read_text(encoding="utf-8")
        want_sha = re.search(r"\b([0-9a-f]{64})\b", ledger).group(1)
        want_size = int(
            re.search(r"\|\s*字节数\s*\|\s*([\d,]+)", ledger).group(1).replace(",", "")
        )
        body = r.get_data()
        self.assertEqual(len(body), want_size)
        self.assertEqual(hashlib.sha256(body).hexdigest(), want_sha)
        self.assertIn(b'version="5.5.0"', body)

    def test_app_js_still_no_store(self):
        r = self._get("/js/api.js")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["Cache-Control"], "no-store")

    def test_immutable_rule_only_matches_versioned_names(self):
        from app import _IMMUTABLE_VENDOR

        self.assertIsNotNone(_IMMUTABLE_VENDOR.match(f"vendor/{ECHARTS_NAME}"))
        for rel in (
            "vendor/echarts.min.js",
            "vendor/echarts-5.5.0.min.js.map",
            "js/api.js",
            "index.html",
            "vendor/README.md",
        ):
            self.assertIsNone(_IMMUTABLE_VENDOR.match(rel), rel)

    def test_no_external_script_or_resource_origin(self):
        for page in sorted(STATIC_DIR.glob("*.html")):
            text = page.read_text(encoding="utf-8")
            self.assertNotRegex(text, r'<script[^>]+src="https?://', page.name)
            self.assertNotIn('src="http', text, page.name)

    def test_chart_pages_use_vendored_echarts(self):
        for page in ("index.html", "trades.html"):
            text = (STATIC_DIR / page).read_text(encoding="utf-8")
            self.assertIn(f'src="{VENDOR_URL}"', text, page)
            self.assertNotIn("cdn.jsdelivr.net", text, page)

    def test_csp_has_no_third_party_origin(self):
        r = self._get("/login.html")
        csp = r.headers["Content-Security-Policy"]
        self.assertNotIn("http", csp)
        self.assertIn("script-src 'self' 'unsafe-inline'", csp)


if __name__ == "__main__":
    unittest.main()
