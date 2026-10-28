---
name: desktop-actions
description: "Desktop 桌面自动化动作全集。Triggers: 发送文件, 上传文件, 粘贴文件, 剪贴板, 复制粘贴, setclipboard, 右键, 拖拽, 滑动, swipe, 滚动, 控件, uia, 控件树, omniparser, 启动应用, 激活窗口, 腾讯文档, 企业微信, 桌面操作"
tags: [desktop, desktop-automation, windows, dlp]
platforms: [windows]
---

# Desktop 桌面动作全集

桌面自动化动作能力（launch / activate / setclipboard / paste file / swipe / UIA 控件…）
已封装为 `desktop_*` 工具。本技能是它们的**使用说明书**：按场景选对动作。

## 坐标系（全部统一）

- 所有坐标均为窗口内归一化 **0–1000**（左上 (0,0)，右下 (1000,1000)），基准为窗口客户区。
- `desktop_analyze`（本地视觉解析）与 `desktop_uia_tree`（UIA 控件树）返回的 bbox 与
  `desktop_click` / `desktop_right_click` / `desktop_swipe` / `desktop_scroll` 使用**同一坐标系**，可直接互换。
- `desktop_analyze.width/height` 是返回的 `image_path` 实际像素尺寸（截图可能缩至 1366）；`source_width/source_height` 是缩放前的窗口客户区像素尺寸。桌面分辨率、原始尺寸、保存图像素和窗口归一化 bbox 是不同坐标系，不要混用。
- 优先从最新 bbox 算中心 `x=round((x1+x2)/2), y=round((y1+y2)/2)` 并直接交给 `desktop_click`。如果位置是按保存截图的像素读出的，使用 `desktop_click_image_point(window_id, image_x, image_y, image_width, image_height)`；不要把截图像素直接传给归一化 `desktop_click`。看不清或框太窄时用 `desktop_uia_tree` 核对；点击后重新分析确认是否命中。
- 正常桌面操作保留 `desktop_analyze` 默认的 `annotate=true`，供界面显示解析框；只有用户要干净截图或确有具体原因时才设 `annotate=false`。模型 `read_image` 读取的仍是干净图。
- Qwen 图标 caption 是语义提示，不保证正确。点击重要图标前若 caption 与周边文字/布局不符，先用 `desktop_uia_tree` 或重新观察较清晰截图核实，不能单凭 caption 猜点击。


## 1. 感知（先看清楚再动手）

| 工具 | 用途 |
| --- | --- |
| `desktop_analyze(window_id, engine="omni", caption=True, caption_icons=False, annotate=True)` | 截图 → 远端 OmniParser（元素+文本，bbox 0-1000）。快速核对可用 `caption=False` 跳过 Florence 图标描述；`caption_icons=True` 对小图标做 Qwen3-VL 语义二次校正，结果在元素 `caption` 字段；`annotate=True` 额外生成元素标注图（彩色框+红十字鼠标位置，聊天界面优先显示，便于人工核对），置 false 只回干净图 |
| `desktop_uia_tree(window_id, max_depth=7, max_nodes=120, target_hint="", target_kind="control", max_attempts=3)` | 读 Windows UIA 控件树：真实控件 `control_type` / `name` / `auto_id` / bbox；返回截断、原因及 UIA 覆盖摘要 |

用法：标准控件使用 `desktop_uia_tree(window_id, target_hint="按钮名", target_kind="control", max_attempts=3)`；核对聊天消息/文本用 `target_kind="message"`；已知自绘页面用 `target_kind="visual"`。目标导向模式在一次工具调用内按 +5 渐进深度（最多三次），节点上限截断则扩节点；命中即停，目标缺失时回退 OmniParser。`message_like_candidate_count` 只是 UIA 树内容覆盖的参考，不代表消息送达。对连续观测为 `sparse` 的应用消息区域，不再反复扩深，直接走视觉解析。OmniParser 主解析（YOLO/OCR/Florence）拿“看见什么”和候选框，控件树拿“确切控件名/类型”；两者互相验证。Qwen 图标复核是独立的二阶段能力。注意：`desktop_analyze` 主解析会向 `/parse` 显式发送 `caption` 和所选 `engine` query，并保留服务返回的元素 caption、`engine_used`、`fallback`、`service_proxy_called` 诊断信息。初次完整观察用默认 `caption=true`；组合观察的视觉回退用 `caption=false`；只在特定小图标确实看不清时才额外设 `caption_icons=true` 调用 Qwen 二次服务。




**避免重复等待**：每次有效点击/提交后做一次快速核对即可；若截图/布局没有状态变化，不要连续重复完整 `desktop_analyze`。优先用 `desktop_uia_tree` 确认标准输入框/控件；只有需要视觉确认、UIA 不可用或画面可能变化时再截图。动作尚未呈现结果时，先短暂等待后核对一次，不要密集轮询。`caption_icons` 参数默认关闭，表示额外再请求 `/caption_icons` 二次校正；当图标语义仍不清或 caption 缺失时才显式开启，并检查 `caption_service_proxy_called` / `icon_caption_error`。服务仅有 `/parse` 时，主解析可用，但独立 `/caption_icons` 能力需服务端支持。

## 2. 窗口与应用管理

| 工具 | 对应桌面自动化动作 | 用途 |
| --- | --- | --- |
| `desktop_list_windows` | list_windows | 列出可见窗口（title/exe/pid） |
| `desktop_find_app(name)` | —（探测） | 应用不在窗口列表时探测：进程/快捷方式/注册表卸载项/系统内置 exe，返回 verdict 与建议 |
| `desktop_launch_app(shortcut_path)` | launch / activate | 启动已安装应用（开始菜单/桌面 .lnk 或系统 exe），启动后 `desktop_list_windows` 等窗口 |
| `desktop_pick_window(hwnd)` | bind_window | 绑定窗口（可绑多个） |
| `desktop_switch_window(window_id, wait=1)` | **activate** | **激活窗口——独立的前置动作**，流程开头调用；懒加载应用 wait=2-3 等控件树展开 |

流程：窗口里没有 → `desktop_find_app` → `installed_not_running` 就 `desktop_launch_app` →
`likely_not_installed` 才考虑网页版（并告知用户）。

## 3. 输入动作

| 工具 | 对应桌面自动化动作 | 说明 |
| --- | --- | --- |
| `desktop_click(window_id, x, y, clicks=1)` | click / double_click | 左键单击；`clicks=2` 双击 |
| `desktop_right_click(window_id, x, y)` | right_click | 右键弹上下文菜单 |
| `desktop_swipe(window_id, x1, y1, x2, y2, speed=1.0)` | drag / swipe | 按住拖拽（滑块、拖拽上传、选区） |
| `desktop_scroll(window_id, x, y, clicks, direction)` | wheel | 滚轮，clicks 负值向下 |
| `desktop_type_text(window_id, text)` | type_text(paste) | 点击输入框后输入（剪贴板粘贴，中文安全）。零激活，非前台拒绝 |
| `desktop_type_keys(window_id, text)` | type(SendInput) | **逐字符键入英文/数字/符号，零窗口激活**——下拉搜索框过滤场景专用。非前台拒绝；被输入法吞字母时先让用户切英文 |
| `desktop_press_key(window_id, key)` | press | 单键：enter/tab/escape/backspace/delete/home/end/pageup/pagedown/方向键。零激活，非前台拒绝 |

**组合键规则**：优先使用 `Ctrl+C / Ctrl+V / Ctrl+A`。`desktop_press_key` 校验格式并拒绝
危险组合键；旧的 `controller.hotkey` 接口白名单更严格。拒绝后不得换工具绕过。
打开菜单、新建文档等优先点击 UI 元素。

**发送顺序与验证**：聚焦目标输入框 → 粘贴 → 核对草稿 → 单次 Enter → 核对目标会话中的消息。
这些动作有前后依赖，禁止并行提交。`desktop_type_text` 已完成一次粘贴，不会清空已有草稿，
不得再补 Ctrl+V。工具返回成功只证明输入动作已调用，不代表消息送达。
粘贴成功但 Enter 被拒绝时先检查草稿，只重试未完成动作；发送结果不明时核对历史，禁止盲目重发。
输入文本单次最多 2000 字符；分片必须明确编号，不能把分片数冒充已完成记录数。

**焦点规则（对齐 external_agent buddy 实战设计）**：
- `desktop_switch_window` 是**独立的激活动作**——放在流程开头（或切换窗口时），懒加载应用 `wait=2-3`
- 之后的 click/press/type **零激活**：已前台窗口不做任何 ShowWindow/SetWindowPos，连续动作不扰动焦点，弹层不会被震掉
- 键盘类动作（press_key / type_text / type_keys / paste_clipboard_file）在目标**非前台时直接拒绝**（防打错窗口），按提示先激活再重试
- 采集类动作（desktop_analyze / desktop_uia_tree）自动带前台（Electron 懒加载 UIA 树需要），已前台时零扰动

下拉过滤类控件的正确顺序：`desktop_click` 点开展开（真实点击让焦点落到搜索框）→
`desktop_type_keys` 直接键入关键词 → `desktop_press_key("enter")` 确认。
若键入的字母没出现在框里，多半是**中文输入法在吞键**——请用户切英文输入，或改用 `desktop_type_text` 粘贴。

## 4. 剪贴板（剪贴板与文件传输核心）

| 工具 | 对应桌面自动化动作 | 说明 |
| --- | --- | --- |
| `desktop_set_clipboard_text(text)` | set_clipboard_text | 只放文本进剪贴板，不粘贴 |
| `desktop_get_clipboard_text()` | get_clipboard | 读剪贴板，验证复制结果 |
| `desktop_paste_clipboard_file(window_id, path, paths=[])` | set_clipboard_file + 粘贴 | **CF_HDROP 放文件 + 自动 Ctrl+V**，往聊天/邮件输入框发文件的标准动作 |

### 发送文件标准流程（高频 SOP）

1. `desktop_find_app("目标应用")` → 未运行则 `desktop_launch_app` → `desktop_pick_window`
2. `desktop_analyze` 找到会话输入区 / 附件按钮 → `desktop_click` 点输入框
3. `desktop_paste_clipboard_file(window_id, path=r"C:\Users\...\file.xlsx")`
   —— 它已内置粘贴，**之后不要再按 Ctrl+V**（会重复粘贴，历史错题）
4. `desktop_analyze` 确认文件缩略图出现在输入区 → 点发送按钮
5. 多文件用 `paths=[...]` 一次粘贴，不要循环多次粘贴

文本同理：`desktop_set_clipboard_text` + 点输入框 + Ctrl+V，或直接 `desktop_type_text`。

## 5. 受限文件操作

`desktop_create_file / desktop_write_file / desktop_delete_file / desktop_list_my_files`：
只能在工作区新建、改写、删除**本会话自己创建**的文件。桌面已有文件只读不删。
这些文件可直接作为 `desktop_paste_clipboard_file` 的发送源。

## 6. 知识库

`desktop_search_kb(query, source)`：`corrections` 错题本（先读教训）、`success_paths`
成功路径（照着走）、`kb` 业务知识（desktop 控制台元素、playbooks、任务知识）。
会话开始已自动注入相关条目；陌生场景先查一次再动手。
