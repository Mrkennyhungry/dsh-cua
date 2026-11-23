# DSH 桌面运行时重构方案

方案日期为 2026-11-23。本文档来自项目审阅和用户讨论，用作新 high 模式开发会话的任务入口。

## 阅读顺序

1. 项目根目录的 `AGENTS.md`。
2. 本文档，理解已确认的产品方向和重构边界。
3. [PROTOCOL.md](./PROTOCOL.md)，理解六个公开工具及内部数据契约。
4. [PHASES.md](./PHASES.md)，按开发阶段实施并更新进度。
5. [ACCEPTANCE.md](./ACCEPTANCE.md)，执行对应验证并填写结果。
6. [HIGH_HANDOFF.md](./HIGH_HANDOFF.md)，用于开启新的实现会话。

## 当前工作状态

| 项目 | 当前值 |
|---|---|
| 仓库绝对路径 | `C:\Users\Admin\Documents\deepseek-harness\default-workspace\dsh-cua` |
| 开发分支 | `codex/agent-runtime-refactor` |
| 方案起点提交 | `8bea14227945420771f20b348ce777fad0d9f749` |
| 起点版本 | `0.4.14` |
| 本轮交付 | 开发方案、协议、阶段清单、验收和交接提示词 |
| 产品代码状态 | 实现尚未开始 |

创建本方案时，根目录 `AGENTS.md` 是已有未跟踪文件。新会话应先读取工作树状态，识别既有改动。

## 已确认的方向

用户确认 DSH 是主入口，目标模型是响应快、成本低且支持识图的模型，主要考虑 `deepseekv4.1flash1017` 和 `hy4-flash`。具体模型标识与提供方能力以用户 DSH 配置为准。

主要目标是减少项目冗余、缩小 AI 的工具选择空间、建立确定的默认流程，并改善桌面认识和操作效率。实现应围绕这些目标进行取舍。

用户很少使用内置自主 Agent、独立聊天工作台、IM、WeCom 和 cron，已接受将它们迁出默认产品运行链和默认发布内容的方向。Git 历史和用户的运行数据用于保留历史能力与恢复依据。既有账号、浏览器 profile、知识、会话与文件数据需要有明确迁移路径。

浏览器默认采用插件专用的持久化 profile，复用 Cookie、登录状态、站点设置和页面状态。首次登录、验证码和站点登录失效仍可能需要用户介入。现有浏览器连接作为显式连接能力保留，日常默认路径使用专用 profile。

DSH 主模型负责理解任务、选择对象和规划下一步。插件负责采集、对象定位、输入、等待、读回与操作生命周期。任务调度、通用终端和文件工作、长期任务记忆由 DSH 已有能力承担，实施时核验真实能力覆盖。

本重构沿用用户本机已有的运行权限。流程约束服务于定位正确性、状态一致性、取消和恢复。

## 产品完成后的默认流程

```text
DSH 主模型
  │
  ├─ open ──────── 打开或选择目标，获得初始观察
  ├─ observe ───── 读取当前状态或补查一个区域
  ├─ act ───────── 引用目标执行，并获得动作后读回
  ├─ wait ──────── 等待界面条件或瞬态提示
  ├─ status ────── 查看运行时或操作进度，可有限 follow
  └─ stop ──────── 取消本会话进行中的操作
         │
      DSH Host adapter
         │
      DesktopRuntime
         ├─ 目标、观察、引用、帧、操作状态
         ├─ DOM / UIA / 视觉感知
         ├─ 动作路由、读回、等待、取消
         └─ Windows / 浏览器 / 物理输入
```

## 六个公开工具

默认模型工具清单固定为以下六个名称。

- `doraemon_open`
- `doraemon_observe`
- `doraemon_act`
- `doraemon_wait`
- `doraemon_status`
- `doraemon_stop`

open、observe、act、wait 承担交互闭环。status 和 stop 承担运行管理。status 继续满足仓库要求的新 DSH 会话验证入口。

旧工具中与同一操作目的重叠的入口收敛到统一实现。默认工具参数聚焦目标、意图与需要读取的条件。DOM、UIA、物理输入、租约、缓存和图像坐标变换由内核管理。

开发诊断通过本地日志、测试、管理端点和显式开发配置提供。默认模型工具表的数量和行为在不同任务间保持可预测。

## 核心技术决定

### 统一观察与元素引用

一次观察生成 observation_id，包含目标身份、元素 ref、相关正文、必要图片与截断信息。内核保存完整 locator、UIA identity、frame、坐标变换和能力信息。

UIA、DOM、OCR 与 YOLO 的采集结果属于同一目标上下文。每种后端在一次观察中最多执行一次，fallback 可以复用已经得到的结果。不同来源记录采集时间和身份，界面在采集期间变化时报告一致性情况。

模型优先使用 ref 操作。坐标回退统一使用所展示 frame 的图像像素，并显式绑定 frame_id。窗口、客户区、屏幕、多显示器与缩放转换集中实现。

### 一个主要感知后端

连接的浏览器页面优先 DOM，原生应用优先 UIA，语义覆盖较差的界面使用图片。OCR 用于必要的文本补充，YOLO 用于必要的图标候选。首次视觉使用时加载对应模型与权重。

主模型直接理解图像。默认 observe 和 act 的远程 caption 请求数为零。局部语义选择由主模型基于观察完成。历史 goal/decide/KB 自动决策退出默认点击路径。

### 动作自带读回

act 在同一次工具结果里返回逐步执行状态和轻量观察。输入读取值，选择读取选中状态，导航读取页面或窗口身份。出现新布局、弹窗或未知状态时附上新的视觉证据。

同一目标当前已知的连续步骤支持短批量。导航、弹窗、引用失效、预期不符时返回停止位置和已完成步骤，下一轮从当前状态继续。

### 小范围状态管理

核心状态包括 session owner、target、observation、element ref、frame、operation。状态由 runtime 管理生命周期和有界缓存。真实系统输入共用锁，浏览器操作保持线程亲和。

执行前校验引用身份和当前几何。已派发且结果未知的动作保留 operation_id 并支持查询。超时、取消、重复请求和进程重启纳入同一套操作记录。

### 专用持久化浏览器

profile 存放于可写 stateDir 下。复用当前有效的专用 profile 时首先确认实际路径和浏览器占用情况，迁移采用可恢复的文件操作。多个同 URL 页面依靠稳定 tab_id、frame_id 区分。

完成一段 DSH 对话后释放该会话的运行态与订阅。专用 profile 和已创建的用户工作成果按约定持久保存。显式关闭目标、停止操作、卸载插件和关闭浏览器分别定义资源范围。

## 推荐代码边界

Host 保持 ESM 和既有插件合同，Python 保留现有包名与 Windows 适配资产。

| 模块 | 最终职责 |
|---|---|
| `index.js` | 六工具声明、参数适配、结果 renderer、Cordis 生命周期 |
| `bridge-client.js` | HTTP、图像资源、有限 follow、取消传递、传输错误 |
| `kernel.js` | 启动、接管、build/capability 检查、进程生命周期 |
| `kernel/dsh_desktop/runtime.py` | 具体 Windows 服务、输入锁、上下文与资源生命周期 |
| `kernel/dsh_desktop/contracts.py` | 轻量数据对象、统一状态与坐标合同 |
| `kernel/dsh_desktop/observation.py` | 感知选择、结果复用、引用、正文、图片选择 |
| `kernel/dsh_desktop/actions.py` | 路由、批量、逐步结果、读回、条件等待、取消 |
| 现有 `game/` 与适量感知模块 | Windows、截图、输入、DOM、UIA、OCR 的具体实现 |
| companion 与 Host mirrors | 可选的运行事件订阅、窗口选择和取消入口 |

文件边界可随已有可复用代码微调，每个阶段记录实际变更。通用 backend 基类、动态插件注册框架、跨产品 orchestrator 和覆盖所有应用流程的状态机均不属于本轮必需交付。

## 能力去留与迁移

| 当前能力 | 本轮处理 |
|---|---|
| Windows 窗口绑定、DPI、多屏、焦点与输入 | 进入共享 runtime，保留真实行为验证 |
| UIA、DOM、截图、OCR、YOLO | 收敛采集与路由，重用底层实现 |
| 远程 caption、goal/decide 自动选目标 | 退出默认交互链，移除相应默认模型依赖 |
| 多套观察、元素、命中与状态缓存 | 收归观察版本与 runtime 上下文 |
| 内置 Eternity/Hermes Agent 与委托工具 | 迁出 DSH 默认入口与发行依赖 |
| 独立 Web UI、IM、WeCom、cron | 作为历史独立能力归档或移到 standalone 边界，退出默认发布 |
| 知识库复盘和 Markdown 管理 | 迁出默认运行链，明确保留用户数据位置 |
| 应用能力与定位线索 | 可在目标上下文轻量保存，所有执行使用当前对象身份 |
| 文件和通用命令工具 | 由 DSH 现有能力覆盖，桌面 attach 保留选文件语义 |
| watch | 收入 wait 和 act 的有界瞬态观察能力 |
| 桌宠与选择器 | 继续作为可选 UI，订阅 runtime 事件 |

移出默认发行前，列出旧入口、旧配置、旧数据与替代路径。迁移脚本和兼容读取按具体已有格式实现。旧的生产数据目录保留可追溯来源，兼容端点在迁移完成后删除。

## 审阅中发现的实施索引

以下行号来自方案起点提交，开发时用符号重新定位。

| 位置 | 已发现的事项 |
|---|---|
| `index.js` 44 附近 | 共享 renderer 只有 JSON 文本，需要验证图片进入实际模型请求 |
| `index.js` 194 与 `companion-state.js` | 默认 selection sync 可经 ensure 提前启动内核 |
| `index.js` 339 附近 | follow=false 的声明与实际等待行为存在冲突 |
| `index.js` 690 附近 | goal 定位与执行分成多个 Host 请求 |
| `dsh_bridge_cli.py` 48、66 附近 | DSH 启动直接依赖 Eternity.setup |
| `tool_decorator.py` 29、`desktop_tools.py` 52 附近 | 底层感知/执行顶层导入 Hermes registry |
| `desktop_tools.py` 4376 附近 | 元素表退回 analyze 后重复扫描 UIA |
| `desktop_tools.py` 4540、4575 附近 | PrintWindow 和客户区截图边界不一致 |
| `desktop_tools.py` 1461、2524、4430 附近 | 多套缓存的 key、版本和失效规则分散 |
| `web_tools.py` 779、826、1721 附近 | selector 截断、跨 frame nth 和快照排序影响定位 |
| `web_decide.py` 139、173 附近 | ambiguity 丢失、候选字段读取格式漂移 |
| `dsh_bridge.py` 1765 附近 | 统一观察串行采集并重复输出来源，融合半径单位名称失真 |
| `game/input.py` 186 附近 | 默认拟人移动时长可以收敛到快速输入与专门时序 |
| `kernel.js` 174、475 附近 | 常驻内核接管需要版本、build 与 capability 判断 |
| `eternity.py` 1184 附近 | 全局清理需要迁到目标与会话资源归属 |

## 当前验证边界

此前 `npm test` 有 16 项通过，`plugin.test.js` 因缺少 `@deepseek-ai/schemastery` 未能加载。系统 Python 与已有桌面 venv 都缺少 pytest。实现阶段先建立可重复的项目测试环境，再取得新基线。

当前结论主要来自源码与相关测试阅读。尚未测量两个目标模型的识图效果、图像 token、端到端成功率与时延。图片尺寸、元素预算、等待窗口属于需要实测的默认初值。

## 外部参考

精简交互快照、元素引用和局部展开可参考官方项目。Windows 身份、DPI、输入与恢复需要使用本仓库的实际约束实现。

- [Playwright MCP](https://github.com/microsoft/playwright-mcp)
- [agent-browser snapshots](https://agent-browser.dev/snapshots)
- [agent-browser selectors](https://agent-browser.dev/selectors)
