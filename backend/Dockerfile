# ============================================================
# Shore Backend Dockerfile — 多阶段构建，精简镜像
# ============================================================

# 基础镜像已通过 `docker pull` 拉到本地缓存，buildkit 直接命中，不走网络
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 安全扫描工具：nmap (apt) + nuclei/subfinder/httpx (Go 二进制)
RUN apt-get update && apt-get install -y --no-install-recommends \
    nmap curl ca-certificates wget unzip \
    && rm -rf /var/lib/apt/lists/*

# ProjectDiscovery 工具（nuclei/subfinder/httpx）——从 release 下载预编译二进制
# 自动适配架构（amd64 / arm64），失败不中断 build
RUN set -eux; \
    case "$(dpkg --print-architecture)" in \
      amd64) PDARCH=amd64;; \
      arm64) PDARCH=arm64;; \
      *) PDARCH=amd64;; \
    esac; \
    for tool in nuclei subfinder httpx; do \
      case $tool in \
        nuclei) ver=3.3.7;; \
        subfinder) ver=2.6.6;; \
        httpx) ver=1.6.9;; \
      esac; \
      wget -q "https://github.com/projectdiscovery/${tool}/releases/download/v${ver}/${tool}_${ver}_linux_${PDARCH}.zip" -O /tmp/${tool}.zip || true; \
      if [ -f /tmp/${tool}.zip ]; then unzip -o /tmp/${tool}.zip -d /usr/local/bin/ ${tool} 2>/dev/null || true; rm -f /tmp/${tool}.zip; fi; \
    done
# nuclei 模板库很大，构建期 `nuclei -update-templates` 经常要数分钟，且默认写到 /root，
# 镜像以 appuser 运行时用不上。首次扫描前在容器内自行更新即可。

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 非 root 运行（安全第一）
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
