# Codex 续接提示词（本机 worktree 版）

把下面代码块内的文字粘贴到新的 high 模式会话。项目目录用这台机器的实际路径。

```text
请接手 dsh-cua 的 agent-runtime-refactor 重构，从 P5 继续实施，直到阶段验收闸门、P6 清理和 P7 发行验收完成。

项目目录（本机 worktree，请用这个绝对路径，不要用交接文档里的 C:\Users\Admin\... 旧路径）：
C:\Users\kennyyzhang\Documents\Codex\dsh-doraemon-refactor

这是一个 git worktree，分支为 codex/agent-runtime-refactor，跟踪 origin/codex/agent-runtime-refactor。
当前 HEAD 为 bdae18d，工作树干净。注意：这台机器上还有一个 DSH 活跃插件目录
C:\Users\kennyyzhang\Documents\deepseek-harness\default-workspace\plugins\dsh-doraemon（main 分支，0.4.14），
它是 DSH profile 的 junction 目标且内核正在运行 —— 不要在那里开发，不要把两者混用。

按顺序读取这些文件（都在项目目录内）：
1. AGENTS.md
2. DEVELOPMENT.md
3. docs/agent-runtime-refactor/README.md
4. docs/agent-runtime-refactor/PROTOCOL.md
5. docs/agent-runtime-refactor/PHASES.md
6. docs/agent-runtime-refactor/ACCEPTANCE.md
7. docs/agent-runtime-refactor/HIGH_HANDOFF.md（里面有更早的交接提示词，路径已过时，仅作背景）

进度事实（以本段为准，PHASES.md 顶部阶段表是唯一进度来源）：
- P0–P4 已分别提交并通过验收。提交序列：2d03f81(P0) → cc31ffd(P1) → c5ed0ec(P2) → 24ba0ca(P3) → b2c3da9(P4)。
- P5 的部分基础实现**已经提交**，不要当成"工作树里未提交"再做一遍：
  e3ea89f feat(host): add condition waits and bounded image reads（独立条件等待、会话隔离的 JPEG 资源端点、Host 有界图片读取、部分操作桥接入口）；
  bdae18d docs: make PHASES the single source of phase progress。
  相关文件：kernel/dsh_desktop/conditions.py、browser.py、operations.py、observations.py 均已存在。
- 工作树当前干净。如果你看到任何未提交改动，先判断是不是上一次中断留下的，不要在未知状态下叠加提交。
- PHASES.md 顶部表格 P5 仍标"待实施"，是因为 P5 的六项验收还没完成，不是因为代码不存在。
- 起点基线（上次实测）：Node 84 项通过；Python 302 项通过、1 项预期跳过、32 个子测试通过。
- 双模型目标：wb/cn:deepseek-v4.1-flash 和 wb/cn:hy4std。P0 中两条路由各有 1/1 随机图片字符读取成功。
- 尚未测量：P5 的截图闭环（图片确实进入实际模型请求）、P7 的任务对照/成本/耗时数据。

P5 仍需完成的六件事：
1. 六工具（doraemon_open / observe / act / wait / status / stop）完整注册，默认注册表只保留这六个。
2. 动作前瞬态提示监听：在 act 的指定步骤派发前 arm 采样，操作结束或超时释放，act 与 status 返回该 operation 已捕获的证据。
3. Host 图片附件接入：把 P0 核验过的原生图片输出和 e3ea89f 的资源端点接到实际两个模型请求，形成已验证的图文闭环。
4. 新 DSH 会话安装验证：用插件安装机制安装当前分支，在新会话确认只发现六工具且 doraemon_status 独立可答。
5. 补齐 PHASES.md P5 完成条件五条，并在 ACCEPTANCE.md 追加 P5 执行记录（写实测数值，不是打勾）。
6. 独立提交并让工作树干净，才允许进入 P6。

本机独立验证环境的状态（已经实测，不要重复搭建，也不要误用）：
- 已创建 profile `cua-refactor`，已把本 worktree 作为 dsh-doraemon bundle 装进去，
  profile patch 已配 repoRoot 指向本 worktree、port 8767、独立 stateDir。
  启动方式：`dsh --profile cua-refactor --no-open --port 0`。
- 实测阻塞：新会话里内核（8767）没有起来（端口拒绝连接、无新 python 进程）。
  内核模块本身可 import，怀疑是全局共享的 elevated task / kernel-boot.json 与旧会话冲突。
  当前 worktree 注册的是 31 个旧工具，不是六工具 —— 与"P5 六工具注册未完成"一致。
- 因此：不要在 P5 代码完成前花时间修这个环境；也不要用旧 desktop profile 的 doraemon_*
  工具（那连的是 0.4.14 内核）假装验过新代码。内核级验证可用项目内 P1/P4 探针脚本
  （test/runtime_probe.py、test/actions_probe.py），这些不需要独立 profile。
- Host 六工具发现、图片 renderer 进模型请求这类必须在独立 profile 新会话里验的项目，
  作为明确的验收前置条件向用户提出，让用户配合重启 DSH / 处理全局计划任务；
  不要自己改全局计划任务或 kernel-boot.json。

产品方向（已确认，不要重新讨论）：
- DSH 是主入口和主规划者；两个 Flash 模型优先，使用 DSH 实际配置。
- 精简代码与依赖、消除重叠工具、统一观察和执行流程。
- 统一 target / observation / element ref / frame / operation；后端选择、坐标换算、引用校验、批量恢复与取消由 runtime 负责。
- open 返回初始观察，act 返回读回；DOM/UIA 优先结构化信息，OCR/YOLO 按需懒加载，默认 caption 请求数为 0。
- 浏览器默认插件专用持久化 profile，保留显式连接已有浏览器的路径。
- 内置 Agent、独立工作台、IM、WeCom、cron 迁出默认链（P6 执行，记录旧入口与数据去向）。

硬性流程约束（DEVELOPMENT.md，必须遵守）：
- 阶段闸门：交付项齐、ACCEPTANCE 有实测值且全 PASS、独立提交且工作树干净，三条同时满足才进下一阶段。不许跳阶段、不许跨阶段累积改动。
- 开工前先跑全绿基线：pwsh -File scripts/check.ps1（含 node --check、npm test、pytest）。基于真实分支和干净工作树记录起点测试数。
- 提交用 Conventional Commits：type(scope): 说明，正文含 Phase: Pn。禁止 wip/update/fix bug；没跑过测试的提交视为未完成。
- 不改 ~/.dsh/profiles 下任何文件；profile 归 plugin_manager install_bundle 管理。
- 六工具公开合同以 0.5.0 系列为目标版本；注意当前 package.json 是 0.4.18、kernel/dsh_desktop/version.py 还是 0.4.14（分支遗留的不一致），发版前必须对齐并写 CHANGELOG。
- optional peers（cordis / dsh-tools / schemastery）保持 peerDependencies + optional，不写进 dependencies。
- 遇到 DSH API、数据格式或系统能力与方案假设不符，以源码和实测为准并记录修正原因，不要猜。
- 阻塞时停在该阶段，报告具体阻塞点和已尝试做法，不要绕过去做后面的阶段。

请从检查当前目录、git 分支、工作树和基线测试开始，然后从 PHASES.md 的 P5 继续。阶段性汇报已完成内容、验证结果和剩余项；收尾给出变更范围、测试结果、两模型验收实测数据与准确剩余阻塞。
```
