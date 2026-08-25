FROM python:3.12-slim

# 时区兜底: 代码已按北京时间判断交易时段, 这里再让容器进程级 TZ=Asia/Shanghai
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && ln -snf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && echo Asia/Shanghai > /etc/timezone
ENV TZ=Asia/Shanghai

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
USER appuser
CMD ["python", "-u", "server.py", "--host", "0.0.0.0", "--port", "8888"]