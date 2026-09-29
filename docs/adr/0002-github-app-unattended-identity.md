# ADR-0002: 使用 GitHub App 作为无人值守身份

**日期**：2026-09-28<br>
**状态**：accepted<br>
**决策者**：仓库所有者与 Steward

## 背景

合并 Plan PR 后，本机执行器需要在用户不在场时读取授权状态、推送交付分支并创建 Delivery PR。长期使用用户个人凭据会使执行身份和权限边界不清楚。仓库所有者已接受 GitHub App 作为第一版无人值守线路。

## 决定

无人值守的 GitHub 操作由仅安装到目标仓库的 GitHub App 执行。Python 执行器按需使用 App 私钥生成 JWT，再换取短期 Installation Token；它负责推送交付分支和创建 Delivery PR，不把令牌作为 Claude Code 的输入。App 只申请完成这些操作所需的仓库权限；精确权限清单、私钥隔离方式和分支规则在实施前单独核定。

## 考虑过的方案

### 用户个人访问令牌（PAT）

- 优点：初次设置简单，现有 `gh` 命令容易使用。
- 缺点：自动化行为与用户身份混在一起，长期凭据的管理更难。
- 未选择的原因：第一版就要有独立、可撤销的无人值守身份。

### 依赖用户现有的 `gh auth` 登录

- 优点：适合人工监督下的引导和调试。
- 缺点：不同主机上的登录状态不一致，也无法清楚限制 Worker 与执行器的权限。
- 未选择的原因：它不能作为正式无人值守身份协议。

## 后果

### 正面

- GitHub 上的自动化操作可归属到独立 App 身份。
- Installation Token 可在签发时缩小到指定仓库和权限，且约一小时过期。

### 代价

- 需要创建并安装 App，安全保存私钥，并处理短期令牌过期与重新签发。
- 需要在实施前验证最小权限是否覆盖实际使用的 API 和 Git 推送。

### 风险与应对

- 本次只读检查发现目标仓库 `main` 当前没有 branch protection，且没有配置 GitHub App。因而 App 发布接口仍不可用；不得设置启用标记或使用个人 `gh` Token 代替。启用前须由仓库所有者配置并实际验证保护规则与 App 的绕过权限。
- 私钥泄露会让攻击者签发新令牌：私钥不写入仓库、日志或 Claude 的环境；具体存储位置与访问控制在实施阶段确定。若 Claude 和执行器以同一系统用户运行，仅清理环境变量不能证明 Claude 无法读取私钥；正式无人值守启动前须验证隔离边界。
- `Contents:write` 也符合 GitHub 合并 PR 接口的权限要求。执行用 App 不得合并 Plan PR 或直接更新受保护分支，不能只靠“代码没有调用 merge”来保证；须验证 `main` 的仓库规则及 App 的绕过资格。Delivery PR 是否允许在 Codex 审查后自动合并，是独立且尚未接受的新决定，不由本 ADR 授权。
- 短期令牌可能从命令行、Git 凭据或日志泄露：执行器应以受控方式传给需要的 GitHub 操作，随后清理临时状态，并检查日志脱敏。

## 依据

- [GitHub：生成 Installation Token](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app)
- [GitHub：以 App Installation 身份鉴权](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app-installation)
- [GitHub：合并 PR 的权限要求](https://docs.github.com/en/rest/pulls/pulls#merge-a-pull-request)
- [GitHub：Ruleset 可用规则](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets)
