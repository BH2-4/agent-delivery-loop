# 写结果核对、有界返修与安全续接说明

本文面向仓库使用者，概述本轮新增的三项能力：GitHub 写操作结果核对、有界返修与安全续接。事实依据为资料包内附带的 `src/agent_delivery_loop/publish.py`、`src/agent_delivery_loop/rework.py` 与 ADR-0006。

## 写操作核对

- 读/写分离：只有只读 GET 享受分类重试；`gh` 写请求单次执行，响应丢失（网络断开、5xx）一律归为 `GitHubWritePendingError`，绝不自动重放。
- 写前持久化意图：每个外部写（推送分支 / 创建 Delivery PR / 合并 PR）先持久化操作意图与完整身份（仓库、任务与修订、分支、目标分支、候选 SHA、操作类型）；检查点保存失败即不执行写。
- 丢失响应用只读核对：写后用只读 `gh` 查询（`gh api`、`gh pr list`、`gh pr view`）核对远端真实状态——已成功则接纳真实结果且不重复执行；远端明确不存在（404）才允许一次有界重试；其余歧义一律以 `WriteReconciliationError` 停止。
- 推送确认只认远端事实：远端分支指向审查候选 SHA 才算确认；指向其他提交则拒绝覆盖任何内容。
- 合并绑定审查通过的精确 head：PR 不是 OPEN、或 head 不是审查通过的候选 SHA 时不执行合并；已 MERGED 但 head 不符同样报错。合并后核验 PR 状态与 main 包含关系。
- 唯一匹配的 Delivery PR 被复用而非重复创建：查到唯一 open 且 head 等于候选 SHA 的 PR 即复用（记录 `reused`）；匹配多于一条或 head 不符即停止；创建结果不明时先只读核对，找不到唯一匹配绝不再次创建。

## 有界返修

- 仅 `changes_required`（证据明确的成果缺陷）触发返修；`blocked` 不触发返修、也不弱化验收。
- 返修上限两轮（`MAX_REWORK_ROUNDS = 2`），计数持久在编排记录中，跨续接累计不重置；超限即 `ReworkLimitError`，不得再启动返修。
- 每轮返修是全新 Worker 会话：同一固定配置与 Skill，`rework_directive` 逐字携带审查发现，在原 Delivery 分支上工作，完全复用 `run_claude` 的启动登记、进程组停止、安全记录与清理机制。
- 每轮有独立返修记录：`mode: bounded_rework`，关联原 run_id、父提交与返修轮次；原运行记录与原候选永不改写。
- 返修启动前重核授权 Delivery Skill 的 SHA-256 摘要，不一致即阻止返修；审查发现渲染出的返修指令超过 32 KiB 上限时停止而不截断。

## 安全续接

- `agent-delivery resume --orchestration-id` 仅从已确认安全完成的阶段续接；不重跑原 agent-run，也不恢复停止不确定的进程。
- 续接前重核各版本绑定：安装来源、授权、运行记录、候选与外部状态；参数与记录绑定不一致即拒绝。
- 已完成的审查回执不重掷：基础设施失败后复用已捕获且身份匹配的 pass 回执重新核对，不重调模型。
- `deliver` 与 `resume` 共用同一阶段机（install → authorization → evidence → worker → review(→rework→review) → push → PR → CI → merge → main 核验），每阶段可靠落盘检查点；`--auto-merge` 仅在全部门禁满足后合并。

## 边界与未验证项

结尾边界声明：本轮属 ADR-0006 记录的限次个人 `gh` 实验例外（固定期限与停止条件）——一次性使用现有个人 `gh` 登录态，范围仅限本轮工程 PR、Plan PR、Delivery PR 与一张跟踪 Issue 的创建/更新与门禁满足后的普通合并，不使用管理员绕过，不放宽仓库规则；个人 `gh` 权限可能大于本轮授权，不声称已实现硬隔离。返修与续接路径若未在真实试运行中发生，仅以回归验证为据，不称真实发生。本轮结果不证明以下未验证项：

- 正式 App 无人值守线路（App 线路）；
- OS 级凭据隔离；
- 真实 Claude 全部异常停止路径；
- 账单硬上限；
- 部署。
