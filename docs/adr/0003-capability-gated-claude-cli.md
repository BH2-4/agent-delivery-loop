# ADR-0003: 以能力检查管理 Claude Code CLI 执行版本

**日期**：2026-09-28<br>
**状态**：proposed<br>
**决策者**：仓库所有者与 Steward（待确认）

## 背景

v0 的主力 Worker 是 Claude Code，用户希望无人值守的可执行线路保持当前可用。CLI 版本和参数会演进，而一次任务必须使用可复现的执行配置。2026-09-30 在目标主机上检测到 Claude Code 2.1.284，且与 Anthropic 官方当前标记的最新 release v2.1.284 一致；认证状态正常。当前只核对了版本与 CLI 帮助参数，没有运行真实 Work Order。

## 决定

Python `agent-run` 通过官方 Claude Code CLI 的非交互模式启动 Worker。当前实现要求 Claude Code 至少为 2.1.259，以使用 `--restricted` 和 `--permission-prompts none`；每次运行记录实际 CLI 版本、所选模型、路由摘要与新建 Session ID。执行器使用独立 HOME/配置目录、safe/restricted 模式、独立 worktree，并禁用 Session transcript 持久化；只把该次授权的 Delivery Skill 作为显式 system prompt 加入。执行期间不切换 CLI、模型或供应商；检查失败时停止，不降级为绕过权限的模式。正式环境的精确版本提升流程仍需讨论。

## 考虑过的方案

### 每次执行前直接升级到 `latest`

- 优点：最快获得新功能。
- 缺点：新版本变更会直接影响无人值守任务，难以解释同一任务前后行为差异。
- 未选择的原因：执行线路需要先通过兼容性验证，升级应发生在两次任务之间。

### 长期固定一个旧版本

- 优点：短期行为稳定。
- 缺点：错过安全修复和新能力，最终可能与官方服务不兼容。
- 未选择的原因：版本应定期检查并有受控提升路径。

### 第一版直接使用 Claude Agent SDK

- 优点：适合复杂事件流和深度程序化控制。
- 缺点：第一版需额外维护 SDK 适配、依赖和与用户现有 CLI 工作环境的对应关系。
- 未选择的原因：当前目标是一个固定 Claude Code Worker，CLI 已提供非交互运行和结构化输出；需要更复杂编排时再评估 SDK。

## 后果

### 正面

- 每次交付可追溯到确切的 CLI 版本、所选模型标识、Delivery Skill 摘要和授权提交。
- 新版可先通过只读或低风险任务验证，再进入生产线路。

### 代价

- 需要维护启动前诊断，以及稳定版和候选版的提升流程。
- 首版落地前必须在目标主机安装 Claude Code，并验证它与认证方式、Delivery Skill 和所需权限配置的实际组合。

### 风险与应对

- 官方 CLI 参数或行为变化：以 `claude --version`、`claude --help` 和 `claude doctor` 做预检；只在验证通过后运行任务。
- `stable` 通道本身仍会更新：生产安装关闭后台自动更新，版本提升只在无任务运行时进行；不能用 `minimumVersion` 代替精确版本检查。
- 本机 CLI 帮助确认 `--restricted`、`--strict-mcp-config`、`--permission-prompts none`、`--no-session-persistence` 和 `--append-system-prompt-file` 参数存在。当前实现还未真实启动会话；应在首次试运行中确认 Skill 注入与文件编辑权限。
- 本机 CLI 帮助说明 `--bare` 的 Anthropic 认证路径只读取 `ANTHROPIC_API_KEY` 或 `apiKeyHelper`，可能与当前使用的 `ANTHROPIC_AUTH_TOKEN` 固定路由不兼容，因此实现不使用 `--bare`；改用独立 HOME/`CLAUDE_CONFIG_DIR`、restricted 模式和 `--strict-mcp-config`。
- 用户/项目自定义配置被 safe/restricted 模式屏蔽，但管理员托管策略仍可能适用；实际执行主机的托管策略尚未审计。
- 服务端可能在模型标识不变时更新模型：运行记录只能证明当时选用了哪个标识，不能单靠 CLI 记录证明远端权重完全不变。

## 依据

- [Claude Code v2.1.284 官方 release](https://github.com/anthropics/claude-code/releases/tag/v2.1.284)
- [Claude Code：安装、更新通道和版本管理](https://code.claude.com/docs/en/setup)
- [Claude Code：CLI 参数](https://code.claude.com/docs/en/cli-reference)
- [Claude Code：程序化调用与结构化输出](https://code.claude.com/docs/en/headless)
