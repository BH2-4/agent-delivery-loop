# 第一次人工交付：从引导实现到两张真实工单

归档日期：2026-10-04。历史截点：[PR #8 合并提交 `bc8445cb3cc8ec59666c13a74c80f94c3c75105e`](https://github.com/BH2-4/agent-delivery-loop/commit/bc8445cb3cc8ec59666c13a74c80f94c3c75105e)。

这份档案保存我们第一次手动推动交付流程时的实际经过，避免后续自动化覆盖掉这些经验。它是根据 GitHub 记录、本机脱敏运行元数据，以及当时对话中的终端回执和审查报告整理的纪实，不是完整会话的逐字转录。

> 本文是历史记录，不是新的执行授权。下面的命令是脱敏后的历史示例或模板，**不要照抄重跑已完成的工单**。本次归档没有启动 Worker、调用模型、重新测试历史任务或改写运行记录。当前操作规范以 [bootstrap](../bootstrap.md) 和[人工交付检查清单](../manual-delivery-checklist.md) 为准。

## 1. 这第一遍到底完成了什么

- 第一张工单 `WO-PILOT-001-r1` 产出了真实候选成果，但 `agent-run` 退出码为 **2**。后来经人工返修、独立审查、CI 和 Delivery PR #5 合并接纳；原调用仍是失败，没有重跑，也没有改写失败历史。
- 第二张工单 `WO-PILOT-002-r1` 使用修复版执行器，`agent-run` 退出码为 **0**，记录为 `local_ready` / `stopped`。再经人工返修、独立审查、CI 和 Delivery PR #7 合并，完成了一次正常的人工监督文档交付闭环。
- PR #8 将这两次结果写回 README 和 bootstrap。它是文档收尾，不是第三次 Worker 执行。

这里的“人工”指人逐阶段授权、启动、转交信息和决定接纳，其中一些命令由会话中的 Agent 代办；不表示每条命令都由人亲自输入，也不表示 Worker 亲自推送或合并了 PR。

## 2. 可回查的时间线

以下时间统一为 UTC；北京时间为 UTC 加 8 小时。Worker 时间来自运行记录，PR 时间来自 GitHub 的 `merged_at`。

| 时间（UTC） | 发生的事 | GitHub 证据或执行结果 |
| --- | --- | --- |
| 2026-10-03 08:31:21 | 经多轮修复和复审，合并最小执行器 | [引导 PR #1](https://github.com/BH2-4/agent-delivery-loop/pull/1)，merge `193b7f15a269777bca3e151f6393b14884183f0b` |
| 2026-10-03 09:11:08 | 合并第一张工单的开工授权 | [Plan PR #2](https://github.com/BH2-4/agent-delivery-loop/pull/2)，merge `0038259bad1a23f737c5585f66a49b8c3721289a` |
| 2026-10-03 13:13:35 | 合并显式 Worker 配置和供应商绑定修复 | [修复 PR #3](https://github.com/BH2-4/agent-delivery-loop/pull/3)，merge `f4661f9616201ce38be0468f39ee69d493cc0847` |
| 2026-10-04 03:37:03–03:38:43 | 第一张工单真实运行 | Worker `complete`；原命令退出码 2 |
| 2026-10-04 04:17:26 | 合并 tuple/list 运行记录回读修复 | [修复 PR #4](https://github.com/BH2-4/agent-delivery-loop/pull/4)，merge `fb5f16590f6e4b7609c0114858967d456d93e525` |
| 2026-10-04 07:51:35 | 人工接纳第一张工单的候选成果 | [Delivery PR #5](https://github.com/BH2-4/agent-delivery-loop/pull/5)，merge `857610db0b7f72d5cc423b98137e3194f8e45d68` |
| 2026-10-04 08:14:29 | 合并第二张工单的开工授权 | [Plan PR #6](https://github.com/BH2-4/agent-delivery-loop/pull/6)，merge `cde271b5180d10738b2955ef9f073668be790e20` |
| 2026-10-04 08:23:02–08:25:46 | 第二张工单真实运行 | Worker `complete`；原命令退出码 0，`local_ready` / `stopped` |
| 2026-10-04 08:53:54 | 人工接纳第二张工单的交付 | [Delivery PR #7](https://github.com/BH2-4/agent-delivery-loop/pull/7)，merge `15dc48a881d75e19af7e21451a6d5625d0ad286a` |
| 2026-10-04 09:41:29 | 合并实验结果与验证边界的说明 | [维护 PR #8](https://github.com/BH2-4/agent-delivery-loop/pull/8)，merge `bc8445cb3cc8ec59666c13a74c80f94c3c75105e` |

运行记录中的起止时间不是整条人工交付流程的耗时，也不是 Token 或账单统计。

## 3. 当时如何手动串起完整流程

### 3.1 讨论需求，准备 Work Order，再创建 Plan PR

用户与 Codex / Steward 先讨论目标、不做什么、验收条件、允许修改范围、固定 Worker 配置、Skill 和停止条件。准备任务的 Agent 把这些写成 `.agents/work-orders/{task_id}-r{revision}.json`，解析检查后提交计划分支并创建 Plan PR。

第一张只允许新增 `docs/glossary.md`，第二张只允许新增 `docs/manual-delivery-checklist.md`。准备任务的 Agent 没有因此获得执行资格，也没有启动 Worker。执行资格要等用户合并 Plan PR。

准备阶段的脱敏命令模板如下；当时还使用了 `PYTHONPATH=src python3 -m agent_delivery_loop validate <work-order-json>` 解析工单：

```sh
agent-delivery validate <work-order-json>
git diff --check
git push --set-upstream origin <plan-branch>
gh pr create --repo BH2-4/agent-delivery-loop \
  --base main --head <plan-branch> \
  --title <plan-title> --body <plan-summary>
```

### 3.2 用户核对指定版本，再合并计划

用户查看 PR 是否仍开放、目标分支是否为 `main`、head 是否与审查版本一致，以及 CI 是否通过，然后用完整 head SHA 限定合并。

```sh
gh pr view <plan-pr-number> --repo BH2-4/agent-delivery-loop \
  --json state,baseRefName,headRefOid
gh pr checks <plan-pr-number> --repo BH2-4/agent-delivery-loop
gh pr merge <plan-pr-number> --repo BH2-4/agent-delivery-loop \
  --merge --match-head-commit <reviewed-full-head-sha>
gh pr view <plan-pr-number> --repo BH2-4/agent-delivery-loop \
  --json state,mergedAt,mergeCommit
```

当时 Plan PR #2 和 #6 都由用户明确合并。审查用的 head SHA 与合并后产生的 merge SHA 是两个不同标识；执行器查询 GitHub 获得后者，再从该提交读取授权。**合并计划没有自动启动本机。**

### 3.3 检查本机入口、安装版本和固定配置

人工先检查分支、工作树、Python 入口、CLI、认证来源和安全门禁。已有 `.DS_Store` 保留，没有随手删除；不覆盖他人的未提交改动。

```sh
git branch --show-current
git status --short
git pull --ff-only origin main
git rev-parse HEAD
env -u PYTHONPATH .venv/bin/agent-run --help
```

拉取源码不等于更新已安装执行器。首轮使用 PR #3 合并提交构建的普通 wheel；第二轮在授权后安装 PR #4 修复版普通 wheel，并核对安装包 11 个 Python 模块与 Plan PR #6 授权基线对应模块一致。构建来源和 wheel SHA-256 在本机安装回执中留痕，详见后文。

Worker 请求配置固定为 Claude Code CLI、`glm-5.3`、`https://open.bigmodel.cn/api/anthropic`、`max` effort。模型认证从明确指定的仓库外 settings 文件读取，CC Switch 配置保持原样；旧 shell 中的模型与供应商设置不覆盖显式配置。这里记录的是请求配置和实际调用结果，不证明供应商实际推理强度、长期可用性或账单扣费规则。

### 3.4 人工启动一次真实 Worker，并保留真实退出结果

下面是第二次运行命令的脱敏展示。认证文件路径已替换，**不是再次执行 WO-PILOT-002 的指令**。

```sh
env -u PYTHONPATH -u http_proxy -u https_proxy -u all_proxy \
  HTTP_PROXY=http://127.0.0.1:12451 \
  HTTPS_PROXY=http://127.0.0.1:12451 \
  ALL_PROXY=http://127.0.0.1:12451 \
  .venv/bin/agent-run \
  --plan-pr https://github.com/BH2-4/agent-delivery-loop/pull/6 \
  --work-order-path .agents/work-orders/WO-PILOT-002-r1.json \
  --model glm-5.3 \
  --base-url https://open.bigmodel.cn/api/anthropic \
  --effort max \
  --auth-config /path/outside/repository/claude-settings.json

adl_trial_exit=$?
printf 'agent-run exit=%s\n' "$adl_trial_exit"
```

执行器创建独立 worktree、全新 Claude Session 和临时 HOME，核实授权、完成状态和允许路径，生成本地候选提交。两次实际运行的 Session 不同，Claude Code 版本记录均为 `2.1.289`。没有续接用户正在交互的会话，也没有保存完整 transcript。

第一张的 Worker 返回 `complete`，已有本地提交，磁盘记录也出现 `local_ready` / `stopped`，但命令退出码是 **2**。这个失败没有被“看起来做完了”掩盖。第二张的命令退出码 **0**；记录回读通过，Worker 确认停止，临时 HOME 清理，才认定本地执行阶段完成。

### 3.5 人工检查、明确授权返修，再独立审查

执行结束后，人工核对实际分支、提交父节点、修改范围和文档内容。两张工单都出现过需要人工修正的文案；修正是在明确授权后由 Codex 进行，不是后台自动重试，也不是恢复已经销毁的 Claude 对话。

每次新增返修提交后，独立只读审查绑定最终完整 head SHA；旧 head 的审查不拿来放行新 head。原运行记录继续关联 Worker 原始提交，人工修正的提交另行保存在 Git 历史中。

第一张没有重新调用 `agent-run`。第二张同样没有为文案返修重跑 Worker。当前执行器没有接手原 Delivery 分支的续修入口，不能把重复调用 `agent-run` 当成接着原成果返修。

### 3.6 人工推送、创建 Delivery PR，等待 CI 并决定合并

发布操作由人或会话内获授权的代办使用人的 Git/GitHub 身份进行，不由 Claude Worker 使用个人 `gh` 或 App 凭据进行。推送后先核对远端分支，再创建 Delivery PR；检查结果与语义审查必须对应同一交付版本。

```sh
git push --set-upstream origin <delivery-branch>
git ls-remote --heads origin <delivery-branch>
gh pr create --repo BH2-4/agent-delivery-loop \
  --base main --head <delivery-branch> \
  --title <delivery-title> --body <delivery-summary>
gh pr checks <delivery-pr-number> --repo BH2-4/agent-delivery-loop
```

CI 自动完成源码编译、已有聚焦回归、schema JSON 解析和示例 Work Order 解析。它不验证每张新工单，也不代替 Codex 对任务意图和内容的审查。用户取得 CI 与独立审查结论后，按最终 head SHA 人工合并并核对 `MERGED` 和 merge SHA。

PR #5 接纳了第一张工单的候选成果，PR #7 接纳了第二张。之后主工作目录同步 `main`，再通过 PR #8 汇报并记录结果。**进入 `main` 不等于部署上线；这条流程没有部署环节。**

## 4. 两张工单的证据索引

### WO-PILOT-001-r1：原执行失败，后来人工接纳

- [授权工单固定版本](https://github.com/BH2-4/agent-delivery-loop/blob/0038259bad1a23f737c5585f66a49b8c3721289a/.agents/work-orders/WO-PILOT-001-r1.json)：Plan PR #2 merge `0038259bad1a23f737c5585f66a49b8c3721289a`。
- Work Order canonical SHA-256：`3c7bb2d2719ef01421298964ec279cd51cfd684d5b5d8014cebace74597f440f`。
- Worker 原始提交：`b6af9114157a784aa65091868382c1fbd7c1218d`；仅新增 `docs/glossary.md`，原命令退出码 2。
- 人工返修后的审查 / PR head：`5ed83060606fc08c6a0ae4d827535a9b87a34f72`；[对应 CI 成功](https://github.com/BH2-4/agent-delivery-loop/actions/runs/37186784878)。
- Delivery PR #5 merge：`857610db0b7f72d5cc423b98137e3194f8e45d68`；[合并时的术语表](https://github.com/BH2-4/agent-delivery-loop/blob/857610db0b7f72d5cc423b98137e3194f8e45d68/docs/glossary.md)。
- 原安装源码：`f4661f9616201ce38be0468f39ee69d493cc0847`；wheel SHA-256：`a909ae1d78027e0db65625b9252e961f7fa0e3999905b6f93f4e94c685011ca6`。

### WO-PILOT-002-r1：正常执行，后来人工交付

- [授权工单固定版本](https://github.com/BH2-4/agent-delivery-loop/blob/cde271b5180d10738b2955ef9f073668be790e20/.agents/work-orders/WO-PILOT-002-r1.json)：Plan PR #6 merge `cde271b5180d10738b2955ef9f073668be790e20`。
- Work Order canonical SHA-256：`72ef2cdb41e47ddd7999b2606304264e0e7ffc7282bda1d6c1f672f9ce0b8be5`。
- Worker 原始提交：`31be68cad2e24566eae51b82a96a7660379ab8b1`；仅新增 `docs/manual-delivery-checklist.md`，原命令退出码 0，`local_ready` / `stopped`。
- 人工返修后的审查 / PR head：`dbff77762e1a42b68adcb1f7d583f84819bd452c`；[对应 CI 成功](https://github.com/BH2-4/agent-delivery-loop/actions/runs/37190023251)。
- Delivery PR #7 merge：`15dc48a881d75e19af7e21451a6d5625d0ad286a`；[合并时的检查清单](https://github.com/BH2-4/agent-delivery-loop/blob/15dc48a881d75e19af7e21451a6d5625d0ad286a/docs/manual-delivery-checklist.md)。
- 修复版安装源码：`fb5f16590f6e4b7609c0114858967d456d93e525`；wheel SHA-256：`1ae2bbc8cc03d0ae37e357bf9cd685460bb1d7942a6b930f553d5d6f2fc2afb5`。

两张工单使用的 Delivery Skill SHA-256 均为 `f114abd02020b1871cac523471fd514febd3c4a2ff780cc570512046b9407ea2`。CLI 返回码来自当时终端回执，其他选取字段由归档时只读核对运行记录；两份原始记录保持不变。上述独立审查结论属于当时的审查，本次归档没有重新审查历史代码，也未请求重跑上述历史 CI。归档 PR 触发的常规 CI 是新提交的检查，不是重跑这些 Work Order。

## 5. 不应该被忘掉的卡点

| 卡点 | 当时如何处理、应该保留什么认识 |
| --- | --- |
| 引导代码多次审查未通过 | 修复了完成状态、watch 分页、交付路径、提交钩子 / 最终提交核对，以及启动和取消的安全登记问题，再按新 SHA 复审。CI 绿灯不代表这些风险已经被语义审查排除；替身回归不等于真实异常停止已验证。 |
| GitHub 网络代理端口不一致 | 用户将本机网络代理指向 `12451` 后，`gh auth status` 检查成功。网络代理与模型 API 端点不是一个东西；最初的登录错误不能单独证明令牌本身已经失效。 |
| editable Python 入口反复失效 | `.pth` 的隐藏标志从 `32832` 清为 `64` 后入口短暂可用，约十秒后又回到 `32832`；用户普通终端也观察到回变。改用普通 wheel 去掉对 editable `.pth` 的依赖。回变根因未知，不归因于 Finder、CC Switch 或某个后台程序。 |
| CC Switch 与父环境可能不同步 | 不猜测哪个全局配置应该覆盖哪个；通过 PR #3 固定显式模型、端点、effort 与认证来源，不改变用户日常 CC Switch 设置。配置检查不是模型连接成功证明。 |
| 首次 Worker 做完，但执行器报错 | 路径 tuple 经 JSON 回读变成 list，严格对象比较失败。PR #4 在记录边界转换为 list，保留严格验证和安全门禁；没有通过改写旧记录把第一次调用“修成成功”。 |
| 首次创建 PR #5 报 head/base SHA 错误 | 后续确认远端分支存在，再次创建成功。证据不足以给最初错误确定单一根因；发布前应核对远端分支，而不是无条件重复创建。 |
| 人工返修后的版本与 Worker 原版本不同 | 原提交、返修 head、审查 SHA、CI 和 merge SHA 分别留痕；原运行记录不回填为人工返修后的版本。第二张的返修还修正了“所有失败都保留 HOME”的过强表述。 |

## 6. 当时没有验证的能力

截止这个历史截点，观察到的是两次低风险文档任务，其中一次执行器正常结束后的人工交付闭环。尚未证明：

- 重复运行的稳定性、长期持续可用性或真实代码开发任务。
- 真实 Claude 的 Ctrl+C、超时、启动故障及脱离进程组的后代处理。
- 操作系统级凭据隔离、管理员托管策略，以及兼容端点下的预算 / 扣费硬上限。
- GitHub App 权限、私钥隔离、`main` 保护和无人值守 `--publish`。
- `agent-watch --once` 的真实发现路径；这两次均直接调用 `agent-run`。
- 定时触发、自动返修或重试、跨电脑领取、自动合并及部署。

后续即使增加了这些能力，也不能把它们写成第一轮已经验证。

## 7. 如何长期保存和引用

- README 和 bootstrap 保存当前状态，并链接本档案；本档案保留这一轮历史，不跟着后续状态改写结论。
- 更晚的试运行另建带日期的档案。若发现本档案有事实错误，追加有依据的更正或修订，通过 PR 留痕，不删除失败记录。
- 授权工单与交付文件的链接固定到完整提交，不指向容易变化的 `main` 文件；PR 链接保留过程中的讨论和提交历史。
- GitHub 上保存脱敏纪实，不保存个人凭据、认证文件、完整会话或本机私有路径。原始运行记录仍保留在私有状态目录。

这份档案不是新 Work Order、ADR 或自动化运行器；归档完成不授权启动下一轮实验。
