# 2026-10-12 经官方镜像仓库核验的 Python 运行时摘要。
# 联网机器完成全部依赖安装后导出 AMD64 镜像，现场只导入和运行。
ARG NEWS_PYTHON_BASE=python:3.12.13-slim-trixie@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36
FROM ${NEWS_PYTHON_BASE}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    HOME=/home/news \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

COPY requirements.txt /opt/news/requirements.txt

RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-noto-cjk tzdata \
    && rm -rf /var/lib/apt/lists/*

# 仅联网准备时使用逐一核验官方 SHA-256 的本地 Linux AMD64 依赖缓存。
ARG NEWS_PYTHON_CACHE_URL
RUN if [ -n "${NEWS_PYTHON_CACHE_URL:-}" ]; then \
        python -m pip install --no-cache-dir --no-index --find-links "$NEWS_PYTHON_CACHE_URL" \
            --trusted-host "$(python -c 'from urllib.parse import urlsplit; import sys; print(urlsplit(sys.argv[1]).hostname or "")' "$NEWS_PYTHON_CACHE_URL")" \
            -r /opt/news/requirements.txt; \
    else \
        python -m pip install --no-cache-dir -r /opt/news/requirements.txt; \
    fi \
    && python -m pip check

# 使用独立服务账户，避开 Debian 预置的 news 系统账户。
RUN groupadd --gid 10001 newscollector \
    && useradd --uid 10001 --gid 10001 --home-dir /home/news --create-home --shell /usr/sbin/nologin newscollector \
    && mkdir -p /app/config /var/lib/news/data /var/lib/news/state \
    && chown -R newscollector:newscollector /home/news /var/lib/news

# 分别缓存系统依赖与浏览器文件，下载重试时保留已经安装的依赖。
RUN python -m playwright install-deps chromium \
    && rm -rf /var/lib/apt/lists/*

# 仅联网准备时可使用已校验的本地官方文件缓存，该地址不进入运行配置。
ARG PLAYWRIGHT_DOWNLOAD_HOST
RUN PLAYWRIGHT_DOWNLOAD_HOST="${PLAYWRIGHT_DOWNLOAD_HOST:-}" \
    python -m playwright install --only-shell chromium

# 将程序放在依赖层之后，更新时复用已核验的固定运行依赖。
ARG NEWS_VERSION
ARG NEWS_ARTIFACT_SHA256
ARG NEWS_SOURCE_SHA256
ARG NEWS_RUN_UID=10001
ARG NEWS_RUN_GID=10001

# 现场配置由 Compose 只读挂载，不进入镜像或构建上下文。
COPY --chown=${NEWS_RUN_UID}:${NEWS_RUN_GID} news-${NEWS_VERSION}.pyz /app/news.pyz
# 兼容现场 Docker 20 的经典构建器，权限在同一正式层中显式固定。
RUN chmod 0444 /app/news.pyz \
    && chown "${NEWS_RUN_UID}:${NEWS_RUN_GID}" /home/news /var/lib/news/data /var/lib/news/state \
    && chmod 0750 /var/lib/news/state

ENV NEWS_CONFIG_FILE=/app/config/news.env \
    NEWS_VERSION=${NEWS_VERSION} \
    NEWS_ARTIFACT_SHA256=${NEWS_ARTIFACT_SHA256} \
    NEWS_SOURCE_SHA256=${NEWS_SOURCE_SHA256}

USER ${NEWS_RUN_UID}:${NEWS_RUN_GID}
WORKDIR /var/lib/news/data
ENTRYPOINT ["python", "/app/news.pyz"]

LABEL org.opencontainers.image.title="新闻网采集服务" \
      org.opencontainers.image.version="${NEWS_VERSION}" \
      news.artifact.sha256="${NEWS_ARTIFACT_SHA256}" \
      news.source.sha256="${NEWS_SOURCE_SHA256}"
