"""日内走势接口 (/v1/klines/intraday) 的当日可用性状态。

分时取数优先走这个接口 (A股实测: 只回当日、与分钟K批量数值逐列一致, 见
docs/alphafeed-limits.md), 但权限被拒就没必要在同一交易日里反复重试 —— 每次重试
都要等一次超时才落到原接口, 监控轮询下还会刷一屏同样的警告。所以这里只记一件事:

    **某市场 本交易日 日内走势接口是否已被判定权限不可用** (跨日自动恢复)。

按**市场**分别记, 不是全局: AlphaFeed 的权限是按功能 + **按市场**授权的, 实测
2026-09-18 本套餐:

    600519.SH → klines.intraday OK    | klines.batch(1m) OK
    00700.HK  → 403 No permission for 日内分时查询 (markets: HK) | batch 亦 403
    AAPL.US   → 403 No permission for 日内分时查询 (markets: US) | batch 亦 403

也就是港/美股本来就拿不到 (分时靠 akshare 兜底)。要是熔断记成全局的, 自选表里
带一只美股就会把 **A股** 的日内走势偏好一起关掉 —— 那正是这条链路想避免的。

调用方约定:
    if af_intraday.available(symbol):
        try: 用日内走势接口取数
        except ... as e:
            if af_intraday.is_permission_error(e): af_intraday.note_denied(e, symbol)
            # 其余异常 (超时/空/429) 只本次回退, 不封接口 —— 偶发失败不该把
            # 一整天都降级成原接口。

权限错误按 HTTP status_code / code 判定 (alphafeed SDK 的 PermissionError 带
status_code=403, 见其 _exceptions.py); 不 import 那个 PermissionError 名字, 免得
遮蔽内建同名异常, 也免得为一次判定把 alphafeed 拉进 import 图。
"""
from __future__ import annotations

import logging
import threading

import market_hours
from logger import redact_message

log = logging.getLogger("af_intraday")

# 权限类错误: 401 未认证 / 402 需付费 / 403 无权限
_PERMISSION_STATUS = (401, 402, 403)
# 少数情况下 status 缺失, 用 code/文案兜底 (如 PLAN_NOT_INCLUDED / FORBIDDEN)
_PERMISSION_HINTS = ("permission", "forbidden", "not allowed", "plan", "upgrade", "quota")

_lock = threading.Lock()
_denied = {}      # {market: date} 已判定该市场权限不可用的那一天


def _today():
    """当前北京日期 (熔断按交易日/自然日滚动; 测试可 patch 本函数)。"""
    return market_hours.now().date()


def market_of(symbol) -> str:
    """市场归类 cn/hk/us —— 与 market._symbol_market 同口径 (本模块不能反向 import
    market: market → feed → af_intraday 已经成链, 反向引用会成环)。"""
    s = str(symbol or "").upper().strip()
    if s.endswith(".HK"):
        return "hk"
    if s.endswith(".US"):
        return "us"
    code = s.split(".")[0]
    if code.isalpha() and 1 <= len(code) <= 6:
        return "us"
    if code.isdigit() and len(code) == 5:
        return "hk"
    return "cn"


def available(symbol) -> bool:
    """该标的所在市场本日是否还能用日内走势接口。跨日自动恢复 (只针对「当日」)。"""
    denied = _denied.get(market_of(symbol))
    return denied is None or denied != _today()


def denied_markets():
    """已判定权限不可用的市场 → 日期 (供日志/测试查看)。"""
    return dict(_denied)


def is_permission_error(exc) -> bool:
    """是否属于「套餐/权限不给用」这类错误 (而非超时/限流/空数据)。

    status_code 明确给出时以它为准 (429/404/5xx 一律不算权限问题, 免得把一个限流
    错误升级成"当日不再尝试"); 只有 status 缺失时才退回 code/文案判词。
    """
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status in _PERMISSION_STATUS
    code = getattr(exc, "code", None)
    text = f"{code or ''} {exc}".lower()
    return any(h in text for h in _PERMISSION_HINTS)


def note_denied(exc=None, symbol="") -> bool:
    """记下「该市场本日 日内走势接口被拒」, 之后同一市场当日不再尝试。

    返回是否**新进入**该状态 (同一市场同一天只记一次, 日志不刷屏)。
    """
    market = market_of(symbol)
    today = _today()
    with _lock:
        if _denied.get(market) == today:
            return False
        _denied[market] = today
    detail = ""
    if exc is not None:
        status = getattr(exc, "status_code", None)
        code = getattr(exc, "code", None)
        detail = (f" [{type(exc).__name__}"
                  + (f" status_code={status}" if status is not None else "")
                  + (f" code={code}" if code else "")
                  + f"] {redact_message(exc)}")
    log.warning("日内走势接口 (klines.intraday) 本日对 %s 市场不可用%s; "
                "该市场分时取数当日一律回退分钟K批量 (klines.batch)",
                market.upper(), detail)
    return True


def reset():
    """清空状态 (测试隔离用)。"""
    with _lock:
        _denied.clear()
