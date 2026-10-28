# 数据边界与效果表述（dsh-doraemon）

本文回答两个问题：**什么会离开这台机器**，以及**"成功"到底证明了什么**。
结论都对应到具体代码位置，改代码后请同步本文。

## 一、什么会离开本机

| 功能 | 是否外发 | 目标 | 触发条件 | 关闭方式 |
|---|---|---|---|---|
| OCR 文字识别（RapidOCR） | 否 | 本地 ONNX 模型（`.runtime/weights/rapidocr`） | 每次 `desktop_analyze` / uia 回退 | — |
| 图标检测（YOLO） | 否 | 本地 ONNX（`.runtime/weights/icon_detect`） | 每次 `desktop_analyze` | — |
| 图标语义 caption | **是** | `https://api.deepseek.com/chat/completions`，`model: deepseek-flash` | `desktop_analyze` 且 `caption=true`，**且**环境变量 `DEEPSEEK_API_KEY` 非空 | 让 `DEEPSEEK_API_KEY` 为空；或调用时 `caption=false` |
| `desktop_read_image` | **是** | 由 DSH 侧配置的视觉模型端点决定（插件只返回 base64） | 显式调用该工具 | 不调用 |
| 受控浏览器 `web_*` | 否（本机 Chromium） | — | — | — |
| 网页 `web_extract` 的正文 | 由调用方模型决定 | 同上 | 调用后把文本交给模型 | 不调用 |

关键事实：

1. **caption 是唯一一处"把画面送出去"的地方**。发送内容是被检测图标标注过的整帧截图 + 若干图标裁图拼贴，`deepseek_caption.py:104`；不含桌面其它窗口的裁剪（整帧按被测窗口客户区截取）。
2. 未配置 `DEEPSEEK_API_KEY` 时，`deepseek_caption.caption_icons()` 直接返回 `deepseek_called: false`，**不会发起任何网络请求**（`deepseek_caption.py:167-168`）。此时"纯本地"成立。
3. 截图本身只落盘在 `.runtime/screenshots/<date>/`，不自动上传。
4. `kernel-boot.json` 以**明文**保存 bridge token，以及（存在时）`DEEPSEEK_API_KEY`。权限已收紧为：当前用户 + `SYSTEM` + `Administrators`（`kernel.js` 写入后用 `icacls /inheritance:r` 重设）。它必须是明文——高权限计划任务要读它传给内核。

## 二、CDP 端口

- 受控浏览器以 `--remote-debugging-port=9333` 启动（`web_tools.py:295`），未指定 `--remote-debugging-address`，因此 Chromium 默认**只绑定 127.0.0.1**。
- 这**不是**安全隔离：CDP 本身没有认证，**本机任何进程**都能附着并驱动这个浏览器（含已登录的会话 Cookie）。插件只保证"不对外网暴露"，不保证"本机其它程序不能用"。
- 若需彻底隔离，应在独立 Windows 用户下运行，或改用不保留登录态的临时 profile。

## 三、"成功"到底证明了什么

工具返回值混用了三种不同强度的结论，读的时候必须区分：

| 等级 | 含义 | 现实中的证据 |
|---|---|---|
| **动作已发送** | 输入事件已递交给目标窗口 | `desktop_click` / `desktop_type` 的 `action_performed: true` |
| **界面已变化** | 像素或 UIA 状态相对动作前发生了变化 | `effect: "confirmed"`、`visual_delta`、`window_closed`、`windows_added` |
| **业务结果已验证** | 用可判读的读回证明了目标状态（列表出现该行、数值等于预期、文件内容正确） | 只有显式读回才有：`text_readback`、`desktop_uia` 命中后读到的文本、`web_extract` 的结果 |

必须注意：

- `effect: "confirmed"` **不等于业务成功**。它只说明界面动过——一个弹窗、一次错误的跳转也会让像素变化。
- `desktop_type` 的 `effect_verified: false` 是**故意的**：它明确告诉你"还没核对"。
- `desktop_launch_app` 的 `success: true` 只代表"观察到可归属的新窗口"，不代表应用进入可用状态。
- `web_click` 在点击已发出但随后读取状态失败时返回 `RESULT_UNKNOWN`，这是**未知**，不是失败也不是成功；正确做法是重新 `web_snapshot` 观察，而不是重试点击。
- 清理报告 `cleanup_success: false` 时，可能只是"目标窗口没关掉"（例如它弹了保存确认框），**不代表数据损坏**。

## 四、未验证的部分（不要当成已保证）

- 高权限（High integrity）启动链路用的是计划任务 + 明文 boot 文件，**这不是安全边界**，只是为了让受控应用与内核同身份，避免 UIPI 拦截。
- `DSH_DESKTOP_LIVE` 相关的手工验证依赖真实桌面会话，CI 不覆盖。
