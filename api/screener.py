"""Flask 路由: 条件选股 screener (/api/screener/*)。

后台线程扫描 A 股列表 (数量受 SCREENER_MAX_SYMBOLS 限制, 默认 300),
逐标的拉日K (磁盘缓存复用) 计算 MACD金叉/站上MA20/RSI/涨跌幅 条件;
筹码获利盘条件走 chips 缓存 (未缓存的标的跳过, 避免全市场拉取)。

限频: 每只标的取数前等待 SCREENER_KLINE_PER_MIN (默认 6/min) 令牌,
阻塞式节流尊重数据源配额 (冷缓存约 6 只/分钟); 取消时立即退出。
与页面解耦: 关闭页面扫描继续, 结果落盘 .cache/screener_last.json,
支持手动取消 (保留已扫描的部分结果)。
"""
from __future__ import annotations

import json
import os
import threading
import time

from flask import request

import feed
import market
from api import api_bp
from api.common import _error, _json, _read_json_body, _require_user
from indicators import macd, rsi, sma

CONDITION_METRICS = {"macd_cross_up", "above_ma20", "chip_profit_gt", "rsi6_lt", "change_pct_gt"}

_job_lock = threading.Lock()
_job = {
    "running": False, "stop": False, "stopped": False, "progress": 0, "total": 0,
    "results": [], "conditions": [], "started_at": None, "done_at": None, "error": None,
}
_last_file = market.SCRIPT_DIR / ".cache" / "screener_last.json"
_scan_bucket = feed.TokenBucket(rate_per_min=int(os.environ.get("SCREENER_KLINE_PER_MIN", "6")))


def _persist_job():
    """完成的扫描落盘 (重启后仍可查看最近一次结果)。"""
    try:
        snap = {k: _job.get(k) for k in ("results", "conditions", "done_at", "stopped")}
        snap["saved_at"] = time.time()
        _last_file.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _load_last_job():
    """服务重启后恢复最近一次扫描结果 (仅在内存无结果时)。"""
    with _job_lock:
        if _job["running"] or _job["done_at"] or _job["results"]:
            return
    try:
        if _last_file.exists():
            raw = json.loads(_last_file.read_text(encoding="utf-8"))
            with _job_lock:
                _job.update({
                    "running": False, "progress": len(raw.get("results") or []),
                    "total": len(raw.get("results") or []),
                    "results": raw.get("results") or [],
                    "conditions": raw.get("conditions") or [],
                    "started_at": None, "done_at": raw.get("done_at"),
                    "stopped": bool(raw.get("stopped")), "error": None,
                })
    except Exception:
        pass


def _scan_worker(conditions, count):
    try:
        cap = int(os.environ.get("SCREENER_MAX_SYMBOLS", "300"))
        universe = market._load_stock_list()[:cap]
        with _job_lock:
            _job.update({"running": True, "stop": False, "stopped": False, "progress": 0,
                         "total": len(universe), "results": [], "conditions": conditions,
                         "error": None, "started_at": time.time(), "done_at": None})
        wants_chips = any(c["metric"] == "chip_profit_gt" for c in conditions)
        results = []
        for i, row in enumerate(universe):
            with _job_lock:
                if _job["stop"]:
                    _job["stopped"] = True
                    break
            # 限频: 阻塞式等令牌 (每只 1 token, 冷缓存约 6 只/分钟), 取消时立即退出
            waited = 0.0
            while not _scan_bucket.try_acquire():
                time.sleep(0.5)
                waited += 0.5
                with _job_lock:
                    if _job["stop"]:
                        break
                if waited >= 120:  # 兜底: 等待超 2 分钟视为异常
                    break
            with _job_lock:
                if _job["stop"]:
                    _job["stopped"] = True
                    break

            sym = row["symbol"]
            try:
                df, name, _src = market.fetch_kline_ex(sym, "1d", count, adjust="forward")
                if df is None or len(df) < 35:
                    continue
                closes = df["close"]
                feats = {"change_pct": None}
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
                    m, vf = c["metric"], c.get("value")
                    vf = float(vf) if vf is not None else None
                    if m == "macd_cross_up":
                        ok = ok and feats.get("macd_cross_up")
                    elif m == "above_ma20":
                        ok = ok and feats.get("above_ma20")
                    elif m == "rsi6_lt":
                        ok = ok and feats.get("rsi6") is not None and feats["rsi6"] < vf
                    elif m == "change_pct_gt":
                        ok = ok and feats.get("change_pct") is not None and feats["change_pct"] > vf
                    elif m == "chip_profit_gt":
                        ok = ok and feats.get("chip_profit") is not None and feats["chip_profit"] > vf
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
        _persist_job()
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
    return _json({"ok": True})


@api_bp.route("/api/screener/stop", methods=["POST"])
def screener_stop():
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    with _job_lock:
        if not _job["running"]:
            return _json({"ok": False, "message": "没有进行中的扫描"})
        _job["stop"] = True
    return _json({"ok": True})


@api_bp.route("/api/screener/status", methods=["GET"])
def screener_status():
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    _load_last_job()
    with _job_lock:
        return _json({k: (list(v) if k == "results" else v) for k, v in _job.items()})
