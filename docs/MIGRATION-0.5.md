# 升级到 0.5 系列

DSH 负责规划与模型配置，插件提供六个统一工具。通过 DSH 的 Plugins 页面或 plugin_manager install_bundle 安装本地目录或发布包，升级后创建新 DSH 会话并调用 doraemon_status。

## 入口迁移

| 旧入口 | 0.5 系列入口 |
| --- | --- |
| doraemon_run、doraemon_steer、自主任务轮询 | 在 DSH 会话描述任务，由 DSH 调用六工具 |
| doraemon_health、旧任务 status/stop | doraemon_status、doraemon_stop，长操作使用 operation_id |
| bind、foreground、apps/find、launch、web/open | doraemon_open，返回 target_id 与初始观察 |
| analyze、capture、UIA、elements、web/read、locate | doraemon_observe，返回 observation_id、ref 和可选图片 |
| topmost、highlight、unbind、cleanup、foreground | doraemon_act 的 window 步骤，op 选择操作，目标使用 target_id |
| Web dom、extract、settle | act 的 dom_query/extract 使用 CSS selector；wait 的 settle 检查连续稳定时段，batch/action_status/lease 使用 act.steps、status 和 runtime 输入锁 |
| invoke | doraemon_act 的 invoke、set_value、expand 步骤使用 UIA ref；select 和 check 对应选择和切换 |
| files | doraemon_act 的 file 步骤支持 create、write、read、list 和 delete，路径限定在 homeDir\\desktop_workspace，结果位于操作步骤的 method |
| clipboard | doraemon_act 的 clipboard 步骤支持 get、set 和 paste_file，结果位于操作步骤的 method |
| click、type、key、scroll、select、attach、web 动作 | doraemon_act，引用最近观察，返回动作后读回 |
| watch、wait-stable、wait-window | doraemon_wait 的 transient_watch 返回独立监听时间线，duration_ms 控制时长；动作触发的短提示放在 act.expect，设置 transient=true |
| act_by_goal、decide、KB 自动挑选与复盘 | DSH 根据当前观察选择动作 |
| 内置工作台、IM、WeCom、cron、自主 Agent | 源码中的 standalone 历史边界与旧版本恢复入口 |

默认 HTTP 桥接提供 /runtime/*、/health 及可选 companion 状态接口。旧 /windows/*、/web、/tasks、/kb 等调用者应迁移到六工具。浏览器显式连接保留在 open.connect，传入 CDP URL 或 configured。

## 输入模式

`doraemon_act` 的 `input` 需要明确指定 `mode`。`replace` 聚焦目标后全选、删除并键入新文本，`insert` 在当前插入位置键入文本。原生控件使用键盘输入，让依赖输入事件的界面能够更新状态。

## 视觉观察与点击读回

`doraemon_observe` 的 `source` 可选 auto、ocr、image、combined。auto 默认使用结构化信息，ocr 直接识别画面，image 返回截图，combined 在同一次观察中合并结构化状态与 OCR。`region` 可将视觉识别限制到指定图片区域。OCR 使用 `raw_text` 与 `confidence`，视觉 ref 使用 v 前缀。查看 `uia_coverage` 和 `semantic_missing` 可判断哪些画面内容需要视觉定位。

click/invoke 返回 `post_click_frame` 与 `click_region_frames.before/after`。原生局部图采集点击位置周围 ±200 物理像素的桌面合成画面，覆盖独立窗口形式的菜单。结合 `local_visual_effect`、全图变化及控件状态判断下一步。`expect.transient=true` 另外生成有界区域 OCR 候选，使用 `popup_observation_id` 与对应 p ref 点击，候选有效期为 30 秒。验证未知时先查看这些即时证据，重复点击可能切换菜单开关状态。

目标窗口重建后，安全重绑定会返回 `rebound=true`、`original_target_id` 和新的 `target_id`。后续动作使用新的观察与 ref。`observation.integrity.uipi_risk=true` 时按返回建议通过已有 elevated task 重启 kernel，再继续输入。

## 数据与恢复

| 数据 | 位置与读取方式 |
| --- | --- |
| 当前 kernel 状态、日志、操作 journal | stateDir，默认 %USERPROFILE%\.dsh\desktop，包含 kernel-state.json、kernel.out.log、kernel.err.log、operations.sqlite3 |
| Python 与 runtime home | 默认 %LOCALAPPDATA%\dsh-desktop，homeDir 可覆盖，旧 logDir 作为 stateDir 兼容字段 |
| 浏览器 Cookie、登录和站点设置 | 优先复用 homeDir\web_profile_edge 或 web_profile，新的 profile 位于 stateDir\browser\web_profile_edge 或 web_profile |
| 截图与视觉权重 | homeDir\screenshots、homeDir\weights，支持 config.yaml 的 workspace 显式目录 |
| 历史独立工作台配置、知识、cron | 原 DSH_DESKTOP_HOME，历史默认 %LOCALAPPDATA%\DesktopCore，含 config.yaml、knowledge、cron\jobs.json 与 cron\output |
| 历史 Hermes 会话与记忆 | 独立 __main__ 入口把 HERMES_HOME 设为同一工作台 home，会话数据库为其中的 state.db；直接调用其他入口时检查实际 HERMES_HOME，连同工作台 home 整体备份 |
| DSH 会话和模型设置 | 由 DSH 自身维护，可在 DSH 会话中继续使用六工具 |

运行时按实际浏览器进程与 profile mutex 检查占用。关闭占用浏览器后可重试同一目录。首次登录、验证码或失效登录由用户在专用浏览器中处理，成功后沿用该 profile。

需要搬移 home 或 profile 时，先通过插件页停用插件并关闭占用该 profile 的浏览器，复制整个原目录到一个新的空目录，再通过插件配置界面设置 homeDir/stateDir。保留原目录作为恢复点，启动新会话验证 Cookie、localStorage 与 status 后继续使用。SQLite journal 复制需在 kernel 退出后进行，包含当时存在的 -wal 与 -shm 文件。支持直接用 SQLite 只读查询 operations 表中的 data JSON，operation_id、completed_steps、uncertain_step 用于核对部分执行。

恢复历史自主能力可将独立源码 checkout 到 6dd5563 或更早版本，在单独 venv 安装该版本依赖，设置 DSH_DESKTOP_HOME 与 HERMES_HOME 指向历史数据的备份副本，然后使用该版本的 python -m dsh_desktop 入口。当前仓库的 standalone/requirements.txt 与 standalone/legacy-host.js 保留源码兼容入口，kernel 中的 standalone_bridge、legacy_bridge、eternity、server、IM/WeCom、cron、static 等属于同一源码边界。默认发布白名单列出共享执行模块，历史模块可通过 Git 获取。

## 安装依赖与视觉

基础安装执行 scripts/bootstrap.ps1，安装 UIA、截图、输入和 Playwright 依赖。按需视觉执行以下命令。

```powershell
.\scripts\bootstrap.ps1 -Vision
.\scripts\bootstrap.ps1 -Vision -WeightsDir 'C:\existing-models\weights'
```

WeightsDir 内应包含 icon_detect\model.onnx，可同时提供 rapidocr 模型目录。OCR 使用 RapidOCR 包内模型作为后备。既有 homeDir\weights 继续可用。缺少视觉包或图标权重时，观察返回后端诊断与已有 DOM/UIA/图片，安装后重试观察即可。

0.5 系列保留 commandFeedback、companionSelection 和原有自定义路径配置。waitSeconds、apiKeyFile、enableExperimentalPageCache 是历史配置兼容字段。DSH 模型设置决定主模型，默认 caption 请求数为 0。companion 通过插件配置页开启 commandFeedback，构建方法见 commander-mode.md。

## 0.5.15 窗口发现

无 target 的 observe 默认包含可见无标题窗口。识别时可查看 window_text 原始标题和 title_source，目录返回 hwnd、pid、process。若只需原本有标题的窗口，可传 include_untitled=false。open 可使用目录中的 target_id 或正整数 hwnd。

菜单操作后的 new_window_candidates 提供本次新出现的 owner 链或子进程窗口。closed=true 时唯一关联窗口可自动重绑定，返回新 target_id 和观察。存在多个候选时，用 open 选定目标，再使用新观察中的 ref 继续操作。
