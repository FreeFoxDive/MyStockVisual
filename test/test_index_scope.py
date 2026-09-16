# -*- coding: utf-8 -*-
"""index.html 内联脚本作用域自查: 函数体里引用的标识符必须有出处。

为什么需要: 这个文件没有构建/打包/lint 步骤, 静态文件就是线上跑的代码。把一段代码从
A 函数挪到 B 函数时, 很容易漏掉只属于 A 的局部变量 —— 真实踩过: 抽 cacheGridRects()
时把 cw/ch 和 spanPx 一起带走, updateChart 里剩下的引用直到浏览器里才炸
`ReferenceError: spanPx is not defined` (单测只覆盖抽出来的纯函数, 覆盖不到这种
"函数体引用了别的函数的局部变量")。

判据 (保守, 只求不漏报关键错): 函数体内出现的标识符必须属于
  (a) 文件顶层声明 (缩进 0 的 function/const/let/var/class, 含箭头函数常量);
  (b) 该函数体内声明 (含形参/解构/剩余参数/catch/for-of 的 const/let, 嵌套函数取其并集);
  (c) 允许的宿主与 JS 内置全局。
其余一律报出 —— 包括"在别的函数里声明过"这种跨作用域引用。

自测: 本文件末尾会把顶层 spanPx 声明删掉再跑一遍, 断言能报出来 (防止检查器悄悄失效)。

运行:
    venv/Scripts/python.exe -u visual/test/test_index_scope.py
"""

import re
import sys
import unittest
from pathlib import Path

_VISUAL_DIR = Path(__file__).resolve().parents[1]
_TEST_DIR = Path(__file__).resolve().parent
# 与 test_quote_poll_js 同一套引导: 直接跑文件 / discover / `-m unittest visual.test.x`
# 三种方式都要能 import 到公共工具
for _p in (str(_VISUAL_DIR), str(_TEST_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from js_test_util import inline_script_text  # noqa: E402

INDEX_HTML = _VISUAL_DIR / "static" / "index.html"

# JS 内置 + 浏览器宿主 + 本页依赖的第三方全局 (ECharts/Vue/自研 UMD 模块)
ALLOWED_GLOBALS = {
    # JS 内置
    "Array", "Boolean", "Date", "Error", "Function", "Infinity", "JSON", "Map", "Math",
    "NaN", "Number", "Object", "Promise", "Proxy", "Reflect", "RegExp", "Set", "String",
    "Symbol", "TypeError", "URL", "URLSearchParams", "WeakMap", "WeakSet", "decodeURIComponent",
    "encodeURIComponent", "isFinite", "isNaN", "parseFloat", "parseInt", "undefined", "NaN",
    "console", "globalThis", "structuredClone",
    # 浏览器宿主
    "AbortController", "Blob", "CustomEvent", "DOMParser", "Document", "Element", "Event",
    "FileReader", "HTMLElement", "Image", "IntersectionObserver", "MutationObserver",
    "Node", "Notification", "ResizeObserver", "Screen", "URL", "WebSocket", "Worker",
    "alert", "atob", "btoa", "cancelAnimationFrame", "clearInterval", "clearTimeout",
    "confirm", "crypto", "devicePixelRatio", "document", "fetch", "getComputedStyle",
    "history", "innerHeight", "innerWidth", "localStorage", "location", "matchMedia",
    "navigator", "performance", "prompt", "queueMicrotask", "requestAnimationFrame",
    "screen", "sessionStorage", "setInterval", "setTimeout", "window",
    # 本页加载的全局模块 (vendor + static/js 的 UMD 导出, 见各自 root.X / global.X)
    "ChipChart", "Drawings", "GapScanner", "PatternScanner", "VisualApi", "VisualGaps",
    "VisualLive", "VisualMarketStore", "VisualTheme", "Vue", "Pinia", "echarts",
}

# 关键字 / 字面量, 不是标识符引用
KEYWORDS = {
    "arguments", "async", "await", "break", "case", "catch", "class", "const", "continue",
    "debugger", "default", "delete", "do", "else", "export", "extends", "false", "finally",
    "for", "from", "function", "get", "if", "import", "in", "instanceof", "let", "new",
    "null", "of", "return", "set", "static", "super", "switch", "this", "throw", "true",
    "try", "typeof", "var", "void", "while", "with", "yield",
}

_IDENT_RE = re.compile(r"[A-Za-z_$][\w$]*")
_DECL_RE = re.compile(r"\b(?:const|let|var|function|class)\s+([A-Za-z_$][\w$]*)")
_TOP_DECL_RE = re.compile(r"^(?:async\s+)?(?:function|const|let|var|class)\s+([A-Za-z_$][\w$]*)", re.M)


def _read_scripts() -> str:
    """页面所有内联脚本拼成一份 (按顺序, 便于统一检查作用域)。

    用 HTMLParser 解析而不是正则匹配 <script>: 正则处理不好引号/属性顺序与
    `</script >` 这类变体 (CodeQL py/bad-tag-filter 正是盯这种写法)。
    """
    return inline_script_text(INDEX_HTML)


def _clean(text: str) -> str:
    """把注释/字符串/模板串的字面量/正则字面量涂成空格 (保留长度与换行)。

    涂掉是为了让后面的花括号配平与标识符扫描不被字符串内容带偏 (整页脚本里有 HTML 模板
    串和正则, 裸扫必然错位)。但模板串里的 `${...}` 是**代码**, 必须保留 —— 那里既有调用
    也有声明, 整段涂掉会把真正的引用一起漏掉; 插值里还可能再嵌模板, 所以递归处理。
    """
    out = list(text)
    n = len(text)
    prev = [""]   # 最近一个"代码"字符, 用于判断 '/' 是正则还是除号

    def blank(a, b):
        for k in range(max(0, a), min(b, n)):
            if out[k] != "\n":
                out[k] = " "

    def scan_code(i, stop_at_brace=False):
        depth = 0   # 插值内的花括号深度: 只有深度归零的 '}' 才是插值结束
        while i < n:
            c = text[i]
            nxt = text[i + 1] if i + 1 < n else ""
            if c == "{":
                if stop_at_brace:
                    depth += 1
                i += 1
                continue
            if c == "}":
                if stop_at_brace:
                    if depth == 0:
                        return i
                    depth -= 1
                i += 1
                continue
            if c == "/" and nxt == "/":
                j = text.find("\n", i)
                j = n if j < 0 else j
                blank(i, j); i = j; continue
            if c == "/" and nxt == "*":
                j = text.find("*/", i + 2)
                j = n if j < 0 else j + 2
                blank(i, j); i = j; continue
            if c == "`":
                i = scan_template(i); prev[0] = "0"; continue
            if c in "'\"":
                q = c
                j = i + 1
                while j < n:
                    if text[j] == "\\":
                        j += 2
                        continue
                    if text[j] == q:
                        break
                    j += 1
                j = min(j + 1, n); blank(i, j); i = j; prev[0] = "0"; continue
            if c == "/" and (prev[0] == "" or prev[0] in "(,=:[!&|?{};+-*%<>~"):
                j = i + 1
                in_class = False
                while j < n:
                    if text[j] == "\\":
                        j += 2
                        continue
                    if text[j] == "[":
                        in_class = True
                    elif text[j] == "]":
                        in_class = False
                    elif text[j] == "/" and not in_class:
                        break
                    elif text[j] == "\n":
                        break
                    j += 1
                k = j + 1
                while k < n and text[k].isalpha():   # 正则 flag (/.../gi)
                    k += 1
                blank(i, k); i = k; prev[0] = "0"; continue
            if not c.isspace():
                prev[0] = c
            i += 1
        return i

    def scan_template(i):
        i += 1
        seg = i
        while i < n:
            c = text[i]
            if c == "\\":
                i += 2
                continue
            if c == "`":
                blank(seg, i)
                return i + 1
            if c == "$" and i + 1 < n and text[i + 1] == "{":
                blank(seg, i + 1)                    # 插值前的字面量 + `$` (留下 `{` 供配平)
                i = scan_code(i + 2, stop_at_brace=True) + 1
                seg = i
                continue
            i += 1
        blank(seg, n)
        return n

    scan_code(0)
    return "".join(out)


def _body_of(src: str, open_brace: int) -> tuple:
    """从 '{' 开始按花括号配平取出函数体, 返回 (body, end_index); 传 _clean 后的文本。"""
    depth = 0
    for i in range(open_brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[open_brace:i + 1], i + 1
    raise AssertionError("花括号不配对")


def _iter_functions(src: str):
    """产出顶层函数 (含箭头函数常量): (名字, 函数体, 形参文本); 传 _clean 后的文本。"""
    patterns = [
        re.compile(r"^(?:async\s+)?function\s*([A-Za-z_$][\w$]*)?\s*\(", re.M),
        re.compile(r"^const\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^()]*\)|[A-Za-z_$][\w$]*)\s*=>\s*\{", re.M),
        re.compile(r"^const\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?function\s*\(", re.M),
    ]
    seen = set()
    for pat in patterns:
        for m in pat.finditer(src):
            name = m.group(1) or "<anonymous>"
            brace = src.find("{", m.end() - 1)
            if brace < 0:
                continue
            params = src[m.end():brace]
            body, _ = _body_of(src, brace)
            key = (name, brace)
            if key in seen:
                continue
            seen.add(key)
            yield name, body, params


def _scan_identifiers(clean: str):
    """产出 (名字, 前一个非空字符, 后一个非空字符); 传 _clean 后的文本, 无需再处理字符串。"""
    out = []
    for m in _IDENT_RE.finditer(clean):
        name = m.group(0)
        k = m.start() - 1
        while k >= 0 and clean[k].isspace():
            k -= 1
        before = clean[k] if k >= 0 else ""
        if before.isdigit():
            continue          # 科学计数法的尾巴 (1e4 → "e4"), 不是标识符
        k = m.end()
        while k < len(clean) and clean[k].isspace():
            k += 1
        after = clean[k] if k < len(clean) else ""
        out.append((name, before, after))
    return out


def _collect_locals(body: str, params: str) -> set:
    """函数体内声明的名字。

    形参/解构/剩余参数 + const/let/var/function/class + catch 形参 + 箭头形参; 另外两种
    容易漏的写法单独兜住 —— 数组/对象解构 (`const [a, b] = ...`) 与一条里逗号声明多个
    (`const a = 1, b = 2`); 漏了会把这些名字误报成跨作用域引用。
    """
    names = set(_IDENT_RE.findall(params))
    names |= set(_DECL_RE.findall(body))
    names |= set(re.findall(r"catch\s*\(\s*([A-Za-z_$][\w$]*)\s*\)", body))
    for m in re.finditer(r"\b(?:const|let|var)\s+([^=;]{1,200}?)=", body):
        names |= set(_IDENT_RE.findall(m.group(1)))   # 解构 / 多个声明符
    for m in re.finditer(r"function\s*[A-Za-z_$]*\s*\(([^)]*)\)", body):
        names |= set(_IDENT_RE.findall(m.group(1)))   # 体内嵌套的匿名函数形参
    for m in re.finditer(r"([A-Za-z_$][\w$]*)\s*=>", body):
        names.add(m.group(1))
    for m in re.finditer(r"\(([^()]*)\)\s*=>", body):
        names |= set(_IDENT_RE.findall(m.group(1)))
    for m in re.finditer(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*=(?!=)", body):
        names.add(m.group(1))
    return names


def find_scope_issues(src: str) -> list:
    """返回 [(函数名, 标识符, 上下文片段)] —— 引用了既非本函数局部、也非顶层/全局的名字。"""
    clean = _clean(src)
    top_level = set(_TOP_DECL_RE.findall(clean))
    issues = []
    for name, body, params in _iter_functions(clean):
        locals_ = _collect_locals(body, params)
        for ident, before, after in _scan_identifiers(body):
            if ident in locals_ or ident in top_level or ident in ALLOWED_GLOBALS:
                continue
            if ident in KEYWORDS:
                continue
            if before == ".":          # 成员访问 obj.foo
                continue
            if after == ":":           # 对象字面量的键 { foo: ... }
                continue
            if before in "{," and after == "(":   # 对象方法简写 { foo() {} }
                continue
            if after == "=" and before in "{,":   # 解构默认值 (绑定, 非引用)
                continue
            at = body.find(ident)
            issues.append((name, ident, body[max(0, at - 40):at + 30].strip()))
    return issues


class InlineScriptScopeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = _read_scripts()

    def test_scripts_found(self):
        self.assertGreater(len(self.src), 10000, "没抓到内联脚本, 检查器失效了")

    def test_no_cross_scope_references(self):
        issues = find_scope_issues(self.src)
        detail = "\n".join(f"  {fn}() 引用未定义 {ident}: {ctx}" for fn, ident, ctx in issues[:20])
        self.assertEqual(issues, [], f"函数体引用了不在作用域内的标识符:\n{detail}")

    def test_checker_catches_moved_declaration(self):
        """自测: 顶层 spanPx 删掉后必须能被报出来 (真实踩过的那个错)。"""
        broken = re.sub(r"^function spanPx\(.*?\n\}\n", "", self.src, count=1, flags=re.S | re.M)
        self.assertNotEqual(broken, self.src, "没能构造出坏样本")
        self.assertTrue(any(i[1] == "spanPx" for i in find_scope_issues(broken)),
                        "检查器漏报: 跨作用域引用没被检出")


if __name__ == "__main__":
    unittest.main(verbosity=2)
