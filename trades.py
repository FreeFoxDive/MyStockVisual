"""
交易记录统计模块
================
SQLite 持久化的多账户交易日志：注册/登录、买卖记录增删改查、按周/月/年统计盈亏与胜率。

仅依赖 Python 标准库 (sqlite3 / hashlib / secrets)，与 visual 项目"零额外依赖"保持一致。

数据表:
  - users    用户账户 (PBKDF2-SHA256 口令哈希)
  - sessions 会话令牌 (30 天过期)
  - models   量化模型 (全局共享, 软删除)
  - trades   交易记录 (open 持仓 / closed 平仓, 可选关联 model_id, 可选止盈/止损/保本)
  - trade_legs 批次交易腿
  - monitor_alerts 持仓监控告警 (去重)

数据库文件默认位于 `visual/data/trades.db`。
"""

import hashlib
import hmac
import json
import secrets
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# ── 路径与常量 ──
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = SCRIPT_DIR / "data" / "trades.db"

_db_path = DEFAULT_DB_PATH

# 所有入库时间戳统一北京时间 (naive UTC+8), 与容器/系统时区无关,
# 避免 UTC 容器下 fired_at 显示差 8 小时、加速告警冷却误判刷屏。
_CST = timezone(timedelta(hours=8))


def _now():
    return datetime.now(_CST).replace(tzinfo=None)

PBKDF2_ITERATIONS = 200_000      # PBKDF2-SHA256 迭代次数
SESSION_TTL_DAYS = 30            # 会话有效期 (天)

# ── 预设理由分类 (参考券商/交易社区常用口径) ──
ENTRY_REASONS = [
    "突破买入",
    "均线金叉",
    "MACD金叉",
    "回踩支撑",
    "超跌反弹",
    "趋势跟随",
    "形态突破",
    "放量上涨/资金流入",
    "业绩增长/基本面改善",
    "估值低估",
    "政策利好/行业景气",
    "题材热点/消息面",
    "动力绿转",
    "动力蓝转",
    "其他",
]

EXIT_REASONS = [
    "止盈(达到目标价)",
    "止损(跌破止损位)",
    "均线死叉/MACD死叉",
    "跌破支撑/破位",
    "基本面恶化",
    "利空消息/政策风险",
    "资金流出/放量下跌",
    "调仓换股",
    "时间止损(持有超期)",
    "动力红转",
    "其他",
]

# ── 量化模型种子 (对齐回测管线 A–E) ──
# 策略描述默认留空，不暴露内部策略细节
# hold_days: 推荐持仓交易日; None = 不发到期平仓提醒
SEED_MODELS = [
    ("A 60分钟超短", "", 3),
    ("B 日线波段", "", 20),
    ("C 日线波段·阳包阴", "", 10),
    ("D 动力管线", "", 7),
    ("E K线反转管线", "", None),
]
SEED_HOLD_DAYS = {name: days for name, _desc, days in SEED_MODELS if days is not None}

_UNSET = object()
HOLD_DAYS_MIN = 1
HOLD_DAYS_MAX = 250

# ── 数据库表结构 ──
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    salt          TEXT NOT NULL,
    is_admin         INTEGER NOT NULL DEFAULT 0,
    monitor_enabled  INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS models (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    hold_days   INTEGER,                  -- 推荐持仓交易日, 空=不提醒平仓
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    deleted_at  TEXT
);
-- 启用中的模型名唯一 (软删后同名可复用)
CREATE UNIQUE INDEX IF NOT EXISTS idx_models_name_active
    ON models(name) WHERE active = 1;

CREATE TABLE IF NOT EXISTS trades (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    symbol       TEXT NOT NULL,
    name         TEXT NOT NULL,
    status       TEXT NOT NULL,           -- 'open' 持仓 / 'closed' 已平仓
    entry_price  REAL NOT NULL,
    exit_price   REAL,                    -- open 时为空
    quantity     INTEGER NOT NULL,        -- 数量/股数
    entry_date   TEXT NOT NULL,           -- YYYY-MM-DD
    exit_date    TEXT,                    -- YYYY-MM-DD
    entry_reason TEXT NOT NULL,
    entry_note   TEXT,
    exit_reason  TEXT,                    -- open 时为空
    exit_note    TEXT,
    model_id     INTEGER,                 -- 量化模型 (可空, 软删不影响历史)
    type         TEXT NOT NULL DEFAULT 'simple',  -- 'simple' 单笔 / 'batch' 批次 / 'reverse_repo' 逆回购
    take_profit  REAL,                    -- 建议止盈价, 可空
    stop_loss    REAL,                    -- 建议止损价, 可空
    breakeven    REAL,                    -- 建议保本价, 可空
    repo_days    INTEGER,                 -- 逆回购实际计息天数 (默认=期限, 可覆盖节假日放大)
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (model_id) REFERENCES models(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_user_exit   ON trades(user_id, exit_date);
CREATE INDEX IF NOT EXISTS idx_trades_user_symbol ON trades(user_id, symbol);

CREATE TABLE IF NOT EXISTS trade_legs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id   INTEGER NOT NULL,
    side       TEXT NOT NULL,            -- 'buy' | 'sell'
    price      REAL NOT NULL,
    quantity   INTEGER NOT NULL,
    date       TEXT NOT NULL,            -- YYYY-MM-DD
    time       TEXT,                     -- HH:MM[:SS], 可选; 用于同日内正T/反T排序
    reason     TEXT,                     -- 该腿理由 (自由文本)
    note       TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (trade_id) REFERENCES trades(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_trade_legs_trade ON trade_legs(trade_id, date, id);

CREATE TABLE IF NOT EXISTS monitor_alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    trade_id    INTEGER,
    symbol      TEXT NOT NULL,
    alert_type  TEXT NOT NULL,
    trade_date  TEXT NOT NULL,
    fired_at    TEXT NOT NULL,
    price       REAL,
    detail      TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (trade_id) REFERENCES trades(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_monitor_alerts_lookup
    ON monitor_alerts(user_id, symbol, alert_type, fired_at);
"""


# ── 交易费率默认值 ──
# 比例均为小数 (如 0.0001 = 万分之一); reverse_repo_rates 为国债逆回购佣金率
# (十万分之X → 小数, 键为期限天数)。
DEFAULT_FEES = {
    "commission_rate_stock": 0.0001,   # 沪深股票 佣金 万一
    "commission_rate_etf":   0.0001,   # ETF     佣金 万一
    "commission_rate_hk":    0.0001,   # 港股通  佣金 万一
    "min_commission":        5.0,      # A股/ETF 每笔佣金起点 (元, 买卖各算)
    "stamp_duty_rate":       0.0005,   # 印花税 0.05% 仅卖出 (ETF 免征)
    "transfer_fee_rate":     0.00001,  # 过户费 0.001% 买卖双向
    "reverse_repo_rates": {            # 国债逆回购 佣金率 (十万分之X → 小数)
        "1": 0.00001, "2": 0.00002, "3": 0.00003, "4": 0.00004,
        "7": 0.00005, "14": 0.00010, "28": 0.00020,
        "91": 0.00030, "182": 0.00030,
    },
}

# 逆回购代码 → 期限 (天)
_REPO_SSE = {"001": "1", "002": "2", "003": "3", "004": "4", "007": "7",
             "014": "14", "028": "28", "091": "91", "182": "182"}   # 204xxx.SH
_REPO_SZSE = {"810": "1", "811": "2", "800": "3", "809": "4", "801": "7",
              "802": "14", "803": "28", "805": "91", "806": "182"}  # 1318xx.SZ


def _now_iso():
    return _now().isoformat(timespec="seconds")


def init_db(db_path=None):
    """建库建表 (幂等)。传入 db_path 可覆盖默认位置。"""
    global _db_path
    if db_path is not None:
        _db_path = Path(db_path)
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_db_path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(SCHEMA)
        # 迁移: 旧库 users 表补 is_admin 列 (CREATE TABLE IF NOT EXISTS 不会加列)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
        if "is_admin" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
        # 迁移: 旧库 trades 表补 model_id 列
        tcols = {r[1] for r in conn.execute("PRAGMA table_info(trades)")}
        if "model_id" not in tcols:
            conn.execute(
                "ALTER TABLE trades ADD COLUMN model_id INTEGER "
                "REFERENCES models(id) ON DELETE SET NULL"
            )
        # 迁移: 旧库 trades 表补 type 列 (默认 'simple', 老数据零改动)
        if "type" not in tcols:
            conn.execute(
                "ALTER TABLE trades ADD COLUMN type TEXT NOT NULL DEFAULT 'simple'"
            )
        # 迁移: 旧库 users 表补 fee_config 列 (TEXT 存 JSON; NULL = 用默认值)
        if "fee_config" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN fee_config TEXT")
        # 迁移: 旧库 users 表补 monitor_enabled 列 (管理员恒开, 普通用户需授权)
        if "monitor_enabled" not in cols:
            conn.execute(
                "ALTER TABLE users ADD COLUMN monitor_enabled INTEGER NOT NULL DEFAULT 0"
            )
        # 迁移: 搜索/浏览历史 (JSON 数组, 跟账号走, 不依赖浏览器 localStorage)
        if "search_history" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN search_history TEXT")
        # 迁移: 旧库 trades 表补风控价格列 (可空, 老数据零改动)
        for col in ("take_profit", "stop_loss", "breakeven"):
            if col not in tcols:
                conn.execute(f"ALTER TABLE trades ADD COLUMN {col} REAL")
        # 迁移: 旧库 trades 表补逆回购计息天数列 (可空, 老数据零改动)
        if "repo_days" not in tcols:
            conn.execute("ALTER TABLE trades ADD COLUMN repo_days INTEGER")
        # 迁移: 旧库 models 表补 hold_days; 仅在刚加列时回填 A–D 默认值, 之后不覆盖人工修改
        mcols = {r[1] for r in conn.execute("PRAGMA table_info(models)")}
        if "hold_days" not in mcols:
            conn.execute("ALTER TABLE models ADD COLUMN hold_days INTEGER")
            for name, days in SEED_HOLD_DAYS.items():
                conn.execute(
                    "UPDATE models SET hold_days=? WHERE name=?", (days, name)
                )
        # 种子模型 A–E (仅当 models 表为空时插入, 含停用行也计入)
        n = conn.execute("SELECT COUNT(*) FROM models").fetchone()[0]
        if n == 0:
            conn.executemany(
                "INSERT INTO models (name, description, hold_days, created_at) "
                "VALUES (?,?,?,?)",
                [(name, desc, days, _now_iso()) for name, desc, days in SEED_MODELS],
            )
        conn.commit()
    finally:
        conn.close()


def get_conn():
    """短连接 (每操作独立, 线程安全)。"""
    conn = sqlite3.connect(str(_db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


# ── 交易费率: 类型识别与单笔费用计算 ──
def classify_symbol(symbol):
    """按代码识别交易类型。

    返回 ("stock"|"etf"|"hk", None) 或 ("reverse_repo", 期限天数)。
    兼容多种写法: 00700 / 00700.HK / 600031.SH / 513290.SH / 204001.SH / 131810.SZ。
    """
    s = (symbol or "").upper()
    code = s.split(".")[0]
    if code.startswith("204") and code[3:] in _REPO_SSE:
        return ("reverse_repo", _REPO_SSE[code[3:]])
    if code.startswith("1318") and code[3:] in _REPO_SZSE:
        return ("reverse_repo", _REPO_SZSE[code[3:]])
    if s.endswith(".HK"):
        return ("hk", None)
    if len(code) == 6 and code[:2] in ("51", "56", "58", "15", "16"):
        return ("etf", None)   # 沪 51/56/58, 深 15/16 (含 LOF, 费率同 ETF)
    if code.isdigit() and len(code) < 6:
        return ("hk", None)    # 5 位及以下数字 → 港股通
    return ("stock", None)


def _calc_fee_detail(trade, fees, side="closed"):
    """计算单笔交易费用明细, fees 为已合并默认值的费率 dict。

    side="closed": 完整往返 (买入+卖出佣金 / 印花税 / 双向过户费);
    side="open":   仅买入侧 (买入佣金+最低佣金 / 买入过户费), 无卖出佣金/印花税。
    返回 dict: buy_comm/sell_comm/stamp/transfer/total (元)。
    """
    kind, tenor = classify_symbol(trade["symbol"])
    buy_amt = trade["entry_price"] * trade["quantity"]

    if kind == "reverse_repo":
        # 逆回购佣金: 成交额 × 期限费率 (单边), 仅买入算一次
        rate = fees.get("reverse_repo_rates", {}).get(tenor, 0.0)
        total = round(buy_amt * rate, 4)
        return {"buy_comm": 0.0, "sell_comm": 0.0, "stamp": 0.0,
                "transfer": 0.0, "total": total}

    rate = fees.get(
        {
            "stock": "commission_rate_stock",
            "etf": "commission_rate_etf",
            "hk": "commission_rate_hk",
        }[kind],
        0.0,
    )
    min_comm = fees.get("min_commission", 0.0) if kind in ("stock", "etf") else 0.0
    buy_comm = buy_amt * rate
    if min_comm:
        buy_comm = max(buy_comm, min_comm)
    buy_transfer = buy_amt * fees.get("transfer_fee_rate", 0.0)

    sell_comm = 0.0
    stamp = 0.0
    sell_transfer = 0.0
    if side == "closed":
        sell_amt = (trade["exit_price"] or 0.0) * trade["quantity"]
        sell_comm = sell_amt * rate
        if min_comm:
            sell_comm = max(sell_comm, min_comm)
        # 印花税: 仅卖出, 且 ETF 免征
        stamp = 0.0 if kind == "etf" else sell_amt * fees.get("stamp_duty_rate", 0.0)
        sell_transfer = sell_amt * fees.get("transfer_fee_rate", 0.0)

    return {
        "buy_comm": round(buy_comm, 4),
        "sell_comm": round(sell_comm, 4),
        "stamp": round(stamp, 4),
        "transfer": round(buy_transfer + sell_transfer, 4),
        "total": round(buy_comm + sell_comm + stamp + buy_transfer + sell_transfer, 4),
    }


def _calc_fees(trade, fees, side="closed"):
    """计算单笔交易费用合计 (元), 见 _calc_fee_detail。"""
    return _calc_fee_detail(trade, fees, side)["total"]


def _calc_side_fees(symbol, side, price, quantity, fees):
    """单边费用明细 (供批次交易每条腿各算一次)。

    side='buy'/'sell'。返回 dict: comm / stamp / transfer / total (元, 各 round(...,4))。
    与 _calc_fee_detail 的费率口径一致, 仅把买入侧/卖出侧拆开, 便于按腿累加。
    """
    kind, tenor = classify_symbol(symbol)
    amt = (price or 0.0) * quantity

    if kind == "reverse_repo":
        # 逆回购佣金: 成交额 × 期限费率 (单边), 仅买入算一次
        rate = fees.get("reverse_repo_rates", {}).get(tenor, 0.0)
        total = round(amt * rate, 4) if side == "buy" else 0.0
        return {"comm": 0.0, "stamp": 0.0, "transfer": 0.0, "total": total}

    rate = fees.get(
        {
            "stock": "commission_rate_stock",
            "etf": "commission_rate_etf",
            "hk": "commission_rate_hk",
        }[kind],
        0.0,
    )
    min_comm = fees.get("min_commission", 0.0) if kind in ("stock", "etf") else 0.0
    comm = amt * rate
    if min_comm:
        comm = max(comm, min_comm)
    transfer = amt * fees.get("transfer_fee_rate", 0.0)
    stamp = 0.0
    if side == "sell" and kind != "etf":
        stamp = amt * fees.get("stamp_duty_rate", 0.0)

    return {
        "comm": round(comm, 4),
        "stamp": round(stamp, 4),
        "transfer": round(transfer, 4),
        "total": round(comm + stamp + transfer, 4),
    }


def _clean_fees(config):
    """校验并规范化费率配置; 非法值抛 ValueError。仅保留 DEFAULT_FEES 中已知键。"""
    if not isinstance(config, dict):
        raise ValueError("配置必须是对象")

    def _num(key, allow_zero=True):
        v = config.get(key)
        if v is None:
            return None  # 未提供 → 由调用方保留默认
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"{key} 必须是数字")
        if allow_zero:
            if f < 0:
                raise ValueError(f"{key} 不能为负")
        elif f <= 0:
            raise ValueError(f"{key} 必须大于 0")
        return f

    clean = {}
    for key in ("commission_rate_stock", "commission_rate_etf", "commission_rate_hk",
                "stamp_duty_rate", "transfer_fee_rate"):
        v = _num(key)
        if v is not None:
            clean[key] = v
    v = _num("min_commission", allow_zero=False)
    if v is not None:
        clean["min_commission"] = v

    rr = config.get("reverse_repo_rates")
    if rr is not None:
        if not isinstance(rr, dict):
            raise ValueError("reverse_repo_rates 必须是对象")
        clean_rr = {}
        for tenor in DEFAULT_FEES["reverse_repo_rates"]:
            tv = rr.get(tenor)
            if tv is None:
                continue
            try:
                f = float(tv)
            except (TypeError, ValueError):
                raise ValueError(f"reverse_repo_rates.{tenor} 必须是数字")
            if f < 0:
                raise ValueError(f"reverse_repo_rates.{tenor} 不能为负")
            clean_rr[tenor] = f
        clean["reverse_repo_rates"] = clean_rr
    return clean


def get_user_fees(user_id):
    """读取用户费率 (未设置则返回默认值)。"""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT fee_config FROM users WHERE id=?", (user_id,)
        ).fetchone()
    finally:
        conn.close()
    if row and row["fee_config"]:
        try:
            merged = dict(DEFAULT_FEES)
            merged.update(json.loads(row["fee_config"]))
            return merged
        except (ValueError, TypeError):
            pass
    return dict(DEFAULT_FEES)


def update_user_fees(user_id, config):
    """保存用户费率 (校验并规范化后写库)。"""
    clean = _clean_fees(config)
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE users SET fee_config=? WHERE id=?",
            (json.dumps(clean), user_id),
        )
        conn.commit()
    finally:
        conn.close()
    return clean


# ── 口令哈希 ──
def hash_password(password, salt=None):
    """PBKDF2-SHA256 派生密钥，返回 (hash_hex, salt_hex)。"""
    if salt is None:
        salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITERATIONS
    )
    return dk.hex(), salt


def verify_password(password, salt, password_hash):
    dk, _ = hash_password(password, salt)
    return hmac.compare_digest(dk, password_hash)


# ── 鉴权 ──
def create_user(username, password, is_admin=False):
    """创建用户并返回 user_id（不登录）。用户名已存在抛 ValueError。"""
    username = username.strip()
    if not (2 <= len(username) <= 32):
        raise ValueError("用户名长度需 2~32 个字符")
    if len(password) < 6:
        raise ValueError("密码至少 6 位")

    password_hash, salt = hash_password(password)
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO users(username, password_hash, salt, is_admin, created_at) VALUES(?,?,?,?,?)",
            (username, password_hash, salt, 1 if is_admin else 0, _now_iso()),
        )
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        raise ValueError("用户名已存在")
    finally:
        conn.close()


def login(username, password):
    """校验口令，成功返回 (token, expires_iso)，失败返回 None。"""
    username = username.strip()
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id, password_hash, salt FROM users WHERE username=?", (username,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    if not verify_password(password, row["salt"], row["password_hash"]):
        return None
    return create_session(row["id"])


def create_session(user_id):
    token = secrets.token_hex(32)
    now = _now()
    expires = now + timedelta(days=SESSION_TTL_DAYS)
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO sessions(token, user_id, created_at, expires_at) VALUES(?,?,?,?)",
            (token, user_id, now.isoformat(timespec="seconds"),
             expires.isoformat(timespec="seconds")),
        )
        conn.commit()
    finally:
        conn.close()
    return token, expires.isoformat(timespec="seconds")


def get_session(token):
    """根据令牌返回当前用户 dict {id, username, is_admin, monitor_enabled}，无效/过期返回 None。"""
    if not token:
        return None
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT s.token, s.expires_at, u.id, u.username, u.is_admin, u.monitor_enabled "
            "FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token = ?",
            (token,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    try:
        expires = datetime.fromisoformat(row["expires_at"])
    except ValueError:
        return None
    if expires < _now():
        delete_session(token)
        return None
    return {
        "id": row["id"],
        "username": row["username"],
        "is_admin": bool(row["is_admin"]),
        "monitor_enabled": bool(row["monitor_enabled"]),
    }


def delete_session(token):
    if not token:
        return
    conn = get_conn()
    try:
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        conn.commit()
    finally:
        conn.close()


# ── 搜索/浏览历史 (跟账号持久化) ──
SEARCH_HISTORY_MAX = 15


def _normalize_search_history(raw):
    """校验为 [{symbol, name, ts?}, ...]，最多 SEARCH_HISTORY_MAX 条。

    排序：有 ts 的按 ts 降序在前；无 ts 的旧数据一律排在后面（保持原相对顺序）。
    同 symbol：优先保留带 ts 的；都有 ts 取较大者；都无 ts 保留先出现的。
    """
    if not isinstance(raw, list):
        return []
    best = {}
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").strip()[:32]
        name = str(item.get("name") or symbol).strip()[:64]
        if not symbol:
            continue
        name = name or symbol
        ts = None
        if "ts" in item and item.get("ts") is not None:
            try:
                ts_val = int(item["ts"])
                if ts_val >= 0:
                    ts = ts_val
            except (TypeError, ValueError):
                ts = None
        entry = {"symbol": symbol, "name": name, "_i": i}
        if ts is not None:
            entry["ts"] = ts
        prev = best.get(symbol)
        if prev is None:
            best[symbol] = entry
            continue
        prev_ts = prev.get("ts")
        if ts is not None and (prev_ts is None or ts >= prev_ts):
            best[symbol] = entry
        # else: 当前无 ts 而 prev 有 ts → 保留 prev；都无 ts → 保留先出现的 prev

    with_ts = [e for e in best.values() if "ts" in e]
    without_ts = [e for e in best.values() if "ts" not in e]
    with_ts.sort(key=lambda e: e["ts"], reverse=True)
    without_ts.sort(key=lambda e: e["_i"])
    out = []
    for e in with_ts + without_ts:
        row = {"symbol": e["symbol"], "name": e["name"]}
        if "ts" in e:
            row["ts"] = e["ts"]
        out.append(row)
        if len(out) >= SEARCH_HISTORY_MAX:
            break
    return out


def get_search_history(user_id):
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT search_history FROM users WHERE id=?", (user_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row or not row["search_history"]:
        return []
    try:
        raw = json.loads(row["search_history"])
    except (TypeError, json.JSONDecodeError):
        return []
    return _normalize_search_history(raw)


def set_search_history(user_id, history, *, allow_clear=True):
    """写入搜索历史。allow_clear=False 时，禁止用空列表抹掉已有记录（防客户端清缓存误上传）。"""
    cleaned = _normalize_search_history(history)
    if not cleaned and not allow_clear:
        existing = get_search_history(user_id)
        if existing:
            return existing
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE users SET search_history=? WHERE id=?",
            (json.dumps(cleaned, ensure_ascii=False), user_id),
        )
        conn.commit()
    finally:
        conn.close()
    return cleaned


# ── 用户管理 (仅 admin) ──
def count_admins():
    """返回管理员数量，供启动引导判断。"""
    conn = get_conn()
    try:
        row = conn.execute("SELECT COUNT(*) c FROM users WHERE is_admin=1").fetchone()
        return row["c"]
    finally:
        conn.close()


def sync_admin_password(username, password):
    """将管理员口令与 .env 对齐 (启动时调用)。

    返回 'updated' | 'unchanged' | 'not_found' | 'not_admin'。
    口令变化时更新哈希并吊销该管理员全部会话 (旧口令/旧 token 立即失效)。
    """
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id, password_hash, salt, is_admin FROM users WHERE username=?",
            (username,),
        ).fetchone()
    finally:
        conn.close()

    if not row:
        return "not_found"
    if not row["is_admin"]:
        return "not_admin"
    if verify_password(password, row["salt"], row["password_hash"]):
        return "unchanged"

    password_hash, salt = hash_password(password)
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE users SET password_hash=?, salt=? WHERE id=?",
            (password_hash, salt, row["id"]),
        )
        conn.execute("DELETE FROM sessions WHERE user_id=?", (row["id"],))
        conn.commit()
    finally:
        conn.close()
    return "updated"


def list_users():
    """返回所有用户 {id, username, is_admin, monitor_enabled, created_at}（不含口令哈希/盐）。"""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, username, is_admin, monitor_enabled, created_at FROM users ORDER BY id"
        ).fetchall()
        return [
            {
                "id": r["id"],
                "username": r["username"],
                "is_admin": bool(r["is_admin"]),
                "monitor_enabled": bool(r["monitor_enabled"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]
    finally:
        conn.close()


def set_user_monitor(user_id, enabled):
    """设置普通用户的持仓监控授权。管理员恒开, 改 flag 不影响实际行为。用户不存在返回 False。"""
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE users SET monitor_enabled=? WHERE id=?",
            (1 if enabled else 0, user_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def user_can_monitor(user):
    """管理员恒开; 普通用户看 monitor_enabled。"""
    if not user:
        return False
    return bool(user.get("is_admin") or user.get("monitor_enabled"))


def delete_user(user_id):
    """删除用户 (级联删除其 trades/sessions)。管理员不可删，抛 ValueError。"""
    conn = get_conn()
    try:
        row = conn.execute("SELECT is_admin FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            return False
        if row["is_admin"]:
            raise ValueError("不能删除管理员")
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))
        conn.commit()
        return True
    finally:
        conn.close()


def reset_password(user_id, new_password):
    """重置指定用户口令。密码过短抛 ValueError；用户不存在返回 False。"""
    if len(new_password) < 6:
        raise ValueError("密码至少 6 位")
    password_hash, salt = hash_password(new_password)
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE users SET password_hash=?, salt=? WHERE id=?",
            (password_hash, salt, user_id),
        )
        # 重置口令后吊销该用户所有会话，防止旧 token 继续有效
        conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ── 量化模型管理 (全局共享, 软删除) ──
def _parse_hold_days(value):
    """空/None → None (不提醒); 1~250 的整数原样返回; 其余抛 ValueError。"""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("推荐持仓交易日无效")
    if isinstance(value, float):
        if value != int(value):
            raise ValueError("推荐持仓交易日须为整数")
        value = int(value)
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValueError("推荐持仓交易日无效")
    if isinstance(value, str) and value.strip() != str(n):
        raise ValueError("推荐持仓交易日无效")
    if n < HOLD_DAYS_MIN or n > HOLD_DAYS_MAX:
        raise ValueError(f"推荐持仓交易日须为 {HOLD_DAYS_MIN}~{HOLD_DAYS_MAX}")
    return n


def list_models(active_only=True):
    """返回模型列表 {id, name, description, hold_days, active, created_at, deleted_at}。

    active_only=True 只返回启用中的 (供交易下拉); False 返回全部 (供管理员列表)。
    """
    sql = "SELECT id, name, description, hold_days, active, created_at, deleted_at FROM models"
    if active_only:
        sql += " WHERE active=1"
    sql += " ORDER BY id"
    conn = get_conn()
    try:
        rows = conn.execute(sql).fetchall()
        return [
            {
                "id": r["id"],
                "name": r["name"],
                "description": r["description"],
                "hold_days": r["hold_days"],
                "active": bool(r["active"]),
                "created_at": r["created_at"],
                "deleted_at": r["deleted_at"],
            }
            for r in rows
        ]
    finally:
        conn.close()


def create_model(name, description, hold_days=None):
    """新增模型并返回 model_id。名称非空/启用中重名抛 ValueError。"""
    name = (name or "").strip()
    if not name:
        raise ValueError("模型名称不能为空")
    if len(name) > 64:
        raise ValueError("模型名称过长")
    description = (description or "").strip()
    days = _parse_hold_days(hold_days)
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO models(name, description, hold_days, active, created_at) "
            "VALUES(?,?,?,1,?)",
            (name, description, days, _now_iso()),
        )
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        raise ValueError("模型名已存在")
    finally:
        conn.close()


def update_model(model_id, name, description, hold_days=_UNSET):
    """更新模型名称/描述/推荐持仓日。模型不存在返回 False；启用中重名抛 ValueError。

    hold_days 默认不改; 传入 None/'' 清空 (不再提醒平仓)。
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("模型名称不能为空")
    if len(name) > 64:
        raise ValueError("模型名称过长")
    description = (description or "").strip()
    conn = get_conn()
    try:
        if hold_days is _UNSET:
            cur = conn.execute(
                "UPDATE models SET name=?, description=? WHERE id=?",
                (name, description, model_id),
            )
        else:
            days = _parse_hold_days(hold_days)
            cur = conn.execute(
                "UPDATE models SET name=?, description=?, hold_days=? WHERE id=?",
                (name, description, days, model_id),
            )
        if cur.rowcount == 0:
            return False
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        raise ValueError("模型名已存在")
    finally:
        conn.close()


def delete_model(model_id):
    """软删除模型 (置 active=0, 不物理删行, 交易记录不受影响)。"""
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE models SET active=0, deleted_at=? WHERE id=?",
            (_now_iso(), model_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def restore_model(model_id):
    """恢复停用模型 (置 active=1)。恢复后与启用中重名抛 ValueError。"""
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE models SET active=1, deleted_at=NULL WHERE id=?",
            (model_id,),
        )
        if cur.rowcount == 0:
            return False
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        raise ValueError("模型名与启用中的模型重复")
    finally:
        conn.close()


def _model_exists(model_id):
    """模型存在 (含停用行) 返回 True, 否则 False。"""
    conn = get_conn()
    try:
        row = conn.execute("SELECT 1 FROM models WHERE id=?", (model_id,)).fetchone()
        return row is not None
    finally:
        conn.close()


# ── 交易记录 CRUD ──
def _valid_date(s):
    try:
        date.fromisoformat(s)
        return True
    except (ValueError, TypeError):
        return False


def _today_iso():
    return _now().date().isoformat()


def _reject_future_date(date_str, label="日期"):
    """未来日返回错误文案，否则 None。"""
    if date_str > _today_iso():
        return f"{label}不能晚于今天"
    return None


def _batch_leg_label(i, n):
    """批次 API 数组与 UI 列表同序(最新在上); 第 N 笔与界面编号一致。"""
    return n - i


def _validate_price_on_bar(symbol, date_str, price, side_label, leg_prefix=""):
    """校验价在当日振幅内且成交量>0。返回错误文案或 None。

    side_label: '买入' / '卖出'；leg_prefix: '第N笔 '（可空）。
    """
    try:
        from market import get_daily_bar
        bar = get_daily_bar(symbol, date_str)
    except Exception:
        bar = None

    pfx = leg_prefix or ""
    if bar is None:
        return f"{pfx}{date_str} 无日K数据（非交易日或未上市）"
    vol = bar.get("volume") or 0
    if vol <= 0:
        return f"{pfx}{date_str} 成交量为0，疑似停牌，不能录入"

    high = float(bar["high"])
    low = float(bar["low"])
    # 分位比较，避免浮点毛刺
    price_c = round(float(price) * 100)
    low_c = round(low * 100)
    high_c = round(high * 100)
    if price_c < low_c or price_c > high_c:
        return (
            f"{pfx}{side_label}价 {float(price):.2f} 不在当日振幅 "
            f"[{low:.2f}, {high:.2f}] 内"
        )
    return None


def _repo_maturity(entry_date, tenor_days):
    """逆回购到期日 = 买入日 + 期限 (自然日); 落在非交易日则顺延到下一交易日。"""
    try:
        d = date.fromisoformat(str(entry_date)[:10]) + timedelta(days=int(tenor_days))
    except (ValueError, TypeError):
        return None
    import market_hours
    while not market_hours.is_trading_day(d.strftime("%Y-%m-%d")):
        d += timedelta(days=1)
    return d.isoformat()


def _repo_net_income(principal, rate_pct, days, commission_rate):
    """逆回购净收益: 利息 - 佣金。principal 本金(元), rate_pct 年化利率(%), days 计息天数。"""
    interest = principal * (rate_pct / 100.0) * days / 365.0
    commission = principal * (commission_rate or 0.0)
    return interest, commission


def _clean_reverse_repo(data, existing=None):
    """校验并规范化逆回购交易 (type='reverse_repo')。

    字段映射: entry_price=年化利率(%), quantity=本金(元), entry_date=买入日,
    exit_date=到期日(自动), repo_days=实际计息天数(默认=期限)。status 恒 open,
    到期由 exit_date 判定 (自动视为平仓)。
    """
    merged = {}
    if existing:
        merged.update(existing)
    for k, v in data.items():
        if v is not None:
            merged[k] = v

    def s(v):
        return v.strip() if isinstance(v, str) else v

    symbol = s(merged.get("symbol", "")).upper()
    if not symbol:
        return None, "缺少逆回购代码"
    kind, tenor = classify_symbol(symbol)
    if kind != "reverse_repo":
        return None, f"{symbol} 不是逆回购代码 (应为 204xxx.SH 或 1318xx.SZ)"
    tenor = int(tenor)

    name = s(merged.get("name", ""))
    if not name:
        name = f"国债逆回购{tenor}天"

    raw_rate = merged.get("entry_price")
    if raw_rate in (None, ""):
        return None, "缺少成交利率"
    try:
        rate = float(raw_rate)
    except (TypeError, ValueError):
        return None, "成交利率无效"
    if rate <= 0:
        return None, "成交利率必须大于 0"

    raw_p = merged.get("quantity")
    if raw_p in (None, ""):
        return None, "缺少本金金额"
    try:
        principal = float(raw_p)
    except (TypeError, ValueError):
        return None, "本金无效"
    if principal <= 0:
        return None, "本金必须大于 0"
    if principal < 1000:
        return None, "逆回购最低 1000 元"

    entry_date = s(merged.get("entry_date", ""))
    if not _valid_date(entry_date):
        return None, "买入日期无效 (格式 YYYY-MM-DD)"
    fut = _reject_future_date(entry_date, "买入日期")
    if fut:
        return None, fut

    days = tenor
    raw_days = merged.get("repo_days")
    if raw_days not in (None, ""):
        try:
            days = int(float(raw_days))
        except (TypeError, ValueError):
            return None, "计息天数无效"
        if days < 1 or days > 400:
            return None, "计息天数须为 1~400"

    exit_date = _repo_maturity(entry_date, tenor)
    if not exit_date:
        return None, "到期日计算失败"

    entry_reason = s(merged.get("entry_reason", ""))
    entry_note = s(merged.get("entry_note")) or None

    return {
        "symbol": symbol,
        "name": name,
        "status": "open",
        "type": "reverse_repo",
        "entry_price": rate,
        "exit_price": None,
        "quantity": principal,
        "entry_date": entry_date,
        "exit_date": exit_date,
        "entry_reason": entry_reason,
        "entry_note": entry_note,
        "exit_reason": None,
        "exit_note": None,
        "model_id": None,
        "repo_days": days,
        "take_profit": None,
        "stop_loss": None,
        "breakeven": None,
    }, None


_RISK_KEYS = ("take_profit", "stop_loss", "breakeven")


def _parse_risk_prices(merged, enforce=True):
    """解析止盈/止损/保本。空串或 None → 清空为 None; 非空须 > 0。

    enforce=True 时须全部留空或全部填写, 且 止盈 > 保本 > 止损。
    旧记录未改风控字段时 enforce=False, 允许历史不完整数据继续读出。

    返回 ({take_profit, stop_loss, breakeven}, error_msg)。
    """
    result = {}
    for key, label in (
        ("take_profit", "止盈价"),
        ("stop_loss", "止损价"),
        ("breakeven", "保本价"),
    ):
        raw = merged.get(key, None)
        if raw in (None, ""):
            result[key] = None
            continue
        try:
            val = float(raw)
        except (TypeError, ValueError):
            return None, f"{label}无效"
        if val <= 0:
            return None, f"{label}必须大于 0"
        result[key] = val
    if not enforce:
        return result, None
    filled = [k for k in _RISK_KEYS if result[k] is not None]
    if not filled:
        return result, None
    if len(filled) != 3:
        return None, "止盈/止损/保本须全部填写或全部留空"
    if not (result["take_profit"] > result["breakeven"] > result["stop_loss"]):
        return None, "须满足止盈价 > 保本价 > 止损价"
    return result, None


def _clean_batch(data, existing=None):
    """校验并规范化批次交易 (type='batch')。返回 (clean_dict, error_msg)。

    腿按 (date, time) 排序后滚动校验超卖, 并回填父行汇总字段 (净持仓/加权均价/首买日/末卖日/status)。
    """
    merged = {}
    if existing:
        merged.update(existing)
    for k, v in data.items():
        if v is not None:
            merged[k] = v

    def s(v):
        return v.strip() if isinstance(v, str) else v

    symbol = s(merged.get("symbol", "")).upper()
    name = s(merged.get("name", ""))
    if not symbol:
        return None, "缺少股票代码"
    if not name:
        return None, "缺少股票名称"

    model_id = None
    if merged.get("model_id") not in (None, ""):
        try:
            model_id = int(merged["model_id"])
        except (TypeError, ValueError):
            return None, "模型无效"
        if not _model_exists(model_id):
            return None, "模型不存在"

    raw_legs = merged.get("legs")
    if not isinstance(raw_legs, list) or not raw_legs:
        return None, "批次交易至少需要一笔"

    legs = []
    has_buy = False
    n_legs = len(raw_legs)
    for i, leg in enumerate(raw_legs):
        leg_no = _batch_leg_label(i, n_legs)
        if not isinstance(leg, dict):
            return None, f"第{leg_no}笔格式无效"
        side = s(leg.get("side", ""))
        if side not in ("buy", "sell"):
            return None, f"第{leg_no}笔方向必须为买入或卖出"
        try:
            price = float(leg.get("price"))
        except (TypeError, ValueError):
            return None, f"第{leg_no}笔价格无效"
        if price <= 0:
            return None, f"第{leg_no}笔价格必须大于 0"
        try:
            qty = int(leg.get("quantity"))
        except (TypeError, ValueError):
            return None, f"第{leg_no}笔数量无效"
        if qty <= 0:
            return None, f"第{leg_no}笔数量必须大于 0"
        date_str = s(leg.get("date", ""))
        if not _valid_date(date_str):
            return None, f"第{leg_no}笔日期无效 (格式 YYYY-MM-DD)"
        fut = _reject_future_date(date_str, f"第{leg_no}笔日期")
        if fut:
            return None, fut
        side_label = "买入" if side == "buy" else "卖出"
        bar_err = _validate_price_on_bar(
            symbol, date_str, price, side_label, leg_prefix=f"第{leg_no}笔 ",
        )
        if bar_err:
            return None, bar_err
        time_str = s(leg.get("time")) or None
        if time_str is not None:
            parts = time_str.split(":")
            if len(parts) not in (2, 3) or not all(p.isdigit() for p in parts):
                return None, f"第{leg_no}笔时间无效 (格式 HH:MM 或 HH:MM:SS)"
        if side == "buy":
            has_buy = True
        legs.append({
            "side": side,
            "price": price,
            "quantity": qty,
            "date": date_str,
            "time": time_str,
            "reason": s(leg.get("reason")) or None,
            "note": s(leg.get("note")) or None,
        })

    if not has_buy:
        return None, "批次交易至少需要一笔买入"

    # 按 (date, time) 排序后滚动校验超卖 + 计算净持仓/加权均价
    legs.sort(key=lambda l: (l["date"], l["time"] or "", 0))
    held = 0
    cost_total = 0.0
    for leg in legs:
        if leg["side"] == "buy":
            held += leg["quantity"]
            cost_total += leg["price"] * leg["quantity"]
        else:
            if leg["quantity"] > held:
                return None, "卖出数量超过当前持仓"
            avg = cost_total / held if held else 0.0
            held -= leg["quantity"]
            cost_total = held * avg

    status = "closed" if held == 0 else "open"
    avg_cost = cost_total / held if held else 0.0
    first_buy = next(l for l in legs if l["side"] == "buy")
    sells = [l for l in legs if l["side"] == "sell"]
    last_sell = sells[-1] if sells else None

    clean = {
        "symbol": symbol,
        "name": name,
        "model_id": model_id,
        "type": "batch",
        "legs": legs,
        "status": status,
        "entry_price": round(avg_cost, 4),
        "quantity": held,
        "entry_date": first_buy["date"],
        "exit_price": None,
        "exit_date": last_sell["date"] if status == "closed" else None,
        "entry_reason": first_buy.get("reason") or "",
        "entry_note": first_buy.get("note"),
        "exit_reason": last_sell.get("reason") if last_sell else None,
        "exit_note": last_sell.get("note") if last_sell else None,
    }
    enforce_risk = existing is None or any(k in data for k in _RISK_KEYS)
    prices, err = _parse_risk_prices(merged, enforce=enforce_risk)
    if err:
        return None, err
    clean.update(prices)
    return clean, None


def _clean(data, existing=None):
    """合并并规范化字段。返回 (clean_dict, error_msg)。"""
    ttype = data.get("type") or (existing or {}).get("type") or "simple"
    if ttype == "reverse_repo":
        return _clean_reverse_repo(data, existing)
    if ttype not in ("simple", "batch"):
        return None, "type 必须为 simple 或 batch 或 reverse_repo"
    if ttype == "batch":
        return _clean_batch(data, existing)

    merged = {}
    if existing:
        merged.update(existing)
    for k, v in data.items():
        if v is not None:
            merged[k] = v

    def s(v):
        return v.strip() if isinstance(v, str) else v

    status = s(merged.get("status", "open"))
    if status not in ("open", "closed"):
        return None, "status 必须为 open 或 closed"

    symbol = s(merged.get("symbol", "")).upper()
    name = s(merged.get("name", ""))
    if not symbol:
        return None, "缺少股票代码"
    if not name:
        return None, "缺少股票名称"

    try:
        entry_price = float(merged.get("entry_price"))
    except (TypeError, ValueError):
        return None, "买入价无效"
    if entry_price <= 0:
        return None, "买入价必须大于 0"

    try:
        quantity = int(merged.get("quantity"))
    except (TypeError, ValueError):
        return None, "数量无效"
    if quantity <= 0:
        return None, "数量必须大于 0"

    entry_date = s(merged.get("entry_date", ""))
    if not _valid_date(entry_date):
        return None, "买入日期无效 (格式 YYYY-MM-DD)"
    fut = _reject_future_date(entry_date, "买入日期")
    if fut:
        return None, fut
    bar_err = _validate_price_on_bar(symbol, entry_date, entry_price, "买入")
    if bar_err:
        return None, bar_err
    entry_reason = s(merged.get("entry_reason", ""))
    if not entry_reason:
        return None, "缺少买入理由"

    entry_note = s(merged.get("entry_note")) or None
    exit_note = s(merged.get("exit_note")) or None

    # 量化模型: 允许空/空串 → None (「无」); 非空须为存在模型的 id (含停用行)
    model_id = None
    if merged.get("model_id") not in (None, ""):
        try:
            model_id = int(merged["model_id"])
        except (TypeError, ValueError):
            return None, "模型无效"
        if not _model_exists(model_id):
            return None, "模型不存在"

    clean = {
        "symbol": symbol,
        "name": name,
        "status": status,
        "type": "simple",
        "entry_price": entry_price,
        "quantity": quantity,
        "entry_date": entry_date,
        "entry_reason": entry_reason,
        "entry_note": entry_note,
        "model_id": model_id,
    }

    enforce_risk = existing is None or any(k in data for k in _RISK_KEYS)
    prices, err = _parse_risk_prices(merged, enforce=enforce_risk)
    if err:
        return None, err
    clean.update(prices)

    if status == "closed":
        try:
            exit_price = float(merged.get("exit_price"))
        except (TypeError, ValueError):
            return None, "退出价无效"
        if exit_price <= 0:
            return None, "退出价必须大于 0"

        exit_date = s(merged.get("exit_date", ""))
        if not _valid_date(exit_date):
            return None, "卖出日期无效 (格式 YYYY-MM-DD)"
        if exit_date < entry_date:
            return None, "卖出日期不能早于买入日期"
        fut = _reject_future_date(exit_date, "卖出日期")
        if fut:
            return None, fut
        bar_err = _validate_price_on_bar(symbol, exit_date, exit_price, "卖出")
        if bar_err:
            return None, bar_err

        exit_reason = s(merged.get("exit_reason", ""))
        if not exit_reason:
            return None, "缺少卖出理由"

        clean.update({
            "exit_price": exit_price,
            "exit_date": exit_date,
            "exit_reason": exit_reason,
            "exit_note": exit_note,
        })
    else:
        clean.update({
            "exit_price": None,
            "exit_date": None,
            "exit_reason": None,
            "exit_note": None,
        })

    return clean, None


def _get_legs(trade_id):
    """读取批次交易的全部腿 (按 date, time, id 排序)。"""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, side, price, quantity, date, time, reason, note "
            "FROM trade_legs WHERE trade_id=? ORDER BY date, time, id",
            (trade_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _batch_metrics(symbol, legs, fee_config):
    """批次交易滚动盈亏/费用 (移动加权平均, 卖出不改变均价)。

    legs 为腿列表 (含 id 可选)。返回:
      pnl / return_pct / fees / cost / total_buy_amt / total_sell_amt /
      net_qty / avg_cost / cost_price / first_date / last_date / fee_breakdown

    口径说明 (与 simple 路径的差异, 有意为之):
      - pnl        = 已实现毛盈亏 - 费用。
      - cost       = 累计买入金额 total_buy_amt (收益率分母)。
      - return_pct = pnl / cost * 100。注意: 未平仓批次也会返回非空 return_pct
        (已实现盈亏/累计买入), 而 simple open 恒为 None —— 字段语义不统一, 消费方需自辨。
      - avg_cost   = 移动加权均价, 随买入更新、卖出不变, 平仓后归 0。
      - cost_price = 买入均价 Σ(价×量)/Σ量, 不随卖出更新 —— 与 avg_cost 是两个概念,
        仅作"平均买入价"展示用, 勿当作移动加权成本。
    """
    legs = sorted(legs, key=lambda l: (l["date"], l["time"] or "", l.get("id", 0)))
    held = 0
    cost_total = 0.0
    realized = 0.0
    total_buy_amt = 0.0
    total_buy_qty = 0
    total_sell_amt = 0.0
    fees = 0.0
    bd = {"buy_comm": 0.0, "sell_comm": 0.0, "stamp": 0.0, "transfer": 0.0}
    first_date = None
    last_date = None

    for leg in legs:
        p = leg["price"]
        q = leg["quantity"]
        d = leg["date"]
        first_date = d if first_date is None else min(first_date, d)
        last_date = d if last_date is None else max(last_date, d)
        if leg["side"] == "buy":
            cost_total += p * q
            held += q
            total_buy_amt += p * q
            total_buy_qty += q
        else:
            if q > held:
                raise ValueError("卖出数量超过当前持仓")
            avg = cost_total / held if held else 0.0
            realized += (p - avg) * q
            held -= q
            cost_total = held * avg
            total_sell_amt += p * q
        if fee_config:
            f = _calc_side_fees(symbol, leg["side"], p, q, fee_config)
            fees += f["total"]
            if leg["side"] == "buy":
                bd["buy_comm"] += f["comm"]
            else:
                bd["sell_comm"] += f["comm"]
            bd["stamp"] += f["stamp"]
            bd["transfer"] += f["transfer"]

    fees = round(fees, 4)
    pnl = round(realized - fees, 2)
    return {
        "pnl": pnl,
        "return_pct": round(pnl / total_buy_amt * 100, 2) if total_buy_amt else None,
        "fees": fees,
        "cost": total_buy_amt,
        "total_buy_amt": total_buy_amt,
        "total_sell_amt": total_sell_amt,
        "total_buy_qty": total_buy_qty,
        "net_qty": held,
        "avg_cost": round(cost_total / held, 4) if held else 0.0,
        "cost_price": round(total_buy_amt / total_buy_qty, 4) if total_buy_qty else 0.0,  # 买入均价, 非移动加权
        "first_date": first_date,
        "last_date": last_date,
        "fee_breakdown": {
            "buy_comm": round(bd["buy_comm"], 4),
            "sell_comm": round(bd["sell_comm"], 4),
            "stamp": round(bd["stamp"], 4),
            "transfer": round(bd["transfer"], 4),
            "total": fees,
        },
    }


def _t_stats(legs):
    """做T统计: 同日既有买又有卖记一次做T (机械定义, 不要求先有底仓)。

    正T = 当日首腿为买; 反T = 当日首腿为卖。
    T盈亏 = (当日平均卖价 - 当日平均买价) × min(买量, 卖量); > 0 记为成功。
    返回 {count, positive, reverse, success, success_rate, pnl}。
    """
    days = {}
    for leg in legs:
        days.setdefault(leg["date"], []).append(leg)
    for ls in days.values():
        ls.sort(key=lambda l: (l["time"] or "", l.get("id", 0)))

    count = 0
    positive = 0
    reverse = 0
    success = 0
    pnl = 0.0
    for ls in days.values():
        buy_qty = sum(l["quantity"] for l in ls if l["side"] == "buy")
        sell_qty = sum(l["quantity"] for l in ls if l["side"] == "sell")
        if not buy_qty or not sell_qty:
            continue
        count += 1
        if ls[0]["side"] == "buy":
            positive += 1
        else:
            reverse += 1
        matched = min(buy_qty, sell_qty)
        buy_amt = sum(l["price"] * l["quantity"] for l in ls if l["side"] == "buy")
        sell_amt = sum(l["price"] * l["quantity"] for l in ls if l["side"] == "sell")
        avg_buy = buy_amt / buy_qty
        avg_sell = sell_amt / sell_qty
        t_pnl = (avg_sell - avg_buy) * matched
        pnl += t_pnl
        if t_pnl > 0:
            success += 1

    return {
        "count": count,
        "positive": positive,
        "reverse": reverse,
        "success": success,
        "success_rate": round(success / count * 100, 2) if count else None,
        "pnl": round(pnl, 2),
    }


def _trade_metrics(trade_row, fee_config=None, deduct_fees=True):
    """归一化单笔交易指标 (simple / batch / reverse_repo 的单一数据源)。

    返回 {pnl, return_pct, fees, cost, fee_breakdown, t_stats, legs}。
    legs 仅 batch 交易非 None; t_stats 仅 batch 交易非 None。
    逆回购: pnl=净收益(利息-佣金), fees=佣金, cost=本金, 始终可算 (到期判定由调用方按 exit_date)。
    """
    t = dict(trade_row)

    if t.get("type") == "reverse_repo":
        rr = {}
        if deduct_fees:
            cfg = fee_config or DEFAULT_FEES
            rr = cfg.get("reverse_repo_rates", {})
        tenor = int(classify_symbol(t["symbol"])[1] or 0)
        interest, commission = _repo_net_income(
            t["quantity"], t["entry_price"], int(t.get("repo_days") or tenor), rr.get(str(tenor), 0.0),
        )
        return {
            "pnl": round(interest - commission, 2),
            "return_pct": round((interest - commission) / t["quantity"] * 100, 4) if t["quantity"] else None,
            "fees": round(commission, 2),
            "cost": t["quantity"],
            "interest": round(interest, 2),
            "repo_days": int(t.get("repo_days") or tenor),
            "tenor": tenor,
            "cost_price": None,
            "fee_breakdown": None,
            "t_stats": None,
            "legs": None,
        }

    if t.get("type") == "batch":
        legs = _get_legs(t["id"])
        cfg = fee_config if (deduct_fees and fee_config) else None
        m = _batch_metrics(t["symbol"], legs, cfg)
        return {
            "pnl": m["pnl"],
            "return_pct": m["return_pct"],
            "fees": m["fees"],
            "cost": m["cost"],
            "cost_price": m["cost_price"],
            "total_buy_qty": m["total_buy_qty"],
            "fee_breakdown": m["fee_breakdown"],
            "t_stats": _t_stats(legs),
            "legs": legs,
        }

    # simple 路径: 与历史口径逐位一致
    pnl = None
    return_pct = None
    fees = 0.0
    detail = None
    cost = t["entry_price"] * t["quantity"]
    if t["status"] == "closed" and t["exit_price"] is not None:
        if fee_config and deduct_fees:
            detail = _calc_fee_detail(t, fee_config, "closed")
            fees = detail["total"]
        pnl = round((t["exit_price"] - t["entry_price"]) * t["quantity"] - fees, 2)
        if cost:
            return_pct = round(pnl / cost * 100, 2)
    elif t["status"] == "open" and fee_config and deduct_fees:
        # 持仓: 仅买入侧费用, 浮盈亏由前端 (现价-买入价)*数量 - fees 计算
        detail = _calc_fee_detail(t, fee_config, "open")
        fees = detail["total"]

    return {
        "pnl": pnl,
        "return_pct": return_pct,
        "fees": fees,
        "cost": cost,
        "cost_price": None,
        "fee_breakdown": detail,
        "t_stats": None,
        "legs": None,
    }


def _row_to_dict(row, fee_config=None):
    d = dict(row)
    m = _trade_metrics(d, fee_config=fee_config, deduct_fees=True)
    d["pnl"] = m["pnl"]
    d["return_pct"] = m["return_pct"]
    d["fees"] = m["fees"]
    d["fee_breakdown"] = m["fee_breakdown"]
    d["cost"] = m["cost"]
    if m.get("cost_price") is not None:
        d["cost_price"] = m["cost_price"]
    if d.get("type") == "reverse_repo":
        d["interest"] = m.get("interest")
        d["repo_days"] = m.get("repo_days")
        d["tenor"] = m.get("tenor")
    if m["legs"] is not None:
        d["legs"] = m["legs"]
        d["t_stats"] = m["t_stats"]
        d["total_buy_qty"] = m["total_buy_qty"]
    return d


def _attach_hold_days(records):
    """给交易 dict 补 hold_days (来自关联模型, 无模型为 None)。"""
    if not records:
        return records
    mids = {d.get("model_id") for d in records if d.get("model_id")}
    hold_map = {}
    if mids:
        conn = get_conn()
        try:
            q = "SELECT id, hold_days FROM models WHERE id IN (%s)" % (
                ",".join("?" * len(mids))
            )
            hold_map = {
                r["id"]: r["hold_days"] for r in conn.execute(q, list(mids))
            }
        finally:
            conn.close()
    for d in records:
        d["hold_days"] = hold_map.get(d["model_id"]) if d.get("model_id") else None
    return records


def get_trade(user_id, tid):
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM trades WHERE id=? AND user_id=?", (tid, user_id)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return _attach_hold_days([_row_to_dict(row)])[0]


def list_trades(user_id, filters=None, fee_config=None):
    """查询交易记录，返回 (records, total)。filters: status/symbol/q/from/to/model_id/limit/offset。

    model_id: 空不加条件; 'none'/'null' 匹配未关联模型; 正整数精确匹配。
    fee_config 非空时按费率扣佣计算单笔盈亏 (平仓=完整往返, 持仓=仅买入侧)。
    """
    filters = filters or {}
    sql = "SELECT * FROM trades WHERE user_id=?"
    args = [user_id]

    if filters.get("status"):
        st = filters["status"]
        # 逆回购 DB status 恒为 open, 到期由 exit_date 判定 (与前端徽章一致)
        if st == "open":
            today = _now().strftime("%Y-%m-%d")
            sql += (
                " AND ((type!='reverse_repo' AND status='open')"
                " OR (type='reverse_repo' AND exit_date>?))"
            )
            args.append(today)
        elif st == "closed":
            today = _now().strftime("%Y-%m-%d")
            sql += (
                " AND ((type!='reverse_repo' AND status='closed')"
                " OR (type='reverse_repo' AND exit_date IS NOT NULL"
                " AND exit_date<=?))"
            )
            args.append(today)
        else:
            sql += " AND status=?"
            args.append(st)
    if filters.get("symbol"):
        sql += " AND symbol=?"
        args.append(filters["symbol"].upper())
    if filters.get("q"):
        like = f"%{filters['q']}%"
        sql += " AND (symbol LIKE ? OR name LIKE ?)"
        args.extend([like, like])
    mid = filters.get("model_id")
    if mid in ("none", "null"):
        sql += " AND model_id IS NULL"
    elif mid not in (None, ""):
        try:
            sql += " AND model_id=?"
            args.append(int(mid))
        except (TypeError, ValueError):
            pass
    # 归属日期: 逆回购按买入日 (到期日可能超出查询区间导致看不到);
    # 其余用 COALESCE(exit_date, entry_date)
    date_col = "CASE WHEN type='reverse_repo' THEN entry_date "
    date_col += "ELSE COALESCE(exit_date, entry_date) END"
    if filters.get("from") or filters.get("to"):
        if filters.get("from"):
            sql += f" AND {date_col} >= ?"
            args.append(filters["from"])
        if filters.get("to"):
            sql += f" AND {date_col} <= ?"
            args.append(filters["to"])

    conn = get_conn()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) FROM ({sql})", args
        ).fetchone()[0]

        sql += f" ORDER BY {date_col} DESC, id DESC"
        limit = _clamp_int(filters.get("limit"), 1, 500, 50)
        offset = max(0, _clamp_int(filters.get("offset"), 0, 10 ** 9, 0))
        sql += " LIMIT ? OFFSET ?"
        args.extend([limit, offset])
        rows = conn.execute(sql, args).fetchall()
    finally:
        conn.close()
    recs = [_row_to_dict(r, fee_config) for r in rows]
    return _attach_hold_days(recs), total


def _clamp_int(v, lo, hi, default):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return lo if n < lo else (hi if n > hi else n)


def _insert_legs(conn, trade_id, legs):
    now = _now_iso()
    conn.executemany(
        "INSERT INTO trade_legs(trade_id, side, price, quantity, date, time, reason, note, "
        "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        [
            (
                trade_id, leg["side"], leg["price"], leg["quantity"], leg["date"],
                leg.get("time"), leg.get("reason"), leg.get("note"), now, now,
            )
            for leg in legs
        ],
    )


def create_trade(user_id, data):
    clean, err = _clean(data)
    if err:
        raise ValueError(err)
    now = _now_iso()
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO trades(user_id, symbol, name, status, entry_price, exit_price, "
            "quantity, entry_date, exit_date, entry_reason, entry_note, exit_reason, "
            "exit_note, model_id, type, take_profit, stop_loss, breakeven, repo_days, "
            "created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                user_id, clean["symbol"], clean["name"], clean["status"],
                clean["entry_price"], clean["exit_price"], clean["quantity"],
                clean["entry_date"], clean["exit_date"], clean["entry_reason"],
                clean["entry_note"], clean["exit_reason"], clean["exit_note"],
                clean["model_id"], clean.get("type", "simple"),
                clean.get("take_profit"), clean.get("stop_loss"), clean.get("breakeven"),
                clean.get("repo_days"),
                now, now,
            ),
        )
        tid = cur.lastrowid
        if clean.get("type") == "batch":
            _insert_legs(conn, tid, clean["legs"])
        conn.commit()
    finally:
        conn.close()
    return get_trade(user_id, tid)


def update_trade(user_id, tid, data):
    existing = get_trade(user_id, tid)
    if not existing:
        return None
    clean, err = _clean(data, existing)
    if err:
        raise ValueError(err)
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE trades SET symbol=?, name=?, status=?, entry_price=?, exit_price=?, "
            "quantity=?, entry_date=?, exit_date=?, entry_reason=?, entry_note=?, "
            "exit_reason=?, exit_note=?, model_id=?, type=?, "
            "take_profit=?, stop_loss=?, breakeven=?, repo_days=?, updated_at=? "
            "WHERE id=? AND user_id=?",
            (
                clean["symbol"], clean["name"], clean["status"], clean["entry_price"],
                clean["exit_price"], clean["quantity"], clean["entry_date"],
                clean["exit_date"], clean["entry_reason"], clean["entry_note"],
                clean["exit_reason"], clean["exit_note"], clean["model_id"],
                clean.get("type", "simple"),
                clean.get("take_profit"), clean.get("stop_loss"), clean.get("breakeven"),
                clean.get("repo_days"),
                _now_iso(), tid, user_id,
            ),
        )
        conn.execute("DELETE FROM trade_legs WHERE trade_id=?", (tid,))
        if clean.get("type") == "batch":
            _insert_legs(conn, tid, clean["legs"])
        conn.commit()
    finally:
        conn.close()
    return get_trade(user_id, tid)


def delete_trade(user_id, tid):
    conn = get_conn()
    try:
        cur = conn.execute(
            "DELETE FROM trades WHERE id=? AND user_id=?", (tid, user_id)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ── 统计 ──
def _bucket_label(d, granularity):
    y, m, day = d.split("-")
    if granularity == "year":
        return y
    if granularity == "month":
        return f"{y}-{m}"
    if granularity == "week":
        iso = date(int(y), int(m), int(day)).isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    return y


def _win_rate(wins, losses):
    denom = wins + losses
    return round(wins / denom * 100, 2) if denom else None


def compute_stats(user_id, start=None, end=None, deduct_fees=False, fee_config=None):
    """统计 closed 交易 (按 exit_date 过滤 [start,end]) 的盈亏/胜率/分桶序列/按股票汇总。

    deduct_fees=True 且传入 fee_config 时, 每笔 pnl 扣除交易费用 (佣金/印花税/过户费)。
    """
    conn = get_conn()
    try:
        open_count = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE user_id=? AND status='open' "
            "AND type != 'reverse_repo'",
            (user_id,),
        ).fetchone()[0]
        open_rows = conn.execute(
            "SELECT symbol, name, entry_price, quantity, take_profit, stop_loss, "
            "breakeven, model_id "
            "FROM trades WHERE user_id=? AND status='open' AND type != 'reverse_repo' "
            "ORDER BY entry_date",
            (user_id,),
        ).fetchall()
        rows = conn.execute(
            "SELECT * FROM trades WHERE user_id=? AND status='closed' ORDER BY exit_date",
            (user_id,),
        ).fetchall()
        repo_rows = conn.execute(
            "SELECT * FROM trades WHERE user_id=? AND type='reverse_repo' ORDER BY entry_date",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()

    open_positions = [
        {
            "symbol": r["symbol"],
            "name": r["name"],
            "entry_price": r["entry_price"],
            "quantity": r["quantity"],
            "take_profit": r["take_profit"],
            "stop_loss": r["stop_loss"],
            "breakeven": r["breakeven"],
            "model_id": r["model_id"],
        }
        for r in open_rows
    ]

    trades = []
    for r in rows:
        t = dict(r)
        if start and t["exit_date"] < start:
            continue
        if end and t["exit_date"] > end:
            continue
        m = _trade_metrics(t, fee_config=fee_config, deduct_fees=deduct_fees)
        t["fees"] = m["fees"]
        t["pnl"] = m["pnl"]
        t["cost"] = m["cost"]
        t["return_pct"] = m["return_pct"] if m["return_pct"] is not None else 0.0
        t["t_stats"] = m["t_stats"]
        trades.append(t)

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    break_even = [t for t in trades if t["pnl"] == 0]

    # 逆回购: 按到期日 (exit_date) 落在 [start,end] 的净收益合计, 单独汇总不并总盈亏
    repo_income = 0.0
    repo_count = 0
    repo_list = []
    for r in repo_rows:
        t = dict(r)
        if start and t["exit_date"] < start:
            continue
        if end and t["exit_date"] > end:
            continue
        m = _trade_metrics(t, fee_config=fee_config, deduct_fees=deduct_fees)
        repo_income += m["pnl"]
        repo_count += 1
        repo_list.append({
            "symbol": t["symbol"], "name": t["name"], "entry_date": t["entry_date"],
            "exit_date": t["exit_date"], "entry_price": t["entry_price"],
            "quantity": t["quantity"], "net": m["pnl"],
        })

    total_pnl = sum(t["pnl"] for t in trades)
    total_cost = sum(t["cost"] for t in trades)
    total_return_pct = (total_pnl / total_cost * 100) if total_cost else 0.0

    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    avg_win = (gross_win / len(wins)) if wins else None
    avg_loss = (-gross_loss / len(losses)) if losses else None
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else None

    max_win = max(wins, key=lambda t: t["pnl"]) if wins else None
    max_loss = min(losses, key=lambda t: t["pnl"]) if losses else None

    holding_days = []
    for t in trades:
        try:
            d0 = date.fromisoformat(t["entry_date"])
            d1 = date.fromisoformat(t["exit_date"])
            holding_days.append((d1 - d0).days)
        except (ValueError, TypeError):
            pass
    avg_holding_days = (
        round(sum(holding_days) / len(holding_days), 1) if holding_days else None
    )

    t_count = t_positive = t_reverse = t_success = 0
    t_pnl = 0.0
    for t in trades:
        ts = t.get("t_stats")
        if not ts:
            continue
        t_count += ts["count"]
        t_positive += ts["positive"]
        t_reverse += ts["reverse"]
        t_success += ts["success"]
        t_pnl += ts["pnl"]
    summary_t_stats = {
        "count": t_count,
        "positive": t_positive,
        "reverse": t_reverse,
        "success": t_success,
        "success_rate": round(t_success / t_count * 100, 2) if t_count else None,
        "pnl": round(t_pnl, 2),
    }

    summary = {
        "closed_count": len(trades),
        "open_count": open_count,
        "total_pnl": round(total_pnl, 2),
        "total_return_pct": round(total_return_pct, 2),
        "win_count": len(wins),
        "loss_count": len(losses),
        "break_even_count": len(break_even),
        "win_rate": _win_rate(len(wins), len(losses)),
        "avg_win": round(avg_win, 2) if avg_win is not None else None,
        "avg_loss": round(avg_loss, 2) if avg_loss is not None else None,
        "profit_factor": round(profit_factor, 2) if profit_factor is not None else None,
        "avg_holding_days": avg_holding_days,
        "total_fees": round(sum(t["fees"] for t in trades), 2),
        "deduct_fees": deduct_fees,
        "t_stats": summary_t_stats,
        "repo_income": round(repo_income, 2),
        "repo_count": repo_count,
        "repo_list": repo_list,
        "max_win": (
            {
                "symbol": max_win["symbol"], "name": max_win["name"],
                "pnl": round(max_win["pnl"], 2),
                "return_pct": round(max_win["return_pct"], 2),
            } if max_win else None
        ),
        "max_loss": (
            {
                "symbol": max_loss["symbol"], "name": max_loss["name"],
                "pnl": round(max_loss["pnl"], 2),
                "return_pct": round(max_loss["return_pct"], 2),
            } if max_loss else None
        ),
    }

    series = {}
    for granularity in ("week", "month", "year"):
        buckets = {}
        for t in trades:
            label = _bucket_label(t["exit_date"], granularity)
            b = buckets.setdefault(label, {"pnl": 0.0, "count": 0, "wins": 0, "losses": 0})
            b["pnl"] += t["pnl"]
            b["count"] += 1
            if t["pnl"] > 0:
                b["wins"] += 1
            elif t["pnl"] < 0:
                b["losses"] += 1
        series[granularity] = [
            {
                "label": label,
                "pnl": round(b["pnl"], 2),
                "count": b["count"],
                "win_rate": _win_rate(b["wins"], b["losses"]),
            }
            for label, b in sorted(buckets.items())
        ]

    by_symbol = {}
    for t in trades:
        s = by_symbol.setdefault(
            t["symbol"], {"symbol": t["symbol"], "name": t["name"],
                           "pnl": 0.0, "count": 0, "wins": 0, "losses": 0}
        )
        s["pnl"] += t["pnl"]
        s["count"] += 1
        if t["pnl"] > 0:
            s["wins"] += 1
        elif t["pnl"] < 0:
            s["losses"] += 1
    by_symbol = [
        {
            "symbol": s["symbol"], "name": s["name"], "pnl": round(s["pnl"], 2),
            "count": s["count"], "win_rate": _win_rate(s["wins"], s["losses"]),
        }
        for s in sorted(by_symbol.values(), key=lambda x: -x["pnl"])
    ]

    # 按模型汇总 (model_id=None 归入「无」; 停用模型历史仍计入并标注)
    model_names = {}
    conn = get_conn()
    try:
        for r in conn.execute("SELECT id, name, active FROM models"):
            model_names[r["id"]] = (r["name"], bool(r["active"]))
    finally:
        conn.close()

    by_model = {}
    for t in trades:
        mid = t.get("model_id")
        m = by_model.setdefault(
            mid, {"model_id": mid, "pnl": 0.0, "count": 0, "wins": 0, "losses": 0}
        )
        m["pnl"] += t["pnl"]
        m["count"] += 1
        if t["pnl"] > 0:
            m["wins"] += 1
        elif t["pnl"] < 0:
            m["losses"] += 1

    def _model_label(mid):
        if mid is None:
            return "无"
        name, active = model_names.get(mid, ("未知模型", True))
        return name if active else f"{name}（已删除）"

    by_model = [
        {
            "model_id": m["model_id"],
            "name": _model_label(m["model_id"]),
            "active": m["model_id"] is None or model_names.get(m["model_id"], (None, True))[1],
            "pnl": round(m["pnl"], 2),
            "count": m["count"],
            "win_rate": _win_rate(m["wins"], m["losses"]),
        }
        for m in sorted(by_model.values(), key=lambda x: -x["pnl"])
    ]

    return {
        "summary": summary,
        "series": series,
        "by_symbol": by_symbol,
        "by_model": by_model,
        "open_positions": open_positions,
    }


# ── 持仓监控 ──
def list_monitored_positions():
    """授权用户 (管理员恒开 / monitor_enabled=1) 的 open 持仓, 且至少填了一个风控价。"""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT t.id, t.user_id, t.symbol, t.name, t.status, t.entry_price, "
            "t.quantity, t.take_profit, t.stop_loss, t.breakeven, "
            "u.username, u.is_admin, u.monitor_enabled "
            "FROM trades t JOIN users u ON u.id = t.user_id "
            "WHERE t.status='open' "
            "AND t.type != 'reverse_repo' "
            "AND (u.is_admin=1 OR u.monitor_enabled=1) "
            "AND (t.take_profit IS NOT NULL OR t.stop_loss IS NOT NULL "
            "     OR t.breakeven IS NOT NULL) "
            "ORDER BY t.user_id, t.symbol"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_hold_expire_positions():
    """授权用户的 open 持仓, 且关联模型填了 hold_days。不要求风控价。

    hold_anchor_date: 单笔用买入日; 批次用最晚一笔买入腿日期。
    """
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT t.id, t.user_id, t.symbol, t.name, t.status, t.entry_date, "
            "t.type, t.entry_price, t.quantity, t.model_id, "
            "m.name AS model_name, m.hold_days, u.username, "
            "CASE WHEN t.type='batch' THEN ("
            "  SELECT MAX(l.date) FROM trade_legs l "
            "  WHERE l.trade_id=t.id AND l.side='buy'"
            ") ELSE t.entry_date END AS hold_anchor_date "
            "FROM trades t "
            "JOIN users u ON u.id = t.user_id "
            "JOIN models m ON m.id = t.model_id "
            "WHERE t.status='open' "
            "AND (u.is_admin=1 OR u.monitor_enabled=1) "
            "AND m.hold_days IS NOT NULL AND m.hold_days > 0 "
            "ORDER BY t.user_id, t.symbol"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def insert_monitor_alert(user_id, trade_id, symbol, alert_type, trade_date,
                         price=None, detail=None):
    """写入一条监控告警。返回 row id。"""
    now = _now_iso()
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO monitor_alerts(user_id, trade_id, symbol, alert_type, "
            "trade_date, fired_at, price, detail) VALUES(?,?,?,?,?,?,?,?)",
            (user_id, trade_id, symbol, alert_type, trade_date, now, price, detail),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def last_monitor_alert(user_id, symbol, alert_type, trade_id=None):
    """最近一条同用户同标的同类型告警, 无则 None。trade_id 非空时再按持仓行过滤。"""
    conn = get_conn()
    try:
        sql = (
            "SELECT id, trade_id, trade_date, fired_at, price, detail "
            "FROM monitor_alerts "
            "WHERE user_id=? AND symbol=? AND alert_type=?"
        )
        args = [user_id, symbol, alert_type]
        if trade_id is not None:
            sql += " AND trade_id=?"
            args.append(trade_id)
        sql += " ORDER BY fired_at DESC, id DESC LIMIT 1"
        row = conn.execute(sql, args).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_monitor_alerts(user_id, limit=20):
    """当前用户最近 limit 条告警 (新→旧)。"""
    limit = _clamp_int(limit, 1, 200, 20)
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, trade_id, symbol, alert_type, trade_date, fired_at, price, detail "
            "FROM monitor_alerts WHERE user_id=? "
            "ORDER BY fired_at DESC, id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

