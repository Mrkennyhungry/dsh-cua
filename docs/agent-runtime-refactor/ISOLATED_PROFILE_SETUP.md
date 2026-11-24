# 独立验证环境（0.4.18）操作指南

目的：让 codex 在 `C:\Users\kennyyzhang\Documents\Codex\dsh-doraemon-refactor`（worktree，
分支 `codex/agent-runtime-refactor`，0.4.18）里改的代码，能由一个**独立内核**加载并验证，
而不是连到当前 DSH 正在用的 0.4.14 内核。

---

## 为什么需要独立环境

| 事实 | 说明 |
|---|---|
| DSH `desktop` profile 的 `doraemon` bundle | junction 指向 `...\plugins\dsh-doraemon`（main / 0.4.14） |
| 当前内核 | pid 42760，加载 0.4.14 的 `kernel/` |
| worktree | 有 0.4.18 源码，但**没有 `.runtime`**（venv / weights / state） |
| `kernel.js` layout() | `repoRoot` 或 `DSH_DESKTOP_REPO` 决定内核源码根；`pythonPath` / `homeDir` / `stateDir` 可配；`port` 默认 8766 |

如果两个内核共用一个 `kernel-state.json` 和 8766 端口，会互相顶掉。所以必须**独立 profile +
独立 port + 独立 stateDir**。

---

## 当前 desktop profile 的 doraemon 配置（参考，勿直接照抄到生产）

```yaml
- id: doraemon
  name: dsh-doraemon
  config:
    pythonPath: C:/Users/kennyyzhang/Documents/deepseek-harness/default-workspace/plugins/dsh-doraemon/.runtime/venv/Scripts/python.exe
    homeDir: C:/Users/kennyyzhang/Documents/deepseek-harness/default-workspace/plugins/dsh-doraemon/.runtime
    stateDir: .runtime/state
    companionSelection: "[]"
```

新环境需要覆盖 `pythonPath` / `homeDir` / `stateDir` / `port` / `repoRoot`，指向 worktree。

---

## 操作步骤

### 1. 给 worktree 准备运行依赖（bootstrap）

⚠️ `scripts/bootstrap.ps1` 的 `-InstallDir` **默认是 `%LOCALAPPDATA%\dsh-desktop`**（全局共享，
当前 0.4.14 内核就用它）。直接跑会覆盖共享 venv / weights，可能影响正在运行的 DSH。
两个安全选项：

**选项 A（更简单，推荐先试）——复用共享 venv，只用 repoRoot 指向新源码：**
不动 bootstrap。新 profile 里 `pythonPath` 指向共享 venv：
```
C:/Users/kennyyzhang/AppData/Local/dsh-desktop/venv/Scripts/python.exe
```
同时把 `repoRoot` 设为 worktree 路径 —— 内核解释器共用，但加载的 `kernel/` 是 0.4.18 的。
注意：如果 0.4.18 改了 `requirements.txt`，共享 venv 可能缺新依赖，届时再走选项 B。

**选项 B（完全隔离）——用 `-InstallDir` 建私有 venv：**
```powershell
cd C:\Users\kennyyzhang\Documents\Codex\dsh-doraemon-refactor
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/bootstrap.ps1 `
  -InstallDir "C:\Users\kennyyzhang\Documents\Codex\dsh-doraemon-refactor\.runtime\install"
```
这会在 worktree 私有目录建 venv / weights（gitignored，不影响提交）。`pythonPath` / `homeDir`
就指向这个 `.runtime\install`。需要本机有 `uv`（脚本硬性要求）。

### 2. 创建一个独立 profile

profile 放在 `C:\Users\kennyyzhang\.dsh\profiles\<名字>`。用 DSH CLI 从 web 模板派生一个：

```powershell
$dsh='C:\Users\kennyyzhang\AppData\Local\Programs\DeepSeek Harness\resources\runtime\cli\bin\dsh.cmd'
# 从 web shipped 模板创建/重建 cua-refactor profile
& $dsh --profile cua-refactor --from-default-profile web
```

> 语法已实测：`& $dsh --profile cua-refactor --from-default-profile web --dump-config`
> 能打印出完整配置（加 `--dump-config` 只打印不落盘，用来预览）。
> 不带 `--dump-config` 执行一次即真正创建 profile。
> 也可以用 GUI 新建 profile，效果等价。

### 3. 把 worktree 作为插件装进该 profile

**推荐（GUI）**：打开 DSH → 该 profile → **Plugins → Add plugin** → 选目录：
```
C:\Users\kennyyzhang\Documents\Codex\dsh-doraemon-refactor
```
（文档里 `plugin_manager install_bundle` 是等价做法；`dsh plugin add` 是 pnpm 转发，
适合 npm 包，local directory bundle 用 GUI 更稳。）

### 4. 在新 profile 里覆盖 doraemon 配置

关键：覆盖 `pythonPath` / `homeDir` / `stateDir` / `port` / `repoRoot`，否则新插件会
去连主目录的内核和 8766 端口。用 DSH 设置界面（Plugins → doraemon → 配置）填写：

选项 A（复用共享 venv）：
```yaml
# 注意：profile patch 的 config 会**整体替换** bundle 的默认 config，
# 所以要把 bundle patch 里的默认项一并带上（tokenEnv/autoStart 等）。
tokenEnv: DSH_DESKTOP_BRIDGE_TOKEN
waitSeconds: 600
autoStart: true
bootSeconds: 120
keepAlive: true
repoRoot:   C:/Users/kennyyzhang/Documents/Codex/dsh-doraemon-refactor
pythonPath: C:/Users/kennyyzhang/AppData/Local/dsh-desktop/venv/Scripts/python.exe
homeDir:    C:/Users/kennyyzhang/AppData/Local/dsh-desktop
stateDir:   C:/Users/kennyyzhang/Documents/Codex/dsh-doraemon-refactor/.runtime/state-refactor
port:       8767                           # 与 8766 隔离
```

选项 B（完全隔离，需先跑 bootstrap -InstallDir .runtime\install）：
```yaml
repoRoot:   C:/Users/kennyyzhang/Documents/Codex/dsh-doraemon-refactor
pythonPath: C:/Users/kennyyzhang/Documents/Codex/dsh-doraemon-refactor/.runtime/install/venv/Scripts/python.exe
homeDir:    C:/Users/kennyyzhang/Documents/Codex/dsh-doraemon-refactor/.runtime/install
stateDir:   C:/Users/kennyyzhang/Documents/Codex/dsh-doraemon-refactor/.runtime/state-refactor
port:       8767
```

> `stateDir` 写绝对路径更稳妥（`layout()` 里 `stateOverride` 会 `resolve(root, value)`，
> 相对值会落到 worktree 下，也是隔离的，但绝对路径不会受 root 变化影响）。

> 不要手改 `~/.dsh/profiles/cua-refactor/cordis.patch.yml`（AGENTS.md 禁止）。
> 用 DSH 的插件配置编辑器或 `configEditor` 保存。

### 5. 新开一个 DSH 会话验证

```powershell
& $dsh --profile cua-refactor web --no-open --port 0
```

在新会话里确认：
- 工具列表只出现 `doraemon_open/observe/act/wait/status/stop` 六个；
- `doraemon_status` 独立回答，且报告的 build/版本来自 0.4.18 分支；
- `doraemon_health` 的 `bridge.pid` 是新内核（不是 42760），`port` 是 8767。

这一步就是 P5 完成条件里的「新 DSH 会话安装验证」，也满足 P7 的两模型验收起点。

---

## 给 codex 的分工

- **codex 可自主做**：worktree 里的代码改动、单元测试（Node/Python）、内核级探针
  （P1/P4 的 `runtime_probe.py` / `actions_probe.py`）。这些不需要新 profile。
- **需要你配合做**：上面第 1–5 步（bootstrap、建 profile、装 bundle、配端口/路径、开新会话）。
  特别是 Host 六工具发现、图片 renderer 进模型请求，只能在独立 profile 的新会话里验。
- 提示词里应把这条列为**验收前置条件**，让 codex 到 P5/P7 时明确提醒你，而不是
  用 0.4.14 的 `doraemon_*` 工具假装验过。

---

## 注意

- 旧 profile `desktop` 和它的 0.4.14 内核全程不动，两套环境互不影响。
- worktree 是共享仓库的独立工作目录：codex 在里面的 commit 进 `codex/agent-runtime-refactor`
  分支；`git push origin codex/agent-runtime-refactor` 后家里/这台机器同步。
- 版本不一致遗留：`package.json` 是 0.4.18、`kernel/dsh_desktop/version.py` 还是 0.4.14，
  发版前必须对齐（目标 0.5.0）。
