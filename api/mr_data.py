"""Flask 路由: 麦蕊(mairui)市场数据 (/api/cn/zljlr|exchange-announcement|holder-change|top-holders|float-holders|unlock)。

数据源为麦蕊 SDK, 取数与 TTL 缓存均在 market.mr_* 数据层完成 (全市场榜单长缓存
进程共享), 本模块只做中文列名映射, 复用抽屉的通用表格协议 {rows, hint}。
"""
from __future__ import annotations

import datetime
import logging
import re

from flask import request

import market
from api import api_bp
from api.common import _error, _json, _require_user

log = logging.getLogger("api")


def _yi(v):
    """元 → 亿元 (保留 2 位); None 原样返回。"""
    f = market._safe_float(v)
    return round(f / 1e8, 2) if f is not None else None


def _iso_date(v):
    """日期归一成 YYYY-MM-DD; 认不出的格式返回 None (调用方按空值处理)。

    数据源可能给 2025/01/05、20250105 或带时间的串; 前端「近三年」窗口是按 ISO
    字符串直接比较的, 格式不归一就会静默把窗口外的期数纳进来, 所以卡在出口这一层。
    """
    m = re.match(r"(\d{4})[-/.]?(\d{2})[-/.]?(\d{2})", str(v or "").strip())
    if not m:
        return None
    try:
        return datetime.date(*(int(g) for g in m.groups())).isoformat()
    except ValueError:
        return None


def _cn_ctx(require_market=True):
    """解析 symbol; 返回 (symbol, code, err_response)。

    require_market=True 时非 A 股直接返回「暂无该数据」提示。
    """
    raw = (request.args.get("symbol") or "").strip().upper()
    if not raw:
        return None, None, _error("缺少 symbol")
    symbol = market.normalize_symbol(raw)
    code = symbol.split(".")[0]
    if require_market and market._symbol_market(symbol) != "cn":
        return symbol, code, _json({"rows": [], "hint": "港股/美股暂无该数据"})
    return symbol, code, None


@api_bp.route("/api/cn/zljlr", methods=["GET"])
def cn_zljlr():
    """当前股票主力净流入 (单票键值视图; 全市场排名见 hint)。

    数据源 /higg/zljlr 为全市场快照 (每只股票一行, 按主力净流入额倒序):
    zljlr=主力净流入(元), zljlrl=主力净流入率%, zllrzj/zllczj=主力流入/流出资金,
    jlr/jlrl=净流入(全单)/率, 恒等式 zljlr = zllrzj - zllczj。
    (该接口的 cje/hsl 恒为 0, 故不展示。)
    """
    if not _require_user():
        return _error("未登录", 401)
    symbol, code, err = _cn_ctx(require_market=False)
    if err:
        return err

    raw = market.mr_zljlr()
    if not raw:
        return _json({"kv": [], "hint": "数据源暂时不可用"})

    rows = [r for r in raw if isinstance(r, dict)]
    total = len(rows)
    idx = next(
        (i for i, r in enumerate(rows) if str(r.get("dm") or "").strip().zfill(6) == code),
        None,
    )
    if idx is None:
        return _json({"kv": [], "hint": f"该股不在主力净流入榜单中（全市场 {total} 只）"})

    r = rows[idx]
    raw_t = str(r.get("t") or "")
    t = raw_t if len(raw_t) <= 10 else raw_t[:10] + " " + raw_t[10:]
    kv = [
        ["主力净流入额(亿)", _yi(r.get("zljlr"))],
        ["主力净流入率%", market._safe_float(r.get("zljlrl"))],
        ["主力流入(亿)", _yi(r.get("zllrzj"))],
        ["主力流出(亿)", _yi(r.get("zllczj"))],
        ["净流入额(亿)", _yi(r.get("jlr"))],
        ["净流入率%", market._safe_float(r.get("jlrl"))],
        ["最新价", market._safe_float(r.get("zxj"))],
        ["涨跌幅%", market._safe_float(r.get("zdf"))],
        ["全市场排名", f"{idx + 1}/{total}"],
        ["数据时间", t or "—"],
    ]
    return _json({"kv": kv, "hint": ""})


@api_bp.route("/api/cn/exchange-announcement", methods=["GET"])
def cn_exchange_announcement():
    """交易所公告 (麦蕊, 按代码; 源为升序, 倒序后最新在前)。"""
    if not _require_user():
        return _error("未登录", 401)
    _symbol, code, err = _cn_ctx()
    if err:
        return err
    raw = market.mr_announcements(code)
    if raw is None:
        return _json({"rows": [], "hint": "数据源暂时不可用"})
    rows = [
        {"日期": r.get("t"), "标题": r.get("zt"), "链接": r.get("nr")}
        for r in raw if isinstance(r, dict)
    ]
    rows.sort(key=lambda x: str(x["日期"] or ""), reverse=True)
    return _json({"rows": rows, "hint": "" if rows else "暂无公告"})


@api_bp.route("/api/cn/holder-change", methods=["GET"])
def cn_holder_change():
    """股东户数变化趋势 (按截止日期倒序)。"""
    if not _require_user():
        return _error("未登录", 401)
    _symbol, code, err = _cn_ctx()
    if err:
        return err
    raw = market.mr_holder_change(code)
    if raw is None:
        return _json({"rows": [], "hint": "数据源暂时不可用"})
    rows = [
        {
            "截止日期": _iso_date(r.get("jzrq")),
            "股东户数": market._safe_int(r.get("gdhs")),
            "增减": r.get("bh"),
        }
        for r in raw if isinstance(r, dict)
    ]
    # 上游顺序不作契约: 显式按截止日期倒序 (最新在上), 与公告路由同做法
    rows.sort(key=lambda x: str(x["截止日期"] or ""), reverse=True)
    return _json({"rows": rows, "hint": "" if rows else "暂无数据"})


def _holder_rows(raw):
    """十大(流通)股东: 取最新报告期并展平嵌套 sdgd 为扁平行。"""
    if raw is None:
        return [], "数据源暂时不可用"
    latest = next((r for r in raw if isinstance(r, dict)), None)
    if not latest:
        return [], "暂无数据"
    jzrq, ggrq = latest.get("jzrq"), latest.get("ggrq")
    rows = []
    for it in (latest.get("sdgd") or []):
        if not isinstance(it, dict):
            continue
        cgsl = market._safe_float(it.get("cgsl"))
        rows.append({
            "报告期": jzrq,
            "排名": market._safe_int(it.get("pm")),
            "股东名称": it.get("gdmc"),
            "持股数(万股)": round(cgsl / 1e4, 2) if cgsl is not None else None,
            "持股比例%": market._safe_float(it.get("cgbl")),
            "股份性质": it.get("gbxz"),
        })
    if not rows:
        return [], "暂无数据"
    hint = f"报告期 {jzrq}" + (f" · 公告 {ggrq}" if ggrq else "")
    return rows, hint


@api_bp.route("/api/cn/top-holders", methods=["GET"])
def cn_top_holders():
    """十大股东 (麦蕊 /hscp/sdgd, 展示最新报告期)。"""
    if not _require_user():
        return _error("未登录", 401)
    _symbol, code, err = _cn_ctx()
    if err:
        return err
    rows, hint = _holder_rows(market.mr_top_holders(code))
    return _json({"rows": rows, "hint": hint})


@api_bp.route("/api/cn/float-holders", methods=["GET"])
def cn_float_holders():
    """十大流通股东 (麦蕊 /hscp/ltgd, 展示最新报告期)。"""
    if not _require_user():
        return _error("未登录", 401)
    _symbol, code, err = _cn_ctx()
    if err:
        return err
    rows, hint = _holder_rows(market.mr_float_holders(code))
    return _json({"rows": rows, "hint": hint})


def _float_shares(symbol):
    """流通股本(股): AlphaFeed 元数据优先, 回退 麦蕊流通市值 ÷ 最新价; 失败 None。"""
    try:
        meta = market._fetch_instrument_meta(symbol)
    except Exception:
        meta = None
    fs = market._safe_float((meta or {}).get("float_shares"))
    if fs:
        return fs
    try:
        inst = market._mr_instrument(symbol)
    except Exception:
        inst = None
    fv = market._safe_float((inst or {}).get("float_value"))
    if not fv:
        return None
    try:
        q = market.fetch_quote(symbol)
    except Exception:
        q = None
    price = market._safe_float((q or {}).get("last_price"))
    return fv / price if price else None


@api_bp.route("/api/cn/unlock", methods=["GET"])
def cn_unlock():
    """解禁限售 (按解禁日期倒序; 附 占流通股% 与反推解禁均价)。

    麦蕊 /hscp/jjxs 的 `rprice` 实测并非单价, 而是**解禁市值(亿元)**
    (≈ 解禁数量 × 解禁日收盘价; 5 只票交叉验证一致), 故单价用 市值/数量 反推。
    """
    if not _require_user():
        return _error("未登录", 401)
    symbol, code, err = _cn_ctx()
    if err:
        return err
    raw = market.mr_unlock(code)
    if raw is None:
        return _json({"rows": [], "hint": "数据源暂时不可用"})

    fs = _float_shares(symbol)
    rows = []
    for r in raw:
        if not isinstance(r, dict):
            continue
        ra = market._safe_float(r.get("ramount"))   # 万股
        rv = market._safe_float(r.get("rprice"))    # 解禁市值(亿元)
        rows.append({
            "解禁日期": r.get("rdate"),
            "解禁数量(万股)": ra,
            "解禁市值(亿)": rv,
            "解禁均价(元)": round(rv * 1e4 / ra, 2) if (rv is not None and ra) else None,
            "占流通股%": round(ra * 1e4 / fs * 100, 2) if (fs and ra is not None) else None,
            "批次": market._safe_int(r.get("batch")),
            "公告日期": r.get("pdate"),
        })
    return _json({"rows": rows, "hint": "" if rows else "暂无解禁记录"})
