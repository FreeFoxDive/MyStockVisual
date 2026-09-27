FROM python:3.12-slim

# 创建非root用户 (固定 UID=1000 对齐宿主机 data/.cache 属主, 可用 --build-arg UID 覆盖)
ARG UID=1000
RUN groupadd -r appuser && useradd -r -g appuser -u ${UID} -m -d /app appuser

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 放在 pip 之后: site-packages 里已编好的 pyc 保留 (启动快), 只是不再让运行期
# 往容器可写层新写 __pycache__
ENV PYTHONDONTWRITEBYTECODE=1

# 构建守卫: 上下文混进宿主机目录就直接失败, 而不是静静产出一个 +300MB 的层
# (正常由 .dockerignore 拦掉; 这里防的是排除规则被改坏, 或 build context 指错目录)
RUN test ! -e venv && test ! -e .venv && test ! -e .cache && test ! -e data || \
    (echo "ERROR: 上下文混入宿主机目录 (venv/ .venv/ .cache/ data/), 检查 .dockerignore" >&2; exit 1)

# --chown 在复制时即确定属主。不要改回 "COPY . . + RUN chown -R appuser:appuser /app":
# OverlayFS 对整棵树 chown 会触发 copy-up, 每次构建多出一份与 COPY 等大的层 (实测 ≈305MB)。
COPY --chown=appuser:appuser . .

# 只补构建上下文里没有的两个目录 (运行期由 compose 的 bind mount 覆盖)
RUN mkdir -p .cache data && chown appuser:appuser .cache data

EXPOSE 8888
# 健康检查只提供可见性 (docker ps 的 health 列); restart: unless-stopped 只响应
# 进程退出, 不会因 unhealthy 自动重启 —— 卡死自愈靠应用内 watchdog
# (WATCHDOG_EXIT_ON_STALL=1 时由进程主动退出触发重启)。
# python-slim 无 curl, 用 python 探测; 禁用代理避免 .env 中 http_proxy 干扰。
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request,sys;o=urllib.request.build_opener(urllib.request.ProxyHandler({}));sys.exit(0 if o.open('http://127.0.0.1:8888/api/health',timeout=8).status==200 else 1)"
USER appuser
CMD ["python", "-u", "server.py", "--host", "0.0.0.0", "--port", "8888"]