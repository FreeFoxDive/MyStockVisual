"""Flask 路由: 持仓监控页 (/api/monitor/overview、/api/monitor/enabled)。"""
from __future__ import annotations

import logging

from flask import request

import market_hours
import monitor
import trades
from api import api_bp
from api.common import _error, _json, _read_json_body, _require_user
from logger import redact_message

log = logging.getLogger("api")


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
        st["last_error"] = redact_message(st["last_error"])
    return _json({
        "positions": positions,
        "scope_all": scope_all,
        "username": user["username"],
        "is_admin": bool(user.get("is_admin")),
        "monitor_enabled": bool(user.get("is_admin") or user.get("monitor_enabled")),
        "status": st,
        "alerts": trades.list_monitor_alerts(user["id"], limit=20),
        # 价格监控: 趋势线跌破 (配置挂在画线上) 与任意条件预警, 均限当前用户
        "trendline_monitors": trades.list_trendline_monitors(user["id"]),
        "price_alerts": trades.list_price_alerts(user["id"]),
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
