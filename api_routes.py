"""Flask Blueprint: all /api/* handlers except auth login/logout/me."""
from __future__ import annotations

import json
import logging

import pandas as pd
from flask import Blueprint, g, make_response, request

import kline_source
import market_hours
import trades
from indicators import compute_all_indicators, _safe_list
from logger import sanitize_error as _sanitize_error
from market import (
    MINUTE_COUNTS,
    MINUTE_PERIODS,
    NumpyEncoder,
    _fetch_etf_nav,
    _fetch_impulse_qfq,
    _fetch_mairui_quota,
    _is_etf,
    _is_index_symbol,
    _load_pledge,
    _safe_float,
    _safe_int,
    _search_stocks,
    fetch_kline_ex,
    fetch_quote,
    fetch_quotes,
    kline_cache,
    kline_cache_long,
    kline_cache_minute,
    normalize_symbol,
)

log = logging.getLogger("api")

api_bp = Blueprint("api", __name__)


def _json(data, code=200):
    body = json.dumps(data, ensure_ascii=False, cls=NumpyEncoder)
    resp = make_response(body, code)
    resp.headers["Content-Type"] = "application/json; charset=utf-8"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


def _error(msg, code=400):
    return _json({"error": msg}, code)


def _attach_quote(resp, symbol):
    """给 kline 响应挂实时快照 (30s 缓存); 失败静默, quote 缺席即可。"""
    try:
        q = fetch_quote(symbol)
        if q:
            resp["quote"] = q
    except Exception:
        pass


def _require_user():
    user = getattr(g, "user", None)
    if not user:
        return None
    return user


def _require_admin():
    user = _require_user()
    if not user:
        return None
    if not user.get("is_admin"):
        return False  # authenticated but not admin
    return user


def _read_json_body():
    """Match server.py: None = invalid JSON; {} = empty/oversized; dict otherwise."""
    raw = request.get_data(cache=True, as_text=False) or b""
    if len(raw) > 1_000_000:
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


# ── ping / search / market data ──

@api_bp.route("/api/ping", methods=["GET"])
def ping():
    now = market_hours.now()
    return _json({
        "ok": True,
        "time": str(now),
        "in_session": market_hours.in_session(now),
        "is_trading_day": market_hours.is_trading_day(now),
    })


@api_bp.route("/api/search", methods=["GET"])
def search():
    q = (request.args.get("q") or "").strip()
    if not q:
        return _json({"results": []})
    return _json({"results": _search_stocks(q)})


@api_bp.route("/api/quote", methods=["GET"])
def quote():
    symbol_raw = request.args.get("symbol")
    if not symbol_raw:
        return _error("缺少 symbol 参数")
    symbol = normalize_symbol(symbol_raw)
    try:
        q = fetch_quote(symbol)
        if q is None:
            return _error(f"无法获取 {symbol} 的快照", 404)
        return _json(q)
    except Exception as e:
        return _error(f"获取快照失败: {_sanitize_error(e)}", 500)


@api_bp.route("/api/quotes", methods=["GET"])
def quotes():
    raw = request.args.get("symbols") or ""
    symbols = [s.strip() for s in raw.split(",") if s.strip()]
    if not symbols:
        return _error("缺少 symbols 参数")
    fresh = (request.args.get("fresh") or "0").lower() in ("1", "true", "yes")
    try:
        return _json(fetch_quotes(symbols, fresh=fresh))
    except Exception as e:
        return _error(f"获取快照失败: {_sanitize_error(e)}", 500)


@api_bp.route("/api/pledge", methods=["GET"])
def pledge():
    symbol_raw = request.args.get("symbol")
    if not symbol_raw:
        return _error("缺少 symbol 参数")
    symbol = normalize_symbol(symbol_raw)
    if _is_index_symbol(symbol):
        return _json({"symbol": symbol, "pledge": None})
    code = symbol.split(".")[0]
    data = _load_pledge().get(code)
    return _json({"symbol": symbol, "pledge": data if data else None})


@api_bp.route("/api/quota", methods=["GET"])
def quota():
    if not _require_user():
        return _error("未登录", 401)
    return _json(_fetch_mairui_quota())


@api_bp.route("/api/trade-reasons", methods=["GET"])
def trade_reasons():
    return _json({"entry": trades.ENTRY_REASONS, "exit": trades.EXIT_REASONS})


@api_bp.route("/api/repo-maturity", methods=["GET"])
def repo_maturity():
    """逆回购到期日预览。

    与 create_trade 入库口径完全一致 (自然日 + XSHG 交易日历顺延, 含节假日),
    前端不再自行用"只跳周末"的近似逻辑。
    """
    if not _require_user():
        return _error("未登录", 401)
    entry_date = (request.args.get("entry_date") or "").strip()
    try:
        tenor = int(request.args.get("tenor"))
    except (TypeError, ValueError):
        return _error("tenor 无效")
    return _json({"maturity": trades._repo_maturity(entry_date, tenor)})


# ── K 线 ──

@api_bp.route("/api/kline", methods=["GET"])
def kline():
    symbol_raw = request.args.get("symbol")
    if not symbol_raw:
        return _error("缺少 symbol 参数")

    symbol = normalize_symbol(symbol_raw)
    period = request.args.get("period") or "1d"
    # 日K: 3年可见 (3×252) + RSI 收敛 warmup 250；其余非分钟默认 200
    DAILY_COUNT = 3 * 252 + 250  # 1006
    if period == "1d":
        default_count = DAILY_COUNT
    else:
        default_count = MINUTE_COUNTS.get(period, 200)
    try:
        count = min(int(request.args.get("count") or str(default_count)), 1500)
    except ValueError:
        count = default_count

    cache_key = f"{symbol}:{period}:{count}"
    skip_1d_cache = period == "1d" and market_hours.is_trading_day(
        market_hours.now().date().isoformat()
    )
    if period in MINUTE_PERIODS:
        cache = kline_cache_minute
    elif period in ("1w", "1M"):
        cache = kline_cache_long
    else:
        cache = kline_cache
    cached = None if skip_1d_cache else cache.get(cache_key)
    if cached:
        resp = cached.copy()
        resp["meta"] = dict(resp.get("meta") or {})
        resp["meta"]["cached"] = True
        _attach_quote(resp, symbol)
        return _json(resp)

    try:
        df, name, source = fetch_kline_ex(symbol, period, count)
    except Exception as e:
        return _error(f"获取K线失败: {_sanitize_error(e)}", 500)

    if df is None:
        return _error(f"无法获取 {symbol} 的K线数据", 404)

    try:
        df, indicators = compute_all_indicators(df, period)
    except Exception as e:
        log.warning(f"指标计算失败 {symbol} {period}: {e}")
        return _error(f"指标计算失败: {_sanitize_error(e)}", 500)

    if period == "1d":
        impulse_qfq = _fetch_impulse_qfq(symbol, count)
        if impulse_qfq is not None:
            aligned = impulse_qfq.reindex(df.index)
            df["impulse"] = aligned.fillna(df["impulse"]).astype(int)

    premium_data = None
    is_etf = _is_etf(symbol)
    if is_etf and period not in MINUTE_PERIODS:
        nav_df = _fetch_etf_nav(symbol)
        if nav_df is not None and len(nav_df) > 0:
            df_sorted = df.sort_index()
            premiums = []
            for idx in df_sorted.index:
                nav_matches = nav_df[nav_df.index <= idx]
                if len(nav_matches) > 0:
                    nav_val = float(nav_matches.iloc[-1]["nav"])
                    close_val = float(df_sorted.loc[idx, "close"])
                    prem = (close_val - nav_val) / nav_val * 100 if nav_val > 0 else None
                else:
                    prem = None
                premiums.append(prem)
            prem_series = pd.Series(premiums, index=df_sorted.index)
            premium_data = {
                "values": _safe_list(prem_series),
                "params": {"source": "akshare fund_open_fund_info_em"},
            }

    klines = []
    for idx, row in df.iterrows():
        date_str = str(idx)
        if hasattr(idx, "strftime"):
            date_str = idx.strftime(
                "%Y-%m-%d %H:%M" if period in MINUTE_PERIODS else "%Y-%m-%d"
            )
        entry = {
            "date": date_str,
            "open": _safe_float(row.get("open")),
            "high": _safe_float(row.get("high")),
            "low": _safe_float(row.get("low")),
            "close": _safe_float(row.get("close")),
            "volume": _safe_int(row.get("volume")),
            "amount": _safe_float(row.get("amount")),
            "ma5": _safe_float(row.get("ma5")),
            "ma10": _safe_float(row.get("ma10")),
            "ma20": _safe_float(row.get("ma20")),
            "macd_dif": _safe_float(row.get("macd_dif")),
            "macd_dea": _safe_float(row.get("macd_dea")),
            "macd_hist": _safe_float(row.get("macd_hist")),
            "rsi6": _safe_float(row.get("rsi6")),
            "rsi12": _safe_float(row.get("rsi12")),
            "rsi24": _safe_float(row.get("rsi24")),
            "kdj_k": _safe_float(row.get("kdj_k")),
            "kdj_d": _safe_float(row.get("kdj_d")),
            "kdj_j": _safe_float(row.get("kdj_j")),
            "atr14": _safe_float(row.get("atr14")),
            "ema13": _safe_float(row.get("ema13")),
            "impulse": _safe_int(row.get("impulse")),
        }
        klines.append(entry)

    if premium_data:
        prem_vals = premium_data["values"]
        for i, k in enumerate(klines):
            if i < len(prem_vals) and prem_vals[i] is not None:
                k["premium"] = prem_vals[i]
            else:
                k["premium"] = None

    resp = {
        "symbol": symbol,
        "name": name,
        "period": period,
        "count": len(klines),
        "is_etf": is_etf,
        "is_index": _is_index_symbol(symbol),
        "macd_params": indicators["macd"]["params"],
        "klines": klines,
        "meta": {
            "cached": False,
            "server_time": str(market_hours.now()),
            "last_trade_date": klines[-1]["date"] if klines else None,
            "source": source,
        },
    }

    # 缓存里不存 quote: TTLCache 存引用, 事后挂 quote 会污染缓存条目,
    # 让后续命中拿到最长 TTL 前的旧快照。改为存副本 (不含 quote), 每次返回前现挂。
    _attach_quote(resp, symbol)
    if not skip_1d_cache:
        cache.set(cache_key, {k: v for k, v in resp.items() if k != "quote"})

    return _json(resp)


@api_bp.route("/api/intraday", methods=["GET"])
def intraday():
    symbol_raw = request.args.get("symbol")
    if not symbol_raw:
        return _error("缺少 symbol 参数")
    symbol = normalize_symbol(symbol_raw)
    period = request.args.get("period") or "5m"
    try:
        count = min(int(request.args.get("count") or "120"), 250)
    except ValueError:
        count = 120
    try:
        # 分钟源经 kline_source 路由 (默认 alphafeed, 失败自动回退), df 已标准化
        df, _src = kline_source.fetch_kline_df("minute", symbol, period, count)
        if df is None or len(df) == 0:
            return _error(f"无法获取 {symbol} 的分钟线", 404)
        last_day = df.index.normalize().max()
        mask = df.index.normalize() == last_day
        df = df[mask]
        if len(df) == 0:
            return _error("无分时数据", 404)
        df, indicators = compute_all_indicators(df, period="1d", with_atr_val=True)
        bars = []
        for i, (idx, row) in enumerate(df.iterrows()):
            ts = idx.strftime("%H:%M") if hasattr(idx, "strftime") else str(idx)[-8:-3]
            bars.append({
                "time": ts,
                "open": _safe_float(row.get("open")),
                "high": _safe_float(row.get("high")),
                "low": _safe_float(row.get("low")),
                "close": _safe_float(row.get("close")),
                "volume": _safe_int(row.get("volume")),
                "macd_dif": indicators["macd"]["dif"][i],
                "macd_dea": indicators["macd"]["dea"][i],
                "macd_hist": indicators["macd"]["hist"][i],
                "kdj_k": indicators["kdj"]["k"][i] if "kdj" in indicators else None,
                "kdj_d": indicators["kdj"]["d"][i] if "kdj" in indicators else None,
                "kdj_j": indicators["kdj"]["j"][i] if "kdj" in indicators else None,
                "rsi6": indicators["rsi"]["rsi6"][i] if "rsi" in indicators else None,
                "rsi12": indicators["rsi"]["rsi12"][i] if "rsi" in indicators else None,
                "rsi24": indicators["rsi"]["rsi24"][i] if "rsi" in indicators else None,
                "atr14": indicators["atr"]["values"][i] if "atr" in indicators else None,
            })
        return _json({"symbol": symbol, "period": period, "bars": bars})
    except Exception as e:
        return _error(f"获取分钟线失败: {_sanitize_error(e)}", 500)


# ── 搜索历史 ──

@api_bp.route("/api/me/search-history", methods=["GET"])
def search_history_get():
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    return _json({"history": trades.get_search_history(user["id"])})


@api_bp.route("/api/me/search-history", methods=["PUT"])
def search_history_put():
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    body = _read_json_body()
    if body is None:
        return _error("请求体无效 JSON", 400)
    history = trades.set_search_history(
        user["id"], body.get("history") or [], allow_clear=False,
    )
    return _json({"ok": True, "history": history})


# ── 管理员用户 ──

@api_bp.route("/api/admin/users", methods=["GET"])
def admin_users_list():
    admin = _require_admin()
    if admin is None:
        return _error("未登录", 401)
    if admin is False:
        return _error("无权限", 403)
    return _json(trades.list_users())


@api_bp.route("/api/admin/users", methods=["POST"])
def admin_users_create():
    admin = _require_admin()
    if admin is None:
        return _error("未登录", 401)
    if admin is False:
        return _error("无权限", 403)
    body = _read_json_body()
    if body is None:
        return _error("请求体无效 JSON", 400)
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if not username or not password:
        return _error("用户名和密码不能为空")
    try:
        user_id = trades.create_user(username, password, is_admin=False)
    except ValueError as e:
        return _error(str(e), 409)
    return _json({"ok": True, "id": user_id, "username": username})


@api_bp.route("/api/admin/users/<int:user_id>", methods=["DELETE"])
def admin_users_delete(user_id):
    admin = _require_admin()
    if admin is None:
        return _error("未登录", 401)
    if admin is False:
        return _error("无权限", 403)
    try:
        deleted = trades.delete_user(user_id)
    except ValueError as e:
        return _error(str(e), 400)
    if not deleted:
        return _error("用户不存在", 404)
    return _json({"ok": True})


@api_bp.route("/api/admin/users/<int:user_id>/reset-password", methods=["POST"])
def admin_users_reset(user_id):
    admin = _require_admin()
    if admin is None:
        return _error("未登录", 401)
    if admin is False:
        return _error("无权限", 403)
    body = _read_json_body()
    if body is None:
        return _error("请求体无效 JSON", 400)
    password = body.get("password") or ""
    if not password:
        return _error("密码不能为空")
    try:
        updated = trades.reset_password(user_id, password)
    except ValueError as e:
        return _error(str(e), 400)
    if not updated:
        return _error("用户不存在", 404)
    return _json({"ok": True})


@api_bp.route("/api/admin/users/<int:user_id>/monitor", methods=["POST"])
def admin_users_monitor(user_id):
    admin = _require_admin()
    if admin is None:
        return _error("未登录", 401)
    if admin is False:
        return _error("无权限", 403)
    body = _read_json_body()
    if body is None:
        return _error("请求体无效 JSON", 400)
    enabled = body.get("enabled")
    if enabled not in (True, False, 0, 1, "0", "1", "true", "false"):
        return _error("enabled 必须为布尔值")
    if isinstance(enabled, str):
        enabled = enabled.lower() in ("1", "true")
    else:
        enabled = bool(enabled)
    if not trades.set_user_monitor(user_id, enabled):
        return _error("用户不存在", 404)
    return _json({"ok": True, "id": user_id, "monitor_enabled": enabled})


# ── 监控状态 ──

@api_bp.route("/api/monitor/status", methods=["GET"])
def monitor_status():
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    try:
        import monitor as _mon
        st = _mon.get_status()
    except Exception:
        st = {"running": False, "backend": None, "last_poll": None, "n_symbols": 0}
    alerts = trades.list_monitor_alerts(user["id"], limit=20)
    st["alerts"] = alerts
    st["monitor_enabled"] = bool(user.get("is_admin") or user.get("monitor_enabled"))
    if st.get("last_error"):
        from logger import redact_message
        st["last_error"] = redact_message(st["last_error"])
    return _json(st)


# ── 模型 ──

@api_bp.route("/api/models", methods=["GET"])
def models_list():
    if not _require_user():
        return _error("未登录", 401)
    return _json(trades.list_models(active_only=False))


@api_bp.route("/api/models", methods=["POST"])
def models_create():
    admin = _require_admin()
    if admin is None:
        return _error("未登录", 401)
    if admin is False:
        return _error("无权限", 403)
    body = _read_json_body()
    if body is None:
        return _error("请求体无效 JSON", 400)
    try:
        mid = trades.create_model(
            body.get("name"), body.get("description", ""),
            body.get("hold_days"),
        )
    except ValueError as e:
        return _error(str(e), 409)
    return _json({"ok": True, "id": mid})


@api_bp.route("/api/models/<int:mid>", methods=["PUT"])
def models_update(mid):
    admin = _require_admin()
    if admin is None:
        return _error("未登录", 401)
    if admin is False:
        return _error("无权限", 403)
    body = _read_json_body()
    if body is None:
        return _error("请求体无效 JSON", 400)
    try:
        hold_days = body["hold_days"] if "hold_days" in body else trades._UNSET
        updated = trades.update_model(
            mid, body.get("name"), body.get("description", ""), hold_days,
        )
    except ValueError as e:
        return _error(str(e), 409)
    if not updated:
        return _error("模型不存在", 404)
    return _json({"ok": True})


@api_bp.route("/api/models/<int:mid>", methods=["DELETE"])
def models_delete(mid):
    admin = _require_admin()
    if admin is None:
        return _error("未登录", 401)
    if admin is False:
        return _error("无权限", 403)
    if not trades.delete_model(mid):
        return _error("模型不存在", 404)
    return _json({"ok": True})


@api_bp.route("/api/models/<int:mid>/restore", methods=["POST"])
def models_restore(mid):
    admin = _require_admin()
    if admin is None:
        return _error("未登录", 401)
    if admin is False:
        return _error("无权限", 403)
    try:
        restored = trades.restore_model(mid)
    except ValueError as e:
        return _error(str(e), 409)
    if not restored:
        return _error("模型不存在", 404)
    return _json({"ok": True})


# ── 交易记录 ──

@api_bp.route("/api/trades", methods=["GET"])
def trades_list():
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    filters = {
        "status": request.args.get("status"),
        "symbol": request.args.get("symbol"),
        "q": request.args.get("q"),
        "from": request.args.get("from"),
        "to": request.args.get("to"),
        "model_id": request.args.get("model_id"),
        "limit": request.args.get("limit"),
        "offset": request.args.get("offset"),
    }
    deduct = (request.args.get("deduct_fees") or "").lower() in ("1", "true")
    fee_config = trades.get_user_fees(user["id"]) if deduct else None
    records, total = trades.list_trades(user["id"], filters, fee_config=fee_config)
    return _json({"trades": records, "total": total})


@api_bp.route("/api/trades", methods=["POST"])
def trades_create():
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    body = _read_json_body()
    if body is None:
        return _error("请求体无效 JSON", 400)
    try:
        trade = trades.create_trade(user["id"], body)
    except ValueError as e:
        return _error(str(e), 400)
    return _json({"trade": trade}, 201)


@api_bp.route("/api/trades/<int:tid>", methods=["PUT"])
def trades_update(tid):
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    body = _read_json_body()
    if body is None:
        return _error("请求体无效 JSON", 400)
    try:
        trade = trades.update_trade(user["id"], tid, body)
    except ValueError as e:
        return _error(str(e), 400)
    if trade is None:
        return _error("记录不存在", 404)
    return _json({"trade": trade})


@api_bp.route("/api/trades/<int:tid>", methods=["DELETE"])
def trades_delete(tid):
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    if not trades.delete_trade(user["id"], tid):
        return _error("记录不存在", 404)
    return _json({"ok": True})


@api_bp.route("/api/trades/stats", methods=["GET"])
def trades_stats():
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    start = request.args.get("from")
    end = request.args.get("to")
    deduct = (request.args.get("deduct_fees") or "").lower() in ("1", "true")
    fee_config = trades.get_user_fees(user["id"]) if deduct else None
    return _json(trades.compute_stats(
        user["id"], start, end, deduct_fees=deduct, fee_config=fee_config
    ))


# ── 费率 ──

@api_bp.route("/api/fees", methods=["GET"])
def fees_get():
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    return _json({"fees": trades.get_user_fees(user["id"])})


@api_bp.route("/api/fees", methods=["PUT"])
def fees_put():
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    body = _read_json_body()
    if body is None:
        return _error("请求体必须是 JSON 对象", 400)
    try:
        fees = trades.update_user_fees(user["id"], body)
    except ValueError as e:
        return _error(f"费率配置无效: {_sanitize_error(e)}", 400)
    return _json({"fees": fees})
