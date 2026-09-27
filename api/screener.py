"""Flask 路由: 条件选股 screener (/api/screener/*)。

任务化 + 队列 (第一批) + 因子库扫描 (第二批):

- **扫因子库, 不拉K线**: 判定读每日 18:00 构建的全市场因子快照 (visual/factors.py),
  7800 只逐行判定 → 秒级; 条件定义在 screener_metrics 注册表 (前后端共用);
- **AND / OR**: `mode="or"` 时命中任一条件即入选, 结果带 `hit_count` 与逐条明细
  (「满足 N 项」分组/筛选由页面完成);
- **同日同条件不重扫**: `cache_key = sha1(mode|条件|数据版本|价格口径)`; 命中已有
  完成任务时直接为请求者落一条 `from_cache` 的完成记录 (结果相同), 不扫描不取数;
- 队列/隔离/取消/通知语义同第一批 (详见 docs/screener.md)。

接口:
  GET  /api/screener/metrics      指标目录 (按分组, 供页面渲染下拉)
  POST /api/screener/run          提交任务 (本人已有排队/运行中则 409)
  GET  /api/screener/status       本人当前任务 + 最近 3 次历史
  GET  /api/screener/runs/<id>    单条任务详情 (本人或管理员)
  POST /api/screener/stop         取消本人任务 (管理员可停任意)
  GET  /api/admin/screener/runs   管理员: 全部排队/运行 + 最近完成
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time

from flask import request

import factors
import market_hours
import screener_metrics as metrics
import trades
from api import api_bp
from api.common import _error, _json, _read_json_body, _require_admin, _require_user
from logger import sanitize_error as _sanitize_error

log = logging.getLogger("api")

MODES = ("and", "or")
PRICE_MODES = ("close", "live")
HISTORY_LIMIT = 3          # 页面显示最近 3 次 (trades.SCREENER_RUN_KEEP 保留更多)
MILESTONE_MIN_SEC = 30.0   # 任务跑得比这还快就不发阶段通知
RESULT_MAX = int(os.environ.get("SCREENER_RESULT_MAX", "3000"))   # 单次落库结果上限
PUSH_MAX_PER_HOUR = max(0, int(os.environ.get("SCREENER_PUSH_MAX_PER_HOUR", "10")))
# 扫描推送预算: 改一个阈值就能换 cache_key 绕过去重, 没有预算就能刷爆共享群
_push_log: dict = {}
_push_lock = threading.Lock()

# 取数: 扫描只读因子库快照 (factors), 拉K/限速都在那边 (90% 额度, 见 factors.kline_rate)
_job_lock = threading.Lock()
_current = {"id": None, "stop": False}   # 正在执行的任务 id 与取消标志
# 已请求取消、但 worker 尚未把 id 写进 _current 的任务 (认领与写内存态之间有窗口,
# 不记账的话这期间的取消会丢); _execute 开头会认领并立即置 stop。
_pending_stop: set = set()
_wake = threading.Event()
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()


# ── 参数 ──
def _max_symbols():
    """SCREENER_MAX_SYMBOLS: 0/未设 = 全市场 (因子库模式下不再需要限制)。"""
    try:
        return max(0, int(os.environ.get("SCREENER_MAX_SYMBOLS", "0")))
    except (TypeError, ValueError):
        return 0


def _notify_pcts():
    """SCREENER_NOTIFY_PCTS=25,50,75 → 阶段通知的百分比节点 (默认只 50)。"""
    out = set()
    for part in os.environ.get("SCREENER_NOTIFY_PCTS", "50").split(","):
        try:
            v = int(part.strip())
        except (TypeError, ValueError):
            continue
        if 0 < v < 100:
            out.add(v)
    return sorted(out)


def _today(now=None):
    return (now or market_hours.now()).strftime("%Y-%m-%d")


def _cond_text(conditions, mode="and"):
    """条件 → 一行可读文案 (通知/详情用); 连接词随模式变 (OR 不能写成"且")。"""
    parts = []
    for c in conditions or []:
        label = c.get("label") or c.get("metric")
        if c.get("kind") in ("bool",):
            parts.append(label)
        elif c.get("kind") == "range":
            parts.append(f"{label} {c.get('value')}~{c.get('value2')}")
        elif c.get("kind") == "text":
            parts.append(f"{label}={_safe_text(c.get('value'))}")
        else:
            unit = (metrics.get(c.get("metric")) or {}).get("unit") or ""
            parts.append(f"{label} {c.get('op')} {c.get('value')}{unit}")
    return (" 或 " if mode == "or" else " 且 ").join(parts) if parts else "-"


def _safe_text(value, limit=24):
    """推送文案里的自由文本: 压成单行 + 截断 (markdown 控制字符不进群消息)。"""
    text = " ".join(str(value or "").split())[:limit]
    return text.replace("[", "(").replace("]", ")").replace("`", "'")


def _cache_key(mode, conditions, data_version, price_mode, cap=0):
    """同条件 + 同数据版本 + 同价格口径 + 同标的范围 + 同口径指纹 → 同一份结果。

    cap 与 metrics.VERSION 一并入键: 改了 SCREENER_MAX_SYMBOLS 或指标口径后,
    同一天也不会把旧范围/旧口径的结果当成本次结果复用。
    """
    payload = {
        "mode": mode, "data_version": data_version, "price_mode": price_mode,
        "cap": int(cap or 0), "metrics": metrics.VERSION,
        "conditions": [{"metric": c.get("metric"), "op": c.get("op"),
                        "value": c.get("value"), "value2": c.get("value2")}
                       for c in conditions or []],
    }
    return hashlib.sha1(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _push_allowed(user_id):
    """扫描推送预算 (每小时 PUSH_MAX_PER_HOUR 条/用户); 超了只落站内告警不推送。"""
    if PUSH_MAX_PER_HOUR <= 0:
        return False
    now = time.time()
    with _push_lock:
        times = [t for t in _push_log.get(user_id, []) if now - t < 3600]
        if len(times) >= PUSH_MAX_PER_HOUR:
            _push_log[user_id] = times
            return False
        times.append(now)
        _push_log[user_id] = times
        return True


def _notify(title, text, *, key=None, kind="event", user_id=None):
    """统一出口: 受推送预算约束, 超限只记日志 (站内告警不受影响)。"""
    if user_id is not None and not _push_allowed(user_id):
        log.info("选股推送超出预算, 仅落站内: %s", title)
        return False
    import notify
    return notify.notify(title, text, key=key, kind=kind)


# ── 通知 (先落库, 再入队) ──
def _alert(user_id, alert_type, detail, day):
    try:
        trades.insert_monitor_alert(user_id, None, "", alert_type, day, price=None, detail=detail)
    except Exception as e:
        log.warning("选股通知落库失败: %s", _sanitize_error(e))


def _notify_start(run, total, data_version):
    cond = _cond_text(run.get("conditions"), run.get("mode"))
    detail = f"开始扫描: {cond} (数据 {data_version})"
    _alert(run["user_id"], "screener_start", detail, _today())
    _notify("选股开始",
            f"## 选股开始\n\n**{run.get('username') or ''}** {cond}\n\n"
            f"因子库 {data_version} · {total} 只, 完成后会再通知。",
            user_id=run["user_id"])


def _notify_progress(run, pct, progress, total, n_hits):
    cond = _cond_text(run.get("conditions"), run.get("mode"))
    _alert(run["user_id"], "screener_progress",
           f"进度 {pct}%: {progress}/{total}, 暂命中 {n_hits} 只", _today())
    _notify(f"选股进度 {pct}%",
            f"## 选股进度 {pct}%\n\n**{run.get('username') or ''}** {cond}\n\n"
            f"{progress}/{total} 只, 暂命中 {n_hits} 只",
            key=f"screener:{run['id']}", kind="progress", user_id=run["user_id"])


def _notify_done(run, results, stopped, total, n_cond, mode, truncated=False, from_cache=False):
    n = len(results or [])
    cond = _cond_text(run.get("conditions"), mode)
    head = "已取消" if stopped else "完成"
    odd = "满足任一" if mode == "or" else "全部满足"
    tail = ""
    if truncated:
        tail = f"，已按上限保留前 {RESULT_MAX} 只"
    detail = f"{head}: 命中 {n} 只 ({odd} {n_cond} 项条件){tail}"
    _alert(run["user_id"], "screener_done", detail, _today())
    lines = [f"## 选股{head}", "",
             f"**{run.get('username') or ''}** {cond}", "",
             f"{odd} · 命中 **{n}** 只 (扫描 {total} 只)"
             + ("，已取消，保留部分结果" if stopped else "")
             + (f"，已达单次上限 {RESULT_MAX} 只 (截断)" if truncated else "")]
    if from_cache:
        lines.append("")
        lines.append("（同日同条件已扫过, 本次直接复用其结果, 未重复扫描）")
    if results:
        lines.append("")
        for r in results[:5]:
            chg = r.get("change_pct")
            chg_txt = f" {chg:+.2f}%" if isinstance(chg, (int, float)) else ""
            hit = f" [{r.get('hit_count')}/{n_cond}]" if mode == "or" else ""
            lines.append(f"- {r.get('symbol')} {r.get('name') or ''}{chg_txt}{hit}")
        if n > 5:
            lines.append(f"- … 其余 {n - 5} 只见选股页")
    _notify(f"选股{head}: 命中 {n} 只", "\n".join(lines), user_id=run["user_id"])


# ── 扫描执行 ──
def _stopped(run_id):
    with _job_lock:
        return _current["id"] == run_id and _current["stop"]


def _result_row(rec, conditions, hits):
    """快照一行 + 判定明细 → 结果行 (含每条件数值, 供页面明细列)。"""
    detail = []
    for c, (ok, value) in zip(conditions, hits):
        spec = metrics.get(c["metric"]) or {}
        if isinstance(value, bool) or value is None:
            shown = value                     # 布尔/无数据原样
        elif isinstance(value, (int, float)):
            shown = round(float(value), 4)
        else:
            shown = value                     # 文本类 (行业) 不能转 float
        detail.append({
            "metric": c["metric"], "label": c.get("label") or spec.get("label"),
            "ok": bool(ok), "unit": spec.get("unit") or "", "value": shown,
        })
    return {
        "symbol": rec.get("symbol"), "name": rec.get("name"),
        "close": rec.get("close"), "change_pct": rec.get("change_pct"),
        "chip_profit": rec.get("chip_profit"),
        "hit_count": sum(1 for ok, _v in hits if ok),
        "hits": detail,
    }


def _scan(df, conditions, mode, total, run_id):
    """全市场逐行判定 → (结果列表, 已处理行数, 是否被取消, 是否被上限截断)。"""
    milestones = _notify_pcts()
    hit_milestones = set()
    started = time.time()
    results = []
    done = 0
    truncated = False
    n_rows = len(df)
    for rec in df.to_dict("records"):
        done += 1
        if done % 200 == 0 or done == n_rows:
            if _stopped(run_id):
                return results, done, True, truncated
            trades.update_screener_run(run_id, progress=done)
            pct = int(done / max(1, n_rows) * 100)
            if milestones and time.time() - started >= MILESTONE_MIN_SEC:
                for m in milestones:
                    if pct >= m and m not in hit_milestones:
                        hit_milestones.add(m)
                        try:
                            _notify_progress(_run_ctx[run_id], m, done, n_rows, len(results))
                        except Exception as e:
                            log.warning("选股进度通知失败: %s", _sanitize_error(e))
        passed, hits, _count = metrics.evaluate_all(rec, conditions, mode)
        if not passed:
            continue
        results.append(_result_row(rec, conditions, hits))
        if len(results) >= RESULT_MAX:
            truncated = True     # 显式标记: 页面/推送要能区分"共 N 只"与"截断到 N 只"
            break
    return results, done, _stopped(run_id), truncated


_run_ctx = {}   # run_id -> run dict (阶段通知要用户名/条件, 避免每轮重读库)


def _execute(run):
    """执行一条已认领 (running) 的任务; 所有出口都把终态写回库。

    数据版本与 cache_key 都在这里定 (而不是提交时): 盘中口径的实际版本要等
    live 快照拿到才知道, 回退收盘时也要把 price_mode 一起改回 close —— 否则会把
    "收盘结果"记成"盘中", 并让当天后续 live 请求全部命中这条缓存。
    """
    run_id = run["id"]
    conditions = run["conditions"]
    mode = run.get("mode") or "and"
    price_mode = run.get("price_mode") or "close"
    _run_ctx[run_id] = run
    with _job_lock:
        _current.update({"id": run_id, "stop": run_id in _pending_stop})
        _pending_stop.discard(run_id)
    try:
        df, data_version = factors.snapshot()
        eff_mode = price_mode
        if price_mode == "live":
            # 取快照要几分钟: 先写 total, 页面不再显示「扫描中 0/?」
            trades.update_screener_run(
                run_id, total=len(df), progress=0,
                data_version=data_version, price_mode="live")
            try:
                live_df, live_ver = factors.live_snapshot(
                    progress_cb=lambda done, total: trades.update_screener_run(
                        run_id, progress=done, total=total))
            except Exception as e:
                log.warning("盘中快照失败, 回退收盘口径 run=%s: %s",
                            run_id, _sanitize_error(e))
                live_df, live_ver = None, None
            if live_df is not None:
                df, data_version = live_df, live_ver
            else:
                log.info("盘中快照不可用, 回退收盘口径 run=%s", run_id)
                eff_mode = "close"
        if df is None or len(df) == 0:
            raise RuntimeError("因子库尚未构建 (交易日 18:00 自动更新)")

        cap = _max_symbols()
        if cap and len(df) > cap:
            df = df.head(cap)
        total = len(df)
        key = _cache_key(mode, conditions, data_version, eff_mode, cap)

        # 去重: 同数据版本 + 同条件 + 同口径已有结果 → 直接复用, 不扫不取数。
        # (提交时已按收盘口径查过一次; live 口径的实际版本此刻才知道, 所以这里再查)
        if key != run.get("cache_key") or eff_mode != price_mode:
            cached = trades.find_screener_run_by_cache(key, exclude_id=run_id)
            if cached and cached.get("results") is not None:
                done_at = trades._now_iso()
                trades.update_screener_run(
                    run_id, status="done", results=cached["results"],
                    data_version=data_version, price_mode=eff_mode, cache_key=key,
                    truncated=int(cached.get("truncated") or 0),
                    progress=cached.get("progress") or cached.get("n_results")
                    or len(cached["results"]),
                    total=cached.get("total") or total, done_at=done_at)
                try:
                    _notify_done(dict(run), cached["results"], False, total,
                                 len(conditions), mode,
                                 truncated=bool(cached.get("truncated")), from_cache=True)
                except Exception as e:
                    log.warning("选股复用通知失败: %s", _sanitize_error(e))
                return

        trades.update_screener_run(run_id, total=total, progress=0, cache_key=key,
                                   data_version=data_version, price_mode=eff_mode)
        _notify_start(dict(run, total=total, mode=mode), total, data_version)

        results, done, stopped, truncated = _scan(df, conditions, mode, total, run_id)
        trades.update_screener_run(
            run_id, status="stopped" if stopped else "done", results=results,
            done_at=trades._now_iso(), progress=done, truncated=int(truncated),
            data_version=data_version)
        try:
            _notify_done(dict(run), results, stopped, total, len(conditions), mode,
                         truncated=truncated)
        except Exception as e:
            log.warning("选股完成通知失败: %s", _sanitize_error(e))
    except Exception as e:
        detail = _sanitize_error(e)
        log.warning("选股扫描失败 run=%s: %s", run_id, detail)
        trades.update_screener_run(run_id, status="error", error=detail[:120],
                                   done_at=trades._now_iso())
        try:
            _alert(run["user_id"], "screener_error", f"扫描失败: {detail[:80]}", _today())
        except Exception:
            pass
    finally:
        with _job_lock:
            _current.update({"id": None, "stop": False})
            _pending_stop.discard(run_id)
        _run_ctx.pop(run_id, None)
        try:
            trades.prune_screener_runs(run["user_id"])
        except Exception:
            pass


def _process_next():
    """取队首任务执行 (无任务立即返回)。"""
    run = trades.active_screener_run()
    if not run:
        return False
    if run["status"] == "running":
        # 单进程单 worker: 启动时遗留的 running 不可能真在跑, 收尾掉免得堵住队列
        trades.update_screener_run(run["id"], status="error", error="服务重启中断",
                                   done_at=trades._now_iso())
        return True
    if not trades.claim_screener_run(run["id"]):
        return True
    _execute(trades.get_screener_run(run["id"]) or run)
    return True


def _worker_loop():
    while True:
        try:
            while _process_next():
                pass
        except Exception as e:
            log.warning("选股 worker 异常: %s", _sanitize_error(e))
            time.sleep(1.0)
        _wake.wait(5.0)
        _wake.clear()


def start_worker():
    """启动选股 worker 线程 (幂等); app.start_background_jobs 调用。"""
    global _worker
    with _worker_lock:
        if _worker is not None and _worker.is_alive():
            return _worker
        _worker = threading.Thread(target=_worker_loop, name="screener-worker", daemon=True)
        _worker.start()
        return _worker


def wake():
    _wake.set()


# ── 路由 ──
def _run_summary(r):
    """任务摘要 (不含 results; 命中数取 n_results 列, 轮询不再解析结果 JSON)。"""
    return {
        "id": r["id"], "status": r["status"], "mode": r["mode"],
        "conditions": r["conditions"], "progress": r["progress"], "total": r["total"],
        "queued_at": r["queued_at"], "started_at": r["started_at"], "done_at": r["done_at"],
        "stopped": r["status"] == "stopped", "error": r["error"],
        "n_results": r.get("n_results"),
        "truncated": bool(r.get("truncated")),
        "data_version": r.get("data_version"), "price_mode": r.get("price_mode"),
    }


def _active_payload(r):
    p = _run_summary(r)
    p["position"] = trades.screener_queue_position(r["id"]) if r["status"] == "queued" else 0
    return p


@api_bp.route("/api/screener/metrics", methods=["GET"])
def screener_metrics_catalog():
    """指标目录 (登录可见): 按分组返回, 文本类带 options (行业取自当前因子库)。"""
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    df, day = factors.snapshot()
    options = {}
    if df is not None:
        text_fields = [m["field"] for m in metrics.METRICS if m["kind"] == "text"]
        options = metrics.text_field_options(df, text_fields)
    return _json({"groups": metrics.catalog(options), "data_version": day,
                  "metrics": [{"key": m["key"], "label": m["label"], "group": m["group"],
                               "kind": m["kind"], "unit": m["unit"], "hint": m["hint"]}
                              for m in metrics.METRICS]})


@api_bp.route("/api/screener/run", methods=["POST"])
def screener_run():
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    body = _read_json_body()
    if body is None:
        return _error("请求体无效 JSON", 400)
    mode = (body.get("mode") or "and").lower()
    if mode not in MODES:
        return _error("扫描模式无效")
    price_mode = (body.get("price_mode") or "close").lower()
    if price_mode not in PRICE_MODES:
        return _error("价格口径无效")

    df, data_version = factors.snapshot()
    if df is None:
        return _error("因子库尚未构建, 交易日 18:00 自动更新后即可扫描", 503)
    options = metrics.text_field_options(
        df, [m["field"] for m in metrics.METRICS if m["kind"] == "text"])
    clean, err = metrics.clean(body.get("conditions"), options)
    if err:
        return _error(err)
    if trades.active_screener_run(user_id=user["id"]):
        return _error("你已有任务在排队或运行中", 409)

    cap = _max_symbols()
    # 收盘口径可以在提交时就去重 (版本已知); 盘中口径的实际版本要等 live 快照,
    # 交给 worker 在 _execute 里按真实版本去重 (见那里的注释)。
    key = _cache_key(mode, clean, data_version, price_mode, cap) \
        if price_mode == "close" else None
    if key:
        cached = trades.find_screener_run_by_cache(key)
        if cached and cached.get("results") is not None:
            run_id = trades.create_screener_run(
                user["id"], clean, mode=mode, price_mode=price_mode, status="done",
                results=cached["results"], data_version=data_version, cache_key=key,
                truncated=int(cached.get("truncated") or 0),
                progress=cached.get("progress") or len(cached["results"]),
                total=cached.get("total") or len(df))
            trades.prune_screener_runs(user["id"])
            return _json({"ok": True, "run_id": run_id, "position": 0,
                          "from_cache": True, "n_results": len(cached["results"]),
                          "truncated": bool(cached.get("truncated"))})

    run_id = trades.create_screener_run(user["id"], clean, mode=mode,
                                        price_mode=price_mode, cache_key=key)
    wake()
    return _json({"ok": True, "run_id": run_id,
                  "position": trades.screener_queue_position(run_id)})


@api_bp.route("/api/screener/status", methods=["GET"])
def screener_status():
    """本人当前任务 + 最近 3 次历史。

    默认**不下发 results**, 也不在服务端解析它 (首页徽章 10~60s 轮询一次):
    摘要查询只读 n_results 列。需要详情时用 `?full=1` 或 GET /api/screener/runs/<id>。
    """
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    full = (request.args.get("full") or "").lower() in ("1", "true", "yes")
    active = trades.active_screener_run(user_id=user["id"])
    recent = trades.list_screener_runs(user["id"], limit=HISTORY_LIMIT)
    current_id = active["id"] if active else (recent[0]["id"] if recent else None)
    current = None
    if current_id is not None:
        # 默认用摘要 (active/recent 已含全部展示字段); 只有 ?full=1 才读结果 JSON
        row = next((r for r in ([active] if active else []) + recent
                    if r["id"] == current_id), None)
        if full:
            row = trades.get_screener_run(current_id)
        if row:
            current = _run_summary(row)
            current["conditions"] = row.get("conditions") or []
            current["results"] = (row.get("results") or []) if full else None
    return _json({
        "active": _active_payload(active) if active else None,
        "recent": [_run_summary(r) for r in recent],
        "current": current,
        "factor_day": factors.current_version(),
    })


@api_bp.route("/api/screener/runs/<int:run_id>", methods=["GET"])
def screener_run_detail(run_id):
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    row = trades.get_screener_run(run_id)
    if not row:
        return _error("任务不存在", 404)
    if row["user_id"] != user["id"] and not user.get("is_admin"):
        return _error("无权限", 403)
    payload = _run_summary(row)
    payload["results"] = row.get("results") or []
    payload["conditions"] = row.get("conditions") or []
    payload["username"] = row.get("username")
    return _json(payload)


@api_bp.route("/api/screener/stop", methods=["POST"])
def screener_stop():
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    body = _read_json_body() or {}
    raw_id = body.get("run_id")
    target = None
    if raw_id not in (None, ""):
        # 只接受正整数 id: 非数字/浮点/列表此前会抛 ValueError/TypeError → 500
        if isinstance(raw_id, bool) or not isinstance(raw_id, (int, str)):
            return _error("run_id 无效", 400)
        text = str(raw_id).strip()
        if not text.isdigit():
            return _error("run_id 无效", 400)
        target = trades.get_screener_run(int(text))
        if target is None:
            return _json({"ok": False, "message": "任务不存在"})
    if target is None:
        target = trades.active_screener_run(user_id=user["id"])
    if target is None:
        return _json({"ok": False, "message": "没有进行中的扫描"})
    if target["user_id"] != user["id"] and not user.get("is_admin"):
        return _error("无权限", 403)
    status = trades.stop_screener_run(target["id"])
    if status is None:
        return _json({"ok": False, "message": "任务不存在"})
    if status not in ("cancelled", "stopping"):
        return _json({"ok": False, "status": status, "message": "任务已结束"})
    if status == "stopping":
        with _job_lock:
            if _current["id"] == target["id"]:
                _current["stop"] = True
            else:
                _pending_stop.add(target["id"])
    return _json({"ok": True, "status": status})


@api_bp.route("/api/admin/screener/runs", methods=["GET"])
def admin_screener_runs():
    admin = _require_admin()
    if admin is None:
        return _error("未登录", 401)
    if admin is False:
        return _error("无权限", 403)
    limit = request.args.get("limit")
    try:
        limit = max(1, min(int(limit), 100)) if limit else 20
    except (TypeError, ValueError):
        limit = 20
    queue = trades.list_screener_queue(limit=limit)
    recent = trades.list_recent_screener_runs(limit=limit)
    return _json({
        "queue": [_active_payload(r) | {"username": r.get("username")} for r in queue],
        "recent": [_run_summary(r) | {"username": r.get("username")} for r in recent],
        "factor_day": factors.current_version(),
    })
