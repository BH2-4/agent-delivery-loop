# 当前单机阶段：细粒度 PAT 接入

日期：2026-10-05。状态：**本地源码已修改；真实 PAT 接入、安装与交付尚未验证。**

方案比较保存在[身份备选](identity-options-and-pat-transition.md)，正式决策草案见 [ADR-0007 草案](adr-0007-pat-draft.md)。用户已选择 PAT，但选择路线不等于提供令牌或授权新一轮 GitHub 写操作。本文不启动实验，不延长 ADR-0004/0005/0006 的期限。

## 1. 本次实际修改

- `deliver/resume` 必须显式指定 `--github-pat-file` 和 `--github-login`，不再读取 `gh auth token` 或推断日常登录身份。
- 文件必须在 Git checkout 与 Worker 状态目录之外；父目录属于当前用户且为 `0700`，文件为当前用户所有的单链接普通文件，模式为 `0400` 或 `0600`。拒绝符号链接、classic Token、空值与不明确格式。前缀检查不是 GitHub 端权限证明。
- 本次运行在内存中固定 PAT 快照。每次调用 `gh` 使用独立临时配置、固定 `github.com` 与显式 `GH_TOKEN`；清除旧 Token 和 API 调试变量，不覆盖用户登录。推送与写后查询使用同一身份。
- `check-github-auth` 只读核对实际账号、目标仓库、指定 PR 和 Actions REST 的 run／jobs 接口。`deliver/resume` 在 Worker 开工前也做此预检。
- 编排记录只保存身份类型、目标仓库、预期账号和核对结果，不保存 PAT、Token 摘要或原始认证输出。续接必须匹配同一身份元数据；旧个人 `gh` 编排记录不能自动迁移，令牌续期仍需用户在本地处理。
- 外层授权读取可以使用 PAT；**可信 Python 程序**（编排器与它启动的 agent-run 运行器）在 PAT 模式下使用同一内存快照：编排器把快照值经一次性进程环境通道（`AGENT_DELIVERY_PAT`）传给 agent-run，子进程读取后立即关闭通道并按需构建认证读取客户端，父子进程不可能各自读到不同的令牌。该变量不进入命令行、临时凭据文件或编排器自身环境，且列于全部凭据擦除名单；Claude 模型子进程使用独立 allowlist 环境并使用隔离 HOME，审查子进程使用凭据擦除环境，二者都拿不到 PAT、文件路径或该通道。
- 直接以旧方式调用 agent-run（无该环境通道）时保持原有匿名公开读取行为；此时仍可能遇到匿名限流。**Claude 模型 Worker 与只读审查子进程永远不获得 PAT**——"agent-run" 是 Python 控制程序，不是模型 Worker，不要混淆两者。
- 匿名额度探针不再是 PAT 模式的启动门槛：**编排链路上**的授权读取——编排器自身、被通道启动的 agent-run、以及审查资料包准备与回执核验（`prepare_review`/`check_review`，含返修后审查与合法续接的回执复验）——都使用同一认证身份。旧式无通道直接调用（人工 agent-run、verify-review、agent-watch）仍走匿名公开读取并可能遇匿名限流。共享代理出口的匿名额度被消耗曾是中断原因之一（来源未确认，只是合理解释），与 PAT 编排模式无关。认证读取自身也可能限流，仍按有界策略失败关闭。
- 已明确识别的只读 GitHub 请求（`gh api` 无字段/无方法覆盖的 GET、`gh pr view`）遇到瞬时传输故障（如 TLS 握手超时、连接重置、暂时性服务端错误）时最多重试两次，共三次尝试，共享一个单调时钟总预算；预算已耗尽或不足一秒时不再发起请求，单次超时不超过剩余时间也不向上取整；预算不延长任何外层 CI 期限。限流、认证/权限拒绝与无法识别的错误立即停止；推送、建 PR、合并等写操作永不自动重试。这是读取容错，不是任务重试，也不保证网络永远可用。
- 控制程序安全：编排器在启动 agent-run 前先写入可回读的持久 spawn 门禁登记（Popen 成功即翻转为阻断态，并被后续一切启动检查识别）；外层 agent-run 与内层 Claude 属独立进程组，停止外层（先 SIGINT 交由内层运行器完成自身 Worker 清理、有界宽限后才升级强停）不构成内层已停止的证明——门禁只在该 spawn 窗口内产生 `worker_status=stopped` 的可信运行记录时才解除，否则保持阻断直至人工安全审查。
- 旧 `agent-run/agent-watch --publish` 仍是 App 专用入口，门禁不变。PAT 路线不创建 App，不伪造隔离或保护验证标记。

## 2. 推荐初始权限

在 GitHub 的个人 Settings → Developer settings → Personal access tokens → **Fine-grained tokens** 中，由用户创建：资源所有者 `BH2-4`，Only select repositories → `agent-delivery-loop`；建议首次 30 天过期，不选永久或 classic。令牌仍代表用户本人，并含公开仓库只读访问。[GitHub：管理 PAT](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens)

| 仓库权限 | 建议 | 用途 |
| --- | --- | --- |
| Metadata | Read | 基本信息 |
| Contents | Read and write | 授权文件、交付分支推送；该权限也可能允许合并 |
| Pull requests | Read and write | PR 查询、创建 |
| Actions | Read | 列出固定工作流的运行及指定 attempt 的任务；不重跑 workflow |
| Checks、Commit statuses | 不申请 | PAT 入口不依赖 Checks API 或 `gh pr checks` |
| Issues | 默认不申请 Write | 本阶段不新增进度 Issue 发布器 |
| Administration、Workflows、检查写权限 | 不申请 | 不改规则、工作流或检查结论 |

官方 PAT 总览仍列出 Checks API 限制，而具体 check-runs 读取端点列出细粒度 PAT 支持；因此本阶段不依赖 `gh pr checks`。选择官方明确支持细粒度 PAT 与 `Actions:read` 的工作流运行／指定 attempt 的 jobs REST 接口；真实账号兼容性仍需预检，不自动换 classic、个人登录或追加权限。[Workflow runs API](https://docs.github.com/en/rest/actions/workflow-runs#list-workflow-runs-for-a-workflow)、[Jobs API](https://docs.github.com/en/rest/actions/workflow-jobs#list-jobs-for-a-workflow-run-attempt)

当前只验证 `.github/workflows/ci.yml` 的 `pull_request` 运行：必须匹配精确 head SHA 并与目标 PR 不冲突（已合并 PR 的运行其关联列表可能为空，空关联按 head 绑定接受；显式关联其他 PR 的运行被排除），选最新匹配 run，固定 run attempt、核对任务及回读 run；工作流与全部任务成功且唯一 `validate` 成功才通过。列表超过 100 条或不完整即停止，不漏扫后宣称通过。不读取外部 App checks／commit statuses，也不声称已核验所有仓库保护要求；若新增必需检查或改变工作流，必须先更新明确检查契约，不能默默忽略。

`Contents:write` 与 PR 合并接口权限重叠，不能称为“令牌硬性禁止合并”。`--auto-merge` 默认关闭；开启它还需要具体实验的授权和全部质量门禁，不得使用管理员绕过。[GitHub：合并 PR](https://docs.github.com/en/rest/pulls/pulls#merge-a-pull-request)

## 3. 本地保存与首次预检

先经独立审查、提交、合并并按准确源码 SHA 重建普通 wheel。**当前已安装的 wheel 不会随着源码改动自动更新**；不要用 `PYTHONPATH` 冒充安装验证或重新改 editable `.pth`。

由用户把令牌保存为仓库外私有文件，推荐 `~/.config/agent-delivery-loop/github.pat`，只存一行 Token。父目录 `0700`，文件 `0600`；使用本地可信编辑／密码管理方式，不在命令历史、聊天或剪贴板日志中保留密钥。程序只读取，不代建密钥或修改权限。不要将它放入项目 `.env`、全局 shell 配置或 Worker 状态目录。

安装获准版本后，从目标仓库运行：

```sh
env -u PYTHONPATH .venv/bin/agent-delivery check-github-auth \
  --github-pat-file "$HOME/.config/agent-delivery-loop/github.pat" \
  --github-login BH2-4 --pr 24 \
  --proxy http://127.0.0.1:12451
```

`--pr 24` 只是查询一张已有 PR，不重跑其任务。预期退出码 `0`、状态 `read_access_verified`，账号／仓库／PR／CI 读取字段为真；`write_permissions_verified`、`protection_verified`、`delivery_verified` 仍为假。空检查列表不能证明 CI 通过；预检成功也不能证明推送、建 PR、合并、Token 实际权限边界或到期日已验证。

`GH_TOKEN` 优先于已存登录，本实现仅在必要子进程中设置，并使用临时 `GH_CONFIG_DIR`；不会要求 `gh auth login/logout`。[gh 环境变量](https://cli.github.com/manual/gh_help_environment)

## 4. 真正的试运行与失败停止

下一轮必须有新的范围清单、时间窗和已合并 Plan PR；不要复用旧工单。使用安装回执、完整源码 SHA、wheel SHA-256，固定 `glm-5.3`、`https://open.bigmodel.cn/api/anthropic`、`max` 与用户确认的认证来源。CC Switch 不切换，认证端点不匹配即停止。

`deliver/resume` 除原有参数外需添加：

```text
--github-pat-file <outside-repository-private-file> --github-login BH2-4
```

路径不是令牌本身，但也不应写进公开简报。真实写操作成功后才能逐项记录“推送／创建 PR／合并已验证”，不能用只读结果或替身回归代替。网络失败、认证失败、格式异常、身份错配或状态不明时停止，保留检查点，不打印原始 `gh` 输出、不换身份、不盲目重放。

同一 macOS 用户仍可能读取文件、钥匙串或进程信息。文件模式、临时 HOME 和环境清理**不等于 OS 级隔离**；本阶段没有验证 ACL、后台共享程序或抗恶意 Worker 的完整边界。若所需安全目标超出受控实验，应重新评估保留的 App／托管发布方案。
