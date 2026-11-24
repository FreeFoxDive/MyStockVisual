# -*- coding: utf-8 -*-
"""仓位计算纯函数 + 主页接线（Node）。

默认 10 万 / 风险 2% / 止损 2×ATR / 100 股向下取整。
运行:
    venv/Scripts/python.exe -u visual/test/test_pos_calc_js.py
"""
from __future__ import annotations

import json
import os
import unittest

from js_test_util import require_node, run_node

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
VISUAL_DIR = os.path.dirname(TEST_DIR)
POS_JS = os.path.join(VISUAL_DIR, "static", "js", "pos-calc.js").replace("\\", "/")
INDEX_HTML = os.path.join(VISUAL_DIR, "static", "index.html")

HK_OFF = {
    "halfKelly": False, "hkSource": "manual",
    "hkWinRate": None, "hkPayoff": None, "hkModelId": None,
}


def _cfg(**over):
    base = {"capital": 100000, "riskPct": 2, "atrMult": 2, "atrPeriod": 14}
    base.update(HK_OFF)
    base.update(over)
    return base


def _calc(payload):
    require_node()
    script = (
        f"const P = require({json.dumps(POS_JS)});\n"
        "const c = JSON.parse(process.argv[1]);\n"
        "const out = { cfg: P.normalizePosCalcConfig(c.cfg || null) };\n"
        "if (c.sizing) out.sizing = P.calcPositionSizing(c.sizing);\n"
        "if (c.hk) { out.hk = P.calcHalfKelly(c.hk); out.hkText = P.halfKellyResultText(out.hk); }\n"
        "if (c.live) out.live = P.hkLiveFromQuant(c.live.modelId, c.live.payload, c.live.now);\n"
        "if (c.pick) out.pick = P.pickHalfKellyInputs(c.pick.cfg, c.pick.hkLive);\n"
        "if (c.side) out.side = P.formatSideRows(c.side);\n"
        "process.stdout.write(JSON.stringify(out));"
    )
    return json.loads(run_node(script, json.dumps(payload)))


class PosCalcMathTest(unittest.TestCase):
    def test_defaults(self):
        out = _calc({})
        self.assertEqual(out["cfg"], _cfg())

    def test_boolean_is_not_a_number(self):
        out = _calc({"cfg": {"capital": True, "riskPct": True, "atrMult": True, "atrPeriod": True}})
        self.assertEqual(out["cfg"], _cfg())

    def test_normalize_rejects_out_of_range(self):
        out = _calc({"cfg": {"capital": 10, "riskPct": 80, "atrMult": 0, "atrPeriod": 30}})
        self.assertEqual(out["cfg"]["capital"], 100000)
        self.assertEqual(out["cfg"]["riskPct"], 2)
        self.assertEqual(out["cfg"]["atrMult"], 2)
        self.assertEqual(out["cfg"]["atrPeriod"], 14)
        kept = _calc({"cfg": {"capital": 200000, "riskPct": 1.5, "atrMult": 2.5, "atrPeriod": 20}})
        self.assertEqual(kept["cfg"], _cfg(capital=200000, riskPct=1.5, atrMult=2.5, atrPeriod=20))

    def test_rounds_shares_down_to_lot(self):
        # 止损距离 0.201, 2000/0.201 = 9950.25 → 9900 股, 不能进到 10000
        out = _calc({"sizing": {"entryPrice": 10, "atr": 0.1005, "atrMult": 2}})["sizing"]
        self.assertEqual(out["shares"], 9900)
        self.assertEqual(out["lots"], 99)
        self.assertEqual(out["byCapShares"], 10000)
        self.assertTrue(out["ok"])
        self.assertIsNone(out["note"])
        self.assertEqual(out["stopPrice"], 9.8)

    def test_capital_cap_and_too_small(self):
        capped = _calc({"sizing": {"entryPrice": 10, "atr": 0.01}})["sizing"]
        self.assertEqual(capped["shares"], 10000)
        self.assertEqual(capped["byRiskShares"], 100000)
        self.assertEqual(capped["note"], "受本金上限约束")
        self.assertEqual(capped["posPct"], 100)

        risk = _calc({"sizing": {"entryPrice": 100, "atr": 15}})["sizing"]
        self.assertEqual(risk["shares"], 0)
        self.assertEqual(risk["note"], "不足1手（超风险预算）")
        self.assertFalse(risk["ok"])

        cash = _calc({"sizing": {"entryPrice": 200000, "atr": 1}})["sizing"]
        self.assertEqual(cash["shares"], 0)
        self.assertEqual(cash["note"], "不足1手（超本金）")

    def test_etf_stop_uses_three_decimals_and_index_hides_rows(self):
        etf = _calc({"sizing": {"entryPrice": 1.234, "atr": 0.01, "isEtf": True}})["sizing"]
        self.assertEqual(etf["dp"], 3)
        self.assertEqual(etf["stopPrice"], 1.214)
        side = _calc({"side": {
            "isEtf": True, "entryPrice": 1.234, "atr14": 0.01, "atr20": 0.02,
            "downColor": "down",
        }})["side"]
        self.assertEqual(side[1]["text"], "1.214")
        self.assertEqual(side[1]["color"], "down")
        self.assertEqual(side[0]["action"], "poscalc")

        hidden = _calc({"side": {
            "isIndex": True, "entryPrice": 3000, "atr14": 20, "atr20": 18,
        }})["side"]
        self.assertEqual(hidden, [])
        missing = _calc({"side": {"entryPrice": 10, "atr14": None, "atr20": 0.4}})["side"]
        self.assertEqual(missing, [])

    def test_atr_period_switch(self):
        common = {"entryPrice": 10, "atr14": 1, "atr20": 0.5, "downColor": "down"}
        a14 = _calc({"side": dict(common, config={"atrPeriod": 14})})["side"]
        a20 = _calc({"side": dict(common, config={"atrPeriod": 20})})["side"]
        self.assertEqual(a14[0]["text"], "10手 (10%)")
        self.assertEqual(a14[1]["text"], "8.00")
        self.assertIn("ATR14", a14[1]["title"])
        self.assertIn("点击修改算仓参数", a14[0]["title"])
        self.assertEqual(a20[0]["text"], "20手 (20%)")
        self.assertEqual(a20[1]["text"], "9.00")
        self.assertIn("ATR20", a20[1]["title"])

    def test_stop_below_zero_is_invalid(self):
        out = _calc({"sizing": {"entryPrice": 10, "atr": 6}})["sizing"]
        self.assertEqual(out["reason"], "stop_non_positive")
        self.assertFalse(out["ok"])
        side = _calc({"side": {"entryPrice": 10, "atr14": 6}})["side"]
        self.assertEqual(side, [])

    def test_half_kelly_off_by_default_and_manual_math(self):
        hidden = _calc({"side": {
            "entryPrice": 10, "atr14": 0.4, "atr20": 0.5, "downColor": "down",
        }})["side"]
        self.assertEqual([r["label"] for r in hidden], ["建议仓位", "止损参考"])

        # 胜率 47.3% 盈亏比 2.09 → f*≈0.2208, ½≈0.1104, 10 元 / 10 万 → 1100 股
        hk = _calc({"hk": {
            "entryPrice": 10, "capital": 100000, "winRate": 47.3, "payoff": 2.09,
        }})["hk"]
        self.assertEqual(hk["shares"], 1100)
        self.assertEqual(hk["lots"], 11)
        self.assertAlmostEqual(hk["kelly"], 0.473 - 0.527 / 2.09, places=6)
        self.assertAlmostEqual(hk["half"], hk["kelly"] / 2, places=6)
        self.assertTrue(hk["ok"])

        flat = _calc({"hk": {"entryPrice": 10, "capital": 100000, "winRate": 50, "payoff": 1}})["hk"]
        self.assertEqual(flat["shares"], 0)
        self.assertEqual(flat["reason"], "non_positive")
        neg = _calc({"hk": {"entryPrice": 10, "capital": 100000, "winRate": 30, "payoff": 1}})["hk"]
        self.assertEqual(neg["reason"], "non_positive")
        self.assertLess(neg["kelly"], 0)

        # 高价凑不满 1 手
        tiny = _calc({"hk": {"entryPrice": 500, "capital": 100000, "winRate": 55, "payoff": 1.2}})["hk"]
        self.assertEqual(tiny["shares"], 0)
        self.assertEqual(tiny["reason"], "below_lot")

        # 二元赔率下 ½f* < 50%, 仍不得超过本金
        capped = _calc({"hk": {
            "entryPrice": 10, "capital": 100000, "winRate": 99.9, "payoff": 1000,
        }})["hk"]
        self.assertLessEqual(capped["shares"], 10000)
        self.assertLessEqual(capped["posPct"], 100)

    def test_half_kelly_lot_boundary_does_not_lose_or_add_a_lot(self):
        common = {"capital": 100000, "winRate": 70, "payoff": 2}
        self.assertEqual(_calc({"hk": dict(common, entryPrice=27.5)})["hk"]["shares"], 1000)
        self.assertEqual(_calc({"hk": dict(common, entryPrice=27.50001)})["hk"]["shares"], 900)

    def test_half_kelly_star_market_minimum_and_single_shares(self):
        common = {"symbol": "688001.SH", "capital": 100000, "winRate": 50, "payoff": 3}
        below_minimum = _calc({"hk": dict(common, entryPrice=100)})
        self.assertEqual(below_minimum["hk"]["shares"], 0)
        self.assertIn("不足200股", below_minimum["hkText"])
        above_minimum = _calc({"hk": dict(common, entryPrice=82.5)})
        self.assertEqual(above_minimum["hk"]["shares"], 202)
        self.assertIn("202 股", above_minimum["hkText"])
        side = _calc({"side": {"symbol": common["symbol"], "entryPrice": 82.5, "config": {
            "halfKelly": True, "hkWinRate": 50, "hkPayoff": 3,
        }}})["side"]
        self.assertTrue(side[-1]["text"].startswith("202股"))

    def test_half_kelly_side_row_only_when_on_and_follows_inputs(self):
        common = {"entryPrice": 10, "atr14": 0.4, "atr20": 0.5, "downColor": "down"}
        off = _calc({"side": dict(common, config={"halfKelly": False, "hkWinRate": 47.3, "hkPayoff": 2.09})})["side"]
        self.assertNotIn("半凯利", [r["label"] for r in off])

        on = _calc({"side": dict(common, config={
            "halfKelly": True, "hkSource": "manual", "hkWinRate": 47.3, "hkPayoff": 2.09,
        })})["side"]
        labels = [r["label"] for r in on]
        self.assertLess(labels.index("止损参考"), labels.index("半凯利"))
        row = on[-1]
        self.assertEqual(row["text"], "11手 (11%)")
        self.assertEqual(row["action"], "poscalc")
        self.assertIn("手动", row["title"])

        later = _calc({"side": dict(common, config={
            "halfKelly": True, "hkSource": "manual", "hkWinRate": 60, "hkPayoff": 2,
        })})["side"]
        self.assertNotEqual(later[-1]["text"], row["text"], "胜率变化后侧栏仓位要变")

    def test_model_source_uses_live_stats_not_stored_rates(self):
        cfg = _calc({"cfg": {
            "halfKelly": True, "hkSource": "model", "hkModelId": 4,
            "hkWinRate": 80, "hkPayoff": 3,
        }})["cfg"]
        self.assertTrue(cfg["halfKelly"])
        self.assertEqual(cfg["hkModelId"], 4)
        self.assertEqual(cfg["hkWinRate"], 80)
        self.assertEqual(cfg["hkPayoff"], 3)
        self.assertIsNone(_calc({"cfg": {"hkModelId": "all"}})["cfg"]["hkModelId"])
        self.assertEqual(_calc({"cfg": {"hkModelId": "none"}})["cfg"]["hkModelId"], "none")

        common = {"entryPrice": 10, "atr14": 0.4, "config": {
            "halfKelly": True, "hkSource": "model", "hkModelId": 4,
            "hkWinRate": 10, "hkPayoff": 1,
        }}
        waiting = _calc({"side": common})["side"]
        self.assertEqual(waiting[-1]["label"], "半凯利")
        self.assertEqual(waiting[-1]["text"], "—")

        settled = _calc({"side": dict(common, hkLive={
            "modelId": 4, "winRate": 47.3, "payoff": 2.09, "closedCount": 74, "error": None,
        })})["side"]
        self.assertEqual(settled[-1]["text"], "11手 (11%)")
        self.assertIn("74 笔", settled[-1]["title"])

        after = _calc({"side": dict(common, hkLive={
            "modelId": 4, "winRate": 70, "payoff": 3, "closedCount": 80, "error": None,
        })})["side"]
        self.assertNotEqual(after[-1]["text"], settled[-1]["text"])

        unassigned = _calc({"side": {
            "entryPrice": 10, "atr14": 0.4,
            "config": {"halfKelly": True, "hkSource": "model", "hkModelId": "none"},
            "hkLive": {"modelId": "none", "winRate": 47.3, "payoff": 2.09, "closedCount": 12, "error": None},
        }})["side"]
        self.assertEqual(unassigned[-1]["text"], "11手 (11%)")
        self.assertIn("无模型 12 笔", unassigned[-1]["title"])

        failed = _calc({"side": dict(common, hkLive={
            "modelId": 4, "winRate": None, "payoff": None, "error": "加载失败",
        })})["side"]
        self.assertEqual(failed[-1]["text"], "—")
        self.assertIn("加载失败", failed[-1]["title"])
        self.assertNotIn("样本不足", failed[-1]["title"])
        thin = _calc({"side": dict(common, hkLive={
            "modelId": 4, "winRate": None, "payoff": None, "error": "样本不足",
        })})["side"]
        self.assertIn("样本不足", thin[-1]["title"])

        first = _calc({"live": {"modelId": 4, "now": 1, "payload": {
            "summary": {"win_rate": 47.3, "payoff_ratio": 2.09, "closed_count": 74},
        }}})["live"]
        second = _calc({"live": {"modelId": 4, "now": 2, "payload": {
            "summary": {"win_rate": 70, "payoff_ratio": 3, "closed_count": 80},
        }}})["live"]
        self.assertEqual(first["winRate"], 47.3)
        self.assertEqual(second["winRate"], 70)
        self.assertIsNone(first.get("hkWinRate"))
        thin = _calc({"live": {"modelId": 4, "payload": {"summary": {"closed_count": 0}}}})["live"]
        self.assertEqual(thin["error"], "样本不足")
        self.assertIsNone(thin["winRate"])


class PosCalcPageWiringTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = open(INDEX_HTML, encoding="utf-8").read()

    def _page_node(self, script):
        run_node(r"""
const assert = require('node:assert/strict');
const VisualPosCalc = require(process.argv[1]);
const src = require('node:fs').readFileSync(process.argv[2], 'utf8');
const fn = n => src.match(new RegExp('(?:async )?function ' + n + '\\([^]*?\\r?\\n}'))[0];
""" + script, POS_JS, INDEX_HTML)

    def test_manual_modal_refills_before_calculating_after_model_switch(self):
        self._page_node(r"""
const el = {};
const document = { getElementById: id => el[id] ??= {
  value: '', style: {}, classList: { toggle() {} }, appendChild() {},
}, createElement: () => ({}) };
const STATE = { posCalc: { halfKelly: true, hkSource: 'manual', hkWinRate: 47.3, hkPayoff: 2.09 } };
const get = document.getElementById;
get('pc-hk-win').value = '70';
get('pc-hk-payoff').value = '3';
get('pc-entry').value = '10';
let picked;
const fillPosCalcConfigInputs = () => {}, dailyAtrPair = () => ({});
const posCalcModalOpen = () => true, posCalcIsIndex = () => false, posCalcIsEtf = () => false;
const _posCalcFollow = false, posCalcInputIssue = () => null;
const halfKellyResultLines = cfg => { picked = VisualPosCalc.pickHalfKellyInputs(cfg, null); return []; };
const render = eval([fn('posCalcDraft'), fn('syncHalfKellyControls'), fn('renderPosCalcModal'), 'renderPosCalcModal'].join('\n'));
render({ refill: true });
assert.equal(get('pc-hk-win').value, '47.3');
assert.equal(picked.winRate, 47.3);
assert.equal(picked.payoff, 2.09);
get('pc-hk-win').value = '';
render();
assert.equal(picked.ready, false);
""")

    def test_model_refresh_ignores_out_of_order_body_and_runs_without_market_clock(self):
        self._page_node(r"""
let now = 50000, _hkFetchSeq = 0, _hkRequest = null;
const Date = { now: () => now }, HK_LIVE_TTL_MS = 30000;
const STATE = { posCalc: { halfKelly: true, hkSource: 'model', hkModelId: 1 }, period: '1d', _lastBarAt: 0 };
const document = { hidden: false };
const infoPanelOn = () => true, posCalcModalOpen = () => false;
let release, entered;
const waitForOldJson = new Promise(resolve => { entered = resolve; });
const oldJson = new Promise(resolve => { release = resolve; });
const stats = win => ({ summary: { win_rate: win, payoff_ratio: 2, closed_count: 10 } });
const calls = [];
const fetch = async (url, opts) => {
  calls.push({ url, signal: opts.signal });
  if (url.endsWith('=1')) return { ok: true, json: () => { entered(); return oldJson; } };
  return { ok: true, json: async () => stats(70) };
};
const refresh = eval([fn('hkLiveFresh'), fn('refreshHalfKellyFromModel'), fn('maybeRefreshHalfKelly'), 'maybeRefreshHalfKelly'].join('\n'));
(async () => {
  await refresh(); // 首屏请求尚未开始
  STATE._lastBarAt = now;
  STATE._foregroundLoading = true;
  await refresh({ force: true }); // 切回前台也不抢首图
  STATE._foregroundLoading = false;
  STATE.period = 'intraday';
  STATE._intradayLoading = true;
  await refresh();
  assert.equal(calls.length, 0);
  STATE._intradayLoading = false;
  const old = refresh();
  await waitForOldJson;
  STATE.posCalc.hkModelId = 2;
  await refresh();
  assert.equal(calls[0].signal.aborted, true);
  release(stats(40));
  await old;
  assert.equal(STATE.hkLive.modelId, 2);
  assert.equal(STATE.hkLive.winRate, 70);
  await refresh();
  assert.equal(calls.length, 2);
  now += 30000;
  document.hidden = true;
  await refresh();
  assert.equal(calls.length, 2);
  document.hidden = false;
  await refresh();
  assert.equal(calls[2].url, '/api/analysis/model-position-stats?model_id=2');
})().catch(err => { console.error(err); process.exitCode = 1; });
""")

    def test_toolbar_modal_and_script(self):
        self.assertIn('id="btn-poscalc"', self.src)
        btn = self.src[self.src.index('id="btn-poscalc"'):self.src.index('id="btn-poscalc"') + 80]
        self.assertIn('class="icon-btn"', btn)
        self.assertIn('id="poscalc-modal"', self.src)
        self.assertIn('src="/js/pos-calc.js"', self.src)
        self.assertIn("VisualPosCalc.formatSideRows", self.src)
        self.assertIn('data-action=', self.src)
        self.assertIn("function wirePosCalc()", self.src)
        self.assertIn("function openPosCalcModal()", self.src)
        self.assertIn("entryEl.value = px != null ? String(px) : ''", self.src)
        self.assertIn("atrEl.value = followedAtr != null ? String(followedAtr) : ''", self.src)
        self.assertIn("function posCalcInputIssue()", self.src)
        self.assertIn('id="pc-hk-on"', self.src)
        self.assertIn('id="pc-hk-off"', self.src)
        self.assertIn('value="none">无模型', self.src)
        self.assertIn("raw === 'none' ? 'none'", self.src)
        self.assertIn("function refreshHalfKellyFromModel(", self.src)
        self.assertIn("hkLive: (typeof STATE !== 'undefined' && STATE.hkLive) || null", self.src)
        refresh = self.src[self.src.index("async function refreshHalfKellyFromModel"):
                           self.src.index("function maybeRefreshHalfKelly")]
        self.assertIn("STATE.hkLive", refresh)
        self.assertNotIn("commitPosCalc", refresh)
        self.assertNotIn("hkWinRate", refresh)
        self.assertIn("visibilitychange", self.src)
        self.assertIn("info.symbol === STATE.symbol", self.src)
        self.assertIn("晚到的账号参数要补一次", self.src)
        # 算仓参数不是勾选态, 手机独立缓存不能把它从共享配置里拆走
        keys = self.src[self.src.index("const CFG_CHECK_KEYS"):self.src.index("function splitCfg")]
        self.assertNotIn("posCalc", keys)
        self.assertIn("posCalc:", self.src[self.src.index("function buildConfig"):self.src.index("function saveConfig")])


if __name__ == "__main__":
    unittest.main(verbosity=2)
