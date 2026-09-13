FROM python:3.12-slim

# 创建非root用户 (固定 UID=1000 对齐宿主机 data/.cache 属主, 可用 --build-arg UID 覆盖)
ARG UID=1000
RUN groupadd -r appuser && useradd -r -g appuser -u ${UID} -m -d /app appuser

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# 确保 .cache / data 目录可写
RUN mkdir -p .cache data && chown -R appuser:appuser /app

EXPOSE 8888
# 健康检查只提供可见性 (docker ps 的 health 列); restart: unless-stopped 只响应
# 进程退出, 不会因 unhealthy 自动重启 —— 卡死自愈靠应用内 watchdog
# (WATCHDOG_EXIT_ON_STALL=1 时由进程主动退出触发重启)。
# python-slim 无 curl, 用 python 探测; 禁用代理避免 .env 中 http_proxy 干扰。
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request,sys;o=urllib.request.build_opener(urllib.request.ProxyHandler({}));sys.exit(0 if o.open('http://127.0.0.1:8888/api/health',timeout=8).status==200 else 1)"
USER appuser
CMD ["python", "-u", "server.py", "--host", "0.0.0.0", "--port", "8888"]