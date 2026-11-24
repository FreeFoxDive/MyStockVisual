"""账户级量化分析: 净值曲线、回撤与交易质量。

两条净值口径:

- realized  平仓日结算。单笔在卖出日记已实现盈亏; 批次按每条卖出腿、
  用与 ``trades._batch_metrics`` 相同的移动加权均价结算。不拉行情。
- mtm       逐日盯市。持仓用未复权收盘价估值; 区间开始前已有的底仓
  以起始日前一交易日收盘价 (缺失则用当时成本) 作为期初基准, 不把
  区间前的浮盈亏算进本期。

批次交易无论当前是持仓还是已平仓, 都从第一笔腿按时间回放。起始日
之前的腿只推进底仓, 不计入本期收益。胜率、盈亏比等「一笔策略」指标
只统计区间内完全平仓的交易 (一批次算一笔), 与 ``compute_stats`` 一致。
"""
from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import date, datetime, timedelta

import market_hours
import trades

log = logging.getLogger("quant_analysis")

_HIST_EDGES = (-10, -5, 0, 2, 5, 10, 15, 20)
_HIST_LABELS = [
    "≤-10%", "-10~-5%", "-5~0%", "0~2%", "2~5%",
    "5~10%", "10~15%", "15~20%", ">20%",
]
_EARLIEST = date(1990, 1, 1)
_CAPITAL_MIN = 1000.0
_CAPITAL_MAX = 1_000_000_000.0
_COMPARE_KEYS = (
    "end_nav", "total_return_pct", "cagr", "total_pnl", "max_drawdown",
    "max_dd_pct", "sharpe", "sortino", "calmar", "win_rate", "profit_factor",
    "payoff_ratio", "closed_count", "open_count", "volatility",
)


class AnalysisError(Exception):
    """参数不合法。message 是可直接回给前端的固定文案。"""

    def __init__(self, message):
        super().__init__(message)
        self.message = message


def compute_quant_analysis(
    user_id,
    *,
    model_id="all",
    start=None,
    end=None,
    capital=100_000.0,
    mode="realized",
    deduct_fees=True,
    fee_config=None,
    compare=False,
    bar_fetcher=None,
    today=None,
):
    """计算当前账户在所选模型与区间上的量化分析。

    model_id: ``"all"`` / ``"none"`` / 模型 id。
    start/end: ``YYYY-MM-DD`` 或 ``date``; 空起始日取最早成交日, 空结束日取今天。
    capital: 期初资金, 默认 10 万。
    mode: ``realized`` (默认) 或 ``mtm``。
    bar_fetcher: ``(symbol, dates) -> {date: bar}``, 仅盯市使用; 缺省走行情模块。
    """
    capital = _parse_capital(capital)
    mode = _parse_mode(mode)
    model_key = _parse_model_id(model_id)
    start_d = _parse_day(start, "起始日期")
    end_d = _parse_day(end, "结束日期")
    today_d = _parse_day(today, "今天") if today else market_hours.now().date()
    if end_d and end_d > today_d:
        raise AnalysisError("结束日期不能晚于今天")
    if start_d and start_d < _EARLIEST:
        raise AnalysisError("起始日期过早")

    if deduct_fees and fee_config is None:
        fee_config = trades.get_user_fees(user_id)
    elif not deduct_fees:
        fee_config = None

    # 未注入 bar_fetcher 时盯市走日K批量; 单测注入的 fetcher 仍逐只调用。
    batch_closes = bar_fetcher is None
    fetcher = _memo_fetcher(bar_fetcher or _market_bars)
    rows, models = _load_trades(user_id, "all")
    start_d, end_d = _resolve_range(rows, model_key, start_d, end_d, today_d)

    result = _analyze(
        rows, models, model_key, start_d, end_d, capital, mode,
        bool(deduct_fees), fee_config, fetcher, detail=True,
        batch_closes=batch_closes,
    )
    if compare:
        result["model_comparison"] = _compare(
            rows, models, start_d, end_d, capital, mode,
            bool(deduct_fees), fee_config, fetcher, batch_closes,
        )
    else:
        result["model_comparison"] = None
    return result


def compute_model_position_stats(user_id, model_id, *, cache=None):
    """全历史已平仓统计，同量化报告的扣费、胜负口径；保留原精度供仓位计算。"""
    model_key = _parse_model_id(model_id)
    if model_key == "all" or (isinstance(model_key, int) and model_key > 2**63 - 1):
        raise AnalysisError("请选择有效的模型")
    today = market_hours.now().date().isoformat()
    cache_key = (user_id, model_key, today)
    if cache is not None:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
    where = (
        "t.user_id=? AND t.status='closed' AND t.exit_date > '' AND t.exit_date <= ? "
        "AND IFNULL(t.type, 'simple')!='reverse_repo'"
    )
    args = [user_id, today]
    if model_key == "none":
        where += " AND t.model_id IS NULL"
    else:
        where += " AND t.model_id=?"
        args.append(model_key)

    fee_config = trades.get_user_fees(user_id)
    conn = trades.get_conn()
    try:
        # 两次读取使用同一快照；只加载命中批次的成交腿，避免逐笔开连接。
        conn.execute("BEGIN")
        rows = conn.execute(f"SELECT t.* FROM trades t WHERE {where} ORDER BY t.exit_date, t.id", args).fetchall()
        legs = {}
        for leg in conn.execute(
            "SELECT l.* FROM trades t JOIN trade_legs l ON l.trade_id=t.id "
            f"WHERE {where} AND t.type='batch' ORDER BY l.trade_id, l.date, IFNULL(l.time, ''), l.id",
            args,
        ):
            legs.setdefault(leg["trade_id"], []).append(dict(leg))
    finally:
        conn.close()

    wins, losses = [], []
    for row in rows:
        if row["type"] == "batch":
            metrics = trades._batch_metrics(row["symbol"], legs.get(row["id"], []), fee_config)
        else:
            metrics = trades._trade_metrics(row, fee_config=fee_config)
        pnl = metrics.get("pnl") or 0.0
        if pnl > 0:
            wins.append(pnl)
        elif pnl < 0:
            losses.append(pnl)
    win_rate, payoff, *_ = _win_loss_stats(wins, losses)
    summary = {"closed_count": len(rows), "win_rate": win_rate,
               "payoff_ratio": payoff if payoff is not None and math.isfinite(payoff) else None}
    if cache is not None:
        cache.set(cache_key, summary)
    return summary


def _analyze(
    rows, models, model_key, start_d, end_d, capital, mode,
    deduct_fees, fee_config, fetcher, *, detail, batch_closes=False,
):
    selected = [r for r in rows if _match_model(r, model_key)]
    start_s, end_s = start_d.isoformat(), end_d.isoformat()
    preps = [_prepare(r, fee_config, deduct_fees, start_s, end_s) for r in selected]
    days = _axis(start_d, end_d, preps)
    warnings = []

    if mode == "mtm":
        equity, pos_pct, hold_count = _mtm_curve(
            preps, days, capital, fetcher, warnings, batch_closes=batch_closes,
        )
    else:
        equity, pos_pct, hold_count = _realized_curve(preps, days, capital)

    risk = _risk_block(days, equity, capital)
    curve_nav = risk.pop("_nav")
    curve_rets = risk.pop("_rets")
    curve_dd = risk.pop("_dd")
    peak_i = risk.pop("_peak_i")
    trough_i = risk.pop("_trough_i")
    fade_i0 = risk.pop("_fade_i0")
    fade_i1 = risk.pop("_fade_i1")
    rounds = [_round_trip(p, models, fee_config, deduct_fees) for p in preps if _closed_in(p, start_s, end_s)]
    rounds.sort(key=lambda t: (t["exit_date"], t["id"]))
    quality = _quality(rounds, preps, days, pos_pct, hold_count, capital, start_s, end_s)
    summary = {**risk, **quality}
    summary["total_fees"] = _window_fees(preps, start_s, end_s)
    summary["open_partial_pnl"] = round(sum(p.window_pnl for p in preps if p.raw["status"] == "open"), 2)

    notes = []
    if not days:
        notes.append("所选区间没有交易日。")
    if mode == "realized" and summary["open_count"]:
        notes.append("平仓结算不含未平仓浮盈亏，可切换逐日盯市。")
    if abs(summary["open_partial_pnl"]) >= 0.01:
        notes.append(
            "未平仓批次在区间内已实现减仓盈亏 "
            f"{summary['open_partial_pnl']:.2f} 元，已计入净值，未计入胜率。"
        )
    if 0 < len(days) < 10:
        notes.append("区间不足 10 个交易日，年化收益仅供参考。")
    peak_pos = summary.get("max_position_pct")
    if peak_pos is not None and peak_pos > 100:
        basis = "持仓市值相对当日权益" if mode == "mtm" else "持仓成本相对初始资金"
        notes.append(f"峰值仓位超过 100%（{basis}超配），统计按真实仓位计算，未截断。")

    name, active = _scope_name(model_key, models)
    payload = {
        "mode": mode,
        "capital": capital,
        "start": start_s,
        "end": end_s,
        "model_id": model_key if model_key != "all" else "all",
        "model_name": name,
        "model_active": active,
        "deduct_fees": deduct_fees,
        "warnings": warnings,
        "notes": notes,
        "summary": summary,
        "curve": {
            "dates": days,
            "nav": curve_nav,
            "equity": [round(x, 2) for x in equity],
            "daily_ret": [_r(x * 100, 4) for x in curve_rets],
            "drawdown": curve_dd,
            "pos_pct": pos_pct,
            "hold_count": hold_count,
            "max_dd_peak_i": peak_i,
            "max_dd_i": trough_i,
            "dd_start_i": fade_i0,
            "dd_end_i": fade_i1,
        },
    }
    if detail:
        payload["monthly"] = _monthly(days, equity, curve_nav, curve_dd, rounds, capital)
        payload["return_hist"] = _hist([t["return_pct"] for t in rounds if t["return_pct"] is not None])
        payload["by_exit_reason"] = _by_reason(rounds)
        payload["holding_scatter"] = _scatter(rounds, capital)
        payload["rolling"] = _rolling(rounds)
        payload["by_symbol"] = _by_symbol(rounds)
        payload["trades"] = _trade_rows(preps, models, fee_config, deduct_fees, start_s, end_s)
    return payload


def _compare(rows, models, start_d, end_d, capital, mode, deduct_fees, fee_config, fetcher, batch_closes):
    seen = []
    has_none = False
    for row in rows:
        mid = row.get("model_id")
        if mid is None:
            has_none = True
        elif mid not in seen:
            seen.append(mid)
    keys = (["none"] if has_none else []) + seen
    out = []
    for key in keys:
        part = _analyze(
            rows, models, key, start_d, end_d, capital, mode,
            deduct_fees, fee_config, fetcher, detail=False,
            batch_closes=batch_closes,
        )
        if part["summary"]["closed_count"] == 0 and part["summary"]["open_count"] == 0 and not part["summary"]["total_pnl"]:
            continue
        out.append({
            "model_id": None if key == "none" else key,
            "name": part["model_name"],
            "active": part["model_active"],
            "nav": part["curve"]["nav"],
            "summary": {k: part["summary"].get(k) for k in _COMPARE_KEYS},
        })
    return out


# ── 交易加载与逐腿回放 ──

class _Prep:
    def __init__(self, raw, fills, day_pnl, snaps, window_pnl):
        self.raw = raw
        self.fills = fills
        self.day_pnl = day_pnl
        self.snaps = snaps
        self.window_pnl = window_pnl


def _load_trades(user_id, model_key):
    conn = trades.get_conn()
    try:
        sql = "SELECT * FROM trades WHERE user_id=? AND IFNULL(type, 'simple')!='reverse_repo'"
        params = [user_id]
        if model_key == "none":
            sql += " AND model_id IS NULL"
        elif model_key != "all":
            sql += " AND model_id=?"
            params.append(model_key)
        rows = [dict(r) for r in conn.execute(sql, params)]
        for row in rows:
            if row.get("type") == "batch":
                legs = conn.execute(
                    "SELECT * FROM trade_legs WHERE trade_id=? ORDER BY date, IFNULL(time, ''), id",
                    (row["id"],),
                ).fetchall()
                row["legs"] = [dict(leg) for leg in legs]
            else:
                row["legs"] = None
        models = {
            r["id"]: {"name": r["name"], "active": bool(r["active"])}
            for r in conn.execute("SELECT id, name, active FROM models")
        }
    finally:
        conn.close()
    return rows, models


def _prepare(row, fee_config, deduct_fees, start_s, end_s):
    fills = _fills(row, fee_config, deduct_fees)
    day_pnl, snaps = _replay(fills)
    # 未平仓批次的官方 pnl 扣了全部买入费用 (含尚未卖出部分和之后才买的腿),
    # 对齐过去会把未来的费用记到更早的卖出日, 所以只对已平仓交易对齐。
    official = _official_pnl(row, fee_config, deduct_fees) if row.get("status") == "closed" else None
    day_pnl = _align_cents(day_pnl, official)
    window = round(sum(v for d, v in day_pnl.items() if start_s <= d <= end_s), 2)
    return _Prep(row, fills, day_pnl, snaps, window)


def _fills(row, fee_config, deduct_fees):
    fees = fee_config if deduct_fees else None
    symbol = row["symbol"]
    if row.get("type") == "batch":
        legs = sorted(
            row.get("legs") or [],
            key=lambda leg: (leg["date"], leg.get("time") or "", leg.get("id") or 0),
        )
        out = []
        for leg in legs:
            price = float(leg["price"])
            qty = float(leg["quantity"])
            fee = trades._calc_side_fees(symbol, leg["side"], price, qty, fees)["total"] if fees else 0.0
            out.append({
                "date": leg["date"], "side": leg["side"], "price": price, "qty": qty, "fee": fee,
            })
        return out

    qty = float(row["quantity"])
    entry = float(row["entry_price"])
    buy_fee = trades._calc_side_fees(symbol, "buy", entry, qty, fees)["total"] if fees else 0.0
    out = [{"date": row["entry_date"], "side": "buy", "price": entry, "qty": qty, "fee": buy_fee}]
    if row.get("status") == "closed" and row.get("exit_price") is not None and row.get("exit_date"):
        exit_px = float(row["exit_price"])
        sell_fee = trades._calc_side_fees(symbol, "sell", exit_px, qty, fees)["total"] if fees else 0.0
        out.append({"date": row["exit_date"], "side": "sell", "price": exit_px, "qty": qty, "fee": sell_fee})
    return out


def _replay(fills):
    """按日回放。卖出盈亏 = 价差 - 卖出费用 - 按剩余股数分摊的买入费用。

    清仓那一笔吃掉尚未分摊的买入费用尾差, 这样各卖出日之和等于整笔净盈亏。
    """
    held = 0.0
    cost_total = 0.0
    buy_fee = 0.0
    day_pnl = {}
    snaps = []
    by_day = defaultdict(list)
    for fill in fills:
        by_day[fill["date"]].append(fill)
    for day in by_day:
        pnl = 0.0
        sold = False
        for fill in by_day[day]:
            if fill["side"] == "buy":
                cost_total += fill["price"] * fill["qty"]
                held += fill["qty"]
                buy_fee += fill["fee"]
                continue
            qty = fill["qty"]
            if qty > held:
                qty = held
            if qty <= 1e-9 or held <= 1e-9:
                continue
            avg = cost_total / held
            gross = (fill["price"] - avg) * qty
            portion = qty / fill["qty"] if fill["qty"] else 1.0
            if held - qty <= 1e-6:
                alloc = buy_fee
                held = 0.0
                cost_total = 0.0
                buy_fee = 0.0
            else:
                alloc = buy_fee * (qty / held)
                buy_fee -= alloc
                held -= qty
                cost_total = held * avg
            pnl += gross - fill["fee"] * portion - alloc
            sold = True
        if sold:
            day_pnl[day] = pnl
        snaps.append((day, held, cost_total))
    return day_pnl, snaps


def _official_pnl(row, fee_config, deduct_fees):
    metrics = trades._trade_metrics(
        row, fee_config=fee_config if deduct_fees else None, deduct_fees=deduct_fees,
    )
    return metrics.get("pnl")


def _align_cents(day_pnl, official):
    """把逐日盈亏圆到分, 尾差补在最后一笔卖出日, 与整笔官方盈亏对齐。"""
    if not day_pnl:
        return {}
    keys = list(day_pnl)
    rounded = {key: round(day_pnl[key], 2) for key in keys}
    if official is None:
        return rounded
    gap = round(round(float(official), 2) - round(sum(rounded.values()), 2), 2)
    rounded[keys[-1]] = round(rounded[keys[-1]] + gap, 2)
    return rounded


def _state_on(snaps, day):
    held = 0.0
    cost = 0.0
    for snap_day, held_i, cost_i in snaps:
        if snap_day <= day:
            held, cost = held_i, cost_i
        else:
            break
    return held, cost


def _realized_curve(preps, days, capital):
    pnl_by_day = defaultdict(float)
    for prep in preps:
        for day, pnl in prep.day_pnl.items():
            pnl_by_day[day] += pnl
    equity = []
    pos_pct = []
    hold_count = []
    running = 0.0
    for day in days:
        running = round(running + pnl_by_day.get(day, 0.0), 2)
        eq = round(capital + running, 2)
        equity.append(eq)
        cost = 0.0
        symbols = set()
        for prep in preps:
            held, held_cost = _state_on(prep.snaps, day)
            if held > 1e-9:
                cost += held_cost
                symbols.add(prep.raw["symbol"])
        denom = max(eq, capital)
        pos_pct.append(round(cost / denom * 100, 2) if denom > 0 else 0.0)
        hold_count.append(len(symbols))
    return equity, pos_pct, hold_count


def _mtm_curve(preps, days, capital, fetcher, warnings, *, batch_closes=False):
    if not days:
        return [], [], []
    prev = _previous_trading_day(date.fromisoformat(days[0]))
    spans = defaultdict(set)
    for prep in preps:
        if not _held_during(prep, days[0], days[-1]):
            continue
        first = prep.fills[0]["date"]
        last = days[-1] if prep.raw.get("status") == "open" else prep.fills[-1]["date"]
        wanted = spans[prep.raw["symbol"]]
        if prev and first < days[0]:
            wanted.add(prev)
        wanted.update(day for day in days if first <= day <= last)
    closes = {}
    warned = set()
    books = None
    if batch_closes and spans:
        try:
            import market
            books = market.get_daily_bars_many({
                symbol: sorted(wanted) for symbol, wanted in spans.items()
            })
        except Exception as exc:
            log.warning("盯市批量日K失败, 改逐只: %s", type(exc).__name__)
            books = None
    for symbol, wanted in spans.items():
        dates = sorted(wanted)
        if books is not None:
            book = books.get(symbol) or {}
            source = lambda _symbol, asked, book=book: {day: book.get(day) for day in asked}
        else:
            source = fetcher
        closes[symbol] = _load_closes(symbol, dates, source, warnings, warned)

    paths = [_mtm_path(prep.fills, days, prev, closes.get(prep.raw["symbol"], {})) for prep in preps]
    equity = []
    pos_pct = []
    hold_count = []
    for i, _day in enumerate(days):
        realized = 0.0
        unreal = 0.0
        market_value = 0.0
        symbols = set()
        for prep, path in zip(preps, paths):
            part_realized, part_unreal, part_value, held = path[i]
            realized += part_realized
            unreal += part_unreal
            market_value += part_value
            if held > 1e-9:
                symbols.add(prep.raw["symbol"])
        eq = round(capital + realized + unreal, 2)
        equity.append(eq)
        pos_pct.append(round(market_value / eq * 100, 2) if eq > 1e-6 else 0.0)
        hold_count.append(len(symbols))
    return equity, pos_pct, hold_count


def _held_during(prep, start_s, end_s):
    if not prep.fills:
        return False
    first = prep.fills[0]["date"]
    if prep.raw.get("status") == "open":
        return first <= end_s
    last = prep.fills[-1]["date"]
    return first <= end_s and last >= start_s


def _mtm_path(fills, days, prev, closes):
    start = days[0]
    held, cost, buy_fee = _apply_fills([f for f in fills if f["date"] < start], 0.0, 0.0, 0.0)[:3]
    if held > 1e-9:
        baseline = closes.get(prev) if prev else None
        if not baseline or baseline <= 0:
            baseline = cost / held
        cost = baseline * held
        buy_fee = 0.0
    by_day = defaultdict(list)
    for fill in fills:
        if fill["date"] >= start:
            by_day[fill["date"]].append(fill)
    realized = 0.0
    last_close = closes.get(prev) if prev and closes.get(prev) else None
    out = []
    for day in days:
        held, cost, buy_fee, pnl = _apply_fills(by_day.get(day, []), held, cost, buy_fee)
        realized += pnl
        close = closes.get(day)
        if close is None or close <= 0:
            close = last_close if last_close else (cost / held if held > 1e-9 else None)
        else:
            last_close = close
        if held > 1e-9 and close:
            unreal = (close - cost / held) * held - buy_fee
            value = close * held
        else:
            unreal = 0.0
            value = 0.0
        out.append((realized, unreal, value, held))
    return out


def _apply_fills(fills, held, cost, buy_fee):
    pnl = 0.0
    for fill in fills:
        if fill["side"] == "buy":
            cost += fill["price"] * fill["qty"]
            held += fill["qty"]
            buy_fee += fill["fee"]
            continue
        qty = min(fill["qty"], held)
        if qty <= 1e-9 or held <= 1e-9:
            continue
        avg = cost / held
        gross = (fill["price"] - avg) * qty
        portion = qty / fill["qty"] if fill["qty"] else 1.0
        if held - qty <= 1e-6:
            alloc = buy_fee
            held = 0.0
            cost = 0.0
            buy_fee = 0.0
        else:
            alloc = buy_fee * (qty / held)
            buy_fee -= alloc
            held -= qty
            cost = held * avg
        pnl += gross - fill["fee"] * portion - alloc
    return held, cost, buy_fee, pnl


def _load_closes(symbol, dates, fetcher, warnings, warned):
    try:
        raw = fetcher(symbol, dates) or {}
    except Exception as exc:
        log.warning("量化分析日K失败 symbol=%s: %s", symbol, type(exc).__name__)
        raw = {}
        if symbol not in warned:
            warnings.append(f"{symbol} 日K获取失败，已用前值或成本价估值")
            warned.add(symbol)
    closes = {}
    missing = False
    for day in dates:
        bar = raw.get(day) if isinstance(raw, dict) else None
        price = None
        if isinstance(bar, dict):
            price = bar.get("close")
        if price is None or price == "":
            missing = True
            continue
        try:
            price = float(price)
        except (TypeError, ValueError):
            missing = True
            continue
        if price > 0:
            closes[day] = price
        else:
            missing = True
    if missing and symbol not in warned:
        warnings.append(f"{symbol} 部分交易日缺少收盘价，已用前值或成本价")
        warned.add(symbol)
    return closes


def _market_bars(symbol, dates):
    import market
    return market.get_daily_bars(symbol, dates)


def _memo_fetcher(fetcher):
    cache = {}

    def wrapped(symbol, dates):
        key = (symbol, tuple(dates))
        if key not in cache:
            cache[key] = fetcher(symbol, dates)
        return cache[key]

    return wrapped


# ── 风险指标 ──

def _risk_block(days, equity, capital):
    nav = [round(eq / capital, 6) if capital else 0.0 for eq in equity]
    rets = []
    prev = 1.0
    for value in nav:
        rets.append((value / prev - 1.0) if prev > 1e-12 else 0.0)
        prev = value
    drawdown, meta = _drawdown(days, nav)
    end_nav = nav[-1] if nav else 1.0
    end_equity = equity[-1] if equity else capital
    total_pnl = round(end_equity - capital, 2)
    total_return = (end_nav - 1.0) * 100 if nav else 0.0
    n_days = len(nav)
    cagr = None
    if n_days and end_nav > 0:
        cagr = end_nav ** (252 / n_days) - 1.0
    sharpe = _ratio(rets, downside=False)
    sortino = _ratio(rets, downside=True)
    vol = None
    if len(rets) >= 2:
        std = _std(rets, downside=False)
        if std is not None:
            vol = std * math.sqrt(252)
    max_dd_frac = -(meta["max_dd_pct"] or 0.0) / 100.0
    calmar = None
    if cagr is not None and meta["max_dd_pct"]:
        calmar = cagr / (meta["max_dd_pct"] / 100.0)
    current = drawdown[-1] / 100.0 if drawdown else 0.0
    return {
        "end_nav": _r(end_nav, 4) if nav else 1.0,
        "end_equity": round(end_equity, 2),
        "total_pnl": total_pnl,
        "total_return_pct": _r(total_return, 2),
        "cagr": _r(cagr, 4),
        "cagr_short_sample": n_days < 10,
        "sharpe": _r(sharpe, 2),
        "sortino": _r(sortino, 2),
        "calmar": _r(calmar, 2),
        "volatility": _r(vol, 4),
        "max_drawdown": _r(max_dd_frac, 4) if meta["max_dd_pct"] else 0.0,
        "max_dd_pct": meta["max_dd_pct"] or 0.0,
        "max_dd_peak": meta["max_dd_peak"],
        "max_dd_date": meta["max_dd_date"],
        "max_dd_days": meta["max_dd_days"],
        "dd_start": meta["dd_start"],
        "dd_end": meta["dd_end"],
        "current_drawdown": _r(current, 4),
        "_nav": nav,
        "_rets": rets,
        "_dd": drawdown,
        "_peak_i": meta["peak_i"],
        "_trough_i": meta["trough_i"],
        "_fade_i0": meta["fade_i0"],
        "_fade_i1": meta["fade_i1"],
    }


def _drawdown(days, nav):
    """与回测报告 equity_drawdown 同一口径: 高点从 1 起, 创新高当天衰落归零。"""
    peak = 1.0
    peak_i = None
    cur = 0
    cur_start = None
    best_n = 0
    best_start = None
    best_end = None
    max_dd = 0.0
    max_dd_i = None
    max_dd_peak_i = None
    series = []
    for i, eq in enumerate(nav):
        if eq + 1e-12 >= peak:
            cur = 0
            cur_start = None
            if eq > peak:
                peak = eq
            peak_i = i
            series.append(0.0)
        else:
            if cur == 0:
                cur_start = i
            cur += 1
            if cur > best_n:
                best_n = cur
                best_start = cur_start
                best_end = i
            depth = (peak - eq) / peak * 100 if peak > 0 else 0.0
            series.append(round(-depth, 4))
            if depth > max_dd + 1e-12:
                max_dd = depth
                max_dd_i = i
                max_dd_peak_i = peak_i

    def at(index):
        if index is None or index < 0 or index >= len(days):
            return None
        return days[index]

    return series, {
        "max_dd_pct": round(max_dd, 2) if max_dd_i is not None else None,
        "max_dd_peak": at(max_dd_peak_i),
        "max_dd_date": at(max_dd_i),
        "max_dd_days": best_n,
        "dd_start": at(best_start),
        "dd_end": at(best_end),
        "peak_i": max_dd_peak_i,
        "trough_i": max_dd_i,
        "fade_i0": best_start,
        "fade_i1": best_end,
    }


def _std(values, downside):
    data = [min(v, 0.0) if downside else v for v in values]
    if len(values) < 2:
        return None
    if downside:
        var = sum(v * v for v in data) / (len(values) - 1)
    else:
        mean = sum(values) / len(values)
        var = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    if var <= 0:
        return None
    return math.sqrt(var)


def _ratio(rets, downside):
    if len(rets) < 2:
        return None
    std = _std(rets, downside=downside)
    if std is None or std < 1e-12:
        return None
    mean = sum(rets) / len(rets)
    return mean / std * math.sqrt(252)


# ── 交易质量 ──

def _closed_in(prep, start_s, end_s):
    if prep.raw.get("status") != "closed":
        return False
    exit_day = prep.raw.get("exit_date")
    return bool(exit_day) and start_s <= exit_day <= end_s


def _round_trip(prep, models, fee_config, deduct_fees):
    row = prep.raw
    metrics = trades._trade_metrics(
        row, fee_config=fee_config if deduct_fees else None, deduct_fees=deduct_fees,
    )
    entry = row.get("entry_date")
    exit_day = row.get("exit_date")
    held_days = None
    if entry and exit_day:
        held_days = (date.fromisoformat(exit_day) - date.fromisoformat(entry)).days
    return {
        "id": row["id"],
        "symbol": row["symbol"],
        "name": row["name"],
        "model_id": row.get("model_id"),
        "model_name": _model_label(row.get("model_id"), models),
        "entry_date": entry,
        "exit_date": exit_day,
        "pnl": metrics.get("pnl") or 0.0,
        "return_pct": metrics.get("return_pct"),
        "cost": metrics.get("cost") or 0.0,
        "fees": metrics.get("fees") or 0.0,
        "holding_days": held_days,
        "exit_reason": _exit_reason(row),
    }


def _exit_reason(row):
    if row.get("type") == "batch":
        sells = [
            leg for leg in (row.get("legs") or [])
            if leg.get("side") == "sell" and (leg.get("reason") or "").strip()
        ]
        if sells:
            sells.sort(key=lambda leg: (leg["date"], leg.get("time") or "", leg.get("id") or 0))
            return sells[-1]["reason"].strip()
    reason = (row.get("exit_reason") or "").strip()
    return reason or "未填"


def _win_loss_stats(wins, losses):
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    avg_win = gross_win / len(wins) if wins else None
    avg_loss = -gross_loss / len(losses) if losses else None
    payoff = (avg_win / abs(avg_loss)) if avg_win is not None and avg_loss else None
    decided = len(wins) + len(losses)
    win_rate = len(wins) / decided * 100 if decided else None
    return win_rate, payoff, gross_win, gross_loss, avg_win, avg_loss


def _quality(rounds, preps, days, pos_pct, hold_count, capital, start_s, end_s):
    wins = [t for t in rounds if t["pnl"] > 0]
    losses = [t for t in rounds if t["pnl"] < 0]
    flats = [t for t in rounds if t["pnl"] == 0]
    win_rate, payoff, gross_win, gross_loss, avg_win, avg_loss = _win_loss_stats(
        [t["pnl"] for t in wins], [t["pnl"] for t in losses],
    )
    win_rets = [t["return_pct"] for t in wins if t["return_pct"] is not None]
    loss_rets = [t["return_pct"] for t in losses if t["return_pct"] is not None]
    payoff_pct = None
    if win_rets and loss_rets and sum(loss_rets) != 0:
        payoff_pct = (sum(win_rets) / len(win_rets)) / abs(sum(loss_rets) / len(loss_rets))
    decided = len(wins) + len(losses)
    rets = [t["return_pct"] for t in rounds if t["return_pct"] is not None]
    kelly = None
    if decided and payoff:
        chance = len(wins) / decided
        kelly = chance - (1 - chance) / payoff
    holding = [t["holding_days"] for t in rounds if t["holding_days"] is not None]
    hold_win = [t["holding_days"] for t in wins if t["holding_days"] is not None]
    hold_loss = [t["holding_days"] for t in losses if t["holding_days"] is not None]
    max_win = max(wins, key=lambda t: t["pnl"]) if wins else None
    max_loss = min(losses, key=lambda t: t["pnl"]) if losses else None
    open_count, open_cost = _open_book(preps, end_s)
    buy_notional = 0.0
    for prep in preps:
        for fill in prep.fills:
            if fill["side"] == "buy" and start_s <= fill["date"] <= end_s:
                buy_notional += fill["price"] * fill["qty"]
    return {
        "closed_count": len(rounds),
        "open_count": open_count,
        "open_cost": round(open_cost, 2),
        "win_count": len(wins),
        "loss_count": len(losses),
        "break_even_count": len(flats),
        "win_rate": _r(win_rate, 2),
        "profit_factor": _r(gross_win / gross_loss, 2) if gross_loss else None,
        "payoff_ratio": _r(payoff, 2),
        "payoff_ratio_pct": _r(payoff_pct, 2),
        "avg_win": _r(avg_win, 2),
        "avg_loss": _r(avg_loss, 2),
        "ev_pct": _r(sum(rets) / len(rets), 2) if rets else None,
        "ev_amount": _r(sum(t["pnl"] for t in rounds) / len(rounds), 2) if rounds else None,
        "kelly": _r(kelly, 4),
        "avg_holding_days": _r(sum(holding) / len(holding), 1) if holding else None,
        "avg_holding_days_win": _r(sum(hold_win) / len(hold_win), 1) if hold_win else None,
        "avg_holding_days_loss": _r(sum(hold_loss) / len(hold_loss), 1) if hold_loss else None,
        "max_win_streak": _streak(rounds, positive=True),
        "max_loss_streak": _streak(rounds, positive=False),
        "avg_position_pct": _r(sum(pos_pct) / len(pos_pct), 2) if pos_pct else None,
        "max_position_pct": _r(max(pos_pct), 2) if pos_pct else None,
        "max_hold_count": max(hold_count) if hold_count else 0,
        "turnover": _r(buy_notional / capital, 2) if capital else None,
        "closed_pnl": round(sum(t["pnl"] for t in rounds), 2),
        "max_win": _extreme(max_win),
        "max_loss": _extreme(max_loss),
        "t_stats": _t_aggregate(preps, start_s, end_s),
    }


def _open_book(preps, end_s):
    count = 0
    cost = 0.0
    for prep in preps:
        if prep.raw.get("status") != "open":
            continue
        held, held_cost = _state_on(prep.snaps, end_s)
        if held > 1e-9:
            count += 1
            cost += held_cost
    return count, cost


def _extreme(trade):
    if not trade:
        return None
    return {
        "symbol": trade["symbol"],
        "name": trade["name"],
        "pnl": round(trade["pnl"], 2),
        "return_pct": trade["return_pct"],
    }


def _streak(rounds, positive):
    best = 0
    cur = 0
    for trade in rounds:
        hit = trade["pnl"] > 0 if positive else trade["pnl"] < 0
        if hit:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def _t_aggregate(preps, start_s, end_s):
    count = positive = reverse = success = 0
    pnl = 0.0
    for prep in preps:
        if prep.raw.get("type") != "batch":
            continue
        legs = [
            leg for leg in (prep.raw.get("legs") or [])
            if start_s <= leg.get("date", "") <= end_s
        ]
        if not legs:
            continue
        stats = trades._t_stats(legs)
        count += stats["count"]
        positive += stats["positive"]
        reverse += stats["reverse"]
        success += stats["success"]
        pnl += stats["pnl"]
    return {
        "count": count,
        "positive": positive,
        "reverse": reverse,
        "success": success,
        "success_rate": round(success / count * 100, 2) if count else None,
        "pnl": round(pnl, 2),
    }


def _window_fees(preps, start_s, end_s):
    total = 0.0
    for prep in preps:
        for fill in prep.fills:
            if start_s <= fill["date"] <= end_s:
                total += fill["fee"]
    return round(total, 2)


def _monthly(days, equity, nav, drawdown, rounds, capital):
    if not days:
        return []
    groups = []
    for i, day in enumerate(days):
        if not groups or days[groups[-1][0]][:7] != day[:7]:
            groups.append([i])
        else:
            groups[-1].append(i)
    rows = []
    prev_eq = capital
    prev_nav = 1.0
    for group in groups:
        end_i = group[-1]
        month = days[end_i][:7]
        closed = [t for t in rounds if (t["exit_date"] or "").startswith(month)]
        wins = sum(1 for t in closed if t["pnl"] > 0)
        losses = sum(1 for t in closed if t["pnl"] < 0)
        nav_end = nav[end_i] if nav else 1.0
        ret = (nav_end / prev_nav - 1.0) * 100 if prev_nav else 0.0
        rows.append({
            "month": month,
            "days": len(group),
            "count": len(closed),
            "win_rate": round(wins / (wins + losses) * 100, 2) if wins + losses else None,
            "pnl": round(equity[end_i] - prev_eq, 2),
            "closed_pnl": round(sum(t["pnl"] for t in closed), 2),
            "return_pct": round(ret, 2),
            "end_nav": round(nav_end, 4),
            "end_drawdown": drawdown[end_i] if drawdown else 0.0,
            "end_equity": round(equity[end_i], 2),
        })
        prev_eq = equity[end_i]
        prev_nav = nav_end
    return rows


def _hist(pcts):
    counts = [0] * (len(_HIST_EDGES) + 1)
    for raw in pcts:
        placed = False
        for i, edge in enumerate(_HIST_EDGES):
            if float(raw) <= edge:
                counts[i] += 1
                placed = True
                break
        if not placed:
            counts[-1] += 1
    return {"bins": list(_HIST_LABELS), "counts": counts}


def _by_reason(rounds):
    groups = {}
    for trade in rounds:
        bucket = groups.setdefault(trade["exit_reason"], {"n": 0, "wins": 0, "losses": 0, "pnl": 0.0, "rets": []})
        bucket["n"] += 1
        bucket["pnl"] += trade["pnl"]
        if trade["pnl"] > 0:
            bucket["wins"] += 1
        elif trade["pnl"] < 0:
            bucket["losses"] += 1
        if trade["return_pct"] is not None:
            bucket["rets"].append(trade["return_pct"])
    rows = []
    for reason, bucket in groups.items():
        decided = bucket["wins"] + bucket["losses"]
        rows.append({
            "reason": reason,
            "count": bucket["n"],
            "win_rate": round(bucket["wins"] / decided * 100, 2) if decided else None,
            "avg_return_pct": _r(sum(bucket["rets"]) / len(bucket["rets"]), 2) if bucket["rets"] else None,
            "pnl": round(bucket["pnl"], 2),
        })
    rows.sort(key=lambda item: -item["pnl"])
    return rows


def _scatter(rounds, capital):
    points = []
    for trade in rounds:
        points.append({
            "symbol": trade["symbol"],
            "name": trade["name"],
            "days": trade["holding_days"],
            "return_pct": trade["return_pct"],
            "pnl": round(trade["pnl"], 2),
            "exit_reason": trade["exit_reason"],
            "cost_pct": round(trade["cost"] / capital * 100, 2) if capital else None,
        })
    return points


def _rolling(rounds, window=10):
    n = len(rounds)
    size = window if n >= window else n
    labels, win_rate, avg_return = [], [], []
    if size >= 2:
        for i in range(size - 1, n):
            chunk = rounds[i - size + 1:i + 1]
            wins = sum(1 for t in chunk if t["pnl"] > 0)
            losses = sum(1 for t in chunk if t["pnl"] < 0)
            rets = [t["return_pct"] for t in chunk if t["return_pct"] is not None]
            labels.append(chunk[-1]["exit_date"])
            win_rate.append(round(wins / (wins + losses) * 100, 2) if wins + losses else None)
            avg_return.append(round(sum(rets) / len(rets), 2) if rets else None)
    return {"window": size, "labels": labels, "win_rate": win_rate, "avg_return": avg_return}


def _by_symbol(rounds):
    groups = {}
    for trade in rounds:
        bucket = groups.setdefault(trade["symbol"], {
            "symbol": trade["symbol"], "name": trade["name"],
            "pnl": 0.0, "count": 0, "wins": 0, "losses": 0,
        })
        bucket["pnl"] += trade["pnl"]
        bucket["count"] += 1
        if trade["pnl"] > 0:
            bucket["wins"] += 1
        elif trade["pnl"] < 0:
            bucket["losses"] += 1
    rows = []
    for bucket in groups.values():
        decided = bucket["wins"] + bucket["losses"]
        rows.append({
            "symbol": bucket["symbol"],
            "name": bucket["name"],
            "pnl": round(bucket["pnl"], 2),
            "count": bucket["count"],
            "win_rate": round(bucket["wins"] / decided * 100, 2) if decided else None,
        })
    rows.sort(key=lambda item: -item["pnl"])
    return rows


def _trade_rows(preps, models, fee_config, deduct_fees, start_s, end_s):
    rows = []
    for prep in preps:
        if not _overlaps(prep, start_s, end_s):
            continue
        raw = prep.raw
        metrics = trades._trade_metrics(
            raw, fee_config=fee_config if deduct_fees else None, deduct_fees=deduct_fees,
        )
        entry = raw.get("entry_date")
        exit_day = raw.get("exit_date")
        held_days = None
        if entry and exit_day:
            held_days = (date.fromisoformat(exit_day) - date.fromisoformat(entry)).days
        legs = None
        if raw.get("type") == "batch":
            ordered = sorted(
                raw.get("legs") or [],
                key=lambda leg: (leg["date"], leg.get("time") or "", leg.get("id") or 0),
            )
            legs = []
            for leg in ordered:
                price = float(leg["price"])
                qty = float(leg["quantity"])
                fee = 0.0
                if deduct_fees and fee_config:
                    fee = trades._calc_side_fees(raw["symbol"], leg["side"], price, qty, fee_config)["total"]
                legs.append({
                    "date": leg["date"],
                    "time": leg.get("time") or "",
                    "side": leg["side"],
                    "price": price,
                    "quantity": qty,
                    "fee": round(fee, 2),
                    "reason": leg.get("reason") or "",
                    "note": leg.get("note") or "",
                })
        rows.append({
            "id": raw["id"],
            "symbol": raw["symbol"],
            "name": raw["name"],
            "type": raw.get("type") or "simple",
            "status": raw.get("status"),
            "model_id": raw.get("model_id"),
            "model_name": _model_label(raw.get("model_id"), models),
            "entry_date": entry,
            "exit_date": exit_day,
            "entry_price": raw.get("entry_price"),
            "exit_price": raw.get("exit_price"),
            "quantity": raw.get("quantity"),
            "pnl": metrics.get("pnl"),
            "return_pct": metrics.get("return_pct"),
            "window_pnl": prep.window_pnl,
            "fees": metrics.get("fees") or 0.0,
            "holding_days": held_days,
            "exit_reason": _exit_reason(raw) if raw.get("status") == "closed" else "",
            "legs": legs,
            "t_stats": metrics.get("t_stats"),
            "leg_count": len(legs) if legs else 0,
        })
    rows.sort(key=lambda item: (item["exit_date"] or item["entry_date"] or "", item["id"]), reverse=True)
    return rows


def _overlaps(prep, start_s, end_s):
    if not prep.fills:
        return False
    first = prep.fills[0]["date"]
    last = prep.fills[-1]["date"] if prep.raw.get("status") != "open" else end_s
    return first <= end_s and last >= start_s


# ── 日历与参数 ──

def _axis(start_d, end_d, preps):
    days = set(_trading_days(start_d, end_d))
    start_s, end_s = start_d.isoformat(), end_d.isoformat()
    for prep in preps:
        for fill in prep.fills:
            if start_s <= fill["date"] <= end_s:
                days.add(fill["date"])
        for day in prep.day_pnl:
            if start_s <= day <= end_s:
                days.add(day)
    return sorted(days)


def _trading_days(start_d, end_d):
    start_s, end_s = start_d.isoformat(), end_d.isoformat()
    try:
        cached = market_hours._xshg_sorted(start_d.year - 1, end_d.year + 1)
        return [day for day in cached if start_s <= day <= end_s]
    except Exception:
        out = []
        cur = start_d
        while cur <= end_d:
            if cur.weekday() < 5:
                out.append(cur.isoformat())
            cur += timedelta(days=1)
        return out


def _previous_trading_day(day):
    try:
        prev = market_hours.last_trading_day(day - timedelta(days=1))
        return prev.strftime("%Y-%m-%d") if prev else None
    except Exception:
        cur = day - timedelta(days=1)
        for _ in range(40):
            if cur.weekday() < 5:
                return cur.isoformat()
            cur -= timedelta(days=1)
        return None


def _resolve_range(rows, model_key, start_d, end_d, today_d):
    end_d = end_d or today_d
    if start_d is None:
        picked = []
        for row in rows:
            if not _match_model(row, model_key):
                continue
            if row.get("entry_date"):
                picked.append(row["entry_date"])
            for leg in row.get("legs") or []:
                if leg.get("date"):
                    picked.append(leg["date"])
        start_d = date.fromisoformat(min(picked)) if picked else end_d
    if start_d > end_d:
        raise AnalysisError("起始日期不能晚于结束日期")
    return start_d, end_d


def _match_model(row, model_key):
    if model_key == "all":
        return True
    if model_key == "none":
        return row.get("model_id") is None
    return row.get("model_id") == model_key


def _scope_name(model_key, models):
    if model_key == "all":
        return "全部", True
    if model_key == "none":
        return "无", True
    info = models.get(model_key)
    if not info:
        return "未知模型", False
    name = info["name"] if info["active"] else f"{info['name']}（已删除）"
    return name, info["active"]


def _model_label(model_id, models):
    if model_id is None:
        return "无"
    info = models.get(model_id)
    if not info:
        return "未知模型"
    return info["name"] if info["active"] else f"{info['name']}（已删除）"


def _parse_capital(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise AnalysisError("初始资金须在 1000 到 1000000000 之间")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise AnalysisError("初始资金须在 1000 到 1000000000 之间")
    if not math.isfinite(number) or number < _CAPITAL_MIN or number > _CAPITAL_MAX:
        raise AnalysisError("初始资金须在 1000 到 1000000000 之间")
    return float(number)


def _parse_mode(value):
    if value in (None, "", "realized"):
        return "realized"
    if value == "mtm":
        return "mtm"
    raise AnalysisError("净值口径无效")


def _parse_model_id(value):
    if value is None or value in ("", "all"):
        return "all"
    if value in ("none", "null"):
        return "none"
    if isinstance(value, bool):
        raise AnalysisError("模型参数无效")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise AnalysisError("模型参数无效")
    if number <= 0:
        raise AnalysisError("模型参数无效")
    return number


def _parse_day(value, label):
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value)[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise AnalysisError(f"{label}无效")


def _r(value, digits):
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, digits)
