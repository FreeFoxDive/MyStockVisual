# -*- coding: utf-8 -*-
"""前端 sortHistory 逻辑的镜像测试（与 index.html 保持一致）。

用 Node 执行与页面相同的合并/排序规则，避免仅 Python 侧通过、浏览器仍乱序。
"""

import json
import os
import shutil
import subprocess
import unittest

_SORT_HISTORY_JS = r"""
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
  return withTs.concat(withoutTs).slice(0, 10).map(e => {
    const row = { symbol: e.symbol, name: e.name };
    if (e.ts != null) row.ts = e.ts;
    return row;
  });
}
const input = JSON.parse(process.argv[1]);
process.stdout.write(JSON.stringify(sortHistory(input)));
"""


@unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
class TestSearchHistoryJsMirror(unittest.TestCase):
    def _sort(self, items):
        proc = subprocess.run(
            ["node", "-e", _SORT_HISTORY_JS, json.dumps(items, ensure_ascii=True)],
            capture_output=True,
            check=True,
        )
        return json.loads(proc.stdout.decode("utf-8"))

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
