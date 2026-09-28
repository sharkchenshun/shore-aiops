# Shore — 测试环境接入指南

> Version 0.1.0 | 2026-08-10
>
> 按功能模块说明怎么把 Shore 接到你的测试环境。  
> 环境变量速查见 [CONFIG.md](CONFIG.md)。

---

## 目录

1. [基础部署 — docker compose 起全栈](#1-基础部署)
2. [环境标识 — NEXT_PUBLIC_ENV](#2-环境标识)
3. [K8s 自动发现 — Discovery Engine](#3-k8s-自动发现)
4. [可观测性 — Prometheus + Elasticsearch 日志检索](#4-可观测性)
5. [AI 引擎 — LLM + RAG 本地 Embedding](#5-ai-引擎)
6. [事件中心 — Prometheus Alert → Incident 全生命周期](#6-事件中心)
7. [发布管理 — GitLab CI Webhook + ArgoCD](#7-发布管理)
8. [安全巡查 — 全平台自动巡检](#8-安全巡查)

---

## 1. 基础部署

### 你需要什么

- Docker 20+ / Docker Compose 2+
- 测试环境有 PostgreSQL 16（可复用现有）；无则 docker compose 自带

### 操作

```bash
cd shore
cp backend/.env.example backend/.env
# 编辑 backend/.env（最少配置见下面）
docker compose up -d --build
```

### 验证

```bash
curl http://localhost:8000/api/health       # → {"status":"ok",...}
curl http://localhost:8000/api/ready        # → {"status":"ready","checks":{"database":"ok"}}
curl http://localhost:3456                  # → 前端 Dashboard，HTTP 200
```

### 最小 .env 配置

```env
ENVIRONMENT=test                          # ← 改成你的环境名
LLM_API_KEY=sk-xxxx                       # ← LLM Key（必填，OpenAI 兼容 API）
SECRET_KEY=change-me-to-random-32-chars   # ← 生产必改
```

---

## 2. 环境标识

### 设计

Shore 按 **单环境独立部署** 设计。不提供界面环境切换器。每个环境（test / prod / futures）部署一个独立实例，环境名通过环境变量注入。

### 需要改什么

**部署时注入** `NEXT_PUBLIC_ENV` 和 `ENVIRONMENT`：

```yaml
# docker compose — frontend service:
environment:
  NEXT_PUBLIC_ENV: test

# docker compose — api service:
environment:
  ENVIRONMENT: test
```

### 验证

打开前端 → 左上角显示环境名；`curl http://localhost:8000/api/health` 返回 `"environment":"test"`

---

## 3. K8s 自动发现

### 工作原理

Discovery Engine 每 5 分钟自动：
1. 列出 Namespace → Deployment → Service → Pod
2. 从 Endpoints / ConfigMap / 环境变量 提取 **K8s → VM 中间件桥接依赖**（如 `spring.datasource.url` → `mysql://192.168.1.100`）
3. Upsert 到 PostgreSQL（幂等）

### 需要改什么

**给 API 容器挂载 kubeconfig**：

```yaml
# docker compose — api service:
volumes:
  - /Users/you/.kube/config-futures:/home/appuser/.kube/config:ro
environment:
  KUBECONFIG: /home/appuser/.kube/config
  DISCOVERY_INTERVAL: 300                    # 秒，默认 300
```

> 部署到 K8s 内：设置 `K8S_IN_CLUSTER=true`，用 ServiceAccount 自动发现（无需 kubeconfig）。

### 验证

```bash
# 查看发现的集群
curl http://localhost:8000/api/topology/clusters
# → [{"name":"futures-k8s","provider":"kubernetes","health":"healthy","nodeCount":9}]

# 查看拓扑图
curl http://localhost:8000/api/topology/graph
# → {"nodes":[{...service/middleware/ingress...}],"edges":[{...}]}
```

打开前端 `/` → 拓扑图显示自动发现的服务和中间件。

### 如果没有 K8s / 还用 SSH VM

`SSH_VM` Provider 可通过 SSH 直连探测 VM 上的中间件进程（MySQL/Redis/RabbitMQ 端口）：

```env
SSH_HOST=192.168.1.100
SSH_USER=root
SSH_KEY_PATH=/home/appuser/.ssh/id_rsa    # 优先密钥
```

---

## 4. 可观测性

### 工作原理

- **Prometheus**：复用现有 Prometheus，API 直接查询 PromQL（不采集、不写入）
- **Elasticsearch**：复用现有 ES，专用于日志全文搜索和错误模式聚合。Agent 诊断时自动拉取异常时段日志

### 需要改什么

```env
PROMETHEUS_URL=http://your-prometheus:9090
# Elasticsearch（复用现有）
ES_HOST=192.168.12.111
ES_PORT=9200
ES_USER=elastic
ES_PASSWORD=your-es-password
```

### 验证

```bash
# ES 连接测试（直接在容器内）
docker compose exec api python3 -c "
from app.services.elasticsearch import search_logs
import asyncio
results = asyncio.run(search_logs('ERROR', hours_back=24))
print(f'返回 {len(results)} 条日志')
"
```

> 不配 ES_HOST 时，日志搜索返回空数组，Agent 诊断功能不受影响（核心走 Prometheus 指标）。前端监控面板 `/monitoring` 展示集群健康+Prometheus 接入引导。

---

## 5. AI 引擎

### 5.1 LLM（OpenAI 兼容 API）

支持 DeepSeek、OpenAI、Azure OpenAI、本地 vLLM 等任意 OpenAI 兼容端点。

```env
LLM_API_KEY=sk-xxxx                          # ← 必填
LLM_BASE_URL=https://api.deepseek.com/v1     # 按 provider 修改
LLM_MODEL=deepseek-chat
```

旧变量 `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL` 仍可用，未设 `LLM_API_KEY` 时自动回退。

### 5.2 RAG 本地 Embedding（可选）

```env
# 默认 BAAI/bge-small-zh-v1.5（512 维，中文优化）
EMBEDDING_MODEL=BAAI/bge-small-zh-v1.5
RERANK_MODEL=BAAI/bge-reranker-v2-m3
```

> 这两个模型首次加载时自动从 HuggingFace 下载，不需要 API Key。
> 不装 ML 依赖时（`requirements.txt` 不含 torch），RAG 自动降级 —— 知识的检索/写入返回空，核心诊断/发布/安全功能不受影响。
> 要启用 RAG：`pip install -r requirements-ml.txt`（增加 ~2GB torch 依赖）

### 验证

```bash
curl -X POST http://localhost:8000/api/incidents/<any-incident-id>/diagnose
# → {"status":"diagnosis_started","incident_id":"..."}
# 后台 AI 分析 → 完成后 curl GET /api/incidents/<id> 可看到 reports
```

---

## 6. 事件中心

### 事件来源

目前 Shore **不主动采集 Prometheus 告警**，事件有四种入站方式：

| 来源 | 说明 |
|:-----|:-----|
| **Prometheus Alertmanager Webhook**（推荐） | Alertmanager 配置 webhook→Shore API |
| **手动创建** | 前端 / API 直接 POST 创建 |
| **安全扫描自动生成** | 漏洞高危自动建 Incident |
| **部署失败自动生成** | 发布回滚时自动建 Incident |

### 需要改什么

**Alertmanager 配置**（`alertmanager.yml`）：

```yaml
receivers:
  - name: 'shore'
    webhook_configs:
      - url: 'http://api:8000/api/incidents/alertmanager'
        send_resolved: true
        http_config:
          headers:
            - key: X-Alert-Token
              value: '<ALERTMANAGER_WEBHOOK_TOKEN>'
```

> 后端已实现 `POST /api/incidents/alertmanager`。配置 `ALERTMANAGER_WEBHOOK_TOKEN` 后需在 Header 携带 `X-Alert-Token`。告警 ingest 后可自动触发 AI 诊断（`AUTO_DIAGNOSE_ON_ALERT=true`）。

### 手动创建事件（验证）

```bash
curl -X POST http://localhost:8000/api/incidents \
  -H 'Content-Type: application/json' \
  -d '{"title":"exchange-market 延迟告警","severity":"warning","source":"prometheus","affected_services":["exchange-market"]}'
```

### 事件如何流转

```
firing → acknowledged → analyzing (触发 AI 诊断) → analyzed → resolved
```

### 触发 AI 诊断

```bash
curl -X POST http://localhost:8000/api/incidents/<incident-id>/diagnose
# → 后台运行 Diagnosis Agent（RAG 检索历史 → ReAct → 结构化根因报告）
```

### 验证

打开前端 `/incidents` → 左侧事件列表、右侧详情+AI 分析+Agent 执行记录。

---

## 7. 发布管理（GitLab 只读扫描 — 无需改 CI）

### 设计

**各项目 `.gitlab-ci.yml` 完全不用改。** Shore 用只读 GitLab Token 定时扫描：

1. 列出 Group 内全部项目（或指定项目列表）
2. 拉取 `dev/test/main` 分支最近完成的 pipeline
3. 解析各 job 耗时（prepare / package / build / deploy …）
4. 失败 → 拉 job 日志 + AI 分析 + 可选 ArgoCD 回滚 + 事件中心 + Slack
5. 偏慢 → 对比历史耗时 + AI 优化建议
6. 每服务只展示**最新构建**，历史在该项目内保留

扫描默认每 **120 秒** 一次；新 pipeline 自动去重（按 `project_id + pipeline_id`）。

### 需要改什么

#### 7.1 Shore `.env`（只需配 GitLab 只读账号）

```env
GITLAB_CI_SCAN_ENABLED=true
GITLAB_URL=gitlab.test.exc888.org          # 或 https://gitlab.xxx.com
GITLAB_TOKEN=glpat-xxxx                    # 只读 Token，scope: read_api
GITLAB_GROUP_ID=123                        # 服务所在 Group ID（推荐）
# 或额外指定项目 path（逗号分隔）：
GITLAB_PROJECTS=sre/exchange-uc,jys/exchange-gateway
GITLAB_CI_BRANCHES=dev,test,main
GITLAB_CI_SCAN_INTERVAL=120

SLACK_WEBHOOK_URL=...                      # 可选
AUTO_ROLLBACK=true                         # 构建失败时 ArgoCD 回滚
ARGOCD_SERVER=...
ARGOCD_TOKEN=...
```

**GitLab Token 权限**：Personal Access Token，勾选 `read_api`（只读即可）。

**查 Group ID**：GitLab → Group → Settings → 页面底部 ID，或 `GET /api/v4/groups?search=xxx`。

#### 7.2 无需改 CI

exchange-uc 等项目的 `.gitlab-ci.yml` **保持原样**。

### API

| 方法 | 路径 | 说明 |
|------|------|------|
| — | 后台定时 | 每 `GITLAB_CI_SCAN_INTERVAL` 秒自动扫描 |
| `POST` | `/api/deployments/scan-gitlab` | 手动触发扫描 |
| `GET` | `/api/deployments/scan-gitlab/status` | 扫描配置状态 |
| `GET` | `/api/deployments?view=latest` | 每服务最新构建 |
| `GET` | `/api/deployments/service/{name}/history` | 项目内历史 |

可选：仍支持 `POST /api/deployments/webhook` 主动回调，但**不是必需**。

### 验证

```bash
# 1. 检查 GitLab 连通（设置页 integrations 或）
curl http://localhost:8000/api/settings/integrations

# 2. 手动触发扫描
curl -X POST http://localhost:8000/api/deployments/scan-gitlab

# 3. 查看 exchange-uc 最新构建
curl 'http://localhost:8000/api/deployments?view=latest' | jq '.[] | select(.service=="exchange-uc")'
```

前端 `/deployments` 查看各服务最新构建、阶段耗时、失败分析/优化建议。

---

## 8. 安全巡查

### 工作方式

1. **全平台自动巡检**：每天凌晨 3:00（`AUTO_SCAN_CRON`）自动扫描已发现的全部服务/中间件域名和 IP
2. **手动扫描触发**：输入域名/IP，实时扫描 + WebSocket 进度

### 安全 Agent 内部流程

```
目标解析 → subfinder(子域名) → httpx(存活探测) → nmap(端口) → nuclei(CVE)
→ AI 汇总分析 → 漏洞入库 → 高危自动建 Incident
```

### 需要改什么

扫描工具（nmap/nuclei/subfinder/httpx）已打包在 Docker 镜像中，无需额外配置。

```env
# 可选调整
SCAN_TIMEOUT=3600              # 单次扫描超时（秒）
MAX_CONCURRENT_SCANS=3         # 最大并发扫描数
AUTO_SCAN_CRON="0 3 * * *"     # 每日巡检时间（cron）
```

### 验证

```bash
# 手动扫描一个域名
curl -X POST http://localhost:8000/api/security/scans \
  -H 'Content-Type: application/json' \
  -d '{"target":"your-service.example.com","scope":"manual"}'
# → {"status":"started","session_id":"xxx"}

# 查看扫描结果
curl http://localhost:8000/api/security/scans/<session_id>
# → {"assets": [...], "vulnerabilities": [...], "report": "..."}

# 查看所有漏洞
curl http://localhost:8000/api/security/vulnerabilities?severity=critical
```

> 注意：nuclei 需要下载模板库（容器内 `nuclei -update-templates`），已在 Dockerfile 预留但未自动执行（首次需手动或启动脚本触发）。

---

## 本地开发（不用 Docker）

如果你在前端开发时不想每次改代码都重 build Docker：

```bash
# Terminal 1: 起后端基础设施
docker compose up -d postgres redis

# Terminal 2: 本地起 API（不装 torch）
cd backend && python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m app.bootstrap
uvicorn app.main:app --reload --port 8000

# Terminal 3: 本地起前端（你已有的 dev server）
cd shore && npm run dev   # → localhost:3456
```

---

## 常见问题

### Q: Docker Hub 拉不到镜像（502 / Bad Gateway）

Docker Hub registry 不稳定时，用 `docker pull` 走镜像加速再构建：

```bash
docker pull docker.m.daocloud.io/library/python:3.11-slim
docker tag docker.m.daocloud.io/library/python:3.11-slim python:3.11-slim
# 同样操作 node:20-alpine
docker compose up -d --build
```

### Q: bootstrap 报错 "password cannot be longer than 72 bytes"

这是 passlib + bcrypt 4.x 兼容性 bug（已修复）—— Shore 直接用 `bcrypt` 库，不再依赖 passlib。确认使用最新代码即可。

### Q: 部署到 K8s 内

参考 [docker compose](docker-compose.yml)，将 postgres/redis/api/frontend 分别转译为 Deployment + Service。
建议：postgres/redis 用外部托管（云数据库），api/frontend 各 2 副本，env 统一用 Secret/ConfigMap 注入。

---

## 功能接入状态一览

| 功能 | 前端页面 | 所需配置 | 状态 |
|:-----|:---------|:---------|:-----|
| 总览 Dashboard | `/` | Kubeconfig | ✅ |
| 服务拓扑 | `/topology` | Kubeconfig + ES（trace 可选） | ✅ |
| 事件中心 | `/incidents` | Alertmanager webhook 或手动 POST | ✅ |
| Pod 管理 | `/pods` | Kubeconfig | ✅ |
| 发布管理 | `/deployments` | GitLab Token + ArgoCD（回滚可选） | ✅ |
| 日志监控 | `/logs` | K8s kubeconfig + Slack / S3（可选） | ✅ |
| 安全合规 | `/security` | 扫描工具（镜像内已装） | ✅ |
| 监控巡检 | `/monitoring` | Prometheus（清单巡检）+ 可选 LLM | ✅ |
| 成本分析 | `/cost` | 服务发现数据 | ✅ |
| 值班排班 | `/schedules` | 无 | ✅ |
| 运维助手 | `/copilot` | LLM_API_KEY | ✅ |
| 知识库 | `/knowledge` | RAG 模型（可选） | ✅ |
| 平台设置 | `/settings` | 无 | ✅ |
