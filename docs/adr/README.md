# 架构决策记录

ADR 记录一项设计决定的背景、取舍和后果。`accepted` 是已确认的方向；`proposed` 是供讨论的草案。记录决定不等于相关功能已完整实现或通过真实试运行。

| ADR | 标题 | 状态 | 日期 |
| --- | --- | --- | --- |
| [0001](0001-python-local-orchestrator.md) | 使用 Python 编写本机执行器 | accepted | 2026-09-28 |
| [0002](0002-github-app-unattended-identity.md) | 使用 GitHub App 作为无人值守身份 | accepted（阶段身份选择被 0007 替代） | 2026-09-28 |
| [0003](0003-capability-gated-claude-cli.md) | 以能力检查管理 Claude Code CLI 执行版本 | proposed | 2026-09-28 |
| [0004](0004-one-shot-personal-gh-bootstrap.md) | 一次性个人 gh 身份引导实验（限期） | accepted（限次例外，已结束） | 2026-10-04 |
| [0005](0005-evidence-contract-and-read-reliability.md) | 审查证据契约与只读查询可靠性（第二轮限次实验） | accepted（限次例外） | 2026-10-05 |
| [0006](0006-write-verification-rework-resume.md) | 写结果核对、有界返修与安全续接（第三轮限次实验） | accepted（限次例外） | 2026-10-05 |
| [0007](0007-fine-grained-pat-stage.md) | 当前单机阶段采用细粒度 PAT | accepted | 2026-10-05 |

新增决定可从 [模板](template.md) 开始。状态变化时同时更新文件和本索引。
