# -*- coding: utf-8 -*-
"""前端 sortHistory / 按下标删除历史 逻辑的镜像测试（与 index.html 保持一致）。

用 Node 执行与页面相同的合并/排序规则，避免仅 Python 侧通过、浏览器仍乱序。
另含搜索记录条的交互结构断言: 触屏没有可靠 hover, 删除角标改为默认隐藏 +
「管理」开关统一切换, 平板上最多展示 10 条。
"""

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

_VISUAL_DIR = Path(__file__).resolve().parents[1]
INDEX_HTML = _VISUAL_DIR / "static" / "index.html"


def _extract_fn(src, name):
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


def _css_block(src, selector):
    i = src.index(selector + " {")
    start = src.index("{", i)
    depth = 0
    for j in range(start, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    raise AssertionError(f"{selector} 大括号不配对")


# 与 index.html sortHistory 逐行一致
_SORT_HISTORY_FN = r"""
function sortHistory(list) {
  if (!Array.isArray(list) || !list.length) return [];
  const best = new Map();
  list.forEach((item, i) => {
    if (!item || item.symbol == null) return;
    const symbol = String(item.symbol).trim();
    if (!symbol) return;
    const name = String(item.name || symbol).trim() || symbol;
    let ts = null;
    if (item.ts != null && item.ts !== '') {
      const n = Number(item.ts);
      if (Number.isFinite(n) && n >= 0) ts = n;
    }
    const entry = { symbol, name, _i: i };
    if (ts != null) entry.ts = ts;
    const prev = best.get(symbol);
    if (!prev) {
      best.set(symbol, entry);
      return;
    }
    const prevTs = prev.ts;
    if (ts != null && (prevTs == null || ts >= prevTs)) best.set(symbol, entry);
  });
  const withTs = [];
  const withoutTs = [];
  for (const e of best.values()) {
    if (e.ts != null) withTs.push(e);
    else withoutTs.push(e);
  }
  withTs.sort((a, b) => b.ts - a.ts);
  withoutTs.sort((a, b) => a._i - b._i);
  return withTs.concat(withoutTs).slice(0, 20).map(e => {
    const row = { symbol: e.symbol, name: e.name };
    if (e.ts != null) row.ts = e.ts;
    return row;
  });
}
"""

_SORT_HISTORY_JS = _SORT_HISTORY_FN + r"""
const input = JSON.parse(process.argv[1]);
process.stdout.write(JSON.stringify(sortHistory(input)));
"""

# 镜像 index.html delHistoryTag: 按下标删除。渲染传的是 sortHistory 顺序上的下标,
# 删除时重新 loadHistory() 拿到同一顺序再 splice, 故两者必须同序。
_DEL_BY_INDEX_JS = _SORT_HISTORY_FN + r"""
function delByIndex(list, idx) {
  const rows = sortHistory(list);
  if (!(idx >= 0) || idx >= rows.length) return rows.map(r => r.symbol);
  rows.splice(idx, 1);
  return rows.map(r => r.symbol);
}
const input = JSON.parse(process.argv[1]);
process.stdout.write(JSON.stringify({
  order: sortHistory(input).map(r => r.symbol),
  left: delByIndex(input, Number(process.argv[2])),
}));
"""


@unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
class TestSearchHistoryJsMirror(unittest.TestCase):
    def _run(self, script, *args):
        proc = subprocess.run(
            ["node", "-e", script] + [json.dumps(a, ensure_ascii=True) for a in args],
            capture_output=True,
            check=True,
        )
        return json.loads(proc.stdout.decode("utf-8"))

    def _sort(self, items):
        return self._run(_SORT_HISTORY_JS, items)

    def test_legacy_after_timestamped(self):
        out = self._sort([
            {"symbol": "LEGACY.SZ", "name": "legacy"},
            {"symbol": "A.SZ", "name": "A", "ts": 100},
            {"symbol": "B.SZ", "name": "B", "ts": 200},
        ])
        self.assertEqual([x["symbol"] for x in out], ["B.SZ", "A.SZ", "LEGACY.SZ"])
        self.assertNotIn("ts", out[2])

    def test_new_search_beats_legacy_same_symbol(self):
        out = self._sort([
            {"symbol": "000001.SZ", "name": "old"},
            {"symbol": "000001.SZ", "name": "new", "ts": 999},
        ])
        self.assertEqual(out, [{"symbol": "000001.SZ", "name": "new", "ts": 999}])

    def test_merge_server_legacy_and_local_new(self):
        # 与 sync mergeHistory(server+local) 一致：先 server 再 local 传入 sortHistory
        out = self._sort([
            {"symbol": "OLD.SZ", "name": "old"},
            {"symbol": "MID.SZ", "name": "mid", "ts": 500},
            {"symbol": "NEW.SZ", "name": "new", "ts": 900},
        ])
        self.assertEqual([x["symbol"] for x in out], ["NEW.SZ", "MID.SZ", "OLD.SZ"])

    def test_cap_20(self):
        # 前端 HIST_MAX=20：25 条不同标的只保留最新的 20 条
        out = self._sort([
            {"symbol": f"S{i}.SZ", "name": f"s{i}", "ts": i} for i in range(25)
        ])
        self.assertEqual(len(out), 20)
        self.assertEqual(out[0]["symbol"], "S24.SZ")
        self.assertEqual(out[-1]["symbol"], "S5.SZ")

    # 含无 ts 旧数据 + 同 symbol 重复, 是排序最易漂移的输入
    _MESSY = [
        {"symbol": "LEGACY.SZ", "name": "legacy"},
        {"symbol": "000001.SZ", "name": "平安银行", "ts": 300},
        {"symbol": "000001.SZ", "name": "平安银行旧", "ts": 100},
        {"symbol": "600000.SH", "name": "浦发银行", "ts": 200},
        {"symbol": "000636.SZ", "name": "风华高科"},  # 无 ts
    ]
    # 有 ts 按 ts 降序, 无 ts 的按原始相对顺序排最后
    _MESSY_ORDER = ["000001.SZ", "600000.SH", "LEGACY.SZ", "000636.SZ"]

    def test_sort_is_deterministic(self):
        """渲染下标 == 删除下标的前提：同一输入必须每次同序。"""
        self.assertEqual(self._sort(self._MESSY), self._sort(self._MESSY))

    def test_del_by_index_removes_rendered_entry(self):
        """删掉下标 i, 等于把渲染顺序上第 i 个标签摘掉, 其余顺序不变。"""
        out = self._run(_DEL_BY_INDEX_JS, self._MESSY, 0)
        order = out["order"]
        self.assertEqual(len(order), 4, "重复 symbol 归一后 4 条")
        self.assertEqual(order, self._MESSY_ORDER)
        for idx in range(len(order)):
            left = self._run(_DEL_BY_INDEX_JS, self._MESSY, idx)["left"]
            self.assertEqual(left, [s for s in order if s != order[idx]],
                             f"删除下标 {idx} ({order[idx]}) 后其余顺序不变")

    def test_del_by_index_out_of_range_is_noop(self):
        """越界/负数不得误删首条 (delHistoryTag 的 guard)。"""
        for idx in (-1, 4, 99):
            self.assertEqual(
                self._run(_DEL_BY_INDEX_JS, self._MESSY, idx)["left"],
                self._MESSY_ORDER,
            )


class HistoryBarUiStaticTest(unittest.TestCase):
    """搜索记录条的交互与平板展示上限。

    触屏没有可靠 hover: 删除角标不能再「常显」(悬在标签外会把行高撑得不一致,
    也容易误触), 改成默认全隐藏 + 一个「管理」开关统一切换。
    """

    @classmethod
    def setUpClass(cls):
        cls.src = INDEX_HTML.read_text(encoding="utf-8")
        cls.bar = cls.src[cls.src.index('<div id="history-bar"'):cls.src.index('id="indicator-bar"')]

    def test_edit_toggle_button_wired(self):
        self.assertIn('id="hist-edit"', self.bar)
        self.assertIn('onclick="toggleHistoryEdit()"', self.bar)
        self.assertIn('aria-pressed="false"', self.bar, "开关要有按下态供无障碍与高亮用")
        body = _extract_fn(self.src, "toggleHistoryEdit")
        self.assertIn("STATE._histEdit", body)
        self.assertIn("renderHistoryTags()", body)

    def test_delete_badge_hidden_by_default_everywhere(self):
        """opacity:0 必须在基础规则里 —— 只写在 @media (hover: hover) 里触屏不匹配, 就会常显。"""
        base = _css_block(self.src, ".history-tag .tag-del")
        self.assertIn("opacity: 0", base)
        self.assertIn("pointer-events: none", base)
        i = self.src.index("@media (hover: hover)")
        hover = self.src[i:self.src.index("}", self.src.index(".tag-del:hover", i))]
        self.assertNotIn("opacity: 0", hover, "悬停块只管浮现")
        edit = self.src.index("#history-bar.edit .history-tag .tag-del")
        self.assertGreater(edit, i, "编辑态规则要排在 hover 块之后才能盖住它")
        self.assertIn("opacity: 1", self.src[edit:edit + 200])

    def test_prefix_and_edit_keep_row_height_stable(self):
        self.assertIn('class="hist-prefix"', self.bar, "前缀样式进 CSS, 不再内联")
        self.assertNotIn('style="color:var(--text-secondary);font-size:12px', self.bar)
        self.assertIn("line-height", _css_block(self.src, "#history-bar .hist-prefix"))
        # 角标悬出的空间常驻: 切换编辑态不改变栏高 (否则又是一次高度跳动)
        self.assertIn("padding-top: 10px", _css_block(self.src, "@media (pointer: coarse)"))

    def test_tablet_caps_rendered_tags(self):
        body = _extract_fn(self.src, "renderHistoryTags")
        self.assertIn("histLayoutMode()", body, "布局口径统一走 histLayoutMode")
        self.assertIn("const tablet = mode !== 'desktop';", body, "平板 = 非桌面")
        self.assertIn("list.slice(0, HIST_VIEW_MAX_TABLET)", body)
        self.assertIn("const HIST_VIEW_MAX_TABLET = 10;", self.src)
        self.assertIn("HIST_MAX = 20", self.src, "存储仍留 20 条, 只改展示")
        self.assertIn("bar.classList.toggle('edit', editOn)", body)

    def test_breakpoint_change_rerenders(self):
        """转屏/拖窗口跨过平板阈值时要重渲染: 否则条数上限停留在旧口径。"""
        body = _extract_fn(self.src, "watchHistoryBarBreakpoint")
        self.assertIn("STATE._histMode", body)
        self.assertIn("histLayoutMode()", body)
        self.assertIn("renderHistoryTags()", body)
        # 三态 (phone/tablet/desktop) 任一切换都要重渲染: 只比 isTabletUi 会漏掉 640 那条线
        mode = _extract_fn(self.src, "histLayoutMode")
        self.assertIn("isPhoneUi()", mode)
        self.assertIn("isTabletUi()", mode)
        self.assertIn("STATE._histMode = mode;", _extract_fn(self.src, "renderHistoryTags"))
        self.assertIn("watchHistoryBarBreakpoint();", self.src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
