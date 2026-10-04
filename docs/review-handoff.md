# 有限上下文的只读审查交接

本阶段固化两个 Python 工具步骤，而不是实现整条自动交付链：

- `agent-delivery prepare-review`：生成审查资料包，不启动模型。
- `agent-delivery check-review`：核对操作者交来的结构化回执，不发布或合并。

本轮没有新 Worker 或新 Work Order；原执行器安装保持不变。新入口只存在于本次源码变更，尚未构建安装成普通 wheel，不能假定现有 `.venv/bin/agent-delivery` 已具有新子命令。开发审查期间可以使用下面明确标注的临时源码入口；不持久修改 `PYTHONPATH` 或 CC Switch。

## 1. 准备审查资料

由操作者指定已合并 Plan PR、工单路径、仓库外原始运行记录、Delivery 分支当前的完整 head，以及一个尚不存在的资料包目录：

```sh
PYTHONPATH=src .venv/bin/python -m agent_delivery_loop prepare-review \
  --plan-pr '<merged-plan-pr-url>' \
  --work-order-path '.agents/work-orders/<task-id>-r<revision>.json' \
  --run-record '/path/outside/repository/runs/<task-key>/<run-id>.json' \
  --head-sha '<full-delivery-branch-head-sha>' \
  --output-dir '/path/outside/repository-and-worker-state/new-review-bundle'
```

这不是执行任务的命令，不接受模型认证参数或 `--publish`。使用无 Token 的 GitHub 公共读取，重新确认计划已合并至同仓库 `main`；Work Order 和 Skill 来自固定合并提交，并与运行记录摘要核对。所需提交对象须已在本机存在；缺少时停止，由操作者先同步，不自动 fetch、切换分支或修改 Git 状态。执行器的全部 Git 读取（提交身份、祖先、文件树、路径与差异）统一按 `core.useReplaceRefs=false` 忽略对象替换：标注的 SHA 始终读取原始对象，不读取 `refs/replace/*` 指向的替换对象；本工具不修改用户全局 Git 配置，也不删除已有 replace refs。

运行记录须是普通非符号链接文件，有明确的执行 / Session 身份，状态为 `local_ready` 或 `delivery_pr_open`，Worker 为 `stopped`、完成状态为 `complete`、无失败及未完成事项。核对原始提交直接基于 Plan merge、原始记录路径与实际原始提交一致，指定候选是原提交或其后代，且与本机 Delivery 分支 head 一致。原始与最终净变更都必须在允许范围内；不改变原运行记录。

资料包目录新建为 `0700`，文件为 `0600`；拒绝覆盖现有目录，拒绝放入仓库或 Worker 状态目录。文件包括：

- `request.json`：脱敏版本绑定、原始与候选提交、目标 `main` 快照、上下文摘要。
- `context.md`：版本信息、授权 Work Order、授权 Skill、完整 Plan merge 至候选的净差异。
- `review.schema.json`：固定的结构化回执格式。
- `prompt.txt`：限定审查资料与行为的短提示词。

不复制原始运行记录、认证文件、认证环境变量、Session ID 或完整会话。资料包仍可能含候选文件里的敏感内容，不要直接公开上传；本工具不承诺识别任意秘密。目录权限和提示词约束也不证明操作系统级隔离。

净变更最多 100 个文件，涉及的两端 blob 总大小最多 64 KiB，差异最多 32 KiB，整个上下文最多 96 KiB（若工单声明 `review_evidence`，另计单文件 ≤32 KiB、合计 ≤48 KiB 的固定版本证据）。超过就停止，不悄悄截断。拒绝变更中的符号链接、子模块与 Git 判断为二进制的差异。这些上限只限制本资料包，不是模型 Token 或账单硬上限，也不证明编译、测试或真实代码行为。

## 1a. 工单证据契约（review_evidence）

Work Order v1 可选字段 `review_evidence`（1–20 条 `{path, ref, purpose}`）声明本工单验收所需的固定版本依据：`path` 为仓库相对普通文件，`ref` 为完整提交 SHA，`purpose` 说明用途。资料包会从固定 `ref` 读取这些 blob，标注 git blob id 与嵌入文本 SHA-256，放入 "Pinned baseline review evidence" 区，明确区分**主线依据**与候选 diff。check-review 重建同一快照并逐字核对。

- 授权：证据清单随 Plan PR 的合并提交固定；候选文档、Issue 或模型建议不能扩大读取范围。
- 就绪检查：`agent-delivery check-evidence --source-ref <sha> --work-order-path <path>` 在 Worker 开工前确认全部条目可解析、未超限；deliver 也在授权核对后内联执行同一检查。
- 读取仍走统一 `core.useReplaceRefs=false` 入口；拒绝任意本机路径、越界路径、符号链接、子模块与非 UTF-8 内容。

## 1b. 只读查询可靠性与回执复核

`GitHubClient` 对只读请求按实际响应分类：瞬时网络错误最多两次有界重试（总预算 120 秒）；明确限流在预算内等待一次；401/403 非限流与 404 等立即失败。固定 ref 的文件内容经 blob sha 核验后可缓存复用；可变状态在关键动作前重新查询。推送、创建 PR 与合并不做盲目重试。

审查进程已正常完成（记录 `confirmed_stopped` 且退出码 0）而仅网络复核失败时，编排记录先保存 `review_completed_pending_check` 及回执关联；网络恢复后用：

```sh
agent-delivery verify-review --plan-pr … --work-order-path … --run-record … \
  --head-sha … --bundle-dir … --review-record <state>/reviews/<review-id>.json
```

复核**同一回执**（退出码取自 harness 捕获的记录），不重新调用模型。候选或基线变化后旧回执不得复用。

## 2. 独立只读审查

审查现在有两种入口，均不修改候选、不取得发布或合并职责：

- `agent-delivery check-review`：操作者自己启动审查 CLI 后提交回执与**声明**的退出码；输出标记 `operator_attested_not_independently_proven`，仅核对数据契约。
- `agent-delivery run-review`（推荐）：由程序启动一次全新、只读、非续接的审查进程，**亲自捕获**其实际退出码、结构化结果、进程停止与清理结果，然后执行同一套回执核对；输出标记 `captured_by_orchestrator`。

```sh
PYTHONPATH=src .venv/bin/python -m agent_delivery_loop run-review \
  --plan-pr '<merged-plan-pr-url>' \
  --work-order-path '.agents/work-orders/<task-id>-r<revision>.json' \
  --run-record '/path/outside/repository/runs/<task-key>/<run-id>.json' \
  --head-sha '<full-delivery-branch-head-sha>' \
  --output-dir '/path/outside/repository-and-worker-state/new-review-bundle' \
  --review-model '<codex-model>' --review-effort '<effort>' \
  --review-timeout 900 --proxy 'http://127.0.0.1:12451'
```

审查进程固定为 `codex exec --ignore-user-config --ignore-rules --ephemeral --skip-git-repo-check --sandbox read-only`，工作目录限定在资料包内；不加载用户 MCP、通知钩子或 rules，不续接任何会话。认证只来自 Codex 自身的登录态（CODEX_HOME）；资料包与提示词不含凭据。审查资料保持有界（沿用资料包上限）；资料不足时应返回 `blocked`，不得自动扩大读取范围。

进程控制与停止语义：启动前在仓库外审查记录目录写入 `start_unconfirmed` 门禁；超时或取消时对整个进程组先 TERM 后 KILL，仅当确认进程组已消失才写 `confirmed_stopped` 并允许后续审查；无法确认停止时保留证据并阻断后续启动。任何审查记录不可读或存在未确认停止的记录时，新审查一律阻断。回执核对沿用第 3 节规则；`pass` 必须没有发现。

临时子进程（如假 CLI 回归）只能证明控制路径，不能冒充真实 Codex 行为；真实调用的结论以真实运行记录为准。CLI 只读沙箱不证明操作系统级凭据隔离。

回执字段固定为 `base_sha`、`head_sha`、`context_sha256`、`verdict`、`summary`、`findings`、`unverified`。`pass` 必须没有发现；`changes_required`、`blocked` 或仍有任何发现都不能通过本阶段核对。

## 3. 核对回执

```sh
PYTHONPATH=src .venv/bin/python -m agent_delivery_loop check-review \
  --plan-pr '<same-merged-plan-pr-url>' \
  --work-order-path '.agents/work-orders/<task-id>-r<revision>.json' \
  --run-record '/path/outside/repository/runs/<task-key>/<run-id>.json' \
  --head-sha '<same-full-delivery-branch-head-sha>' \
  --bundle-dir '/path/outside/repository-and-worker-state/new-review-bundle' \
  --result '/path/outside/repository/structured-review.json' \
  --review-exit-code '<actual-review-cli-exit-code>'
```

核对时重新读取 GitHub 授权、目标 `main`、原记录和本机分支，并重建资料包内容逐字比较。目标主线、候选分支、授权、记录或资料包变化时拒绝复用旧回执。JSON 重复键、非法常量、格式错误、SHA / 上下文摘要不匹配、命令非零或不完整结果都失败关闭；错误不打印原始记录或模型输出。

`--review-exit-code` 是操作者声明，不是程序独立捕获的进程结果；输出明确标记 `operator_attested_not_independently_proven`。这个入口只核对数据契约，不能证明 Codex 真实运行或确实读过资料包；未来外层编排须亲自启动 CLI 并捕获退出码。

成功输出 `review_checked`，不输出未经审计的模型自由文本，也不写回原记录。`candidate_already_on_main` 标识候选是否已在主线；历史成果可做只读交接核对，但不能当成新交付或重复发布。

**原记录并没有保存 `agent-run` 退出码**，因此两入口均保留 `run_command_exit_code: not_recorded_not_proven`。尤其第一张工单原调用退出码 2，不能因为它有 `local_ready` 记录或本工具核对成功而改称执行成功。

## 4. 下一步和失败停止

下一步再按明确范围连接一次真实 Codex 调用与自动捕获，之后才讨论 PR / CI 的编排接口。没有个人 Token 发布后门，不调用 watcher 或 Worker，不自动返修、重试、合并或部署。App 发布的验证门禁保持不变。

缺少网络、Git 对象、原始记录、CLI 或足够审查依据时停止，保留原成果与已有资料包。部分资料包创建失败时不覆盖、不自动删除；确认原因后另行选择新的目录，不绕过 Worker 安全门禁。

本轮验证按仓库规则先编译受影响源码；没有具体失败信号就不追加回归、模型调用或真实工单试运行。后续最小人工验证是：在新版本获准安装后，用已有脱敏候选生成一次资料包，预期 `review_prepared` 且绑定匹配；只读 CLI 真实退出后核对回执，预期 `review_checked`，仍不得把它称为完整交付。实施前应另行确认验证范围；伪造或模拟回执不能作为真实审查证据。
