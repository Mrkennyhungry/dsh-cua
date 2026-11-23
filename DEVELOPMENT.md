# DEVELOPMENT.md — 开发流程规范

本文件规定 `dsh-cua` 的开发流程。它约束**人和 AI 编码代理**双方。

> 背景：规范驱动开发（SDD）约束"往哪做"，评测驱动开发（EDD）约束"做得对不对"。
> 本项目已有 `docs/agent-runtime-refactor/` 作为 SDD 产物、`ACCEPTANCE.md` 作为 EDD 产物。
> 本文件补的是**把两者串起来的闸门**——没有闸门，AI 会一路往前冲，最后交付一个无法验证的整体。

---

## 1. 分支模型

| 分支 | 用途 | 规则 |
|---|---|---|
| `main` | 可发布状态 | 不直接提交；只接受通过验收的合并 |
| `codex/<主题>` | 功能/重构分支 | 当前重构使用 `codex/agent-runtime-refactor` |

- 开工前先 `git status` 和 `git log --oneline -5`，确认分支与起点。
- 工作树里已有的未跟踪文件（如 `AGENTS.md`、`docs/`）属于本轮交付的一部分，**不要 `git clean`**。
- 合并前 `main` 必须能通过一次完整检查（§6）。

## 2. 提交规范（Conventional Commits）

```
<type>(<scope>): <一句话说明>

<可选正文：为什么这么改>

Phase: P1
```

**type** 只能取：`feat` `fix` `refactor` `test` `docs` `chore` `perf` `build`

**scope** 建议取：`host`（`index.js` 等）`kernel`（Python）`bridge` `ui` `docs` `deps`

示例：

```
feat(host): add doraemon_observe with unified observation contract

Phase: P1

refactor(kernel): move frame coordinate conversion into contracts.py

Phase: P2
```

规则：

- **一个逻辑变更一个 commit。** 不要把三个阶段的改动堆在一个提交里。
- 提交前必须跑过 §6 的检查，且全绿。
- 禁止 `wip` / `update` / `fix bug` / `changes` 这类没有信息量的说明。
- 严禁 `git commit --amend` 已推送的提交，严禁 `git push --force` 到 `main`。

## 3. 阶段闸门（最重要的一条）

`PHASES.md` 的每个阶段必须**同时满足以下三条**，才允许开始下一阶段：

1. **交付项齐了**：该阶段列出的文件/模块/行为都已存在。
2. **验收过了**：`ACCEPTANCE.md` 中对应的验收项全部 PASS，并且**填写了实际结果**（不是打勾，是写实测值）。
3. **已提交**：该阶段的工作有独立 commit，工作树干净。

任一条不满足 → **停在该阶段**，报告阻塞点，不许"先把下一步做了再说"。

> 这条是为了防止最常见的事故：AI 连续做了五个阶段，最后一个测试都跑不过，
> 而你无法判断是从哪一步开始坏的。

## 4. 测试基线

**动手改代码之前，必须先有一个全绿的基线。** 否则改坏了无法区分新旧问题。

首次准备（Python 侧目前缺 pytest）：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip pytest
```

Node 侧：

```powershell
npm test          # node --test test/*.test.js
```

已知问题（起点提交状态）：

- `npm test` 有 16 项通过。
- `plugin.test.js` 因缺少 `@deepseek-ai/schemastery` 无法加载。

处理要求：**要么修好，要么显式 `skip` 并在 `ACCEPTANCE.md` 记录原因**。
不允许"加载失败就当没看见"——那会让后续所有回归测试失去意义。

P0 增加了复用本机已安装 DSH SDK 和 kernel 依赖的测试准备入口。在创建 .venv 并安装 pytest 后执行以下命令。测试依赖复制到项目的忽略目录，DSH peers 继续由宿主提供。

```powershell
node scripts/prepare-test-peers.mjs
pwsh -File scripts/prepare-test-python.ps1
pwsh -File scripts/check.ps1
```

DSH 安装位置可用 DSH_TEST_INSTALLATION 指定。Python 准备脚本支持 -RuntimeDir，要求已有 bootstrap 运行环境和相同 Python 主次版本。pip 缓存卡住时已实测使用 --no-cache-dir --disable-pip-version-check --timeout 15 --retries 0 恢复安装。check.ps1 为 COM 和 pytest 使用独立的 .runtime/test-temp，避免共享 TEMP 的权限冲突。

P1 的独立基础依赖验收可以复现如下。prepare-runtime-test.py 复制本机分发包的基础闭包到 .runtime/runtime-test-venv，并拒绝引入 standalone Agent 或视觉包。--companion 需要已完成 companion 构建。

```powershell
.\.venv\Scripts\python.exe scripts/prepare-runtime-test.py
$env:PYTHONPATH = Join-Path (Get-Location) 'kernel'
$env:TEMP = $env:TMP = Join-Path (Get-Location) '.runtime/test-temp'
.\.runtime\runtime-test-venv\Scripts\python.exe test/runtime_probe.py .runtime/p1-probe-home --live --companion
```

## 5. 版本与 CHANGELOG

P4 的真实输入验证使用本机原有 Python，并加载独立基础依赖目录。项目目录继承 Low Integrity 标签，直接运行其中的 venv 可执行文件会降低进程完整性，实测前台线程连接返回 WinError 5。下列入口沿用已安装解释器的原有权限，执行器与系统标签均保持原值。

```powershell
$env:PYTHONIOENCODING = 'utf-8'
$env:TEMP = $env:TMP = Join-Path (Get-Location) '.runtime/test-temp'
python scripts/run-live-probe.py test/actions_probe.py .runtime/p4-live-home
```

P2 的实机目标、几何与持久 profile 验收使用隔离 home。程序创建并清理自己的窗口、网页和测试浏览器，输出可归档 JSON。

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'kernel'
$env:TEMP = $env:TMP = Join-Path (Get-Location) '.runtime/test-temp'
.\.runtime\runtime-test-venv\Scripts\python.exe test/targets_probe.py .runtime/p2-probe-home
```

- 本轮目标版本 **0.5.0**：工具表从 30+ 收敛到 6 个，属破坏性变更（本项目仍在 0.x，用 minor 表达）。
- 维护 `CHANGELOG.md`，格式用 [Keep a Changelog](https://keepachangelog.com/)：
  `Added` / `Changed` / `Deprecated` / `Removed` / `Fixed` / `Security`。
- **每完成一个可见行为变更就追加一条**，不要等发布前凭记忆补。
- 被迁出的能力（内置 Agent、IM、WeCom、cron、独立 Web UI）必须在 `Removed` 段写明旧入口与数据去向。

## 6. 本地检查脚本

在 `scripts/check.ps1` 提供一键检查，任何一项失败即非零退出：

```powershell
# 语法
node --check index.js
node --check client.js

# 单元测试
npm test

# Python 测试（装了 venv 之后）
if (Test-Path .venv\Scripts\python.exe) {
  .\.venv\Scripts\python.exe -m pytest test -q
}
```

提交前跑一次。没有跑过测试的提交，一律视为未完成。

## 7. AI 编码代理的硬性约束

以下六条不可协商：

1. **不许跳阶段。** 见 §3。
2. **不许没跑测试就 commit。**
3. **不许手改 profile。** `~/.dsh/profiles/<name>/cordis.patch.yml`、profile 的
   `package.json`、`$DSH_PROFILE_DIR/node_modules` 全部由 `plugin_manager install_bundle` 管理。
4. **不许削弱 `AGENTS.md` 里的插件契约。** 六工具名、`ctx.effect` 清理、`exec.signal`
   透传、optional peerDependencies 的包装方式都要保留。
5. **不许把 `@deepseek-ai/cordis` / `dsh-tools` / `schemastery` 写进 `dependencies`。**
   它们由 DSH 提供。
6. **不确定就查证，不要猜。** DSH 的 API、数据格式、系统能力与方案假设不一致时，
   以源码和实测为准，并在文档里记录修正原因。

## 8. 关键决策记录（ADR）

位置：`docs/adr/NNNN-短标题.md`，一件决策一个文件。

只在三种情况写：

- 引入或更换第三方依赖
- 修改公开契约（工具名、参数、返回结构、版本号规则）
- 放弃某个已讨论过的方案

模板（四行就够）：

```markdown
# NNNN. 决策标题
- 状态：已采纳
- 背景：为什么要做这个决定
- 决定：选了什么
- 后果：放弃了什么，代价是什么
```

**只记"为什么"，不记"是什么"**——"是什么"在代码和 PROTOCOL.md 里。

## 9. 参考

- [GitHub Spec Kit](https://github.com/github/spec-kit) — 规格 → 计划 → 任务 → 实现，150K star
- [OpenSpec](https://github.com/Fission-AI/OpenSpec) — 提案 → 应用 → 归档，更轻量
- [Conventional Commits](https://www.conventionalcommits.org/)
- [Keep a Changelog](https://keepachangelog.com/)
- [ADR](https://adr.github.io/)
