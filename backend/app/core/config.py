# ============================================================
# app/core/config.py — 全局配置（pydantic-settings）
# 单实例多环境：backend/config/environments.json 定义 dev/test/prod 等；
# 当前环境存 Redis，UI 可切换。无 JSON 时回退 .env 单环境。
# ============================================================

from functools import lru_cache
import os
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", ".env.secrets"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- App ----
    APP_NAME: str = "Shore"
    APP_VERSION: str = "0.1.0"
    # 当前实例所属环境（部署时注入，用于展示与资源发现范围）
    ENVIRONMENT: Literal["dev", "test", "prod", "futures", "development"] = "development"
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"

    # ---- Database (PostgreSQL + pgvector) ----
    DATABASE_URL: str = "postgresql+asyncpg://shore:shore@localhost:5432/shore"
    DATABASE_URL_SYNC: str = "postgresql://shore:shore@localhost:5432/shore"
    DB_POOL_SIZE: int = 20
    DB_MAX_OVERFLOW: int = 10

    # ---- Redis (缓存 + Pub/Sub，替代 Kafka/Celery) ----
    REDIS_URL: str = "redis://localhost:6379/0"

    # ---- Observability ----
    PROMETHEUS_URL: str = "http://localhost:9090"
    PROMETHEUS_SSL_VERIFY: bool = False   # 内网/自签证书设为 false
    PROMETHEUS_USERNAME: str = ""           # nginx Basic Auth 用户名
    PROMETHEUS_PASSWORD: str = ""           # nginx Basic Auth 密码（建议放 .env.secrets）
    # Elasticsearch (日志检索，复用现有 ES)
    ES_HOST: str = ""
    ES_PORT: int = 9200
    ES_USER: str = "elastic"
    ES_PASSWORD: str = ""

    # ---- LLM（OpenAI 兼容 API：DeepSeek / OpenAI / Azure / 本地 vLLM 等） ----
    LLM_API_KEY: str = ""
    LLM_BASE_URL: str = ""                  # 空则按 provider 默认
    LLM_MODEL: str = ""                       # 空则按 provider 默认
    LLM_TEMPERATURE: float = 0.3
    LLM_MAX_TOKENS: int = 4096
    # 兼容旧变量名（仅配置 DEEPSEEK_* 时自动回退）
    DEEPSEEK_API_KEY: str = ""
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com/v1"
    DEEPSEEK_MODEL: str = "deepseek-chat"

    # ---- RAG (本地 embedding / rerank，零 API 费) ----
    EMBEDDING_MODEL: str = "BAAI/bge-small-zh-v1.5"
    RERANK_MODEL: str = "BAAI/bge-reranker-v2-m3"
    EMBEDDING_DIM: int = 512
    RAG_TOP_K: int = 10
    RAG_RERANK_TOP_N: int = 3

    # ---- Auth (JWT) ----
    # 生产必须通过环境变量注入，禁止使用默认值
    SECRET_KEY: str = Field(default="dev-only-insecure-key-change-in-production-min-32c")
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 480  # 8h
    AUTH_REQUIRED: bool = False  # prod 建议 True
    ALERTMANAGER_WEBHOOK_TOKEN: str = ""
    SLACK_WEBHOOK_URL: str = ""
    AUTO_DIAGNOSE_ON_ALERT: bool = True

    # ---- Infra Discovery ----
    K8S_IN_CLUSTER: bool = False       # 部署到 K8s 内时用 in-cluster SA；凭证 otherwise 走 DB
    CLUSTER_NAME: str = "default"   # 集群名称（前端展示用；配置为本环境实际名称）
    DISCOVERY_INTERVAL: int = 300   # 自动发现周期（秒）

    # ---- Security Scan (合并自 PentestAgent) ----
    SCAN_TOOLS_PATH: str = "/usr/local/bin"
    SCAN_TIMEOUT: int = 3600
    MAX_CONCURRENT_SCANS: int = 3
    # 全平台自动扫描周期（cron 表达式，每天凌晨3点）
    AUTO_SCAN_CRON: str = "0 3 * * *"
    # 全平台扫描额外目标（逗号分隔域名/IP/Ingress），discovery 为空时仍可扫
    SECURITY_SCAN_EXTRA_TARGETS: str = ""

    # ---- 发布管理 (GitLab CI 外挂观测 / 可选 ArgoCD 回滚) ----
    # ---- 发布管理 / ArgoCD（按 dev/test 分支区分） ----
    GITLAB_WEBHOOK_TOKEN: str = ""
    ARGOCD_DEV_SERVER: str = ""
    ARGOCD_DEV_USERNAME: str = "admin"
    ARGOCD_DEV_PASSWORD: str = ""
    ARGOCD_DEV_TOKEN: str = ""              # 可填静态 token，优先于 password
    ARGOCD_TEST_SERVER: str = ""
    ARGOCD_TEST_USERNAME: str = "admin"
    ARGOCD_TEST_PASSWORD: str = ""
    ARGOCD_TEST_TOKEN: str = ""
    ARGOCD_CLI_EXTRA: str = "--plaintext --insecure --grpc-web"
    ARGOCD_SSL_VERIFY: bool = False
    # 兼容旧单环境变量
    ARGOCD_SERVER: str = ""
    ARGOCD_TOKEN: str = ""
    ARGOCD_USERNAME: str = "admin"
    ARGOCD_PASSWORD: str = ""
    DEPLOY_VERIFY_WAIT: int = 60
    AUTO_ROLLBACK: bool = True              # 构建失败时回滚已部署版本
    BUILD_OBSERVE_MODE: bool = True         # True=仅观测 CI，不接管部署
    BUILD_SLOW_THRESHOLD_SEC: int = 600     # 超过此秒数视为慢构建
    BUILD_SLOW_RATIO: float = 1.5           # 或超过历史均值此倍数
    BUILD_HISTORY_PER_SERVICE: int = 1      # 每服务只保留最新一条
    BUILD_NOTIFY_SLOW: bool = False         # 慢构建 Slack 通知（默认关，仅失败通知）

    # ---- GitLab CI 只读扫描（无需改各项目 .gitlab-ci.yml） ----
    GITLAB_CI_SCAN_ENABLED: bool = True
    GITLAB_URL: str = ""                    # 如 gitlab.test.exc888.org 或 https://gitlab.example.com
    GITLAB_TOKEN: str = ""                  # 只读 Personal Access Token / Project Access Token
    GITLAB_GROUP_ID: str = ""               # Group ID，扫描组内全部项目（含子组）
    GITLAB_PROJECTS: str = ""               # 额外项目 path，逗号分隔，如 exchange-uc,group/exchange-order
    GITLAB_CI_BRANCHES: str = "dev,test,main"
    GITLAB_CI_SCAN_INTERVAL: int = 120      # 扫描间隔（秒）
    GITLAB_CI_PIPELINES_PER_REF: int = 1      # 每分支取最近 N 条 finished pipeline
    GITLAB_CI_MAX_PIPELINE_AGE_HOURS: int = 72  # 超过此时间的 pipeline 不扫描、不通知
    GITLAB_SSL_VERIFY: bool = True

    # ---- Log Monitor (合并自 shark-Platform) ----
    LOG_MONITOR_ENABLED: bool = True
    LOG_MONITOR_DIR: str = ""          # 空则使用 backend/logs/monitor_logs
    LOG_MONITOR_MAX_LINES_PER_POLL: int = 8000   # 单 Pod 单次轮询最大行数
    LOG_MONITOR_MAX_RAW_BYTES: int = 2 * 1024 * 1024  # 单 Pod 单次 raw 缓冲上限（字节）
    LOG_MONITOR_MAX_ALERTS_PER_BATCH: int = 30   # 单次流处理最大告警数
    LOG_MONITOR_VIEW_PAGE_SIZE: int = 500        # API 默认分页行数
    LOG_MONITOR_VIEW_MAX_PAGE_SIZE: int = 2000   # API 单页最大行数
    PUBLIC_URL: str = "http://localhost:3456"  # Slack 告警 deep link

    # ---- System Inspection (合并自 shark-Platform inspection) ----
    INSPECTION_ENABLED: bool = True
    INSPECTION_CRON: str = "0 8 * * *"   # 每日 08:00

    # ---- Ops Agents ----
    MONITOR_PATROL_ENABLED: bool = True
    MONITOR_PATROL_INTERVAL: int = 900       # 秒，默认 15 分钟
    COST_ANALYSIS_ENABLED: bool = True
    COST_CPU_CORE_HOUR_USD: float = 0.04
    COST_MEM_GB_HOUR_USD: float = 0.005
    ARCHITECTURE_CRON: str = "0 9 * * 1"     # 每周一 9:00
    OPS_AUTO_REMEDIATE: bool = False         # 自动修复需显式开启

    # ---- CORS ----
    CORS_ORIGINS: list[str] = ["http://localhost:3456"]

    @property
    def log_monitor_dir(self) -> str:
        if self.LOG_MONITOR_DIR:
            return self.LOG_MONITOR_DIR
        return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "logs", "monitor_logs")

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "prod"

    def gitlab_ci_branches(self) -> list[str]:
        return [b.strip() for b in self.GITLAB_CI_BRANCHES.split(",") if b.strip()]

    def gitlab_project_list(self) -> list[str]:
        return [p.strip() for p in self.GITLAB_PROJECTS.split(",") if p.strip()]

    def security_scan_extra_targets(self) -> list[str]:
        return [t.strip() for t in self.SECURITY_SCAN_EXTRA_TARGETS.split(",") if t.strip()]

    @property
    def llm_api_key(self) -> str:
        return (self.LLM_API_KEY or self.DEEPSEEK_API_KEY).strip()

    @property
    def llm_base_url(self) -> str:
        if self.LLM_API_KEY.strip():
            return (self.LLM_BASE_URL or "https://api.openai.com/v1").strip()
        if self.DEEPSEEK_API_KEY.strip():
            return self.DEEPSEEK_BASE_URL.strip()
        return (self.LLM_BASE_URL or self.DEEPSEEK_BASE_URL or "https://api.openai.com/v1").strip()

    @property
    def llm_model(self) -> str:
        if self.LLM_API_KEY.strip():
            return (self.LLM_MODEL or "gpt-4o-mini").strip()
        if self.DEEPSEEK_API_KEY.strip():
            return self.DEEPSEEK_MODEL.strip()
        return (self.LLM_MODEL or self.DEEPSEEK_MODEL or "gpt-4o-mini").strip()

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
