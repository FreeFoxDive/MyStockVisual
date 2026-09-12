"""Flask 路由: A 股特色数据 (/api/cn/*, akshare 数据源, 内存缓存 + 重试)。

资金流向/龙虎榜/分红送配/公告 为个股维度 (龙虎榜按当前代码过滤),
北向资金为市场维度。akshare/东财连接抖动常见 (RemoteDisconnected),
统一 _retry 重试 + 友好错误文案; 列名按候选匹配映射为中文, 缺列省略。
"""
from __future__ import annotations

import threading
import time

from flask import request

import market
import market_hours
from api import api_bp
from api.common import _error, _json, _require_user
from logger import redact_message

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
        if hit:
            return hit[1]  # 过期回退
        return {"error": "数据源暂时不可用, 请稍后重试"}


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


def _rows_pick(df, colmap, limit=12):
    """按 colmap (raw列名子串 → 中文输出名) 选列; 候选均未命中时回退前几列原样输出。"""
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
    for _, r in df.head(limit).iterrows():
        row = {}
        for match, dst in picked:
            row[dst] = _clean_cell(r[match])
        if any(v is not None for v in row.values()):
            out.append(row)
    return out


def _cn_market(symbol):
    if symbol.endswith(".SH"):
        return "sh"
    if symbol.endswith(".BJ"):
        return "bj"
    return "sz"


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
        import akshare as ak
        df = ak.stock_individual_fund_flow(stock=code, market=_cn_market(symbol))
        return {"rows": _rows_pick(df, [
            ("日期", "日期"), ("收盘价", "收盘"), ("涨跌幅", "涨跌幅"),
            ("主力净流入-净额", "主力净额(万)"), ("主力净流入-净占比", "主力占比%"),
            ("超大单净流入-净额", "超大单净额(万)"),
        ], limit=10)}

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
        ], limit=15), "hint": "" if len(df) else "近 7 日该股无龙虎榜记录"}

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
            ("送转股份-送转总股本比例", "送转比例"), ("现金分红-现金分红比例", "现金分红"),
            ("除权除息日", "除权除息日"),
        ], limit=12)}

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
        ], limit=15)}

    return _json(_cached(f"ann:{symbol}", fetch, ttl=1800))


@api_bp.route("/api/cn/north", methods=["GET"])
def cn_north():
    """北向资金 (沪股通/深股通近期净流入, 市场维度)。"""
    user = _require_user()
    if not user:
        return _error("未登录", 401)

    def fetch():
        import akshare as ak
        df = ak.stock_hsgt_fund_flow_summary_em()
        return {"rows": _rows_pick(df, [
            ("交易日期", "日期"), ("类型", "类型"),
            ("净买额", "净买额(亿)"), ("成交净值比", "成交净值比%"),
        ], limit=10)}

    return _json(_cached("north", fetch, ttl=1800))
