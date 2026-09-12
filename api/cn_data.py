"""Flask 路由: A 股特色数据 (/api/cn/*, akshare 数据源, 磁盘缓存)。"""
from __future__ import annotations

import json
import time

from flask import request

import market
from api import api_bp
from api.common import _error, _json, _require_user
from logger import redact_message

CACHE_TTL = 900  # 15 分钟
_cache = {}


def _cached(key, fetcher, ttl=CACHE_TTL):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    try:
        data = fetcher()
        _cache[key] = (time.time(), data)
        return data
    except Exception as e:
        if hit:
            return hit[1]  # 过期回退
        return {"error": redact_message(str(e))}


def _df_rows(df, limit=30):
    if df is None or len(df) == 0:
        return []
    return json.loads(df.head(limit).to_json(orient="records", force_ascii=False))


@api_bp.route("/api/cn/fund-flow", methods=["GET"])
def cn_fund_flow():
    """个股资金流向 (近 N 日主力/超大单净流入)。"""
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    symbol = (request.args.get("symbol") or "").strip()
    if not symbol:
        return _error("缺少 symbol")
    code = symbol.split(".")[0]

    def fetch():
        import akshare as ak
        df = ak.stock_individual_fund_flow(stock=code, market=market._symbol_market(symbol) == "hk" and "hk" or ("sh" if symbol.endswith(".SH") else "sz"))
        return {"rows": _df_rows(df, 15)}

    return _json(_cached(f"ff:{symbol}", fetch))


@api_bp.route("/api/cn/lhb", methods=["GET"])
def cn_lhb():
    """最近龙虎榜 (市场级, 近 5 个交易日)。"""
    user = _require_user()
    if not user:
        return _error("未登录", 401)

    def fetch():
        import akshare as ak
        from market import market_hours
        end = market_hours.now().strftime("%Y%m%d")
        start = market_hours.now().strftime("%Y%m%d")
        import datetime as _dt
        start = (market_hours.now() - _dt.timedelta(days=7)).strftime("%Y%m%d")
        df = ak.stock_lhb_detail_em(start_date=start, end_date=end)
        return {"rows": _df_rows(df, 30)}

    return _json(_cached("lhb", fetch, ttl=1800))


@api_bp.route("/api/cn/dividends", methods=["GET"])
def cn_dividends():
    """分红送配 (按代码)。"""
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    symbol = (request.args.get("symbol") or "").strip()
    if not symbol:
        return _error("缺少 symbol")
    code = symbol.split(".")[0]

    def fetch():
        import akshare as ak
        df = ak.stock_fhps_detail_em(symbol=code)
        return {"rows": _df_rows(df, 15)}

    return _json(_cached(f"div:{symbol}", fetch, ttl=3600))


@api_bp.route("/api/cn/announcements", methods=["GET"])
def cn_announcements():
    """近期公告 (按代码, 东财公告接口)。"""
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    symbol = (request.args.get("symbol") or "").strip()
    if not symbol:
        return _error("缺少 symbol")
    code = symbol.split(".")[0]

    def fetch():
        import akshare as ak
        df = ak.stock_notice_report(symbol=code)
        return {"rows": _df_rows(df, 15)}

    return _json(_cached(f"ann:{symbol}", fetch, ttl=1800))


@api_bp.route("/api/cn/north", methods=["GET"])
def cn_north():
    """北向资金 (沪股通/深股通近期净流入)。"""
    user = _require_user()
    if not user:
        return _error("未登录", 401)

    def fetch():
        import akshare as ak
        df = ak.stock_hsgt_fund_flow_summary_em()
        return {"rows": _df_rows(df, 10)}

    return _json(_cached("north", fetch, ttl=1800))
