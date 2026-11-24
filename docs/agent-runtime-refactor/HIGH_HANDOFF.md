# High 模式实现交接提示词

将下面代码块内的文字粘贴到一个新的 high 模式会话。该会话应绑定本项目目录。提示词中的路径为当前机器的绝对路径。

```text
请直接实施已经讨论确认的 dsh-cua 重构方案，持续完成可执行的开发阶段、验证和清理。

项目目录是
C:\Users\Admin\Documents\deepseek-harness\default-workspace\dsh-cua

开发分支为 codex/agent-runtime-refactor，方案起点提交为 8bea14227945420771f20b348ce777fad0d9f749。先检查实际分支、工作树和远端更新，保留已有改动。根目录的 AGENTS.md 和 DEVELOPMENT.md 已纳入版本控制，必须保留并遵循。

按顺序读取这些绝对路径文件

1. C:\Users\Admin\Documents\deepseek-harness\default-workspace\dsh-cua\AGENTS.md
2. C:\Users\Admin\Documents\deepseek-harness\default-workspace\dsh-cua\DEVELOPMENT.md
3. C:\Users\Admin\Documents\deepseek-harness\default-workspace\dsh-cua\docs\agent-runtime-refactor\README.md
4. C:\Users\Admin\Documents\deepseek-harness\default-workspace\dsh-cua\docs\agent-runtime-refactor\PROTOCOL.md
5. C:\Users\Admin\Documents\deepseek-harness\default-workspace\dsh-cua\docs\agent-runtime-refactor\PHASES.md
6. C:\Users\Admin\Documents\deepseek-harness\default-workspace\dsh-cua\docs\agent-runtime-refactor\ACCEPTANCE.md

已确认的产品方向如下。

- DSH 是主入口和主规划者，优先适配响应快、便宜且能识图的 deepseekv4.1flash1017 与 hy4-flash。模型使用 DSH 实际配置。
- 目标是精简代码与依赖、消除重叠工具选择、建立统一观察和执行流程，并提升实际操作效率。
- 默认公开六个工具，名称为 doraemon_open、doraemon_observe、doraemon_act、doraemon_wait、doraemon_status、doraemon_stop。
- 统一 target、observation、element ref、frame 和 operation。后端选择、坐标换算、引用校验、批量恢复与取消由 runtime 负责。
- open 返回初始观察，act 返回动作后读回。DOM/UIA 优先提供结构化信息，图片正式进入主模型输入，OCR/YOLO 按需懒加载，默认 caption 请求数为零。
- 浏览器默认插件专用持久化 profile，复用登录状态与站点设置。保留显式连接已有浏览器的能力。首次登录、验证码、登录失效可以需要用户介入，保留既有 profile 与用户数据。
- 用户很少使用内置自主 Agent、独立工作台、IM、WeCom 和 cron，接受将它们迁出默认运行链和默认发布包。迁移需记录旧入口与数据去向。
- 用户沿用本机已有的大权限。实现约束集中于真实定位、取消、结果读回、部分完成和恢复，遵循现有插件框架与安装规则。

以 PHASES.md 顶部的阶段表为唯一进度来源。P0–P6 已通过验收并提交，P7 的旧新双模型正式尝试共 84 次，修复版本为 0.5.4。逐次结果、修复后复验和剩余环境条件见 ACCEPTANCE.md 第 17 节及 p7-evidence。

先读 ACCEPTANCE.md 第 17 节并核对 PHASES.md 的阻塞原因。0.5.4 的 18 次受影响任务复验已结束，真实异 DPI/负原点多屏矩阵需要补齐对应显示器条件。所有已完成阶段以实测记录和提交为依据。真实 DSH 工具 DSL 与图片 renderer 合同已通过实际 SDK 和两条 wb 路由验证，保留 optional peers 的包装方式。

开工前先建立测试基线，不要在全红或无法运行的测试环境上改代码。

- 已有测试环境先运行 pwsh -File scripts/check.ps1 并记录实际基线。
- 新机器按 DEVELOPMENT.md 创建 .venv、安装 pytest，并运行 prepare-test-peers.mjs 和 prepare-test-python.ps1。
- 起点的 schemastery 加载失败已在 P0 修复。0.5.4 常规基线为 Node 94 PASS，Python 314 PASS、1 项显式真实窗口 SKIP、43 subtests PASS。

严格执行 DEVELOPMENT.md 的阶段闸门。每个阶段必须同时满足三条才允许进入下一阶段：该阶段交付项全部存在；ACCEPTANCE.md 对应项全部 PASS 且填写了实测结果（填数值，不是打勾）；该阶段已有一个独立 commit 且工作树干净。禁止跨阶段累积改动，禁止五个阶段做完再一起测。

提交使用 Conventional Commits，格式 type(scope): 说明，正文包含 Phase: Pn。示例 feat(host): add doraemon_observe with unified observation contract。没有跑过测试的提交视为未完成。禁止 wip、update、fix bug 这类无信息量的提交说明。

遇到阶段阻塞时停在该阶段，报告具体阻塞点和已尝试的做法，不要绕过去继续做后面的阶段。

文档给出具体推荐方案和待测初值。遇到 DSH API、现有数据格式或系统能力与方案假设不同，基于源码和实测修正实现细节并记录原因。日常实现选择自行推进，独立可验证部分继续完成。只有真实外部阻塞或产品范围出现实质变化时再集中说明。

保留已有 Windows/DPI/多屏、取消租约、浏览器线程亲和和批量部分结果等可复用资产。将旧入口逐步迁移到单一实现，在阶段验收后清理重复代码和依赖。覆盖普通桌面、DOM、视觉回退、输入、选择、附件、窗口/标签、瞬态提示和长操作查询等必要能力。

行为变化按 AGENTS 版本规则处理，六工具合同以 0.5.0 系列作为建议目标。安装使用插件管理机制，验证必须包含新的 DSH 会话、六工具发现和 doraemon_status 回答。真实双模型效果、图片成本和耗时以测量记录为依据。

请开始实际开发，阶段性汇报已完成内容、验证和下一步。收尾时给出变更范围、测试结果、两模型验收数据以及准确的剩余阻塞。
```
