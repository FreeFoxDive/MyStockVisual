# -*- coding: utf-8 -*-
"""前端测试公共工具: Node 脚本执行 + 静态页 HTML 解析。"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import unittest
from html.parser import HTMLParser

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


class InlineScriptParser(HTMLParser):
    """提取内联 <script> 正文 (跳过带 src 的外链脚本)。

    一律解析而不是正则匹配 HTML: 正则处理不好引号/属性顺序与 `</script >` 这类变体
    (CodeQL py/bad-tag-filter 也正是盯这种写法)。多个页面可复用一个实例累积 feed。
    """

    def __init__(self):
        super().__init__()
        self.blocks = []
        self._buf = None

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "script" and not any(k.lower() == "src" for k, _ in attrs):
            self._buf = []

    def handle_data(self, data):
        if self._buf is not None:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "script" and self._buf is not None:
            self.blocks.append("".join(self._buf))
            self._buf = None


def inline_script_text(path) -> str:
    """读一个静态页, 取其中所有内联 <script> 正文并按出现顺序拼成一份。"""
    parser = InlineScriptParser()
    with open(path, encoding="utf-8") as fh:
        parser.feed(fh.read())
    return "\n".join(parser.blocks)

