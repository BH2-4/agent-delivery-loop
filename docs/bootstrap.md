# 从设计稿走到第一次真实交付

## 当前检查点（2026-10-04）

仓库已有第一版 Python `agent-run`、`agent-watch --once`、Work Order v1、Claude Code 受限启动器和最小 CI。CI 编译源码、运行聚焦回归用例、解析 Work Order schema，并解析示例 Work Order；它不验证每张新 Work Order。首次真实 Work Order `WO-PILOT-001-r1` 已执行：Plan PR #2 合并于 `0038259bad1a23f737c5585f66a49b8c3721289a`，Worker 返回结构化 `complete`，并生成本地候选分支 `agent/wo-pilot-001-r1-cb0ef3e5` 和提交 `b6af9114157a784aa65091868382c1fbd7c1218d`，唯一变更为 `docs/glossary.md`。`agent-run` 在最终运行记录持久化/回读校验时报错并以退出码 2 结束；只读检查发现磁盘记录为 `local_ready` / `stopped`。根因是 Git 提交路径以 tuple 进入运行记录，而 JSON 回读为 list，严格比较失败。没有创建 Delivery PR，完整交付链路尚未成功。现有候选成果与运行记录保持不变，本次不重跑该工单。这次真实执行能证明该次 Worker 返回及本地提交已经发生，但不证明完整交付链路成功。

本次真实运行中的 Worker 已启动，返回结构化 `complete` 并停止；这只记录该次执行，不代表账号或路由的持续可用性已验证。本机记录的 Claude Code CLI 版本为 2.1.288。Codex CLI 安装问题已修复，可人工启动 Steward 审查。仓库是公开仓库，但只读查询显示 `main` 当前没有 branch protection，且未配置 GitHub App。因此 `--publish` 不能使用，默认执行只提交本地 Delivery 分支。后续应先审查本次修复，再由用户决定如何接纳既有本地候选成果；本次不重跑任务。GitHub App、`main` 保护与无人值守发布仍未验证。不要把个人 GitHub 凭据传给 Claude Code，也不要用个人 Token 冒充 App 身份。

下方首轮提示词保留为本次引导交付的原始任务范围和审查依据，不表示需要再次交给另一个 Agent 重做。

本页区分两件容易混淆的事：**引导交付**用于实现执行器；**第一次真实 Work Order 执行**已经发生，但其最终登记报错，完整交付链路（包括 Delivery PR）尚未成功。

## 当前边界

- 一个 GitHub 仓库、一台受信任的执行主机、一个固定配置的 Claude Code Worker。
- Codex / Steward 负责需求整理、Plan PR 和 Delivery PR 审查，向用户报告结论。Plan PR 仍由用户合并授权。
- `agent-watch` 和 `agent-run` 是仓库实现的本机 Python CLI，不是 GitHub 或 Agent 框架的内置功能。
- `agent-watch --once` 按 PR 更新时间降序分页读取关闭 PR，最多读取 1000 个，并检查其中先遇到的 30 个已合并 PR。旧 PR 后续活动可能改变排序，因此不保证覆盖按合并时间最新的 30 个 PR；达到扫描上限仍未检查满 30 个已合并 PR 时会报错，不报告“无任务”。
- `agent-run` 与 `agent-watch --once` 都要求显式提供 `--model`、`--base-url`、`--effort`、`--auth-config`。认证只从所指定 Claude settings JSON 的 `env` 对象读取一个受支持且非空的认证项；同一对象中的 `ANTHROPIC_BASE_URL` 还必须有效并与显式端点一致。若当前 CC Switch 供应商与本次目标不同，应停止并由用户另行明确来源；不自动切换设置，也不创建 Worker 专用认证文件。配置文件必须在目标仓库之外，且不会被复制到 Worker HOME。CC Switch 配置保持原样，父环境中的模型、路由、effort 和认证变量不会进入 Worker。
- CLI 在创建 Worker 前通过不带认证值的 `--version` 和普通 `--help` 检查版本及所请求 effort；如果当前 CLI 没有在帮助中列出该 effort，则停止，不尝试模型请求或降级。
- 创建 Worker 前必须成功持久化并回读验证未确认安全结束的运行记录。Ctrl+C、超时和异常会尝试有界停止进程组；进入创建阶段后，即使 `Popen` 未返回句柄，也不能据此判定 Worker 未启动。无法确认时保留 HOME 与门禁，后续更新失败、再次取消或执行器退出释放锁不会授权下一次启动。只有明确未启动或已确认停止才允许清理 HOME、保存安全状态。启动登记及停止/HOME 清理关键区延迟 SIGINT，不向 Worker 传递阻塞信号掩码，取消后不继续正常交付。
- 执行器的 Git 命令使用单次 `core.hooksPath` 配置屏蔽钩子，不改用户全局设置或删除钩子；提交前固定暂存树与父提交，提交后核对提交对象并重新计算交付路径。
- GitHub Actions 只执行确定性的仓库检查：源码编译、聚焦回归用例、schema JSON 解析和一个示例 Work Order 解析；它不扫描每张新 Work Order。Codex 的语义审查是另一道门。
- Hermes 通知尚未实现。跨主机领取、自动重试、自动部署不进入首轮。
- Delivery PR 自动合并的身份与分支规则尚未锁定；在验证前不得实现或宣称可用。

显式配置的人工命令示例：

```sh
agent-run --plan-pr https://github.com/OWNER/REPO/pull/123 \
  --work-order-path .agents/work-orders/WO-2026-001-r1.json \
  --model glm-5.3 \
  --base-url https://open.bigmodel.cn/api/anthropic \
  --effort max \
  --auth-config /path/outside/repository/claude-settings.json

agent-watch --once \
  --model glm-5.3 \
  --base-url https://open.bigmodel.cn/api/anthropic \
  --effort max \
  --auth-config /path/outside/repository/claude-settings.json
```

本次首次真实 Work Order 使用此前已安装的普通 wheel 执行；该 wheel 来自已知源码提交 `f4661f9616201ce38be0468f39ee69d493cc0847`。当前已安装 wheel 不随源码修改更新，本轮不替换它。后续若用户决定运行新的 Work Order，应先审查并合并修复，再从准确完整 SHA 的干净 checkout 重建普通 wheel；记录源码 SHA 与 wheel SHA-256，并按用户确认安装。本次修复不重跑既有 Work Order。

## 运行记录的安全门禁

现有私有运行记录是新运行唯一的安全状态来源，新增字段 `worker_status`，不另建故障标记。写入使用原子替换、文件及目录同步，并回读核对；领取任务持锁后扫描全部记录。记录读取失败、JSON 损坏、字段重复、缺少安全字段或状态矛盾均阻止启动。旧版记录缺少此字段，或仍存在旧故障标记时，也需要人工审查，不自动迁移或清除。

| Worker 状态 | 含义与门禁 |
| --- | --- |
| `start_unconfirmed` | 启动前已保存“尚未确认安全结束”；阻止后续任务，即使执行器随后退出。 |
| `running` / `stop_unconfirmed` | 正在运行或无法确认是否仍在运行；阻止后续任务。 |
| `not_started` | 明确未进入创建阶段；仅与 `not_started` 或 `failed` 运行状态相配时允许后续任务。 |
| `stopped` | 已启动且确认进程组停止；仅与 `validating`、`local_ready`、`delivery_pr_open`、`cancelled` 或 `failed` 相配时允许后续任务。 |

不能确认停止时尽可能写入 `cleanup_failed` / `stop_unconfirmed`，`finished_at` 保持空值；更新失败时仍保留启动前记录，并继续阻断。实际进入创建函数后发生异常而未取得句柄，保守归为停止未确认；即使某次创建可能事实上没有成功，也不会自动放行。年龄、超时或某个 PID 不存在均不能解除门禁。

出现未确认运行、记录损坏或持久化故障，**停止继续试运行**。保留运行记录、临时 HOME 和 worktree，记录运行 ID，由维护者检查进程及其后代和状态存储，取得独立安全确认后再单独制定、审查恢复方案。当前没有恢复/清除命令；不要删除或改写记录、删除旧标记、换用 `AGENT_STATE_DIR` 或重跑任务绕过门禁。本轮不验证或执行人工恢复。

## 两次交付

### 0. 引导交付：人工实现最小链路

本次由 Codex 按照下方提示词在普通开发分支实现最小链路，并通过普通 PR 交付。此时还没有用该执行器执行 Work Order，因此这次**不是**系统自我运行的证明，也不应伪装成已自动授权、领取或审查。

首轮实现已做到：能根据明确的 Plan PR 引用，读取其合并后的 Work Order，固定任务内容与代码基线，在单机启动一次 Claude Code，并将结果整理为本地分支。`agent-watch` 只提供手动 `--once` 入口。GitHub App 凭据隔离和分支规则未验证，因此执行器不会自动推送；人工检查后可以自行推送本地分支并创建 Delivery PR。推送凭据由人使用，不传给 Claude Code。Codex 审查也由人手动启动。

### 1. 首次真实交付：小而真实的 Work Order

首次真实 Work Order 已按此前流程执行并生成本地候选分支，但最终运行记录校验失败；候选成果尚未被接纳为完整交付。后续完整交付仍需先按维护者审查结论处理该登记问题，再由人检查候选分支，并决定如何手动推送和创建 Delivery PR；本轮修复不重跑 Work Order，也不覆盖既有候选成果或运行记录。Actions 的确定性 CI、Codex 独立审查与用户简报仍是完整链路的一部分。GitHub App 和 `main` 保护仍未验证，不用于这条手动试运行路径。

首次真实 Work Order 的执行已经发生；首次完整交付链路尚未成功，因为 `agent-run` 未成功结束，且没有创建 Delivery PR。即使 Delivery PR 尚未合并，也只能称为“候选交付”，不能称为代码已进入 `main`；如果没有部署流程，更不能称为 `deployed`。

Worker 已确认退出但任务未完成时，保留成果与脱敏运行记录。返修需要另行明确授权，并由人以新 Session 接入；只有目标、验收条件或允许路径发生变化时才要求新 Work Order 修订。Session ID 不能恢复完整对话。当前没有续修命令；`agent-run` 从所授权的 Plan merge commit 创建新工作分支，不会接手原 Delivery 分支。无法确认 Worker 已停止时，继续保留 HOME、worktree 和安全门禁，不进入返修流程。

## 首轮观察记录

只记录能帮助第二轮设计的信息：Work Order 与 Plan PR 链接、实际执行主机和 CLI 版本、开始及结束时间、Delivery PR 链接、CI 结果、Codex 审查结论，以及是否出现重复启动、中断或凭据问题。第一版不制定跨主机领取回执或自动失败重试的精确协议；看到首次真实运行结果后再讨论。

## 首轮执行提示词（任务原文）

```text
你在 BH2-4/agent-delivery-loop 仓库进行“引导交付”，不是在运行已经建成的 Agent Delivery Loop。先阅读 README.md、docs/adr/ 和 docs/bootstrap.md；检查工作树，保留现有未提交改动，不覆盖其他人的工作。

目标：用 Python 做出单机、单 Worker、手动优先的最小可运行链路，为随后一次真实 Work Order 试运行创造条件。先写一份简短实现计划并指出实际缺失的 CLI、认证或 GitHub 权限；然后在可验证的范围内实现，不要把未验证的功能写成已完成。

本轮范围：
1. 定义最小 Work Order 格式及示例：任务 ID、修订号、目标、不做什么、验收条件、允许修改范围、固定 Worker 配置、Skill 引用和停止条件。Plan PR 的合并提交由执行器查询 GitHub 并记录，不要求 Work Order 预先填写尚不存在的 merge SHA。
2. 实现 Python CLI 的手动 `agent-run` 路径：用明确的 Plan PR 引用核实它已合并到目标主分支，读取该次合并所授权的 Work Order 内容并固定摘要与基线；在独立工作目录和全新 Session 中启动固定 Claude Code 配置，记录本次运行与实际 Session 的关联，不复用用户正在交互的会话；检查产出与允许范围；留下脱敏结果。不要从未合并 PR、Issue、评论或最新浮动文件直接获取执行授权。
3. 仅在手动路径可用后，实现 `agent-watch --once` 作为单机发现入口；不要安装定时器，不做跨主机抢单、自动重试或模型切换。
4. 准备 GitHub App 创建交付分支和 Delivery PR 所需的最小接口；只有在 App 最小权限、私钥隔离和 `main` 保护规则得到实际验证后，才把无人值守推送标为可用。绝不能把 App 私钥或令牌传给 Claude Code、写进仓库或日志。
5. 为 PR 加最小 CI：至少检查 Python 代码可编译或构建，以及 Work Order 格式可解析。只做有明确价值的检查，不为展示覆盖率编写大量测试。
6. 在 README 中清楚区分“已实现”“尚未验证”“后续阶段”；给出人工启动、首次真实试运行和失败停止的操作说明。Codex 审查及简报接口可以先说明人工接入方式；不要假装已有自动触发或自动合并。

禁止扩展：不实现通用 MCP 调度、多模型路由、Hermes 控制入口、自动部署、跨电脑领取协议、自动失败重试或 Delivery PR 自动合并。不要擅自安装全局 CLI、创建生产密钥、放宽权限或改变仓库保护规则。

验证顺序：先使受影响 Python 代码编译/构建通过；若无具体失败信号，到此停止，不扩大测试。若环境缺少 Claude Code、Codex CLI 或有效 GitHub 认证，报告缺失项和最小人工验证步骤，不用模拟输出冒充真实端到端结果。

交付：一个范围清楚的 Delivery PR 或（若认证不可用）本地分支及可审查的变更摘要。报告已完成、未完成、阻塞项、实际运行过的命令与结果，并明确“首次真实 Work Order 试运行”是否已经发生。
```
