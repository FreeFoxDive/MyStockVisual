# -*- coding: utf-8 -*-
"""信号忽视记录 (signal_ignores) — 校验规则 / CRUD / 过滤分页 / API 权限。

规则要点 (量化管理页录入口径):
  * 模型必填且必须存在 (不可为空/不可为 "无");
  * 剔除理由必填非空;
  * 信号日期必填、归一为 YYYY-MM-DD、不得晚于今天;
  * 现价/止盈/保本/止损可空, 非空须 > 0; 已填的风控价之间须满足 止盈 > 保本 > 止损;
  * 全部接口仅管理员可用 (页面本身也是管理员页)。

运行:
    venv/Scripts/python.exe -u visual/test/test_signal_ignores.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

_VISUAL_DIR = Path(__file__).resolve().parents[1]
if str(_VISUAL_DIR) not in sys.path:
    sys.path.insert(0, str(_VISUAL_DIR))

import trades  # noqa: E402


def _today(offset=0):
    return (date.today() + timedelta(days=offset)).isoformat()


class SignalIgnoreStoreTest(unittest.TestCase):
    """trades 层: 校验矩阵 + CRUD + 过滤分页。"""

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls._db = Path(cls._tmpdir.name) / "test_ignores.db"
        cls._orig_db = trades._db_path
        trades.init_db(cls._db)
        cls.user_id = trades.create_user("ignore_admin", "password123", is_admin=True)
        cls.model_id = trades.create_model("T 测试模型", "单测用", 5)
        cls.other_model_id = trades.create_model("T 停用模型", "", None)
        trades.delete_model(cls.other_model_id)

    @classmethod
    def tearDownClass(cls):
        trades._db_path = cls._orig_db
        cls._tmpdir.cleanup()

    def base(self, **over):
        data = {
            "symbol": "000001.SZ", "name": "平安银行", "model_id": self.model_id,
            "reason": "量能不足", "reason_note": "", "signal_date": _today(-1),
        }
        data.update(over)
        return data

    # ── 校验 ──
    def test_model_is_required(self):
        for bad in (None, "", "abc"):
            err = trades.validate_signal_ignore(self.base(**{"model_id": bad}))
            self.assertTrue(err, f"model_id={bad!r} 应被拒绝")
            self.assertIn("模型", err)
        self.assertEqual(trades.validate_signal_ignore(self.base(model_id=999999)), "模型不存在")
        self.assertIsNone(trades.validate_signal_ignore(self.base()), "合法数据不应报错")

    def test_reason_is_required(self):
        self.assertEqual(trades.validate_signal_ignore(self.base(reason="")), "请选择剔除理由")
        self.assertEqual(trades.validate_signal_ignore(self.base(reason="   ")), "请选择剔除理由")
        self.assertIn("剔除理由不能超过", trades.validate_signal_ignore(
            self.base(reason="很长的理由" * 20)))
        self.assertIn("剔除理由补充不能超过", trades.validate_signal_ignore(
            self.base(reason_note="补充" * 300)))

    def test_symbol_and_name_required(self):
        self.assertEqual(trades.validate_signal_ignore(self.base(symbol="")), "缺少股票代码")
        self.assertEqual(trades.validate_signal_ignore(self.base(name="")), "缺少股票名称")

    def test_signal_date_normalized_and_not_future(self):
        self.assertEqual(trades.validate_signal_ignore(self.base(signal_date="")), "信号日期无效")
        self.assertEqual(trades.validate_signal_ignore(self.base(signal_date="2026/01/05")), "信号日期无效")
        self.assertEqual(trades.validate_signal_ignore(self.base(signal_date=_today(1))),
                         "信号日期不能晚于今天")
        rec = trades.create_signal_ignore(self.user_id, self.base(signal_date="20260105"))
        try:
            self.assertEqual(rec["signal_date"], "2026-01-05", "紧凑写法须归一, 否则字典序比较会打挂")
        finally:
            trades.delete_signal_ignore(rec["id"])

    def test_optional_prices(self):
        # 全部留空合法; 只填一个也合法
        self.assertIsNone(trades.validate_signal_ignore(self.base()))
        self.assertIsNone(trades.validate_signal_ignore(self.base(take_profit=12.5)))
        self.assertIsNone(trades.validate_signal_ignore(self.base(stop_loss=9.5)))
        self.assertIsNone(trades.validate_signal_ignore(self.base(current_price=10.5)))
        # 非空须 > 0
        for key, label in (("current_price", "现价"), ("take_profit", "止盈价"),
                           ("stop_loss", "止损价"), ("breakeven", "保本价")):
            self.assertEqual(trades.validate_signal_ignore(self.base(**{key: 0})),
                             f"{label}必须大于 0")
            self.assertEqual(trades.validate_signal_ignore(self.base(**{key: "x"})),
                             f"{label}无效")
        # 已填的风控价之间顺序不能反 (缺项不比较)
        self.assertIsNone(trades.validate_signal_ignore(
            self.base(take_profit=12, breakeven=10.5, stop_loss=9)))
        self.assertIsNone(trades.validate_signal_ignore(self.base(take_profit=12, stop_loss=9)))
        self.assertEqual(trades.validate_signal_ignore(self.base(take_profit=9, stop_loss=12)),
                         "须满足止盈价 > 保本价 > 止损价")
        self.assertEqual(trades.validate_signal_ignore(
            self.base(take_profit=12, breakeven=13)), "须满足止盈价 > 保本价 > 止损价")
        self.assertEqual(trades.validate_signal_ignore(
            self.base(take_profit=12, stop_loss=12.0)), "须满足止盈价 > 保本价 > 止损价",
            "止盈等于止损不是合法顺序")

    # ── CRUD ──
    def test_create_and_read_back(self):
        rec = trades.create_signal_ignore(self.user_id, self.base(
            symbol=" 600000.sh ", current_price=10.5, take_profit=12, breakeven=10.8, stop_loss=9.5,
            reason_note="分时量能不足"))
        try:
            self.assertEqual(rec["symbol"], "600000.SH", "代码须去空格并大写")
            self.assertEqual(rec["name"], "平安银行")
            self.assertEqual(rec["model_id"], self.model_id)
            self.assertEqual(rec["model_name"], "T 测试模型")
            self.assertTrue(rec["model_active"])
            self.assertEqual(rec["created_by"], "ignore_admin")
            self.assertEqual(rec["reason_note"], "分时量能不足")
            self.assertAlmostEqual(rec["take_profit"], 12.0)
            self.assertEqual(rec["breakeven"], 10.8)
            got = trades.get_signal_ignore(rec["id"])
            self.assertEqual(got["stop_loss"], 9.5)
        finally:
            trades.delete_signal_ignore(rec["id"])

    def test_update_is_partial_and_can_clear_prices(self):
        rec = trades.create_signal_ignore(self.user_id, self.base(
            current_price=10.5, take_profit=12, breakeven=10.8, stop_loss=9.5))
        try:
            # 只改理由 → 其余字段保留
            updated = trades.update_signal_ignore(rec["id"], {"reason": "估值过高"})
            self.assertEqual(updated["reason"], "估值过高")
            self.assertEqual(updated["reason_note"], None)
            self.assertAlmostEqual(updated["take_profit"], 12.0)
            self.assertEqual(updated["symbol"], "000001.SZ")
            # 现价/风控价可清空 (空串 = 未填)
            cleared = trades.update_signal_ignore(rec["id"], {
                "take_profit": "", "breakeven": "", "stop_loss": "", "current_price": ""})
            self.assertIsNone(cleared["take_profit"])
            self.assertIsNone(cleared["breakeven"])
            self.assertIsNone(cleared["stop_loss"])
            self.assertIsNone(cleared["current_price"])
        finally:
            trades.delete_signal_ignore(rec["id"])

    def test_update_rejects_bad_payload_and_missing_row(self):
        rec = trades.create_signal_ignore(self.user_id, self.base())
        try:
            with self.assertRaises(ValueError):
                trades.update_signal_ignore(rec["id"], {"reason": ""})
            with self.assertRaises(ValueError):
                trades.update_signal_ignore(rec["id"], {"model_id": ""})
            self.assertIsNone(trades.update_signal_ignore(999999, {"reason": "其他"}))
        finally:
            trades.delete_signal_ignore(rec["id"])

    def test_delete(self):
        rec = trades.create_signal_ignore(self.user_id, self.base())
        self.assertTrue(trades.delete_signal_ignore(rec["id"]))
        self.assertIsNone(trades.get_signal_ignore(rec["id"]))
        self.assertFalse(trades.delete_signal_ignore(rec["id"]))

    def test_soft_deleted_model_keeps_record(self):
        rec = trades.create_signal_ignore(self.user_id, self.base(model_id=self.other_model_id))
        try:
            self.assertFalse(rec["model_active"], "模型已停用仍要能读到 (历史记录不可丢)")
            records, _ = trades.list_signal_ignores({"q": rec["symbol"]})
            self.assertTrue(any(r["id"] == rec["id"] for r in records))
            # 停用模型名与启用中不重名时仍可继续用它更新
            again = trades.update_signal_ignore(rec["id"], {"reason_note": "补充"})
            self.assertEqual(again["reason_note"], "补充")
        finally:
            trades.delete_signal_ignore(rec["id"])

    # ── 过滤 / 分页 ──
    def test_filters_and_pagination(self):
        ids = []
        rows = [
            ("600519.SH", "贵州茅台", self.model_id, "量能不足", _today(-1)),
            ("300750.SZ", "宁德时代", self.model_id, "估值过高", _today(-5)),
            ("002472.SZ", "双环传动", self.other_model_id, "位置过高/追高风险", _today(-10)),
        ]
        try:
            for symbol, name, mid, reason, day in rows:
                ids.append(trades.create_signal_ignore(self.user_id, self.base(
                    symbol=symbol, name=name, model_id=mid, reason=reason, signal_date=day))["id"])
            records, total = trades.list_signal_ignores({"q": "600519"})
            self.assertEqual(total, 1)
            self.assertEqual(records[0]["symbol"], "600519.SH")
            # q 覆盖名称与理由
            self.assertEqual(trades.list_signal_ignores({"q": "宁德"})[1], 1)
            self.assertEqual(trades.list_signal_ignores({"q": "追高"})[1], 1)
            # 模型过滤 + 日期区间
            self.assertEqual(trades.list_signal_ignores({"model_id": self.model_id})[1], 2)
            self.assertEqual(
                trades.list_signal_ignores({"model_id": self.other_model_id})[0][0]["symbol"],
                "002472.SZ")
            self.assertEqual(trades.list_signal_ignores({"from": _today(-6)})[1], 2)
            self.assertEqual(trades.list_signal_ignores(
                {"from": _today(-6), "to": _today(-2)})[1], 1)
            # 排序: 信号日期倒序
            all_records, _ = trades.list_signal_ignores({"q": ""})
            days = [r["signal_date"] for r in all_records]
            self.assertEqual(days, sorted(days, reverse=True))
            # 分页: total 为过滤后总数, 不是当前页条数
            page1, total1 = trades.list_signal_ignores({"limit": 1, "offset": 0})
            page2, _ = trades.list_signal_ignores({"limit": 1, "offset": 1})
            self.assertEqual(total1, len(all_records))
            self.assertEqual(len(page1), 1)
            self.assertNotEqual(page1[0]["id"], page2[0]["id"])
            # 非法 limit/offset 回落默认值, 不抛异常
            safe, _ = trades.list_signal_ignores({"limit": "abc", "offset": -5})
            self.assertEqual(len(safe), len(all_records))
            # 非法 model_id 忽略该条件 (不报错)
            self.assertEqual(trades.list_signal_ignores({"model_id": "x"})[1], len(all_records))
        finally:
            for i in ids:
                trades.delete_signal_ignore(i)


class SignalIgnoreApiTest(unittest.TestCase):
    """API 层: 仅管理员可用 + 固定校验文案。"""

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls._db = Path(cls._tmpdir.name) / "test_ignores_api.db"
        cls._orig_db = trades._db_path
        trades.init_db(cls._db)
        trades.create_user("ig_admin", "password123", is_admin=True)
        trades.create_user("ig_user", "password123", is_admin=False)
        cls.model_id = trades.create_model("T API 模型", "", 3)
        from app import create_app
        cls.app = create_app()
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls):
        trades._db_path = cls._orig_db
        cls._tmpdir.cleanup()

    def _headers(self):
        self.client.get("/login.html")
        c = self.client.get_cookie("csrf_token")
        return {"X-CSRF-Token": c.value if hasattr(c, "value") else str(c)}

    def _login(self, username):
        r = self.client.post(
            "/api/auth/login",
            data=json.dumps({"username": username, "password": "password123"}),
            content_type="application/json", headers=self._headers(),
        )
        self.assertEqual(r.status_code, 200, r.data)

    def _logout(self):
        self.client.post("/api/auth/logout", headers=self._headers())

    def _body(self, **over):
        body = {"symbol": "600519.SH", "name": "贵州茅台", "model_id": self.model_id,
                "reason": "量能不足", "signal_date": _today(-1)}
        body.update(over)
        return body

    def test_requires_login(self):
        self.assertEqual(self.client.get("/api/signal-ignores").status_code, 401)
        self.assertEqual(self.client.get("/api/ignore-reasons").status_code, 401)

    def test_non_admin_is_forbidden(self):
        self._login("ig_user")
        try:
            self.assertEqual(self.client.get("/api/signal-ignores").status_code, 403)
            self.assertEqual(self.client.get("/api/ignore-reasons").status_code, 403)
            r = self.client.post("/api/signal-ignores", data=json.dumps(self._body()),
                                 content_type="application/json", headers=self._headers())
            self.assertEqual(r.status_code, 403)
        finally:
            self._logout()

    def test_admin_crud_roundtrip(self):
        self._login("ig_admin")
        headers = self._headers()
        try:
            r = self.client.get("/api/ignore-reasons")
            self.assertEqual(r.status_code, 200)
            self.assertIn("量能不足", r.get_json()["reasons"])
            self.assertNotIn("突破买入", r.get_json()["reasons"], "忽视理由不应混入买入理由")

            before = self.client.get("/api/signal-ignores").get_json()["total"]
            r = self.client.post("/api/signal-ignores", data=json.dumps(self._body(take_profit=2000)),
                                 content_type="application/json", headers=headers)
            self.assertEqual(r.status_code, 201, r.data)
            iid = r.get_json()["ignore"]["id"]
            self.assertEqual(r.get_json()["ignore"]["model_name"], "T API 模型")

            data = self.client.get("/api/signal-ignores").get_json()
            self.assertEqual(data["total"], before + 1)
            row = next(x for x in data["ignores"] if x["id"] == iid)
            self.assertEqual(row["symbol"], "600519.SH")
            self.assertEqual(row["reason"], "量能不足")
            self.assertAlmostEqual(row["take_profit"], 2000)
            self.assertIsNone(row["stop_loss"])

            r = self.client.put(f"/api/signal-ignores/{iid}",
                                data=json.dumps({"reason": "估值过高"}),
                                content_type="application/json", headers=headers)
            self.assertEqual(r.status_code, 200, r.data)
            self.assertEqual(r.get_json()["ignore"]["reason"], "估值过高")
            self.assertAlmostEqual(r.get_json()["ignore"]["take_profit"], 2000, msg="未提供的字段应保留")

            self.assertEqual(self.client.put(f"/api/signal-ignores/{iid}",
                                             data=json.dumps({"reason": ""}),
                                             content_type="application/json",
                                             headers=headers).status_code, 400)
            self.assertEqual(self.client.delete(f"/api/signal-ignores/{iid}",
                                                headers=headers).status_code, 200)
            self.assertEqual(self.client.delete(f"/api/signal-ignores/{iid}",
                                                headers=headers).status_code, 404)
            self.assertEqual(
                self.client.get("/api/signal-ignores").get_json()["total"], before)
        finally:
            self._logout()

    def test_create_returns_fixed_error_messages(self):
        self._login("ig_admin")
        try:
            cases = [
                ({"model_id": ""}, "请选择模型"),
                ({"model_id": 999999}, "模型不存在"),
                ({"reason": ""}, "请选择剔除理由"),
                ({"signal_date": _today(3)}, "信号日期不能晚于今天"),
                ({"take_profit": 9, "stop_loss": 12}, "须满足止盈价 > 保本价 > 止损价"),
            ]
            for over, msg in cases:
                r = self.client.post("/api/signal-ignores", data=json.dumps(self._body(**over)),
                                     content_type="application/json", headers=self._headers())
                self.assertEqual(r.status_code, 400, f"{over} 应被拒绝: {r.data}")
                self.assertEqual(r.get_json()["error"], msg)
            r = self.client.post("/api/signal-ignores", data="{", content_type="application/json",
                                 headers=self._headers())
            self.assertEqual(r.status_code, 400)
        finally:
            self._logout()


if __name__ == "__main__":
    unittest.main(verbosity=2)
