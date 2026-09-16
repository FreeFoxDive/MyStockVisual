# -*- coding: utf-8 -*-
"""量化管理页 (quant.html) 回归测试: 仅管理员可见 + 模型管理搬移 + 信号忽视记录。

回归点:
  * 量化模型管理从 admin.html 搬到 quant.html (交易页面用户菜单入口同步改指);
  * 页面仅管理员可用: /api/auth/me 非管理员直接跳走, 且只调用管理员接口;
  * 信号忽视记录: 搜索选股带入代码/名称, 模型/剔除理由必填, 现价/止盈/保本/止损可空,
    行内代码可跳回主页 K 线, 样式与交易页面同款类名;
  * login.html 的 next 白名单必须包含 /quant.html, 否则登录后回跳被降级到主页。

运行:
    venv/Scripts/python.exe -u visual/test/test_quant_page_js.py
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

_VISUAL_DIR = Path(__file__).resolve().parents[1]
STATIC_DIR = _VISUAL_DIR / "static"
QUANT_HTML = STATIC_DIR / "quant.html"


def _extract_fn(src: str, name: str, page: str = "quant.html") -> str:
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", src)
    if not m:
        raise AssertionError(f"{page} 中找不到 function {name}")
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


class QuantPageStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = QUANT_HTML.read_text(encoding="utf-8")
        cls.admin = (STATIC_DIR / "admin.html").read_text(encoding="utf-8")
        cls.trades = (STATIC_DIR / "trades.html").read_text(encoding="utf-8")
        cls.login = (STATIC_DIR / "login.html").read_text(encoding="utf-8")

    def test_admin_only_gate(self):
        auth = _extract_fn(self.src, "checkAuth")
        self.assertIn("data.is_admin", auth)
        self.assertIn("location.href = '/trades.html'", auth, "非管理员直接退回交易页面")
        # 页面只用这些接口: 除登录态/搜索/快照外, 模型与忽视记录接口都是管理员接口
        allowed = ("/api/auth/me", "/api/auth/logout", "/api/models", "/api/ignore-reasons",
                   "/api/signal-ignores", "/api/search", "/api/quote")
        for path in re.findall(r"api\('(/api/[^'?]*)", self.src):
            self.assertTrue(
                any(path == a or path.startswith(a + "/") for a in allowed), path)

    def test_model_management_moved_from_admin(self):
        # 搬移: admin.html 不再有模型表格/弹窗/函数, quant.html 必须有
        for token in ('id="model-tbody"', 'id="model-modal-mask"', "openModelModal", "saveModel"):
            self.assertNotIn(token, self.admin, f"admin.html 不应再保留 {token}")
            self.assertIn(token, self.src, f"quant.html 缺少 {token}")
        self.assertIn("量化模型管理", self.src)
        # 入口: admin.html 与交易页面用户菜单都指向 quant.html
        self.assertIn("location.href='/quant.html'", self.admin)
        self.assertIn('href="/quant.html"', self.trades)
        self.assertNotIn("/admin.html#models", self.trades)
        # 登录回跳白名单
        self.assertIn("if (next === '/quant.html') return '/quant.html';", self.login)

    def test_ignore_table_columns(self):
        head = self.src[self.src.index('id="ignore-tbody"') - 1200:self.src.index('id="ignore-tbody"')]
        for col in ("代码 / 名称", "模型", "剔除理由", "信号日期", "现价", "止盈", "保本", "止损", "操作"):
            self.assertIn(col, head, col)
        # 空态列数与表头一致 (9 列), 否则表格错行
        self.assertIn('colspan="9"', self.src)

    def test_ignore_symbol_links_to_home(self):
        body = _extract_fn(self.src, "renderIgnores")
        self.assertIn('class="sym-link"', body)
        self.assertIn('href="/?symbol=', body)
        self.assertIn('class="code"', body, "代码应带 .code 灰字, 与交易页面同款")

    def test_ignore_rows_style_matches_trades_page(self):
        # 记录样式参考交易页面: 同一套类名
        for cls in ("table-wrap", "reason-cell", "row-actions", "pagination", "section-title"):
            self.assertIn(f'class="{cls}"', self.src, cls)
        self.assertIn("td.sym a.sym-link", self.src)

    def test_ignore_form_fields_and_required(self):
        modal = self.src[self.src.index('id="ignore-modal-mask"'):]
        modal = modal[:modal.index("</script>")]
        for field in ('id="i-symbol-input"', 'id="i-symbol-dropdown"', 'id="i-model"',
                      'id="i-reason"', 'id="i-reason-note"', 'id="i-signal-date"',
                      'id="i-current-price"', 'id="i-take-profit"', 'id="i-breakeven"',
                      'id="i-stop-loss"'):
            self.assertIn(field, modal, field)
        # 模型与剔除理由必填 (标红星号在各自 label 上)
        for label_match in (r"模型 <span[^>]*>\*</span>", r"剔除理由 <span[^>]*>\*</span>",
                            r"信号日期 <span[^>]*>\*</span>"):
            self.assertRegex(modal, label_match, label_match)
        for optional in ("现价", "止盈", "保本", "止损"):
            seg = modal[modal.index(f"<label>{optional}</label>"):]
            self.assertNotIn("<span", seg[:60], f"{optional} 是可选项, 不应标必填星号")

    def test_ignore_save_validates_required_and_order(self):
        save = _extract_fn(self.src, "saveIgnore")
        self.assertIn("请先搜索并选择股票", save)
        self.assertIn("请选择模型", save)
        self.assertIn("请选择剔除理由", save)
        self.assertIn("请选择信号日期", save)
        # 可选价只校验"已填的"之间顺序, 与后端 _parse_ignore_prices 一致
        self.assertIn("须满足止盈 > 保本", save)
        self.assertIn("须满足保本 > 止损", save)
        self.assertIn("须满足止盈 > 止损", save)
        for token in ("tp !== ''", "be !== ''", "sl !== ''"):
            self.assertIn(token, save, token)

    def test_reason_presets_from_api(self):
        load = _extract_fn(self.src, "loadIgnoreReasons")
        self.assertIn("/api/ignore-reasons", load)
        # 编辑旧记录时模型可能已停用 → 必须回填, 否则保存会把原模型改掉
        sel = _extract_fn(self.src, "renderIgnoreModelSelect")
        self.assertIn("inactive.some(m => m.id === keepId)", sel)
        self.assertIn("（已删除）", sel)
        self.assertIn("renderIgnoreModelSelect(t ? t.model_id : null)",
                      _extract_fn(self.src, "openIgnoreModal"))

    def test_signal_date_max_today(self):
        modal = _extract_fn(self.src, "openIgnoreModal")
        self.assertIn("dateEl.max = todayISO()", modal, "信号日期不得晚于今天")
        self.assertIn("todayISO()", self.src)

    def test_symbol_search_debounced(self):
        body = _extract_fn(self.src, "onIgnoreSymbolInput")
        self.assertIn("clearTimeout", body)
        self.assertIn("}, 200)", body, "搜索必须防抖")
        self.assertIn("/api/search?q=", body)


class QuantPageBehaviorTest(unittest.TestCase):
    """真实源码镜像: 剔除理由拼接/转义 + 翻页夹取 + 分页按钮禁用态。"""

    @classmethod
    def setUpClass(cls):
        src = QUANT_HTML.read_text(encoding="utf-8")
        page_size = re.search(r"const\s+IGNORE_PAGE_SIZE\s*=\s*(\d+);", src)
        if not page_size:
            raise AssertionError("quant.html 中找不到 IGNORE_PAGE_SIZE")
        cls.script = (
            f"const IGNORE_PAGE_SIZE = {page_size.group(1)};\n"
            + _extract_fn(src, "escHtml") + "\n"
            + _extract_fn(src, "ignoreReasonHtml") + "\n"
            + _extract_fn(src, "changeIgnorePage") + "\n"
            + _extract_fn(src, "renderIgnorePager") + "\n"
            + """
const els = {};
function el(id) { return els[id] || (els[id] = { textContent: '', disabled: null }); }
const document = { getElementById: (id) => el(id) };
const loads = [];
function loadIgnores() { loads.push(state.ignorePage); }
const state = { ignorePage: 0, ignoreTotal: 0 };

function run(step) {
  state.ignorePage = step.page;
  state.ignoreTotal = step.total;
  loads.length = 0;
  if (step.change) changeIgnorePage(step.change);
  renderIgnorePager();
  return {
    reason: step.reason === undefined ? null : ignoreReasonHtml(step.reason),
    page: state.ignorePage,
    loads: loads.slice(),
    info: el('ignore-page-info').textContent,
    count: el('ignore-count').textContent,
    prev: el('btn-ignore-prev').disabled,
    next: el('btn-ignore-next').disabled,
    pageSize: IGNORE_PAGE_SIZE,
  };
}
process.stdout.write(JSON.stringify(run(JSON.parse(process.argv[1]))));
"""
        )

    def _run(self, step):
        proc = subprocess.run(["node", "-e", self.script, json.dumps(step)],
                              capture_output=True, check=True)
        return json.loads(proc.stdout.decode("utf-8"))

    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
    def test_reason_joins_and_escapes(self):
        out = self._run({"page": 0, "total": 0, "reason": {"reason": "量能不足", "reason_note": "分时萎缩"}})
        self.assertEqual(out["reason"], "量能不足；分时萎缩")
        self.assertEqual(self._run({"page": 0, "total": 0,
                                    "reason": {"reason": "其他", "reason_note": ""}})["reason"], "其他")
        self.assertEqual(self._run({"page": 0, "total": 0,
                                    "reason": {"reason": "", "reason_note": ""}})["reason"], "—")
        escaped = self._run({"page": 0, "total": 0,
                             "reason": {"reason": "<b>位置过高</b>", "reason_note": "a&b"}})["reason"]
        self.assertEqual(escaped, "&lt;b&gt;位置过高&lt;/b&gt;；a&amp;b")

    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
    def test_paging_clamped_and_only_loads_on_change(self):
        out = self._run({"page": 0, "total": 0, "change": -1})
        self.assertEqual(out["page"], 0)
        self.assertEqual(out["loads"], [], "已在首页不应再发请求")
        self.assertEqual(out["prev"], True)
        self.assertEqual(out["next"], True, "共 0 条时下一页也禁用")
        out = self._run({"page": 0, "total": 45, "change": 1})
        self.assertEqual(out["page"], 1)
        self.assertEqual(out["loads"], [1])
        self.assertEqual(out["pageSize"], 20)
        out = self._run({"page": 2, "total": 45, "change": 1})
        self.assertEqual(out["page"], 2, "末页再点下一页应夹住")
        self.assertEqual(out["loads"], [])
        self.assertEqual(out["next"], True)
        self.assertEqual(out["info"], "第 3 / 3 页")
        self.assertEqual(out["count"], "共 45 条")
        out = self._run({"page": 0, "total": 45, "change": 0})
        self.assertEqual(out["info"], "第 1 / 3 页")
        self.assertEqual(out["prev"], True)
        self.assertEqual(out["next"], False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
