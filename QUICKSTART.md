# 快速上手

需要 Windows 10/11、Node.js >=22、DeepSeek Harness，以及 PATH 中的 uv。

## 安装与自检

1. 在 DSH Plugins → Add plugin 中安装 dsh-doraemon。开发版本可使用本地目录或打包文件，安装由插件管理器处理。
2. 在插件目录的 PowerShell 中执行基础安装。

```powershell
.\scripts\bootstrap.ps1
```

3. 重载插件并新建 DSH 会话，检查以下六个工具可见。

```text
doraemon_open
doraemon_observe
doraemon_act
doraemon_wait
doraemon_status
doraemon_stop
```

4. 调用 doraemon_status，确认 success=true 和插件版本。打开测试网页或记事本，读取初始观察，再输入内容并核对读回。

DSH 模型设置决定提供方和模型。open 返回初始观察，act 返回动作后的观察，图片通过附件进入模型输入。慢操作返回 operation_id，可用 status 跟进、stop 取消。

## 可选视觉

```powershell
.\scripts\bootstrap.ps1 -Vision
.\scripts\bootstrap.ps1 -Vision -WeightsDir 'C:\existing-models\weights'
```

WeightsDir 应包含 icon_detect\model.onnx。基础安装即可使用 DOM、UIA、截图和输入。视觉后端缺失时返回诊断，安装后可重新观察。默认 caption 请求数为 0。

## 既有安装

默认 Python home 为 %LOCALAPPDATA%\dsh-desktop，stateDir 为 %USERPROFILE%\.dsh\desktop。浏览器继续复用已有 homeDir\web_profile_edge 或 web_profile，新的 profile 位于 stateDir\browser。登录和站点设置随 profile 保留。

首次登录、验证码和登录失效由用户在专用浏览器处理。commandFeedback 在插件配置界面开启，选择器还需构建 companion。

遇到启动提示 Run the bundled scripts/bootstrap.ps1 or set repoRoot/pythonPath，检查基础安装和插件路径。版本或能力握手不匹配时按提示重载旧内核并创建新会话。

旧入口、数据目录、备份和恢复步骤见迁移说明。[0.5 迁移](docs/MIGRATION-0.5.md) · [数据边界](docs/data-boundary.md) · [English](README.en.md)
