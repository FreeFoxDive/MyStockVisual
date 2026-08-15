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
USER appuser
CMD ["python", "-u", "server.py", "--host", "0.0.0.0", "--port", "8888"]