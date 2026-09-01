"""日K 快照补 bar 逻辑单测。"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import date
from unittest import mock

import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import market as market_mod


def _df_with_dates(dates, close=10.0):
    idx = pd.to_datetime(dates)
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": 1000,
            "amount": 0.0,
        },
        index=idx,
    )


class TestStripTodayBarDf(unittest.TestCase):
    def test_strip_when_last_is_today(self):
        today = date.today()
        yesterday = (pd.Timestamp(today) - pd.Timedelta(days=1)).date()
        df = _df_with_dates([yesterday.isoformat(), today.isoformat()], close=10.0)
        with mock.patch("market_hours.now") as mn:
            mn.return_value = pd.Timestamp(today)
            out = market_mod._strip_today_bar_df(df)
        self.assertEqual(len(out), 1)
        self.assertEqual(market_mod._last_bar_date(out), yesterday)

    def test_keep_when_last_is_yesterday(self):
        today = date.today()
        yesterday = (pd.Timestamp(today) - pd.Timedelta(days=1)).date()
        df = _df_with_dates([yesterday.isoformat()], close=10.0)
        with mock.patch("market_hours.now") as mn:
            mn.return_value = pd.Timestamp(today)
            out = market_mod._strip_today_bar_df(df)
        self.assertEqual(len(out), 1)


class TestMaybeAppendTodayBar(unittest.TestCase):
    def test_append_when_history_ends_yesterday(self):
        today = date.today()
        yesterday = (pd.Timestamp(today) - pd.Timedelta(days=1)).date()
        df = _df_with_dates([yesterday.isoformat()])
        quote = {
            "date": today.isoformat(),
            "open": 11.0,
            "high": 12.0,
            "low": 10.5,
            "close": 11.5,
            "volume": 5000,
        }
        with mock.patch.object(market_mod, "_daily_bar_from_quote", return_value=quote):
            with mock.patch("market_hours.is_trading_day", return_value=True):
                with mock.patch("market_hours.in_session", return_value=True):
                    out = market_mod._maybe_append_today_bar("000001.SH", df)
        self.assertEqual(len(out), 2)
        self.assertEqual(out.iloc[-1]["close"], 11.5)
        self.assertEqual(out.iloc[-1]["volume"], 5000)

    def test_skip_when_history_has_today_and_session_closed(self):
        today = date.today()
        df = _df_with_dates([today.isoformat()], close=9.0)
        with mock.patch.object(market_mod, "_daily_bar_from_quote") as fq:
            with mock.patch("market_hours.is_trading_day", return_value=True):
                with mock.patch("market_hours.in_session", return_value=False):
                    out = market_mod._maybe_append_today_bar("000001.SH", df)
        fq.assert_not_called()
        self.assertEqual(len(out), 1)
        self.assertEqual(out.iloc[-1]["close"], 9.0)

    def test_refresh_last_bar_when_in_session(self):
        today = date.today()
        df = _df_with_dates([today.isoformat()], close=9.0)
        quote = {
            "date": today.isoformat(),
            "open": 9.0,
            "high": 10.0,
            "low": 8.8,
            "close": 9.6,
            "volume": 8000,
        }
        with mock.patch.object(market_mod, "_daily_bar_from_quote", return_value=quote):
            with mock.patch("market_hours.is_trading_day", return_value=True):
                with mock.patch("market_hours.in_session", return_value=True):
                    out = market_mod._maybe_append_today_bar("510300.SH", df)
        self.assertEqual(len(out), 1)
        self.assertEqual(out.iloc[-1]["close"], 9.6)
        self.assertEqual(out.iloc[-1]["high"], 10.0)

    def test_no_append_on_non_trading_day(self):
        today = date.today()
        yesterday = (pd.Timestamp(today) - pd.Timedelta(days=1)).date()
        df = _df_with_dates([yesterday.isoformat()])
        with mock.patch.object(market_mod, "_daily_bar_from_quote") as fq:
            with mock.patch("market_hours.is_trading_day", return_value=False):
                out = market_mod._maybe_append_today_bar("000001.SH", df)
        fq.assert_not_called()
        self.assertEqual(len(out), 1)


if __name__ == "__main__":
    unittest.main()
