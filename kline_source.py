"""K线数据源注册与路由: 按类别 (分钟/股票/指数/基金) 配置数据源回退链。

market.fetch_kline_ex 是唯一取数入口; 每个数据源实现 KlineSource 接口, 产出与
market._normalize 一致的标准 DataFrame (DatetimeIndex + open/high/low/close/
volume/amount, 升序, 未复权), 异常自吞返回 None。

回退链用 .env 配置 (逗号分隔, 依次尝试, 未配置用默认链):
    KLINE_SOURCE_MINUTE=alphafeed,akshare
    KLINE_SOURCE_STOCK=mairui,alphafeed,akshare
    KLINE_SOURCE_INDEX=mairui,akshare
    KLINE_SOURCE_FUND=mairui,alphafeed,akshare

默认链 = 券商/付费源优先 (与既有行为一致), akshare 只兜底。主源失败自动
切换下一源并记日志。分钟数据带新鲜度守卫: 末根 bar 距今超过
MINUTE_STALE_DAYS 天视为该源失败 (防止滞后窗口的旧数据被当成功渲染)。
"""
from __future__ import annotations

import abc
import logging
import os
from datetime import timedelta

import market_hours

log = logging.getLogger("kline_source")

# 分钟末根 bar 距今超过该天数视为数据源滞后 (判失败, 继续回退)。
# 覆盖长假 + 短期停牌; 麦蕊 fsjy 冻结窗口 (数月) 会被拦下。
MINUTE_STALE_DAYS = 30


class KlineSource(abc.ABC):
    """K线数据源接口: supports 声明能力, fetch 返回标准 df 或 None。"""

    name = ""

    def supports(self, category: str, period: str) -> bool:
        """该源能否服务 (category, period) 组合, 不支持直接跳过不发起请求。"""
        raise NotImplementedError

    @abc.abstractmethod
    def fetch(self, symbol: str, period: str, count: int):
        """拉取 K 线, 返回标准 DataFrame 或 None (异常自行吞掉并记日志)。"""
        raise NotImplementedError


class MairuiSource(KlineSource):
    """麦蕊智数: 股票/指数/基金 日周月K + 分钟K (hszbl/fsjy, 1m/北交所除外)。"""

    name = "mairui"

    def supports(self, category, period):
        if category in ("stock", "index", "fund"):
            return period in ("1d", "1w", "1M")
        if category == "minute":
            return period in ("5m", "15m", "30m", "60m")
        return False

    def fetch(self, symbol, period, count):
        import market
        if period in market.MINUTE_PERIODS:
            return market._fetch_mr_minute_kline(symbol, period, count)
        if market._is_etf(symbol):
            return market._fetch_fund_kline(symbol, period, count)
        return market._fetch_mr_kline(symbol, period, count)


class AlphaFeedSource(KlineSource):
    """AlphaFeed: 分钟K主源 + 股票/ETF 日K备选 (未复权, 指数未验证)。"""

    name = "alphafeed"

    def supports(self, category, period):
        import market
        if category == "minute":
            return period in market.MINUTE_PERIODS
        return category in ("stock", "fund") and period == "1d"

    def fetch(self, symbol, period, count):
        import market
        if period in market.MINUTE_PERIODS:
            return market._fetch_minute_kline(symbol, period, count)
        return market._fetch_af_daily_kline(symbol, count)


class AkshareSource(KlineSource):
    """akshare(东财) 免费兜底: 日/周/月K + 分钟K, 无需 key。

    列名映射与单位实测见 probe_akshare_source.py: 股票日K与麦蕊逐位一致;
    基金日K东财为「手」麦蕊为「股」, fetch 内 ×100 对齐主图既有口径,
    保证今日 bar 合成 (_daily_bar_from_quote 的 ETF ×100) 跨源一致。
    东财限流期可能持续拒绝连接 -> 返回 None, 由回退链下沉。
    """

    name = "akshare"

    _AK_PERIOD = {"1d": "daily", "1w": "weekly", "1M": "monthly"}
    _CN_COLS = {
        "开盘": "open", "最高": "high", "最低": "low", "收盘": "close",
        "成交量": "volume", "成交额": "amount",
    }

    def supports(self, category, period):
        import market
        if category == "minute":
            return period in market.MINUTE_PERIODS
        return category in ("stock", "index", "fund") and period in ("1d", "1w", "1M")

    def fetch(self, symbol, period, count):
        import akshare as ak
        import market
        if period in market.MINUTE_PERIODS:
            return self._fetch_minute(ak, market, symbol, period, count)
        return self._fetch_daily(ak, market, symbol, period, count)

    def _fetch_daily(self, ak, market, symbol, period, count):
        # 东财按起止日期取数: count 根日K ≈ 1.7 倍自然日 + 缓冲 (同 get_daily_bar)
        natural = int(count * 1.7) + 40
        start = (market_hours.now() - timedelta(days=natural)).strftime("%Y%m%d")
        code = symbol.split(".")[0]
        try:
            if market._is_etf(symbol):
                df = ak.fund_etf_hist_em(symbol=code, period=self._AK_PERIOD[period],
                                         start_date=start, end_date="20991231", adjust="")
            elif market._is_index_symbol(symbol):
                df = ak.index_zh_a_hist(symbol=code, period=self._AK_PERIOD[period],
                                        start_date=start, end_date="20991231")
            else:
                df = ak.stock_zh_a_hist(symbol=code, period=self._AK_PERIOD[period],
                                        start_date=start, end_date="20991231", adjust="")
        except Exception as e:
            log.warning(f"akshare 获取 {symbol} {period} 失败: {market._sanitize_error(e)}")
            return None
        df = self._rename_cn(df, date_col="日期")
        if df is None:
            return None
        if market._is_etf(symbol):
            # 东财基金日K volume=「手」vs 麦蕊 jj/lskx=「股」(probe 实测恰差 100 倍)
            df["volume"] = df["volume"] * 100
        df = market._normalize(df)
        if df is not None and count:
            df = df.tail(count)
        return df

    def _fetch_minute(self, ak, market, symbol, period, count):
        code = symbol.split(".")[0]
        minutes = period[:-1]  # "5m" -> 东财接口的 "5"
        # 东财分钟接口: 股票/ETF 支持 adjust, 指数接口无该参数;
        # 1m 走 trends2 仅近 5 个交易日, 5m+ 走 kline 接口全量 (服务端截 1488 根)
        try:
            if market._is_etf(symbol):
                df = ak.fund_etf_hist_min_em(symbol=code, period=minutes, adjust="")
            elif market._is_index_symbol(symbol):
                df = ak.index_zh_a_hist_min_em(symbol=code, period=minutes)
            else:
                df = ak.stock_zh_a_hist_min_em(symbol=code, period=minutes, adjust="")
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


SOURCES = {cls.name: cls() for cls in (MairuiSource, AlphaFeedSource, AkshareSource)}

CATEGORY_ENV = {
    "minute": "KLINE_SOURCE_MINUTE",
    "stock": "KLINE_SOURCE_STOCK",
    "index": "KLINE_SOURCE_INDEX",
    "fund": "KLINE_SOURCE_FUND",
}

# 券商/付费源优先, akshare 兜底。mairui 分钟实测数据窗口滞后 (见
# probe_mairui_minute.py), 故默认不入分钟链; 可显式配置
# KLINE_SOURCE_MINUTE=alphafeed,mairui,akshare 加入 (新鲜度守卫自动拦旧数据)。
DEFAULT_CHAINS = {
    "minute": "alphafeed,akshare",
    "stock": "mairui,alphafeed,akshare",
    "index": "mairui,akshare",
    "fund": "mairui,alphafeed,akshare",
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


def fetch_kline_df(category, symbol, period, count):
    """按类别数据源链依次尝试, 返回 (标准 df, 源名) 或 (None, None)。

    单源失败 (异常/空数据/分钟数据过旧) 记 warning 后继续下一源; 全部失败
    返回 (None, None), 对外表现与旧版单源一致 (调用方返回 404)。
    """
    chain = _chain(category)
    for i, name in enumerate(chain):
        src = SOURCES[name]
        if not src.supports(category, period):
            continue
        try:
            df = src.fetch(symbol, period, count)
        except Exception as e:  # 单源异常不拖垮整条链
            log.warning("数据源 %s 获取 %s %s 异常: %s", name, symbol, period, e)
            df = None
        if df is not None and category == "minute" and not _minute_fresh(df):
            log.warning("数据源 %s 分钟K数据过旧 (末根 %s), 视为失败",
                        name, df.index[-1])
            df = None
        if df is not None:
            if i > 0:
                log.info("%s %s 已回退到数据源 %s", symbol, period, name)
            return df, name
        if i < len(chain) - 1:
            log.warning("数据源 %s 获取 %s %s 失败, 回退 %s",
                        name, symbol, period, chain[i + 1])
    return None, None
