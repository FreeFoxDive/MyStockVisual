# -*- coding: utf-8 -*-
"""visual/trades.py 后端模块的单元测试。

覆盖范围（纯标准库，无网络依赖，可离线运行）：
- 建库 / 迁移 / 种子模型（A–E）
- 口令哈希与校验（PBKDF2-SHA256）
- 鉴权：create_user / login / 会话过期与撤销
- 用户管理：list / count / delete / reset_password
- 搜索/浏览历史：迁移 / 归一化 / 去重截断 / 账号隔离
- 模型 CRUD：软删除 / 恢复 / 名称唯一性 / 停用后同名复用
- 交易 CRUD：字段校验 / 归一化 / 账户隔离 / 过滤 / 分页
- 统计：summary / series(周/月/年) / by_symbol / by_model / open_positions / 日期区间

运行：
    venv/Scripts/python.exe -u visual/test/test_trades.py
"""

import os
import sys
import tempfile
import unittest
from datetime import date as _date

# visual/ 不是包（无 __init__.py），把其目录加入 sys.path 后直接 import trades
_VISUAL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _VISUAL_DIR not in sys.path:
    sys.path.insert(0, _VISUAL_DIR)

import trades  # noqa: E402


def _week_label(d):
    """把 'YYYY-MM-DD' 转成 trades.compute_stats 里的 ISO 周标签, 如 '2026-W33'。"""
    iso = _date.fromisoformat(d).isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


# 测试期间把 PBKDF2 迭代次数调低, 口令哈希测试从 ~0.1s/次 降到毫秒级
trades.PBKDF2_ITERATIONS = 1000


class TradesTestCase(unittest.TestCase):
    """每个测试用独立的临时 DB 文件, 保证互相隔离。

    注意: 不能用 ':memory:' —— trades.get_conn() 每次开新连接, 内存库会各自独立。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = os.path.join(self._tmp.name, "test_trades.db")
        trades.init_db(self.db_path)
        trades.PBKDF2_ITERATIONS = 1000

    def tearDown(self):
        self._tmp.cleanup()

    # ---- 辅助构造 ----
    def _make_user(self, username="alice", password="secret123", is_admin=False):
        return trades.create_user(username, password, is_admin=is_admin)

    @staticmethod
    def _closed(symbol="000001.SZ", name="平安银行", entry=10.0, exit_=13.0,
                qty=100, entry_date="2026-01-05", exit_date="2026-01-12",
                reason="突破买入", exit_reason="止盈(达到目标价)", model_id=None,
                **extra):
        d = {
            "symbol": symbol, "name": name, "status": "closed",
            "entry_price": entry, "exit_price": exit_, "quantity": qty,
            "entry_date": entry_date, "exit_date": exit_date,
            "entry_reason": reason, "exit_reason": exit_reason,
            "model_id": model_id,
        }
        d.update(extra)
        return d

    @staticmethod
    def _open(symbol="300750.SZ", name="宁德时代", entry=200.0, qty=100,
              entry_date="2026-08-01", reason="动力绿转", model_id=None,
              **extra):
        d = {
            "symbol": symbol, "name": name, "status": "open",
            "entry_price": entry, "quantity": qty,
            "entry_date": entry_date, "entry_reason": reason,
            "model_id": model_id,
        }
        d.update(extra)
        return d

    def _assert_value_error(self, fn, *args, sub=None, **kwargs):
        with self.assertRaises(ValueError) as cm:
            fn(*args, **kwargs)
        if sub is not None:
            self.assertIn(sub, str(cm.exception))


# ---------------------------------------------------------------------------
# 1. 建库 / 种子 / 迁移
# ---------------------------------------------------------------------------
class TestInitDb(TradesTestCase):
    def test_seed_models_created_in_order(self):
        models = trades.list_models(active_only=False)
        self.assertEqual([m["name"] for m in models],
                         ["A 60分钟超短", "B 日线波段", "C 日线波段·阳包阴",
                          "D 动力管线", "E K线反转管线"])
        self.assertEqual([m["id"] for m in models], [1, 2, 3, 4, 5])
        self.assertTrue(all(m["active"] == 1 for m in models))
        self.assertEqual([m["hold_days"] for m in models], [3, 20, 10, 7, None])

    def test_init_db_idempotent_no_reseed(self):
        trades.create_model("自定义模型", "")
        trades.init_db(self.db_path)  # 再次 init 不应重复播种
        names = [m["name"] for m in trades.list_models(active_only=False)]
        self.assertEqual(names.count("A 60分钟超短"), 1)
        self.assertIn("自定义模型", names)

    def test_migration_adds_model_id_column(self):
        # 新建库自带 model_id; 模拟旧库: 先手工删列无法直接测, 此处验证列存在
        with trades.get_conn() as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(trades)")}
        self.assertIn("model_id", cols)

    def test_migration_adds_hold_days_and_backfills_abcd(self):
        import sqlite3
        old_db = os.path.join(self._tmp.name, "old_models.db")
        conn = sqlite3.connect(old_db)
        conn.executescript("""
            CREATE TABLE models (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                deleted_at TEXT
            );
        """)
        now = "2026-01-01T00:00:00"
        conn.executemany(
            "INSERT INTO models(name, description, created_at) VALUES(?,?,?)",
            [
                ("A 60分钟超短", "", now),
                ("B 日线波段", "", now),
                ("C 日线波段·阳包阴", "", now),
                ("D 动力管线", "", now),
                ("E K线反转管线", "", now),
                ("自定义模型", "", now),
            ],
        )
        conn.commit()
        conn.close()
        trades.init_db(old_db)
        hold = {m["name"]: m["hold_days"] for m in trades.list_models(active_only=False)}
        self.assertEqual(hold["A 60分钟超短"], 3)
        self.assertEqual(hold["B 日线波段"], 20)
        self.assertEqual(hold["C 日线波段·阳包阴"], 10)
        self.assertEqual(hold["D 动力管线"], 7)
        self.assertIsNone(hold["E K线反转管线"])
        self.assertIsNone(hold["自定义模型"])


# ---------------------------------------------------------------------------
# 2. 口令哈希与校验
# ---------------------------------------------------------------------------
class TestPassword(TradesTestCase):
    def test_hash_and_verify_roundtrip(self):
        h, salt = trades.hash_password("secret123")
        self.assertTrue(trades.verify_password("secret123", salt, h))
        self.assertFalse(trades.verify_password("wrong", salt, h))

    def test_hash_random_salt(self):
        h1, s1 = trades.hash_password("same")
        h2, s2 = trades.hash_password("same")
        self.assertNotEqual(s1, s2)
        self.assertNotEqual(h1, h2)

    def test_verify_wrong_salt(self):
        h, _ = trades.hash_password("secret123")
        _, other_salt = trades.hash_password("whatever")
        self.assertFalse(trades.verify_password("secret123", other_salt, h))


# ---------------------------------------------------------------------------
# 3. 鉴权
# ---------------------------------------------------------------------------
class TestAuth(TradesTestCase):
    def test_create_user_validation(self):
        self._assert_value_error(trades.create_user, "a", "secret123",
                                 sub="用户名长度需 2~32 个字符")
        self._assert_value_error(trades.create_user, "x" * 33, "secret123",
                                 sub="用户名长度需 2~32 个字符")
        self._assert_value_error(trades.create_user, "bob", "123",
                                 sub="密码至少 6 位")

    def test_create_user_duplicate(self):
        self._make_user("alice", "secret123")
        self._assert_value_error(trades.create_user, "alice", "secret123",
                                 sub="用户名已存在")

    def test_username_stripped(self):
        uid = trades.create_user("  alice  ", "secret123")
        self.assertEqual(trades.list_users()[0]["username"], "alice")
        self.assertEqual(trades.login("alice", "secret123")[0] is not None, True)
        self.assertEqual(uid > 0, True)

    def test_login_success_and_failure(self):
        self._make_user("alice", "secret123")
        ok = trades.login("alice", "secret123")
        self.assertIsNotNone(ok)
        token, expires = ok
        self.assertTrue(token)
        self.assertTrue(expires)
        self.assertIsNone(trades.login("alice", "wrong"))
        self.assertIsNone(trades.login("nobody", "secret123"))

    def test_session_roundtrip(self):
        uid = self._make_user("alice", "secret123", is_admin=True)
        token, _ = trades.login("alice", "secret123")
        sess = trades.get_session(token)
        self.assertEqual(sess["id"], uid)
        self.assertEqual(sess["username"], "alice")
        self.assertIs(sess["is_admin"], True)

    def test_get_session_invalid(self):
        self.assertIsNone(trades.get_session("deadbeef"))

    def test_session_revoked_by_logout(self):
        self._make_user("alice", "secret123")
        token, _ = trades.login("alice", "secret123")
        self.assertIsNotNone(trades.get_session(token))
        trades.delete_session(token)
        self.assertIsNone(trades.get_session(token))

    def test_session_expiry(self):
        self._make_user("alice", "secret123")
        token, _ = trades.login("alice", "secret123")
        # 手动把过期时间改到过去, 模拟过期
        with trades.get_conn() as conn:
            conn.execute("UPDATE sessions SET expires_at='2000-01-01T00:00:00' WHERE token=?",
                         (token,))
        self.assertIsNone(trades.get_session(token))


# ---------------------------------------------------------------------------
# 4. 用户管理
# ---------------------------------------------------------------------------
class TestUserAdmin(TradesTestCase):
    def test_count_admins(self):
        self.assertEqual(trades.count_admins(), 0)
        self._make_user("admin", "secret123", is_admin=True)
        self.assertEqual(trades.count_admins(), 1)

    def test_list_users_shape(self):
        uid = self._make_user("alice", "secret123", is_admin=True)
        users = trades.list_users()
        self.assertEqual(len(users), 1)
        u = users[0]
        self.assertEqual(u["id"], uid)
        self.assertEqual(u["username"], "alice")
        self.assertIs(u["is_admin"], True)
        self.assertIn("created_at", u)

    def test_delete_admin_rejected(self):
        uid = self._make_user("admin", "secret123", is_admin=True)
        self._assert_value_error(trades.delete_user, uid, sub="不能删除管理员")

    def test_delete_user_ok_and_not_found(self):
        uid = self._make_user("alice", "secret123")
        self.assertTrue(trades.delete_user(uid))
        self.assertFalse(trades.delete_user(uid))  # 已删除 → 不存在

    def test_delete_user_cascades_trades(self):
        uid = self._make_user("alice", "secret123")
        trades.create_trade(uid, self._closed())
        trades.delete_user(uid)
        records, total = trades.list_trades(uid, {})
        self.assertEqual(total, 0)

    def test_reset_password(self):
        uid = self._make_user("alice", "oldpass123")
        self.assertTrue(trades.reset_password(uid, "newpass123"))
        self.assertIsNotNone(trades.login("alice", "newpass123"))
        self.assertIsNone(trades.login("alice", "oldpass123"))

    def test_reset_password_short_and_not_found(self):
        uid = self._make_user("alice", "oldpass123")
        self._assert_value_error(trades.reset_password, uid, "123",
                                 sub="密码至少 6 位")
        self.assertFalse(trades.reset_password(999999, "newpass123"))

    def test_reset_password_revokes_sessions(self):
        self._make_user("alice", "oldpass123")
        token, _ = trades.login("alice", "oldpass123")
        self.assertIsNotNone(trades.get_session(token))
        uid = trades.list_users()[0]["id"]
        trades.reset_password(uid, "newpass123")
        self.assertIsNone(trades.get_session(token))


# ---------------------------------------------------------------------------
# 5. 模型 CRUD
# ---------------------------------------------------------------------------
class TestModelCRUD(TradesTestCase):
    def test_create_model(self):
        mid = trades.create_model("F 突破模型", "描述")
        self.assertGreater(mid, 0)
        models = {m["id"]: m for m in trades.list_models(active_only=False)}
        self.assertEqual(models[mid]["name"], "F 突破模型")
        self.assertEqual(models[mid]["description"], "描述")
        self.assertIsNone(models[mid]["hold_days"])

    def test_create_model_hold_days(self):
        mid = trades.create_model("F 突破模型", "", 15)
        m = next(x for x in trades.list_models(active_only=False) if x["id"] == mid)
        self.assertEqual(m["hold_days"], 15)
        mid2 = trades.create_model("G 空持仓", "", "")
        m2 = next(x for x in trades.list_models(active_only=False) if x["id"] == mid2)
        self.assertIsNone(m2["hold_days"])

    def test_create_model_hold_days_validation(self):
        self._assert_value_error(trades.create_model, "F", "", 0,
                                 sub="推荐持仓交易日须为")
        self._assert_value_error(trades.create_model, "F", "", 251,
                                 sub="推荐持仓交易日须为")
        self._assert_value_error(trades.create_model, "F", "", "x",
                                 sub="推荐持仓交易日无效")

    def test_create_model_validation(self):
        self._assert_value_error(trades.create_model, "", "", sub="模型名称不能为空")
        self._assert_value_error(trades.create_model, "x" * 65, "", sub="模型名称过长")

    def test_create_model_duplicate_active(self):
        self._assert_value_error(trades.create_model, "A 60分钟超短", "",
                                 sub="模型名已存在")

    def test_update_model(self):
        self.assertTrue(trades.update_model(1, "A 改名", "新描述"))
        m = next(x for x in trades.list_models(active_only=False) if x["id"] == 1)
        self.assertEqual(m["name"], "A 改名")
        self.assertEqual(m["description"], "新描述")
        self.assertEqual(m["hold_days"], 3)  # 未传 hold_days 不改
        self.assertFalse(trades.update_model(999, "不存在", ""))

    def test_update_model_hold_days(self):
        self.assertTrue(trades.update_model(1, "A 60分钟超短", "", 5))
        m = next(x for x in trades.list_models(active_only=False) if x["id"] == 1)
        self.assertEqual(m["hold_days"], 5)
        self.assertTrue(trades.update_model(1, "A 60分钟超短", "", None))
        m = next(x for x in trades.list_models(active_only=False) if x["id"] == 1)
        self.assertIsNone(m["hold_days"])

    def test_update_model_duplicate(self):
        self._assert_value_error(trades.update_model, 2, "A 60分钟超短", "",
                                 sub="模型名已存在")

    def test_soft_delete_and_restore(self):
        self.assertTrue(trades.delete_model(1))
        models = trades.list_models(active_only=False)
        m = next(x for x in models if x["id"] == 1)
        self.assertEqual(m["active"], 0)
        self.assertIsNotNone(m["deleted_at"])
        # active_only 默认过滤停用项
        self.assertEqual([x["id"] for x in trades.list_models(active_only=True)],
                         [2, 3, 4, 5])

        self.assertTrue(trades.restore_model(1))
        m = next(x for x in trades.list_models(active_only=False) if x["id"] == 1)
        self.assertEqual(m["active"], 1)
        self.assertIsNone(m["deleted_at"])

    def test_delete_model_not_found(self):
        self.assertFalse(trades.delete_model(999))
        self.assertFalse(trades.restore_model(999))

    def test_reuse_name_after_soft_delete(self):
        # 停用后同名可复用
        trades.delete_model(1)
        mid = trades.create_model("A 60分钟超短", "")
        self.assertGreater(mid, 5)

    def test_restore_conflict_with_active_duplicate(self):
        trades.delete_model(1)
        trades.create_model("A 60分钟超短", "")  # 占用同名
        self._assert_value_error(trades.restore_model, 1,
                                 sub="模型名与启用中的模型重复")


# ---------------------------------------------------------------------------
# 6. 交易 CRUD
# ---------------------------------------------------------------------------
class TestTradeCRUD(TradesTestCase):
    def test_create_open_trade(self):
        uid = self._make_user()
        t = trades.create_trade(uid, self._open())
        self.assertIsNone(t["exit_price"])
        self.assertIsNone(t["pnl"])
        self.assertIsNone(t["return_pct"])
        self.assertEqual(t["symbol"], "300750.SZ")

    def test_create_closed_trade_computes_pnl(self):
        uid = self._make_user()
        t = trades.create_trade(uid, self._closed())
        self.assertEqual(t["pnl"], 300.0)
        self.assertEqual(t["return_pct"], 30.0)

    def test_symbol_uppercased(self):
        uid = self._make_user()
        t = trades.create_trade(uid, self._closed(symbol="600519.sh"))
        self.assertEqual(t["symbol"], "600519.SH")

    def test_closed_trade_validation(self):
        uid = self._make_user()
        # 缺股票代码
        self._assert_value_error(trades.create_trade, uid,
                                 {"status": "closed"}, sub="缺少股票代码")
        # 状态非法
        d = self._closed(); d["status"] = "pending"
        self._assert_value_error(trades.create_trade, uid, d,
                                 sub="status 必须为 open 或 closed")
        # 买入价无效 / 必须大于 0
        d = self._closed(); d["entry_price"] = "abc"
        self._assert_value_error(trades.create_trade, uid, d, sub="买入价无效")
        d = self._closed(); d["entry_price"] = 0
        self._assert_value_error(trades.create_trade, uid, d, sub="买入价必须大于 0")
        # 数量无效 / 必须大于 0
        d = self._closed(); d["quantity"] = "x"
        self._assert_value_error(trades.create_trade, uid, d, sub="数量无效")
        d = self._closed(); d["quantity"] = 0
        self._assert_value_error(trades.create_trade, uid, d, sub="数量必须大于 0")
        # 买入日期
        d = self._closed(); d["entry_date"] = "2026/01/05"
        self._assert_value_error(trades.create_trade, uid, d,
                                 sub="买入日期无效 (格式 YYYY-MM-DD)")
        # 缺买入理由
        d = self._closed(); d["entry_reason"] = ""
        self._assert_value_error(trades.create_trade, uid, d, sub="缺少买入理由")
        # 退出价
        d = self._closed(); d["exit_price"] = "abc"
        self._assert_value_error(trades.create_trade, uid, d, sub="退出价无效")
        d = self._closed(); d["exit_price"] = -1
        self._assert_value_error(trades.create_trade, uid, d, sub="退出价必须大于 0")
        # 卖出日期
        d = self._closed(); d["exit_date"] = "bad"
        self._assert_value_error(trades.create_trade, uid, d,
                                 sub="卖出日期无效 (格式 YYYY-MM-DD)")
        # 卖出日期早于买入日期
        d = self._closed(exit_date="2026-01-01")
        self._assert_value_error(trades.create_trade, uid, d,
                                 sub="卖出日期不能早于买入日期")
        # 缺卖出理由
        d = self._closed(); d["exit_reason"] = ""
        self._assert_value_error(trades.create_trade, uid, d, sub="缺少卖出理由")

    def test_model_id_validation(self):
        uid = self._make_user()
        d = self._closed(model_id="abc")
        self._assert_value_error(trades.create_trade, uid, d, sub="模型无效")
        d = self._closed(model_id=999)
        self._assert_value_error(trades.create_trade, uid, d, sub="模型不存在")

    def test_model_id_accepts_soft_deleted(self):
        uid = self._make_user()
        trades.delete_model(4)
        t = trades.create_trade(uid, self._closed(model_id=4))
        self.assertEqual(t["model_id"], 4)

    def test_get_trade_isolation(self):
        u1 = self._make_user("alice", "secret123")
        u2 = self._make_user("bob", "secret123")
        t = trades.create_trade(u1, self._closed())
        self.assertIsNotNone(trades.get_trade(u1, t["id"]))
        self.assertIsNone(trades.get_trade(u2, t["id"]))  # 他人不可见

    def test_list_trades_filters_and_pagination(self):
        uid = self._make_user()
        for s, n, p in [("000001.SZ", "平安银行", 10.0),
                        ("600519.SH", "贵州茅台", 1500.0),
                        ("000002.SZ", "万科A", 8.0)]:
            trades.create_trade(uid, self._closed(symbol=s, name=n, entry=p, exit_=p))
        # 全部 3 条
        records, total = trades.list_trades(uid, {})
        self.assertEqual(total, 3)
        # symbol 过滤
        _, total = trades.list_trades(uid, {"symbol": "000001.SZ"})
        self.assertEqual(total, 1)
        # status 过滤
        trades.create_trade(uid, self._open())
        _, total = trades.list_trades(uid, {"status": "open"})
        self.assertEqual(total, 1)
        # q 模糊匹配
        _, total = trades.list_trades(uid, {"q": "茅台"})
        self.assertEqual(total, 1)
        _, total = trades.list_trades(uid, {"q": "600519"})
        self.assertEqual(total, 1)
        # 分页
        records, total = trades.list_trades(uid, {"limit": 2, "offset": 0})
        self.assertEqual(total, 4)
        self.assertEqual(len(records), 2)

    def test_list_trades_model_id_filter(self):
        uid = self._make_user()
        trades.create_trade(uid, self._closed(model_id=4))
        trades.create_trade(uid, self._closed(
            symbol="600519.SH", name="贵州茅台", entry=1500.0, exit_=1500.0, model_id=2))
        trades.create_trade(uid, self._closed(
            symbol="000002.SZ", name="万科A", entry=8.0, exit_=8.0, model_id=None))
        trades.create_trade(uid, self._open(model_id=4))
        _, total = trades.list_trades(uid, {"model_id": 4})
        self.assertEqual(total, 2)
        _, total = trades.list_trades(uid, {"model_id": "none"})
        self.assertEqual(total, 1)
        _, total = trades.list_trades(uid, {"model_id": "null"})
        self.assertEqual(total, 1)
        recs, total = trades.list_trades(uid, {"model_id": 4, "status": "open"})
        self.assertEqual(total, 1)
        self.assertEqual(recs[0]["status"], "open")
        recs, total = trades.list_trades(uid, {"model_id": "2"})
        self.assertEqual(total, 1)
        self.assertEqual(recs[0]["model_id"], 2)
        _, total = trades.list_trades(uid, {"model_id": ""})
        self.assertEqual(total, 4)

    def test_update_trade(self):
        uid = self._make_user()
        t = trades.create_trade(uid, self._closed())
        upd = trades.update_trade(uid, t["id"], {"exit_price": 11.0, "quantity": 200})
        self.assertEqual(upd["exit_price"], 11.0)
        self.assertEqual(upd["pnl"], 200.0)  # (11-10)*200
        self.assertEqual(upd["return_pct"], 10.0)
        self.assertIsNone(trades.update_trade(uid, 999, {"quantity": 1}))

    def test_delete_trade(self):
        uid = self._make_user()
        t = trades.create_trade(uid, self._closed())
        self.assertTrue(trades.delete_trade(uid, t["id"]))
        self.assertIsNone(trades.get_trade(uid, t["id"]))
        self.assertFalse(trades.delete_trade(uid, t["id"]))


# ---------------------------------------------------------------------------
# 7. 统计
# ---------------------------------------------------------------------------
class TestStats(TradesTestCase):
    def _seed(self, uid):
        """4 平仓 + 1 持仓, 覆盖盈利/亏损/持平/多股票/多模型/无模型。"""
        # 盈利 300 (000001, D, 1月)
        trades.create_trade(uid, self._closed(
            symbol="000001.SZ", name="平安银行", entry=10.0, exit_=13.0, qty=100,
            entry_date="2026-01-05", exit_date="2026-01-12", model_id=4))
        # 盈利 200 (000001, D, 1月)
        trades.create_trade(uid, self._closed(
            symbol="000001.SZ", name="平安银行", entry=8.0, exit_=10.0, qty=100,
            entry_date="2026-01-15", exit_date="2026-01-22", model_id=4))
        # 亏损 100 (600519, B, 2月)
        trades.create_trade(uid, self._closed(
            symbol="600519.SH", name="贵州茅台", entry=1000.0, exit_=900.0, qty=1,
            entry_date="2026-02-02", exit_date="2026-02-09", model_id=2))
        # 持平 0 (000002, 无模型, 3月)
        trades.create_trade(uid, self._closed(
            symbol="000002.SZ", name="万科A", entry=5.0, exit_=5.0, qty=10,
            entry_date="2026-03-01", exit_date="2026-03-08", model_id=None))
        # 持仓 (300750)
        trades.create_trade(uid, self._open())

    def test_summary(self):
        uid = self._make_user()
        self._seed(uid)
        s = trades.compute_stats(uid)["summary"]
        self.assertEqual(s["closed_count"], 4)
        self.assertEqual(s["open_count"], 1)
        self.assertEqual(s["win_count"], 2)
        self.assertEqual(s["loss_count"], 1)
        self.assertEqual(s["break_even_count"], 1)
        self.assertEqual(s["total_pnl"], 400.0)
        self.assertEqual(s["total_return_pct"], 14.04)  # 400 / (1000+800+1000+50) * 100
        self.assertEqual(s["win_rate"], 66.67)
        self.assertEqual(s["profit_factor"], 5.0)
        self.assertEqual(s["avg_win"], 250.0)
        self.assertEqual(s["avg_loss"], -100.0)
        self.assertEqual(s["max_win"]["pnl"], 300.0)
        self.assertEqual(s["max_win"]["return_pct"], 30.0)
        self.assertEqual(s["max_win"]["symbol"], "000001.SZ")
        self.assertEqual(s["max_loss"]["pnl"], -100.0)
        self.assertEqual(s["max_loss"]["return_pct"], -10.0)
        self.assertEqual(s["max_loss"]["symbol"], "600519.SH")
        self.assertEqual(s["avg_holding_days"], 7.0)

    def test_summary_no_closed_trades(self):
        uid = self._make_user()
        s = trades.compute_stats(uid)["summary"]
        self.assertEqual(s["closed_count"], 0)
        self.assertEqual(s["total_pnl"], 0)
        self.assertEqual(s["total_return_pct"], 0.0)
        self.assertIsNone(s["win_rate"])
        self.assertIsNone(s["profit_factor"])
        self.assertIsNone(s["avg_win"])
        self.assertIsNone(s["avg_loss"])
        self.assertIsNone(s["max_win"])
        self.assertIsNone(s["max_loss"])
        self.assertIsNone(s["avg_holding_days"])

    def test_series_month(self):
        uid = self._make_user()
        self._seed(uid)
        series = trades.compute_stats(uid)["series"]["month"]
        self.assertEqual([b["label"] for b in series],
                         ["2026-01", "2026-02", "2026-03"])
        self.assertEqual(series[0]["pnl"], 500.0)
        self.assertEqual(series[0]["win_rate"], 100.0)
        self.assertEqual(series[1]["pnl"], -100.0)
        self.assertEqual(series[1]["win_rate"], 0.0)
        self.assertEqual(series[2]["pnl"], 0.0)
        self.assertIsNone(series[2]["win_rate"])

    def test_series_week_and_year(self):
        uid = self._make_user()
        self._seed(uid)
        stats = trades.compute_stats(uid)
        weeks = stats["series"]["week"]
        self.assertEqual([b["label"] for b in weeks],
                         [_week_label("2026-01-12"), _week_label("2026-01-22"),
                          _week_label("2026-02-09"), _week_label("2026-03-08")])
        years = stats["series"]["year"]
        self.assertEqual([b["label"] for b in years], ["2026"])
        self.assertEqual(years[0]["pnl"], 400.0)
        self.assertEqual(years[0]["win_rate"], 66.67)

    def test_by_symbol(self):
        uid = self._make_user()
        self._seed(uid)
        by_symbol = trades.compute_stats(uid)["by_symbol"]
        # 按盈亏降序
        self.assertEqual([x["symbol"] for x in by_symbol],
                         ["000001.SZ", "000002.SZ", "600519.SH"])
        self.assertEqual(by_symbol[0]["pnl"], 500.0)
        self.assertEqual(by_symbol[0]["count"], 2)
        self.assertEqual(by_symbol[0]["win_rate"], 100.0)
        self.assertEqual(by_symbol[1]["pnl"], 0.0)
        self.assertIsNone(by_symbol[1]["win_rate"])
        self.assertEqual(by_symbol[2]["pnl"], -100.0)
        self.assertEqual(by_symbol[2]["win_rate"], 0.0)

    def test_by_model(self):
        uid = self._make_user()
        self._seed(uid)
        by_model = trades.compute_stats(uid)["by_model"]
        self.assertEqual([x["name"] for x in by_model],
                         ["D 动力管线", "无", "B 日线波段"])
        self.assertEqual(by_model[0]["pnl"], 500.0)
        self.assertEqual(by_model[0]["count"], 2)
        self.assertEqual(by_model[0]["win_rate"], 100.0)
        self.assertTrue(by_model[0]["active"])
        self.assertEqual(by_model[1]["pnl"], 0.0)
        self.assertIsNone(by_model[1]["model_id"])
        self.assertEqual(by_model[2]["pnl"], -100.0)

    def test_by_model_soft_deleted_suffix(self):
        uid = self._make_user()
        trades.create_trade(uid, self._closed(model_id=4))
        trades.delete_model(4)
        by_model = trades.compute_stats(uid)["by_model"]
        self.assertEqual(by_model[0]["name"], "D 动力管线（已删除）")
        self.assertFalse(by_model[0]["active"])
        self.assertEqual(by_model[0]["pnl"], 300.0)

    def test_date_range(self):
        uid = self._make_user()
        self._seed(uid)
        s = trades.compute_stats(uid, start="2026-01-01", end="2026-01-31")
        self.assertEqual(s["summary"]["closed_count"], 2)
        self.assertEqual(s["summary"]["total_pnl"], 500.0)
        s = trades.compute_stats(uid, start="2026-03-01", end="2026-03-31")
        self.assertEqual(s["summary"]["closed_count"], 1)
        self.assertEqual(s["summary"]["total_pnl"], 0.0)

    def test_open_positions(self):
        uid = self._make_user()
        self._seed(uid)
        pos = trades.compute_stats(uid)["open_positions"]
        self.assertEqual(len(pos), 1)
        self.assertEqual(pos[0]["symbol"], "300750.SZ")
        self.assertEqual(pos[0]["entry_price"], 200.0)
        self.assertEqual(pos[0]["quantity"], 100)


# ---------------------------------------------------------------------------
# 8. 理由分类常量
# ---------------------------------------------------------------------------
class TestReasons(TradesTestCase):
    def test_entry_reasons(self):
        # 绿转=买入, 蓝转=买入
        self.assertIn("动力绿转", trades.ENTRY_REASONS)
        self.assertIn("动力蓝转", trades.ENTRY_REASONS)
        self.assertIn("其他", trades.ENTRY_REASONS)
        self.assertNotIn("动力红转", trades.ENTRY_REASONS)

    def test_exit_reasons(self):
        # 红转=卖出
        self.assertIn("动力红转", trades.EXIT_REASONS)
        self.assertIn("其他", trades.EXIT_REASONS)
        self.assertNotIn("动力绿转", trades.EXIT_REASONS)
        self.assertNotIn("动力蓝转", trades.EXIT_REASONS)

    def test_no_overlap_of_direction_markers(self):
        # 红转=卖出(仅EXIT), 绿转/蓝转=买入(仅ENTRY)
        self.assertIn("动力红转", trades.EXIT_REASONS)
        self.assertNotIn("动力红转", trades.ENTRY_REASONS)
        self.assertIn("动力绿转", trades.ENTRY_REASONS)
        self.assertNotIn("动力绿转", trades.EXIT_REASONS)
        self.assertIn("动力蓝转", trades.ENTRY_REASONS)
        self.assertNotIn("动力蓝转", trades.EXIT_REASONS)


# ---------------------------------------------------------------------------
# 9. 批次交易 (多次买卖/加仓减仓/做T)
# ---------------------------------------------------------------------------
class TestBatchTrade(TradesTestCase):
    @staticmethod
    def _batch(symbol="000001.SZ", name="平安银行", legs=None, model_id=None):
        return {"type": "batch", "symbol": symbol, "name": name,
                "legs": legs or [], "model_id": model_id}

    def test_batch_weighted_avg_pnl_and_return(self):
        uid = self._make_user()
        legs = [
            {"side": "buy", "price": 10.0, "quantity": 1000, "date": "2026-01-05", "time": "09:30"},
            {"side": "buy", "price": 12.0, "quantity": 500, "date": "2026-01-10", "time": "09:30"},
            {"side": "sell", "price": 15.0, "quantity": 800, "date": "2026-02-01", "time": "14:00"},
            {"side": "sell", "price": 16.0, "quantity": 700, "date": "2026-02-10", "time": "14:00"},
        ]
        t = trades.create_trade(uid, self._batch(legs=legs))
        # 累计卖出==累计买入 → 自动平仓
        self.assertEqual(t["type"], "batch")
        self.assertEqual(t["status"], "closed")
        self.assertEqual(t["quantity"], 0)
        self.assertEqual(t["entry_price"], 0.0)   # 平仓后加权均价归零
        self.assertEqual(t["entry_date"], "2026-01-05")
        self.assertEqual(t["exit_date"], "2026-02-10")
        # 移动加权平均毛盈亏: (15-10.667)*800 + (16-10.667)*700 = 7200
        self.assertAlmostEqual(t["pnl"], 7200.0, places=2)
        self.assertAlmostEqual(t["return_pct"], 45.0, places=2)  # 7200/16000
        self.assertAlmostEqual(t["cost"], 16000.0, places=2)
        self.assertEqual(len(t["legs"]), 4)

        # 扣佣: 每腿各计一次最低佣金 5 元 + 印花税(卖) + 过户费(双向)
        fee_config = trades.get_user_fees(uid)
        rows, total = trades.list_trades(uid, fee_config=fee_config)
        f = rows[0]
        # 费用 = 5.1 + 5.06 + 11.12 + 10.712 = 31.992
        self.assertAlmostEqual(f["fees"], 31.992, places=3)
        self.assertAlmostEqual(f["pnl"], 7168.01, places=2)
        self.assertAlmostEqual(f["return_pct"], 44.8, places=2)
        self.assertEqual(f["fee_breakdown"]["buy_comm"], 10.0)    # 两次买入各 5 元
        self.assertEqual(f["fee_breakdown"]["sell_comm"], 10.0)   # 两次卖出各 5 元
        self.assertAlmostEqual(f["fee_breakdown"]["stamp"], 11.6, places=2)
        self.assertAlmostEqual(f["fee_breakdown"]["transfer"], 0.392, places=3)

    def test_batch_partial_sell_stays_open(self):
        uid = self._make_user()
        legs = [
            {"side": "buy", "price": 10.0, "quantity": 1000, "date": "2026-03-02", "time": "09:30"},
            {"side": "sell", "price": 15.0, "quantity": 300, "date": "2026-03-03", "time": "14:00"},
        ]
        t = trades.create_trade(uid, self._batch(legs=legs))
        self.assertEqual(t["status"], "open")
        self.assertEqual(t["quantity"], 700)
        self.assertEqual(t["entry_price"], 10.0)   # 仅买入更新加权均价
        self.assertIsNone(t["exit_date"])
        # 已实现盈亏 = (15-10)*300 (卖出不改变均价)
        self.assertAlmostEqual(t["pnl"], 1500.0, places=2)
        self.assertAlmostEqual(t["return_pct"], 15.0, places=2)

    def test_batch_oversell_rejected(self):
        uid = self._make_user()
        legs = [
            {"side": "buy", "price": 10.0, "quantity": 100, "date": "2026-03-02", "time": "09:30"},
            {"side": "sell", "price": 10.0, "quantity": 200, "date": "2026-03-03", "time": "14:00"},
        ]
        self._assert_value_error(
            trades.create_trade, uid, self._batch(legs=legs), sub="卖出数量超过当前持仓")

    def test_batch_requires_buy_leg(self):
        uid = self._make_user()
        legs = [
            {"side": "sell", "price": 10.0, "quantity": 100, "date": "2026-03-02", "time": "09:30"},
        ]
        self._assert_value_error(
            trades.create_trade, uid, self._batch(legs=legs), sub="至少需要一条买入腿")

    def test_batch_t_stats(self):
        uid = self._make_user()
        legs = [
            # 03-02 正T: 首腿买, 买1000 卖500 → 配对500, (10.5-10.0)*500=+250
            {"side": "buy", "price": 10.0, "quantity": 1000, "date": "2026-03-02", "time": "09:30"},
            {"side": "sell", "price": 10.5, "quantity": 500, "date": "2026-03-02", "time": "14:00"},
            # 03-03 反T: 首腿卖, 卖500 买300 → 配对300, (11.0-10.8)*300=+60
            {"side": "sell", "price": 11.0, "quantity": 500, "date": "2026-03-03", "time": "09:30"},
            {"side": "buy", "price": 10.8, "quantity": 300, "date": "2026-03-03", "time": "14:00"},
            # 03-04 仅卖出 → 不计做T, 清仓
            {"side": "sell", "price": 11.5, "quantity": 300, "date": "2026-03-04", "time": "10:00"},
        ]
        t = trades.create_trade(uid, self._batch(legs=legs))
        self.assertEqual(t["status"], "closed")
        ts = t["t_stats"]
        self.assertEqual(ts["count"], 2)
        self.assertEqual(ts["positive"], 1)
        self.assertEqual(ts["reverse"], 1)
        self.assertEqual(ts["success"], 2)
        self.assertEqual(ts["success_rate"], 100.0)
        self.assertAlmostEqual(ts["pnl"], 310.0, places=2)

        # 汇总做T统计 (同一 closed 集合)
        summary_ts = trades.compute_stats(uid)["summary"]["t_stats"]
        self.assertEqual(summary_ts["count"], 2)
        self.assertEqual(summary_ts["positive"], 1)
        self.assertEqual(summary_ts["reverse"], 1)
        self.assertEqual(summary_ts["success"], 2)
        self.assertEqual(summary_ts["success_rate"], 100.0)
        self.assertAlmostEqual(summary_ts["pnl"], 310.0, places=2)

    def test_batch_list_get_returns_legs(self):
        uid = self._make_user()
        legs = [
            {"side": "buy", "price": 10.0, "quantity": 100, "date": "2026-03-02", "time": "09:30"},
            {"side": "sell", "price": 12.0, "quantity": 100, "date": "2026-03-03", "time": "14:00"},
        ]
        t = trades.create_trade(uid, self._batch(legs=legs))
        got = trades.get_trade(uid, t["id"])
        self.assertEqual(len(got["legs"]), 2)
        self.assertIsNotNone(got["t_stats"])
        rows, total = trades.list_trades(uid)
        self.assertEqual(total, 1)
        self.assertEqual(len(rows[0]["legs"]), 2)
        self.assertIsNotNone(rows[0]["t_stats"])

    def test_batch_update_replaces_legs(self):
        uid = self._make_user()
        legs = [
            {"side": "buy", "price": 10.0, "quantity": 100, "date": "2026-03-02", "time": "09:30"},
            {"side": "sell", "price": 12.0, "quantity": 100, "date": "2026-03-03", "time": "14:00"},
        ]
        t = trades.create_trade(uid, self._batch(legs=legs))
        new_legs = [
            {"side": "buy", "price": 20.0, "quantity": 100, "date": "2026-03-04", "time": "09:30"},
        ]
        upd = trades.update_trade(uid, t["id"], {"type": "batch", "legs": new_legs})
        self.assertEqual(upd["status"], "open")
        self.assertEqual(len(upd["legs"]), 1)
        self.assertEqual(upd["entry_price"], 20.0)
        self.assertEqual(upd["quantity"], 100)

    def test_migration_old_trades_gain_type_simple(self):
        # 模拟「旧库」: 仅建无 type 列的 trades 表, 其余表交给 init_db 的 executescript
        import sqlite3
        old_db = os.path.join(self._tmp.name, "old.db")
        conn = sqlite3.connect(old_db)
        conn.executescript("""
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT NOT NULL,
                status TEXT NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL,
                quantity INTEGER NOT NULL,
                entry_date TEXT NOT NULL,
                exit_date TEXT,
                entry_reason TEXT NOT NULL,
                entry_note TEXT,
                exit_reason TEXT,
                exit_note TEXT,
                model_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)
        conn.commit()
        conn.close()

        trades.init_db(old_db)  # 迁移应补 type 列
        with trades.get_conn() as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(trades)")}
        self.assertIn("type", cols)

        uid = trades.create_user("alice", "secret123")
        t = trades.create_trade(uid, self._closed())
        self.assertEqual(t["type"], "simple")
        self.assertEqual(t["pnl"], 300.0)   # 老口径逐位不变


class TestRiskPricesAndMonitorAuth(TradesTestCase):
    def test_migration_adds_risk_and_monitor_columns(self):
        with trades.get_conn() as conn:
            tcols = {r[1] for r in conn.execute("PRAGMA table_info(trades)")}
            ucols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
        for col in ("take_profit", "stop_loss", "breakeven", "repo_days"):
            self.assertIn(col, tcols)
        self.assertIn("monitor_enabled", ucols)
        self.assertIn("search_history", ucols)
        self.assertIn("monitor_alerts", tables)

    def test_risk_prices_optional_and_clear(self):
        uid = self._make_user()
        t = trades.create_trade(uid, self._open(take_profit=220, stop_loss=180, breakeven=200))
        self.assertEqual(t["take_profit"], 220)
        self.assertEqual(t["stop_loss"], 180)
        self.assertEqual(t["breakeven"], 200)
        t2 = trades.update_trade(uid, t["id"], {"take_profit": "", "stop_loss": "", "breakeven": ""})
        self.assertIsNone(t2["take_profit"])
        self.assertIsNone(t2["stop_loss"])
        self.assertIsNone(t2["breakeven"])
        self._assert_value_error(
            trades.update_trade, uid, t["id"], {"take_profit": -1}, sub="止盈价必须大于 0"
        )
        self._assert_value_error(
            trades.update_trade, uid, t["id"], {"stop_loss": "x"}, sub="止损价无效"
        )

    def test_risk_prices_all_or_nothing_and_order(self):
        uid = self._make_user()
        self._assert_value_error(
            trades.create_trade, uid, self._open(stop_loss=180),
            sub="止盈/止损/保本须全部填写或全部留空",
        )
        self._assert_value_error(
            trades.create_trade, uid,
            self._open(take_profit=200, stop_loss=180, breakeven=210),
            sub="须满足止盈价 > 保本价 > 止损价",
        )
        self._assert_value_error(
            trades.create_trade, uid,
            self._open(take_profit=200, stop_loss=200, breakeven=200),
            sub="须满足止盈价 > 保本价 > 止损价",
        )
        t = trades.create_trade(uid, self._open())
        self.assertIsNone(t["take_profit"])
        t2 = trades.update_trade(uid, t["id"], {
            "take_profit": 220, "stop_loss": 180, "breakeven": 200,
        })
        self.assertEqual((t2["take_profit"], t2["breakeven"], t2["stop_loss"]), (220, 200, 180))

    def test_legacy_partial_risk_kept_on_other_update(self):
        uid = self._make_user()
        t = trades.create_trade(uid, self._open())
        with trades.get_conn() as conn:
            conn.execute("UPDATE trades SET stop_loss=180 WHERE id=?", (t["id"],))
            conn.commit()
        t2 = trades.update_trade(uid, t["id"], {"entry_note": "备注"})
        self.assertEqual(t2["stop_loss"], 180)
        self.assertIsNone(t2["take_profit"])
        self.assertEqual(t2["entry_note"], "备注")
        self._assert_value_error(
            trades.update_trade, uid, t["id"], {"take_profit": 220},
            sub="止盈/止损/保本须全部填写或全部留空",
        )

    def test_batch_keeps_risk_prices(self):
        uid = self._make_user()
        t = trades.create_trade(uid, {
            "type": "batch", "symbol": "000001.SZ", "name": "平安银行",
            "take_profit": 12.0, "stop_loss": 9.0, "breakeven": 10.5,
            "legs": [
                {"side": "buy", "price": 10.0, "quantity": 1000, "date": "2026-03-02"},
            ],
        })
        self.assertEqual(t["take_profit"], 12.0)
        self.assertEqual(t["status"], "open")

    def test_open_positions_include_risk_fields(self):
        uid = self._make_user()
        trades.create_trade(uid, self._open(take_profit=240, stop_loss=180, breakeven=200))
        pos = trades.compute_stats(uid)["open_positions"]
        self.assertEqual(len(pos), 1)
        self.assertEqual(pos[0]["take_profit"], 240)
        self.assertEqual(pos[0]["stop_loss"], 180)
        self.assertEqual(pos[0]["breakeven"], 200)

    def test_monitor_enabled_and_positions(self):
        admin = self._make_user("admin", "secret123", is_admin=True)
        bob = self._make_user("bob", "secret123")
        trades.create_trade(admin, self._open(
            symbol="600000.SH", name="浦发", take_profit=10.0, breakeven=9.0, stop_loss=8.0))
        trades.create_trade(bob, self._open(
            symbol="000001.SZ", name="平安", take_profit=13.0, breakeven=11.0, stop_loss=9.0))
        # bob 未授权 → 只有管理员持仓
        pos = trades.list_monitored_positions()
        self.assertEqual({p["symbol"] for p in pos}, {"600000.SH"})
        self.assertTrue(trades.set_user_monitor(bob, True))
        pos = trades.list_monitored_positions()
        self.assertEqual({p["symbol"] for p in pos}, {"600000.SH", "000001.SZ"})

    def test_hold_expire_positions_anchor_and_auth(self):
        admin = self._make_user("admin", "secret123", is_admin=True)
        bob = self._make_user("bob", "secret123")
        # 无风控价也进到期列表; 无模型 / E(空 hold_days) 不进
        trades.create_trade(admin, self._open(
            symbol="000001.SZ", name="平安", model_id=1, entry_date="2026-08-21"))
        trades.create_trade(admin, self._open(
            symbol="000002.SZ", name="万科", model_id=5, entry_date="2026-08-21"))
        trades.create_trade(admin, self._open(
            symbol="000003.SZ", name="无模型", model_id=None, entry_date="2026-08-21"))
        trades.create_trade(bob, self._open(
            symbol="600000.SH", name="浦发", model_id=2, entry_date="2026-08-10"))
        pos = trades.list_hold_expire_positions()
        self.assertEqual({p["symbol"] for p in pos}, {"000001.SZ"})
        self.assertEqual(pos[0]["hold_days"], 3)
        self.assertEqual(pos[0]["hold_anchor_date"], "2026-08-21")
        trades.set_user_monitor(bob, True)
        pos = {p["symbol"]: p for p in trades.list_hold_expire_positions()}
        self.assertEqual(set(pos), {"000001.SZ", "600000.SH"})
        self.assertEqual(pos["600000.SH"]["hold_days"], 20)

    def test_hold_expire_batch_uses_latest_buy(self):
        admin = self._make_user("admin", "secret123", is_admin=True)
        trades.create_trade(admin, {
            "type": "batch", "symbol": "000001.SZ", "name": "平安银行",
            "model_id": 1,
            "legs": [
                {"side": "buy", "price": 10.0, "quantity": 1000,
                 "date": "2026-08-03", "time": "09:30"},
                {"side": "sell", "price": 10.5, "quantity": 400,
                 "date": "2026-08-10", "time": "14:00"},
                {"side": "buy", "price": 11.0, "quantity": 200,
                 "date": "2026-08-19", "time": "10:00"},
            ],
        })
        pos = trades.list_hold_expire_positions()
        self.assertEqual(len(pos), 1)
        self.assertEqual(pos[0]["hold_anchor_date"], "2026-08-19")

    def test_trade_row_attaches_hold_days(self):
        uid = self._make_user()
        t = trades.create_trade(uid, self._open(model_id=4))
        self.assertEqual(t["hold_days"], 7)
        recs, _ = trades.list_trades(uid)
        self.assertEqual(recs[0]["hold_days"], 7)

    def test_monitor_alerts_roundtrip(self):
        uid = self._make_user()
        t = trades.create_trade(uid, self._open(take_profit=240, stop_loss=180, breakeven=200))
        aid = trades.insert_monitor_alert(
            uid, t["id"], t["symbol"], "accel_down", "2026-08-19",
            price=18.8, detail="test",
        )
        self.assertTrue(aid > 0)
        last = trades.last_monitor_alert(uid, t["symbol"], "accel_down")
        self.assertEqual(last["trade_date"], "2026-08-19")
        rows = trades.list_monitor_alerts(uid)
        self.assertEqual(len(rows), 1)


# ---------------------------------------------------------------------------
# 逆回购 (国债逆回购 reverse_repo)
# ---------------------------------------------------------------------------
class TestReverseRepo(TradesTestCase):
    def test_classify_symbol_repo_codes(self):
        self.assertEqual(trades.classify_symbol("204001.SH"), ("reverse_repo", "1"))
        self.assertEqual(trades.classify_symbol("204007.SH"), ("reverse_repo", "7"))
        self.assertEqual(trades.classify_symbol("204182.SH"), ("reverse_repo", "182"))
        self.assertEqual(trades.classify_symbol("131810.SZ"), ("reverse_repo", "1"))
        self.assertEqual(trades.classify_symbol("131802.SZ"), ("reverse_repo", "14"))
        self.assertEqual(trades.classify_symbol("131806.SZ"), ("reverse_repo", "182"))
        self.assertEqual(trades.classify_symbol("600000.SH"), ("stock", None))
        self.assertEqual(trades.classify_symbol("204001"), ("reverse_repo", "1"))

    def test_clean_reverse_repo_defaults(self):
        d, err = trades._clean_reverse_repo({
            "symbol": "204001.SH", "entry_price": 2.5, "quantity": 100000,
            "entry_date": "2026-08-25",
        })
        self.assertIsNone(err)
        self.assertEqual(d["type"], "reverse_repo")
        self.assertEqual(d["status"], "open")
        self.assertEqual(d["name"], "国债逆回购1天")
        self.assertEqual(d["entry_price"], 2.5)
        self.assertEqual(d["quantity"], 100000)
        self.assertEqual(d["repo_days"], 1)          # 默认=期限
        self.assertEqual(d["exit_date"], "2026-08-26")  # 买入日+1天
        self.assertIsNone(d["take_profit"])

    def test_clean_reverse_repo_validation(self):
        base = {"symbol": "204001.SH", "entry_price": 2.5,
                "quantity": 100000, "entry_date": "2026-08-25"}

        def _err(**over):
            d, err = trades._clean_reverse_repo({**base, **over})
            return err

        self.assertIn("不是逆回购代码", _err(symbol="600000.SH"))
        self.assertIn("必须大于 0", _err(entry_price=0))
        self.assertIn("成交利率无效", _err(entry_price="x"))
        self.assertIn("最低 1000", _err(quantity=500))
        self.assertIn("必须大于 0", _err(quantity=0))
        self.assertIn("买入日期无效", _err(entry_date="bad"))
        self.assertIn("计息天数", _err(repo_days=0))
        self.assertIn("计息天数无效", _err(repo_days="x"))

    def test_repo_maturity_skips_weekend(self):
        # 2026-08-28 是周五, 1天期到期 08-29(周六) → 顺延 08-31(周一)
        self.assertEqual(trades._repo_maturity("2026-08-28", 1), "2026-08-31")

    def test_repo_metrics_interest_and_commission(self):
        # 10万 × 2.5% × 3天 /365 = 20.55; 1天期佣金 10万×十万分之1 = 1.0
        d, _ = trades._clean_reverse_repo({
            "symbol": "204001.SH", "entry_price": 2.5, "quantity": 100000,
            "entry_date": "2026-08-25", "repo_days": 3,
        })
        m = trades._trade_metrics(d, fee_config=trades.DEFAULT_FEES, deduct_fees=True)
        self.assertAlmostEqual(m["interest"], 20.55, places=2)
        self.assertEqual(m["fees"], 1.0)
        self.assertAlmostEqual(m["pnl"], 19.55, places=2)
        self.assertEqual(m["tenor"], 1)
        self.assertEqual(m["repo_days"], 3)
        # 不扣佣金 → 毛利 = 利息
        m2 = trades._trade_metrics(d, fee_config=trades.DEFAULT_FEES, deduct_fees=False)
        self.assertAlmostEqual(m2["pnl"], 20.55, places=2)
        self.assertEqual(m2["fees"], 0.0)

    def test_create_repo_and_stats_separate(self):
        uid = self._make_user("admin", "secret123", is_admin=True)
        r1 = trades.create_trade(uid, {
            "type": "reverse_repo", "symbol": "204001.SH", "entry_price": 2.5,
            "quantity": 100000, "entry_date": "2026-08-25", "repo_days": 3,
        })
        self.assertEqual(r1["name"], "国债逆回购1天")
        self.assertEqual(r1["interest"], 20.55)
        self.assertEqual(r1["fees"], 1.0)
        self.assertEqual(r1["pnl"], 19.55)
        trades.create_trade(uid, {
            "type": "reverse_repo", "symbol": "131802.SZ", "entry_price": 2.8,
            "quantity": 50000, "entry_date": "2026-08-10",
        })
        st = trades.compute_stats(uid, start="2026-08-01", end="2026-08-31",
                                  deduct_fees=True, fee_config=trades.DEFAULT_FEES)
        s = st["summary"]
        self.assertEqual(s["repo_count"], 2)
        self.assertAlmostEqual(s["repo_income"], 19.55 + 48.70, places=2)
        # 单独汇总, 不并总盈亏
        self.assertEqual(s["total_pnl"], 0)
        self.assertEqual(s["win_count"], 0)
        # 不进持仓/不进监控
        self.assertEqual(st["open_positions"], [])
        self.assertEqual(trades.list_monitored_positions(), [])

    def test_repo_excluded_from_monitor_and_open_positions(self):
        uid = self._make_user("admin", "secret123", is_admin=True)
        trades.create_trade(uid, {
            "type": "reverse_repo", "symbol": "204001.SH", "entry_price": 2.5,
            "quantity": 100000, "entry_date": "2026-08-25",
        })
        trades.create_trade(uid, self._open(take_profit=220, stop_loss=180, breakeven=200))
        self.assertEqual(len(trades.list_monitored_positions()), 1)
        st = trades.compute_stats(uid)
        self.assertEqual(len(st["open_positions"]), 1)
        self.assertEqual(st["open_positions"][0]["symbol"], "300750.SZ")

    def test_update_repo_recomputes(self):
        uid = self._make_user("admin", "secret123", is_admin=True)
        r = trades.create_trade(uid, {
            "type": "reverse_repo", "symbol": "204007.SH", "entry_price": 2.0,
            "quantity": 50000, "entry_date": "2026-08-20",
        })
        self.assertEqual(r["tenor"], 7)
        u = trades.update_trade(uid, r["id"], {"repo_days": 9, "entry_price": 2.2})
        # 5万 × 2.2% × 9/365 = 27.12; 佣金 5万×十万分之5 = 2.5
        self.assertAlmostEqual(u["interest"], 27.12, places=2)
        self.assertEqual(u["fees"], 2.5)
        self.assertAlmostEqual(u["pnl"], 24.62, places=2)

    def test_repo_list_by_entry_date_not_maturity(self):
        """列表按买入日归属: 到期日(次日)超出查询区间时仍应显示。"""
        uid = self._make_user("admin", "secret123", is_admin=True)
        trades.create_trade(uid, {
            "type": "reverse_repo", "symbol": "204001.SH", "entry_price": 2.0,
            "quantity": 100000, "entry_date": "2026-08-25",
        })
        recs, total = trades.list_trades(
            uid, {"from": "2026-08-25", "to": "2026-08-25"}, fee_config=trades.DEFAULT_FEES,
        )
        self.assertEqual(total, 1)
        self.assertEqual(recs[0]["type"], "reverse_repo")
        # 到期日 08-26 在区间外, 若按 COALESCE(exit_date, entry_date) 会被过滤
        recs2, total2 = trades.list_trades(
            uid, {"from": "2026-08-20", "to": "2026-08-24"}, fee_config=trades.DEFAULT_FEES,
        )
        self.assertEqual(total2, 0)

    def test_repo_status_filter_uses_exit_date(self):
        """逆回购 status 恒 open: 筛持仓中只含未到期, 筛已平仓含已到期。"""
        from datetime import timedelta
        uid = self._make_user("admin", "secret123", is_admin=True)
        today = trades._now().date()
        # 已到期: 买入日 = 今天-3 → 到期约今天-2
        matured = trades.create_trade(uid, {
            "type": "reverse_repo", "symbol": "204001.SH", "entry_price": 2.0,
            "quantity": 100000,
            "entry_date": (today - timedelta(days=3)).isoformat(),
        })
        self.assertLessEqual(matured["exit_date"], today.isoformat())
        # 未到期: 买入日 = 今天 → 到期至少明天
        active = trades.create_trade(uid, {
            "type": "reverse_repo", "symbol": "131810.SZ", "entry_price": 2.0,
            "quantity": 100000, "entry_date": today.isoformat(),
        })
        self.assertGreater(active["exit_date"], today.isoformat())
        open_recs, open_n = trades.list_trades(uid, {"status": "open"})
        closed_recs, closed_n = trades.list_trades(uid, {"status": "closed"})
        open_ids = {r["id"] for r in open_recs}
        closed_ids = {r["id"] for r in closed_recs}
        self.assertIn(active["id"], open_ids)
        self.assertNotIn(matured["id"], open_ids)
        self.assertIn(matured["id"], closed_ids)
        self.assertNotIn(active["id"], closed_ids)
        self.assertEqual(open_n, 1)
        self.assertEqual(closed_n, 1)


class TestSearchHistory(TradesTestCase):
    def test_migration_adds_search_history_column(self):
        with trades.get_conn() as conn:
            ucols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
        self.assertIn("search_history", ucols)

    def test_empty_by_default(self):
        uid = self._make_user()
        self.assertEqual(trades.get_search_history(uid), [])

    def test_roundtrip_with_ts(self):
        uid = self._make_user()
        saved = trades.set_search_history(uid, [
            {"symbol": "000001.SZ", "name": "平安银行", "ts": 2000},
            {"symbol": "600000.SH", "name": "浦发银行", "ts": 1000},
        ])
        self.assertEqual(saved, [
            {"symbol": "000001.SZ", "name": "平安银行", "ts": 2000},
            {"symbol": "600000.SH", "name": "浦发银行", "ts": 1000},
        ])
        self.assertEqual(trades.get_search_history(uid), saved)

    def test_sort_by_ts_descending(self):
        uid = self._make_user()
        saved = trades.set_search_history(uid, [
            {"symbol": "600000.SH", "name": "浦发", "ts": 100},
            {"symbol": "000001.SZ", "name": "平安", "ts": 300},
            {"symbol": "300750.SZ", "name": "宁德", "ts": 200},
        ])
        self.assertEqual([h["symbol"] for h in saved], [
            "000001.SZ", "300750.SZ", "600000.SH",
        ])

    def test_legacy_without_ts_goes_after_timestamped(self):
        """无 ts 的旧记录排在有 ts 的后面，且不伪造 ts。"""
        uid = self._make_user()
        saved = trades.set_search_history(uid, [
            {"symbol": "LEGACY.SZ", "name": "旧股"},
            {"symbol": "000001.SZ", "name": "平安", "ts": 100},
            {"symbol": "OLD2.SZ", "name": "旧2"},
            {"symbol": "600000.SH", "name": "浦发", "ts": 200},
        ])
        self.assertEqual([h["symbol"] for h in saved], [
            "600000.SH", "000001.SZ", "LEGACY.SZ", "OLD2.SZ",
        ])
        self.assertIn("ts", saved[0])
        self.assertIn("ts", saved[1])
        self.assertNotIn("ts", saved[2])
        self.assertNotIn("ts", saved[3])

    def test_legacy_only_preserves_relative_order(self):
        uid = self._make_user()
        saved = trades.set_search_history(uid, [
            {"symbol": "000001.SZ", "name": "平安银行"},
            {"symbol": "600000.SH", "name": "浦发银行"},
            {"symbol": "300750.SZ", "name": "宁德时代"},
        ])
        self.assertEqual([h["symbol"] for h in saved], [
            "000001.SZ", "600000.SH", "300750.SZ",
        ])
        for h in saved:
            self.assertNotIn("ts", h)

    def test_dedupe_prefers_timestamped_over_legacy(self):
        uid = self._make_user()
        saved = trades.set_search_history(uid, [
            {"symbol": "000001.SZ", "name": "无时间戳版"},
            {"symbol": "000001.SZ", "name": "有时间戳版", "ts": 50},
        ])
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["name"], "有时间戳版")
        self.assertEqual(saved[0]["ts"], 50)

    def test_dedupe_keeps_newer_ts(self):
        uid = self._make_user()
        saved = trades.set_search_history(uid, [
            {"symbol": "000001.SZ", "name": "旧名", "ts": 100},
            {"symbol": "000001.SZ", "name": "新名", "ts": 200},
            {"symbol": "600000.SH", "name": "浦发", "ts": 150},
        ])
        self.assertEqual(len(saved), 2)
        self.assertEqual(saved[0], {"symbol": "000001.SZ", "name": "新名", "ts": 200})
        self.assertEqual(saved[1]["symbol"], "600000.SH")

    def test_dedupe_legacy_keeps_first(self):
        uid = self._make_user()
        saved = trades.set_search_history(uid, [
            {"symbol": "000001.SZ", "name": "先出现"},
            {"symbol": "000001.SZ", "name": "后出现应丢"},
        ])
        self.assertEqual(saved, [{"symbol": "000001.SZ", "name": "先出现"}])

    def test_invalid_ts_treated_as_legacy(self):
        uid = self._make_user()
        saved = trades.set_search_history(uid, [
            {"symbol": "BAD.SZ", "name": "坏", "ts": "x"},
            {"symbol": "NEG.SZ", "name": "负", "ts": -1},
            {"symbol": "OK.SZ", "name": "好", "ts": 10},
        ])
        self.assertEqual(saved[0]["symbol"], "OK.SZ")
        self.assertEqual([h["symbol"] for h in saved[1:]], ["BAD.SZ", "NEG.SZ"])
        self.assertNotIn("ts", saved[1])
        self.assertNotIn("ts", saved[2])

    def test_merge_local_new_with_server_legacy(self):
        """模拟：服务端旧无 ts + 本地新搜索带 ts → 新的在前。"""
        merged = trades._normalize_search_history([
            {"symbol": "LEGACY.SZ", "name": "旧"},
            {"symbol": "NEW.SZ", "name": "新", "ts": 999},
            {"symbol": "MID.SZ", "name": "中", "ts": 500},
        ])
        self.assertEqual([h["symbol"] for h in merged], ["NEW.SZ", "MID.SZ", "LEGACY.SZ"])

    def test_truncate_keeps_timestamped_first(self):
        uid = self._make_user()
        raw = (
            [{"symbol": f"L{i}.SZ", "name": f"旧{i}"} for i in range(8)]
            + [{"symbol": f"T{i}.SZ", "name": f"新{i}", "ts": 100 + i} for i in range(5)]
        )
        saved = trades.set_search_history(uid, raw)
        self.assertEqual(len(saved), trades.SEARCH_HISTORY_MAX)
        # 5 条有 ts（按 ts 降序）应全部在前，再补无 ts
        self.assertTrue(all("ts" in h for h in saved[:5]))
        self.assertEqual(saved[0]["symbol"], "T4.SZ")
        self.assertTrue(all("ts" not in h for h in saved[5:]))

    def test_normalize_skips_invalid(self):
        uid = self._make_user()
        saved = trades.set_search_history(uid, [
            None,
            "not-a-dict",
            {"symbol": "", "name": "空代码"},
            {"symbol": "  000001.SZ  ", "name": "  平安  ", "ts": 50},
            {"name": "无代码"},
            {"symbol": "600000.SH", "ts": 40},
        ])
        self.assertEqual(saved[0]["symbol"], "000001.SZ")
        self.assertEqual(saved[0]["name"], "平安")
        self.assertEqual(saved[1]["symbol"], "600000.SH")
        self.assertEqual(saved[1]["name"], "600000.SH")

    def test_corrupt_json_returns_empty(self):
        uid = self._make_user()
        with trades.get_conn() as conn:
            conn.execute(
                "UPDATE users SET search_history=? WHERE id=?",
                ("{not-json", uid),
            )
            conn.commit()
        self.assertEqual(trades.get_search_history(uid), [])

    def test_isolation_between_users(self):
        a = self._make_user("alice", "secret123")
        b = self._make_user("bob", "secret123")
        trades.set_search_history(a, [{"symbol": "000001.SZ", "name": "平安银行", "ts": 1}])
        trades.set_search_history(b, [{"symbol": "600000.SH", "name": "浦发银行", "ts": 1}])
        self.assertEqual(trades.get_search_history(a)[0]["symbol"], "000001.SZ")
        self.assertEqual(trades.get_search_history(b)[0]["symbol"], "600000.SH")

    def test_overwrite_and_clear(self):
        uid = self._make_user()
        trades.set_search_history(uid, [{"symbol": "000001.SZ", "name": "平安银行", "ts": 1}])
        cleared = trades.set_search_history(uid, [])
        self.assertEqual(cleared, [])
        self.assertEqual(trades.get_search_history(uid), [])

    def test_empty_put_preserves_existing_when_clear_disallowed(self):
        """模拟浏览器清缓存后上传 []：不得抹掉服务端已有记录。"""
        uid = self._make_user()
        trades.set_search_history(uid, [
            {"symbol": "000001.SZ", "name": "平安银行", "ts": 100},
            {"symbol": "600000.SH", "name": "浦发银行", "ts": 50},
        ])
        kept = trades.set_search_history(uid, [], allow_clear=False)
        self.assertEqual(len(kept), 2)
        self.assertEqual(kept[0]["symbol"], "000001.SZ")
        self.assertEqual(trades.get_search_history(uid), kept)

    def test_empty_put_allowed_when_explicit_clear(self):
        uid = self._make_user()
        trades.set_search_history(uid, [{"symbol": "000001.SZ", "name": "平安", "ts": 1}])
        trades.set_search_history(uid, [], allow_clear=True)
        self.assertEqual(trades.get_search_history(uid), [])

    def test_sync_scenario_local_empty_server_intact(self):
        """本地空 + 服务端有数据：合并后应保留服务端，且不应因 [] 上传而清空。"""
        merged = trades._normalize_search_history([
            {"symbol": "000636.SZ", "name": "风华", "ts": 500},
        ])
        self.assertEqual(len(merged), 1)
        # 客户端若误传 []，服务端 guard 仍保留
        uid = self._make_user()
        trades.set_search_history(uid, merged)
        after = trades.set_search_history(uid, [], allow_clear=False)
        self.assertEqual(after, merged)

    def test_explicit_null_ts_treated_as_legacy(self):
        uid = self._make_user()
        saved = trades.set_search_history(uid, [
            {"symbol": "NULL.SZ", "name": "空ts", "ts": None},
            {"symbol": "OK.SZ", "name": "好", "ts": 1},
        ])
        self.assertEqual(saved[0]["symbol"], "OK.SZ")
        self.assertEqual(saved[1]["symbol"], "NULL.SZ")
        self.assertNotIn("ts", saved[1])

    def test_unknown_user_get_empty(self):
        self.assertEqual(trades.get_search_history(999999), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
