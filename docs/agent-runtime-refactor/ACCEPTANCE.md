# 验收与性能对照

## 1. 使用方式

本文供后续 High 模式实施使用，与本目录的架构、协议和阶段计划共同生效。阶段顺序以阶段计划为准，本文件按交付物给出退出门槛。每完成一个阶段，执行对应验证并记录结果，再继续依赖该阶段的工作。

验收围绕 DSH 主入口和两个候选模型 `deepseekv4.1flash1017`、`hy4-flash`。目标是让模型通过稳定的六工具完成桌面任务，降低工具选择、感知和恢复成本。正确率、调用次数、延迟与实际成本共同决定重构收益。

优先扩展已有测试。只有现有测试覆盖不到具体回归时才增加用例。保留真实公共入口和行为断言，源代码字符串匹配只能用于适合静态检查的契约。

## 2. 已知基线与环境恢复

本轮调研基于 2026-11-23 的 `0.4.14`，已有结果如下。

| 项目 | 已观察结果 | 实施阶段的处理 |
| --- | --- | --- |
| Node 测试 | 16 个用例通过，`test/plugin.test.js` 因缺少 `@deepseek-ai/schemastery` 加载失败，整体命令失败 | 先恢复项目测试依赖解析，再完整重跑 |
| Python 测试 | 系统 Python 3.11 与已安装运行时 Python 均缺少 pytest | 选定实际解释器，在对应测试环境安装测试依赖并执行 |
| DSH peers | 公共 npm 查询的 `@deepseek-ai/dsh-tools` 最新版本为 `0.0.1-rc.1`，项目声明要求 `>=0.2.1-rc.2` | 检查运行中的 DSH 所提供版本与解析路径，建立匹配的项目本地测试环境 |
| 桌面延迟 | 尚未执行可复现基准 | 阶段开始前采集旧版本数据 |
| 两个 Flash 模型 | 尚未执行任务对照 | 在相同任务集、账号状态和环境下采集 |

DSH 提供的 Cordis、tools、schemastery 继续作为 optional peer。先核对 DSH 的实际版本与已有测试支持方式，再处理依赖。公共注册表的最新包名版本不足以证明兼容性。

固定并记录 Node、Python 绝对路径、DSH 版本、peer 版本、浏览器版本、Windows 缩放与显示器布局、插件提交和模型标识。Python 的 pip、pytest 与实际执行测试的解释器必须对应。

在仓库根目录执行常规检查。选定虚拟环境时将 `python` 换成该解释器的绝对路径。

```powershell
npm test
$env:PYTHONPATH = Join-Path (Get-Location) 'kernel'
python -m pytest test -q
npm pack --dry-run --json
```

现有 `test/test_cleanup_dialog_live.py` 通过 `DSH_LIVE_DIALOG_TEST=1` 启用真实窗口用例。先读当前文件确认开关与操作，再显式运行。常规测试中该用例按既有约定跳过，报告应列出跳过原因和单独实测结果。

每次记录必须区分通过、失败、预期跳过、环境阻塞、尚未执行。原有失败先复现并定位。环境阻塞应保留完整命令、解释器和摘要，继续执行独立可用的检查，待恢复后补齐对应门槛。

## 3. 阶段退出门槛

| 对应交付物 | 必须具备的证据 |
| --- | --- |
| 基线与契约冻结 | 旧版测试结果、固定任务集、实际 DSH 图片传递链路、工具参数 DSL 与输出格式均已核实，环境缺口有明确恢复路径 |
| 共享 runtime 与桥接 | 无 Hermes、无模型密钥时可启动健康检查及 UIA/DOM 流程，公共入口的取消、错误和生命周期用例通过 |
| 统一感知与引用 | DOM/UIA 引用、图像帧、几何变换、失效重验证和按需视觉加载矩阵通过，模型请求中可验证原生图片 |
| 执行、等待与恢复 | 单步、短批次、瞬时反馈、操作状态查询、幂等及取消用例通过，重复动作与跨会话输入问题有回归覆盖 |
| 六工具 DSH 接入 | 新 DSH 会话发现默认六工具，真实调用 open → observe → act → wait/status/stop 的任务链通过 |
| 持久化浏览器与可选组件 | 重启后 fixture 登录状态恢复，profile 锁可诊断，companion 开关和卸载资源回收通过 |
| 清理、打包与交付 | 默认依赖可独立安装，发布文件齐全，既有支持行为迁移完成，完整回归与两模型对照已记录 |

跨阶段的硬门槛包括公共协议兼容、图片确实送达模型、目标与坐标正确、操作状态可恢复、会话资源隔离和真实 DSH 发现成功。阶段内继续完成独立可验证项，该阶段所有退出项 PASS 并独立提交后才进入下一阶段。

性能退出条件在采集旧版数据后固定。相同任务的最终完成率至少保持基线水平，重构产生的可复现功能回归必须修复。速度或成本优化声明必须对应实际测量，异常慢调用应能定位到模型、采集、桥接、执行或等待环节。

## 4. 公共契约与部署矩阵

| 检查面 | 输入与场景 | 通过条件 |
| --- | --- | --- |
| Host 契约 | 正常导入、apply、卸载 | 保持 `name`、`inject`、schemastery `Config`、同步 `apply`，注册及资源生命周期符合根目录 AGENTS.md |
| 工具集合 | 默认配置的新 DSH 会话 | 恰好公开 `doraemon_open`、`doraemon_observe`、`doraemon_act`、`doraemon_wait`、`doraemon_status`、`doraemon_stop`，历史底层能力经约定迁移后可由 runtime 调用 |
| 参数与错误 | 必填缺失、错误类型、无目标、运行失败、取消 | DSH plain-object 参数 DSL 正确，参数错误抛出，运行失败保持 `success:false` 与既有错误识别语义 |
| 公共调用链 | 从实际 defineTool execute 到 bridge 与 kernel | 参数、owner、call/operation 身份、结果和图片序列化完整，所有 fetch 转发 `exec.signal` |
| 启动解耦 | Hermes 不在环境、provider 配置为空、无模型 API key | 默认插件导入、启动和 UIA/DOM 操作成功，依赖图中默认路径无需内部 Agent/provider |
| 按需视觉 | 移除可选 OCR/YOLO 权重，执行 UIA/DOM 任务 | 健康检查与相应任务可用，视觉能力缺失可由 capabilities 识别，默认远端图标描述请求数为零 |
| 版本握手 | bridge/kernel 协议不匹配、旧 kernel 遗留、断线重连 | capabilities 与版本不匹配有确定结果，连接身份正确，无法把旧 kernel 的响应冒充当前操作结果 |
| 源目录只读 | runtime 状态目录可写、插件源目录只读 | profile、截图与运行状态按配置写入 stateDir，正常流程不依赖源码可写 |
| 打包 | dry-run 文件列表及实际安装 | 新运行文件在 files 白名单内，peer 身份正确，默认依赖收敛，所需资源可从安装包加载 |
| 安装 | DSH Plugins/install_bundle 安装、新会话 | 无需手写 profile 所有文件，新会话发现六工具，`doraemon_status` 返回当前运行时状态 |

## 5. 感知、目标与图片矩阵

| 检查面 | 必测场景 | 通过条件 |
| --- | --- | --- |
| 图片原生传递 | 真实 DSH renderer 到实际模型请求 | 请求包含接口支持的原生 image 内容与正确媒体类型，模型能够读取 fixture 中只有图片可见的信息，路径或 JSON 字符串不能充当成功证据 |
| 帧与限额 | 默认截图、裁剪、压缩、超限图片、连续观察 | 图片尺寸、frame_id、坐标基准可核对，超限处理保留变换信息，发送的图片与对应引用属于同一观察结果 |
| 图像几何 | PrintWindow、client 区域、桌面范围、移动与缩放 | 捕获区域定义一致，模型坐标能映射到实际点击位置，裁剪偏移、缩放比和客户区偏移均参与变换 |
| DPI 与显示器 | 100%/150% 缩放、不同 DPI 屏幕、负屏幕原点 | 屏幕、窗口、客户区和图像坐标闭合，跨屏移动与 resize 后重新验证 |
| 引用与分页生命周期 | 同文案控件、控件替换、navigation、scroll、虚拟列表、正文分页 | ref 绑定正确对象与观察，cursor 复用同一快照，缓存过期有明确反馈，重验证不会把另一个同名对象视作原对象 |
| UIA 退化 | 空树、超时、不可访问控件、几何小幅变化 | 返回有界结果与可执行恢复信息，保留现有失效和几何验证行为 |
| DOM 属性 | 很长的 selector、role、input_type、checkbox、select | 可用字段经过桥接与压缩后保留，操作语义正确，长 selector 无截断导致的错目标 |
| frame 与 tab | 嵌套 iframe、nth 匹配、同 URL 多 tab、重复标题 | tab/frame 身份稳定，重复项可消歧，动作作用于已选实例 |
| 弹层与焦点 | DOM overlay、浏览器 dialog、桌面 owned popup | 当前交互层可识别，动作结果与焦点变化被读回，弹层出现后按最新状态执行 |

优先复用 `test/test_uia_element_invalidation.py`、`test/test_dpi.py`、`test/test_foreground_gate.py` 和现有 web、watch、compact、frame-limit 相关测试。具体文件与行号在实施前重新检查。

## 6. 执行、并发和等待矩阵

| 检查面 | 必测场景 | 通过条件 |
| --- | --- | --- |
| 单步读回 | 点击、输入、选择、滚动 | 返回精简可判定的执行与观察结果，能够判断目标变化或期望状态，结果体积有记录 |
| 短批次 | 全部成功、第 k 步目标失效、意外弹窗、部分执行后超时 | 返回逐步状态、停止位置与当前状态，已完成步骤可辨识，后续输入在停止条件触发后终止 |
| 操作身份 | 重复 call/operation、网络断开、响应丢失、重连 | 区分未派发、已派发、待定/结果未知、已观察和期望状态，查询能解析未知结果，重试策略依据已记录状态 |
| 幂等恢复 | dispatch 前后分别中断、已成功但响应丢失、kernel epoch 更新、完整结果过期 | 持久 journal 与幂等摘要保留期可验证，同一请求身份的重送关联原操作，未知副作用先核对现场，恢复过程可通过 status/follow 验证 |
| 取消 | 等锁排队、执行中、等待中、卸载时、只取消 status follow | 原始操作取消向下传递，排队项移除，后续输入终止，锁与观察资源释放，取消查询仅结束该次查询 |
| 输入串行化 | 两个 DSH 会话同时键鼠操作 | 全局输入互斥，动作与必要读回保持约定的原子边界，目标激活和焦点结果正确 |
| owner 隔离 | 两会话打开目标，清理/stop 其中一个 | stop 范围符合请求，另一会话的目标、操作、订阅与 artifact 保持可用 |
| 瞬时反馈 | fixture 点击后出现短暂 toast | 监听在触发动作前就绪，返回捕获到的变化及 artifact，严格验证先 arm 后触发的顺序 |
| 等待生命周期 | 命中条件、超时、取消、目标关闭、TTL 到期 | 最终状态确定，watch 与截图资源释放，artifact 保留和过期规则可测试 |
| 重启恢复 | pending 时 kernel/Host 重启 | 旧 operation 身份处理明确，结果未知有可查询说明，新运行时不会静默复用过期 ref |
| 可选 companion | 关闭、开启、UI 状态轮询、Host 卸载 | 默认可独立运行，companion 消费 runtime 事件，读取 UI 状态与卸载清理不会意外 ensure/start kernel |

操作恢复测试可注入 bridge 故障与 fixture 日志，证明实际输入次数。不要只检查返回的 `success` 字段。

## 7. 真实任务集

测试文件和网页由 fixture 管理，任务完成标准在运行前固定。执行测试遵循已有授权，沿用项目当前运行方式。

| 任务 | 固定条件 | 完成标准 |
| --- | --- | --- |
| 记事本编辑保存 | fixture 临时目录与唯一文本 | 正确窗口写入指定文本并保存到指定临时文件，读取文件内容一致，保存提示状态正确 |
| 本地表单 | text、password fixture 值、checkbox、select | 所有字段达到指定值，本地提交结果页或 fixture 日志仅出现一次成功记录 |
| 本地上传 | fixture 文件与 file input | 上传页显示期望文件名与校验摘要，回读匹配 |
| tab 与 iframe | 相同 URL 的两个 tab、嵌套 frame、重复按钮 | 只有指定 tab/frame 的 fixture 状态变化，其他实例保持其初始 fixture 状态 |
| 瞬时 toast | 触发即显示短时反馈 | act.expect 捕获期望文本及对应 artifact，status 可查询已存证据，任务日志证明监听先就绪 |
| DPI 与跨屏 | 窗口移动、resize、负原点屏幕 | resize 前后都能定位同一语义目标，点击落点和结果正确 |
| 取消与并发 | 两会话 fixture 输入日志 | 顺序和归属正确，stop 后无尾随输入，另一会话继续完成 |
| 持久化浏览器 | 专用 profile、本地 cookie/登录态 fixture | 首次设置状态，正常关闭并重启后同 profile 恢复状态，重复启动 profile 锁有可诊断结果 |

持久化验收覆盖首次登录、登录过期和验证码引导状态。专用 profile 能减少重复登录，实际站点需要人工处理的环节应如实返回并保留继续任务的入口。

## 8. 两个 Flash 模型的对照

对旧版与重构版分别使用 `deepseekv4.1flash1017`、`hy4-flash`。记录可获取的模型版本、temperature、最大输出、图片限制、DSH 系统提示和工具描述。任务指令保持一致，工具 schema 使用对应版本实际公开的形式。

每个模型先执行整套任务各一次，排除 fixture 与接入错误。正式对照中每个任务、每个模型、每个版本至少重复三次，条件允许时五次，并记录每次结果。样本不足时明确列出次数。冷启动与已启动 runtime 分开记录，浏览器 profile 的初始 cookie、窗口布局和缓存条件按场景恢复。

从用户任务提交开始计时，到预定义业务完成标准被验证为止。失败、超时和人工介入均保留原记录。恢复后成功单独标注，避免重跑后覆盖初次失败。

每次运行记录以下数据。

- 最终任务成功、失败原因、人工介入与是否经过恢复。
- 模型请求次数、工具调用次数、失败调用数、重试与恢复调用数。
- DOM、UIA、截图、OCR、YOLO 的调用数与耗时，远端图标描述请求数。
- 图片张数、实际发送尺寸与总像素面积，文本观察的大小。
- 可获取的输入/输出 token、缓存 token、图片计费项与真实价格来源。
- 端到端耗时、模型耗时、各工具耗时和总体工具延迟 p50/p95。
- 冷启动时间、首个有效观察时间、profile 恢复结果。

工具 p50/p95 应基于已记录的调用样本计算，并同时列样本数。少量任务的 p95 波动较大，只适合描述本次样本。实际 usage、缓存计费或模型价目不可获取时标为未知，保留可测的请求与图片统计。

比较时先核对业务成功率，再比较成功任务的端到端耗时、调用数和成本，同时附上失败尝试的消耗。两模型分别汇总，任务类型分别展示。任何百分比收益均由旧新测量值计算，文档计划阶段不预设收益。

## 9. 可复用记录模板

```text
提交 / 版本 / 阶段
DSH / Node / Python / peer / 浏览器版本
模型完整标识 / 采样参数 / 提示与工具版本
显示器布局 / DPI / profile 初始状态
任务与预定义成功标准
命令或 DSH 调用入口
结果    通过 / 失败 / 预期跳过 / 环境阻塞 / 尚未执行
证据    业务状态、fixture 日志、operation、必要截图
调用    模型、工具、DOM、UIA、截图、OCR、YOLO
图片    张数、尺寸、总像素面积
耗时    总体、模型、工具、冷启动，样本数与 p50/p95
成本    token、缓存、图片、价目来源，未知字段逐项标记
恢复    失败原因、重试次数、人工介入、最终状态
待办    对应文件、责任阶段、复验命令
```

最终交付检查清单如下。

- [x] 常规 Node/Python 套件完整执行，失败、跳过及环境阻塞有记录，见第 17 节。
- [x] 六工具通过实际 DSH 新会话发现，参数与图片链路实测通过。
- [x] 无 Hermes、无 key、无视觉权重的对应路径通过。
- [ ] 目标引用、坐标、frame、取消、幂等、并发、瞬时反馈矩阵全部通过。可执行部分已通过，真实异 DPI/负原点多屏仍受硬件条件阻塞。
- [x] 专用持久化 profile 重启恢复，首次登录与过期状态有验证。
- [x] companion 可选运行及卸载行为通过。
- [x] 发布白名单与依赖检查通过，安装后新会话状态正常。
- [x] 两模型旧新对照与逐次结果已保存，性能结论可追溯。
- [x] 用户可按交付说明在 High 实施记录中找到未验证项和剩余条件。

本文件中的现有源码与测试名称来自调研时版本，实施后按实际路径与契约复查。

## 10. P0 执行记录

起点为 8bea14227945420771f20b348ce777fad0d9f749，分支 codex/agent-runtime-refactor，运行版本 0.4.14。本阶段改变测试准备、测试断言及开发文档打包范围，运行版本保持 0.4.14。后续行为变更遵循版本规则。

### 环境与恢复

| 项目 | 实测值 |
| --- | --- |
| Node | D:\Apps\node22\node.exe，22.23.4 |
| Python | C:\Users\Admin\AppData\Local\Programs\Python\Python311\python.exe，3.11.7，项目 .venv 同版本 |
| 测试包 | pip 26.3，pytest 9.2.0 |
| DSH | 0.2.2-rc.3，安装内 Electron Node 24.21.0 |
| 实际 peers | dsh-tools 0.2.2-rc.3，schemastery 3.19.0，cordis 4.1.0，共复制 SDK 依赖闭包 28 包到忽略的 node_modules |
| peer 范围差异 | 项目 cordis ~4.0.5 与安装的 4.1.0 有差异，实际 API 测试通过，P6 核对发布兼容范围 |
| Windows | 10.0.26200，1 屏，矩形 [0,0,2560,1440]，有效 DPI 96×96 |
| 浏览器 | Edge 158.0.4370.33，既有专用 profile 位于 %LOCALAPPDATA%\dsh-desktop\web_profile_edge |

起点 npm test 复现 16 PASS、1 加载失败，原因是缺少 schemastery。prepare-test-peers.mjs 从本机 DSH ASAR 复制真实 SDK 及依赖，保留三个 optional peer。恢复后测试暴露了旧工具数量、click 参数和发布白名单扫描断言过期，按当前真实行为修正。发布文件扫描改用 npm pack 实际列表，开发方案与已有机器路径设计笔记从 npm 包排除，仓库文件保留。

按要求创建 .venv，初次 pip 安装缓存卡住，增加 --no-cache-dir 后成功。Python 初始 32 个收集错误，经 prepare-test-python.ps1 复用已安装 kernel 的同版本依赖后恢复。COM 生成缓存存在共享 TEMP 权限冲突，check.ps1 使用项目独立临时目录解决。

### P0 退出项

| 项目 | 状态 | 实测结果与入口 |
| --- | --- | --- |
| 全绿测试基线 | PASS | scripts/check.ps1，提交前复跑 Node 77 PASS / 0 FAIL / 0 SKIP，1746.0537 ms；Python 262 PASS / 0 FAIL / 1 预期 SKIP / 13 subtests PASS，8.18 s；Host/Client 语法 2/2 PASS |
| 真实窗口补验 | PASS | DSH_LIVE_DIALOG_TEST=1，.venv/Scripts/python.exe -m pytest test/test_cleanup_dialog_live.py -q -s，1 PASS，2.66 s；常规跳过的 1 项需要显式开关，保存提示保留验证后清理本次 fixture |
| SDK 参数与 renderer | PASS | test/dsh-contract.test.js，3/3 PASS，4 种非法嵌套输入执行次数 0，预取消执行次数 0，真实 pipeline 原生图片附件保留 |
| 两条实际模型路由 | PASS | 用户已确认 wb/cn:deepseek-v4.1-flash 与 wb/cn:hy4std，各 1/1 独立图片字符与方位正确 |
| 固定起点指标 | PASS | 真实 Host execute → HTTP kernel，16/16 成功，fixture 3/3；数值见下表和 p0-evidence/legacy-baseline.json |
| 后续任务完成标准 | PASS | 第 7 节固定 8 类任务及成功条件，P7 执行旧新版本逐次对照 |
| 发布文件检查 | PASS | npm pack --dry-run --json --ignore-scripts，162 文件，压缩 27570384 bytes，解包 30449573 bytes，排除的开发笔记进入发布包 0 文件 |

### 旧入口测量

node scripts/measure-legacy-baseline.mjs 使用温启动的既有 kernel，注册当前真实 Host 工具，启动唯一标题的自有 Win32 fixture。每轮执行列窗口、绑定、UIA、截图解码和解绑，重复 3 次，健康检查 1 次。成功条件为同一自有窗口绑定成功、读取根控件、截图可解码并完成解绑。准备阶段窗口发现轮询未计入样本。

| 指标 | 实测值 |
| --- | --- |
| 默认工具数量 | 31 |
| kernel 协议 | 1 |
| 工具样本 | 16，成功 16，失败 0 |
| 工具时间总和 | 886.177 ms |
| p50 / p95 | 7.524 / 270.362 ms，最近秩法，n=16 |
| UIA | 3 调用，270.362 / 261.292 / 262.679 ms，每次后端尝试 1，每次命名根控件 1，截断 0 |
| UIA 输出 | 1525 / 1524 / 1524 bytes JSON |
| capture | 3 调用，14.052 / 11.440 / 5.875 ms |
| 图片 | 3 张 JPEG，每张 480×180、6950 bytes，总面积 259200 pixels |
| 该基准模型请求 / 模型接收图片 | 0 / 0，旧通用 renderer 输出 JSON 文本 |
| OCR / YOLO / caption | 本基准未埋点计数，当前 UIA/capture 源码路径未请求 parser |

### 双模型图片通路

新 DSH 会话“P0 图片读取独立验证”使用框架内置 read_image。两张图片各 800×500、400000 pixels，随机字符只写在像素中，提示与文件名均无答案。安装 SDK 的 provider 序列化源码、实际工具轨迹与模型读取结果共同验证图片进入模型输入。P5 负责 doraemon 图片 renderer 闭环，P7 负责完整任务、成本和端到端比较。

| 实际路由 | 结果 | 请求与图片 | 时间与 usage |
| --- | --- | --- | --- |
| wb/cn:hy4std | PASS，0C2FED，bottom-right，1/1 | read_image 1，模型请求 2，PNG 5871 bytes，图片 1 | UI 回合 21 s，聚合约 40.9K token |
| wb/cn:deepseek-v4.1-flash | PASS，独立新图 875922，bottom-right，1/1 | read_image 1，模型请求 2，PNG 6278 bytes，图片 1 | 末请求 3.23 s，首 token 2.60 s，生成 631 ms，输入 21631、缓存 20992、输出 182 token；完整回合时间未知 |

前序路由尝试保留结果。openrouter/deepseek/deepseek-v4.1-flash-1017 的 read_image 1 次被路由 image input 声明校验拒绝，模型请求 2、图片送达 0、UI 13 s。wb/cn:hy4free 返回 API 400/model_unavailable，上游 11102、图片送达 0。用户确认以成功的两个 wb 路由作为后续验收目标，见 ADR 0001。hy4std 由用户在 DSH 中切换后发送，deepseek 使用另一张随机图独立验证。

本次各路由成功样本量为 1。provider 原始 HTTP 请求体、图片独立计费和精确金额未知，UI 的约 ¥0.02 显示存在舍入，成本字段保留未知。P0 的测量用于确认通路与建立起点，性能收益在 P7 实测后填写。

## 11. P1 执行记录

2026-11-24，版本 0.4.15，P0 提交 2d03f81 之后实施。contracts.py 承载图片结果常量和提取函数，runtime.py 复用原 AppController 服务、选择器、截图和 overlay 辅助实现，AppController 继承共享实现。配置只读取 workspace/parser，parser 在并发锁内延迟初始化。CLI 直接构造 runtime，Agent task 路由在 legacy_bridge.py 中显式安装。默认 task 请求返回 503 和迁移诊断，历史数据仍保存在原 home。

Host 取消了启动时的模型 credential 解析，保留既有高权限启动机制。health 协议升级为 2，并给出 build、epoch、capabilities、已初始化后端和 parser 构造次数。旧协议或缺失能力的内核返回明确处理指引，正在运行的用户内核由用户完成或停止操作后再关闭。详见 ADR 0002。

| P1 退出项 | 状态 | 实测结果 |
| --- | --- | --- |
| 无 standalone 依赖启动 | PASS | scripts/prepare-runtime-test.py 创建独立 venv，基础分发包 42，Hermes/pywebview/croniter/onnxruntime/rapidocr/anthropic 安装 0；runtime_probe 还主动拒绝导入，实际禁用模块加载 0 |
| 基础 UIA/DOM 与截图 | PASS | 通过 create_app 的真实 HTTP 路由入口和 TestClient 启动生命周期，health 协议 2，列窗口 13，自有窗口截图 JPEG 480×180，UIA 控件 1，本地网页 DOM 匹配 1；provider Key 为空，weights 目录不存在 |
| 按需初始化 | PASS | 完成上述动作 parser 构造 0，OCR/YOLO 包 0，远端 caption 请求 0；并发访问 8 次，parser 构造 1 次 |
| companion 与取消/卸载 | PASS | 实机 start/shutdown 2 轮，2 个子进程和 2 个 overlay 线程均退出；回调取消 1/1，watch 清理 2/2；idle polling 和 unload 启动请求 0，后台读失败调用 1 次 |
| 协议与恢复 | PASS | 旧协议拒绝 1/1，缺失 capability 拒绝 1/1，兼容驻留内核 PID/state 接管用例通过；注入读取断线后 HTTP read 2 次、health 2 次，写响应丢失后派发 1 次；启动取消 1/1，保持未接管 |
| 完整回归 | PASS | scripts/check.ps1，Node 83 PASS / 0 FAIL / 0 SKIP，1598.7006 ms；Python 267 PASS / 0 FAIL / 1 预期 SKIP / 13 subtests PASS，8.80 s；语法 2/2 PASS |
| 打包 | PASS | npm dry-run 167 文件，压缩 27572465 bytes，解包 30456205 bytes，新 runtime/contracts/legacy_bridge 和 CHANGELOG 都在包内；三个 optional peers 保留 |

隔离实机日志 p1-final-probe 的 health 加列窗口时间为 1813 ms，截图/UIA/DOM 主体总耗时 6359 ms，n=1。companion 两轮耗时另行执行于同次进程中，此数值用于确认通路。测试使用独立临时 home 和本地网页，既有用户浏览器 profile 延续。

基础依赖曾尝试 pip 联网安装，在构建依赖阶段停滞，uv 重试下载也耗时较长。已终止本次安装进程，改为复制已安装分发包的声明依赖闭包。最终独立 venv 经 metadata 核对只有 42 包，无被排除包，也没有链接完整运行环境的 pth。复制脚本和 smoke fixture 已归档，可重复执行。清理了本轮产生的 25 个 pip 临时目录。

重构导入顺序曾触发 DPI 回归，PyAutoGUI 在 DPI 服务之前设置进程模式。desktop_tools 现在先调用现有 set_per_monitor_v2，再导入输入依赖，完整 DPI 套件已通过。真实旧驻留 kernel 保留，隔离验证在相同 create_app 工厂启动 HTTP 应用，CLI 全局单实例互斥继续生效。P5/P7 再经插件安装机制验证新 DSH 会话。

## 12. P2 执行记录

版本 0.4.16，P1 提交 cc31ffd 之后实施。TargetRegistry 按 owner 和 epoch 记录 desktop/window/tab/frame，FrameRegistry 保存捕获时 revision，最多 128 帧、300 秒。WindowIdentities 使用随窗口销毁自动消失的 Win32 属性，补足原有 hwnd/pid/process_created 对同进程 HWND 复用的识别。runtime 卸载释放属性句柄。

BrowserRuntime 复用单一 _WEB_EXECUTOR、真实 CDP tab 身份和 Playwright frame 对象。默认直接启动专用 persistent context，显式 connect 才执行 CDP 连接。新 profile 位于 stateDir/browser，已存在的 home/web_profile_edge 或 home/web_profile 继续使用。启动前检查进程命令行并持有按 profile 路径区分的全局 mutex，占用失败返回 BROWSER_PROFILE_UNAVAILABLE。首次实测发现只依赖 Chromium 的锁错误会附带打开两个空 tab，增加启动前检查后额外 tab 为 0。

open 在导航前登记 call 身份，失败导航的重试保留原 target 和错误，实际派发 1 次。当前内存记录上限 1024，owner 清理释放，跨重启持久 journal 在 P4 实施。跨 CDP 连接将同一真实 tab 归并到同一对象，owner 释放共享 tab 时交给存活 owner。外接浏览器在释放和卸载后继续运行。

| P2 退出项 | 状态 | 实测结果 |
| --- | --- | --- |
| 持久 profile 与占用 | PASS | 独立基础环境运行 targets_probe，正常关闭/重启 1 轮，Cookie 恢复 1/1，localStorage 恢复 1/1；profile 锁诊断 1/1；旧路径选择测试 1/1；未触碰既有用户 profile |
| 同 URL 与嵌套 frame | PASS | 同 URL 独立 tab 3 个，frame 3 层，内层按钮实际点击 1 次，其他实例变化 0；同 call 重试新建 tab 0；跨 CDP 身份复用与失败连接诊断各 1/1 |
| 截图与坐标 | PASS | 实机 PrintWindow 整窗 480×180，客户区 [108,131,464,141]，桌面 [0,0,2560,1440] 输出 1366×768；窗口移至 [200,160]、resize 600×240，crop [20,30,200,100] 缩至 100×50，图片点 [50,25] 映射屏幕 [320,240]；客户区 fallback 单测 1/1 |
| DPI 与负原点 | PASS | 参数化物理像素测试 4/4，DPI 96/144 × 原点 [0,0]/[-2560,-400]，含客户区偏移、crop、2 倍缩放和往返闭合；越界/NaN/bool 坐标拒绝 4/4；末像素映射 1/1 |
| owner 隔离与卸载 | PASS | A 清理后目标 0，B 保留 5 个可用目标，清理期间 B 的 300 ms 浏览器读取返回 42；显式连接的独立测试浏览器在 runtime 卸载后存活 1/1，页面标题读回 1/1 |
| 目标版本与关闭重建 | PASS | 窗口移动/resize revision 1→2；导航/滚动使旧帧失效 2/2；关闭 tab/window 拒绝 3/3；同进程 HWND 属性丢失/重新生成身份 1/1；epoch、owner、TTL 和容量淘汰测试通过 |
| 完整回归 | PASS | scripts/check.ps1，Node 83 PASS / 0 FAIL / 0 SKIP，1680.4914 ms；Python 276 PASS / 0 FAIL / 1 预期 SKIP / 17 subtests PASS，9.72 s；Host/Client 语法 2/2 PASS |
| 打包与依赖 | PASS | npm dry-run 171 文件，压缩 27583864 bytes，解包 30488345 bytes；新增 browser/geometry/targets/window_identity 共 4 个 runtime 文件均在包内，新增第三方依赖 0，optional peers 保留 |

实机入口为 .runtime/runtime-test-venv/Scripts/python.exe test/targets_probe.py .runtime/p2-gate-home，完整 fixture 耗时 8000 ms，n=1，parser 初始化 0。证据保存在 p2-evidence/targets-probe.json。实际机器为单屏、96 DPI，150% 与负屏原点由确定性几何测试验证，实际异 DPI 多屏操作仍列入 P7 硬件覆盖记录。

捕获基准按后端实际能力修正。原生图采用物理屏幕坐标，PrintWindow 整窗和 client fallback 均记录各自 source_rect。DOM 使用 Playwright scale=css 截图及 browser_css 坐标，通过同一映射实现处理缩放和 crop，由原有 browser worker 派发。外层 frame 元数据包含完整来源、模型矩形、缩放、DPI 和 revision。P3 继续将这些帧与语义观察、引用和布局变化关联。

## 13. P3 执行记录

版本 0.4.17，P2 提交 c5ed0ec 之后实施。ObservationService 统一 DOM/UIA、图片、query、ROI、ref 和分页，输出逐后端次数与毫秒耗时。复用既有 _SNAPSHOT_JS 与 _uia_batch_elements，扩充可执行字段和真实对象身份。旧 element_table 的 OCR fallback 关闭重复 UIA 扫描。视觉通过 UIParser 的 enable_ocr/enable_yolo 分别惰性加载。

| P3 退出项 | 状态 | 实测结果 |
| --- | --- | --- |
| 单次采集 | PASS | 实机 DOM 观察扫描 1 次、frame 3 个各 1 次；原生 UIA 1 次、截图 1 次，UIA 196.346 ms、截图 15.726 ms、总体 220.513 ms；ROI DOM 1、截图 1、OCR 1、YOLO 0；旧表格 fallback UIA 调用 1/1 |
| DOM 数据保真 | PASS | 107 元素、三层 frame、209 字符 selector 原样保留；中文字段 value、checkbox checked、select 两个 options 与 selected、dialog 首位排序各通过 1/1；password value 输出 0；旧对象被同文案 clone 替换后 ref 拒绝 1/1 |
| 正文与分页 | PASS | 正文 11230 字符、分页 2 页，元素总数 107、唯一 ref 107，完整正文读回相同；后续页新增 DOM/UIA/截图 0，observation_id 和 captured_at 相同；分页跨 owner、过期及非法偏移拒绝 |
| 引用与图片生命周期 | PASS | 原生 UIA ref 现场验证 1/1，窗口改名后拒绝 1/1；frame、owner、TTL、图片总量和观察数量淘汰共 4 个回归用例通过，关联帧随观察删除；后续纯语义页 bounds 0，图片跨 owner 读取拒绝 1/1 |
| 图像策略与局部读取 | PASS | 首次有效观察图 1，语义稳定后图 0；ROI 只截图 1 次并保留源像素。画布独有“中文小字A7B9”通过 query 识别 1/1，DOM 1、截图 1、OCR 1、YOLO 0、caption 0，总体 9841.4 ms，其中 OCR 9208.411 ms |
| 惰性视觉与零 caption | PASS | 独立基础依赖环境完成 DOM/UIA/ROI 降级，parser 构造 0；完整环境独立 OCR 初始化 723.965 ms，独立 YOLO 357.891 ms；OCR 加载 YOLO 0、YOLO 加载 OCR 0；禁止 socket connect 时视觉通过，网络连接 0、caption 请求 0 |
| UIA 超时与清理 | PASS | 故障注入在途阻塞 1、排队 1、第三次 BACKEND_BUSY 1，实际 apartment worker 线程 1；释放阻塞后排队结果 7，shutdown 后线程 0。owner 清理遇阻时记录 pending，provider 返回后统一释放 |
| 完整回归 | PASS | scripts/check.ps1，Node 83 PASS / 0 FAIL / 0 SKIP，1615.5166 ms；Python 283 PASS / 0 FAIL / 1 预期 SKIP / 17 subtests PASS，9.75 s；Host/Client 语法 2/2 PASS |
| 发布检查 | PASS | npm dry-run 174 文件，压缩 27593103 bytes，解包 30523601 bytes；dom/observations/worker 共 3 个新 runtime 文件入包，新增依赖 0 |

实机基础命令为 targets_probe.py <隔离 home> --observations，完整视觉环境增加 --vision。基础环境完整 fixture 耗时 12359 ms、parser 初始化 0；视觉环境完整 fixture 耗时 24140 ms、parser 初始化 1，各 n=1。独立 vision_probe.py 使用本机已有 weights 并禁止网络连接。三份 JSON 证据位于 p3-evidence。

首次图片一致性测试发现 Playwright 默认 caret=hide 会修改 input style，引发 MutationObserver 版本变化。源码确认该行为后改用 caret=initial，使采集过程保持原始光标样式。浏览器目标记录子 frame 的 document/layout 版本，保证内层变化也会使整页旧图失效。整页 OCR 的单样本约 9.2 s 已如实记录，P4 将慢采集接入可查询 operation。

## 14. P4 执行记录

版本 0.4.18，P3 提交 24ba0ca 之后实施。DesktopRuntime.act 使用同一 dispatcher 顺序执行单步及 1–8 步批次，校验观察、目标、引用和帧，并在同步动作后返回观察。SQLite journal 在 stateDir 保存 owner/call 身份、参数摘要、派发边界和逐步结果，完整结果保留 1 天，请求摘要保留 30 天。浏览器与 UIA 操作分别在原线程执行，目标锁先于全局输入锁，条件等待释放全局输入锁。

| P4 退出项 | 状态 | 实测结果 |
| --- | --- | --- |
| 单步、批次与读回 | PASS | 实机原生 4 步表单、DOM 8 步表单、17 次同步操作，原生和浏览器图片点输入各 1/1，内层 frame 动作 1/1；返回体 561–6554 bytes，结果与现场一致 |
| 动作能力 | PASS | 中文 replace/insert、checkbox、选择上海、右键 1、双击 2、hover 1、文件上传 1、frame 拖动 down/up 各 1、滚轮 3 刻度、键盘和关闭标签各通过；未变化的 checkbox 派发 0 |
| 目标与界面变化 | PASS | 旧 DOM 对象替换拒绝 1/1 并带当前观察；原生保存确认保留 1/1，DOM 模态、浏览器确认和页面导航分别在第 0 步后中断，后续输入 0；浏览器确认经 Escape 明确关闭 1/1 |
| 期望与输入锁 | PASS | 文本期望命中采样 1 次；200 ms 期望超时保留已完成步骤 [0]，后续点击 0；两个 owner 的原生批次各保存自身值 1 次；等待中另一目标点击 1 次并完成，stop 后保留已完成步骤 [0] |
| 持久身份与恢复 | PASS | 8 个并发同 call 请求派发 1 次，真实动作重放额外派发 0；实际子进程在派发前和第二步写入后分别终止 2 次，重启恢复 cancelled / unknown 各 1，已完成步骤 [0] 和 uncertain_step=1 保留；过期结果与请求摘要分别按 10 / 100 秒模拟验证 1/1 |
| 取消、超时与并发 | PASS | 实际 apartment 阻塞超时后 worker_running=true，输入锁保持，释放后尾随输入 0、worker_running=false；队列取消不执行回调 1/1；原请求取消撤销执行 1/1，follow 取消不撤销执行 1/1；30 ms 同步预算返回 handle 1/1 |
| 完整回归 | PASS | scripts/check.ps1，Node 83 PASS / 0 FAIL / 0 SKIP，1643.1866 ms；Python 299 PASS / 0 FAIL / 1 预期 SKIP / 28 subtests PASS，9.92 s；Host/Client 语法 2/2 PASS |
| 发布检查 | PASS | npm dry-run 178 文件，压缩 27607899 bytes，解包 30583249 bytes；actions/conditions/journal/operations 4 个新增运行文件全部入包，第三方依赖新增 0，3 个 optional peers 保留 |

实机 fixture 使用隔离 home、本地 HTTP 页面与独立原生窗口。17 次同步操作 p50 199.042 ms、p95 3082.903 ms，最近秩法，n=17，完整 fixture 15328 ms，证据位于 p4-evidence/actions-probe.json。浏览器原生确认框的 3 秒来自 Playwright 点击等待超时后保留 dialog 的路径。无模型调用、parser 初始化和 caption 请求均为 0。

直接运行项目 venv 的可执行文件继承 Low Integrity 标签，前台连接返回 WinError 5。复用本机已安装 Python 加载隔离依赖后获得高完整性运行，未更改任何可执行文件标签。浏览器原生 dialog 使 Playwright 无法继续常规页面采集，观察改为返回保留的消息和类型，并由明确键操作处理。瞬态提示的预先 arm 及六工具框架传输在 P5 实施和验证。

## 15. P5 执行记录

独立等待接入 operation journal，覆盖参数校验、超时证据和同请求重放。新增会话隔离的 JPEG 资源端点，Host 二进制读取使用 4 MiB 上限。act、wait、status、stop 的内核桥接入口已初步接线，尚需与 Host 六工具及取消路径完成端到端验证。完整检查 scripts/check.ps1 实测 Node 84 PASS，Python 302 PASS、1 预期 SKIP、32 subtests PASS。此记录不代表 P5 验收通过。

### 本机续接实测（2026-11-24）

起点 bdae18d，分支 codex/agent-runtime-refactor。用户要求保留的两份本机交接文档已单独提交为 08e1e2e，观察桥接补充提交为 29634d2。开工干净基线 scripts/check.ps1 为 Node 84 PASS，Python 302 PASS、1 预期 SKIP、32 subtests PASS。当前 P5 改动包含六工具 Host、原生附件 renderer、open 初始观察、瞬态监听、取消与后台状态跟踪，版本统一为 0.5.0。阶段闸门仍待指定的双模型验收及最终独立提交。

| P5 验证项 | 状态 | 实测结果 |
| --- | --- | --- |
| 默认六工具发现 | PASS | 已安装的 cua-refactor bundle 为 link 指向本 worktree，插件页显示 0.5.0、1 个组件运行中。新会话模型请求的实际工具轨迹确认 open/observe/act/wait/status/stop 共 6 个 doraemon 工具 |
| 独立 status | PASS | 新会话仅调用一次 status({})，success=true、version=0.5.0、build=778b47b8965e6bc7；windows/capture/input 初始化，browser_initialized/uia_running/transient_uia_running 全 false，input_busy=false，operations=[]。该回合 2 个模型请求、1 个工具、UI 总时长 8 s、聚合 23.7K token |
| 瞬态提示与清理 | PASS | actions_probe 两次完整复验均成功。第一轮原生 600 ms 提示采样 3 次、派发至命中 340.003 ms，浏览器 120 ms 提示采样 4 次、命中 25 ms。第二轮对应为 3 次/383.921 ms 和 4 次/26 ms。act/status 捕获证据相同，取消清理 1/1，parser 初始化 0、caption 0 |
| 原生和浏览器操作 | PASS | 完整实机 probe 两轮各 19 个同步操作，4 步原生表单、8 步 DOM 表单、两 owner 各保存自身值 1 次，open 初始观察与同请求重放通过，额外导航 0。两轮整体耗时 31719/35610 ms，证据 actions-probe.json |
| Host 原生图片补充闭环 | PASS | 当前可用 DeepSeek 官方路由、High，在新 DSH 会话完成 open→status→observe，3 个工具调用、4 个模型请求，图像独有随机字符 CMSULQDN 8/8 正确，1/1。DOM text 为空、elements=[]，OCR/YOLO/caption 均 0。JPEG 1366×854、28476 bytes，observe DOM 33.062 ms、截图 147.683 ms、总体 214.489 ms，UI 回合 17 s、聚合 54.4K token。此项是附加通路验证 |
| 指定双模型图片闭环 | 环境阻塞 | cua-refactor UI 当前只列出 DeepSeek 账号和 DeepSeek 官方提供方下的 Flash/Pro。wb/cn:deepseek-v4.1-flash 与 wb/cn:hy4std 可选项均缺失，本阶段两条指定路由各执行 0 次。已请求用户配置其本机实际路由 |
| 包与 peers | PASS | npm pack --dry-run 为 186 文件、压缩 112136150 bytes、解包 127274796 bytes，index/runtime-host/legacy-host/actions/conditions/worker 全部入包。三个 SDK peers 保持 optional。包内现有 YOLO 80.4 MB 权重及历史静态文件清理归 P6 |

独立内核采用已验证的 create_app 工厂在 8767 启动，进程 49632，更新源码后重启为 45112，source_root 指向本 worktree，home/state 在本项目 .runtime 内。DSH 由本次进程环境注入临时 bridge token 后启动 cua-refactor，以 health 能力握手接管该内核。该验证方式复用了 P1 的工厂入口，CLI 全局单实例启动路径仍需在发行验收中覆盖。全局 elevated task 与共享 kernel-boot.json 的冲突尚未通过正常启动路径复验。

第一次实机复验在双 owner 原生并发处失败，第二请求返回 BACKEND_BUSY。定位到 ApartmentWorker 线程启动前的队列容量竞争，改为同时限定在途与排队总数为 2，并增加确定性延迟 COM 初始化回归。修改后的两轮实机并发均通过。

首次 DSH 表单流程因模型遗漏 input.mode 被 422 拒绝。旧 Host 返回泛化错误，模型随后读取旧工作区源码并偏离验收步骤，该回合已停止，未计为成功。已给 mode 增加 replace/insert 枚举和必填说明，桥接返回至多 4096 bytes 内的具体校验原因，新增正常及超长错误回归。修正后的流程另行复验。

修正后在独立新 DSH 会话复验两条流程。open→act→act 完成 1/1，open 为 op_ce4c292647b2455cb5a9e7508380e175，input 为 op_a9c0b1b52d2c47f8b12b8eb4305e7e47，Save 为 op_996db1ff8be74d2a8afe3f6b74a6ef6d，现场读回 Saved P5-flow。第二条初次 wait 因模型把 timeout_ms 放在 condition 内被 SDK 拒绝，派发 0；说明中补充顶层 timeout_ms 位置后复验 observe→act→wait 完成 1/1，act 为 op_a71240cf1d8e400cb36e85e52bfc2fa8，wait 为 op_ae3efe86a12644e6b3dd6af36221314a，expectation=met、samples=1、dispatch=none、worker_running=false。复验时页面已含目标文本，此数值只验证条件满足路径，实际等待和取消由实机 probe 覆盖。首次新会话回合 7 个模型步骤、UI 22 s、107K 聚合 token；第二回合 4 个模型请求、3 个工具调用、UI 13 s、84.4K 聚合 token。费用未知，当前两条指定 wb 路由的成本与对照仍未测量。

提交前最终 scripts/check.ps1 结果为 Node 89 PASS、0 FAIL、0 SKIP、6577.5482 ms；Python 308 PASS、0 FAIL、1 预期 SKIP、37 subtests PASS、22.33 s，Host/Client 语法检查通过。该轮包含六工具公开描述、companion 对 observe/wait/act 的状态映射与回归，以及覆盖实际 runtime 模块的 build 指纹。Client chips 与原生附件插槽的现有兼容测试通过。

只读复核本机模型目录后，cua-refactor 仍缺少指定的两条路由。desktop 中可见 workbuddy/global:deepseek-v4.1-flash 和 hy4/hy4、hy4/hy4-flash，路由标识和配置归属均需用户确认与配置到独立 profile。本次提交保存已完成的 P5 实现和实测证据，P5 的双模型图片退出项继续待验，P6/P7 按阶段闸门继续待实施。

### 本机阶段闸门（2026-11-25）

从干净的 bdae18d 获取 origin 后，快进合入 08e1e2e、29634d2 和 6dd5563，保留公司提交及原有文件。恢复后的基线为 Node 89 PASS、Python 308 PASS、1 预期 SKIP、37 subtests PASS。本机 DSH 已有用户确认的两个 wb 路由，前述公司环境缺少路由的阻塞现已解除。

正常自动启动首次复现 status HTTP 500，kernel.err.log 为 sqlite3 unable to open database file。定位为 kernel.js 优先选择项目测试 .venv，其可执行文件继承 Low Integrity 标签，无法写入默认 stateDir。改为显式 pythonPath 优先、bootstrap 安装解释器次之、项目 venv 后备，增加两个解释器同时存在的回归。实测正常启动采用 bootstrap runtime，版本 0.5.1。解释器和系统的权限标签保持原值。

| P5 退出项 | 状态 | 实测结果 |
| --- | --- | --- |
| 正常安装入口与新会话 | PASS | DSH 插件管理页既有 link 安装指向本目录，经管理页停用/启用重载；新会话 status 1/1 成功，version=0.5.1、初始 browser/UIA/transient 均 false、operations=[]、input_busy=false；模型列出默认工具 6/6 |
| DeepSeek 原生图片 | PASS | wb/cn:deepseek-v4.1-flash，独立新会话 1/1，图片独有 UFHM8K8V 8/8 正确；open 1 次直接带图，模型请求 2 次，UI 回合 10 s、25.7K 聚合 token；DOM 1、截图 1、OCR/YOLO/caption 0，DOM 12.750 ms、截图 54.795 ms、观察 78.359 ms |
| Hy4 原生图片 | PASS | wb/cn:hy4std，独立新会话 1/1，图片独有 BZCPAMNX 8/8 正确；status+open 共 2 工具，模型请求 3 次，UI 回合 20 s、36.1K 聚合 token；DOM 1、截图 1、OCR/YOLO/caption 0，DOM 11.676 ms、截图 92.109 ms、观察 112.017 ms |
| 图片实际请求用量 | PASS | 两条路由均从 DSH 请求详情确认 provider/model。DeepSeek 图片后请求输入 13461、缓存 11776、输出 374 token，总时长 4.18 s、TTFT 2.16 s；Hy4 输入 13200、缓存 10944、输出 556 token，总时长 11.6 s、TTFT 2.95 s。两张实际图片均 1366×854，每张 1166564 pixels，DOM text 长度 0、元素 0 |
| 最终代码与 renderer 复验 | PASS | 重载最终 build=58220ee4f3e259ff、epoch=d51ec01a7fdf469396e55c81867c150e，新 Hy4 会话 status→open→observe→stop 共 4 工具、5 模型请求，图片字符再次 8/8；observe 直接 completed，stop operations=[]，UI 43 s、67.9K 聚合 token |
| 慢观察与首响应丢失 | PASS | 30 ms 观察预算返回 running handle 1/1，同 request 重试采集总数 1；stop 显示 cancelling，阻塞返回后 cancelled、worker_running=false，迟到结果替换 0。Host 注入动作响应丢失后派发 1、按相同 owner/request_key 查询 1、重复派发 0；422 和调用取消各额外恢复查询 0 |
| 本机真实动作与瞬态 | PASS | actions_probe 完整成功，同步操作 19、总耗时 18344 ms，工具 p50 222.490 ms、p95 3086.680 ms，n=19 最近秩；原生 600 ms 提示 2 次采样、命中 124.756 ms，浏览器 120 ms 提示 4 次采样、命中 13 ms；取消清理 1/1、重复派发 0、parser 初始化 0、caption 0 |
| 完整检查 | PASS | scripts/check.ps1，Node 91 PASS、0 FAIL、0 SKIP、1747.9128 ms；Python 310 PASS、0 FAIL、1 预期 SKIP、43 subtests PASS、12.04 s；Host/Client 语法 2/2，git diff --check 通过 |
| 本机发布清单 | PASS | npm pack --dry-run 为 181 文件、压缩 27619700 bytes、解包 30625815 bytes，runtime-host 与所有新增 kernel runtime 文件入包，3 个 optional SDK peers 保留；公司快照中的 80 MB YOLO 文件在本 checkout 缺席，体积按本机实际记录 |

图片 smoke 证据为 p5-evidence/local-model-images.json，完整操作证据为 p5-evidence/local-actions-probe.json。图片卡片在源 1000×650 图片右下区域，映射到 1440×900 浏览器视口后处于水平中央偏下，两模型的位置描述符合实际输出。两路 UI 费用显示分别为 ¥0.00、¥0.02，属于取整显示。精确费用、图片独立计价和资源字节量未知。图片后请求的 token 含整段请求上下文。每模型图片 smoke 样本量为 1，P7 的多次旧新对照仍待执行。

常规 Python 跳过项仍为受 DSH_LIVE_DIALOG_TEST 开关控制的真实窗口测试，P0 已单独执行通过，本轮原生模态能力另经 actions_probe 验证。存在的 invalid escape docstring warning 为历史桥接说明字符串，测试失败数为 0。P5 的所有退出项现为 PASS。

## 16. P6 执行记录

从 P5 独立提交 d1115ea 和干净工作树开始，版本为 0.5.2。Host 配置抽为 host-config.js，历史注册移到 standalone/legacy-host.js。默认桥接移除旧 tasks、windows、web、KB、decide 等路由，历史适配器保留在源码 standalone_bridge.py。BrowserRuntime 持有持久 context，复用原单线程 worker，浏览器启动辅助与 DOM snapshot 分别集中到 browser_support.py 和 dom.py。共享 Win32/UIA、输入、DPI、取消租约和部分结果实现继续由 runtime 使用。

| P6 退出项 | 状态 | 实测结果 |
| --- | --- | --- |
| 默认导入与依赖 | PASS | 新建 C:/Users/Admin/AppData/Local/dsh-desktop-p6-test/venv，bootstrap 联网安装基础分发包 38 个，源码 import 1/1；TestClient 另装 3 个测试依赖。禁止 16 个可选依赖或历史模块导入时，实际 HTTP DOM/UIA 流程成功，禁用集合加载 0、parser 初始化 0、caption 0；旧路由缺席 5/5 |
| 六工具与历史边界 | PASS | 解包后的 index.apply 注册 6/6，卸载 effect 1/1；默认 Host 对历史 Host 导入 0，默认桥接对 standalone_bridge/web_tools 导入 0。旧路由回归和旧 Host 测试保留在源码边界，共享 snapshot、浏览器启动辅助及浏览器 worker 各 1 个实现 |
| 干净 runtime 操作 | PASS | 新安装解释器经真实统一 HTTP open/observe 读取原生 JPEG 480×180、UIA 7 控件、本地 DOM 1 命中，5328 ms；companion 启动/关闭 2 轮、进程退出与 overlay 线程释放 2/2，AppControllerRef 清理 1/1 |
| 真实动作回归 | PASS | 基础依赖新环境执行 actions_probe，19 次同步操作、4 步原生表单、8 步 DOM 表单、附件 1、两 owner 正确输入 2/2；总耗时 15141 ms，p50 165.856 ms、p95 3073.746 ms，最近秩 n=19；原生提示命中 122.550 ms、浏览器 6 ms，取消清理 1/1、重放额外派发 0、caption 0 |
| profile 保留与恢复 | PASS | targets_probe 检查 profile 占用诊断 1/1、Cookie 恢复 1/1、站点设置恢复 1/1、同 URL 标签 3、嵌套 frame 3、错实例点击 0、外接浏览器释放及卸载后存活 1/1，基础 fixture 12781 ms。另将已关闭 fixture profile 完整复制到 home 兼容旧路径，复制恢复 1/1、原目录回退恢复 1/1，Cookie/站点设置各检查 2/2，原目录保留 1/1，18515 ms |
| 可选视觉与权重升级 | PASS | bootstrap -Vision 成功，既有 icon_detect/model.onnx 复制前后各 80428823 bytes，SHA256 一致 1/1。禁止 socket connect 的独立视觉 probe 识别中文小字A7B9，OCR 1579.688 ms、YOLO 351.450 ms、检测 4；OCR 加载 YOLO 0、YOLO 加载 OCR 0、网络 0、caption 0 |
| 发布与只读源码 | PASS | npm pack 实包 104 文件、Python 模块 41，压缩 2593842 bytes、解包 3232856 bytes，3 个 SDK peers 保持 optional。解包直接运行原生/UIA/DOM/companion 成功。隔离包目录加显式写入拒绝 ACL，写入探针拒绝 1/1，真实观察与 companion 2 轮成功，journal/profile 写入隔离 home/state；随后恢复测试目录 ACL |
| 安装升级与内核身份 | PASS | 0.5.1 resident kernel 经 health PID 接管 1/1，核对 pid 后停止 1/1。DSH Plugins 管理页对既有本地 link 安装停用/启用，显示 0.5.2、运行组件 1。新 DSH 会话六工具发现 6/6，status success=true、operations=[]，version=0.5.2、build=0bc6fd65df535032、epoch=41ccd0ff47af40f6afb4a106434422be；旧协议拒绝及 PID 变更回归通过 |
| 升级后图片 smoke | PASS | wb/cn:hy4std，新会话 status→open→observe→stop，4 工具、5 模型请求；图像独有 BZCPAMNX 8/8，DOM text 长度 0、elements 0；全图 1366×854，模型自行追加 1 次 ROI，stop operations=[]。UI 75 s、69.3K 聚合 token、取整费用 ¥0.03，精确费用未知，n=1 |
| 文档与数据去向 | PASS | README 中英文、QUICKSTART、MIGRATION-0.5、data-boundary、commander-mode 共 6 份使用文档更新；迁移表覆盖自主 Agent/旧 status-stop/desktop/web/decide/KB/工作台/IM/WeCom/cron。源码核对 Hermes __main__ 设置 HERMES_HOME 为工作台 home，SessionDB 默认 state.db；知识 knowledge、cron/jobs.json 与 cron/output 原位置保留 |
| 完整检查 | PASS | scripts/check.ps1，Node 91 PASS、0 FAIL、0 SKIP、932.9777 ms；Python 311 PASS、0 FAIL、1 预期 SKIP、43 subtests PASS、11.58 s；Host/Client 语法 2/2，git diff --check 通过 |

证据位于 p6-evidence。基础安装命令使用 scripts/bootstrap.ps1 -InstallDir C:/Users/Admin/AppData/Local/dsh-desktop-p6-test，后续 -Vision -WeightsDir 使用原有 runtime weights。解包验证通过 PYTHONPATH 指向实际 npm 包 kernel，使用新安装解释器运行 test/runtime_probe.py <隔离 home> --live --companion。该 probe 阻止可选包及历史模块导入，验证六工具对应 HTTP 入口、鉴权、退役路由、图片资源和生命周期。只读测试仅对本次解包目录施加和恢复 ACL，用户源目录与数据目录保留原值。

实包验证先发现缺失 worker.py 和 whale_commander 资源，已补白名单及相应用例。资源加载失败还暴露 CursorOverlay._canvas 在异常清理前未初始化，改为构造时初始化并复验。隔离旧 venv 的 pywin32 DLL 在子进程缺失，fixture 按 DSH_TEST_PACKAGES 显式加载测试依赖后恢复。首次 companion probe 与 resident kernel 同时注册热键时记录 WinError 1409，仍完成两轮资源清理；停止本轮旧 kernel 后的只读复验无该冲突。新 companion 可通过 DSH_DESKTOP_COMPANION_UI 使用可写目录中的构建产物。

主包将独立工作台等历史模块排除，源码恢复入口和测试继续保留，既有用户数据移动或删除次数为 0。Cordis optional peer 范围调整为 >=4.0.5 <5.0.0，依据本机 4.1.0 的实际 SDK 测试。常规跳过项沿用显式真实窗口开关，实机模态由 actions_probe 覆盖。P6 所有退出项 PASS，P7 的双模型旧新多次任务对照待独立实施。

## 17. P7 执行记录

2026-11-25 从干净的 5382099 开始。直连 GitHub 两次超时后，通过本机已有 http://127.0.0.1:10808 代理执行一次 git fetch，HEAD 相对 FETCH_HEAD 为 ahead 2 / behind 0，公司远端仍为 6dd5563，无新提交需要合入。开工基线 scripts/check.ps1 为 Node 91 PASS、0 FAIL、0 SKIP、978.5466 ms，Python 311 PASS、1 预期 SKIP、43 subtests PASS、11.46 s。

### 真实入口与本阶段修复

使用 DSH 安装内官方 CLI 的 runDesktopCli 导出入口，以 plugin add link 安装本目录到 headless 测试 profile。launch overlay 引用 DSH credentials 服务中的 WB_API_KEY，路由及模型参数取自本机 wb 设置，baseURL 为本机 7863/v1。bridge token 只从 kernel-state 读入子进程环境，未写入验收数据。首次缺少 apiKeyEnv 和 bridge token 的两次接入失败日志均保留在 .runtime，成功接入的新会话发现工具 6/6，并实际调用 status 返回 0.5.2。后续试验逐次新建 DSH 会话。

P6 迁出旧路由后，companion-state 仍调用 /windows/{id}/unlock。新增 /scope/windows/{id}/release 并复用原 unlock_window，历史桥接保留别名。真实 TestClient 验证有效鉴权、错误 token、浏览器 Origin、owner 格式和重复释放，移除 A 后 B 保持选中。Node 同步测试改用新路径。

Hy4 首次同 URL 标签任务在 180 秒超时，fixture 点击 0 次。定位到 target_directory 只刷新既有对象，漏收录页面通过 window.open 创建的标签。新增按 opener 会话归属发现 popup，目录及子 frame 保留 tab_id。确定性测试覆盖另一 owner 不收录该 popup。真实 targets_probe 验证同 URL 两个标签被发现、其他 owner 新收录 0，随后通过原有 profile 占用、重启恢复、跨 owner 释放及外接浏览器保留检查，总计 6328 ms，parser 初始化 0。修复后 DeepSeek frame smoke 6 工具 / 34677.535 ms、Hy4 8 工具 / 162833.657 ms，业务点击均为指定 inner 唯一 1 次。

版本为 0.5.3。resident 内核经 health PID 核验停止并通过现有自动启动入口恢复，status 返回 version=0.5.3。新增开发验收入口 scripts/benchmark-agent-runtime.mjs、benchmark-transport.mjs、dsh-benchmark-entry.mjs 和 reconcile-benchmark.py。fixture 校验字段值、提交次数、上传 SHA256 与 tab/frame 归属。provider 转发只记录请求形状、图像媒体类型/字节/哈希/尺寸和耗时，凭据与图片原文保留在内存。完整会话日志通过 zstandard 解码核对，开发环境安装 zstandard 0.25.0，发行依赖新增 0。

### 当前执行情况

首轮 DeepSeek 的 form/upload/frames/toast/cancellation 各 1 次均达到业务条件，端到端分别为 22240.490 / 13411.846 / 121405.207 / 14210.288 / 29872.635 ms，工具分别为 5 / 3 / 19 / 3 / 6。此轮使用 0.5.2，属于 smoke。首轮 Hy4 在 0.5.3 上 form/upload/toast/cancellation 各 1 次通过，分别为 55080.568 / 60214.291 / 69865.457 / 52066.520 ms，工具为 4 / 6 / 3 / 6。frames 181119.454 ms 超时，notepad 180201.937 ms 超时，均保留失败记录。

DeepSeek 补充 smoke 中 notepad 文件正文核对通过，116079.526 ms、23 工具。原提示要求额外关闭文件标签，模型重复核查菜单并打开最近日志标签，业务保存之后的动作明显过长。正式提示收敛为保存并读回未修改状态，明确只操作本次文件。两会话并发 smoke 31391.037 ms、11 工具通过，A 取消之后无输入事件，B 在确认 A 取消后保存自身值 1 次。此项包含两个独立真实 DSH 会话。

本阶段仍在执行。实机仅有 1 屏、96×96 DPI，真实异 DPI 跨屏及负原点屏幕缺少硬件条件，现有 96/144 DPI 与负原点确定性测试继续保留。P7 退出条件尚未全部 PASS。

### 正式重复与继续开发核对

继续开发时再次 fetch origin，HEAD 相对 FETCH_HEAD 为 ahead 3 / behind 0，远端仍为 6dd5563，已有公司提交完整保留。恢复检查为 Node 93 PASS、0 FAIL、0 SKIP、945.317 ms，Python 314 PASS、1 预期 SKIP、43 subtests PASS、11.82 s。0.4.14 对照源码保存在忽略目录 .runtime/p7-legacy，index.js 和 dsh_bridge.py 的 Git blob 分别为 4d6f6a64fb8d26a88c08a8d6b1b2ab44920fd7a2 和 8526f6007e5421576521c1eb72f5f58b9e2aa1cb，均与 8bea142 的对象一致。旧版通过官方 CLI 的 plugin add link 安装到新建 cua-p7-legacy profile。

正式任务集共 7 类，每类独立运行 3 次。并发任务包含取消方 A 和继续保存的 B 两个真实 DSH 会话，持久化任务包含退出前和重新打开后的两个新会话。单轮超时固定 300000 ms。fixture ID、文件和页面均逐次新建，模型、maxTokens、系统与工具 schema 哈希、实际图片请求、逐工具到达时间和 usage 保留在 JSON 证据。工具延迟使用 CLI 事件到达时间，p50/p95 采用最近秩法。图片次数包含上下文中再次发送的同一图片。

DeepSeek 0.5.3 的正式结果已完成 21 次，通过 20 次。三次的逐任务汇总如下，完整数值位于 p7-evidence/deepseek-formal.json。

| 任务 | 完成 | 成功端到端 p50 / p95 ms | 工具 p50 / p95 ms | 工具样本 | 模型请求 | 送入 provider 的图片 | 聚合 totalTokens |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 表单 | 3/3 | 15611.952 / 20094.783 | 205.231 / 417.612 | 13 | 16 | 21 | 217929 |
| 附件 | 3/3 | 15303.864 / 19318.108 | 198.608 / 972.666 | 12 | 15 | 17 | 187014 |
| 同 URL 标签与嵌套 frame | 3/3 | 32489.967 / 36285.046 | 172.961 / 1759.085 | 21 | 24 | 35 | 367029 |
| 120 ms 短提示 | 3/3 | 13459.391 / 14072.552 | 208.043 / 224.788 | 9 | 12 | 18 | 152092 |
| 记事本保存 | 3/3 | 74955.945 / 89638.616 | 447.623 / 2463.049 | 39 | 41 | 121 | 1625343 |
| 双会话取消 | 2/3 | 35829.016 / 43566.269 | 1625.718 / 10014.893 | 50 | 56 | 84 | 813667 |
| 浏览器进程重启与登录态 | 3/3 | 54148.963 / 58571.568 | 227.154 / 2401.930 | 32 | 38 | 59 | 612608 |

并发第 2 次失败完整保留，耗时 152370.629 ms。B 模型收到的 URL 包含 fixture ID 尾部 b，实际 open 参数漏写该字符，打开了 A fixture 下的 cancellation_b 页面。B 的输入因此记录到 A fixture，该页面读取了另一份 gate 状态并持续等待。A 自身的等待已经 cancelled，worker_running=false。此失败属于模型打开错误地址，未计为成功，也未以重跑覆盖。其余两次均在 A 取消之后由 B 保存一次，A 取消后的输入事件为 0。

三次持久化均验证旧 browser PID 退出、新 PID 使用同一 profile、Cookie 与 localStorage 恢复，随后分别读回模拟登录失效和验证码的用户介入提示。该恢复任务每次包含两个新 DSH 会话。源 profile 与用户数据保留。

默认 caption 合计 0。少数 query 使用自然语言句子或 UIA 控件类别名，实际 query 为大小写无关的子串匹配，因此产生额外 OCR/YOLO 和补查。记事本三次共 39 工具、121 次上下文图片发送，作为后续预算评估的实测依据。完整会话对账补齐了 CLI 截断结果中的失败、帧和 token 信息。精确费用、图片独立 token 与独立计费仍未知。

### 续接检查与对照适配

本轮再次经本机代理 fetch，FETCH_HEAD 为 6dd5563，HEAD 为 3a526e4，ahead 3 / behind 0。该 checkout 的 fetch 配置只更新 FETCH_HEAD，因此用 HEAD...FETCH_HEAD 核对远端。工作树中已有基准脚本与文档改动均保留。修改前完整检查为 Node 93 PASS、0 FAIL、0 SKIP、903.1658 ms，Python 314 PASS、1 预期 SKIP、43 subtests PASS、11.40 s。

旧版取消证据按原协议判定。先确认同 task_id 的 stop_requested=true，再确认 status=finished、result.status=stopped、result.reason=interrupted。单独的停止请求、另一个 task 的结束、正常完成均不算取消通过。新版要求 cancelled 且 worker_running 不是 true。针对两种实际协议的回归为 1 PASS，共 7 个断言。旧版缺少统一 observation trace 时后端统计记为 null。超时尝试仅保留已完成请求的 usage_observed，完整 usage 记为 null。

Hy4 0.5.3 正式尝试为 17/21 通过，详细数据位于 p7-evidence/hy4-formal.json。

| 任务 | 完成 | 成功端到端 p50 / p95 ms | 工具 p50 / p95 ms | 工具样本 | 模型请求 | provider 图片 | 完整 totalTokens |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 表单 | 3/3 | 60876.821 / 71103.720 | 292.661 / 1394.067 | 15 | 18 | 37 | 280815 |
| 附件 | 3/3 | 33007.296 / 35573.256 | 240.261 / 277.867 | 9 | 12 | 18 | 156954 |
| 同 URL 标签与嵌套 frame | 3/3 | 73422.150 / 77775.675 | 213.203 / 372.385 | 23 | 25 | 60 | 422733 |
| 120 ms 短提示 | 3/3 | 55409.307 / 85029.142 | 241.451 / 308.487 | 9 | 12 | 18 | 159522 |
| 记事本保存 | 0/3 | 无成功样本 | 235.944 / 1322.923 | 57 | 60 | 296 | 未知，已完成请求累计 3042542 |
| 双会话取消 | 3/3 | 133960.531 / 198807.643 | 1554.312 / 6973.853 | 42 | 47 | 105 | 770763 |
| 浏览器进程重启与登录态 | 2/3 | 214583.459 / 250359.959 | 136.522 / 1027.985 | 62 | 63 | 145 | 未知，已完成请求累计 1167022 |

Hy4 失败耗时合计 1286176.403 ms。记事本三次分别在 300256.210、301111.338、300229.008 ms 超时，第 2 次文件已正确保存，第 1/3 次文件正文未达标。轨迹出现 keys=["ctrl+o"] 校验失败和自然语言 query 无匹配后反复补查。持久化第 2 次第一会话 300 秒超时，第二恢复会话完成，合计 384579.847 ms，原 browser PID 19708 退出，新 PID 23084 使用同 profile，Cookie、站点设置和模拟登录提示均达标。第一会话在关闭成功后仍重复核查，保留为 FAIL。其余两次进程退出和重开验证通过。21 次 caption 总数为 0。

针对实测参数错误，0.5.4 补充 args 直接打开文件、query 字面子串、组合键拆分、正文和输入值读回说明，并在 state schema 列出内核已有允许值。键名校验错误提供 ["CTRL", "O"] 示例，支持的键名规则沿用原实现。Node 真实 SDK pipeline 验证合法 closed 状态可派发，非法 browser_closed 在桥接前拒绝，图片附件仍保持原生输出。修正后完整检查为 Node 94 PASS、0 FAIL、0 SKIP、962.8456 ms，Python 314 PASS、1 预期 SKIP、43 subtests PASS、11.22 s，追加的 pipeline 断言单独 6/6 通过。

旧内核首次启动等待报告超时，实际 HTTP 服务已就绪。核对 Win32 进程后确认 venv launcher PID 23012 启动工作进程 PID 2944，health 报告后者。确认父子关系、原版源码路径和 protocol=1 后校正独立测试 home 的状态 PID，继续旧版 smoke。health.execution_enabled=false，原版因缺少 DEEPSEEK_API_KEY 拒绝独立 Agent 委托，主 DSH 模型和直接工具正常可用，该限制纳入旧版对照。

旧版 smoke 各任务各 1 次。DeepSeek 表单、附件、标签/frame、记事本分别 PASS，耗时 43289.130 / 58338.081 / 66414.353 / 56011.189 ms。120 ms 提示在 watch 启动后仅触发 1 次，采样 431 次、保留帧 0、OCR 命中 0，FAIL，69774.295 ms。并发 FAIL，301137.543 ms、77 工具，直接 web_wait 同步等待结束才返回 action_id，独立 Agent 委托因缺 Key 返回 503，取消证据缺失。持久化 smoke 69665.317 ms，Cookie 与站点设置恢复，但 runner 在第一个工具返回时过早保存空进程列表，process_exit=false、same_profile=false，按原记录 FAIL。修正后 runner 每个工具返回时累积本次专用 profile 的 PID，正式对照使用修正版。

旧版 Hy4 smoke 为 1/7 PASS。表单 PASS，206152.767 ms、16 工具。附件 300252.388 ms 超时，上传 SHA256 已达标；frame 301128.656 ms 超时且业务未达标；toast 300236.858 ms 超时且文字证据缺失；记事本 300234.117 ms 超时且文件未达标；并发 300236.318 ms 超时且取消证据缺失。持久化两个会话均超时，合计 603789.669 ms，原 PID 20872 退出、新 PID 21656 使用同 profile、恢复和模拟登录状态均达标，仍保留 FAIL。两份 smoke 已完整对账为 7/7 会话证据，脱敏报告位于 p7-evidence/legacy-deepseek-smoke.json 和 legacy-hy4-smoke.json。

0.5.4 最后一次完整检查为 Node 94 PASS、0 FAIL、0 SKIP、1133.2008 ms，Python 314 PASS、1 预期 SKIP、43 subtests PASS、11.88 s。npm pack dry-run 为 104 文件、压缩 2594813 bytes、解包 3236082 bytes，基准及证据入包 0。基准拒绝复用非空输出目录的实测为退出码 1，既有尝试记录保留。

### 旧版正式对照

DeepSeek 0.4.14 正式对照完成 21 次，14 次 PASS。每类均为 3 次，原始失败继续保留，脱敏数值位于 p7-evidence/legacy-deepseek-formal.json。

| 任务 | 完成 | 成功端到端 p50 / p95 ms | 总工具 | 模型请求 | provider 图片 |
| --- | --- | --- | --- | --- | --- |
| 表单 | 3/3 | 47796.426 / 52499.466 | 55 | 54 | 6 |
| 附件 | 3/3 | 54025.729 / 55899.193 | 54 | 54 | 21 |
| 同 URL 标签与嵌套 frame | 3/3 | 88461.220 / 107850.103 | 56 | 56 | 34 |
| 120 ms 短提示 | 0/3 | 无成功样本 | 71 | 73 | 15 |
| 记事本保存 | 3/3 | 106253.538 / 205483.596 | 112 | 112 | 26 |
| 双会话取消 | 0/3 | 无成功样本 | 229 | 222 | 27 |
| 浏览器进程重启与登录态 | 2/3 | 79998.489 / 101652.525 | 93 | 90 | 17 |

旧版持久化第 1 次 fixture 状态恢复和模拟登录提示均通过，旧 PID 15252 已退出。恢复会话在结束前调用 cleanup，runner 结束时读取 reopened=[]，无法证实新 PID 与 profile，因此保留 FAIL。第 2/3 次通过。runner 已补充恢复会话每个工具返回后的进程采集，随后独立复验该任务，原 3 次数据继续保留。旧版每观察的后端计数缺少统一 trace，记为 null。旧 renderer 返回本地图片路径，个别会话调用 DSH read_image 读取同一工具产生的图片，provider 图片据实际请求统计。

Hy4 0.4.14 正式对照完成 21 次，8 次 PASS，脱敏数值位于 p7-evidence/legacy-hy4-formal.json。

| 任务 | 完成 | 成功端到端 p50 / p95 ms | 总工具 | 模型请求 | provider 图片 |
| --- | --- | --- | --- | --- | --- |
| 表单 | 3/3 | 131708.739 / 132010.904 | 67 | 65 | 0 |
| 附件 | 3/3 | 139678.731 / 249072.966 | 77 | 75 | 17 |
| 同 URL 标签与嵌套 frame | 1/3 | 241331.494 / 241331.494 | 72 | 66 | 29 |
| 120 ms 短提示 | 0/3 | 无成功样本 | 83 | 61 | 30 |
| 记事本保存 | 0/3 | 无成功样本 | 104 | 102 | 89 |
| 双会话取消 | 0/3 | 无成功样本 | 149 | 140 | 0 |
| 浏览器进程重启与登录态 | 1/3 | 362737.081 / 362737.081 | 164 | 139 | 0 |

Hy4 旧版记事本三次均超过 300 秒。持久化三次业务恢复均达标，第 1/3 次第一会话超时，整体分别 450133.181 / 526445.238 ms，第 2 次两个会话合计 362737.081 ms 成功。三个轮次均观测到专用 profile 的旧 PID 退出和新的 PID。全部正式 84 次原始尝试现已完成，其中新版 DeepSeek 20/21、Hy4 17/21，旧版 DeepSeek 14/21、Hy4 8/21。旧版存在缺独立 Agent Key、历史 renderer 需 read_image、PID 采样不足的记录，对照局限按上述具体任务保留。修复后复验将单独记录版本及样本。

### 正式对照汇总与图像开销

下表汇总原始 84 次正式尝试。新版正式样本版本为 0.5.3，修复版本 0.5.4 的受影响任务另表记录。每组 7 类任务、每类 3 次，原始失败均计入工具、请求、图片和耗时总量。

| 路由与版本 | 任务完成 | 工具总数 | 模型请求 | provider 图片次数 | 全部耗时 ms | 失败耗时 ms | 已观测 totalTokens |
| --- | --- | --- | --- | --- | --- | --- | --- |
| wb/cn:deepseek-v4.1-flash，0.4.14 | 14/21 | 670 | 661 | 146 | 2544130.260 | 1383937.367 | 20820390，完整 usage 19/21 |
| wb/cn:deepseek-v4.1-flash，0.5.3 | 20/21 | 176 | 202 | 355 | 849611.235 | 152370.629 | 3975682，完整 usage 21/21 |
| wb/cn:hy4std，0.4.14 | 8/21 | 716 | 648 | 165 | 5776683.552 | 4283086.690 | 17008657，完整 usage 8/21 |
| wb/cn:hy4std，0.5.3 | 17/21 | 217 | 237 | 679 | 2854611.321 | 1286176.403 | 6000351，完整 usage 17/21 |

totalTokens 来自实际 provider usage，包含提供方报告的缓存量。usage 不完整组的数值仅累计已结束请求，缺失请求的消耗未知。精确金额、图片独立 token、图片单独计费和缓存价格均未知，本机 wb 路由未提供这些字段。各任务成功样本的端到端和工具 p50/p95 见前表与对应 JSON。

新版 DeepSeek 的 355 次图像发送共 14038692 bytes、383161052 pixels，Hy4 的 679 次共 23277245 bytes、674199628 pixels。相同图片随上下文再次发送时重复计数。实际尺寸分别覆盖 7 和 10 种，完整窗口最长边 1366，局部最小 68×41。旧 renderer 部分任务仅传图片路径，旧版图片数受模型是否调用 read_image 影响，原生图片投递增加的上下文开销保留在记录中。

从完整会话结果按 observation_id 去重，DeepSeek 112 份观察、Hy4 148 份观察，DOM/UIA/capture/OCR/YOLO 在同一观察中调用超过一次的计数各为 0，caption 合计 0。两模型分别实际发起 10 和 3 次含 frame_id 图片点的 act，图片帧和坐标继续由 runtime 验证。DOM 的 dom_frames 计数包含嵌套 frame 的独立读取。

### 固定运行参数

保留已经过正式任务验证的默认值。全图最长边 1366、单图 JPEG 上限 4 MiB、元素页 80 项、正文页 6000 字、元素 JSON 上限 45000 bytes、批次上限 8 步。ROI 保留源分辨率。首次目标和布局变化附图，语义稳定动作使用轻量读回。普通表单、附件、frame 和 120 ms 提示两模型各 3/3，通过样本支持当前预算。记事本的额外观察和图片发送已定位到 query 误用、快捷键参数与重复核查，0.5.4 先修正文义和允许值说明。

open/observe/act/wait 同步预算 5000 ms，wait/expect 默认 5000 ms、范围 100–30000 ms，status follow 上限 10000 ms，瞬态采样间隔 100 ms。正式尝试每会话上限 300000 ms。两模型请求的 temperature 未显式覆盖，使用 DSH 路由默认，实际请求记录为 null。DeepSeek maxTokens 为 393216、Hy4 为 64000，均沿用本机配置。每次请求保存实际系统提示和工具 schema 的 SHA256，工具 schema 按对应版本记录。

### 续接检查与安装复核

再次经本机代理 fetch origin，并用 ls-remote 核对全部远端分支。开发分支仍为 6dd5563137cecd264b84609b6732af6f60f67310，main 为方案起点 8bea14227945420771f20b348ce777fad0d9f749。本地 HEAD 3a526e4 相对开发分支 ahead 3 / behind 0，拉取新增提交 0。本轮接续工作树已有的 11 个修改文件和 P7 未跟踪脚本、证据全部保留。AGENTS.md 与 DEVELOPMENT.md 已跟踪。

修改前再次完整检查，Node 94 PASS、0 FAIL、0 SKIP、996.0321 ms，Python 314 PASS、1 显式真实窗口 SKIP、43 subtests PASS、11.42 s。补查真实 SDK ToolExecutionPipeline、原生附件 renderer、provider 的 prepareRequestImages 路径及实际请求图像统计。

官方 CLI 的 plugin add link 已通过插件管理机制重装当前目录，安装日志位于 .runtime/p7-054-install.log。新会话 session-6db5c6a0-1d2b-4cc5-9b0a-ad99fd171869 实际调用 status 一次，返回 success=true、version=0.5.4、build=ce3fad10e131cdab、operations=[]、input_busy=false，browser/UIA/transient worker 初始均为 false。六工具清单通过该会话及基准会话实际 request/header 核对。

六个基准 JavaScript 入口分别 node --check 全部通过，reconcile-benchmark.py 编译通过。0.5.4 npm pack --dry-run 实测 104 文件、压缩 2594813 bytes、解包 3236082 bytes，基准脚本和 p7-evidence 入包 0。对比 P0 压缩 27570384 bytes、162 文件，当前发行白名单保持三个 optional peers。

旧版 DeepSeek 持久化在补齐恢复阶段 PID 采样后独立复验 3/3，成功端到端 p50/p95 为 94741.306/97570.795 ms，98 工具、96 模型请求、provider 图片 0，完整 totalTokens 2130087。证据为 legacy-deepseek-persistence-recheck.json，原正式对照的 2/3 继续保留。

### 0.5.4 受影响任务复验

记事本、附件、持久化分别重新独立执行 3 次，两模型合计 18 次，使用原任务提示与业务完成标准。DeepSeek 完成 8/9，Hy4 完成 9/9。完整会话对账各 9/9，归档为 deepseek-054-recheck.json 与 hy4-054-recheck.json。

| 路由 | 任务 | 完成 | 成功端到端 p50 / p95 ms | 工具 p50 / p95 ms | 工具样本 | 模型请求 | provider 图片 | 完整 totalTokens |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| DeepSeek | 记事本 | 2/3 | 32050.086 / 33512.103 | 465.815 / 5048.514 | 19 | 22 | 13 | 未知，已完成请求累计 360988 |
| DeepSeek | 附件 | 3/3 | 12370.045 / 17522.276 | 301.238 / 2913.106 | 8 | 11 | 10 | 133847 |
| DeepSeek | 持久化 | 3/3 | 47160.662 / 52094.004 | 231.665 / 2342.753 | 33 | 39 | 59 | 650802 |
| Hy4 | 记事本 | 3/3 | 66949.359 / 136291.575 | 343.139 / 5059.975 | 29 | 32 | 54 | 916254 |
| Hy4 | 附件 | 3/3 | 22250.318 / 24869.934 | 313.634 / 720.725 | 9 | 12 | 16 | 156980 |
| Hy4 | 持久化 | 3/3 | 176148.236 / 302940.907 | 205.140 / 923.813 | 58 | 60 | 169 | 1396238 |

DeepSeek 记事本第 2 次在 19041.749 ms 结束，最终 reason=error、code=MALFORMED_RESPONSE，提供方返回的 doraemon_act 参数 JSON 在 expect 字段处截断，未进入插件 execute。该次文件正文未达标，继续计为 FAIL，完整 usage 记为未知。此前 open 返回 LAUNCH_UNCONFIRMED，模型已通过目录与指定文件标题恢复到正确窗口。现代记事本复用既有进程时缺少新进程归属证据，运行时保留实际启动不确定状态，后续操作经新观察与 ref 校验。Hy4 的 3 次记事本均保存正确文件，并读回未修改状态。

两模型此次分别使用 60/96 工具、72/104 模型请求、82/239 次上下文图片发送，caption 均为 0。Hy4 持久化第 1 次两个会话合计 302940.907 ms，各单会话均在 300000 ms 上限内。三次均确认旧 browser PID 退出、新 PID 使用同 profile、Cookie/localStorage 恢复及模拟登录失效和验证码提示。模型仍可能重复核查关闭状态，实际耗时及图片开销已保留。

### 最后回归、启动与阶段闸门

真实 actions_probe 在本机已有 Python 权限下执行，通过 19 个操作样本，工具 p50/p95 为 191.932/3086.074 ms，响应 561–7132 bytes，总计 15765 ms。覆盖中文 replace/insert、checkbox、select、附件、右键/双击、hover、DOM 嵌套 frame、原生与浏览器图片点、drag、滚动、失效 ref、模态与导航中断、双 owner 输入、取消及重放。重放额外派发 0、取消后尾随输入 0、parser 初始化 0。600 ms 原生提示采样 2 次、命中 118.102 ms，120 ms 浏览器提示采样 3 次、命中 13 ms，act/status 证据一致。显式真实保存提示测试独立执行 1 PASS、2.053 s，测试窗口和提示均按 fixture 的 finally 清理。

无可选栈 runtime_probe 再次通过，health/目录合计 125 ms、13 个窗口、退役路由 5/5 返回不存在，禁止的 standalone/视觉模块实际导入 0、parser 初始化 0、释放 runtime 1/1。此处 health_ms 包含工厂生命周期、鉴权、路由与目录检查。

先只读核对主运行时 journal，活跃/仍运行操作 0，已保留记录 856。核对专用 profile 的唯一可见窗口为 about:blank 后正常关闭，随后沿用 createKernel 的正式启动入口重启空闲内核。旧 PID 15024、新 PID 23404，cold ready 为 2121.415 ms，首个有效目录观察距启动 2351.325 ms、目标 14，样本 1。browser/UIA/transient worker 初始均未启动，profile 与用户数据保留。factory probe 和真实动作 probe 使用各自隔离 home。

重启后的首次新 DSH status 试验沿用了 headless 的外管内核配置，未注入 token，实际返回 BRIDGE_UNAVAILABLE，该日志保留在 .runtime/p7-final-new-session-status.jsonl。随后在 CLI 的临时 launch overlay 中显式采用默认 manageKernel=true、autoStart=true，真实新会话 session-9ab470a6-2b05-40a6-916c-2d5dee88738b 独立成功，状态 version=0.5.4、build=ce3fad10e131cdab、input_busy=false、operations=[]。该路径自动接管已启动内核，request/header 的六工具发现 6/6，凭据和 profile 文件均经既有机制管理。

| P7 退出项 | 状态 | 最终实测 |
| --- | --- | --- |
| 完整回归与版本 | PASS | scripts/check.ps1，Node 94 PASS、0 FAIL、0 SKIP、991.9607 ms，Python 314 PASS、1 显式真实窗口 SKIP、43 subtests PASS、10.83 s；显式窗口补验 1 PASS；Host/Client 语法 2/2 |
| 六工具、图片与单一规划入口 | PASS | 新版正式会话 request/header 54/54 只含六个 doraemon 工具，重启后新 managed 会话再次发现 6/6、status 1/1，provider 原生图片正式投递 DeepSeek 355 次、Hy4 679 次 |
| 观察、引用与可执行动作矩阵 | PASS | 260 份正式观察重复后端采集 0、caption 0；最后实机动作样本 19，取消清理、幂等重放、图片点与窗口/标签/frame 归属均通过 |
| 持久化与模型对照 | PASS | 旧新正式尝试 84 次，受影响任务复验 18 次，旧版持久化另行复验 3 次，逐次失败保留；0.5.4 持久化两个模型均 3/3 |
| 发布、依赖与迁移 | PASS | 0.5.4 打包 104 文件、2594813 bytes，3 个 SDK optional peers，开发基准和证据入包 0，P6 数据迁移与只读包记录保留 |
| 真实异 DPI / 负原点多屏 | BLOCKED | 再次 EnumDisplayMonitors 实测 1 屏，矩形 [0,0,2560,1440]，GetDpiForMonitor=96×96、HRESULT 0；混合 DPI 可用 false、负原点可用 false，实际跨屏场景 0 次 |

P7 保留 BLOCKED，受 DEVELOPMENT 阶段闸门约束。已执行的数值和断言见 p7-evidence/release-verification.json。真实多屏矩阵需要至少两个不同 DPI 的显示器，并将其中一个配置到主屏左侧或上方，再按第 7 节执行移动、resize、同语义引用和图片点读回。当前 test/test_dpi.py 与 test/test_runtime_targets.py 的确定性变换和失效用例均随完整套件通过，真实多屏项继续保持未通过。精确费用与图片独立计费按协议允许值记为未知。

### 0.5.5 缺陷复核与 0.5.6 修复

2026-11-26 检查用户提供的验收报告和本地 patch。报告中的 UIA `Region` 下标、OpenCV `IMREAD_BGR`、NumPy 布尔序列化、视觉依赖顶层导入、首次 open 来源帧变化和运行时版本落后均在当前源码中复现。修复后包版本与内核版本同步为 0.5.6。

| 检查项 | 状态 | 实测结果 |
| --- | --- | --- |
| UIA 客户区重复节点 | PASS | 隔离回归 1/1，Region 使用 left/top/width/height，边界外重复节点被客户区内节点替换 |
| 视觉动作效果与可选依赖 | PASS | 回归 2/2，相同 JPEG 返回原生 `bool(false)`，不同纯色 JPEG 返回原生 `bool(true)` 且 JSON 编码成功；模拟缺少 cv2/numpy 时返回未知值 |
| open 初始观察竞态 | PASS | 回归 1/1，首次 `TARGET_CHANGED` 后 150 ms 重试并成功读回；最多重试 3 次 |
| 版本与变更记录 | PASS | `package.json` 与 `kernel/dsh_desktop/version.py` 均为 0.5.6，CHANGELOG 已增加对应条目 |
| 完整检查 | PASS | `scripts/check.ps1`，Node 94 PASS、0 FAIL、0 SKIP；Python 318 PASS、1 显式真实窗口 SKIP、43 subtests PASS，11.63 s |
| 真实 DSH 会话及模型验收 | NOT RUN | 本轮仅执行源码、单元及完整仓库检查，未重新启动 DSH，也未重新发起两个模型请求 |

Steam 同名控件的近似尺寸归并和最小化窗口的幽灵 target 仍需实机复现后分别确定判据。本次没有加入点击失败后的兄弟节点重试，因为自动重放输入可能作用于错误控件。`observation_changed` 继续表示语义观察变化，是否有可见画面变化由独立的 `visual_effect` 表达，符合 PROTOCOL 的证据区分。

### 0.5.6 复验问题修正与 0.5.7

2026-11-26 读取用户提供的 0.5.6 复验报告。报告中 `visual_effect: false` 与另行比较截图得到的 16.52% 差异无法从已提交的原始图片资源独立复算，因为报告未附工具使用的两个 frame ID 或图像字节。本次修正了可确认的错误分类，并让每个可比较结果携带实际 `visual_effect_changed_ratio`，可在下一次实机复验中定位该不一致。

| 检查项 | 状态 | 实测结果 |
| --- | --- | --- |
| 比较不可用状态 | PASS | 无法解码返回 `(null, decode_failed)`，不同尺寸返回 `(null, shape_mismatch)`，均不再声称无变化 |
| 比较比例与序列化 | PASS | 相同 JPEG 的比例 0.0 且 `false`，不同纯色 JPEG 的比例大于 0.005 且 `true`，Python 原生 bool 可 JSON 序列化 |
| 读回未知状态与原因 | PASS | 1/1 回归，返回 `visual_effect: null` 与 `visual_effect_reason: shape_mismatch` |
| 启动窗口去歧义 | PASS | 1/1 回归，同一窗口 PID 下唯一有标题窗口被登记，即使进程树证据含多个 helper PID |
| 完整检查 | PASS | `scripts/check.ps1`，Node 94 PASS、0 FAIL、0 SKIP；Python 321 PASS、1 显式真实窗口 SKIP、43 subtests PASS，11.68 s |
| 真实 DSH 与 QQMusic 冷启动 | NOT RUN | 本轮没有重装后启动 DSH 新会话，也未结束 QQMusic 并实测冷启动 |
| 双模型验收 | NOT RUN | 未调用 wb/cn:deepseek-v4.1-flash 与 wb/cn:hy4std |

启动筛选改为比较候选窗口记录中的 owning PID。报告中 unique-title 规则原本已存在，但跨候选汇总的 matched process-tree PID 集合会被 helper 子进程扩大，导致规则失效。真实 QQMusic 候选及首次 `visual_effect_changed_ratio` 仍需运行时复验。P7 继续受真实异 DPI、负原点多屏硬件条件阻塞。

复现最后实机检查可在项目根目录执行以下命令，使用新的隔离 home 目录保留已有证据。

```powershell
pwsh -File scripts/check.ps1
$env:PYTHONIOENCODING = 'utf-8'
$env:TEMP = $env:TMP = Join-Path (Get-Location) '.runtime/test-temp'
python scripts/run-live-probe.py test/actions_probe.py .runtime/p7-actions-reproduce-home
$env:DSH_LIVE_DIALOG_TEST = '1'
python scripts/run-live-probe.py test/test_cleanup_dialog_live.py
```

受影响模型任务的命令沿用 README 的基准入口，增加 --tasks=notepad,upload,persistence，每次使用空输出目录。原始会话和请求日志继续位于本机忽略目录，归档 JSON 排除工具全文、模型全文、凭据和图片原文。更新 HIGH_HANDOFF 中过期的 P5 状态与测试基线，后续进度以 PHASES 顶部表为准。

### 0.5.8 原生输入事件修复

2026-11-26 针对 Electron 输入框替换文本后界面状态未更新的问题，原生 `replace` 改为聚焦后全选、删除并键入，取消 UIA SetValue 快路径。

| 检查项 | 状态 | 实测结果 |
| --- | --- | --- |
| 完整检查 | PASS | `scripts/check.ps1`，Node 94 PASS，Python 322 PASS、1 SKIP、43 subtests PASS，19.33 s |
| 原生输入批量动作 | PASS | 隔离 `actions_probe.py`，replace 与 END 按键 2/2 完成，读回 `batch input`，总耗时 44937 ms；原生与浏览器短提示均命中，parser 初始化与 caption 均为 0 |
| Electron / WorkBuddy | NOT RUN | 尚未验证替换后发送按钮 enabled，也未向对话发送消息 |
| 双模型请求 | NOT RUN | 本次未重新发起两个模型请求 |

P7 仍受真实异 DPI、负原点多屏硬件条件阻塞。

### 2026-11-26 验收范围修订与 0.5.13

用户明确将真实双屏、混合 DPI 与负原点多屏验收挂起，以当前固定 1920×1080 单屏作为后续执行验收范围。旧硬件实测数值保留，挂起项不再阻塞当前范围，亦未计为实测通过。新增缺口能力仍须逐项回归。0.5.13 完整检查为 Node 94 PASS、Python 330 PASS、1 SKIP、43 subtests PASS（20.15 s）。新增独立监听回归覆盖两次原生提示出现、取消释放、浏览器执行线程让出和无命中空时间线。

### 0.5.14 续接检查

工作树包含续接前已有的窗口、网页查询与稳定等待改动，尚未提交。完整检查为 Node 94 PASS、Python 334 PASS、1 SKIP、43 subtests PASS（20.20 s）。隔离 actions_probe 通过，20 个操作样本，原生与浏览器瞬态提示均命中，取消清理 1/1、重复派发 0、parser 与 caption 均为 0，总耗时 43218 ms。此探针未覆盖新增窗口和网页查询能力，仍需单独实机验证。

本轮完整检查为 Node 94 PASS、Python 340 PASS、1 SKIP、43 subtests PASS（21.80 s）。隔离 actions_probe 20 个操作样本通过，原生与浏览器瞬态提示均命中，取消清理 1/1、重复派发 0，parser 初始化 1 次、caption 0，总耗时 49406 ms。云智安全管家实测病毒 Tab 为 selected=true，OCR 查询「日志」置信度 0.999、耗时约 2–2.4 秒。点击后 UIA 超时，操作保留已派发、验证未知及重新定位建议，未观察到菜单弹出。此次未发生窗口关闭，自动重绑定仅有单元测试证据；页面主体文本、真实瞬态菜单及新窗口和网页查询入口仍需实机验收。

### 0.5.14 桌面合成局部证据与最终回归

用户指出日志菜单可能属于独立窗口。源码确认主窗口 capture_detailed 优先 PrintWindow，独立 HWND 的弹层可能遗漏。原生点击局部证据改为派发前后采集桌面合成画面，主窗口即时全图与局部前后图一起通过 Host 附件返回。该行为适用于原生窗口的独立弹层，采集区域为点击点周围 ±200 物理像素。

| 检查项 | 状态 | 实测结果 |
| --- | --- | --- |
| 完整检查 | PASS | scripts/check.ps1，Node 95 PASS、0 FAIL、0 SKIP，2542.8469 ms；Python 347 PASS、1 显式真实窗口 SKIP、43 subtests PASS，21.17 s；Host/Client 语法通过 |
| 真实原生与浏览器动作 | PASS | actions_probe v6，29 个 operation，总计 59906 ms；窗口 activate/topmost/highlight/cleanup、Web dom_query/extract/settle、中文表单、窗口重建重绑定与 TARGET_STALE、取消及重放全部通过，重放额外派发 0，parser 初始化 1，caption 0 |
| 独立 HWND 菜单闭环 | PASS | 隔离 owned popup 夹具，screen_region 局部变化比例 0.173538；OCR 候选置信度 0.994，通过 popup_observation_id 与 p ref 点击后父窗口读回 Owned popup selected |
| 低对比度变化与 toggle | PASS | 浏览器夹具全图变化比例 0.001536、visual_effect=false，局部比例 0.077371、local_visual_effect=true；第二次点击关闭、toggle=true |
| 云智真实菜单采集 | PASS | status=completed、success=true、physical_click 已派发；全图比例 0.000104、visual_effect=false，局部比例 0.022394、local_visual_effect=true；局部 after 图实际显示两项菜单，p1「病毒查杀日志」置信度 0.998、p2「病毒库升级日志」置信度 0.999；verification=unknown，保留图片、候选与后续建议 |
| 云智双通道观察 | PASS | 同一 combined 观察含「病毒」selected=true 与页面实际文字「已经有很久没杀毒了」，OCR 置信度 0.999；uia_coverage=partial，缺失按钮标记 semantic_missing；总计 3266.717 ms，UIA 946.902 ms、capture 99.474 ms、OCR 2176.412 ms，caption 0 |
| 云智纯视觉入口 | PASS | source=ocr、query=日志，735.784 ms，其中 capture 93.476 ms、OCR 608.904 ms；UIA 0 次、OCR 1 次、caption 0，置信度 0.999；kernel/target integrity 均为 12288、uipi_risk=false |
| 轻量 runtime | PASS | 独立 runtime_probe，protocol 2、31 个窗口、health/目录 843 ms、退役路由缺失 5/5、禁止依赖导入 0、parser 初始化 0、runtime 释放成功 |
| 发布检查 | PASS | npm pack --dry-run，0.5.14，104 文件，2610797 bytes，解包 3304271 bytes；三个 DSH SDK peers 均保持 optional；git diff --check 通过 |
| 新 DSH 会话与两模型 | NOT RUN | 0.5.14 尚待安装后的实际六工具/附件与两个模型复验，历史 0.5.4 的模型数值保留在前文 |
| 云智特定窗口重建 | NOT REPRODUCED | 本次云智操作期间未发生 HWND 重建。通用真实窗口重建、自动重绑定与旧观察失效已由隔离 fixture 实测通过 |

上段「未观察到菜单弹出」已由本轮桌面局部图修正。owned popup 夹具初次断言因 OCR 返回 Ownedmenuitem 而非含空格文字失败，断言改为忽略空白后 v6 通过，raw_text 保留 OCR 原文。像素变化与 OCR 候选均作为证据返回，popup_detected=null、appearance_verified=false 表示弹层语义尚未由控件状态确认。

本机复现证据分别位于 .runtime/check-final.log、.runtime/actions-screen-region-v6.log、.runtime/runtime-final.log、.runtime/security-screen-region-click.log，以及 %TEMP%\\dsh-local-evidence-20261126-v6\\actions-probe.json、%TEMP%\\dsh-local-click-_hafobe_、%TEMP%\\dsh-combined-hw8epnvd\\report.json。P7 继续进行中，固定单屏范围内可交 DSH 体验验收；真实双屏、异 DPI 与负原点多屏按用户决定保持挂起。

### 0.5.15 无标题窗口发现与跨进程跟随

2026-11-26 针对 DSH 实测空标题日志窗口未被发现的反馈，增加窗口标题来源、目录过滤与 HWND 打开入口，并沿 owner 链和子进程关系发现新 HWND。开工分支为 codex/agent-runtime-refactor，HEAD babc4c4，工作树干净。基线 scripts/check.ps1 为 Node 95 PASS、0 SKIP、2972.3387 ms，Python 347 PASS、1 SKIP、43 subtests PASS、21.54 s。包与内核版本同步至 0.5.15。

| 检查项 | 状态 | 实测结果 |
| --- | --- | --- |
| 完整检查 | PASS | scripts/check.ps1，Node 95 PASS、0 FAIL、0 SKIP、2833.2684 ms；Python 359 PASS、1 显式真实窗口 SKIP、57 subtests PASS、20.65 s；Host/Client 语法通过 |
| 无标题窗口与 HWND 打开 | PASS | actions_probe v5 的真实 owned popup 顶层 window_text 为空，title_source=child_text，目录保留 HWND/PID/process/owner；默认发现、include_untitled=false 过滤与 public open(hwnd) 激活及初始观察全部通过 |
| 跨子进程重绑定 | PASS | 源 fixture PID 27620 销毁 HWND 后，发现子进程 PID 25056 的 HWND 14946700，owner_hwnd=0、window_text=""、title=日志与信任管理、title_source=child_text、relationship=child_process；动作 1154.545 ms，自动重绑定、新观察及旧 observation 的 TARGET_STALE 校验通过 |
| 关联与去歧义回归 | PASS | 带创建时间的 owner/进程实例匹配、无关共同祖先排除、已存在窗口排除、多候选保留、延迟候选采样、派发结果保留与停止后续批量步骤全部通过 |
| 真实动作矩阵 | PASS | actions_probe v5，30 个 operation 全部 completed，总耗时 60203 ms；原生/浏览器表单、瞬态提示、窗口控件、Web 查询与稳定等待、同进程 HWND 重建、取消及重放通过，重放额外派发 0，parser 初始化 1，caption 0，模型请求 0 |
| 独立弹层与局部图 | PASS | owned popup OCR confidence=0.995，local_changed_ratio=0.1284，p ref 点击后读回成功；浏览器浅色弹层全图比例 0.001536、局部比例 0.077371，第二次点击关闭且 toggle=true |
| 轻量 runtime | PASS | protocol 2，目录 52 个窗口，其中原始标题为空 21 个，元数据完整与无标题过滤均通过；health/目录等检查合计 266 ms，退役路由缺失 5/5，禁止依赖导入 0，parser 初始化 0，runtime 释放成功 |
| 发布检查 | PASS | npm pack --dry-run，0.5.15，104 文件，2613316 bytes，解包 3315189 bytes；三个 DSH SDK peers 均为 optional；git diff --check 通过 |
| 当前版本 DSH 与两模型 | NOT RUN | 0.5.15 尚待真实新会话复验六工具、status、图片附件与云智菜单跨进程路径，以及 wb/cn:deepseek-v4.1-flash、wb/cn:hy4std 请求；前文历史模型数值继续保留 |

实机探针 v1–v4 保留失败记录。完整窗口描述用于每次动作边界时开销过高，已改为轻量身份快照，并仅在新候选需要子进程证明时读取 ancestry。探针改为通过 operation/status 跟随完成，并将输入锁等待检查延长至 10 秒。500 ms 采样曾漏掉源窗口关闭后的延迟子窗口，销毁路径现使用 3 秒预算，v5 全部通过。探针 finally 清理自身子进程，已精确清理 v4 遗留的夹具进程。

证据保留于 .runtime/check-window-discovery-baseline.log、.runtime/check-window-discovery-final.log、.runtime/actions-window-discovery-v1.log 至 v5.log、.runtime/runtime-window-discovery.log、.runtime/pack-window-discovery.json 及 %TEMP%\\dsh-window-discovery-20261126-v5\\actions-probe.json。P7 继续进行中，真实双屏、异 DPI 与负原点多屏按用户决定保持挂起。

### 本地未发布 0.5.8 动作局部视觉变化修复

2026-11-26 在原分支完成 P7 缺陷修复。修改前完整检查通过 Node 94 项、Python 321 项、1 项显式真实窗口 SKIP、43 subtests，Python 耗时 11.74 s。版本同步为 0.5.8。本轮增加 18 项有针对性的回归测试，公开返回结构的决定见 ADR 0011。

用户提供的 evidence-0.5.6-visual-effect 原图使用内核同一算法复算，QQ 音乐全图为 0.165184，Steam 为 0.70763。前者精确复现原报告 16.52% 的量级，后者与工具返回完全一致。QQ 音乐动作内部 before 帧仍未留存，因此这两张公开帧继续用于图像算法复核。按钮区域由原图人工指定，回放脚本中明确列出坐标。

实现复用现有动作前后截图，保留全图半采样灰度差口径，并增加控件边界外扩 12 图片像素、动作点周边 64×64 和 256×256 区域。局部使用已解码图像原始分辨率，比例严格大于 1% 且至少 12 个变化像素时命中。新字段分别给出全图、局部结果、阈值、区域、动作下标、实际内部 observation/frame ID 和图像 SHA256。比较前核对坐标几何，变化时返回 unknown。帧沿用会话隔离的有界缓存，获取期限见 PROTOCOL。

| 检查项 | 状态 | 实测结果 |
| --- | --- | --- |
| 合成与入口回归 | PASS | 新增 18/18，覆盖微小双竖线、不同窗口尺寸、相同帧、低幅 JPEG 噪声、细小光标、最小像素门槛、远处横幅、外围菜单、裁剪、缩放、负原点、iframe CSS 原点、边缘裁剪、几何变化、缺失前帧、语义控件和批次坐标复位 |
| QQ 音乐原图与按钮重构回放 | PASS | 完整原图全图 16.5184%，局部控件 2.7065%、64×64 周边 2.3682%，局部变化 97 个原分辨率像素。仅把后图按钮区域复制到前图并用 PNG 保留解码像素后，全图 0.011%，局部仍命中。相同原图返回 false |
| 原生与浏览器实际入口 | PASS | actions_probe 连续两轮完整通过，耗时 19.329 s 与 17.984 s，每轮 22 个成功返回的 act 操作。UIA checkbox 局部命中，两轮 DOM ref 与纯 frame 坐标均检测到 32 像素按钮变化。两次浏览器动作全图各 0.0082%，局部近邻约 2.1065%，semantic-only 观察保持稳定。精确比较帧共 4 张每轮均按 ID 取回并核对 SHA256 |
| DSH 安装与新会话 | PASS | 使用官方 CLI plugin add link 安装 headless profile，新的 wb/cn:deepseek-v4.1-flash 会话调用 status→open→act→status，版本 0.5.8、build c7b762237d12917e。5 个 Agent 请求均含六个 doraemon 工具，另有 1 个辅助请求，图片内容块累计 6 个。业务页独立读回 clicks=1、playing=true |
| DSH 局部证据与持久读回 | PASS | 全图 0.024%，控件局部 6.5004%、点击近邻 6.1065%，返回 basis=local、visual_effect=true、observation_changed=false。同一 operation 的 status 视觉字段全部一致，两个内部 frame 下载后哈希匹配并精确复算全部比较字段 |
| 比较计算耗时 | PASS | 1366×854 同一对真实帧，预热各 5 次再测各 50 次。全图 median 4.054 ms、p95 4.474 ms，联合局部 median 4.136 ms、p95 4.522 ms。该数字只覆盖图像比较 |
| 完整检查与打包 | PASS | 提交前 scripts/check.ps1，Node 94 PASS、0 FAIL、0 SKIP，Python 339 PASS、1 显式真实窗口 SKIP、45 subtests PASS，10.91 s。npm pack --dry-run 共 104 文件、2598585 bytes，运行文件清单完整 |
| QQ 音乐 0.5.8 实际播放与 Hy4 | NOT RUN | 本轮 QQ 音乐使用用户留存图像复算，模型验收使用 DeepSeek 与自建播放图标页面 |
| 真实异 DPI / 负原点多屏 | SUSPENDED | 后续用户决定挂起真实硬件矩阵，本轮确定性坐标测试覆盖负原点与缩放 |

首次 live fixture 已检出小图标，但点击同时改变 focused 字段，导致要求 observation_changed=false 的隔离断言失败。随后在测试按钮阻止 mousedown 默认焦点行为，两轮均只改变 canvas。96×96 初始近邻对这枚图标得到约 0.9459%，实测不足 1%，因此近邻缩为 64×64，保留外围范围。最初 DSH 会话缺少 wb adapter，随后复用现有 benchmark 的 provider overlay 与 credentials 服务，正式验证成功。失败日志和原始图片保留在本机 .runtime。

局部阈值由这些样本校准。悬停、焦点或按钮按下反馈也可能命中，业务成功继续由条件与状态读回判断。当前仍使用一次动作后观察，延迟生效的状态可通过 wait/expect 验证。归档测量见 p7-evidence/local-visual-058.json。实机 fixture 与 loopback 验证服务已完成清理。

以下命令可在根目录复现离线证据和实际入口验证。实机 probe 每次使用新的隔离 home。DSH profile 的安装与新会话操作沿用 README 中的官方 CLI 入口及本机已配置 provider。

```powershell
pwsh -File scripts/check.ps1
$env:PYTHONPATH = Join-Path (Get-Location) 'kernel'
.\.venv\Scripts\python.exe test/visual_effect_replay.py '..\evidence-0.5.6-visual-effect'
$env:PYTHONIOENCODING = 'utf-8'
$env:TEMP = $env:TMP = Join-Path (Get-Location) '.runtime/test-temp'
python scripts/run-live-probe.py test/actions_probe.py .runtime/p7-local-visual-reproduce-home
```

### 本地未发布 0.5.9 视觉变化关联性提示

2026-11-26 基线 `scripts/check.ps1` 通过 Node 94 项、Python 339 项、1 项显式真实窗口 SKIP、45 subtests，Python 11.10 s。外围或全图命中且控件边界与点击近邻均未命中时，增加关联性未知、空间原因和核验界面状态的提示。具体合同见 PROTOCOL，决策见 ADR 0012。

| 检查项 | 状态 | 实测结果 |
| --- | --- | --- |
| 视觉回归 | PASS | 19 项、2 subtests 通过，覆盖播放图标、无变化、外围菜单、全图与外围同时命中、无局部几何、几何变化；Host 的 act/status 字段转发回归通过 |
| 真实动作入口 | PASS | 隔离 actions_probe 完成，耗时 20.719 s。外围测试控件点击一次，控件区域与近邻各 0 个变化像素，外围 3001/66049，返回 `surroundings_only`；同一 operation 的 status 视觉字段一致 |
| 安装与新 DSH 会话 | PASS | 官方 CLI link 安装后，新 DeepSeek 会话调用 status、open、act、status，版本 0.5.9，5 个 Agent 请求均发现六工具。控件与近邻各 0 个变化像素，外围 2890/66049，全图比例 0.002476，act/status 返回相同未知关联提示；页面独立读回 clicks=1、playing=true |
| 完整检查 | PASS | scripts/check.ps1，Node 95 PASS、0 SKIP，Python 340 PASS、1 显式真实窗口 SKIP、45 subtests PASS，11.64 s |
| 真实异 DPI / 负原点多屏 | SUSPENDED | 后续用户决定挂起真实硬件矩阵，记录保留为未实测 |

定向测试首次直接运行缺少 `PYTHONPATH`，完整检查首次执行发现两处测试样本问题，已修正断言位置和近邻区域边界，再通过上述检查。实机页面及隔离服务已关闭。

### 本地未发布 0.5.10 悬停语义及其他动作的视觉读回

2026-11-26 修改前完整基线 Node 95 PASS、Python 340 PASS、1 SKIP、45 subtests，Python 11.52 s。悬停使用当前步骤的前后观察匹配 DOM 身份或 UIA runtime ID，输出新观察元素和可读取的展开状态变化。截图不可用时悬停继续尝试语义采集。hover、scroll、drag 复用画面比较，拖动局部区域位于释放点。

| 检查项 | 状态 | 实测结果 |
| --- | --- | --- |
| 原生和浏览器动作 | PASS | 隔离 actions_probe 24 个操作，20.235 s；浏览器 hover 返回 tooltip `Hover details` 和 `aria-expanded` false→true，status 一致。原生 ComboBox 展开前后 UIA 状态 false→true，runtime ID 关联后的变化匹配成功；scroll、drag 返回视觉字段 |
| 新 DSH 会话 | PASS | 官方 CLI link 安装 0.5.10，新 DeepSeek 会话 status→open→act→status，5 个 Agent 请求均发现六工具；hover 的新 tooltip、展开状态和视觉结果返回，operation status 的语义证据一致 |
| 完整检查 | PASS | scripts/check.ps1，Node 95 PASS、0 SKIP，Python 344 PASS、1 显式真实窗口 SKIP、48 subtests PASS，11.56 s |
| 真实异 DPI / 负原点多屏 | SUSPENDED | 后续用户决定挂起真实硬件矩阵，记录保留为未实测 |

语义采集受可访问性、采集时机和截断约束，独立窗口或纯绘制 tooltip 可能需要额外观察。延迟显示可通过 expect/wait 验证。UIA 的部分展开和叶节点状态不映射为布尔 `expanded`。测试服务及自建窗口已关闭。


### 2026-11-27 公司端更新与本地动作证据合并 0.5.16

本轮在 `codex/agent-runtime-refactor` 合并公司端 `0475d7a` 与本地 `421a97d`，版本统一为 0.5.16。保留快速截图、独立瞬态监测、桌面合成点击区域、低对比度变化、剪贴板和文件动作、UIA pattern、窗口发现及跨子进程重绑定。加入控件范围、落点近邻和外围的原分辨率比较，以及 hover 的新元素与展开状态证据。ADR 编号冲突通过将本地三份决策记录顺延到 0011–0013 解决。

| 检查项 | 状态 | 实测结果 |
| --- | --- | --- |
| 修改前基线 | PASS | scripts/check.ps1，Node 95 PASS，Python 344 PASS、1 SKIP、48 subtests PASS |
| 最终完整检查 | PASS | scripts/check.ps1，Host/Client 语法通过，Node 96 PASS、0 FAIL、0 SKIP，Python 384 PASS、1 显式真实窗口 SKIP、77 subtests PASS，13.75 s |
| 合并后动作探针 | PASS | 隔离 actions_probe 35 个操作成功完成，45375 ms。覆盖原生与浏览器表单、窗口管理、网页查询和抽取、稳定等待、瞬态提示、owned popup OCR 与 p ref 点击、窗口关闭及子进程重绑定、stale guard、取消和幂等重放，额外重复派发 0。parser 初始化 1 次，caption 0，模型调用 0 |
| 小图标局部变化 | PASS | DOM ref 与纯 frame 坐标点击的全图比例均为 0.000082、global_changed=false，局部为 true、basis=local。动作实际使用的前后图片按 frame ID 取回并核对 SHA256。原生 checkbox 全图比例 0.000664，控件局部比例 0.017131，局部命中 |
| hover、scroll 与 drag | PASS | hover 返回新 tooltip Hover details 与 expanded false→true，operation status 证据一致。UIA ComboBox false→true 使用 runtime ID 匹配。scroll、drag 均返回视觉字段。目标关闭、重绑定及读回失败路径的未知视觉结果有专项回归 |
| 公司端即时点击证据 | PASS | 原生独立 HWND 菜单 OCR 置信度 0.994，局部比例 0.0505，p ref 点击成功。浏览器局部比例 0.07633，后续全图比例 0.001574，第二次点击关闭且 toggle=true。即时 local_visual_effect 与后续聚合 visual_effect 的采集时间和阈值分别保留 |
| 独立轻量 runtime | PASS | protocol 2，24 个窗口含 10 个无标题窗口，目录 metadata 与过滤通过，health/目录 141 ms，退役路由缺失 5/5，禁止依赖导入 0，parser 初始化 0，runtime 释放成功 |
| 官方安装 | PASS | 官方 CLI plugin add link 安装 headless profile 成功。安装器提示链接包的 peers 解析警告，后续两个新会话均正常加载六个 doraemon 工具并调用 status |
| DeepSeek 新会话 smoke | PASS | wb/cn:deepseek-v4.1-flash，1/1，7 个模型请求、6 个图片内容块。各 Agent 请求均包含六个 doraemon 工具，status→open→hover→operation status 完成，版本 0.5.16、build 2a24b4510dc2fd19。新 tooltip 与 expanded false→true 返回且持久化证据一致 |
| Hy4 新会话 smoke | PASS | wb/cn:hy4std，1/1，6 个模型请求、6 个图片内容块。各 Agent 请求均包含六个 doraemon 工具，同一任务完成，hover 的语义和视觉证据与 operation status 一致。独立页面读回两页均为 playing=true、clicks=1 |
| 发布清单 | PASS | npm pack --dry-run，0.5.16，104 文件，压缩 2616029 bytes，解包 3327408 bytes，动作运行模块已包含，standalone 已排除，三个 SDK peers 保持 optional |
| 本轮完整模型矩阵及公司应用专项 | NOT RUN | 两模型本轮各完成一次相同 hover 任务 smoke。完整多任务重复实验和云智等真实应用专项复验仍在 P7 后续范围内 |
| 真实双屏、异 DPI 与负原点多屏 | SUSPENDED | 按用户已确认决定挂起，当前验收范围为固定 1920×1080 单屏 |

首次完整检查发现 sparse checkout 未物化已跟踪 standalone 文件，恢复这些文件后检查通过。第一次实机探针缺少 rapidocr，随后复用本机已有可选视觉依赖。第二次原生菜单 OCR 候选为空，诊断复跑又发现新增 hover 夹具提示会遮挡原有菜单比较区域。将 hover 控件与提示移至独立位置后，第三次完整实机探针通过，原有公司端和本地断言均保留。前述失败日志保留在本机忽略目录。

两模型 smoke 使用同一版已启动内核。随后补齐读回失败和重绑场景的未知原因字段，最终完整检查覆盖该修正。真实服务与自建浏览器页面已关闭。两次 smoke 的 session 分别为 `session-d5caa300-f0db-42b6-94ef-4fa061280dbf` 和 `session-c1eaa6e0-c464-49e3-b20a-42d7c49f4f5a`。

完整检查与实机探针可在仓库根目录复现，每次使用新的隔离 home。下列 PYTHONPATH 复用本机已安装视觉依赖，依赖齐全的环境可按其安装位置调整。

```powershell
pwsh -File scripts/check.ps1
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONPATH = Join-Path $env:LOCALAPPDATA 'dsh-desktop/venv/Lib/site-packages'
$env:TEMP = $env:TMP = Join-Path (Get-Location) '.runtime/test-temp'
python scripts/run-live-probe.py test/actions_probe.py .runtime/p7-merge-0516-reproduce-home
python scripts/run-live-probe.py test/runtime_probe.py .runtime/p7-merge-0516-runtime-reproduce-home --live
node scripts/dsh-benchmark-entry.mjs plugin --profile headless add link:C:/Users/Admin/Documents/deepseek-harness/default-workspace/dsh-cua
npm pack --dry-run --json
```

本机证据包括 `.runtime/merge-0516-baseline.log`、`.runtime/merge-0516-check-final.log`、`.runtime/p7-merge-0516-home-3/actions-probe.json`、`.runtime/merge-0516-runtime.log`、`.runtime/merge-0516-install.log`、`.runtime/merge-dsh-requests.json`、`.runtime/merge-hy4-requests.json`、两份 `*-events-2.jsonl` 及 `.runtime/merge-0516-pack-final.json`。P7 保持进行中。
