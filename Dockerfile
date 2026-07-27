FROM python:3.12-slim

# 创建非root用户
RUN groupadd -r appuser && useradd -r -g appuser -m -d /app appuser

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# 确保 .cache 目录可写
RUN mkdir -p .cache && chown -R appuser:appuser /app

EXPOSE 8888
USER appuser
CMD ["python", "-u", "server.py", "--host", "0.0.0.0", "--port", "8888"]