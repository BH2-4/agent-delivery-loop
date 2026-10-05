# 当前单机阶段：细粒度 PAT 接入

日期：2026-10-05。状态：**本地源码已修改；真实 PAT 接入、安装与交付尚未验证。**

方案比较保存在[身份备选](identity-options-and-pat-transition.md)，正式决策草案见 [ADR-0007 草案](adr-0007-pat-draft.md)。用户已选择 PAT，但选择路线不等于提供令牌或授权新一轮 GitHub 写操作。本文不启动实验，不延长 ADR-0004/0005/0006 的期限。

## 1. 本次实际修改

- `deliver/resume` 必须显式指定 `--github-pat-file` 和 `--github-login`，不再读取 `gh auth token` 或推断日常登录身份。
- 文件必须在 Git checkout 与 Worker 状态目录之外；父目录属于当前用户且为 `0700`，文件为当前用户所有的单链接普通文件，模式为 `0400` 或 `0600`。拒绝符号链接、classic Token、空值与不明确格式。前缀检查不是 GitHub 端权限证明。
- 本次运行在内存中固定 PAT 快照。每次调用 `gh` 使用独立临时配置、固定 `github.com` 与显式 `GH_TOKEN`；清除旧 Token 和 API 调试变量，不覆盖用户登录。推送与写后查询使用同一身份。
- `check-github-auth` 只读核对实际账号、目标仓库、指定 PR 和 Actions REST 的 run／jobs 接口。`deliver/resume` 在 Worker 开工前也做此预检。
- 编排记录只保存身份类型、目标仓库、预期账号和核对结果，不保存 PAT、Token 摘要或原始认证输出。续接必须匹配同一身份元数据；旧个人 `gh` 编排记录不能自动迁移，令牌续期仍需用户在本地处理。
- 外层授权读取可以使用 PAT；Worker 子程序与只读审查子程序不获得 PAT 文件参数或 Token。它们保留原有公开读取行为，仍可能遇到匿名限流。
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

当前只验证 `.github/workflows/ci.yml` 的 `pull_request` 运行：必须关联目标 PR 和精确 head SHA，选最新匹配 run，固定 run attempt、核对任务及回读 run；工作流与全部任务成功且唯一 `validate` 成功才通过。列表超过 100 条或不完整即停止，不漏扫后宣称通过。不读取外部 App checks／commit statuses，也不声称已核验所有仓库保护要求；若新增必需检查或改变工作流，必须先更新明确检查契约，不能默默忽略。

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
