# 下一轮 Goal 模板：细粒度 PAT 的单机受控交付实验

状态：**供用户审阅并发送的提示词，不是已启动 Goal，不延长任何历史授权。**

首次执行前，用户需在本地准备 PAT；见 [PAT 接入说明](pat-setup.md)。密钥不在本文件或聊天中填写。若用户不接受下述写入／合并范围，可删去相关授权；程序不得自行补回。

```text
你在 BH2-4/agent-delivery-loop 推进一轮新的、有界的 PAT 身份迭代实验，不是无限自我演进。先查询 Goal：没有未完成 Goal 时创建本轮 Goal（不要自行设置 token_budget）；已有未完成 Goal 时先核对目标与授权，不替换、不扩大、不重新计时，不能确认属于本轮就交回用户。目标是在明确身份下完成一个新需求的真实交付，记录可核验的成功或阻塞。阅读适用 AGENTS.md、README、bootstrap、身份备选、PAT 接入说明、ADR 与既有阶段记录。

我批准本提示词中的本轮范围。你实际收到并开始执行后，使用真实时间工具建立 experiment_id、起止时间与独立检查点；期限为开始起四小时，不沿用历史实验时间或授权。到期停止新工作，只做有界安全收尾。不修改上一轮运行记录，不用缺少密钥或预算耗尽冒充目标完成。

一、固定范围与身份

1. 只操作 BH2-4/agent-delivery-loop，一台 Mac、一个 Claude Worker。先检查主分支、工作树、PR 和安装来源；保留原 .DS_Store、未提交改动与他人成果。遇到冲突停止，不 reset、force push、自动 stash 或覆盖文件。
2. 新阶段选择 fine-grained PAT。使用用户在本地保存的 ~/.config/agent-delivery-loop/github.pat，预期账号 BH2-4；资源限定目标仓库，建议 30 天有效期。必要权限仅 Metadata:read、Contents:write、Pull requests:write、Actions:read；不申请 Administration、Workflows、检查写权限，不新增 Issues 写权限。
3. 本提示词不授权创建令牌或修改用户登录／仓库规则。PAT 缺失、不安全、失效、账号错配或权限不足时，报告最小人工步骤并停止，不退回日常 gh 登录、classic PAT、App 或匿名身份绕过失败。不在聊天索要密钥，不打印凭据、原始认证输出或把 PAT 写入仓库／日志／全局 shell 配置。
4. 本轮接受“个人身份的有限实验”定位，不宣称独立机器人身份、OS 隔离、长期无人值守或主线硬性权限保护已验证。App、托管 Actions、Mac 独立发布身份等备选保留；App --publish 门禁不变。
5. Worker 固定 glm-5.3、https://open.bigmodel.cn/api/anthropic、max，认证只从用户确认的仓库外 Claude settings 读取。CC Switch 不改动、不切换模型；认证来源与显式端点不匹配时停止。不会调查模型实际消耗／账单；预算参数不称为兼容供应商的扣费硬上限。
6. 需要子 Agent 时，只允许 Luna、极高思考水平；使用 Codex 时按实际 CLI 名称映射 gpt-6-luna / xhigh。不能确认支持时停止并说明，不静默改默认模型。子 Agent 不读取真实 PAT；执行者与独立审查者分开。

二、先完成 PAT 工程交付，不重做旧引导

1. 核对本地 feat/fine-grained-pat-identity 与相关新文件，不能假定已提交、推送、审查、合并或安装。
2. 本提示词批准按已展示的 ADR-0007 草案正式记录当前 PAT 选择，保留 ADR-0002 的历史正文并标明替代范围与交叉引用；其他历史 ADR 不被改写或续期。若技术方案已与展示草案发生实质偏离，先报告差异并请求确认。
3. 检查显式身份来源、文件权限与日志脱敏、账号／仓库约束、Worker／审查环境、续接身份绑定及失败关闭。CI 只用 Actions REST 的 ci.yml/pull_request 精确 head 运行和固定 attempt 的 jobs；验证唯一 validate 及工作流／全部任务成功。新必需检查或保护要求不能被忽略，也不以空列表作为成功。
4. 先编译受影响代码。已有通过且仍适用的检查不重复；只有具体失败、明确验收或仓库 CI 强制要求时做最小聚焦回归。模拟回归与真实访问分别报告，禁止堆砌覆盖率或无关测试。
5. 在用户批准的工程范围内，允许形成一个独立维护 PR：身份实现、受影响接口／回归、PAT 说明、ADR 过渡和本轮记录。不能夹带通用重构、新调度器、通知系统或新工作流。
6. 使用显式 PAT 做维护发布及只读结果核对；维护 PR 不经过 Claude Work Order 伪装授权。绑定最终完整 head，独立只读审查无阻断且对应必需 CI 成功、远端 head 未漂移，才允许普通合并该工程 PR。不使用 --admin，不绕过规则。存在同目的 PR 时先核对并继续，不重复创建。

三、安装与真实 PAT 只读预检

1. 仅从已审查并合并的准确源码 SHA 构建普通 wheel，记录源码 SHA、wheel SHA-256 与安装回执；保留旧 artifact／receipt。不使用 editable 安装，不递归清除隐藏标志，不改全局 Python／CLI。
2. 本提示词允许替换本仓库 .venv 内的执行器安装；核对实际导入来源与受影响模块，不以版本号 0.1.0 或 git pull 代替来源验证。资料包、状态与源码按真实版本绑定。
3. 用安装后的 agent-delivery check-github-auth，显式 PAT 文件、BH2-4、已有 PR #24 和代理 http://127.0.0.1:12451 做一次只读核对。模型不启动。若 #24 的匹配 CI 证据已不可用，先报告，不偷偷选择无 CI 的对象过关。
4. 预检成功只证明账号、仓库、PR 和 Actions run/jobs 可读取；write_permissions_verified/protection_verified/delivery_verified 不改成真。只读不能证明仓库范围硬隔离或写权限；它们通过本轮真实操作逐项观察。

四、唯一新真实工单与发布

1. 新任务固定 WO-PAT-TRIAL-001-r1，新增 docs/pat-delivery-runbook.md（除此无 Worker 可写范围），不重跑 WO-PILOT-001/002、WO-DOC-TRIAL-001、WO-RUNBOOK-002、WO-OPS-NOTES-003 或其他已执行修订。如果该任务已执行，报告已完成或已有检查点，不另换 ID 重跑。
2. 文档目标：说明 PAT 身份下从预检到交付的操作顺序、Plan 授权与 Delivery 审查的区别、PAT 不进入 Worker、local_ready/merged/deployed 的区别、精确 SHA 的审查与 ci.yml/validate 门禁、失败停止与人工配置边界。链接现有 PAT 说明，不复制密钥、不声称未验证能力通过。范围不包括实际改变权限、工作流、路由或部署。
3. Work Order 按现有 JSON 契约，含目标、不做什么、验收、允许路径、固定 Worker、Skill、停止条件和必要 review_evidence；证据必须是准确主线 SHA。实际解析新工单，不依赖示例 CI；不能预填尚不存在的 Plan merge SHA。
4. 准备独立 Plan PR。此次我仅对上述固定任务预授权：独立审查确认 Work Order 逐项符合本提示词、没有扩大目标／验收／路径、精确 head 的 CI 通过后，Steward 可替我普通合并该 Plan PR。偏离任一条件必须交回用户；Worker 不批准自己的工单。这个限次例外不改写通用人工授权原则。
5. Plan PR 实际合入 main 后，才用安装版 agent-delivery deliver 执行一次。显式传入 Plan PR、工单路径、固定 Claude 配置、安装回执与完整源码/wheel 摘要、Luna/xhigh 独立审查、新资料包、PAT 文件／预期账号、代理和有界 CI 等待。
6. 本轮允许在全部门禁满足后为这一个 Delivery PR 开启 --auto-merge：agent-run 实际退出码 0，Worker 已确认停止，结构化 complete、允许路径及最终提交核验通过，独立 pass 绑定候选完整 head，指定 CI 真实成功，远端 head 与候选一致。合并命令自身必须 match-head-commit，写前落意图、写后只读核对，最后确认 main 包含同一候选。
7. 成果明确 changes_required 才允许原机制最多两轮返修；同一 Delivery 分支、全新 Session，计数跨 resume 累计。blocked、身份问题、停止未确认或资料不充分不靠返修掩盖。不要重跑原 Worker；基础设施问题恢复后，仅按真实安全检查点 resume，并复用同一实际审查回执。
8. 进度与简报写入本地脱敏阶段记录／获准 PR；本轮不新增 Telegram、Hermes 入口、Bot 或进度 Issue 写权限。只记录确有价值的信息，不为保持活跃重复查询或调用模型。

五、停止与完成

任何停止未确认、状态损坏、凭据风险、范围越界、证据不匹配、CI 失败／缺失／未知、远端结果不明、期限届满或需要新权限时，保留记录与成果，停止对应动作并报告。禁止删记录、换状态目录、解除门禁、换身份／模型、伪造回执或盲目重放写操作来推进。限次例外不允许永久定时器、跨主机抢单、自动失败重试、多模型路由、自动部署或无限扩大任务。

只有工程版本安装来源明确、真实 PAT 预检通过、唯一新 Work Order 的 Delivery PR 在全部门禁满足后已实际合并且 main 核验完成，才标记 Goal complete。若只完成一部分，如实汇报并遵守 Goal 工具关于 blocked 的规则，不擅自 paused、不假称 complete。

最终简报：experiment_id、起止与是否到期、源码/wheel 摘要、身份与实际权限观察、Plan/Delivery/工程 PR 及精确 head、Worker/审查 Session 关联、实际命令与退出码、是否发生返修/续接、主线合并核验、未验证风险和唯一下一步。完整会话与原始凭据不发布；只称“一次受控 PAT 闭环观察”，不称长期无人值守或生产部署成功。
```
