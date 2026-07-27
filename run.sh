#!/bin/bash
# Visual K线图 — 一键启动 (Linux/Mac 服务器)
# 用法: ./run.sh [端口号, 默认8888] [host, 默认本机IP]

set -e
PORT=${1:-8888}
HOST=${2:-$(hostname -I 2>/dev/null | awk '{print $1}')}
HOST=${HOST:-localhost}
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

cd "$SCRIPT_DIR"

if [ ! -d "venv" ]; then
    echo "[init] 创建虚拟环境..."
    python3 -m venv venv
fi

source venv/bin/activate

if ! pip show alphafeed > /dev/null 2>&1; then
    echo "[init] 安装依赖..."
    pip install -r requirements.txt -q
fi

echo "[run] 启动 Visual K线图服务..."
echo "[run] 监听: http://$HOST:$PORT"
python -u server.py --host "$HOST" --port "$PORT"
