# 单阶段镜像：Python 3.12 + ffmpeg
# 目标是 NAS（群晖/威联通/Unraid）和普通服务器都能直接 docker compose up
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    ABF_DATA_DIR=/data \
    ABF_HOST=0.0.0.0 \
    ABF_PORT=8848

# ffmpeg 负责转码、拼接、m4b 章节；tzdata 让日志时间正确
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tzdata curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . \
    && useradd -m -u 1000 forge \
    && mkdir -p /data \
    && chown -R forge:forge /data /app

USER forge

VOLUME ["/data"]
EXPOSE 8848

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8848/api/status > /dev/null || exit 1

CMD ["abf", "serve", "--host", "0.0.0.0", "--port", "8848"]
