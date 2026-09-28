# Security Model

## 目标

BestNative 是运维平台，不是普通 Web 应用。任何执行能力都可能影响集群、数据库和业务服务，因此必须先建立安全模型，再开放执行接口。

核心原则：

```text
默认只读；执行需审批；高风险需二次确认；所有动作可审计。
```

## 当前已知风险

### 1. API 鉴权不足

项目已有 JWT / bcrypt 工具，但部分 API 路由未强制鉴权。生产化前必须为所有非健康检查接口增加认证与角色校验。

### 2. Pod restart 接口过早暴露

当前存在：

```text
POST /api/topology/pods/{pod_name}/restart
```

底层是 Kubernetes `delete_namespaced_pod`，本质是服务影响操作。必须纳入审批和审计。

### 3. 日志接口可能泄露敏感信息

Pod logs 可能包含 token、连接串、用户数据、内部主机名。返回前应做脱敏和权限控制。

### 4. Discovery 会收集内部拓扑

K8s discovery 会读取节点、服务、Endpoints、环境变量中的连接信息。需要限制访问范围和输出脱敏。

## 操作风险等级

| 等级 | 类型 | 示例 | 平台行为 |
|------|------|------|----------|
| L0 | 只读 | 查看拓扑、健康、指标 | 允许，记录查询 |
| L1 | 低风险 | 生成报告、保存备注 | 允许，记录 audit |
| L2 | 服务影响 | 重启 Pod、回滚 Deployment | 创建审批单，审批后执行 |
| L3 | 数据/不可逆 | DROP、RESET、删 PVC、PRD 变更 | 默认禁用或二次审批 + 人工确认 |

## API 安全要求

### 认证

- `/api/health` 可匿名
- `/api/ready` 可匿名或内部访问
- 其他 API 必须认证

### 授权

建议角色：

| 角色 | 权限 |
|------|------|
| viewer | 只读拓扑/事件/报告 |
| operator | 可发起 L1/L2 操作申请 |
| approver | 可审批 L2 操作 |
| admin | 管理配置和用户 |
| breakglass | 紧急权限，必须强审计 |

### 审批

L2/L3 操作必须创建 approval request：

- 操作对象
- 影响范围
- 回滚方式
- 执行命令摘要
- 风险等级
- 申请人
- 审批人

### 审计

每个执行动作必须写 audit：

- actor
- source
- environment
- action_type
- target
- risk_level
- command_preview_redacted
- approval_id
- result
- verification

## 执行策略

### 默认模式：计划优先

用户点击“重启/回滚/修复”时，平台默认只生成执行计划：

```text
影响范围 → 回滚方式 → 预计中断 → 审批请求
```

审批通过后再由 Hermes Agent 或受控 worker 执行。

### L3 操作

L3 操作建议默认不在 Web UI 直接执行，而是：

1. 生成计划
2. 要求 CLI/人工确认
3. 写 oplog
4. 执行后回填结果

## 数据防泄露

- 不在 UI 展示明文密码/token/API key
- 日志接口默认脱敏
- env/config 只记录凭据来源，不记录值
- 外发报告脱敏
- GitHub 示例不得包含真实 IP/主机名/secret

## 手机端 / ChatOps

手机端适合：

- 巡检
- 只读排查
- 低风险标准操作申请

手机端不建议直接执行：

- DB DROP/RESET
- Longhorn 删除/迁移
- etcd/kube-apiserver 操作
- PRD 变更

## 与 Hermes Ops Kit 的关系

BestNative 应复用 Hermes Ops Kit 的：

- approval matrix
- oplog template
- model routing
- weekly maintenance
- audit-system

## 上线前安全清单

- [ ] 所有非 health API 有鉴权
- [ ] RBAC 生效
- [ ] Pod restart 改为审批流
- [ ] 日志接口脱敏
- [ ] Discovery 输出脱敏
- [ ] L2/L3 操作有 audit
- [ ] 默认 SECRET_KEY 禁止生产使用
- [ ] CORS 收紧
- [ ] kubeconfig/secret 不落库明文
- [ ] 所有执行 API 有 dry-run/plan 模式
