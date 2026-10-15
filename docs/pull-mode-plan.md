# Autonomous Pull 主线计划

**工程状态（2026-10-16）：**PR #43/#44/#46 已先后合入 `main`。其后的真实观察：本机
ZCode 从 `main` 构建 wheel 并经 bootstrap venv 与 `agent-pull-install` 真实安装（来源
SHA 一致）；`PULL-PILOT-001-r1` 由 `arco-mac-zcode` 完成真实 claim/start/running/
completed 并写入共享回执；用户随后移除 ZCode 原生 automation，Claude Code CLI 以独立
claimant `arco-mac-claude-code` 复用同一接入件完成人工只读首扫（无新领取）。会话内
原生 Cron 已创建（仅证明创建，自动唤醒尚未验证）。随后 `PULL-DOCS-002-r1` 首次 acquire
返回 unknown、status 复查为 absent；按执行端报告，用户额外授权同一修订一次人工重试后，Claude
完成真实 claim/start/running/completed；[completed 回执](https://github.com/BH2-4/agent-delivery-loop/blob/423dbd56f97ff4f25d2af2aea29cf7ce39a841b3/receipt.json)
对应待审查的 [Delivery PR #47](https://github.com/BH2-4/agent-delivery-loop/pull/47)。status absent
仅表示查询未见共享执行记录，不证明首次 unknown 没有远端副作用。此例证明一次经人工恢复的 Claude
文档交付，不证明 Cron 自动唤醒到任务完成的无人干预闭环、长期稳定或代码类任务。下一迭代提案：
[模型调用前的本机任务预筛选](pull-model-prefilter-plan.md)。

## 目标与约束

用户已有的本机定时任务负责唤醒对应电脑上的当前 Agent。安装的 Skill/CLI 等接入件
在该会话中读取已授权任务和固定上下文，先核验能力，再确定性领取；当前 Agent
使用本端工具和凭据完成工作并写共享回执。接入件只返回上下文和许可，不启动第二个
Agent，也不创建调度器、守护进程或跨电脑唤醒服务。

通用任务采用独立的 `pull-task-v1`，文件位于
`.agents/pull-tasks/{task_id}-r{revision}.json`。首个来源适配器用已合并 Plan PR
作为授权证据，并把 merge SHA、路径、任务摘要绑定到领取；来源适配器是 GitHub
不等于公共任务语义必须依赖 GitHub、PR 或代码交付。提示词、Issue、评论和任务文件
中自称“已批准”都不是授权。旧 Plan PR + Work Order v1 继续沿用原 Claude Worker
语义，不能被 pull 入口静默改成任意 Agent 可执行。

任务清单是固定修订；领取、进度和回执保存在分离的共享执行记录中。能力不足时在
写入领取前返回不匹配；共享写入结果未知或执行/副作用不明时保持待确认并停止。
不加租约、续租、自动接管或清除旧防重记录的重跑路径。模型凭据和 transcript 不
进入共享任务或回执。

## 首期数据与命令合同

清单只描述任务，不把执行结果追加回去。字段保持小而通用：

- `schema_version` 为整数 `1`，另有稳定 `task_id` 和正整数 `revision`。
- `goal` 与 `instructions` 描述目标和持久工作说明；不要求目标是 Git 仓库或代码交付。
- `scope` 是含 `constraints` 字符串数组的对象；`acceptance_criteria` 与
  `required_capabilities` 是字符串数组。缺少必需能力必须在共享领取前拒绝。
- `context` 项为 `{path, sha256}`；路径从已合并 Plan PR 的固定 merge SHA 读取，
  并核验摘要。不要依赖当前聊天记录补全目标或上下文。
- 授权证据由来源适配器独立核对，不由清单自述。首期只接受显式给出的
  `--repo`、`--plan-pr`、`--task-path`，并核验任务文件来自该已合并 PR 的固定提交。

首期安装入口为独立 `agent-pull` CLI（协议标识 `agent-pull/1`）。`discover --repo`
只列出已合并 PR 中新增/修改的任务路径，不授予执行权；`show` 读取并核验固定任务包；
`acquire` 返回本次许可和上下文，随后由调用它的 Agent 自己执行；`report` 写当前回执；
`status` 只读查询共享状态。任务命令显式使用
`--repo OWNER/REPO --plan-pr URL|OWNER/REPO#N --task-path .agents/pull-tasks/<id>-rN.json`
（authority branch 默认为 `main`）。`acquire` 还接收重复的 `--capability`、稳定的
`--claimant-id`、说明执行端的 `--harness` 和本机私有 `--grant-file`；`report` 接收同一
授权参数、grant 文件及 `--status running|completed|blocked|failed`，并可给出简报、下一步
和产物引用。暂不提供 `TASK@REV` 索引解析，也不自动把旧 Work Order 转换成通用任务。
写操作另需明确的 `--github-pat-file` 和 `--github-login`。`--capability` 是当前 harness
对自己实际可用能力的声明，CLI 只与任务要求作名称匹配，不探测或证明本机工具能力。
实现输出结构是命令契约的一部分，随首期 CLI 一并定稿。

回执与任务分开，至少绑定任务修订/摘要和唯一执行 ID，记录状态、执行端及版本、
简短结果/阻塞原因、可核验产物引用和更新时间。状态须区分工作中、已完成、阻塞、失败
及待确认；`completed` 只说明任务工作结果已回传，不暗示审查、CI、合并或部署完成。
写入结果未知时进入待确认并停止，不重放。
回执不可写入或改写任务清单。

## 实施阶段与验收

### 阶段 1：可用的 GitHub Pull 切片

1. 在 `src/agent_delivery_loop/pull*.py` 实现 `pull-task-v1` 解析/校验、Plan PR
   固定修订授权核验、能力预检、确定性 acquire、回执写入和只读 status；共享记录
   复用现有 GitHub 原子防重能力时，执行身份必须和旧 Work Order 命名空间隔离。
2. 暴露 `agent-pull` CLI，按显式仓库、PR 和任务路径获取与回写；提供短 Skill
   说明“当前调用 Agent 自己执行、完成后必须 report”，任何路径都不得 spawn Worker。
3. 提供一个通用文档/共享资料任务样例，证明目标和产物不必是代码 PR。旧
   `.agents/work-orders/`、`agent-run`、`agent-watch` 与 `agent-delivery deliver`
   保持现有行为。

**验收：**项目现有 CI 的源码编译/构建通过，并经代码审查确认授权固定、能力不足时
不领取、写入结果未知即停止、回执与任务分离，且 pull 入口不启动 Agent。对新代码只
在构建或审查出现具体失败信号时补充针对性检查，不增加大套并发/故障回归门槛。
阶段 1 与本地安装更新切片完成后，由用户已有的本机定时 harness 实际唤醒一个 Agent，
完成一项低风险任务并回执一次；没有这个真实轮次之前明确标为“未实测”，不得写成自动
化已完成。本阶段不要求多电脑试用。

### 阶段 2：可信本地安装与自动更新（已实现，可与阶段 1 并行）

1. 先用审查过的普通 wheel 在独立 Python 3.11+ bootstrap venv 安装
   `agent-pull-install`。第一次启动不是从浮动 PyPI 包或任意在线 URL 下载。
2. 安装器只接受固定的 HTTPS GitHub `OWNER/REPO` 和一个 branch ref。PR #43 已将首期源码合入
   `main`，安装和更新使用 `--ref main`；ref 必须是分支名，不支持 SHA ref。
   安装器 fetch 后解析精确 commit SHA，从该提交构建普通 wheel，校验 wheel 内 Skill
   摘要；版本化 venv 与版本回执放入 managed root，渲染后的 Skill 写入明确指定的
   `--skill-dir`。回执记录
   source/ref/SHA、package/protocol version、Skill SHA-256 与更新时间；不是外部发布清单，
   不宣称有签名 release manifest。
3. 每台主机一次明确 `--root`（独立 managed root）和 `--skill-dir`（当前 harness 能发现
   的专用空目录）；两路径必须分开。更新只沿已保存的固定来源/ref 拉取、构建和激活
   经核验的新版本，不动其它宿主配置，也不读取模型凭据。
4. 已安装的 `agent-pull` Skill 每次激活先调用 `<root>/bin/agent-pull-update`。返回
   `current` 才继续当前轮；返回 `updated` 时当前会话停止，待宿主下一次唤醒加载新 Skill。
   这由已启动 Agent 的薄 Skill 入口完成，不要求宿主支持额外外部前置脚本。

**验收与已观察项：**`compileall`、Skill quick_validate、空格路径 launcher smoke、两类
激活中断恢复模拟与 active receipt/SHA 对齐检查通过；隔离 wheel 曾成功构建，但最新门禁
修复后未重建。没有真实安装该 wheel、从 GitHub 更新实际来源、GitHub 任务领取/回执写入
或宿主定时验证；
因此本地安装/更新在真实用户路径上仍标为未验证。远程机制更新不替代本地安装件更新验收。

### 阶段 3：按真实需求扩展来源和接入

首期单来源路径和更新器通过各自验收后，再逐项接入其他共享任务源或各 harness 的薄 Skill 包装。
每种新来源都必须给出独立、可验证、绑定不可变修订与摘要的授权证据；增加新 Agent
端不能改变旧 Work Order 权限，也不自动扩大用户已批准任务的工具或交付范围。

## 首次接入和一次定时轮次

PR #43 已将 pull 代码合入 `main`。先核对 `main` 的准确提交，在受信环境准备 bootstrap
wheel 并安装到独立 bootstrap venv；安装器首次 fetch 时用 `--ref main` 解析实际
`source_sha`，不支持 SHA 作为 `--ref`。必须核对输出 `source_sha` 等于事先核对的 `main`
提交；`main` 已移动、SHA 不一致或来源不符时停止。POSIX 示例：

```sh
python3.11 -m venv "$HOME/.local/share/agent-delivery-loop/bootstrap-venv"
"$HOME/.local/share/agent-delivery-loop/bootstrap-venv/bin/python" -m pip install \
  "/absolute/path/agent_delivery_loop-<review-sha-wheel>.whl"
"$HOME/.local/share/agent-delivery-loop/bootstrap-venv/bin/agent-pull-install" \
  --source https://github.com/BH2-4/agent-delivery-loop.git \
  --ref main \
  --root "$HOME/.local/share/agent-delivery-loop/pull" \
  --skill-dir "$HOME/.agents/skills/agent-pull"
```

`--skill-dir` 必须是由用户确认、宿主可发现、专供该 Skill 的空目录；按当前使用的
harness 调整示例路径，不覆盖其它 Skill。首次安装输出 `installed`，后续重跑若来源 SHA
未变化则输出 `current`；输出应能核对来源、SHA、protocol/package version、Skill digest
和 launchers。更新命令为
`<root>/bin/agent-pull-update`；状态 `current` 可继续，`updated` 必须结束这轮，下一次
宿主唤醒时才加载新 Skill。若重新安装改变了来源 SHA，也结束当前旧会话，下次唤醒再加载。

本机用户在已有的 harness 私有设置中明确保存任务源 `repo`、`authority_branch`、已有
细粒度 PAT 文件路径和 `expected login`。这只是本机连接元数据，不在项目中新增配置系统，
也不保存 token 内容。当前 Agent 读取这四项并透传；它不得搜索凭据文件、打开/打印 token
内容或向用户索要 secret。缺少设置时停止为 `blocked`。`discover --repo OWNER/REPO`
发现候选后，Plan PR URL 与 `task_path` 从 discovery 结果带入后续 `show/status/acquire`；
不要求每发现一张任务就改定时提示词。写操作使用已配置的 PAT 路径及 `--github-login`。

可复制到用户已有的原生定时任务中的提示词：

```text
每次唤醒后读取本机已安装的 agent-pull Skill，并按其正文执行；使用本机私有设置中的 repo、authority_branch、PAT 文件路径和 expected login。每轮最多领取并执行一张任务：discover 后用返回的精确 Plan PR 和 task_path 执行 show/status，再只在授权和能力匹配时 acquire。只有本轮 acquire 返回 can_execute=true 才由当前会话亲自执行，先 report running，完成/阻塞/失败后 report 对应结果并检查 status。不要启动另一个 Agent。更新返回 updated 时结束本轮，让下次唤醒加载新 Skill；pending_confirmation、pending_gate、未知写响应或中断恢复一律只读 status 并等待人工核对，绝不重领、重报或自动继续。缺少本机身份配置时报告 blocked；不要搜索、读取、输出或请求 token 值。
```

`--claimant-id` 如果指定，应在本机稳定配置；省略时 CLI 为该调用生成 UUID。
receipt 与 grant 文件包含执行关联信息，grant 文件应放在 managed root 的私有 `state/grants`
目录。模式主要信任当前 OS 用户、已配置 GitHub PAT 与其权限；同一 UID 下可读写文件的进程，
以及持有同一 PAT 的主体，都在信任边界内，不承诺隔离恶意同 UID 进程或恶意同凭据用户。

静态检查和临时目录恢复模拟不等于首次真实轮次。#45 已由 `arco-mac-zcode` 完成真实
授权读取、共享 acquire/report 与回执回读，但该轮是否由原生 scheduler 唤醒缺乏宿主
元数据证据；`PULL-DOCS-002-r1` 则是用户授权一次人工重试后由 Claude 完成的真实文档任务，
没有证据表明它由 Cron 唤醒。在被证实的 scheduler 唤醒并完成同一链路之前，不记录定时轮次通过。

## 首个切片边界与暂不做

首个完整工程切片包括通用任务清单、一个 GitHub Plan PR 来源适配器、
discover/show/acquire/report/status、最小 Skill/样例，以及从固定可信来源安装并更新
本地接入件的可用路径。核心命令和 updater 可以并行开发、分开审查，但两者都属于首期
验收。只修改 pull 模式新增文件与注册入口；不重写 `WorkOrder`、Claude Worker 或旧
watcher，不改远程默认分支，不自动合并或部署。

后续再评估租约/自动接管、多源扫描、跨设备唤醒、新中央服务、MCP 服务、多端强制
试用、复杂更新签名/离线恢复、自动审查通知和更大的长期工作流。首期不以这些为前置。

## 状态与接班

当前状态：真实来源安装/更新与一次真实任务领取/回执写入已观察（#45，`arco-mac-zcode`，
回执见共享记录，不证明长期稳定）。Claude Code 已以独立 claimant `arco-mac-claude-code`
接入并完成人工只读首扫；随后 `PULL-DOCS-002-r1` 首次 acquire unknown、status 复查 absent，
按执行端报告由用户额外授权一次人工重试后完成 claim/start/running/completed（[completed 回执](https://github.com/BH2-4/agent-delivery-loop/blob/423dbd56f97ff4f25d2af2aea29cf7ce39a841b3/receipt.json)），
候选交付于待审查的 [Delivery PR #47](https://github.com/BH2-4/agent-delivery-loop/pull/47)。status absent
不证明首次 unknown 没有远端副作用。会话内原生 Cron 创建成功仅为创建证据。仍未验证：Cron
自动唤醒并驱动 Claude 完成任务的完整链路、长期稳定性与代码类任务。下一步按
[预筛选提案](pull-model-prefilter-plan.md)研究模型调用前的本机筛选；在此之前不把
Cron 创建或模拟结果说成真实定时轮次。
