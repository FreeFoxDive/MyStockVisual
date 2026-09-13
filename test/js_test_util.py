# -*- coding: utf-8 -*-
"""Node 前端脚本测试公共工具。"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import unittest

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
VISUAL_DIR = os.path.dirname(TEST_DIR)
PROJECT_DIR = os.path.dirname(VISUAL_DIR)

if VISUAL_DIR not in sys.path:
    sys.path.insert(0, VISUAL_DIR)
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

NODE_AVAILABLE = shutil.which("node") is not None


def require_node():
    if not NODE_AVAILABLE:
        raise unittest.SkipTest("需要 node 才能跑前端镜像测试")


def run_node(script: str, *args: str) -> str:
    """执行 node -e script，额外 argv 传入 process.argv。"""
    require_node()
    proc = subprocess.run(
        ["node", "-e", script, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout or f"node exit {proc.returncode}")
    return proc.stdout.strip()
