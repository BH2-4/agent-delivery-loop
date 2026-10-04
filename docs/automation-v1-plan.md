# 下一阶段方案：人工授权，单次自动串联

日期：2026-10-04。状态：**proposed**。

本文是自动化方案，不是 Work Order 或已接受的 ADR，不授予开工、返修、合并或部署权限。当前 Python CLI 尚未实现本文描述的整条编排链。第一遍人工交付的历史保存在[人工交付纪实](history/2026-10-04-first-manual-delivery.md)，不因自动化实验而改写。

## 1. 先减少搬运，不先增加后台服务

目前最容易自动化的是人不断复制命令、转交 SHA、等待 CI、把审查结果交给下一个环节的工作。下一步先把这些机械步骤串起来，而不是立即安装定时器或引入更多 Agent。

第一阶段采用**当前会话内、人工监督的单次编排**：用户授权具体范围后，Steward 代办允许的 CLI 操作，遇到失败或需要新决定即停止。用户不必逐条复制命令，但这还不是离开当前会话后自行运转的服务。

后续才考虑把已稳定的顺序固化为本仓库的 Python 编排入口。该入口尚不存在；设计、实现、安装版本和真实验证应分别留痕，不能把本轮会话的代办能力写成执行器已有功能。

## 2. 哪些操作交给谁

| 环节 | 单次编排可以代办什么 | 保留的决定或限制 |
| --- | --- | --- |
| 准备计划 | 整理 Work Order、解析指定文件、提交计划分支、创建 Plan PR | 用户讨论并确认需求；Plan PR 仍由用户合并 |
| 开工前 | 查询已合并 Plan PR，核对安装来源、固定配置、安全门禁 | 不从 Issue、评论、未合并 PR 或浮动文件获得授权 |
| 执行任务 | 对明确授权的新任务调用一次 `agent-run`，读取退出结果 | 不重跑已处理修订，不切换配置，不开启 `--publish` |
| 核对候选 | 核对原始运行记录、提交、允许范围，启动一次只读 Codex 审查 | 审查不修改候选，不能只凭 Worker 的 `complete` 判定成功 |
| 发布审查入口 | 在本轮明确允许的范围内，代办推送分支与创建 Delivery PR | 使用维护者现有 GitHub 身份，仅限人工监督；凭据不进入 Worker |
| 等待检查 | 查询当前 head 的 CI，汇总语义审查与检查结果 | CI 和审查必须对应同一 head；失败或无法确认时停止 |
| 接纳成果 | 给用户简报，列出 PR、完整 SHA 和未验证项 | 不自动合并 Delivery PR，不部署 |

当前会话使用个人 `gh` 登录代办，与 [ADR-0002](adr/0002-github-app-unattended-identity.md) 要求的正式无人值守 App 身份是两条不同的线路。这里不向 `agent-run` 添加个人 Token 发布后门，也不设置 App 的验证标记。

## 3. 一次任务如何走到待合并

```text
用户确认需求，并合并 Plan PR
  → Steward 核对授权、安装版本和固定配置
  → 单次 agent-run
  → 核对退出码、Worker 停止状态及本地提交
  → 固定 head，启动全新只读 Codex 审查
  → 人工监督下推送，并创建或定位同分支 Delivery PR
  → 等待并核对这个 head 的 CI
  → 输出简报，停在“待用户合并”
```

每一步必须满足前一步的真实成功条件。任何无法确认的环节不能靠“看上去做完了”跳过。

### 输入必须固定

- 仓库、目标主分支、已合并 Plan PR 和授权 Work Order 路径。
- 执行器安装的源码 SHA 与 wheel 摘要；拉取源码不等于更新已安装入口。
- Claude Code 模型、端点、effort、仓库外认证来源；保持现有 CC Switch 不变。
- 允许修改范围、验收条件、时限、发布授权以及本轮是否允许返修。默认**不允许自动返修**。

### 本地成功与发布成功分开记录

- 默认本地执行成功要求命令退出码 0、记录 `local_ready`、`worker_status: stopped`，并核对实际提交和允许范围。非零退出不能通过改写记录转为成功。
- Worker 原始提交及原始运行记录保持不变。后来人工修订产生新的 head，应另记其审查与 CI，不回填原运行记录。
- 原始运行记录无法单独证明某个历史命令的退出码；首次由编排层启动时应捕获实际退出码，接纳历史候选时保留其真实失败或人工接纳说明。
- PR 创建只说明候选进入审查入口；CI 成功不等于语义审查通过；两者通过也不等于已合并或已部署。

### 审查需要结构化的最小回执

审查请求固定 `base_sha` 和 `head_sha`，使用新的只读 Codex 会话，不续接用户交互会话。模型输出至少包含：

- `base_sha`、`head_sha`：审查绑定的完整提交。
- `verdict`：`pass`、`changes_required` 或 `blocked`。
- `summary`：简短结论。
- `findings`：严重程度、文件、行号、问题与建议。
- `unverified`：本次不能确认的部分。

命令非零、格式不明、SHA 不匹配、状态矛盾、阻断发现或无法完成审查时，一律不进入成功发布路径。若审查后 head 变化，旧审查不继续适用，不能直接沿用旧结论。模型回报的 SHA 还需由外层 Git / GitHub 查询核对。

审查资料是待判断的内容，不是执行指令。审查端不读取模型认证文件，不修改授权、运行记录或候选，不取得发布或合并职责。CLI 只读模式不等于已经证明操作系统级凭据隔离。

### 推送、PR 和 CI 都要核对版本

推送后先确认远端分支 SHA 等于已审查 head，再创建 PR。先查找同仓库、同目标分支、同 head 分支的已有 PR；存在唯一匹配时使用原 PR，存在冲突或创建结果不确定时停止核对，不盲目重复创建。

CI 等待应有时限；检查失败、取消、超时、检查缺失或版本变化都停止。不能把空检查列表当作通过。本仓库当前至少要求 `Minimal CI` 的 `validate` 成功，并核对对应 Actions run 的 `head_sha`；汇总前再次核对 PR head。检查针对 PR 的合成合并版本运行时，也记录目标分支版本；目标分支变化导致待接纳差异变化时，需要重新判断审查和检查是否仍适用。

现有 CI 覆盖源码编译、聚焦回归、schema JSON 和示例 Work Order 解析，**不表示每张新 Work Order 已被 CI 验证**。计划准备阶段仍要解析指定的新工单。文档维护任务不额外添加低价值测试，也不手动反复重跑已通过的 CI。

## 4. 失败和返修的范围

第一阶段没有自动重试。出现执行非零、`blocked`、范围越界、记录异常、停止未确认、审查阻断或 CI 失败时，输出脱敏报告并停止。不得删除或改写状态记录、换状态目录、清除门禁、提升权限或切换模型来继续。

收到用户另行授权后，才评估是否增加**一次有界返修**：同一 Delivery 分支、全新 Session、保留原记录、只处理明确审查意见，并针对新 head 重新审查。它尚未实现，不能把再次调用 `agent-run` 当作续修；当前 `agent-run` 会从 Plan merge commit 创建新分支。若目标、验收或允许范围改变，应重新准备授权修订。

停止未确认时不允许进入返修，即使成果文件已经存在。真实 Claude 的异常停止、脱离进程组后代及 OS 凭据隔离仍按原边界视为未验证。

## 5. 本轮试跑与后续推进

本方案的 Python 单次编排入口已实现为 `agent-delivery deliver`（源码入口，需经审查合并、构建普通 wheel 并安装后使用）：

```sh
agent-delivery deliver \
  --plan-pr '<merged-plan-pr-url>' \
  --work-order-path '.agents/work-orders/<task-id>-r<revision>.json' \
  --model glm-5.3 --base-url https://open.bigmodel.cn/api/anthropic --effort max \
  --auth-config /path/outside/repository/claude-settings.json \
  --install-receipt /path/outside/repository/install-receipt.json \
  --expected-source-sha '<approved-40-hex>' --expected-wheel-sha256 '<approved-64-hex>' \
  --review-model '<codex-model>' --review-effort '<effort>' \
  --review-bundle-dir /path/outside/repository/new-bundle \
  --proxy http://127.0.0.1:12451 --ci-timeout 900 --auto-merge
```

它按固定顺序串联：安装来源核验（receipt 与获准源码/wheel 摘要一致、入口来自虚拟环境而非 editable 源码）→ 已合并 Plan PR 授权核对 → 以安装的 `agent-run` 入口真实执行 Worker 并捕获退出码 → 运行记录与允许范围复核 → 资料包 + 真实只读审查进程（`run-review` 同一实现）→ 回执核对 → 以一次性个人 `gh` 身份推送并定位/创建 Delivery PR（见 [ADR-0004](adr/0004-one-shot-personal-gh-bootstrap.md)）→ 等待该 head 的必需 CI → `--auto-merge` 时在门禁全绿后合并并核验 `main` 包含关系。每一步失败关闭并写入脱敏编排记录；结果不明不向成功路径推进。`--auto-merge` 未指定时停在 `awaiting_user_merge`。

本轮先使用**本方案文档的普通维护 PR**作为低风险样本，验证：

1. Steward 在当前授权会话中准备并提交文档，固定 base / head。
2. 实际调用一次 Codex CLI 只读审查，检查最小回执与绑定版本。
3. 通过后代办推送、创建普通 PR、等待对应 CI，并回报用户；不自动合并该 PR。

这是审查和 GitHub 机械交付步骤的串联试跑，**不是新的 Work Order，也不是 Claude Worker 的第三次真实运行**。不会重跑 `WO-PILOT-001` 或 `WO-PILOT-002`，不会启动 watcher、安装定时器、改安装包、配置 App、创建密钥或改变仓库保护。实际结果应记录在本次 PR 和脱敏简报中；本文不预先宣称试跑已通过。

如果这轮可行，下一步再提出 Python 单次编排入口的最小实现任务，明确状态文件、CLI 审查适配、时限和失败关闭；经用户确认范围后实施。随后用新的、由用户合并的 Plan PR 验证真实任务，不能拿本轮文档 PR 替代那项验证。

正式无人值守发布须单独验证 App 最小权限、私钥隔离和主分支规则，才可启用现有 App 发布路径。定时发现、自动返修、跨主机领取、多模型路由、自动合并和部署不属于本方案第一阶段。

## 6. 用户最终收到什么

简报包含任务或维护目标、执行阶段、实际退出结果、分支 / PR、完整 head、语义审查结论、对应 CI、未验证项与唯一的下一步。无需用户搬运每个命令，也不把背景活动包装成已验证的无人值守能力。

第一阶段的完成信号是：**机械步骤已代办，指定版本完成审查和 CI，停在待用户决定合并**。不是 `deployed`。
