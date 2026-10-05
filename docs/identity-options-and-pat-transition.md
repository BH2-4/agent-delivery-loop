# GitHub 身份备选与 PAT 阶段接入讨论稿

日期：2026-10-05（Asia/Shanghai）

## 当前选择与状态

用户已确认：「保留你的几个提案，然后我们选择 PAT。」下一阶段采用 **fine-grained PAT（细粒度个人访问令牌）**，优先降低单机 Mac 接入成本；其他路线保留，不要求以后必须迁回 App。

**已确认的是路线选择，不是接入完成。** 权限清单、凭据保存方式与具体运行授权仍待核定。本稿不构成 Work Order，也不授权创建令牌、更改登录、推送、合并、修改仓库规则或启动长线 Goal。

初次记录时本地为 `main`，HEAD 为 `b80745e75027af4c3fae8e78e459bbee56f7fae4`。当时仍有使用个人 `gh` 身份的实验编排路径，以及独立的 App 发布入口；路线选择本身不证明 PAT 接入已验证。

用户随后授权调研与修改。本地分支 `feat/fine-grained-pat-identity` 已实现显式 PAT 文件／账号、只读预检、统一发布身份及续接绑定；尚未替换安装、接入真实 PAT 或执行新交付。细节见 [PAT 接入说明](pat-setup.md)，正式 ADR 见[待确认草案](adr-0007-pat-draft.md)。

## 保留的方案

| 方案 | 适合的情况 | 主要代价／边界 | 本阶段位置 |
| --- | --- | --- | --- |
| 复用现有个人 `gh` 登录 | 人工操作、短期引导 | 自动化混用日常身份，权限可能很宽 | 保留作人工工具；过去的限次授权不自动续期 |
| 细粒度 PAT | 当前单机、本地 CLI 编排 | 仍是个人身份；需管理过期、撤销与泄漏风险 | **选用，本地实现，待审查／安装／实测** |
| 托管 GitHub Actions + 内置 `GITHUB_TOKEN` | 把 GitHub 机械操作移到云端工作流 | 需设计本地候选交接；事件触发存在限制 | 保留备选 |
| GitHub App + 托管 Actions 发布 | 希望独立机器人身份，并避免在 Mac 保存 App 私钥 | 仍要注册 App、保存云端私钥、隔离可信发布与不可信代码 | 保留备选 |
| GitHub App + Mac 独立发布身份／进程 | 需要本地发布且希望更强凭据边界 | 系统身份、权限与通信隔离维护较复杂，需实测 | 暂缓，保留备选 |

内置 `GITHUB_TOKEN` 是工作流临时身份，不是普通 Mac CLI 可以直接领取的日常令牌；它触发后续工作流的行为也有特殊限制。不能笼统假设用它创建 PR 就一定自动跑完 CI。[GitHub：GITHUB_TOKEN](https://docs.github.com/en/actions/concepts/security/github_token)

App 放在托管 Actions 中可以把私钥保存在 Actions Secret，但不等于本地候选成果已经有安全交接渠道；带密钥的发布工作流也不能随意执行 Worker 提供的代码。[GitHub：在 Actions 中使用 App](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/making-authenticated-api-requests-with-a-github-app-in-a-github-actions-workflow)

## PAT 最小接入草案

选择 fine-grained PAT，不回退到 classic PAT。资源所有者拟为 `BH2-4`，仓库选择仅 `agent-delivery-loop`；建议初次有效期 30 天，具体期限待确认。这里限制的是选定仓库的授权范围，不能声称 PAT 无法读取其他公开仓库。[GitHub：管理 PAT](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens)

| 权限 | 初步建议 | 用途与待验证项 |
| --- | --- | --- |
| Metadata | Read | 仓库基本信息 |
| Contents | Read and write | 读取授权基线、推送交付分支；同时可能允许合并 PR |
| Pull requests | Read and write | 查询、创建与更新 Delivery PR |
| Actions | Read | 追加调研后选择运行／jobs REST 读取；不依赖 Checks API |
| Checks、Commit statuses | 不申请 | 本阶段不使用检查聚合接口 |
| Issues | 暂不申请 Write | 若本阶段明确纳入进度 Issue，再核定必要权限 |
| Administration、Workflows、Actions／Checks 写权限 | 默认不申请 | 不改保护规则、不修改工作流、不伪造或重跑检查 |

PR 创建支持细粒度 PAT；合并接口要求的 `Contents:write` 与推送需求重叠。因此不能靠令牌权限本身保证「只推送、绝不合并」，也不能把未验证的 `main` 规则当作保护。[GitHub：PR API](https://docs.github.com/en/rest/pulls/pulls)

追加调研选择 `Actions:read` 的 workflow run／jobs REST 端点，以避开 Checks 支持说明和权限选择的不确定性；当前固定 CI 契约为 `ci.yml`／`validate`，不声称检查所有外部 App 状态。公开资源部分可匿名读取，但认证失败不能静默换身份绕过验证。[Workflow run API](https://docs.github.com/en/rest/actions/workflow-runs#get-a-workflow-run)、[Jobs API](https://docs.github.com/en/rest/actions/workflow-jobs#list-jobs-for-a-workflow-run-attempt)

## 凭据与失败边界

- 保留用户日常 `gh` 登录；不通过重新登录来覆盖钥匙串，不在全局 shell 配置中导出 PAT。
- 由外层发布器向必要的 GitHub 子进程显式传入身份；`GH_TOKEN` 会优先于存储的登录凭据。缺失、过期或权限不足时停止，不静默回退个人登录或扩大权限。[gh 环境变量说明](https://cli.github.com/manual/gh_help_environment)
- PAT 不交给 Claude Worker，不放进 Work Order、提示词、仓库、命令参数或日志；不要求用户在聊天中粘贴令牌。具体保存与读取方案须在创建前说明。
- 同一 macOS 用户下，清理 Worker 环境不等于操作系统级凭据隔离。PAT 降低配置复杂度，但没有解决这一风险；必须如实记录剩余边界。
- 已合并 Plan PR 授权、固定基线、新 Session、允许路径、完成状态、停止确认、独立审查、绑定 head 的 CI 与写后核对仍保留；选用 PAT 不放宽这些门禁。
- 现有 `agent-run --publish` 的 App 门禁保持不变。不把 PAT 塞进 App 接口，不伪造 App／隔离／保护已验证标记。

## ADR 过渡与实施顺序

1. **先确认正式决策草案。** 拟新增 ADR-0007，说明当前阶段改用细粒度 PAT，以及对 ADR-0002 的替代范围；保留旧文本、增加互相引用并更新索引。不把历史 App 决策或已结束实验改写成 PAT 已验证。
2. **再实现显式身份入口。** 明确 PAT 来源、必要权限、错误返回和禁止回退；保持 App 接口独立。先编译受影响代码，仅按具体失败信号补充聚焦验证。
3. **随后由用户本地创建并保存凭据。** 不接收聊天中的密钥；先实际验证身份、目标仓库、授权 PR 与 CI 的只读访问。只读通过不等于写入或合并能力通过。
4. **最后申请并执行一轮有界实验。** 明确任务、时间窗、允许的 GitHub 写操作与是否包含合并，再验证交付分支、PR、CI 和写后结果；不重跑历史工单，不把旧的限次授权延长。

初次记录仅保存方案；追加修改不改变验证边界：**没有创建 PAT、修改用户认证、改变规则或运行 Worker；PAT 真实链路尚未验证。**
