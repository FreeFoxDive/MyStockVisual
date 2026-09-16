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


def _extract_const(src: str, name: str) -> str:
    """抽取 `const NAME = ...;` (数组字面量按方括号配平, 其余取到分号)。"""
    m = re.search(r"const\s+" + re.escape(name) + r"\s*=\s*", src)
    if not m:
        raise AssertionError(f"index.html 中找不到 const {name}")
    if src[m.end()] == "[":
        start = m.end()
        depth = 0
        for i in range(start, len(src)):
            if src[i] == "[":
                depth += 1
            elif src[i] == "]":
                depth -= 1
                if depth == 0:
                    return src[m.start():i + 1] + ";"
        raise AssertionError(f"const {name} 方括号不配对")
    return src[m.start():src.index(";", m.end()) + 1]


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

    def test_gap_atr_patterns_folded_into_more(self):
        """缺口/ATR/K线形态收进「▾ 更多」: 只收控件, 渲染照旧 (勾选状态仍生效)。"""
        for anchor in ('id="lbl-gap"', 'id="chk-atr"', 'id="chk-patterns"'):
            i = self.src.index(anchor)
            tag_start = self.src.rindex("<label", 0, i)
            tag = self.src[tag_start:self.src.index(">", i)]
            self.assertIn("extra-ind", tag, f"{anchor} 应带 extra-ind")
        # DOM 次序不动: 缺口仍在筹码之后、MACD 之前 (上一测试的排序契约)
        self.assertLess(self.bar.index('id="lbl-gap"'), self.bar.index('id="chk-macd"'))
        # 折叠纯显示层: 缺口闸门与形态标注仍读勾选状态
        self.assertIn("chk-gap", self.src)
        self.assertIn("chk-patterns", self.src)

    def test_rsi_kdj_folded_by_period(self):
        """周K及以上折 KDJ、日K及以下折 RSI (只折勾选框)。"""
        for anchor in ('id="lbl-kdj"', 'id="lbl-rsi"'):
            i = self.src.index(anchor)
            head = self.src[self.src.rindex("<label", 0, i):i]
            self.assertNotIn(">", head, f"{anchor} 必须是 label 起始标签上的 id")
            # 折叠靠内联 display, 不能同时挂 extra-ind (两套显隐会互相覆盖)
            tag = self.src[self.src.rindex("<label", 0, i):self.src.index(">", i)]
            self.assertNotIn("extra-ind", tag)
        body = _extract_fn(self.src, "applyPeriodUI")
        self.assertIn("weekPlus", body)
        self.assertIn("isWeeklyPlus(period)", body)
        helper = _extract_fn(self.src, "isWeeklyPlus")
        self.assertIn("'1w'", helper)
        self.assertIn("'1M'", helper)
        self.assertNotIn("'1d'", helper, "日K及以下折 RSI, 不能把日K算进「周K及以上」")

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

    def test_phone_checks_stored_separately(self):
        """手机: 勾选态另存一份本地键, 共享键保留其他设备的勾选; 桌面路径逐字不变。"""
        self.assertIn("const CFG_KEY_PHONE = 'visual_chart_config_phone';", self.src)
        self.assertIn("'visual_chart_config_phone'", _extract_fn(self.src, "splitCfg") + self.src.split("LS_KEEP_KEYS", 1)[1][:120],
                      "手机键要进 LS_KEEP_KEYS, 免得被缓存淘汰清掉")
        save = _extract_fn(self.src, "saveConfigParts")
        self.assertIn("isPhoneUi()", save)
        self.assertIn("localStorage.setItem(CFG_KEY_PHONE", save)
        # 手机写共享键时必须先读旧值再合并 → 不抹掉桌面写在里面的勾选
        self.assertIn("Object.assign(readCfg(CFG_KEY), rest)", save)
        load = _extract_fn(self.src, "loadConfig")
        self.assertIn("localChecksCfg()", load)
        self.assertIn("checkDefaults()", load)
        self.assertIn("splitCfg(readCfg(CFG_KEY)).rest", load,
                      "共享键的勾选必须切掉, 否则手机首用会吃进桌面的 info:true")
        self.assertIn("return isPhoneUi() ? { info: false } : {};",
                      _extract_fn(self.src, "checkDefaults"), "手机首用: 基本信息默认折叠")
        # 手机: 勾选 = 设备默认 < 本机手机键, 永不吃服务端/共享键的勾选
        self.assertIn("checkDefaults(), localChecksCfg()", _extract_fn(self.src, "syncPanelConfigWithServer"))

    def test_phone_sync_keeps_other_devices_checks(self):
        """服务端 PUT 是整包覆盖: 手机上必须带上已知勾选, 否则会清掉其他设备的勾选。"""
        push = _extract_fn(self.src, "pushPanelConfig")
        self.assertIn("isPhoneUi()", push)
        self.assertIn("splitCfg(buildConfig()).rest", push, "只上传非勾选字段")
        self.assertIn("STATE._panelCfgChecks", push, "勾选沿用服务端最近给的值")
        self.assertIn("splitCfg(readCfg(CFG_KEY)).checks", push, "拿不到时退回共享键里的勾选")
        pull = _extract_fn(self.src, "syncPanelConfigWithServer")
        self.assertIn("STATE._panelCfgChecks = splitCfg(server).checks", pull)
        self.assertIn("localChecksCfg()", pull, "手机上勾选以本机为准")

    def test_phone_info_panel_default_folded(self):
        body = _extract_fn(self.src, "applyConfig")
        self.assertIn("cfg.info === undefined ? !isPhoneUi() : cfg.info !== false", body,
                      "没存过配置时手机默认折叠基本信息, 桌面默认展开")


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


@unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
class SplitCfgBehaviorTest(unittest.TestCase):
    """splitCfg: 勾选态与非勾选态切分 (手机另存勾选的前提)。"""

    @classmethod
    def setUpClass(cls):
        cls.src = INDEX_HTML.read_text(encoding="utf-8")

    def _run(self, cfg):
        script = (
            _extract_const(self.src, "CFG_CHECK_KEYS") + "\n"
            + _extract_fn(self.src, "splitCfg") + "\n"
            + "process.stdout.write(JSON.stringify(splitCfg(JSON.parse(process.argv[1]))));"
        )
        proc = subprocess.run(["node", "-e", script, json.dumps(cfg)],
                              capture_output=True, check=True)
        return json.loads(proc.stdout.decode("utf-8"))

    def test_splits_checks_from_rest(self):
        out = self._run({"volume": False, "rsi": True, "info": False, "adjust": "hfq",
                         "maPeriods": [5, 8, 13], "indMore": True, "theme": "dark"})
        self.assertEqual(out["checks"], {"volume": False, "rsi": True, "info": False})
        self.assertEqual(out["rest"], {"adjust": "hfq", "maPeriods": [5, 8, 13],
                                       "indMore": True, "theme": "dark"})

    def test_covers_every_checkbox_in_build_config(self):
        """buildConfig 里的勾选字段必须都在 CFG_CHECK_KEYS 里, 否则手机的独立缓存会漏项。"""
        build = _extract_fn(self.src, "buildConfig")
        bool_keys = set(re.findall(
            r"^\s*(\w+):\s*document\.getElementById\('(?:chk|tip)-", build, re.M))
        self.assertTrue(bool_keys, "buildConfig 里没找到勾选字段?")
        check_keys = set(re.findall(r"'(\w+)'", _extract_const(self.src, "CFG_CHECK_KEYS")))
        missing = bool_keys - check_keys
        self.assertFalse(missing, f"CFG_CHECK_KEYS 漏了勾选字段: {sorted(missing)}")

    def test_empty_cfg_yields_empty_parts(self):
        self.assertEqual(self._run({}), {"checks": {}, "rest": {}})


if __name__ == "__main__":
    unittest.main(verbosity=2)
