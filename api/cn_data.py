"""Flask 路由: A 股特色数据 (/api/cn/*, akshare 数据源, 内存缓存 + 重试)。

资金流向/龙虎榜/分红送配/公告 均为个股维度 (龙虎榜按当前代码过滤)。
akshare/东财连接抖动常见 (RemoteDisconnected), 统一 _retry 重试 + 友好错误文案;
列名按候选匹配映射为中文, 缺列省略。
"""
from __future__ import annotations

import logging
import threading
import time

from flask import request

import market
import market_hours
from api import api_bp
from api.common import _error, _json, _require_user
from logger import redact_message

log = logging.getLogger("api")

CACHE_TTL = 900  # 15 分钟
_cache = {}
_cache_lock = threading.Lock()


def _retry(fetcher, attempts=3, delay=0.8):
    """akshare/东财连接抖动常见, 统一重试。"""
    last = None
    for i in range(attempts):
        try:
            return fetcher()
        except Exception as e:
            last = e
            if i < attempts - 1:
                time.sleep(delay * (i + 1))
    raise last


def _cached(key, fetcher, ttl=CACHE_TTL):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    try:
        data = _retry(fetcher)
        with _cache_lock:
            _cache[key] = (time.time(), data)
        return data
    except Exception as e:
        log.warning(f"cn 数据拉取失败 {key}: {redact_message(str(e))}")
        if hit:
            return hit[1]  # 过期回退
        reason = (redact_message(str(e)) or type(e).__name__)[:80]
        return {"error": f"数据源暂时不可用 ({reason})"}


def _clean_cell(v):
    if v is None:
        return None
    try:
        if v != v:  # NaN
            return None
    except TypeError:
        return None
    if hasattr(v, "item"):
        try:
            return v.item()
        except Exception:
            pass
    return v


def _rows_pick(df, colmap, limit=12, date_key=None):
    """按 colmap (raw列名子串 → 中文输出名) 选列; 取最新 limit 条, 按日期倒序 (新→旧)。

    date_key: 输出列里含日期的键名, 用于倒序排序; 候选均未命中时回退前几列原样输出。
    """
    if df is None or len(df) == 0:
        return []
    cols = list(df.columns)
    picked = []
    for src, dst in colmap:
        match = next((c for c in cols if c == src or src in str(c)), None)
        if match is not None:
            picked.append((match, dst))
    if not picked:  # 列名全不认识: 回退前 4 列
        picked = [(c, str(c)) for c in cols[:4]]
    out = []
    for _, r in df.tail(limit).iterrows():  # tail = 最新记录 (akshare 历史为升序)
        row = {}
        for match, dst in picked:
            row[dst] = _clean_cell(r[match])
        if any(v is not None for v in row.values()):
            out.append(row)
    if date_key:
        try:
            out.sort(key=lambda r: str(r.get(date_key) or ""), reverse=True)
        except Exception:
            pass
    return out


def _cn_market(symbol):
    if symbol.endswith(".SH"):
        return "sh"
    if symbol.endswith(".BJ"):
        return "bj"
    return "sz"


# ── 东财个股资金流: push2his(历史) → push2delay(最新一日) 回退 ──
# push2his 与 push2delay 是两个不同 IP 的集群, 部分网络只放行其中一个;
# push2delay 无论 lmt 传多少都只返回最新一个交易日, 故作为降级源。
_EM_FFLOW_PATH = "/api/qt/stock/fflow/daykline/get"
_EM_FFLOW_UT = "b2884a393a59ad64002292a3e90d46a5"
_EM_FFLOW_FIELDS2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"
_EM_FFLOW_HOSTS = ("push2his.eastmoney.com", "push2delay.eastmoney.com")
# 列序与 akshare.stock_individual_fund_flow 一致 (15 字段/行)
_EM_FFLOW_COLUMNS = [
    "日期", "主力净流入-净额", "小单净流入-净额", "中单净流入-净额", "大单净流入-净额",
    "超大单净流入-净额", "主力净流入-净占比", "小单净流入-净占比", "中单净流入-净占比",
    "大单净流入-净占比", "超大单净流入-净占比", "收盘价", "涨跌幅", "-", "-",
]
_EM_FFLOW_NUMERIC = (
    "主力净流入-净额", "小单净流入-净额", "中单净流入-净额", "大单净流入-净额",
    "超大单净流入-净额", "主力净流入-净占比", "小单净流入-净占比", "中单净流入-净占比",
    "大单净流入-净占比", "超大单净流入-净占比", "收盘价", "涨跌幅",
)
# 东财返回净额为「元」, 界面列名标的是「万」
_EM_FFLOW_WAN_COLUMNS = ("主力净流入-净额", "超大单净流入-净额")


def _fetch_fund_flow(code, market):
    """东财个股资金流日线。返回 (DataFrame, degraded)。

    依次尝试 push2his(历史全量) → push2delay(仅最新一日), 任一可用即返回;
    degraded=True 表示只拿到 push2delay 快照。金额列已换算为「万元」。
    两路都失败时抛最后一个异常。
    """
    import pandas as pd
    import requests

    secid = f"{1 if market == 'sh' else 0}.{code}"
    params = {
        "lmt": "0", "klt": "101", "secid": secid,
        "fields1": "f1,f2,f3,f7", "fields2": _EM_FFLOW_FIELDS2,
        "ut": _EM_FFLOW_UT, "_": int(time.time() * 1000),
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/81.0.4044.138 Safari/537.36",
    }
    last_err = None
    for i, host in enumerate(_EM_FFLOW_HOSTS):
        try:
            resp = requests.get(f"https://{host}{_EM_FFLOW_PATH}", params=params,
                                headers=headers, timeout=10)
            resp.raise_for_status()
            klines = ((resp.json() or {}).get("data") or {}).get("klines") or []
            if not klines:
                raise ValueError(f"{host} 返回空 klines")
            df = pd.DataFrame([row.split(",") for row in klines], columns=_EM_FFLOW_COLUMNS)
            for col in _EM_FFLOW_NUMERIC:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            for col in _EM_FFLOW_WAN_COLUMNS:
                df[col] = df[col] / 1e4
            return df, i > 0
        except Exception as e:
            log.warning(f"资金流源 {host} 失败: {redact_message(str(e))}")
            last_err = e
    raise last_err


@api_bp.route("/api/cn/fund-flow", methods=["GET"])
def cn_fund_flow():
    """个股资金流向 (近若干日主力/超大单净流入)。仅 A 股。"""
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    symbol = (request.args.get("symbol") or "").strip().upper()
    if not symbol:
        return _error("缺少 symbol")
    if market._symbol_market(symbol) != "cn":
        return _json({"rows": [], "hint": "港股/美股暂无该数据"})
    code = symbol.split(".")[0]

    def fetch():
        df, degraded = _fetch_fund_flow(code, _cn_market(symbol))
        out = {"rows": _rows_pick(df, [
            ("日期", "日期"), ("收盘价", "收盘"), ("涨跌幅", "涨跌幅"),
            ("主力净流入-净额", "主力净额(万)"), ("主力净流入-净占比", "主力占比%"),
            ("超大单净流入-净额", "超大单净额(万)"),
        ], limit=10, date_key="日期")}
        if degraded:
            out["hint"] = "历史接口 (push2his) 当前不可达, 仅显示最新一个交易日"
        return out

    return _json(_cached(f"ff:{symbol}", fetch))


@api_bp.route("/api/cn/lhb", methods=["GET"])
def cn_lhb():
    """龙虎榜 (近 7 日, 按当前代码过滤; 无记录时返回空列表)。"""
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    symbol = (request.args.get("symbol") or "").strip().upper()
    if not symbol:
        return _error("缺少 symbol")
    code = symbol.split(".")[0]

    def fetch():
        import akshare as ak
        import datetime as dt
        end = market_hours.now().strftime("%Y%m%d")
        start = (market_hours.now() - dt.timedelta(days=7)).strftime("%Y%m%d")
        df = ak.stock_lhb_detail_em(start_date=start, end_date=end)
        if df is None or len(df) == 0:
            return {"rows": [], "hint": "近 7 日无龙虎榜数据"}
        code_col = next((c for c in df.columns if "代码" in str(c)), None)
        if code_col:
            mask = df[code_col].astype(str).str.zfill(6) == code
            df = df[mask]
        return {"rows": _rows_pick(df, [
            ("上榜日", "上榜日"), ("解读", "解读"), ("收盘价", "收盘"),
            ("涨跌幅", "涨跌幅"), ("龙虎榜净买额", "净买额(万)"),
        ], limit=15, date_key="上榜日"), "hint": "" if len(df) else "近 7 日该股无龙虎榜记录"}

    return _json(_cached(f"lhb:{symbol}", fetch, ttl=1800))


@api_bp.route("/api/cn/dividends", methods=["GET"])
def cn_dividends():
    """分红送配 (按代码)。"""
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    symbol = (request.args.get("symbol") or "").strip().upper()
    if not symbol:
        return _error("缺少 symbol")
    if market._symbol_market(symbol) != "cn":
        return _json({"rows": [], "hint": "港股/美股暂无该数据"})
    code = symbol.split(".")[0]

    def fetch():
        import akshare as ak
        df = ak.stock_fhps_detail_em(symbol=code)
        return {"rows": _rows_pick(df, [
            ("公告日期", "公告日期"), ("报告期", "报告期"),
            ("送转股份-送转总比例", "送转比例"),
            # 描述列形如 "10派3.50元(含税)", 直接可读; 裸比例 (每10股派息元) 不直观
            ("现金分红-现金分红比例描述", "现金分红"),
            ("除权除息日", "除权除息日"),
        ], limit=12, date_key="公告日期")}

    return _json(_cached(f"div:{symbol}", fetch, ttl=3600))


@api_bp.route("/api/cn/announcements", methods=["GET"])
def cn_announcements():
    """近期公告 (巨潮资讯, 按代码, 近 90 天)。"""
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    symbol = (request.args.get("symbol") or "").strip().upper()
    if not symbol:
        return _error("缺少 symbol")
    if market._symbol_market(symbol) != "cn":
        return _json({"rows": [], "hint": "港股/美股暂无该数据"})
    code = symbol.split(".")[0]

    def fetch():
        import akshare as ak
        import datetime as dt
        end = market_hours.now().strftime("%Y%m%d")
        start = (market_hours.now() - dt.timedelta(days=90)).strftime("%Y%m%d")
        df = ak.stock_zh_a_disclosure_report_cninfo(
            symbol=code, market="沪深京", start_date=start, end_date=end)
        return {"rows": _rows_pick(df, [
            ("公告日期", "日期"), ("公告标题", "标题"), ("公告链接", "链接"),
        ], limit=15, date_key="日期")}

    return _json(_cached(f"ann:{symbol}", fetch, ttl=1800))
