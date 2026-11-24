# 数据边界与结果读回

## 模型输入与本机数据

| 内容 | 处理位置与触发方式 |
| --- | --- |
| DOM/UIA、正文和动作读回 | 工具结果进入 DSH 当前会话，由其模型配置决定请求目的地 |
| 图片 | open、observe 和动作读回按策略生成，Host renderer 通过 DSH 原生附件交给当前模型 |
| OCR/YOLO | 显式视觉查询时按需加载本地 ONNX 依赖和权重 |
| Caption | 默认 runtime 请求数为 0，历史模块保留在源码兼容边界 |
| 浏览器 | 专用持久 profile 保存 Cookie、登录和站点设置，访问用户任务指定的网站 |
| 日志与 journal | stateDir 默认 %USERPROFILE%\.dsh\desktop，包含操作身份、部分结果与不确定步骤 |
| Python、截图和权重 | homeDir 默认 %LOCALAPPDATA%\dsh-desktop，workspace 配置可覆盖截图与权重目录 |

图片资源按 owner 隔离，经认证的 loopback bridge 读取。操作日志会对常见输入、路径和凭据字段做脱敏。已有日志和数据的保留与备份由实际目录决定。

高权限任务通过 kernel-boot.json 读取 bridge token 与启动参数。文件使用当前用户、SYSTEM 和 Administrators 的访问权限。默认执行沿用本机已有权限。

## 浏览器与 profile

默认使用专用持久 profile，兼容已有 homeDir\web_profile_edge 或 web_profile，新目录位于 stateDir\browser。运行时通过 mutex 和实际进程参数检查占用。首次登录、验证码和失效登录可由用户处理。

CDP 默认端口为 9333，浏览器在本机 loopback 提供调试接口。显式连接已有浏览器使用 open.connect。本机可访问 CDP 的进程能驱动该浏览器及其登录会话。

## 取消、部分完成与结果

act 的 dispatch、completed_steps、uncertain_step 和动作后 observation 描述现场证据。条件满足记录在 expectation 中。status 的 worker_running 表示底层调用是否仍在执行，stop 撤销后续输入租约。返回不确定状态时，先查询 operation 并重新观察目标，再决定恢复步骤。

窗口关闭可能留下保存确认框。任务完成需通过目标文本、状态或其他业务证据明确读回。历史工具的 effect 字段语义和恢复方式保留在独立源码版本中。

迁移时在 kernel 退出后复制整个数据目录及 SQLite 的 WAL/SHM 文件，保留原目录作为恢复点。[迁移与恢复](MIGRATION-0.5.md)

## 五、历史版本（0.4.5–0.4.7）内置过遥测

**当前版本（0.4.8 起）不包含遥测。** 但 0.4.5、0.4.6、0.4.7 三个发布 tag 里存在过一个
`kernel/dsh_desktop/telemetry.py`，需要对历史安装负责地说明：

| 项目 | 事实 |
|---|---|
| 目标地址 | `https://telemetry.gaott.top/event`，**第三方域名**，不在本项目的官方基础设施内 |
| 鉴权 | 硬编码的 `dsh_desktop-write-token-2026`（写令牌公开在源码里，任何人都能向该端点提交事件） |
| 发送内容 | `install_id`（持久 UUID，存于 `~/install_id`）、事件名、版本、OS/arch、时间戳；部分事件附带 `tool_name`、`success`、`error_type`、`error_detail`（**异常消息前 300 字符**）、`tool_call_count`、`duration_seconds`、`model_provider`、`features` 等 |
| 不含 | 截图、剪贴板、按键、API Key、会话正文 |
| 默认状态 | 模块级 `enabled = True`；`config.telemetry_enabled` 默认 `False`，但**只有独立入口 `__main__.py` 会把它写回模块**（`__main__.py:290`）。DSH 插件跑的是 `python -m dsh_desktop.dsh_bridge_cli`，该入口从不赋值，因此**走插件路径时默认是开启的**，配置里的关闭项不生效 |
| 关闭方式（历史版本） | 只能改代码后自行构建；没有对外暴露的可靠开关 |
| 移除 | `b3a8c27 refactor: drop the usage telemetry module and all its call sites`，首个干净的发布版是 **0.4.8** |

风险评估：

- **不是后门或远程控制**：单向 HTTPS POST（`urllib`，5 秒超时，后台线程，失败静默），没有任何入站通道，也无法影响本机的桌面操作能力。
- **主要风险是隐私与信任**：一个能控制整台电脑的插件，默认向第三方上报带持久 `install_id` 的使用元数据；`error_detail` 可能夹带文件路径、窗口标题等片段。
- **写令牌公开**意味着事件可被伪造/污染，但不构成读取他人数据的通道。

建议：安装 0.4.8 及以上（或直接 `#main`）；历史版本请升级，不要仅依赖配置里的开关。
