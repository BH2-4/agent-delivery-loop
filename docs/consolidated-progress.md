# 当前主线与历史进度

## 当前主线（2026-10-15）

方向已从“普通程序发现后启动固定 Claude Worker”转为 pull 模式：用户已有的本机定时
任务唤醒当前 Agent；接入件读取并核验共享授权任务，检查本机能力，确定性领取；当前
Agent 自己执行并写回共享回执。接入件不得启动第二个 Agent，不增加新调度器、守护进程
或跨电脑唤醒服务。

主计划见[自主拉取计划](pull-mode-plan.md)。新任务使用独立
`pull-task-v1` / `.agents/pull-tasks/`，首期由已合并 Plan PR 提供授权证据。旧
Work Order v1、`agent-run`、`agent-watch` 和 Claude Worker 保持原有权限与行为，不自动
迁移或扩大授权。公共任务与回执不要求任务目标是 Git 仓库或代码 PR。安装/更新本地
Skill 与 CLI 也是目标的一部分；远端机制更新不能代替本地接入件更新。

当前接续：[PR #43](https://github.com/BH2-4/agent-delivery-loop/pull/43) 已将 pull 核心、
Skill 和 installer 合入 `main`。合并前的 Python compileall、Skill quick_validate、两种已知
安装激活中断恢复模拟、空格路径 launcher smoke、active receipt/SHA 对齐检查通过，独立代码
审查无 P1/P2。隔离 wheel 曾在最终门禁修复前构建成功，修复后未重复完整 wheel 构建。

真实观察（2026-10-15/16）：本机 ZCode 从 `main` 构建 wheel，经 bootstrap venv 与
`agent-pull-install` 真实安装，来源 SHA 一致；updater 真实完成版本前移（0992511→dbb9290→
e8615c41）。[PR #45](https://github.com/BH2-4/agent-delivery-loop/pull/45) 授权的
`PULL-PILOT-001-r1` 由 `arco-mac-zcode` 完成真实 claim/start/running/completed 并写入
共享回执——证明任务领取与回执写入真实可用，不证明长期稳定、代码任务或无人值守。其后用户
删除 ZCode 原生 automation（排队提示已清空，接入件/凭据/grants 保留），Claude Code CLI
复用同一接入件，以独立 claimant `arco-mac-claude-code` 完成人工只读首扫（无新领取），
并在会话内创建原生 Cron 轮询（先 30 分钟后按用户指令调整为 12 分钟；创建与参数回读成功，
进程退出即停、七天到期；仅证明创建，尚未证明 Cron 自动唤醒）。本任务
`PULL-DOCS-002-r1` 首次 acquire 返回 unknown、status 复查为 absent；按执行端报告，用户额外
授权同一修订一次人工重试后，Claude 完成真实 claim/start/running/completed。completed 共享
[回执](https://github.com/BH2-4/agent-delivery-loop/blob/423dbd56f97ff4f25d2af2aea29cf7ce39a841b3/receipt.json)
对应待审查的 [Delivery PR #47](https://github.com/BH2-4/agent-delivery-loop/pull/47)。status absent
仅表示查询未见共享执行记录，不证明首次 unknown 没有远端副作用；此例证明一次经人工恢复的 Claude
文档交付，不证明 Cron 自动唤醒到任务完成的无人干预闭环。

仍待验证：原生 scheduler 自动唤醒并驱动 Claude 完成任务的完整链路、长期稳定性与代码类任务；
未验证项不得从编译、临时目录模拟或 Cron 创建推断完成。下一迭代
提案：[模型调用前的本机任务预筛选](pull-model-prefilter-plan.md)。

## 上一阶段结果（历史证据保留）

2026-10-14，用户要求合并“完成防重复启动架构迁入与上线”和“评价 DeliveryLoops
仓库”的进度，并继续执行。共同方向是减少人工装配命令、避免重复启动，执行
要求按任务需要讨论。以下保存该阶段的实际完成证据。

| 事项 | 实际进度 |
| --- | --- |
| 共享防重复启动 | PR #39 已合入 main；110 项原回归、真实 GitHub 验证及一次模型冒烟通过。状态不明保留待确认，不自动接管。 |
| 本机运行 | 已有当前电脑 Ubuntu WSL 的安装核对和工单校验记录；这不是完整业务交付。 |
| Schema 与安装核验 | 此前修复仍在本地；本轮迁入最新主线，补齐 review_evidence，核对安装模块与获准 wheel 内容。 |
| CI 精简 | 本轮迁入已完成的重复用例合并、模拟时钟和重复步骤删除；正式及示例工单由同一测试校验。 |
| 简化启动入口（Issue #37） | 本轮新增 launch：一份私有本机配置，每次明确指定任务，调用既有 deliver，默认不自动合并。 |

本轮验证、PR/CI 与本机安装的最终结果由对应交付报告记录，不能仅凭源码
存在就声明发布或业务执行成功。原目录的八处未提交内容保留，迁入使用隔离副本。

## 上一主线当时的接续项（由当前计划取代）

以下待办记录的是 2026-10-14 的旧方向；当前 pull 主线及阶段验收以本文件顶部链接的
计划为准。历史记录保留，避免把旧 Claude Worker 路线误当成新路线要求。

1. 验证真实细粒度 PAT 的安装版完整交付链路；保留历史 review_blocked 记录。
2. 讨论并决定最小任务协议：统一任务身份、领取、启动和状态，按任务选择
   工具、验收与交付方式。现有 Work Order v1、Plan PR 与审查要求尚未改变。
3. 在一次扫描的业务路径走通后接入本地定时扫描；扫描由普通程序完成，
   取得新启动资格才调用模型。本轮 launch 不安装后台任务。
4. 按实际需要讨论不同任务间的写入冲突、共享规则/Skills 和通知。

多步骤持久工作流、自动租约接管、多执行器与通知仍是后续议题。
共享去重保证遵守协议的执行端对同一键至多启动一次，不保证自动完成、
业务副作用恰好一次或旧执行器自动获得保护。

依据：[PR #39](https://github.com/BH2-4/agent-delivery-loop/pull/39)、
[Issue #37](https://github.com/BH2-4/agent-delivery-loop/issues/37)、
[共享启动协议](start-dedupe.md)、[启动说明](launch.md)。
