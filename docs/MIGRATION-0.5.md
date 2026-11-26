# 升级到 0.5 系列

DSH 负责规划与模型配置，插件提供六个统一工具。通过 DSH 的 Plugins 页面或 plugin_manager install_bundle 安装本地目录或发布包，升级后创建新 DSH 会话并调用 doraemon_status。

## 入口迁移

| 旧入口 | 0.5 系列入口 |
| --- | --- |
| doraemon_run、doraemon_steer、自主任务轮询 | 在 DSH 会话描述任务，由 DSH 调用六工具 |
| doraemon_health、旧任务 status/stop | doraemon_status、doraemon_stop，长操作使用 operation_id |
| bind、foreground、apps/find、launch、web/open | doraemon_open，返回 target_id 与初始观察 |
| analyze、capture、UIA、elements、web/read、locate | doraemon_observe，返回 observation_id、ref 和可选图片 |
| invoke | doraemon_act 的 invoke、set_value、expand 步骤使用 UIA ref；select 和 check 对应选择和切换 |
| files | doraemon_act 的 file 步骤支持 create、write、read、list 和 delete，路径限定在 homeDir\\desktop_workspace，结果位于操作步骤的 method |
| clipboard | doraemon_act 的 clipboard 步骤支持 get、set 和 paste_file，结果位于操作步骤的 method |
| click、type、key、scroll、select、attach、web 动作 | doraemon_act，引用最近观察，返回动作后读回 |
| watch、wait-stable、wait-window | doraemon_wait，动作触发的短提示放在 act.expect，设置 transient=true |
| act_by_goal、decide、KB 自动挑选与复盘 | DSH 根据当前观察选择动作 |
| 内置工作台、IM、WeCom、cron、自主 Agent | 源码中的 standalone 历史边界与旧版本恢复入口 |

默认 HTTP 桥接提供 /runtime/*、/health 及可选 companion 状态接口。旧 /windows/*、/web、/tasks、/kb 等调用者应迁移到六工具。浏览器显式连接保留在 open.connect，传入 CDP URL 或 configured。

## 输入模式

`doraemon_act` 的 `input` 需要明确指定 `mode`。`replace` 聚焦目标后全选、删除并键入新文本，`insert` 在当前插入位置键入文本。原生控件使用键盘输入，让依赖输入事件的界面能够更新状态。

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
