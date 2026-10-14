# 一次配置，按任务启动

`agent-delivery launch` 读取仓库外的一份本机配置，再调用既有 `deliver`。
每次仍明确指定已授权的 Plan PR 和 Work Order；配置不保存任务授权、凭据正文
或自动合并选项。安装版本核验、共享启动去重、审查、CI 和失败停止继续由
`deliver` 执行。每次调用生成新的审查目录，旧执行记录不会被清除。

在仓库外创建 `launch.json`，填入当前安装和用户已选定的配置；POSIX 环境
使用 `chmod 600 launch.json`。路径使用绝对路径，凭据字段只填写私有文件路径。

```json
{
  "repo_path": "/absolute/path/to/repository",
  "model": "your-worker-model",
  "base_url": "https://your-provider.example/anthropic",
  "effort": "high",
  "auth_config": "/private/claude-settings.json",
  "github_pat_file": "/private/github-pat.txt",
  "github_login": "your-github-login",
  "install_receipt": "/private/install-receipt.json",
  "expected_source_sha": "FULL_APPROVED_SOURCE_COMMIT",
  "expected_wheel_sha256": "APPROVED_WHEEL_SHA256",
  "review_model": "your-review-model",
  "review_effort": "high",
  "review_bundle_root": "/private/reviews"
}
```

配置可另设 `review_timeout`、`ci_timeout`（整数秒）和 `proxy`，对应既有
`deliver` 参数；未设置时使用其默认值。升级安装后同步更新获准源码 SHA、
wheel 摘要和安装回执，不从浮动主线推断版本。

确认任务后只需运行：

```text
agent-delivery launch --config /private/launch.json \
  --plan-pr https://github.com/owner/repository/pull/123 \
  --work-order-path .agents/work-orders/WO-EXAMPLE-r1.json
```

该入口按一次显式命令启动，默认不自动合并，也不安装定时器。配置文件只能
装配参数，不能替代 Plan PR 授权；本功能未改变现有 Work Order v1 执行要求。
