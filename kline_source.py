"""K线数据源注册与路由: 按类别 (分钟/股票/指数/基金) 配置数据源回退链。

market.fetch_kline_ex 是唯一取数入口; 每个数据源实现 KlineSource 接口, 产出与
market._normalize 一致的标准 DataFrame (DatetimeIndex + open/high/low/close/
volume/amount, 升序)。图表默认前复权 (forward); 成交校验等可显式传 none。

回退链用 .env 配置 (逗号分隔, 依次尝试, 未配置用默认链):
    KLINE_SOURCE_MINUTE=alphafeed,akshare
    KLINE_SOURCE_INTRADAY=alphafeed_intraday
    KLINE_SOURCE_STOCK=mairui,alphafeed,akshare
    KLINE_SOURCE_INDEX=mairui,akshare
    KLINE_SOURCE_FUND=alphafeed,akshare

day-only 的 "intraday" 类别 (分时图) 与 "minute" 类别分开: 后者是跨天分钟K历史,
前者只回当日 (日内走势接口优先, 权限不可用当日退回分钟K批量)。

默认链 = 券商/付费源优先, akshare 只兜底 (分时是例外: 默认只走 alphafeed_intraday,
不兜 akshare —— 见 DEFAULT_CHAINS 里的说明)。基金默认不含麦蕊 (jj/lskx 无复权)。
主源失败自动切换下一源并记日志。分钟数据带新鲜度守卫: 末根 bar 距今超过
MINUTE_STALE_DAYS 天视为该源失败 (防止滞后窗口的旧数据被当成功渲染)。
"""
from __future__ import annotations

import abc
import hashlib
import logging
import os
import threading
import time
from datetime import timedelta

import market_hours
import perf
import source_alert

log = logging.getLogger("kline_source")

# 分钟末根 bar 距今超过该天数视为数据源滞后 (判失败, 继续回退)。
# 覆盖长假 + 短期停牌; 麦蕊 fsjy 冻结窗口 (数月) 会被拦下。
MINUTE_STALE_DAYS = 30

# 单源取数硬超时 (秒): akshare 内部 requests 调用**无超时**, 上游挂起会把用户
# 请求一起拖住 (waitress 只有 8 个线程), 表现为"同一只票忽快忽慢"。超时即判该源
# 失败并下沉到下一源。
SOURCE_TIMEOUT_SEC = max(1.0, float(os.environ.get("KLINE_SOURCE_TIMEOUT_SEC", "8")))
# 源级熔断: 连续失败达阈值后, 冷却期内直接跳过该源 (不再重吃同一份超时)。
SOURCE_FAIL_THRESHOLD = max(1, int(os.environ.get("KLINE_SOURCE_FAIL_THRESHOLD", "3")))
SOURCE_COOLDOWN_SEC = max(1.0, float(os.environ.get("KLINE_SOURCE_COOLDOWN_SEC", "60")))
SOURCE_MAX_INFLIGHT = max(1, int(os.environ.get("KLINE_SOURCE_MAX_INFLIGHT", "2")))
SOURCE_MAX_TOTAL = max(1, int(os.environ.get("KLINE_SOURCE_MAX_TOTAL", "6")))

_health_lock = threading.Lock()
_health = {}   # name -> {"fails": 连续失败数, "until": 冷却截止 ts}
_active = {}   # 实际存活的工作数; join 超时不能释放配额


class SourceBusy(Exception):
    """本地容量不足，不计作上游故障。"""


class SourceSkip(Exception):
    """本源明确不服务这个标的 (能力缺口)，不计作上游故障。

    由 `KlineSource.fetch` 抛出, 用于区分两种"没拿到数据":
      * **能力缺口** —— 上游正常答了, 只是这个市场/标的没有这个功能或没有数据
        (典型: 本套餐的港/美股日内分时 403; 或某代码当日一根 bar 都没有)。
        这不是故障, 记进源健康计数只会让**同一源**在别的市场/标的上一起被冷却。
      * **上游故障** —— 异常/超时, 该记失败 (返回 None)。

    与 SourceBusy 同一路子: 释放 probe 标记、`perf.bump("src_skip_<name>")`, 不记失败。
    单源链 (如分时的 alphafeed_intraday) 尤其依赖这条 —— 冷却整个源等于全市场 404。
    """


def _admit(name):
    with _health_lock:
        ent = _health.setdefault(name, {"fails": 0, "until": 0.0,
                                        "probe": False, "generation": 0})
        if ent["until"] > time.monotonic() or ent["probe"]:
            return None
        if ent["until"]:
            ent["probe"] = True
        return ent, ent["generation"]


def _current(name, token):
    ent = _health.get(name)
    return ent is not None and (token is None or
        (ent is token[0] and ent["generation"] == token[1]))


def reset_health():
    """清空源级健康状态 (测试隔离用)。

    连数据源告警的当日闸门一起清: "每源每天一条"是进程级状态, 用例之间不清会串味
    (前一个用例发过通知, 后一个断言"该发"就永远不成立)。
    """
    with _health_lock:
        _health.clear()
    source_alert.reset()


def _in_cooldown(name):
    with _health_lock:
        ent = _health.get(name)
    return bool(ent and (ent["until"] > time.monotonic() or ent["probe"]))


def _note_ok(name, token=None):
    with _health_lock:
        if _current(name, token):
            _health.pop(name, None)


def _note_fail(name, token=None, detail=""):
    """连续失败计一次; 达阈值则进入冷却窗口。

    detail 是"最后失败的那一次"的上下文 (标的/周期/类别), 只用于熔断通知文案 ——
    进冷却时推一条 (每源每天最多一条), 便于不翻日志就知道是哪个市场/周期在坏。
    """
    now = time.monotonic()
    entered = False
    with _health_lock:
        if token is not None and not _current(name, token):
            return
        ent = _health.setdefault(name, {"fails": 0, "until": 0.0,
                                        "probe": False, "generation": 0})
        ent["fails"] += 1
        if ent["probe"] or ent["fails"] >= SOURCE_FAIL_THRESHOLD:
            ent["until"] = now + SOURCE_COOLDOWN_SEC
            ent["fails"] = 0
            ent["probe"] = False
            ent["generation"] += 1
            entered = True
    if entered:
        perf.bump(f"src_cooldown_{name}")
        log.warning("数据源 %s 连续失败 %d 次, 冷却 %.0fs 内跳过",
                    name, SOURCE_FAIL_THRESHOLD, SOURCE_COOLDOWN_SEC)
        # 通知放在锁外 (推送含入队 + 日志, 别占着健康锁); 失败也只记日志
        source_alert.notify(name, f"连续失败 {SOURCE_FAIL_THRESHOLD} 次",
                            detail=detail, cooldown_sec=SOURCE_COOLDOWN_SEC)


def _fetch_bounded(src, symbol, period, count, adj, name):
    """在独立线程里跑单源取数并加硬超时, 返回 df; 源内异常原样抛出。

    非阻塞获取每源/全局配额，无排队。超时只结束调用者等待，实际工作退出才
    释放配额，故永久挂起也不会无界创建线程。
    """
    box = {}
    with _health_lock:
        if (_active.get(name, 0) >= SOURCE_MAX_INFLIGHT
                or sum(_active.values()) >= SOURCE_MAX_TOTAL):
            raise SourceBusy(name)
        _active[name] = _active.get(name, 0) + 1

    def _release():
        with _health_lock:
            _active[name] -= 1
            if not _active[name]:
                del _active[name]

    def _work():
        try:
            box["df"] = src.fetch(symbol, period, count, adj)
        except Exception as e:  # 交给调用方按原语义记 warning 并下沉
            box["err"] = e
        finally:
            _release()

    t = threading.Thread(target=_work, name=f"klinesrc-{name}", daemon=True)
    try:
        t.start()
    except Exception:
        _release()
        raise
    t.join(SOURCE_TIMEOUT_SEC)
    if t.is_alive():
        perf.bump(f"src_timeout_{name}")
        log.warning("数据源 %s 获取 %s %s 超时 (>%.1fs), 判失败回退",
                    name, symbol, period, SOURCE_TIMEOUT_SEC)
        return None
    if "err" in box:
        raise box["err"]
    return box.get("df")


def _try_source(name, symbol, period, count, adj, category, idx, chain):
    """单源尝试: 有界取数 + 新鲜度守卫 + 健康计数。返回 df 或 None。"""
    src = SOURCES[name]
    token = _admit(name)
    if token is None:
        return None
    try:
        df = _fetch_bounded(src, symbol, period, count, adj, name)
    except SourceBusy:
        with _health_lock:
            if _current(name, token):
                token[0]["probe"] = False
        perf.bump(f"src_busy_{name}")
        return None
    except SourceSkip as e:
        # 本源的"能力缺口": 它自己说清了这个市场/标的它服务不了。与 SourceBusy 同理,
        # 不记失败 —— 否则单源链会被"别的市场没权限/别的标的没数据"打进冷却, 连带
        # 让有数据的市场一起 404。
        with _health_lock:
            if _current(name, token):
                token[0]["probe"] = False
        perf.bump(f"src_skip_{name}")
        log.info("数据源 %s 不服务 %s %s (%s), 直接下沉", name, symbol, period, e)
        return None
    except Exception as e:  # 单源异常不拖垮整条链
        log.warning("数据源 %s 获取 %s %s 异常: %s", name, symbol, period, e)
        df = None
    if df is not None and category in ("minute", "intraday") and not _minute_fresh(df):
        log.warning("数据源 %s 分钟K数据过旧 (末根 %s), 视为失败",
                    name, df.index[-1])
        perf.bump(f"src_stale_{name}")
        df = None
    if df is not None:
        _note_ok(name, token)
        if idx > 0:
            perf.bump("fallback")
            log.info("%s %s 已回退到数据源 %s", symbol, period, name)
        return df
    # 该源本次未取到数据 (异常/空/过旧/超时): 计健康计数, 用于确认慢请求是否
    # 集中在个别坏源上 (回退链会把其耗时叠加到用户请求上)
    _note_fail(name, token, detail=f"{symbol} {period} ({category})")
    perf.bump(f"src_fail_{name}")
    if idx < len(chain) - 1:
        log.warning("数据源 %s 获取 %s %s 失败, 回退 %s",
                    name, symbol, period, chain[idx + 1])
    return None

# 内部口径: forward=前复权 / hfq=后复权 / none=未复权
ADJUST_FORWARD = "forward"
ADJUST_HFQ = "hfq"
ADJUST_NONE = "none"


def normalize_adjust(adjust) -> str:
    """把调用方 adjust 规范为 forward|hfq|none。"""
    if adjust in (None, "", ADJUST_FORWARD, "qfq", "fr"):
        return ADJUST_FORWARD
    if adjust in (ADJUST_HFQ, "backward", "hy"):
        return ADJUST_HFQ
    if adjust in (ADJUST_NONE, "raw", "n", False):
        return ADJUST_NONE
    return ADJUST_FORWARD


def adjust_tag(adjust) -> str:
    """磁盘/内存缓存 key 后缀。"""
    adj = normalize_adjust(adjust)
    if adj == ADJUST_HFQ:
        return "hfq"
    return "qfq" if adj == ADJUST_FORWARD else "raw"


class KlineSource(abc.ABC):
    """K线数据源接口: supports 声明能力, fetch 返回标准 df 或 None。"""

    name = ""

    def supports(self, category: str, period: str, adjust: str = ADJUST_FORWARD) -> bool:
        """该源能否服务 (category, period, adjust) 组合, 不支持直接跳过不发起请求。"""
        raise NotImplementedError

    @abc.abstractmethod
    def fetch(self, symbol: str, period: str, count: int, adjust: str = ADJUST_FORWARD):
        """拉取 K 线, 返回标准 DataFrame 或 None (异常自行吞掉并记日志)。"""
        raise NotImplementedError


class MairuiSource(KlineSource):
    """麦蕊智数: 股票/指数日周月K (股票支持 fr/n); 基金 jj/lskx 与分钟 fsjy 仅未复权。"""

    name = "mairui"

    def supports(self, category, period, adjust=ADJUST_FORWARD):
        adj = normalize_adjust(adjust)
        if category == "fund":
            # jj/lskx 无复权参数, 仅未复权可用
            return adj == ADJUST_NONE and period in ("1d", "1w", "1M")
        if category == "minute":
            # fsjy 无复权参数, 仅未复权可用
            return adj == ADJUST_NONE and period in ("5m", "15m", "30m", "60m")
        if category in ("stock", "index"):
            # 麦蕊股票接口仅 fr(前复权)/n(不复权), 无后复权 → hfq 跳过下沉
            return period in ("1d", "1w", "1M") and adj != ADJUST_HFQ
        return False

    def fetch(self, symbol, period, count, adjust=ADJUST_FORWARD):
        import market
        adj = normalize_adjust(adjust)
        if period in market.MINUTE_PERIODS:
            return market._fetch_mr_minute_kline(symbol, period, count)
        if market._is_etf(symbol):
            return market._fetch_fund_kline(symbol, period, count)
        return market._fetch_mr_kline(symbol, period, count, adjust=adj)


class AlphaFeedSource(KlineSource):
    """AlphaFeed: 分钟K主源 + 股票/ETF 日/周/月K (forward/none)。

    周/月K原生支持, 作为股票/ETF 周月K主源, 不再依赖抖动的 akshare。
    """

    name = "alphafeed"

    def supports(self, category, period, adjust=ADJUST_FORWARD):
        import market
        if category == "minute":
            return period in market.MINUTE_PERIODS
        if category in ("hk", "us"):
            # 港/美股 v1: 仅日/周/月K (分时能力待探测)
            return period in ("1d", "1w", "1M")
        return category in ("stock", "fund") and period in ("1d", "1w", "1M")

    def fetch(self, symbol, period, count, adjust=ADJUST_FORWARD):
        import market
        adj = normalize_adjust(adjust)
        if period in market.MINUTE_PERIODS:
            return market._fetch_minute_kline(symbol, period, count, adjust=adj)
        return market._fetch_af_kline(symbol, period, count, adjust=adj)


class AlphaFeedIntradaySource(AlphaFeedSource):
    """分时 (日内走势) 源: /v1/klines/intraday 优先, 权限不可用则当日退回分钟K批量。

    与 "minute" 类别分开的原因: 分钟K视图 (1m/5m/15m/30m/60m) 是跨天历史
    (前端 1m 要 1200 根 ≈ 5 个交易日), 而日内走势只回当日 —— 两者不能共用一条链,
    分开后回退链、源健康计数、chain_tag 缓存 key 都天然隔离。
    真正"日内走势优先"的取舍与当日熔断在 market._fetch_intraday_kline 里。
    """

    name = "alphafeed_intraday"

    def supports(self, category, period, adjust=ADJUST_FORWARD):
        import market
        return category == "intraday" and period in market.MINUTE_PERIODS

    def fetch(self, symbol, period, count, adjust=ADJUST_FORWARD):
        import market
        return market._fetch_intraday_kline(symbol, period, count,
                                            adjust=normalize_adjust(adjust))


class AkshareSource(KlineSource):
    """akshare(东财) 免费兜底: 日/周/月K + 分钟K, 无需 key。

    列名映射与单位以 akshare/东财实测为准。
    基金日K东财为「手」, 与 AlphaFeed / 快照同口径 (基金默认链已不含麦蕊股)。
    东财限流期可能持续拒绝连接 -> 返回 None, 由回退链下沉。
    """

    name = "akshare"

    _AK_PERIOD = {"1d": "daily", "1w": "weekly", "1M": "monthly"}
    _CN_COLS = {
        "开盘": "open", "最高": "high", "最低": "low", "收盘": "close",
        "成交量": "volume", "成交额": "amount",
    }

    def supports(self, category, period, adjust=ADJUST_FORWARD):
        import market
        if category in ("minute", "intraday"):
            # intraday 类别自带"只回当日"语义, 本源的分钟接口返回多日, 由路由层按当日
            # 截断 (口径与原来一致), 所以能服务该类目 —— 但**不在默认分时链里**
            # (实测其分钟数据不稳), 只有显式配置 KLINE_SOURCE_INTRADAY=...,akshare 才用。
            return period in market.MINUTE_PERIODS
        return category in ("stock", "index", "fund", "hk", "us") and period in ("1d", "1w", "1M")

    def fetch(self, symbol, period, count, adjust=ADJUST_FORWARD):
        import akshare as ak
        import market
        adj = normalize_adjust(adjust)
        if period in market.MINUTE_PERIODS:
            return self._fetch_minute(ak, market, symbol, period, count, adj)
        return self._fetch_daily(ak, market, symbol, period, count, adj)

    @staticmethod
    def _ak_adjust(adjust):
        if adjust == ADJUST_FORWARD:
            return "qfq"
        if adjust == ADJUST_HFQ:
            return "hfq"
        return ""

    def _fetch_daily(self, ak, market, symbol, period, count, adjust):
        # 东财按起止日期取数: count 根日K ≈ 1.7 倍自然日 + 缓冲 (同 get_daily_bar)
        natural = int(count * 1.7) + 40
        start = (market_hours.now() - timedelta(days=natural)).strftime("%Y%m%d")
        code = symbol.split(".")[0]
        category = market._symbol_market(symbol)
        ak_adj = self._ak_adjust(adjust)
        try:
            if market._is_etf(symbol):
                df = ak.fund_etf_hist_em(symbol=code, period=self._AK_PERIOD[period],
                                         start_date=start, end_date="20991231", adjust=ak_adj)
            elif category in ("hk", "us"):
                code = symbol.split(".")[0]
                if category == "hk":
                    code = code.zfill(5)
                    df = ak.stock_hk_hist(symbol=code, period=self._AK_PERIOD[period],
                                          start_date=start, end_date="20991231", adjust=ak_adj)
                else:
                    df = ak.stock_us_hist(symbol=code, period=self._AK_PERIOD[period],
                                          start_date=start, end_date="20991231", adjust=ak_adj)
            elif market._is_index_symbol(symbol):
                # 指数接口无复权参数
                df = ak.index_zh_a_hist(symbol=code, period=self._AK_PERIOD[period],
                                        start_date=start, end_date="20991231")
            else:
                df = ak.stock_zh_a_hist(symbol=code, period=self._AK_PERIOD[period],
                                        start_date=start, end_date="20991231", adjust=ak_adj)
        except Exception as e:
            log.warning(f"akshare 获取 {symbol} {period} 失败: {market._sanitize_error(e)}")
            return None
        df = self._rename_cn(df, date_col="日期")
        if df is None:
            return None
        df = market._normalize(df)
        if df is not None and count:
            df = df.tail(count)
        return df

    def _fetch_minute(self, ak, market, symbol, period, count, adjust):
        code = symbol.split(".")[0]
        category = market._symbol_market(symbol)
        minutes = period[:-1]  # "5m" -> 东财接口的 "5"
        ak_adj = self._ak_adjust(adjust)
        # 东财分钟接口: 股票/ETF 支持 adjust, 指数接口无该参数;
        # 1m 走 trends2 仅近 5 个交易日, 5m+ 走 kline 接口全量 (服务端截 1488 根)
        try:
            if market._is_etf(symbol):
                df = ak.fund_etf_hist_min_em(symbol=code, period=minutes, adjust=ak_adj)
            elif category in ("hk", "us"):
                code = symbol.split(".")[0]
                if category == "hk":
                    df = ak.stock_hk_hist_min_em(symbol=code.zfill(5), period=minutes, adjust=ak_adj)
                else:
                    df = ak.stock_us_hist_min_em(symbol=code)
            elif market._is_index_symbol(symbol):
                df = ak.index_zh_a_hist_min_em(symbol=code, period=minutes)
            else:
                df = ak.stock_zh_a_hist_min_em(symbol=code, period=minutes, adjust=ak_adj)
        except Exception as e:
            log.warning(f"akshare 获取 {symbol} {period} 失败: {market._sanitize_error(e)}")
            return None
        df = self._rename_cn(df, date_col="时间", date_as="trade_time")
        if df is None:
            return None
        df = market._normalize(df, prefer_time=True)
        if df is not None and count:
            df = df.tail(count)
        return df

    @classmethod
    def _rename_cn(cls, df, date_col, date_as="trade_date"):
        if df is None or len(df) == 0:
            return None
        return df.rename(columns={date_col: date_as, **cls._CN_COLS})


SOURCES = {cls.name: cls() for cls in (MairuiSource, AlphaFeedSource,
                                       AlphaFeedIntradaySource, AkshareSource)}

CATEGORY_ENV = {
    "minute": "KLINE_SOURCE_MINUTE",
    "intraday": "KLINE_SOURCE_INTRADAY",
    "stock": "KLINE_SOURCE_STOCK",
    "index": "KLINE_SOURCE_INDEX",
    "fund": "KLINE_SOURCE_FUND",
    "hk": "KLINE_SOURCE_HK",
    "us": "KLINE_SOURCE_US",
}

# 券商/付费源优先, akshare 兜底。mairui 分钟实测数据窗口滞后,
# 故默认不入分钟链; 可显式配置
# KLINE_SOURCE_MINUTE=alphafeed,mairui,akshare 加入 (新鲜度守卫自动拦旧数据)。
# 基金默认 alphafeed,akshare: 麦蕊 jj/lskx 无前复权。
DEFAULT_CHAINS = {
    "minute": "alphafeed,akshare",
    # 分时: 只走日内走势源 —— 默认**不**兜 akshare (实测其分钟数据不稳: 东财限流期可能
    # 整段失败, 而且是静默换供应商, 图上数据的出处会变)。AF 分来源内部已自带
    # 「无权限/无当日数据 → 退回 klines.batch」(见 market._fetch_intraday_kline),
    # A股 恒有数据; 港/美股本套餐两个接口都 403, 会直接报无数据。要老行为就显式配
    # KLINE_SOURCE_INTRADAY=alphafeed_intraday,akshare。
    "intraday": "alphafeed_intraday",
    "stock": "mairui,alphafeed,akshare",
    "index": "mairui,akshare",
    "fund": "alphafeed,akshare",
    # 港/美股: AlphaFeed 优先，akshare 日/周/月及分钟兜底
    "hk": "alphafeed,akshare",
    "us": "alphafeed,akshare",
}

_warned_names = set()


def _chain(category):
    """解析类别数据源链: env 覆盖 > 默认; 未知源名告警跳过。"""
    raw = os.environ.get(CATEGORY_ENV[category], "").strip()
    if not raw:
        raw = DEFAULT_CHAINS[category]
    names = []
    for part in raw.split(","):
        name = part.strip().lower()
        if not name:
            continue
        if name not in SOURCES:
            if name not in _warned_names:
                _warned_names.add(name)
                log.warning("KLINE_SOURCE_%s: 未知数据源 %r 已跳过 (可用: %s)",
                            category.upper(), name, ", ".join(SOURCES))
            continue
        names.append(name)
    return names or DEFAULT_CHAINS[category].split(",")


def describe_chains():
    """启动横幅用: 各类别当前生效的数据源链。"""
    return {cat: ",".join(_chain(cat)) for cat in CATEGORY_ENV}


def chain_tag(category) -> str:
    """当前类别数据源链的短标识, 用作磁盘缓存 key 后缀: 改链即失效。

    依据「配置的链」(env 覆盖 > 默认) 而非实际服务源生成 —— 实际源要拉完才
    知道, 无法用于查缓存; 而改配置正是需要让旧源缓存失效的场景。同一链内主源
    故障回退到次源时仍复用缓存 (TTL 短, 可接受)。
    """
    raw = ",".join(_chain(category))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]


def _minute_fresh(df):
    """分钟新鲜度守卫: 末根 bar 距今超过 MINUTE_STALE_DAYS 天判为滞后。"""
    if df is None or len(df) == 0:
        return False
    try:
        age_days = (market_hours.now() - df.index[-1]).total_seconds() / 86400
        return age_days <= MINUTE_STALE_DAYS
    except Exception as e:
        log.warning("分钟新鲜度检查失败, 按滞后处理: %s", e)
        return False


def fetch_kline_df(category, symbol, period, count, adjust=ADJUST_FORWARD):
    """按类别数据源链依次尝试, 返回 (标准 df, 源名) 或 (None, None)。

    单源失败 (异常/空数据/分钟数据过旧) 记 warning 后继续下一源; 全部失败
    返回 (None, None), 对外表现与旧版单源一致 (调用方返回 404)。
    """
    adj = normalize_adjust(adjust)
    chain = _chain(category)
    for i, name in enumerate(chain):
        src = SOURCES[name]
        if not src.supports(category, period, adj):
            continue
        if _in_cooldown(name):
            # 该源刚连续失败过: 冷却期内直接跳过, 不再重吃同一份超时
            perf.bump(f"src_skip_cooldown_{name}")
            log.info("数据源 %s 冷却中, 跳过 %s %s", name, symbol, period)
            continue
        df = _try_source(name, symbol, period, count, adj, category, i, chain)
        if df is not None:
            return df, name
    perf.bump("chain_exhausted")
    return None, None
