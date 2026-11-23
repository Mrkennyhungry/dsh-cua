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

- [ ] 常规 Node/Python 套件完整执行，失败、跳过及环境阻塞有记录。
- [ ] 六工具通过实际 DSH 新会话发现，参数与图片链路实测通过。
- [ ] 无 Hermes、无 key、无视觉权重的对应路径通过。
- [ ] 目标引用、坐标、frame、取消、幂等、并发、瞬时反馈矩阵通过。
- [ ] 专用持久化 profile 重启恢复，首次登录与过期状态有验证。
- [ ] companion 可选运行及卸载行为通过。
- [ ] 发布白名单与依赖检查通过，安装后新会话状态正常。
- [ ] 两模型旧新对照与逐次结果已保存，性能结论可追溯。
- [ ] 用户可按交付说明在 High 实施记录中找到未验证项和剩余条件。

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
