# DSH CUA — Windows 桌面自动化插件

**简体中文** | [English](README.en.md)

让 **DeepSeek Harness（DSH）操作 Windows 桌面应用与浏览器**。
结合 **UIA 控件树、本地 OCR、图标检测、窗口绑定和视觉语义识别**，为 AI Agent 提供观察界面、点击输入、跨软件操作和结果读回能力。

Windows computer-use plugin for DeepSeek Harness, combining UI Automation (UIA), local OCR, window binding, browser automation and visual grounding.

**[观看完整实测：QQ 音乐 → Steam → B 站 → QQ](https://www.bilibili.com/video/BV1L9xe6sEDi/)**

[快速安装](#快速安装) · [完整安装参考（English）](README.en.md#install-the-plugin) · [数据边界](docs/data-boundary.md) · [反馈问题](https://github.com/Mrkennyhungry/dsh-cua/issues)

## 实测：一句话，完成多个软件里的任务

> 打开 QQ 音乐播放《无敌》；去 Steam 家庭库找《影之刃零》，有就开始下载；再去 B 站找相关教程，把链接通过 QQ 发给我自己，顺便留句话。

这次任务约 **六分半，花费 0.36 元**（作者本次运行记录，非通用性能基准）。演示保留了判断与纠错：识别 Steam 界面、根据磁盘剩余空间选择安装位置、处理独立协议窗口、筛选教程并发送链接。

下载启动后，Agent 继续搜索教程和发送消息；不需要等待整个游戏下载完成。演示中的“完成下载任务”指**已开始下载**。

## 插件提供什么能力

| 能力 | 用途 |
|---|---|
| Windows 窗口枚举与绑定 | 获取目标窗口句柄，把后续观察和操作关联到窗口 |
| UI Automation（UIA） | 读取原生控件的名称、类型、值和位置 |
| 本地 OCR 与图标检测 | 用 RapidOCR、YOLO 补充文字、图标位置等信息 |
| 视觉语义识别 | 为无文字图标补充描述，辅助定位 |
| 桌面输入 | 鼠标点击、键盘输入、快捷键、滚动与拖拽 |
| 浏览器自动化 | 借助 Playwright/DOM 读取网页、操作页面 |
| 状态读回与任务反馈 | 检查操作后的界面状态，显示任务提示 |

## 如何执行：观察 → 操作 → 读回

1. 获取并绑定目标窗口，确定操作对象。
2. 根据任务读取 UIA 控件或浏览器 DOM；信息不足时使用本地 OCR、图标检测及视觉能力。
3. 执行点击、输入或滚动，重新观察目标状态；结果不明确时先检查，再决定下一步。

这些能力按任务选择，并非每次点击都调用全部识别流程。目标是减少不必要的视觉调用和模型往返；实际速度、费用与准确性取决于模型、应用和任务。

**视觉边界：**图标 caption 开启且有可用 Key 时，会向 DeepSeek 接口发送**标注截图和图标裁图**，并非只传局部裁图。显式读取图片也可能把图像交给 DSH 配置的模型。本地运行的是 OCR 与图标检测，不能将整个插件描述为完全离线。详见[数据边界](docs/data-boundary.md)。

## 快速安装

需要 **Windows 10/11、Node.js >= 22、DSH `>=0.2.1-rc.2 <0.3.0`**，以及已加入 PATH 的 [`uv`](https://docs.astral.sh/uv/getting-started/installation/)。首次安装 Python 依赖需要联网；OCR 和图标检测权重已随仓库分发。

仓库名是 `dsh-cua`；安装包名为 **`dsh-doraemon`**，下列安装命令使用的是包名。

**1. 在 DSH 的 Plugins → Add plugin 中粘贴：**

```text
dsh-doraemon@git+https://github.com/Mrkennyhungry/dsh-cua.git#v0.4.6
```

**2. 重启 DSH。**

**3. 在安装包目录中打开 PowerShell，运行一次：**

```powershell
cd "$env:USERPROFILE\.dsh\profiles\<your-profile>\node_modules\dsh-doraemon"
.\scripts\bootstrap.ps1
```

将 `<your-profile>` 替换为自己的 profile 名称；自定义目录请使用实际安装路径。脚本创建 Python 3.11 环境、安装依赖并复制模型权重。**只安装插件不运行 bootstrap，内核无法启动。**

**4. 再次重启 DSH，尝试一个简单任务：**

> 用 doraemon 打开记事本，写一行 hello，然后把窗口标题读回来。

首次配置、插件市场安装、API Key 与常见问题见 [QUICKSTART.md](QUICKSTART.md) 和 [完整安装参考](README.en.md#install-the-plugin)。插件复用 DSH 中配置的 DeepSeek Key；浏览器默认使用已安装的 Microsoft Edge。

## 使用限制与结果判断

- 插件依赖 DeepSeek Harness，不是独立运行的桌面助手。
- 桌面坐标操作使用真实鼠标；前台焦点、自绘界面和管理员窗口会影响操作，不能保证所有 Windows 软件都可自动化。
- 插件在本机执行真实操作，不是隔离沙箱。首次使用建议从记事本等简单任务开始；文件删除、搬家等操作应先备份并确认。
- `effect: "confirmed"` 只说明界面发生了变化。业务是否成功，需要根据窗口内容、下载状态、消息记录等明确证据判断。
- 当前主分支的任务反馈角色是固定停靠、鼠标穿透的像素鲸鱼女仆；它不是虚拟鼠标。详见 [commander mode](docs/commander-mode.md)。

## 反馈与参与

欢迎在 [Issues](https://github.com/Mrkennyhungry/dsh-cua/issues) 提交反馈：请写明 Windows/DSH/插件版本、目标软件、复现步骤和实际结果；截图与日志请先去掉隐私信息和凭据。

如果项目对你有帮助，欢迎点一个 Star，方便后续找到它。

本项目使用 [MIT License](LICENSE)。
