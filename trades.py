"""
交易记录统计模块
================
SQLite 持久化的多账户交易日志：注册/登录、买卖记录增删改查、按周/月/年统计盈亏与胜率。

仅依赖 Python 标准库 (sqlite3 / hashlib / secrets)，与 visual 项目"零额外依赖"保持一致。

数据表:
  - users    用户账户 (PBKDF2-SHA256 口令哈希)
  - sessions 会话令牌 (30 天过期)
  - models   量化模型 (全局共享, 软删除)
  - trades   交易记录 (open 持仓 / closed 平仓, 可选关联 model_id)

数据库文件默认位于 `visual/data/trades.db`。
"""

import hashlib
import hmac
import secrets
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

# ── 路径与常量 ──
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = SCRIPT_DIR / "data" / "trades.db"

_db_path = DEFAULT_DB_PATH

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
    "动力红转",
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
    "动力绿转",
    "动力蓝转",
    "其他",
]

# ── 量化模型种子 (对齐回测管线 A–E) ──
# 策略描述默认留空，不暴露内部策略细节
SEED_MODELS = [
    ("A 60分钟超短", ""),
    ("B 日线波段", ""),
    ("C 日线波段·阳包阴", ""),
    ("D 动力管线", ""),
    ("E K线反转管线", ""),
]

# ── 数据库表结构 ──
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    salt          TEXT NOT NULL,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
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
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (model_id) REFERENCES models(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_user_exit   ON trades(user_id, exit_date);
CREATE INDEX IF NOT EXISTS idx_trades_user_symbol ON trades(user_id, symbol);
"""


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


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
        # 种子模型 A–E (仅当 models 表为空时插入, 含停用行也计入)
        n = conn.execute("SELECT COUNT(*) FROM models").fetchone()[0]
        if n == 0:
            conn.executemany(
                "INSERT INTO models (name, description, created_at) VALUES (?,?,?)",
                [(name, desc, _now_iso()) for name, desc in SEED_MODELS],
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
    now = datetime.now()
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
    """根据令牌返回当前用户 dict {id, username, is_admin}，无效/过期返回 None。"""
    if not token:
        return None
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT s.token, s.expires_at, u.id, u.username, u.is_admin "
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
    if expires < datetime.now():
        delete_session(token)
        return None
    return {"id": row["id"], "username": row["username"], "is_admin": bool(row["is_admin"])}


def delete_session(token):
    if not token:
        return
    conn = get_conn()
    try:
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        conn.commit()
    finally:
        conn.close()


# ── 用户管理 (仅 admin) ──
def count_admins():
    """返回管理员数量，供启动引导判断。"""
    conn = get_conn()
    try:
        row = conn.execute("SELECT COUNT(*) c FROM users WHERE is_admin=1").fetchone()
        return row["c"]
    finally:
        conn.close()


def list_users():
    """返回所有用户 {id, username, is_admin, created_at}（不含口令哈希/盐）。"""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, username, is_admin, created_at FROM users ORDER BY id"
        ).fetchall()
        return [
            {
                "id": r["id"],
                "username": r["username"],
                "is_admin": bool(r["is_admin"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]
    finally:
        conn.close()


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
def list_models(active_only=True):
    """返回模型列表 {id, name, description, active, created_at, deleted_at}。

    active_only=True 只返回启用中的 (供交易下拉); False 返回全部 (供管理员列表)。
    """
    sql = "SELECT id, name, description, active, created_at, deleted_at FROM models"
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
                "active": bool(r["active"]),
                "created_at": r["created_at"],
                "deleted_at": r["deleted_at"],
            }
            for r in rows
        ]
    finally:
        conn.close()


def create_model(name, description):
    """新增模型并返回 model_id。名称非空/启用中重名抛 ValueError。"""
    name = (name or "").strip()
    if not name:
        raise ValueError("模型名称不能为空")
    if len(name) > 64:
        raise ValueError("模型名称过长")
    description = (description or "").strip()
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO models(name, description, active, created_at) VALUES(?,?,1,?)",
            (name, description, _now_iso()),
        )
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        raise ValueError("模型名已存在")
    finally:
        conn.close()


def update_model(model_id, name, description):
    """更新模型名称/描述。模型不存在返回 False；启用中重名抛 ValueError。"""
    name = (name or "").strip()
    if not name:
        raise ValueError("模型名称不能为空")
    if len(name) > 64:
        raise ValueError("模型名称过长")
    description = (description or "").strip()
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE models SET name=?, description=? WHERE id=?",
            (name, description, model_id),
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


def _clean(data, existing=None):
    """合并并规范化字段。返回 (clean_dict, error_msg)。"""
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
        "entry_price": entry_price,
        "quantity": quantity,
        "entry_date": entry_date,
        "entry_reason": entry_reason,
        "entry_note": entry_note,
        "model_id": model_id,
    }

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


def _row_to_dict(row):
    d = dict(row)
    d["pnl"] = None
    d["return_pct"] = None
    if d["status"] == "closed" and d["exit_price"] is not None:
        d["pnl"] = round((d["exit_price"] - d["entry_price"]) * d["quantity"], 2)
        if d["entry_price"]:
            d["return_pct"] = round((d["exit_price"] / d["entry_price"] - 1) * 100, 2)
    return d


def get_trade(user_id, tid):
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM trades WHERE id=? AND user_id=?", (tid, user_id)
        ).fetchone()
    finally:
        conn.close()
    return _row_to_dict(row) if row else None


def list_trades(user_id, filters=None):
    """查询交易记录，返回 (records, total)。filters: status/symbol/q/from/to/limit/offset。"""
    filters = filters or {}
    sql = "SELECT * FROM trades WHERE user_id=?"
    args = [user_id]

    if filters.get("status"):
        sql += " AND status=?"
        args.append(filters["status"])
    if filters.get("symbol"):
        sql += " AND symbol=?"
        args.append(filters["symbol"].upper())
    if filters.get("q"):
        like = f"%{filters['q']}%"
        sql += " AND (symbol LIKE ? OR name LIKE ?)"
        args.extend([like, like])
    if filters.get("from") or filters.get("to"):
        # 用 COALESCE(exit_date, entry_date) 作为记录归属日期
        date_col = "COALESCE(exit_date, entry_date)"
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

        sql += " ORDER BY COALESCE(exit_date, entry_date) DESC, id DESC"
        limit = _clamp_int(filters.get("limit"), 1, 500, 50)
        offset = max(0, _clamp_int(filters.get("offset"), 0, 10 ** 9, 0))
        sql += " LIMIT ? OFFSET ?"
        args.extend([limit, offset])
        rows = conn.execute(sql, args).fetchall()
    finally:
        conn.close()
    return [_row_to_dict(r) for r in rows], total


def _clamp_int(v, lo, hi, default):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return lo if n < lo else (hi if n > hi else n)


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
            "exit_note, model_id, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                user_id, clean["symbol"], clean["name"], clean["status"],
                clean["entry_price"], clean["exit_price"], clean["quantity"],
                clean["entry_date"], clean["exit_date"], clean["entry_reason"],
                clean["entry_note"], clean["exit_reason"], clean["exit_note"],
                clean["model_id"], now, now,
            ),
        )
        conn.commit()
        tid = cur.lastrowid
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
            "exit_reason=?, exit_note=?, model_id=?, updated_at=? WHERE id=? AND user_id=?",
            (
                clean["symbol"], clean["name"], clean["status"], clean["entry_price"],
                clean["exit_price"], clean["quantity"], clean["entry_date"],
                clean["exit_date"], clean["entry_reason"], clean["entry_note"],
                clean["exit_reason"], clean["exit_note"], clean["model_id"],
                _now_iso(), tid, user_id,
            ),
        )
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


def compute_stats(user_id, start=None, end=None):
    """统计 closed 交易 (按 exit_date 过滤 [start,end]) 的盈亏/胜率/分桶序列/按股票汇总。"""
    conn = get_conn()
    try:
        open_count = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE user_id=? AND status='open'",
            (user_id,),
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT * FROM trades WHERE user_id=? AND status='closed' ORDER BY exit_date",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()

    trades = []
    for r in rows:
        t = dict(r)
        if start and t["exit_date"] < start:
            continue
        if end and t["exit_date"] > end:
            continue
        t["pnl"] = (t["exit_price"] - t["entry_price"]) * t["quantity"]
        t["return_pct"] = (
            (t["exit_price"] / t["entry_price"] - 1) * 100 if t["entry_price"] else 0.0
        )
        trades.append(t)

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    break_even = [t for t in trades if t["pnl"] == 0]

    total_pnl = sum(t["pnl"] for t in trades)
    total_cost = sum(t["entry_price"] * t["quantity"] for t in trades)
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

    return {"summary": summary, "series": series, "by_symbol": by_symbol, "by_model": by_model}
