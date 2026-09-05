"""实测麦蕊分钟K线接口: period 取值格式、返回字段、当前证书权限。

背景: market.py 的分钟K一直只走 AlphaFeed。麦蕊侧候选接口:
- stock_vip_history  /hsstock/vip/{code}/{period}/{div}   (SDK 注释: 企业版历史, 含 1m 等)
- stock_history      /hsstock/history/{code}/{period}/{div} (常规历史, 是否收分钟待验证)
- bj_history         /bj/history/{code}/{period}/{div}     (京市历史分时)
period 究竟是 "1m"/"5m" 还是 "1"/"5" 文档页是 JS 渲染抓不到, 只能实测。

运行:
    python -u visual/probe_mairui_minute.py
    python -u visual/probe_mairui_minute.py 600519.SH 833533.BJ
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

for env_dir in (SCRIPT_DIR, SCRIPT_DIR.parent):
    env_file = env_dir / ".env"
    if env_file.exists():
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
        break

import market  # noqa: E402  (模块级, 供 _err/_show 使用; .env 已先加载)

SYMBOLS = ["600519.SH", "833533.BJ"]
# 每次调用都耗当日额度, 候选矩阵保持最小
MINUTE_PERIOD_CANDIDATES = ["5m", "5", "1m"]


def _show(label, rows):
    """紧凑打印响应: dict 视为错误响应, list 打印长度+首尾各一条。"""
    if rows is None:
        print(f"  {label:<38} -> None")
        return
    if isinstance(rows, dict):
        print(f"  {label:<38} -> 错误响应: {json.dumps(rows, ensure_ascii=False)[:160]}")
        return
    if not isinstance(rows, list) or len(rows) == 0:
        print(f"  {label:<38} -> 空响应: {type(rows).__name__} {str(rows)[:80]}")
        return
    first, last = rows[0], rows[-1]
    print(f"  {label:<38} -> {len(rows)} 条, 首条: {json.dumps(first, ensure_ascii=False)[:160]}")
    print(f"  {'':<38}    末条: {json.dumps(last, ensure_ascii=False)[:160]}")


def _err(label, e):
    """打印异常的原始类型与状态码 (sanitize 会把鉴权/含api的URL错误掩成'服务暂不可用')。"""
    status = getattr(e, "status_code", None)
    print(f"  {label:<26} -> {type(e).__name__} status={status} msg={market._sanitize_error(e)}")


def _raw_get(url, label):
    """裸 HTTP: 只打状态码与响应体片段 (URL 含 licence 不打印)。"""
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            body = resp.read().decode("utf-8", "ignore")
            print(f"  {label:<44} -> HTTP {resp.status} body: {body[:200]}")
    except Exception as e:
        code = getattr(e, "code", None)
        print(f"  {label:<44} -> HTTP {code} {type(e).__name__}")


def raw_matrix():
    """区分 '端点不存在(404)' 与 '无权限(403)': 跨域名/周期裸测, 不经 SDK。"""
    lic = os.environ.get("MAIRUI_PAID_API_KEY") or os.environ.get("MAIRUI_FREE_API_KEY") or ""
    if not lic:
        print("未配置麦蕊证书, 跳过裸测矩阵")
        return
    print("[0] 裸测矩阵 (api.mairuiapi.com vs api.mairui.club)")
    for host in ("api.mairuiapi.com", "api.mairui.club"):
        for path in (f"hsstock/vip/600519.SH/5m/n/{lic}",
                     f"hsstock/history/600519.SH/5m/n/{lic}",
                     f"bj/history/833533.BJ/5m/n/{lic}"):
            _raw_get(f"https://{host}/{path}?lt=3", f"{host} {path.split('/')[0]}..{path.split('/')[2]}/{path.split('/')[3]}")
    print("[0b] 候选路径/周期补充试探 (404 不耗额度)")
    base = "https://api.mairuiapi.com"
    for suffix, label in (
        (f"hszbl/fsjy/600519.SH/5m/{lic}?lt=3", "hszbl/fsjy 5m"),
        (f"hszbl/fsjy/600519.SH/5/{lic}?lt=3", "hszbl/fsjy 5"),
        (f"hsstock/latest/600519.SH/5m/n/{lic}?lt=3", "hsstock/latest 5m"),
        (f"hsstock/history/600519.SH/m5/n/{lic}?lt=3", "hsstock/history m5"),
        (f"hsstock/history/full/600519.SH/5/n/{lic}?lt=3", "hsstock/history/full 5(Pro)"),
    ):
        _raw_get(f"{base}/{suffix}", label)

    print("[0c] hszbl/fsjy 参数探明 (lt 分页 / 1m 深度 / BJ / 指数)")
    import json as _json
    for suffix, label in (
        (f"hszbl/fsjy/600519.SH/5m/{lic}", "5m 无参"),
        (f"hszbl/fsjy/600519.SH/5m/{lic}?lt=10", "5m lt=10"),
        (f"hszbl/fsjy/600519.SH/1m/{lic}", "1m 无参"),
        (f"hszbl/fsjy/833533.BJ/5m/{lic}", "BJ 5m"),
        (f"hszbl/fsjy/000001.SH/5m/{lic}", "指数000001.SH 5m"),
    ):
        try:
            import urllib.request as _u
            req = _u.Request(f"{base}/{suffix}", headers={"User-Agent": "Mozilla/5.0"})
            with _u.urlopen(req, timeout=10) as resp:
                rows = _json.loads(resp.read().decode("utf-8", "ignore"))
            if isinstance(rows, list) and rows:
                ds = [r.get("d", "") for r in rows]
                print(f"  {label:<18} -> {len(rows)} 条, 首 {ds[0]} 末 {ds[-1]}, "
                      f"字段: {sorted(rows[0].keys())}")
            else:
                print(f"  {label:<18} -> 非列表/空: {str(rows)[:120]}")
        except Exception as e:
            print(f"  {label:<18} -> HTTP {getattr(e, 'code', '?')} {type(e).__name__}")

    print("[0d] fsjy st/et 与其他周期 (数据是否覆盖近期?)")
    for suffix, label in (
        (f"hszbl/fsjy/600519.SH/5m/{lic}?st=20260901&et=20260905", "5m st=0901 et=0905"),
        (f"hszbl/fsjy/600519.SH/5m/{lic}?st=20260801", "5m 仅st=0801"),
        (f"hszbl/fsjy/600519.SH/15m/{lic}", "15m 无参"),
        (f"hszbl/fsjy/600519.SH/60m/{lic}", "60m 无参"),
        (f"hszbl/fsjy/600519.SH/1m/{lic}?st=20260904", "1m st=0904"),
    ):
        try:
            import urllib.request as _u
            req = _u.Request(f"{base}/{suffix}", headers={"User-Agent": "Mozilla/5.0"})
            with _u.urlopen(req, timeout=10) as resp:
                rows = _json.loads(resp.read().decode("utf-8", "ignore"))
            if isinstance(rows, list) and rows:
                ds = [r.get("d", "") for r in rows]
                print(f"  {label:<20} -> {len(rows)} 条, 首 {ds[0]} 末 {ds[-1]}")
            else:
                print(f"  {label:<20} -> 非列表/空: {str(rows)[:120]}")
        except Exception as e:
            print(f"  {label:<20} -> HTTP {getattr(e, 'code', '?')} {type(e).__name__}")


def main():
    import market

    raw_matrix()
    api = market.get_mr()
    symbols = sys.argv[1:] or SYMBOLS
    stock = symbols[0]
    bj = next((s for s in symbols if s.endswith(".BJ")), None)

    print(f"证书: {'PAID' if os.environ.get('MAIRUI_PAID_API_KEY') else 'FREE/未配置'}")
    print(f"[1] stock_vip_history 分钟周期试探 ({stock})")
    for p in MINUTE_PERIOD_CANDIDATES:
        try:
            _show(f"vip period={p!r} lt=3", api.stock_vip_history(stock, p, "n", lt=3))
        except Exception as e:
            _err(f"vip period={p!r}", e)

    print(f"[2] stock_history 常规历史是否也收分钟 ({stock})")
    try:
        _show("history period='5m' lt=3", api.stock_history(stock, "5m", "n", lt=3))
    except Exception as e:
        _err("history period='5m'", e)

    print(f"[3] bj_history 分钟周期试探 ({bj or '(未提供 BJ 代码, 跳过)'})")
    if bj:
        for p in MINUTE_PERIOD_CANDIDATES[:2]:
            try:
                _show(f"bj period={p!r} lt=3", api.bj_history(bj, p, "n", lt=3))
            except Exception as e:
                _err(f"bj period={p!r}", e)

    print(f"[4] 日K对照 (确认字段/权限正常) ({stock})")
    try:
        _show("history period='d' lt=2", api.stock_history(stock, "d", "n", lt=2))
    except Exception as e:
        _err("history period='d'", e)

    print("\n判读: list=数据(看首条字段名确定列映射), dict=错误响应(看错误码),\n"
          "MairuiAuthError(HTTP 401/403) = 证书无该接口/该周期权限。\n"
          "2026-09-05 实测结论 (PAID 证书):\n"
          "- 分钟K可用接口 = /hszbl/fsjy/{code}/{级别}/{licence} (SDK 未封装,\n"
          "  裸 HTTP): 5m/15m/30m/60m 通, 沪深股票+指数(000001.SH)通; 1m=422,\n"
          "  北交所=404/502, st/et/lt 查询参数全部无效(恒返同一窗口)。\n"
          "- 数据窗口冻结在 2026-02-06 ~ 2026-04-30 (ud 字段为更新时间),\n"
          "  无近期数据 -> 直接回退会拿到 4 个月前旧数据。kline_source 路由\n"
          "  已加分钟新鲜度守卫(末根 bar 距今 >7 天视为失败继续回退), 且\n"
          "  默认分钟链不含 mairui, 需 KLINE_SOURCE_MINUTE 显式加入。\n"
          "- 5m 上限 1488 根(覆盖不足 1200 默认值的场景可用), 60m 仅 124 根;\n"
          "  v=手(600519 单根 1078 手, 与日K 45416 手同量级), e=成交额(元)。\n"
          "- /hsstock/vip=404(网关无此路径), /hsstock/history/full=403(需量化\n"
          "  Pro 增强包, 普通证书错误码 108), 与官方公告一致。日K正常。\n")


if __name__ == "__main__":
    main()
