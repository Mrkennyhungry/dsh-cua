# 快速上手 / Quick start

[中文介绍](README.md) · [English installation reference](README.en.md#install-the-plugin)

需要 **Windows 10/11、Node.js >= 22 和 DeepSeek Harness**，以及已加入 PATH 的 [`uv`](https://docs.astral.sh/uv/getting-started/installation/)。首次安装 Python 依赖需要联网；OCR 和图标检测权重已随仓库分发。

仓库名是 `dsh-cua`；安装包名为 **`dsh-doraemon`**。当前主分支不限定 DSH 客户端版本，实际可用性取决于客户端提供的插件 API。

**1. 在 DSH 的 Plugins → Add plugin 中粘贴：**

```text
dsh-doraemon@git+https://github.com/Mrkennyhungry/dsh-cua.git#main
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

## 安装后自检

- `doraemon_health` 返回 `status: ok`：桥接和内核已启动。
- `doraemon_analyze` 在绑定窗口返回非空 `ui_elements`：OCR/图标解析可用。
- `doraemon_web` 能打开网页：浏览器通道可用。

如果看到 `Run the bundled scripts/bootstrap.ps1 or set repoRoot/pythonPath`，先检查是否在正确目录完成了第 3 步。

## 可选配置与限制

- 图标 caption 复用 DSH 的 DeepSeek Key；本地 OCR/图标检测与云端 caption 的边界见[数据边界说明](docs/data-boundary.md)。
- 默认使用已安装的 Microsoft Edge；新机器需要重新登录网站。
- 需要操作管理员程序时，另见[高权限配置](README.en.md#first-run-on-a-new-machine)。
- `effect: "confirmed"` 只表示界面变化，任务是否成功需要明确读回。

插件市场、固定提交安装及更多配置见[完整安装参考](README.en.md#install-the-plugin)。
