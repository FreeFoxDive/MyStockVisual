# -*- coding: utf-8 -*-
"""面板栏默认布局 + 面板设置服务器同步的前端静态/行为测试。

回归点:
  * 信息/五档/MA设置/金叉死叉 收进「▾ 更多」组 (extra-ind, 默认收起);
  * 金叉死叉默认开启;
  * 动力系统/通道/缺口 排在筹码之后、MACD 之前;
  * 面板设置默认跟账号同步, 且上传载荷剔除主题 (主题走共享本地键)。

运行:
    venv/Scripts/python.exe -u visual/test/test_panel_config_js.py
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

_VISUAL_DIR = Path(__file__).resolve().parents[1]
INDEX_HTML = _VISUAL_DIR / "static" / "index.html"


def _extract_fn(src: str, name: str) -> str:
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\(", src)
    if not m:
        raise AssertionError(f"index.html 中找不到 function {name}")
    start = src.index("{", m.end() - 1)
    depth = 0
    for i in range(start, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[m.start():i + 1]
    raise AssertionError(f"function {name} 大括号不配对")


class PanelBarStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = INDEX_HTML.read_text(encoding="utf-8")
        cls.bar = cls.src[cls.src.index('id="indicator-bar"'):cls.src.index('id="btn-ind-more"')]

    def test_more_group_contains_four_controls(self):
        for anchor in ('id="lbl-info"', 'id="lbl-depth"', 'id="cfg-ma"', 'id="chk-cross"'):
            self.assertIn(anchor, self.bar, f"{anchor} 应在「更多」按钮之前")
        # 折叠类挂在标签/按钮的起始标签上
        for anchor in ('id="lbl-info"', 'id="lbl-depth"'):
            tag_start = self.src.rindex("<label", 0, self.src.index(anchor))
            tag = self.src[tag_start:self.src.index(">", self.src.index(anchor))]
            self.assertIn("extra-ind", tag, f"{anchor} 应带 extra-ind")
        self.assertIn('class="cfg-mini extra-ind" id="cfg-ma"', self.src)

    def test_cross_default_checked(self):
        anchor = self.src.index('id="chk-cross"')
        tag = self.src[self.src.rindex("<input", 0, anchor):self.src.index(">", anchor)]
        self.assertIn("checked", tag, "金叉死叉 input 默认 checked")
        self.assertIn("cfg.cross !== false", _extract_fn(self.src, "applyConfig"))

    def test_overlays_after_chip_before_macd(self):
        chip = self.bar.index('id="lbl-chip"')
        self.assertLess(self.bar.index('id="chk-volume"'), chip)
        for anchor in ('id="lbl-impulse"', 'id="lbl-channel"', 'id="lbl-gap"'):
            self.assertGreater(self.bar.index(anchor), chip, f"{anchor} 应在筹码之后")
        self.assertLess(self.bar.index('id="lbl-gap"'), self.bar.index('id="chk-macd"'))

    def test_more_group_collapsed_by_default(self):
        # 默认 HTML 不带 show-extra; applyConfig 仅在 indMore===true 时展开
        anchor = self.src.index('id="indicator-bar"')
        open_tag = self.src[self.src.rindex("<div", 0, anchor):self.src.index(">", anchor)]
        self.assertNotIn("show-extra", open_tag)
        body = _extract_fn(self.src, "applyConfig")
        self.assertIn("if (cfg.indMore === true)", body)
        self.assertIn("show-extra", body)

    def test_config_functions_wired(self):
        save = _extract_fn(self.src, "saveConfig")
        self.assertIn("buildConfig()", save)
        self.assertIn("scheduleSavePanelConfig()", save)
        self.assertIn("scheduleSavePanelConfig", self.src)
        self.assertIn("/api/me/panel-config", self.src)
        push = _extract_fn(self.src, "pushPanelConfig")
        self.assertIn("panelCfgPayload", push)
        self.assertIn("syncPanelConfigWithServer", self.src)

    def test_startup_awaits_panel_sync(self):
        idx = self.src.index("syncPanelConfigWithServer().then")
        seg = self.src[idx - 300:idx + 400]
        self.assertIn("bootSync", seg, "启动应把面板同步纳入 bootSync")
        self.assertIn("syncHistoryWithServer", seg)

    def test_theme_excluded_from_payload(self):
        body = _extract_fn(self.src, "panelCfgPayload")
        self.assertIn("delete out.theme", body)
        self.assertIn("delete out.themeAuto", body)


@unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
class PanelPayloadBehaviorTest(unittest.TestCase):
    def test_payload_strips_theme(self):
        src = INDEX_HTML.read_text(encoding="utf-8")
        script = _extract_fn(src, "panelCfgPayload") + (
            "\nconst c = JSON.parse(process.argv[1]);"
            "process.stdout.write(JSON.stringify(panelCfgPayload(c)));"
        )
        payload = {"volume": True, "theme": "dark", "themeAuto": False,
                   "maPeriods": [5, 10, 20], "cross": True}
        proc = subprocess.run(["node", "-e", script, json.dumps(payload)],
                              capture_output=True, check=True)
        out = json.loads(proc.stdout.decode("utf-8"))
        self.assertNotIn("theme", out)
        self.assertNotIn("themeAuto", out)
        self.assertEqual(out["volume"], True)
        self.assertEqual(out["maPeriods"], [5, 10, 20])
        self.assertEqual(out["cross"], True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
