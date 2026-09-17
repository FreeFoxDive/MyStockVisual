"""Flask 路由: 持仓监控页 (/api/monitor/overview、/api/monitor/enabled)。"""
from __future__ import annotations

import logging

from flask import request

import market
import market_hours
import monitor
import trades
from api import api_bp
from api.common import _error, _json, _monitor_error_label, _read_json_body, _require_user

log = logging.getLogger("api")


def _with_name(rows, key="name"):
    """给每条挂上标的名称 —— 监控页的「标的」列统一只显示名称, 每条都要有标的名称。

    趋势线监控与告警流水落库时只存 symbol, 这里用 24h 刷新的名称映射补齐
    (market._lookup_name: 指数→指数名, 股票/ETF→列表名, 查不到回落 symbol 本身,
    与 quote/kline 同一口径)。

    key 可指定: 趋势线监控载荷里的 name 是**监控线自己的名字** (用户在画线上起的,
    如"上升支撑线"), 不是标的名称 —— 那张表必须用 symbol_name, 否则标的列会显示
    线名。两家共用一个键正是最容易出错的点。
    """
    try:
        for r in rows:
            if not str(r.get(key) or "").strip():   # 空白串也算没名称
                r[key] = market._lookup_name(r.get("symbol"))
    except Exception as e:
        # 名称只是显示用: 查不到就回落代码, 绝不能因此把整页数据打空
        log.warning("补全标的名称失败: %s", e)
        for r in rows:
            if not str(r.get(key) or "").strip():
                r[key] = r.get("symbol") or ""
    return rows


def _merge_positions(user):
    """风控价持仓与到期提醒持仓按 trade id 合并, 供监控页单表展示。"""
    scope_all = user.get("is_admin") and (request.args.get("all") or "").lower() in ("1", "true")
    uid = None if scope_all else user["id"]
    risk_rows = trades.list_monitored_positions(uid)
    hold_rows = {r["id"]: r for r in trades.list_hold_expire_positions()
                 if uid is None or r["user_id"] == uid}

    merged = {}
    for r in risk_rows:
        row = dict(r)
        row["has_risk_prices"] = True
        row["has_hold_expire"] = False
        merged[r["id"]] = row
    for hid, h in hold_rows.items():
        if hid in merged:
            merged[hid].update({
                "entry_date": h.get("entry_date") or merged[hid].get("entry_date"),
                "model_id": h.get("model_id"),
                "model_name": h.get("model_name"),
                "hold_days": h.get("hold_days"),
                "hold_anchor_date": h.get("hold_anchor_date"),
                "has_hold_expire": True,
            })
        else:
            row = dict(h)
            row["has_risk_prices"] = False
            row["has_hold_expire"] = True
            merged[hid] = row

    positions = list(merged.values())
    for p in positions:
        # 到期日: 起算日 + hold_days 个交易日 (与 monitor._check_hold_expire 口径一致)
        p["hold_end_date"] = None
        p["hold_days_left"] = None
        if p.get("has_hold_expire") and p.get("hold_days"):
            try:
                anchor = p.get("hold_anchor_date") or p.get("entry_date")
                end = market_hours.nth_trading_day(anchor, int(p["hold_days"]))
                p["hold_end_date"] = end
                if end:
                    today = market_hours.now().strftime("%Y-%m-%d")
                    p["hold_days_left"] = _trading_days_between(today, end)
            except Exception:
                log.warning("计算持仓到期日失败: %s", p.get("symbol"), exc_info=True)
        p.pop("is_admin", None)
        p.pop("monitor_enabled", None)
    positions.sort(key=lambda r: (r.get("symbol") or "", r.get("id") or 0))
    return positions, scope_all


def _trading_days_between(start_day, end_day):
    """start_day(含) 到 end_day(含) 之间的交易日个数; start>end 返回 0 (已到期)。"""
    try:
        y0, y1 = int(str(start_day)[:4]), int(str(end_day)[:4])
        days = market_hours._xshg_days(y0 - 1, y1 + 1)
        if end_day < start_day:
            return 0
        return sum(1 for d in days if start_day <= d <= end_day)
    except Exception:
        return None


@api_bp.route("/api/monitor/overview", methods=["GET"])
def monitor_overview():
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    positions, scope_all = _merge_positions(user)
    st = monitor.get_status() or {}
    if st.get("last_error"):
        st["last_error"] = _monitor_error_label(st["last_error"])
    return _json({
        "positions": _with_name(positions),
        "scope_all": scope_all,
        "username": user["username"],
        "is_admin": bool(user.get("is_admin")),
        "monitor_enabled": bool(user.get("is_admin") or user.get("monitor_enabled")),
        "status": st,
        "alerts": _with_name(trades.list_monitor_alerts(user["id"], limit=20)),
        # 价格监控: 趋势线跌破 (配置挂在画线上) 与任意条件预警, 均限当前用户。
        # 趋势线那张表用 symbol_name: 它的 name 是监控线自己的名字。
        "trendline_monitors": _with_name(trades.list_trendline_monitors(user["id"]), "symbol_name"),
        "price_alerts": _with_name(trades.list_price_alerts(user["id"])),
    })


@api_bp.route("/api/monitor/enabled", methods=["POST"])
def monitor_enabled_toggle():
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    body = _read_json_body()
    if body is None:
        return _error("请求体无效 JSON", 400)
    enabled = body.get("enabled")
    if enabled not in (True, False):
        return _error("enabled 必须为布尔值")
    if user.get("is_admin"):
        # 管理员恒开: 拒绝关闭自身监控
        return _json({"ok": False, "is_admin": True,
                      "monitor_enabled": True,
                      "message": "管理员监控恒开, 无需开关"})
    ok = trades.set_user_monitor(user["id"], bool(enabled))
    if not ok:
        return _error("更新失败", 500)
    return _json({"ok": True, "is_admin": False, "monitor_enabled": bool(enabled)})
