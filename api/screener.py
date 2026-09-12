"""Flask 路由: 条件选股 screener (/api/screener/*)。

后台线程扫描 A 股列表 (symbol 数量受 SCREENER_MAX_SYMBOLS 限制, 默认 300),
逐标的拉日K (磁盘缓存复用) 计算 MACD金叉/站上MA20/RSI/涨跌幅 条件;
筹码获利盘条件走 chips 缓存 (未缓存的标的跳过, 避免全市场拉取)。
"""
from __future__ import annotations

import json
import threading
import time

from flask import request

import kline_source
import market
from api import api_bp
from api.common import _error, _json, _read_json_body, _require_user
from indicators import macd, sma, compute_all_indicators

_job_lock = threading.Lock()
_job = {"running": False, "progress": 0, "total": 0, "results": [], "started_at": None,
        "done_at": None, "error": None}

CONDITION_METRICS = {"macd_cross_up", "above_ma20", "chip_profit_gt", "rsi6_lt", "change_pct_gt"}


def _scan_worker(conditions, count):
    global _job
    try:
        universe = market._load_stock_list()[:int(
            __import__("os").environ.get("SCREENER_MAX_SYMBOLS", "300"))]
        with _job_lock:
            _job.update({"running": True, "progress": 0, "total": len(universe),
                         "results": [], "error": None, "started_at": time.time(), "done_at": None})
        results = []
        wants_chips = any(c["metric"] == "chip_profit_gt" for c in conditions)
        for i, row in enumerate(universe):
            sym = row["symbol"]
            try:
                df, name, _src = market.fetch_kline_ex(sym, "1d", count, adjust="forward")
                if df is None or len(df) < 35:
                    continue
                feats = {"change_pct": None}
                closes = df["close"]
                if len(closes) >= 6:
                    feats["change_pct"] = (closes.iloc[-1] - closes.iloc[-6]) / closes.iloc[-6] * 100
                if any(c["metric"] == "macd_cross_up" for c in conditions):
                    dif, dea, _h = macd(df["close"], 12, 26, 9)
                    d0, d1 = dif.iloc[-2], dif.iloc[-1]
                    e0, e1 = dea.iloc[-2], dea.iloc[-1]
                    feats["macd_cross_up"] = bool(
                        d0 == d0 and d1 == d1 and e0 == e0 and e1 == e1  # NaN 检查
                        and d0 <= e0 and d1 > e1)
                feats["above_ma20"] = bool(
                    len(closes) >= 20 and closes.iloc[-1] > sma(closes, 20).iloc[-1])
                feats["rsi6"] = None
                if any(c["metric"] == "rsi6_lt" for c in conditions):
                    from indicators import rsi
                    r = rsi(df["close"], 6)
                    feats["rsi6"] = float(r.iloc[-1]) if r.iloc[-1] == r.iloc[-1] else None
                if wants_chips:
                    try:
                        from chips import get_chips
                        ch = get_chips(sym)
                        feats["chip_profit"] = float(ch["profit_ratio"] * 100) if ch and ch.get("profit_ratio") is not None else None
                    except Exception:
                        feats["chip_profit"] = None

                ok = True
                for c in conditions:
                    m, op, v = c["metric"], c.get("op", ">="), float(c.get("value") or 0)
                    if m == "macd_cross_up":
                        ok = ok and feats.get("macd_cross_up")
                    elif m == "above_ma20":
                        ok = ok and feats.get("above_ma20")
                    elif m == "rsi6_lt":
                        ok = ok and feats.get("rsi6") is not None and feats["rsi6"] < v
                    elif m == "change_pct_gt":
                        ok = ok and feats.get("change_pct") is not None and feats["change_pct"] > v
                    elif m == "chip_profit_gt":
                        ok = ok and feats.get("chip_profit") is not None and feats["chip_profit"] > v
                    if not ok:
                        break
                if ok:
                    results.append({"symbol": sym, "name": row["name"],
                                    "close": float(closes.iloc[-1]),
                                    "change_pct": feats.get("change_pct"),
                                    "chip_profit": feats.get("chip_profit")})
            except Exception:
                continue
            finally:
                with _job_lock:
                    _job["progress"] = i + 1
        with _job_lock:
            _job["results"] = results
            _job["done_at"] = time.time()
            _job["running"] = False
    except Exception as e:
        with _job_lock:
            _job["running"] = False
            _job["error"] = str(e)


@api_bp.route("/api/screener/run", methods=["POST"])
def screener_run():
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    body = _read_json_body()
    if body is None:
        return _error("请求体无效 JSON", 400)
    conditions = body.get("conditions") or []
    clean = []
    for c in conditions:
        if not isinstance(c, dict) or c.get("metric") not in CONDITION_METRICS:
            return _error("条件无效")
        clean.append({"metric": c["metric"], "op": c.get("op", ">="), "value": c.get("value")})
    if not clean:
        return _error("至少一个条件")
    with _job_lock:
        if _job["running"]:
            return _error("扫描进行中", 409)
    count = int(body.get("count") or 120)
    threading.Thread(target=_scan_worker, args=(clean, min(count, 500)), daemon=True).start()
    return _json({"ok": True, "total_hint": clean and len(clean)})


@api_bp.route("/api/screener/status", methods=["GET"])
def screener_status():
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    with _job_lock:
        return _json({k: (list(v) if k == "results" else v) for k, v in _job.items()})
