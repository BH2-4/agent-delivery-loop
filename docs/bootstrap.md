# 从设计稿走到第一次真实交付

## 当前检查点（2026-10-02）

仓库已有第一版 Python `agent-run`、`agent-watch --once`、Work Order v1、Claude Code 受限启动器和最小 CI。CI 编译源码、运行聚焦回归用例、解析 Work Order schema，并解析示例 Work Order；它不验证每张新 Work Order。当前变更增加了 Worker 进程组取消清理与最终提交快照核对，并以不调用模型的临时 Worker 和临时 Git 仓库做两项聚焦回归验证。它们只证明测试替身及本地 Git 路径，不证明真实 Claude Code CLI 在当前主机和模型路由下的停止行为。**尚未启动真实 Work Order**。

当前主机的 Claude Code 为 2.1.284，与 Anthropic 官方当前标记的最新 release v2.1.284 一致；认证状态正常。Codex CLI 安装问题已修复，可人工启动 Steward 审查。仓库是公开仓库，但只读查询显示 `main` 当前没有 branch protection，且未配置 GitHub App。因此 `--publish` 不能使用，默认执行只提交本地 Delivery 分支。当前允许先做人工监督试运行：由人检查本地分支后，手动推送并创建 Delivery PR；GitHub App、`main` 保护与无人值守发布仍未验证。不要把个人 GitHub 凭据传给 Claude Code，也不要用个人 Token 冒充 App 身份。

下方首轮提示词保留为本次引导交付的原始任务范围和审查依据，不表示需要再次交给另一个 Agent 重做。

本页区分两件容易混淆的事：**引导交付**用于实现执行器；**第一次真实交付**才用实现好的执行器处理一张已合并的 Work Order。前者已经完成代码阶段，后者仍未发生。

## 当前边界

- 一个 GitHub 仓库、一台受信任的执行主机、一个固定配置的 Claude Code Worker。
- Codex / Steward 负责需求整理、Plan PR 和 Delivery PR 审查，向用户报告结论。Plan PR 仍由用户合并授权。
- `agent-watch` 和 `agent-run` 是仓库实现的本机 Python CLI，不是 GitHub 或 Agent 框架的内置功能。
- `agent-watch --once` 按 PR 更新时间降序分页读取关闭 PR，最多读取 1000 个，并检查其中先遇到的 30 个已合并 PR。旧 PR 后续活动可能改变排序，因此不保证覆盖按合并时间最新的 30 个 PR；达到扫描上限仍未检查满 30 个已合并 PR 时会报错，不报告“无任务”。
- Ctrl+C、超时或启动后的异常会触发整个 Worker 进程组的有界停止流程。启动时短暂延迟处理 Ctrl+C，直到 `Popen` 返回的进程句柄和生命周期状态登记完成，再立即处理取消请求；不会把屏蔽 SIGINT 的信号状态传给新 Worker。确认进程组消失后才清理临时 HOME、记录结束状态和释放锁。运行记录区分 Worker 未启动、停止后的取消和停止未确认。无法确认时保留 HOME 并写入本机清理失败标记，后续执行入口会拒绝新任务，需人工确认进程状态后清除标记。
- 执行器的 Git 命令使用单次 `core.hooksPath` 配置屏蔽钩子，不改用户全局设置或删除钩子；提交前固定暂存树与父提交，提交后核对提交对象并重新计算交付路径。
- GitHub Actions 只执行确定性的仓库检查：源码编译、聚焦回归用例、schema JSON 解析和一个示例 Work Order 解析；它不扫描每张新 Work Order。Codex 的语义审查是另一道门。
- Hermes 通知尚未实现。跨主机领取、自动重试、自动部署不进入首轮。
- Delivery PR 自动合并的身份与分支规则尚未锁定；在验证前不得实现或宣称可用。

## 两次交付

### 0. 引导交付：人工实现最小链路

本次由 Codex 按照下方提示词在普通开发分支实现最小链路，并通过普通 PR 交付。此时还没有用该执行器执行 Work Order，因此这次**不是**系统自我运行的证明，也不应伪装成已自动授权、领取或审查。

首轮实现已做到：能根据明确的 Plan PR 引用，读取其合并后的 Work Order，固定任务内容与代码基线，在单机启动一次 Claude Code，并将结果整理为本地分支。`agent-watch` 只提供手动 `--once` 入口。GitHub App 凭据隔离和分支规则未验证，因此执行器不会自动推送；人工检查后可以自行推送本地分支并创建 Delivery PR。推送凭据由人使用，不传给 Claude Code。Codex 审查也由人手动启动。

### 1. 首次真实交付：小而真实的 Work Order

引导交付审查完成后，再选一项范围小、验收明确、不会触及生产部署的仓库改动。用户合并其 Plan PR；首次真实试运行由人显式指定这个已合并 Plan PR 调用 `agent-run`，执行器启动 Claude Code 并生成本地交付分支。人检查分支后使用自己的 GitHub 身份手动推送并创建 Delivery PR；Actions 给出确定性 CI 结果，Codex 独立审查并给用户简报。手动路径稳定后，再单独验证 `agent-watch --once` 的发现行为。GitHub App 和 `main` 保护仍未验证，不用于这条手动试运行路径。

只有真实 Work Order 确实经过这些步骤，才能称为“首次真实试运行”；截至当前，这次试运行尚未发生。如果 Delivery PR 尚未合并，只能称为“候选交付完成”，不能称为代码已进入 `main`；如果没有部署流程，更不能称为 `deployed`。

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
