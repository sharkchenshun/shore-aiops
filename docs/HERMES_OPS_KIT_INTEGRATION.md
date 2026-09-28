# Hermes Ops Kit Integration

## 定位

BestNative 是 AI SRE Runbook Platform 的平台外壳；Hermes Ops Kit 是方法论、Runbook、模板和安全规则；Hermes Agent 是推理与执行引擎。

三者关系：

```text
BestNative = Web UI / API / 资产 / 事件 / 审批 / 审计
Hermes Ops Kit = env-map / runbook / skill / safety / templates
Hermes Agent = ChatOps / 工具执行 / 推理 / skill 加载
```

## 为什么要集成

BestNative 已有平台雏形：

- Web UI
- FastAPI 后端
- K8s discovery
- Prometheus / ES / RAG 规划
- Agent 目录结构

但当前缺少：

- 真实 runbook 体系
- 审批和审计闭环
- 操作风险分级
- 手机端/CLI 协同边界
- 故障后 skill 化沉淀机制

Hermes Ops Kit 正好补齐这些。

## 集成原则

1. 先只读接入，不直接执行修复
2. 自动发现只生成候选，人工确认后入库
3. 高风险操作默认生成计划，不直接执行
4. 执行动作必须走审批和审计
5. Runbook 来自 Hermes Ops Kit，平台只承载和编排
6. 故障解决后反向沉淀到 skill/oplog/docs

## 分层架构

```text
用户 / 微信 / Web UI
        ↓
BestNative Console
        ↓
Runbook / Approval / Audit API
        ↓
Hermes Ops Kit schema + templates
        ↓
Hermes Agent 执行诊断/修复
        ↓
K8s / MySQL / RabbitMQ / ES / Longhorn
```

## 集成对象

### 1. env-map schema

BestNative 的 discovery 结果应先生成候选：

```text
env-map.generated.yaml
```

再由用户确认后入库或生效。

### 2. approval matrix

引入 Hermes Ops Kit 的审批矩阵：

- L0 只读：可执行，记录 session
- L1 低风险写入：记录 audit
- L2 服务影响：审批 + audit + 观察
- L3 数据/不可逆：二次审批 + 备份 + oplog

### 3. oplog / audit

BestNative 后续应把文件级 oplog 平台化为 Audit Center。

### 4. runbook / skill

BestNative 不直接把 prompt 写死在代码里，而是引用 Runbook：

- 适用场景
- 输入参数
- 只读检查
- 风险等级
- 审批要求
- 执行步骤
- 验证步骤

### 5. model routing

引入 `ops-model-selection`：

- 日常/只读：DeepSeek v4 Pro
- 高风险/复杂根因：GPT-5.5
- 连续失败：停止并升级

## 路线

### Phase 1：文档对齐

- SECURITY_MODEL.md
- RUNBOOK_PLATFORM_ROADMAP.md
- HERMES_OPS_KIT_INTEGRATION.md

### Phase 2：schema 对齐

- env-map schema
- approval matrix
- audit schema
- incident schema

### Phase 3：只读平台能力

- K8s discovery
- topology
- Prometheus/ES 查询
- 巡检结果展示

### Phase 4：审批执行能力

- 生成操作计划
- 人工审批
- Hermes Agent 执行
- audit/oplog 记录
- 变更后观察

### Phase 5：闭环沉淀

- incident 自动归档
- runbook 命中率统计
- skill 更新建议
- 模板 diff 记录

## 禁止路线

- 不要先做无审批的执行 API
- 不要默认给平台生产写权限
- 不要把 kubeconfig/secret 明文写入数据库
- 不要把真实故障日志原样塞进公开案例
- 不要让 Agent 在 Web UI 后台自动执行 L3 操作

## 结论

BestNative 与 Hermes Ops Kit 结合后，才是完整的 AI SRE Runbook Platform：

```text
平台外壳 + Runbook 模板 + AI 执行引擎 + 审批审计闭环
```
