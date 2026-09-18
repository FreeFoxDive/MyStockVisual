# -*- coding: utf-8 -*-
"""选股条件注册表 (screener_metrics): 目录完整性 + 四种 kind 的判定与清洗。

运行:
    venv/Scripts/python.exe -u visual/test/test_screener_metrics.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_VISUAL_DIR = Path(__file__).resolve().parents[1]
if str(_VISUAL_DIR) not in sys.path:
    sys.path.insert(0, str(_VISUAL_DIR))

import pandas as pd  # noqa: E402
import screener_metrics as sm  # noqa: E402


class CatalogTest(unittest.TestCase):
    def test_keys_unique_and_groups_known(self):
        keys = [m["key"] for m in sm.METRICS]
        self.assertEqual(len(keys), len(set(keys)), "指标 key 不能重复")
        for m in sm.METRICS:
            self.assertIn(m["group"], sm.GROUPS)
            self.assertIn(m["kind"], ("bool", "num", "range", "text"))
            self.assertTrue(m["field"], f"{m['key']} 必须声明快照列")
            self.assertTrue(m["label"])

    def test_every_group_has_metrics(self):
        for g in sm.GROUPS:
            self.assertTrue([m for m in sm.METRICS if m["group"] == g], f"{g} 组为空")

    def test_catalog_groups_and_options(self):
        cats = sm.catalog({"industry": ["银行", "白酒"]})
        self.assertEqual([c["group"] for c in cats], list(sm.GROUPS))
        by_key = {m["key"]: m for c in cats for m in c["metrics"]}
        self.assertEqual(by_key["industry"]["options"], ["银行", "白酒"])
        self.assertEqual(by_key["rsi6"]["ops"], [">=", "<=", ">", "<", "=="])
        self.assertEqual(by_key["turnover"]["ops"], ["between"])
        self.assertEqual(by_key["above_ma20"]["ops"], ["is"])

    def test_text_options_from_df(self):
        df = pd.DataFrame({"industry": ["银行", "白酒", "银行", "", None]})
        out = sm.text_field_options(df, ["industry", "missing"])
        self.assertEqual(out["industry"], sorted(["银行", "白酒"]))   # 去重 + 稳定排序
        self.assertNotIn("missing", out)


class EvaluateTest(unittest.TestCase):
    def test_bool(self):
        row = {"above_ma20": True, "limit_up": False, "bogus": None}
        self.assertEqual(sm.evaluate("above_ma20", row, {}), (True, True))
        self.assertEqual(sm.evaluate("limit_up", row, {}), (False, False))
        self.assertEqual(sm.evaluate("limit_up", {}, {}), (False, None))

    def test_num_and_missing(self):
        row = {"rsi6": 18.5, "pe": None, "change_pct": float("nan")}
        self.assertEqual(sm.evaluate("rsi6", row, {"value": 20, "op": "<="}), (True, 18.5))
        self.assertEqual(sm.evaluate("rsi6", row, {"value": 10, "op": "<="}), (False, 18.5))
        self.assertEqual(sm.evaluate("pe", row, {"value": 10, "op": "<="}), (False, None))
        self.assertEqual(sm.evaluate("change_pct", row, {"value": 1, "op": ">="}), (False, None))

    def test_num_default_op_from_spec(self):
        # roe 的默认 op 是 >=; 不传 op 时按 spec 走
        self.assertEqual(sm.evaluate("roe", {"roe": 15.0}, {"value": 10}), (True, 15.0))
        self.assertEqual(sm.evaluate("roe", {"roe": 5.0}, {"value": 10}), (False, 5.0))

    def test_scale_converts_to_display_unit(self):
        """市值列存元, 条件按亿元填 → 判定与展示都用亿元。"""
        row = {"float_value": 5.2e10}      # 520 亿
        ok, val = sm.evaluate("float_value_yi", row, {"value": 300, "value2": 600})
        self.assertTrue(ok)
        self.assertAlmostEqual(val, 520.0)
        ok2, _ = sm.evaluate("float_value_yi", row, {"value": 1000, "value2": 2000})
        self.assertFalse(ok2)

    def test_amount_wan_scale(self):
        row = {"main_net_inflow": 123_456_789.0}
        ok, val = sm.evaluate("main_net_inflow_wan", row, {"value": 10000, "op": ">="})
        self.assertTrue(ok)
        self.assertAlmostEqual(val, 12345.6789)

    def test_range_swapped_bounds(self):
        row = {"turnover": 5.0}
        self.assertTrue(sm.evaluate("turnover", row, {"value": 10, "value2": 1})[0])

    def test_range_bad_bounds(self):
        self.assertFalse(sm.evaluate("turnover", {"turnover": 5.0}, {"value": None})[0])

    def test_text(self):
        row = {"industry": "银行"}
        self.assertEqual(sm.evaluate("industry", row, {"value": "银行"}), (True, "银行"))
        self.assertFalse(sm.evaluate("industry", row, {"value": "白酒"})[0])
        self.assertFalse(sm.evaluate("industry", {"industry": None}, {"value": "银行"})[0])

    def test_unknown_metric(self):
        self.assertEqual(sm.evaluate("nope", {"x": 1}, {"value": 1}), (False, None))


class EvaluateAllTest(unittest.TestCase):
    CONDS = [
        {"metric": "above_ma20", "label": "站上 MA20", "kind": "bool"},
        {"metric": "rsi6", "label": "RSI6", "kind": "num", "op": "<=", "value": 20},
        {"metric": "change_pct", "label": "当日涨跌幅", "kind": "num", "op": ">=", "value": 5},
    ]

    def test_and_requires_all(self):
        row = {"above_ma20": True, "rsi6": 15.0, "change_pct": 1.0}
        passed, hits, count = sm.evaluate_all(row, self.CONDS, "and")
        self.assertFalse(passed)
        self.assertEqual(count, 2)
        self.assertEqual([ok for ok, _v in hits], [True, True, False])

    def test_or_and_hit_count(self):
        row = {"above_ma20": True, "rsi6": 15.0, "change_pct": 1.0}
        passed, hits, count = sm.evaluate_all(row, self.CONDS, "or")
        self.assertTrue(passed)
        self.assertEqual(count, 2)
        # 明细里带展示值, 页面「满足条件」列直接渲染
        self.assertEqual(hits[1][1], 15.0)
        self.assertEqual(hits[0][1], True)

    def test_or_zero_hits_fails(self):
        row = {"above_ma20": False, "rsi6": 55.0, "change_pct": 0.1}
        passed, _hits, count = sm.evaluate_all(row, self.CONDS, "or")
        self.assertFalse(passed)
        self.assertEqual(count, 0)


class CleanTest(unittest.TestCase):
    def test_bool_drops_values(self):
        out, err = sm.clean([{"metric": "above_ma20", "value": 123}])
        self.assertIsNone(err)
        self.assertIsNone(out[0]["value"])
        self.assertEqual(out[0]["kind"], "bool")
        self.assertEqual(out[0]["label"], "站上 MA20")

    def test_num_requires_number_and_keeps_op(self):
        out, err = sm.clean([{"metric": "rsi6", "op": "<", "value": "20"}])
        self.assertIsNone(err)
        self.assertEqual(out[0]["value"], 20.0)
        self.assertEqual(out[0]["op"], "<")
        self.assertEqual(sm.clean([{"metric": "rsi6", "value": "abc"}])[1], "RSI6 需要填写数值")

    def test_num_bad_op_falls_back(self):
        out, _err = sm.clean([{"metric": "rsi6", "op": "hack", "value": 20}])
        self.assertEqual(out[0]["op"], sm.get("rsi6")["op"])

    def test_range_needs_both_bounds(self):
        self.assertEqual(sm.clean([{"metric": "turnover", "value": 1}])[1],
                         "换手率 需要填写区间上下限")
        out, err = sm.clean([{"metric": "turnover", "value": 1, "value2": 5}])
        self.assertIsNone(err)
        self.assertEqual((out[0]["value"], out[0]["value2"]), (1.0, 5.0))

    def test_text_needs_value(self):
        self.assertEqual(sm.clean([{"metric": "industry", "value": "  "}])[1],
                         "所处行业 需要填写取值")
        out, _err = sm.clean([{"metric": "industry", "value": " 银行 "}])
        self.assertEqual(out[0]["value"], "银行")

    def test_rejects_unknown_and_empty(self):
        self.assertEqual(sm.clean([{"metric": "bogus"}])[1], "条件无效")
        self.assertEqual(sm.clean("x")[1], "条件无效")
        self.assertEqual(sm.clean([])[1], "至少一个条件")

    def test_condition_count_capped(self):
        """无上限时一个账号就能用上万条条件把唯一 worker 与内存堵死。"""
        many = [{"metric": "above_ma20"}] * (sm.MAX_CONDITIONS + 1)
        err = sm.clean(many)[1]
        self.assertIn("最多", err)
        ok, err2 = sm.clean([{"metric": "above_ma20"}] * sm.MAX_CONDITIONS)
        self.assertIsNone(err2)
        self.assertEqual(len(ok), sm.MAX_CONDITIONS)

    def test_text_value_validated_when_options_known(self):
        opts = {"industry": ["银行", "白酒"]}
        self.assertIsNone(sm.clean([{"metric": "industry", "value": "银行"}], opts)[1])
        err = sm.clean([{"metric": "industry", "value": "随便编的"}], opts)[1]
        self.assertIn("可选范围", err)
        # options 为空 (快照无该列) 时不阻拦
        self.assertIsNone(sm.clean([{"metric": "industry", "value": "随便编的"}], {})[1])

    def test_text_value_normalized_and_length_capped(self):
        out, _err = sm.clean([{"metric": "industry", "value": "  银\n行  "}])
        self.assertEqual(out[0]["value"], "银 行", "多行/多余空白压成单行")
        long_v = "银" * (sm.TEXT_MAX_LEN + 1)
        self.assertIn("取值过长", sm.clean([{"metric": "industry", "value": long_v}])[1])

    def test_registry_version_tracks_definitions(self):
        self.assertTrue(sm.VERSION)
        self.assertEqual(sm.VERSION, sm._registry_version(), "同一定义版本稳定")
        self.assertNotEqual(sm.VERSION, "deadbeef")


if __name__ == "__main__":
    unittest.main(verbosity=2)
