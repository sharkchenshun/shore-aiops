# Runbook Platform Roadmap

## 产品定位

BestNative 的长期目标是成为 AI SRE Runbook Platform：

```text
资产发现 + 可观测性 + Runbook + 审批 + 审计 + ChatOps + 知识沉淀
```

不是单纯监控平台，也不是普通 AI 聊天，而是面向 SRE 的 AI-native 运维控制台。

## 当前阶段

BestNative 当前已有：

- Next.js 前端
- FastAPI 后端
- PostgreSQL/pgvector/Redis 基础
- K8s discovery 初版
- topology / incidents / deployments / security API 雏形
- RAG / Agent 目录结构
- Prometheus / ES 接入设计

当前不足：

- 鉴权/RBAC/审批不足
- 执行接口过早暴露
- Runbook 没有与真实 Hermes skill 体系打通
- 审计中心未成型
- 真实故障复盘未结构化入库

## 阶段路线

### Phase 0：安全收口

目标：避免平台变成“有 UI 的危险 kubectl”。

任务：

- 全 API 鉴权
- RBAC
- Pod restart 改为审批计划
- 操作审计表设计
- 日志脱敏
- CORS 收紧
- 生产 SECRET_KEY 校验

完成标准：没有未经授权的执行类 API。

### Phase 1：Hermes Ops Kit 接入

目标：把真实 Runbook 方法论接入 BestNative。

任务：

- 引入 env-map schema
- 引入 approval matrix
- 引入 oplog template
- 引入 model routing
- 引入 weekly maintenance checklist
- 写入 integration docs

完成标准：平台文档和数据模型与 Hermes Ops Kit 对齐。

### Phase 2：只读自动发现

目标：安全地生成资产和组件候选。

任务：

- K8s nodes/ns/svc/deploy/sts/pvc discovery
- middleware candidate discovery
- env-map.generated.yaml
- onboarding report
- 人工确认后入库

完成标准：自动发现不直接生效，所有候选可审计。

### Phase 3：Runbook 管理

目标：让 runbook 成为平台一等对象。

任务：

- Runbook 列表
- 风险等级
- 输入参数
- 前置检查
- 审批要求
- 执行计划
- 验证步骤
- 关联 skill/oplog/incident

完成标准：用户可在平台上查看和发起 runbook，但执行仍需审批。

### Phase 4：审计中心

目标：让每次操作可追踪、可回放、可复盘。

任务：

- operation_audit
- approval_requests
- agent_tool_calls
- safety_events
- incident timeline
- UI 查询页面

完成标准：能回答“谁在何时对什么环境做了什么，谁批准，结果如何”。

### Phase 5：ChatOps + Web 协同

目标：手机端和 Web 控制台共享上下文。

任务：

- 微信触发巡检结果回写 BestNative
- Web 上展示手机端处理记录
- Web 发起审批，手机确认
- Hermes Agent 执行结果回写事件中心

完成标准：CLI / 微信 / Web 三端有统一审计链。

### Phase 6：指标反馈

目标：证明平台价值。

任务：

- MTTR 趋势
- Runbook 命中率
- 审批覆盖率
- Oplog 覆盖率
- 重复故障率
- 误判次数
- 模型升级次数

完成标准：平台能量化展示 AI SRE 体系改进效果。

## MVP 定义

最小可用版本不包含自动修复，只包含：

1. 安全鉴权
2. K8s 只读发现
3. Runbook 列表
4. 审批计划生成
5. 操作审计
6. Hermes Ops Kit 文档/模板接入
7. 巡检结果展示

## 不做事项

- 不在 v0.x 直接做全自动修复
- 不把真实 secret 写入数据库
- 不默认接生产写权限
- 不绕过 Hermes 审批机制
- 不为了 UI 牺牲安全边界

## 成功标准

BestNative 成功不是“页面多”，而是：

```text
每个高风险操作都可审批、可审计、可回滚；
每次故障都能沉淀成 Runbook；
每次误判都能转化为下次不再犯的规则。
```
