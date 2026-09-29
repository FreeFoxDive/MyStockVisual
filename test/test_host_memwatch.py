"""宿主机事件监听接线；以假的 docker/通知函数验证，不访问外部服务。"""
import os
import shutil
import subprocess
import unittest
from pathlib import Path


class HostMemwatchTest(unittest.TestCase):
    def test_oom_event_reaches_notification(self):
        # WindowsApps/bash.exe 是 WSL 启动器，Git Bash 才能运行本地 shell 脚本。
        git_bash = Path(r"C:\Program Files\Git\bin\bash.exe")
        bash = str(git_bash) if os.name == "nt" and git_bash.exists() else (
            shutil.which("bash") if os.name != "nt" else None)
        if not bash:
            self.skipTest("bash unavailable")
        source = (Path(__file__).resolve().parents[1] / "scripts/host_memwatch.sh").read_text(
            encoding="utf-8")
        begin = source.index("cmd_events() {")
        end = source.index('\ncase "${1:-}"', begin)
        script = (
            "set -u\nCONTAINER=stock-visual\nWINDOW=600\nRESTART_MAX=3\n"
            "docker() { echo '1234567890 oom stock-visual 137'; }\n"
            "ntfy_send() { echo \"NOTIFIED: $1\"; }\n"
            "note_event() { echo 1; }\n"
            + source[begin:end] + "\ncmd_events < /dev/null\n")
        result = subprocess.run([bash, "-c", script], capture_output=True, text=True,
                                encoding="utf-8", timeout=5, check=True)
        self.assertIn("NOTIFIED: stock-visual OOM", result.stdout)
