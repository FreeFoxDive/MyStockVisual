"""选股条件注册表: 指标目录 + 判定 (前后端共用同一张表)。

一个条件 = 因子库快照的一列 (见 visual/factors.py) + 一种比较方式。后端把这张表
发给页面渲染下拉 (``GET /api/screener/metrics``), 扫描时用同一份定义判定 ——
所以「页面能选的指标」与「实际判定口径」不可能漂移。

条件 JSON: ``{"metric": key, "op": ">=", "value": 60}``
区间类用 ``value`` + ``value2``; 文本类用 ``value``。

kind:
  bool   无参数, ok = 列为真
  num    单阈值, ok = 列值 op 数值 (op ∈ >= <= > < ==)
  range  双阈值闭区间, ok = value <= 列值 <= value2
  text   文本等值, ok = 列值 == value (行业等)

scale: 列值的存储单位 → 展示单位的折算 (如市值列存元, 条件按亿元填 → scale=1e8)。
判定与展示都用折算后的值, 页面看到的数字与用户填的阈值同一口径。

domain: 仅展示用的取值范围 (如 RSI「0~100」), 不参与判定, 也不进口径指纹。
"""
from __future__ import annotations

import hashlib
import json
import operator
import os

GROUPS = ("技术指标", "量价", "基本面/估值", "资金与筹码")

# 单次任务的条件条数上限: 判定是 O(行数×条件数) 且唯一 worker 被单任务独占,
# 无上限时一个账号就能用 2 万条条件把队列堵死 (结果明细 hits 也会按条件数膨胀)。
MAX_CONDITIONS = max(1, int(os.environ.get("SCREENER_MAX_CONDITIONS", "20")))
# 文本类取值长度上限: 取值会进推送文案 (共享群), 过长/多行的自由文本既无意义也可被滥用
TEXT_MAX_LEN = max(1, int(os.environ.get("SCREENER_TEXT_MAX_LEN", "24")))

OPS = {
    ">=": operator.ge, "<=": operator.le, ">": operator.gt, "<": operator.lt,
    "==": operator.eq,
}
OPS_BY_KIND = {"num": (">=", "<=", ">", "<", "=="), "range": ("between",),
               "bool": ("is",), "text": ("==",)}


def _m(key, label, group, kind, field, unit="", scale=1.0, op=">=", hint="", bars=0,
       domain=""):
    return {"key": key, "label": label, "group": group, "kind": kind, "field": field,
            "unit": unit, "scale": scale, "op": op, "hint": hint, "bars": bars,
            "domain": domain}


# ── 指标目录 (顺序即页面分组内的显示顺序) ──
METRICS = [
    # 技术指标 · 均线
    _m("above_ma5", "站上 MA5", "技术指标", "bool", "above_ma5", hint="收盘 > MA5"),
    _m("above_ma10", "站上 MA10", "技术指标", "bool", "above_ma10"),
    _m("above_ma20", "站上 MA20", "技术指标", "bool", "above_ma20"),
    _m("above_ma60", "站上 MA60", "技术指标", "bool", "above_ma60", bars=60),
    _m("ma5_cross_ma20", "MA5 上穿 MA20", "技术指标", "bool", "ma5_cross_ma20"),
    _m("ma10_cross_ma60", "MA10 上穿 MA60", "技术指标", "bool", "ma10_cross_ma60", bars=60),
    _m("ma_bull_align", "均线多头排列", "技术指标", "bool", "ma_bull_align",
       hint="MA5 > MA10 > MA20 > MA60", bars=60),
    _m("ma_converge_pct", "均线粘合度", "技术指标", "num", "ma_converge_pct", "%", op="<=",
       hint="(MA最高-最低)/收盘×100, 越小越粘合", bars=60),
    # 技术指标 · MACD
    _m("macd_cross_up", "MACD 金叉", "技术指标", "bool", "macd_cross_up"),
    _m("macd_cross_down", "MACD 死叉", "技术指标", "bool", "macd_cross_down"),
    _m("macd_dif_gt0", "MACD DIF > 0", "技术指标", "bool", "macd_above_zero"),
    _m("macd_hist_expand", "MACD 红柱放大", "技术指标", "bool", "macd_hist_expand"),
    # 技术指标 · KDJ / RSI / BOLL
    _m("kdj_golden", "KDJ 金叉", "技术指标", "bool", "kdj_golden"),
    _m("kdj_j", "KDJ J 值", "技术指标", "num", "kdj_j", op="<=", hint="J<0 超卖",
       domain="可超出 0~100"),
    _m("kdj_k", "KDJ K 值", "技术指标", "num", "kdj_k", op="<=", domain="0~100"),
    _m("rsi6", "RSI6", "技术指标", "num", "rsi6", op="<=", hint="<20 超卖", domain="0~100"),
    _m("rsi12", "RSI12", "技术指标", "num", "rsi12", op="<=", domain="0~100"),
    _m("rsi24", "RSI24", "技术指标", "num", "rsi24", op="<=", domain="0~100"),
    _m("boll_break_up", "突破 BOLL 上轨", "技术指标", "bool", "boll_break_up"),
    _m("boll_above_mid", "站上 BOLL 中轨", "技术指标", "bool", "boll_above_mid"),
    _m("boll_break_low", "跌破 BOLL 下轨", "技术指标", "bool", "boll_break_low"),
    # 技术指标 · 其他
    _m("wr14", "WR14", "技术指标", "num", "wr14", op="<=", hint="越小越强",
       domain="0~100"),  # 东财口径: 100*(HHV-C)/(HHV-LLV), 不是 -100~0
    _m("cci14", "CCI14", "技术指标", "num", "cci14", op=">=", hint=">100 强势",
       domain="约 -300~300"),
    _m("bias6", "BIAS6", "技术指标", "num", "bias6", "%", op="<=", hint="负值越大越超跌"),
    _m("dmi_adx", "DMI ADX", "技术指标", "num", "dmi_adx", op=">=", hint="趋势强度",
       domain="0~100"),
    _m("atr_pct", "ATR% (波动)", "技术指标", "num", "atr_pct", "%", op="<="),

    # 量价
    _m("vol_ratio5", "量比 (量/5日均量)", "量价", "num", "vol_ratio5", "倍", op=">="),
    _m("vol_shrink5", "缩量 (量<0.7×5日均量)", "量价", "bool", "vol_shrink5"),
    _m("turnover", "换手率", "量价", "range", "turnover", "%"),
    _m("amplitude", "振幅", "量价", "range", "amplitude", "%"),
    _m("amount_yi", "成交额", "量价", "range", "amount", "亿元", scale=1e8),
    _m("change_pct", "当日涨跌幅", "量价", "num", "change_pct", "%", op=">="),
    _m("chg_3d", "3日涨幅", "量价", "num", "chg_3d", "%", op=">="),
    _m("chg_5d", "5日涨幅", "量价", "num", "chg_5d", "%", op=">="),
    _m("chg_10d", "10日涨幅", "量价", "num", "chg_10d", "%", op=">="),
    _m("chg_20d", "20日涨幅", "量价", "num", "chg_20d", "%", op=">="),
    _m("up_streak", "连涨天数", "量价", "num", "up_streak", "天", op=">="),
    _m("down_streak", "连跌天数", "量价", "num", "down_streak", "天", op=">="),
    _m("new_high_20", "创20日新高", "量价", "bool", "new_high_20"),
    _m("new_high_60", "创60日新高", "量价", "bool", "new_high_60", bars=60),
    _m("new_low_20", "创20日新低", "量价", "bool", "new_low_20"),
    _m("new_low_60", "创60日新低", "量价", "bool", "new_low_60", bars=60),
    _m("limit_up", "涨停", "量价", "bool", "limit_up"),
    _m("limit_down", "跌停", "量价", "bool", "limit_down"),

    # 基本面/估值
    _m("float_value_yi", "流通市值", "基本面/估值", "range", "float_value", "亿元", scale=1e8),
    _m("total_value_yi", "总市值", "基本面/估值", "range", "total_value", "亿元", scale=1e8),
    _m("pe", "市盈率 (动)", "基本面/估值", "range", "pe", "倍",
       hint="东财快照动态口径; 该源不可用时用 收盘/每股收益 反推 (报告期口径)"),
    _m("pb", "市净率", "基本面/估值", "range", "pb", "倍",
       hint="东财快照口径; 不可用时用 收盘/每股净资产 反推"),
    _m("roe", "净资产收益率", "基本面/估值", "num", "roe", "%", op=">=",
       hint="最新报告期累计口径"),
    _m("industry", "所处行业", "基本面/估值", "text", "industry", hint="东财业绩报表口径"),

    # 资金与筹码
    # 单位核对 (2026-09, 本地无因子快照, 按写入源):
    # main_net_inflow ← 麦蕊 zljlr, 单位元 (api/mr_data.py), scale=1e4 后按万元填;
    # main_net_ratio ← zljlrl 已是百分数; pledge_ratio / etf_premium / 换手 / 筹码
    # 同样是 0~100 的百分数 (筹码在 factors.py 里 ×100), 不是 0~1 的小数。
    _m("main_net_inflow_wan", "主力净流入", "资金与筹码", "num", "main_net_inflow", "万元",
       scale=1e4, op=">="),
    _m("main_net_ratio", "主力净流入率", "资金与筹码", "num", "main_net_ratio", "%", op=">="),
    _m("lhb_today", "今日龙虎榜", "资金与筹码", "bool", "lhb_today"),
    _m("pledge_ratio", "质押比例", "资金与筹码", "num", "pledge_ratio", "%", op=">="),
    _m("chip_profit", "筹码获利盘", "资金与筹码", "num", "chip_profit", "%", op=">="),
    _m("chip_concentration", "筹码集中度", "资金与筹码", "num", "chip_concentration", "%",
       op="<=", hint="90%成本区间相对宽度, 越小越集中"),
    _m("above_chip_cost", "现价高于筹码平均成本", "资金与筹码", "bool", "above_chip_cost"),
    _m("etf_premium", "ETF 溢价率", "资金与筹码", "range", "etf_premium", "%",
       hint="仅 ETF 有值"),
]

BY_KEY = {m["key"]: m for m in METRICS}


def _registry_version():
    """指标口径指纹: field/kind/scale/op/unit 任一改动都会变。

    结果去重 (cache_key) 会带它 —— 改了判定口径后, 同一天不会复用旧口径算出的结果。
    """
    raw = json.dumps([{k: m[k] for k in ("key", "field", "kind", "scale", "op", "unit")}
                      for m in METRICS], sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(raw.encode()).hexdigest()[:8]


VERSION = _registry_version()


def get(key):
    return BY_KEY.get(key)


def catalog(options=None):
    """给前端的目录 (按 group 分好; 文本类带 options 供 datalist)。"""
    options = options or {}
    out = []
    for g in GROUPS:
        items = []
        for m in METRICS:
            if m["group"] != g:
                continue
            item = dict(m)
            item["ops"] = list(OPS_BY_KIND[m["kind"]])
            if m["kind"] == "text":
                item["options"] = options.get(m["field"], [])
            items.append(item)
        if items:
            out.append({"group": g, "metrics": items})
    return out


def evaluate(key, row, cond):
    """单条件判定 → (ok, 展示值)。无数据/NaN/类型不符一律 False。

    row 是快照一行 (dict); 展示值按 spec 的 scale 折算成展示单位。
    """
    spec = BY_KEY.get(key)
    if spec is None:
        return False, None
    kind = spec["kind"]
    raw = row.get(spec["field"])
    if kind == "text":
        got = "" if raw is None else str(raw)
        want = "" if cond.get("value") is None else str(cond.get("value"))
        return (bool(got) and got == want), (got or None)
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return False, None
    if val != val:      # NaN
        return False, None
    scale = spec.get("scale") or 1.0
    if scale != 1.0:
        val = val / scale
    if kind == "bool":
        return bool(val), bool(val)
    if kind == "range":
        try:
            lo, hi = float(cond.get("value")), float(cond.get("value2"))
        except (TypeError, ValueError):
            return False, val
        if lo > hi:
            lo, hi = hi, lo
        return (lo <= val <= hi), val
    op = OPS.get(cond.get("op") or spec["op"])
    if op is None:
        return False, val
    try:
        thr = float(cond.get("value"))
    except (TypeError, ValueError):
        return False, val
    return bool(op(val, thr)), val


def clean(raw, options=None):
    """请求体条件列表 → (清洗后的条件, 错误文案)。

    options: {field: [合法取值]} (来自当前因子库快照); 文本类会校验取值属于该集合
    (集合非空时), 避免任意字符串进入推送文案与结果明细。
    """
    if not isinstance(raw, list):
        return None, "条件无效"
    if len(raw) > MAX_CONDITIONS:
        return None, f"条件最多 {MAX_CONDITIONS} 条"
    out = []
    for c in raw:
        if not isinstance(c, dict):
            return None, "条件无效"
        spec = BY_KEY.get(c.get("metric"))
        if spec is None:
            return None, "条件无效"
        kind = spec["kind"]
        item = {"metric": spec["key"], "label": spec["label"], "kind": kind,
                "field": spec["field"], "op": c.get("op") or spec["op"],
                "value": c.get("value"), "value2": c.get("value2")}
        if kind == "bool":
            item["value"] = None
            item["value2"] = None
        elif kind == "text":
            v = "" if item["value"] is None else " ".join(str(item["value"]).split())
            if not v:
                return None, f"{spec['label']} 需要填写取值"
            if len(v) > TEXT_MAX_LEN:
                return None, f"{spec['label']} 取值过长 (最多 {TEXT_MAX_LEN} 字)"
            allowed = (options or {}).get(spec["field"]) or []
            if allowed and v not in allowed:
                return None, f"{spec['label']} 取值不在当前可选范围内"
            item["value"] = v
        elif kind == "num":
            try:
                item["value"] = float(item["value"])
            except (TypeError, ValueError):
                return None, f"{spec['label']} 需要填写数值"
            if item["op"] not in OPS_BY_KIND["num"]:
                item["op"] = spec["op"]
        else:   # range
            try:
                item["value"] = float(item["value"])
                item["value2"] = float(item["value2"])
            except (TypeError, ValueError):
                return None, f"{spec['label']} 需要填写区间上下限"
        out.append(item)
    if not out:
        return None, "至少一个条件"
    return out, None


def evaluate_all(row, conditions, mode="and"):
    """多条件判定 → (passed, hits, hit_count)。

    mode='and' 全部满足; mode='or' 命中任意一项 (hit_count 供「满足 N 项」分组)。
    hits 与 conditions 一一对应, 元素为 (ok, 展示值)。
    """
    hits = [evaluate(c["metric"], row, c) for c in conditions]
    count = sum(1 for ok, _v in hits if ok)
    passed = count == len(conditions) if mode == "and" else count >= 1
    return passed, hits, count


def text_field_options(df, fields):
    """从快照统计文本类指标的可选值 (行业等), 供前端 datalist。"""
    out = {}
    for f in fields:
        if f in df.columns:
            vals = df[f].dropna().astype(str)
            vals = vals[vals.str.len() > 0].value_counts().index.tolist()
            out[f] = sorted(vals)
    return out
