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

这不是执行任务的命令，不接受模型认证参数或 `--publish`。使用无 Token 的 GitHub 公共读取，重新确认计划已合并至同仓库 `main`；Work Order 和 Skill 来自固定合并提交，并与运行记录摘要核对。所需提交对象须已在本机存在；缺少时停止，由操作者先同步，不自动 fetch、切换分支或修改 Git 状态。

运行记录须是普通非符号链接文件，有明确的执行 / Session 身份，状态为 `local_ready` 或 `delivery_pr_open`，Worker 为 `stopped`、完成状态为 `complete`、无失败及未完成事项。核对原始提交直接基于 Plan merge、原始记录路径与实际原始提交一致，指定候选是原提交或其后代，且与本机 Delivery 分支 head 一致。原始与最终净变更都必须在允许范围内；不改变原运行记录。

资料包目录新建为 `0700`，文件为 `0600`；拒绝覆盖现有目录，拒绝放入仓库或 Worker 状态目录。文件包括：

- `request.json`：脱敏版本绑定、原始与候选提交、目标 `main` 快照、上下文摘要。
- `context.md`：版本信息、授权 Work Order、授权 Skill、完整 Plan merge 至候选的净差异。
- `review.schema.json`：固定的结构化回执格式。
- `prompt.txt`：限定审查资料与行为的短提示词。

不复制原始运行记录、认证文件、认证环境变量、Session ID 或完整会话。资料包仍可能含候选文件里的敏感内容，不要直接公开上传；本工具不承诺识别任意秘密。目录权限和提示词约束也不证明操作系统级隔离。

净变更最多 100 个文件，涉及的两端 blob 总大小最多 64 KiB，差异最多 32 KiB，整个上下文最多 64 KiB。超过就停止，不悄悄截断。拒绝变更中的符号链接、子模块与 Git 判断为二进制的差异。这些上限只限制本资料包，不是模型 Token 或账单硬上限，也不证明编译、测试或真实代码行为。

## 2. 独立只读审查

目前仍由操作者 / Steward 单独启动 Codex CLI。使用全新、只读、非续接会话；只读取资料包，按 schema 返回结果。缺少必要代码或业务上下文时返回 `blocked`，不要让 Agent 自动扩大读取范围。自动调用、进程时限和取消处理留给下一小步实现。

这组限制降低不必要的上下文搬运，但不证明模型实际遵守所有提示，也不保证节省多少 Token。资料包没有完整目标主线代码，无法据此认证最终合并后的行为或 CI；语义审查不足时应另行确认所需资料。

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
