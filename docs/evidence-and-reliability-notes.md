# 审查证据与只读可靠性说明

本文面向仓库使用者，概述本轮新增的两项能力：Work Order 的固定版本审查证据声明，以及 GitHub 只读查询的分类重试与回执复核。事实依据为资料包内附带的 `src/agent_delivery_loop/review_handoff.py`、`src/agent_delivery_loop/github.py` 与 ADR-0005。

## 证据声明（review_evidence）

- Work Order v1 新增可选字段 `review_evidence`，含 1–20 条 `{path, ref, purpose}` 记录；缺省该字段的旧工单行为不变。
- 字段约束：`path` 为仓库相对普通文件路径（拒绝通配与越界）；`ref` 为完整 40 位十六进制提交 SHA；`purpose` 不超过 500 字符。
- 声明随 Plan PR 授权固定：审查从已合并 Plan PR 的合并提交读取 Work Order，再按 `ref:path` 读取固定版本 blob。
- 资料包以 "Pinned baseline review evidence" 区嵌入证据全文，记录 git blob id 与嵌入文本 SHA-256，并标注为主线依据而非候选 diff 的一部分。
- 边界：单文件 ≤32 KiB、证据合计 ≤48 KiB、整个上下文 ≤96 KiB；拒绝符号链接、子模块、非普通文件与 UTF-8 之外内容。超限时先停止并要求收窄范围，资料包不截断。

## 就绪检查（check-evidence）

- Worker 开工前执行确定性证据就绪检查：`check-evidence` 命令，或在 `deliver` 内联执行。
- 固定 ref 无法解析、路径缺失或超出限制时先停止，不浪费 Worker 调用。
- 全部通过时返回状态 `evidence_ready`，逐条列出 path、ref、sha256、bytes、purpose，并汇总 `total_evidence_bytes`。

## 回执复核（verify-review）

- 审查进程完成后才发生网络复核失败时，编排层先记录 `review_completed_pending_check`（含捕获退出码与回执路径）。
- 网络恢复后用 `verify-review` 复核同一回执，不重新调用模型。
- 常规验收路径上，check-review 重建同一快照并逐字核对；资料包或候选发生任何变化都需重新准备审查，不得复用旧结果。

## 读取可靠性与边界

`GitHubClient` 对只读请求按实际响应分类处理：

- 瞬时网络错误（URLError/超时/5xx）：最多两次有界重试，间隔 2s/8s；单次超时 30s，总预算 120s。
- 明确限流分两类：带 `X-RateLimit-Remaining: 0` 与有效重置头的 401/403，在总预算内等待一次重置窗口；正文标记 rate limit 的 403 与 429 则进入下述 2s/8s 有界重试。等待或重试后仍限流即报错。
- 401/403 非限流、404 等 4xx：立即失败，绝不重试。
- 只读查询经分类重试后仍失败时以 `GitHubReadError` 报错。
- 固定 ref 的 `content()` 结果经 blob SHA 核验后缓存复用；Plan/PR/main 等可变状态在关键动作前重新查询。
- 指定 `--proxy` 时同时清理 `ALL_PROXY`（含小写）避免被覆盖。
- "推送、创建 PR、合并不做盲目重试"是 ADR-0005 的政策要求；当前实现中 `create_pull_request` 仍经由同一 `request()` 复用网络/5xx/限流重试，尚未对写请求单独隔离（已知差距）；其最终隔离与推送/合并路径不在本文件证据范围内。

结尾边界声明：本轮属 ADR-0005 记录的限次个人 `gh` 实验例外（固定期限与停止条件）——一次性使用现有个人 `gh` 登录态，范围仅限本轮修复 PR、新 Plan PR、新 Delivery PR 的创建/更新与门禁满足后的自动合并及一张跟踪 Issue；个人 `gh` 权限可能大于本轮授权，不声称已实现硬隔离。本轮结果不证明以下未验证项：

- 正式 App 无人值守线路；
- OS 级凭据隔离；
- 真实 Claude 全部异常停止路径；
- 账单硬上限；
- 部署。
