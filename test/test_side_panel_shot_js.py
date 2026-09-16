# -*- coding: utf-8 -*-
"""侧栏截图回归测试: 导出的 PNG 必须带上基本信息 (含代码/名称) 与五档。

回归点:
  * 侧栏是 DOM, 之前只导出 ECharts + 画线层两个 canvas → 基本信息从不出现在图片里,
    而 grid.left 又已为面板让位, 所以导出图左侧是一条空白;
  * 面板 DOM 与截图各取一次数会漂移 → 两者必须共用 infoPanelRows/depthPanelRows;
  * 收起侧栏时不应画面板; 面板内容超过可见高度时裁剪, 不缩放变形;
  * 值右对齐且超宽省略, 不能溢出到面板外。

运行:
    venv/Scripts/python.exe -u visual/test/test_side_panel_shot_js.py
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

_VISUAL_DIR = Path(__file__).resolve().parents[1]
INDEX_HTML = _VISUAL_DIR / "static" / "index.html"
LIVE_MARKET_JS = _VISUAL_DIR / "static" / "js" / "live-market.js"


def _extract_fn(src: str, name: str) -> str:
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", src)
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
    m = re.search(r"^const\s+" + re.escape(name) + r"\s*=", src, re.MULTILINE)
    if not m:
        raise AssertionError(f"index.html 中找不到 const {name}")
    start = src.index("{", m.end())
    depth = 0
    for i in range(start, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[m.start():i + 1] + ";"
    raise AssertionError(f"const {name} 大括号不配对")


def _run_script(script: str, payload=None):
    argv = ["node", "-e", script] + ([json.dumps(payload)] if payload is not None else [])
    proc = subprocess.run(argv, capture_output=True, check=True)
    return json.loads(proc.stdout.decode("utf-8"))


class SidePanelShotStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = INDEX_HTML.read_text(encoding="utf-8")

    def test_shot_composites_panel_not_only_canvases(self):
        body = _extract_fn(self.src, "saveChartImage")
        self.assertIn("sidePanelOn()", body, "收起侧栏时不应画面板")
        self.assertIn("sidePanelShotModel", body)
        self.assertIn("drawSidePanelShot", body)
        # 只有画线层没有像素时也必须进合成分支, 否则侧栏永远进不了图片
        self.assertIn("!hasOverlay && !panel", body)
        # 顺序: 底图 → 画线层 → 侧栏 (侧栏画在最后才能盖住左侧留白)
        self.assertLess(body.index("hasOverlay) ctx.drawImage(overlay"), body.index("drawSidePanelShot"))

    def test_rows_shared_between_dom_and_shot(self):
        # 面板 HTML 与截图共用行规格, 否则截图会缺行/值不同步
        self.assertIn("infoPanelRows", _extract_fn(self.src, "renderStockInfo"))
        model = _extract_fn(self.src, "sidePanelShotModel")
        self.assertIn("infoPanelRows", model)
        self.assertIn("depthPanelRows", model)
        self.assertIn("depthPanelRows", _extract_fn(self.src, "renderDepth"))

    def test_panel_rows_escape_html(self):
        html = _extract_fn(self.src, "stockInfoRowHtml")
        self.assertIn("escHtml(r.text)", html)
        self.assertIn("escHtml(r.label)", html)

    def test_scale_follows_export_ratio(self):
        body = _extract_fn(self.src, "saveChartImage")
        self.assertIn("cv.width / Math.max(1, cssW)", body, "比例须按实际导出像素算, 不能写死 2")
        self.assertNotIn("pixelRatio: 2", body)


class SidePanelShotModelTest(unittest.TestCase):
    """真实 infoPanelRows/depthPanelRows/sidePanelShotModel 的静态模型镜像。"""

    @classmethod
    def setUpClass(cls):
        src = INDEX_HTML.read_text(encoding="utf-8")
        live = LIVE_MARKET_JS.read_text(encoding="utf-8")
        cls.script = (
            _extract_const(src, "SIDE_SHOT_LAYOUT") + "\n"
            + _extract_fn(live, "price") + "\n"
            + _extract_fn(src, "fmtPrice3") + "\n"
            + _extract_fn(src, "fmtCN") + "\n"
            + _extract_fn(src, "fmtLots") + "\n"
            + _extract_fn(src, "normPct") + "\n"
            + _extract_fn(src, "panelStockName") + "\n"
            + _extract_fn(src, "panelStockCode") + "\n"
            + _extract_fn(src, "infoPanelRows") + "\n"
            + _extract_fn(src, "depthPanelRows") + "\n"
            + _extract_fn(src, "fitShotText") + "\n"
            + _extract_fn(src, "drawSidePanelShot") + "\n"
            + _extract_fn(src, "sidePanelShotModel") + "\n"
            + """const STATE = { symbol: '000001.SZ', klineData: null, stockInfo: null, depth: null };
const THEME = { up: '#ef232a', down: '#14b143', muted: '#6c757d', text: '#212529',
                border: '#e9ecef', panelBg: '#f8f9fa' };
function C() { return THEME; }
let _infoOn = true, _depthOn = false;
function infoPanelOn() { return _infoOn; }
function depthSecOn() { return _depthOn; }
VisualLive = { price: price };

// 录制型 canvas 2d 上下文: measureText 用当前 font 的字号估算宽度
function recCtx() {
  const calls = [];
  const ctx = {
    font: '', fillStyle: '', textAlign: '', _clip: 0, calls,
    save() { calls.push({ op: 'save' }); },
    restore() { calls.push({ op: 'restore' }); },
    beginPath() { calls.push({ op: 'beginPath' }); },
    rect(x, y, w, h) { calls.push({ op: 'rect', x, y, w, h }); },
    clip() { calls.push({ op: 'clip' }); this._clip += 1; },
    fillRect(x, y, w, h) { calls.push({ op: 'rule', x, y, w, h, color: this.fillStyle }); },
    fillText(t, x, y) {
      calls.push({ op: 'text', t, x, y, color: this.fillStyle, font: this.font, align: this.textAlign });
    },
    measureText(s) {
      // 取 font 里的 px 字号 (不能 parseFloat 整个 font: "700 12px monospace" 会取到 700)
      const m = /(\\d+(?:\\.\\d+)?)px/.exec(this.font);
      return { width: String(s).length * Number(m ? m[1] : 12) * 0.6 };
    },
  };
  return ctx;
}

const INFO = { symbol: '000001.SZ', name: '平安银行', last_price: 11.5, prev_close: 11.2,
  industry: '银行', volume: 123456, amount: 1.4e8, turnover_rate: 0.51, vol_ratio: 1.2,
  limit_up: 12.32, limit_down: 10.08, chg_3d: 1.5, chg_5d: -2.25, chg_10d: 0, pe: 5.5, pb: 0.55,
  trade_status: 'trading', trade_status_text: '连续竞价' };
const DEPTH = { ask_prices: [11.6, 11.59, 11.58, 11.57, 11.56], ask_volumes: [10, 20, 30, 40, 50],
  bid_prices: [11.5, 11.49, 11.48, 11.47, 11.46], bid_volumes: [11, 21, 31, 41, 51] };

function collect(step) {
  _infoOn = step.info !== false;
  _depthOn = !!step.depth;
  STATE.stockInfo = step.stockInfo === undefined ? INFO : step.stockInfo;
  STATE.depth = step.depthData === undefined ? DEPTH : step.depthData;
  const ctx = recCtx();
  const model = sidePanelShotModel(step.width, step.height);
  drawSidePanelShot(ctx, model, step.scale || 1);
  return {
    model: { width: model.width, height: model.height, content: model.contentHeight, bg: model.bg,
             texts: model.texts, rules: model.rules, texts_n: model.texts.length },
    drawn: ctx.calls.filter(c => c.op === 'text'),
    rules: ctx.calls.filter(c => c.op === 'rule'),
    clip: ctx._clip,
  };
}
process.stdout.write(JSON.stringify(collect(JSON.parse(process.argv[1]))));"""
        )

    def _collect(self, step):
        return _run_script(self.script, step)

    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
    def test_code_and_name_rows_on_top(self):
        out = self._collect({"width": 204, "height": 720})
        texts = [t["t"] for t in out["drawn"]]
        order = [texts.index(x) for x in ("基本信息", "名称", "平安银行", "代码", "000001.SZ", "现价")]
        self.assertEqual(order, sorted(order), f"名称/代码须在现价之前: {texts[:8]}")
        # 代码取完整 symbol (含交易所后缀)
        self.assertIn("000001.SZ", texts)

    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
    def test_label_left_and_value_right_aligned(self):
        out = self._collect({"width": 204, "height": 720})
        drawn = out["drawn"]
        label = next(t for t in drawn if t["t"] == "名称")
        self.assertEqual(label["x"], 8, "标签贴左内边距")
        self.assertEqual(label["align"], "left")
        value = next(t for t in drawn if t["t"] == "平安银行")
        self.assertEqual(value["x"], 204 - 8, "数值右对齐到面板右内边距")
        self.assertEqual(value["align"], "right")
        self.assertEqual(value["color"], "#212529", "默认前景色")
        # 颜色随涨跌: 现价 11.5 > 昨收 11.2 → 涨色
        self.assertEqual(next(t for t in drawn if t["t"] == "11.5")["color"], "#ef232a")
        self.assertEqual(next(t for t in drawn if t["t"] == "-2.25%")["color"], "#14b143")

    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
    def test_name_falls_back_to_kline(self):
        out = self._collect({"width": 204, "height": 720, "stockInfo": {"symbol": "600000.SH"}})
        texts = [t["t"] for t in out["drawn"]]
        self.assertIn("600000.SH", texts)
        self.assertIn("—", texts, "无 name 时占位, 而不是空白行")

    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
    def test_no_data_placeholder(self):
        out = self._collect({"width": 204, "height": 720, "stockInfo": None})
        texts = [t["t"] for t in out["drawn"]]
        self.assertIn("无数据", texts)
        self.assertNotIn("现价", texts, "无数据时不应残留字段名")
        self.assertEqual(next(t for t in out["drawn"] if t["t"] == "无数据")["align"], "center")

    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
    def test_depth_drawn_only_when_enabled(self):
        off = self._collect({"width": 204, "height": 720, "depth": False})
        self.assertNotIn("卖5", [t["t"] for t in off["drawn"]])
        on = self._collect({"width": 204, "height": 720, "depth": True})
        texts = [t["t"] for t in on["drawn"]]
        self.assertIn("档位", texts)
        asks = [texts.index("卖" + str(i)) for i in (5, 4, 1)]
        self.assertEqual(asks, sorted(asks), "卖盘从卖5 递减到卖1")
        self.assertLess(texts.index("卖1"), texts.index("买1"))
        self.assertGreater(len(on["rules"]), len(off["rules"]), "五档多出分隔线")
        self.assertEqual(next(t for t in on["drawn"] if t["t"] == "11.6")["color"], "#14b143",
                         "卖盘用跌色")

    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
    def test_lines_do_not_overlap_and_fit(self):
        out = self._collect({"width": 204, "height": 720})
        ys = [t["y"] for t in out["drawn"]]
        self.assertEqual(ys, sorted(ys), "行基线必须单调下行")
        self.assertLess(out["model"]["content"], 720, "内容高度须在面板内 (否则被裁掉)")
        self.assertEqual(out["clip"], 1, "超长内容按面板高度裁剪, 不缩放变形")

    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
    def test_overflow_value_truncated_with_ellipsis(self):
        info = {"symbol": "000001.SZ", "name": "超长名称" * 12, "last_price": 11.5, "prev_close": 11.2}
        out = self._collect({"width": 204, "height": 720, "stockInfo": info})
        value = next(t for t in out["drawn"] if t["t"].startswith("超长名称"))
        self.assertTrue(value["t"].endswith("…"), value["t"])
        self.assertLess(len(value["t"]), 12 * 4)

    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
    def test_scale_multiplies_every_coordinate(self):
        one = self._collect({"width": 204, "height": 720, "scale": 1})
        two = self._collect({"width": 204, "height": 720, "scale": 2})
        self.assertEqual(len(one["drawn"]), len(two["drawn"]))
        for a, b in zip(one["drawn"], two["drawn"]):
            self.assertEqual(b["x"], a["x"] * 2)
            self.assertEqual(b["y"], a["y"] * 2)
            self.assertEqual(b["t"], a["t"])
            self.assertIn("24px monospace", b["font"], "2 倍导出字号同步放大")
        # 底色与分隔线都按比例放大 (1px 线 → 2px)
        self.assertEqual((two["rules"][0]["w"], two["rules"][0]["h"]), (408, 1440))
        one_seps = [r for r in one["rules"] if r["h"] == 1]
        two_seps = [r for r in two["rules"] if r["h"] == 2]
        self.assertTrue(one_seps, "基本信息标题下应有分隔线")
        self.assertEqual(len(two_seps), len(one_seps))

    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
    def test_collapsed_panel_draws_nothing(self):
        out = self._collect({"width": 204, "height": 720, "info": False})
        self.assertEqual(out["drawn"], [])
        self.assertEqual(out["model"]["content"], 0)
        self.assertEqual(out["model"]["bg"], "#f8f9fa", "仍铺底色, 保持导出图左侧干净")


if __name__ == "__main__":
    unittest.main(verbosity=2)
