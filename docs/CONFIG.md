# Shore 配置说明

> 所有变量写在 `backend/.env`（从 `backend/.env.example` 复制）。  
> 完整字段定义见 `backend/app/core/config.py`。

---

## 1. 基础

> **`.env` 打不开 / 卡死？** 敏感项已拆到 `backend/.env.secrets`（Token、密码、Webhook）。
> 日常改配置只编辑 `.env`；密钥改 `.env.secrets`。两个文件都在 `backend/` 下。

| 变量 | 必填 | 说明 |
|------|:----:|------|
| `ENVIRONMENT` | 建议 | `dev` / `test` / `prod` / `futures` / `development`，前端左上角展示 |
| `DEBUG` | 否 | 开发调试用 |
| `LOG_LEVEL` | 否 | `INFO` / `DEBUG` |
| `SECRET_KEY` | 生产必填 | JWT 签名，至少 32 字符随机串 |
| `AUTH_REQUIRED` | 否 | `true` 时 API 要登录；测试可 `false` |
| `ADMIN_PASSWORD` | 否 | 初始管理员密码（bootstrap 用） |
| `CORS_ORIGINS` | 否 | JSON 数组，默认 `["http://localhost:3456"]` |
| `PUBLIC_URL` | 建议 | 对外访问地址，Slack 链接用 |

**验证：** `curl localhost:8000/api/health` → `environment` 字段正确。

---

## 2. 数据库 & Redis

| 变量 | 必填 | 说明 |
|------|:----:|------|
| `DATABASE_URL` | 是 | 异步连接串，`postgresql+asyncpg://...`（不要填 psycopg2） |
| `DATABASE_URL_SYNC` | 是 | 同步连接串，`postgresql+psycopg2://...`（不要填裸 `postgresql://`，SQLAlchemy 2.1 会走 psycopg3） |
| `REDIS_URL` | 是 | 默认 `redis://localhost:6379/0` |

docker compose 自带 postgres/redis，生产建议用托管实例。

---

## 3. K8s 资源发现

| 变量 | 必填 | 说明 |
|------|:----:|------|
| `KUBECONFIG` | 接 K8s 时 | 容器内 kubeconfig 路径 |
| `K8S_IN_CLUSTER` | 否 | 跑在集群里设 `true` |
| `K8S_CONTEXT` | 否 | 指定 context，空则用 current |
| `CLUSTER_NAME` | 否 | 展示名 |
| `DISCOVERY_INTERVAL` | 否 | 扫描周期（秒），默认 300 |

**挂载示例（docker compose api 服务）：**

```yaml
volumes:
  - ~/.kube/config-test:/home/appuser/.kube/config:ro
environment:
  K8S_CONTEXT: test-cluster
  CLUSTER_NAME: test
```

**验证：** 拓扑页 `/topology` 能看到 Namespace / Deployment。

---

## 4. 可观测性（只读）

| 变量 | 必填 | 说明 |
|------|:----:|------|
| `PROMETHEUS_URL` | 可选 | 指标查询，Agent 诊断时用 |
| `PROMETHEUS_SSL_VERIFY` | 否 | 默认 `false`；公网有效证书可设 `true` |
| `PROMETHEUS_USERNAME` | nginx 鉴权时 | Basic Auth 用户名 |
| `PROMETHEUS_PASSWORD` | nginx 鉴权时 | Basic Auth 密码，建议放 `.env.secrets` |
| `ES_HOST` | 可选 | Elasticsearch 地址 |
| `ES_PORT` | 否 | 默认 9200 |
| `ES_USER` / `ES_PASSWORD` | 可选 | ES 认证 |

不配也能跑，只是诊断 Agent 少几条数据源。

---

## 5. LLM & RAG

LLM 层使用 **OpenAI 兼容 HTTP API**（LangChain `ChatOpenAI`），可对接 DeepSeek、OpenAI、Azure OpenAI、本地 vLLM / Ollama 等。

| 变量 | 必填 | 说明 |
|------|:----:|------|
| `LLM_API_KEY` | 建议 | API Key，放 `.env.secrets` |
| `LLM_BASE_URL` | 否 | 推理端点，如 `https://api.deepseek.com/v1` |
| `LLM_MODEL` | 否 | 模型 ID，如 `deepseek-chat`、`gpt-4o-mini` |
| `LLM_TEMPERATURE` | 否 | 默认 `0.3` |
| `LLM_MAX_TOKENS` | 否 | 默认 `4096` |

**兼容旧配置**：若未设 `LLM_API_KEY`，会自动读取 `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL`。

示例（DeepSeek）：

```env
LLM_API_KEY=sk-xxx
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
```

示例（OpenAI）：

```env
LLM_API_KEY=sk-xxx
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini
```

| 变量 | 必填 | 说明 |
|------|:----:|------|
| `EMBEDDING_MODEL` | 否 | 本地 embedding 模型名 |
| `RERANK_MODEL` | 否 | 精排模型 |
| `AUTO_DIAGNOSE_ON_ALERT` | 否 | 告警进来是否自动调诊断 |

RAG 模型首次启动会下载，镜像里可预装。

---

## 6. 事件中心 & Slack

| 变量 | 必填 | 说明 |
|------|:----:|------|
| `SLACK_WEBHOOK_URL` | 可选 | 事件生命周期通知 |
| `ALERTMANAGER_WEBHOOK_TOKEN` | 可选 | Alertmanager 回调鉴权 |

Alertmanager 配 webhook 指向：

```
POST /api/incidents/webhook/alertmanager
Header: Authorization: Bearer <ALERTMANAGER_WEBHOOK_TOKEN>
```

**验证：** 手动 POST 一条告警，事件中心出现记录。

---

## 7. 发布管理（GitLab CI + ArgoCD）

### 7.1 只读 CI 扫描（推荐，不用改业务 CI）

| 变量 | 必填 | 说明 |
|------|:----:|------|
| `GITLAB_CI_SCAN_ENABLED` | 否 | 默认 `true` |
| `GITLAB_URL` | 是 | 如 `gitlab.example.com` |
| `GITLAB_TOKEN` | 是 | 只读 Token（`read_api`） |
| `GITLAB_GROUP_ID` | 二选一 | 扫整个 Group |
| `GITLAB_PROJECTS` | 二选一 | 逗号分隔 path |
| `GITLAB_CI_BRANCHES` | 否 | 默认 `dev,test,main` |
| `GITLAB_CI_SCAN_INTERVAL` | 否 | 秒，默认 120 |
| `GITLAB_CI_MAX_PIPELINE_AGE_HOURS` | 否 | 太老的 pipeline 忽略 |
| `GITLAB_SSL_VERIFY` | 否 | 内网自签证书可 `false` |

### 7.2 Webhook（可选，实时推送）

| 变量 | 说明 |
|------|------|
| `GITLAB_WEBHOOK_TOKEN` | GitLab Project/Group Webhook 密钥 |

### 7.3 构建观测 & 回滚

| 变量 | 默认 | 说明 |
|------|------|------|
| `BUILD_OBSERVE_MODE` | `true` | 只观测，不接管部署 |
| `AUTO_ROLLBACK` | `true` | 构建失败时尝试 ArgoCD 回滚 |
| `BUILD_HISTORY_PER_SERVICE` | `1` | 每服务只留最新一条 |
| `BUILD_NOTIFY_SLOW` | `false` | 慢构建 Slack（已关，失败才通知） |
| `DEPLOY_VERIFY_WAIT` | `60` | 部署后等待秒数再验 Pod |

### 7.4 ArgoCD（按分支环境）

| 变量 | 说明 |
|------|------|
| `ARGOCD_DEV_SERVER` / `ARGOCD_TEST_SERVER` | 各环境 ArgoCD 地址 |
| `ARGOCD_DEV_USERNAME` / `PASSWORD` / `TOKEN` | 认证 |
| `ARGOCD_CLI_EXTRA` | 如 `--plaintext --insecure --grpc-web` |
| `ARGOCD_SSL_VERIFY` | 是否校验证书 |

旧单环境变量 `ARGOCD_SERVER` / `ARGOCD_TOKEN` 仍兼容。

**验证：** `/deployments` 能看到各服务最新 pipeline 状态。

---

## 8. 日志监控

| 变量 | 默认 | 说明 |
|------|------|------|
| `LOG_MONITOR_ENABLED` | `true` | 总开关 |
| `LOG_MONITOR_DIR` | 空 | 监控目录，空则用 `backend/logs/monitor_logs` |

把各服务日志文件 symlink 或挂载到该目录，按文件名匹配服务。  
告警走 Slack（同上 `SLACK_WEBHOOK_URL`），前端在 `/logs` 看规则、S3 历史和下载。

---

## 8.1 集群巡检（shark-Platform inspection）

| 变量 | 默认 | 说明 |
|------|------|------|
| `INSPECTION_ENABLED` | `true` | 每日 Prometheus 清单巡检 |
| `INSPECTION_CRON` | `0 8 * * *` | 默认同 shark-Platform：每天 08:00 |

Prometheus URL 优先用巡检配置页，其次 `environments.json` / `PROMETHEUS_URL`。AI 分析可用巡检页 ark_* 或平台 `LLM_*`。页面 `/monitoring`。清单语义见 [INSPECTION.md](./INSPECTION.md)。

---

## 9. 安全扫描

| 变量 | 默认 | 说明 |
|------|------|------|
| `SCAN_TOOLS_PATH` | `/usr/local/bin` | nmap、nuclei、httpx 等 |
| `AUTO_SCAN_CRON` | `0 3 * * *` | 定时全平台扫描 |
| `SECURITY_SCAN_EXTRA_TARGETS` | 空 | 额外域名/IP，逗号分隔 |

靶标自动从 K8s Ingress、NodePort、中间件配置里挖，也可手动补。  
页面 `/security` → 漏洞扫描 / 渗透测试。

---

## 10. Ops Agent 配置

| 变量 | 默认 | 说明 |
|------|------|------|
| `MONITOR_PATROL_ENABLED` | `true` | 监控巡检定时任务 |
| `MONITOR_PATROL_INTERVAL` | `900` | 巡检间隔（秒） |
| `COST_ANALYSIS_ENABLED` | `true` | 每日成本分析 |
| `COST_CPU_CORE_HOUR_USD` | `0.04` | CPU 核·小时单价（估算用） |
| `COST_MEM_GB_HOUR_USD` | `0.005` | 内存 GB·小时单价 |
| `ARCHITECTURE_CRON` | `0 9 * * 1` | 架构评估 cron |
| `OPS_AUTO_REMEDIATE` | `false` | 允许自动执行 Pod 重启等修复 |

API 前缀 `/api/ops/`：监控、成本、排班、助手、编排、修复方案等。

---

## 11. 前端环境名

部署时在 **frontend** 容器注入：

```yaml
environment:
  NEXT_PUBLIC_ENV: test
```

与后端 `ENVIRONMENT` 保持一致即可。

---

## 12. 多环境配置

一套 Shore 可管理 **多个环境**（dev / test / prod），配置文件：

`backend/config/environments.json`（参考 `environments.example.json`）

```json
{
  "default": "test",
  "environments": [
    {
      "id": "dev",
      "label": "Dev",
      "cluster_name": "dev",
      "k8s_context": "dev",
      "prometheus_url": "https://prometheus.dev.example.com",
      "prometheus_username": "prometheus",
      "es_host": "192.168.1.10",
      "gitlab_ci_branches": ["dev"],
      "argocd_server": "argocd.dev.example.com"
    },
    {
      "id": "test",
      "label": "Test",
      "cluster_name": "test",
      "k8s_context": "test",
      "prometheus_url": "https://prometheus.test.example.com",
      "es_host": "192.168.1.11",
      "gitlab_ci_branches": ["test", "main"],
      "argocd_server": "argocd.test.example.com"
    }
  ]
}
```

| 字段 | 说明 |
|------|------|
| `default` | 首次进入 / 未切换时的默认环境 id |
| `id` | 环境标识，UI 切换与 API 请求头 `X-Shore-Environment` |
| `cluster_name` | K8s 发现写入的 Cluster 名，拓扑/首页按此过滤 |
| `k8s_context` | kubeconfig 中的 context |
| `prometheus_url` / `es_host` | 该环境的观测数据源 |
| `gitlab_ci_branches` | 可选，标记该环境关注的 CI 分支 |
| `argocd_server` | 文档用途；回滚仍按 Git **分支** 选 ArgoCD |

**密码**（Prometheus / ES）仍用全局 `backend/.env.secrets`，各环境共用。

**切换环境：** 侧边栏下拉，或 `PUT /api/settings/environments/active` `{"id":"dev"}`。当前环境持久化在 Redis。

**发现任务：** 定时任务会对 JSON 里 **每个 enabled 环境** 各跑一遍 K8s Discovery（不同 context → 不同 cluster 入库）。

无 `environments.json` 时自动回退为 `.env` 单环境模式。

---

## 常见问题

**改了 .env 没生效？**  
`docker compose up -d api` 重启 API；改前端变量要 rebuild frontend。

**GitLab 扫不到 pipeline？**  
查 Token 权限、Group ID、分支名是否在 `GITLAB_CI_BRANCHES` 里。

**日志告警太多误报？**  
在 `/logs` 调规则，或看 `log_keyword_match.py` 里的过滤逻辑。

**生产 checklist**

- [ ] `SECRET_KEY` 换成随机串  
- [ ] `AUTH_REQUIRED=true`  
- [ ] `DEBUG=false`  
- [ ] Slack / GitLab Token 用 Secret 注入，别提交 `.env`
