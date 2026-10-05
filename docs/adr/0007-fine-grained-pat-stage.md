# ADR-0007: 当前单机阶段采用细粒度 PAT

**日期**：2026-10-05<br>
**状态**：accepted（限当前单机阶段的身份选择；由 [ADR-0007 草案](../adr-0007-pat-draft.md) 经用户批准正式化）<br>
**替代范围**：仅替代 [ADR-0002](0002-github-app-unattended-identity.md) 在**当前单机阶段**的执行身份选择；ADR-0002 的历史正文、风险记录与 App 发布接口、门禁全部保留，App 仍是长期无人值守的候选路线。

## 背景

三轮限次实验（ADR-0004/0005/0006）验证了单机交付链的机械部分，但身份仍是"一次性个人 gh 登录"例外。Mac 上维护 App 私钥、JWT 签发与操作系统级隔离的成本较高；用户在[身份备选比较](../identity-options-and-pat-transition.md)后选择细粒度 PAT 作为当前阶段身份，同时保留 App 与托管 Actions 备选。

## 决定

当前单机阶段的外层编排身份是**仅选择目标仓库、有限期限的细粒度 PAT**：

- 由用户在本地创建并保存为仓库外私有文件（推荐 `~/.config/agent-delivery-loop/github.pat`，父目录 `0700`、文件 `0600`）；程序只读取，校验属主/模式/单链接/非符号链接/位于 Git checkout 与 Worker 状态目录之外，并要求 `github_pat_` 前缀的细粒度格式。
- `deliver`/`resume`/`check-github-auth` 必须显式给出 `--github-pat-file` 与 `--github-login`；不读取日常 `gh` 登录、不接受 classic PAT、失败不回退其他身份。每次 `gh` 调用使用独立临时 `GH_CONFIG_DIR` 与显式 `GH_TOKEN`，不覆盖用户登录；推送与写后核对使用同一身份。
- 检查点只保存身份元数据（kind/repository/expected_login/只读核对结果），不保存令牌或摘要；续接必须匹配同一身份元数据，旧个人 gh 编排记录不能自动迁移。
- Worker 子进程与只读审查子程序不获得 PAT（参数或环境均不传递）；外层授权读取（`authorized_plan` 等）使用 PAT。
- CI 核对使用明确支持细粒度 PAT 的 Actions REST（固定 `ci.yml` 的 `pull_request` 运行、精确 head、固定 attempt 的 jobs），仅申请 `Actions:read`，不依赖 Checks API；空列表、列表不完整或工作流/任务身份不匹配一律失败关闭。
- 合并命令绑定精确 head（`--match-head-commit`）；`Contents:write` 与合并接口权限重叠，不得宣称令牌硬性禁止合并。

## 备选（均保留）

- 日常个人 `gh`：人工工具保留；已结束的限次授权不延长为长期授权。
- 内置 `GITHUB_TOKEN`：保留托管工作流路线。
- App + 托管 Actions：长期无人值守方向，接口与门禁不变、不被挪作 PAT 后门。
- App + Mac 独立发布用户/进程：更强本地边界，暂缓系统级隔离工程。

## 后果

接入与审计更简单、日常配置不被覆盖、发布与回读身份明确。代价：PAT 仍是个人身份，需手动续期/撤销；同 macOS 用户下文件模式与环境清理**不证明**操作系统级隔离；真实写权限、Actions REST 账号兼容性与主分支规则仍须逐项实测（`write_permissions_verified`/`protection_verified`/`delivery_verified` 保持为假，直到被真实操作逐项观察）。本决策不自动授予任何写操作、合并或新一轮实验；那些由独立的范围清单、时间窗与用户授权决定。

## 实施依据

实现与最小人工步骤见 [PAT 接入说明](../pat-setup.md)；备选比较见[身份讨论记录](../identity-options-and-pat-transition.md)；本轮实验记录见 docs/goal-pat-trial.md 与本地脱敏检查点。
