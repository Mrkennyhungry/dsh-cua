# watch / toast 观测机制：设计回顾与待决策问题

> 记录时间：2026-11-05
> 仓库：`C:\Users\kennyyzhang\Documents\deepseek-harness\default-workspace\plugins\dsh-doraemon`
> 用途：交给多个 AI 评审，征集替代方案。**本文描述的是一轮尚未收敛的设计，不是定稿。**
> 参考的外部资料（仅作事实参考）：
> - [vLLM semantic-router · Keyword Signal](https://github.com/vllm-project/semantic-router/blob/d534efc3a089f8a38479322d2ed66be9e0274d1b/website/i18n/zh-Hans/docusaurus-plugin-content-docs/version-v0.3/tutorials/signal/heuristic/keyword.md)
> - [Okapi BM25](https://en.wikipedia.org/wiki/Okapi_BM25)

---

## 0. 当前仓库状态（务必先读）

| 项目 | 值 |
|---|---|
| 已推送 HEAD | `ce69686`（`fix(decide): do not treat unknown-type duplicates as a passive collision`） |
| 未提交改动 | **6 个文件**：`index.js`、`kernel/dsh_desktop/desktop_tools.py`、`dsh_bridge.py`、`eternity.py`、`watch.py`、`test/test_observation_watch_20261105.py` |
| 工作区测试 | `229 passed, 1 skipped, 13 subtests`；Python/JS 语法均 OK |
| 内核进程 | pid `18704`（最后几次改动**之后没有重启**；复现前请执行 `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/reload-cursor-kernel.ps1 -StateDir "$PWD\.runtime\state"` 并触发一次 `doraemon_health`） |
| 本文本身 | 新增未跟踪文件 |
| 桌面遗留 | `C:\Users\kennyyzhang\Desktop\dsh-invalid-upload-probe.txt`（测试用不合格文件）、相册管家主窗口仍在运行、`.runtime/desktop_workspace/` 下有几个回放/诊断脚本 |

**结论：下面所有实现都还没提交、没推送。** 评审时请以工作区源码为准，不要只看 HEAD。

---

## 1. 需求（用户原话的归纳）

1. `watch` 定位为**专门的弹窗/瞬时消息监视机制**，不是通用截屏工具。
2. 弹窗可能出现在：
   - 应用窗口内部（底部居中 toast，例如「1 个文件格式不支持，已忽略」）；
   - **整个桌面右下角的系统级通知**，不属于任何绑定窗口；
   - 颜色不固定：深色胶囊、浅色弹窗都可能存在。
3. **采集必须走全屏角度**，否则桌面右下角通知永远拍不到。
4. watch 不是全程常开，是「触发动作 → 观察一段时间 → 读回」。
5. 弹窗基本是文字式的。不要把一堆帧图丢给模型识图（太费 token / 上下文），应复用内核已有的 OCR，把**文字**交出去。
6. 但也不该把全屏 OCR 的全部原文交出去。要用「失败 / 被拦截 / 不支持」这类**预期词**做一轮命中处理，只把相关的交给 AI，由 AI 判断哪条是它要等的消息。
7. 误报不是主要矛盾：**「混进来的里面有符合场景的那条就好」**，宁可多留候选，不要漏。
8. 需要评估：上下文占用、速度（是否很慢）。
9. 附带 bug：右上角吉祥物一直闪烁；用户已把 `commandFeedback` 默认关闭，但它仍然出现。

---

## 2. 设计演进（四轮，逐轮被推翻）

### v1：绑定窗口客户区裁剪 + 变化帧拼图
- 10fps 后台采样，**只抓绑定窗口客户区**（`capture.capture(hwnd, activate=False)`）。
- 双分留帧：`global>=12`（页面切换）或 `local>=8`（top 1% 格子均值，抓小面积 toast）。48 帧环形缓冲。
- `since()` 返回时间序拼图 + 双分时间线。
- **实测**：能抓到 toast（210 帧采样只留 9 帧；`global=0.26 / local=11.0` 那帧就是 toast）。但拼图每帧压到 420 宽，toast 里的字只剩几像素，**读不出来**。
- **被推翻原因**：文档设计-01 第 2 条踩坑（拼图缩略图里 toast 约 60×10 像素）。

### v2：底部暗块检测 + 原分辨率放大裁剪
- 加 `_detect_toast()`：底部 22% 区域找 `R,G,B < 90` 暗块，形态学闭运算合并，取 bbox，保留**原分辨率**裁剪，单独出一张放大拼图。
- **实测**：真机上传不合格 .txt，`toast.detected: true`、5 帧、bbox `[757,1039,1043,1091]`，放大图里**文字清晰可读**：「1 个文件格式不支持，已忽略」，还看到出现/淡出两帧。
- 但混进了 3 个误检（文件对话框的 `All Files (*.*)`、文件名输入框）。我当时加了一条 `fill>=0.55`（要求深色像素占主导）来收敛。
- **被用户否决**：过拟合。"其他软件也会出现弹窗，可能就是浅色的" —— `fill` 判据会直接漏掉浅色弹窗。用户明确：**不要收敛，命中的都交给 AI 判断**。

### v3：全屏采集 + OCR 文字候选
- 用户指出：桌面右下角通知在绑定窗口之外，必须全屏。改为 `capture_desktop()` 优先，窗口裁剪只作 fallback。
- 关键澄清（本轮最重要的一条）：**watch 的职责只是「不漏地保留候选」，判断「是不是我要的弹窗」是 AI 的事**。采集层不该做任何语义判定。
- 于是删掉 `_detect_toast()`，改成纯几何的 `_bottom_band()`：每个留帧固定裁一块区域，**零检测、零过滤、零判定**。
- 复用内核 `UIParser` 对 band 跑 OCR，返回去重后的**文字候选**，图片降级为「OCR 读不清时才看」的兜底证据。
- **实测发现新坑**：全屏 2160 高，底部 22% 只覆盖 78% 以下；而窗口内 toast 在屏幕约 68%，桌面通知在 85–95%，**两种都漏**。改成 `BOTTOM_BAND_FRACTION = 0.45`（下半屏）后能覆盖，但把 DSH 自己的对话窗口、任务栏、桌面图标全拍进来了，OCR 出 100+ 条无关文字。

### v4（当前 WIP）：Keyword Signal 命中筛选
- 搜到 vLLM semantic-router 的 **Keyword Signal**：确定性词法匹配 / BM25 / n-gram，作为"学习型信号之前的低延迟第一道筛选"。项目里 `decide.py` 已有 BM25 与 `tokenize()`（CJK 按字 + 英文按词），直接复用思路，不引依赖。
- 新增 `rank_text_candidates()`：BM25 打分 + **短语精确命中加权**（CJK 按字切分会过宽，所以短语命中给强 bonus）。
- 内置 `DEFAULT_EXPECT_TERMS`（中英混合：失败/错误/不支持/忽略/拦截/拒绝/无法/异常/警告/超时/… 及 failed/error/unsupported/…）。调用方可用 `expect` 覆盖。
- 默认只返回 `text_matches`（命中条目：文字+分数+命中词+时间戳+双分），未命中的只返回 `text_other_count`，**不返回全文**以省上下文；`expect_all=true` 才给全部原文。

---

## 3. 当前实现（工作区源码，未提交）

### 3.1 涉及文件

| 文件 | 改动 |
|---|---|
| `kernel/dsh_desktop/watch.py` | 核心：全屏采集、`_bottom_band`、`_band_texts`、`_band_fingerprint`、`rank_text_candidates`、`DEFAULT_EXPECT_TERMS`、`_band_strip`、`since()` 输出 |
| `kernel/dsh_desktop/desktop_tools.py` | `desktop_watch()` 新增 `expect` / `expect_all` 参数 |
| `kernel/dsh_desktop/dsh_bridge.py` | `WatchRequest` 新增 `expect` / `expect_all`；`/watch` 透传 |
| `index.js` | `doraemon_watch` 描述与参数更新；`commandFeedback` 门禁（原本 `=== true` 就存在） |
| `kernel/dsh_desktop/eternity.py` | **修吉祥物 bug**：内核启动时不再无条件 `companion.install(self)`，改为 `DSH_COMPANION_AUTOSTART` 环境变量 opt-in |
| `test/test_observation_watch_20261105.py` | 覆盖 keyword signal、全屏采集、OCR 去重、companion 门禁等 |

### 3.2 数据流

```
10fps 全屏 mss 抓帧
  → _signature 64x36 灰度网格
  → _scores: global(均值) / local(top 1% 格子均值)
  → 任一过阈值则留帧（最多 48 帧环形缓冲）
     ├─ small = _fit(frame, 480)          # 拼图用
     └─ band  = _bottom_band(frame)       # 下半屏 45%，源分辨率（宽>1600 才缩）
  → since(seconds, limit, expect, expect_all, ocr_limit)
     ├─ 时间窗筛选 → 时间序（不按分数排序）
     ├─ 拼图 image_path（全帧缩略，看整体变化）
     ├─ band 指纹去重 → 每个不同 band 跑 UIParser.parse() OCR
     ├─ 文字去重 → candidates[{ts,text,global,local}]
     ├─ rank_text_candidates(candidates, expect_terms)   # BM25 + 短语
     │    → text_matches[{ts,text,score,matched_terms,global,local}]
     │    → text_other_count（未命中条数，不返回全文）
     └─ evidence_image（所有 band 放大拼图，仅 OCR 不足时看）
```

### 3.3 关键参数

| 参数 | 当前值 | 说明 |
|---|---|---|
| `FPS` | 10 | |
| `RING_SIZE` | 48 | |
| `GLOBAL_THRESHOLD` | 12.0 | 页面切换 |
| `LOCAL_THRESHOLD` | 8.0 | 小面积 toast |
| `BOTTOM_BAND_FRACTION` | 0.45 | 下半屏；0.22 实测会漏窗口内 toast（68%）和桌面通知 |
| `BAND_MAX_DIM` | 1600 | 保持中文可读 |
| `MAX_OCR_BANDS` | 16 | 每次 `since()` 最多 OCR 多少个不同 band |
| `BM25_K1 / B` | 1.2 / 0.75 | |
| `EXPECT_THRESHOLD` | 0.12 | 且要求至少一个短语命中 |
| `DSH_COMPANION_AUTOSTART` | 默认空（关） | 内核侧 companion 逃生口 |

### 3.4 对外契约（`doraemon_watch`）

- `action`: `start | since | stop | status`
- `since` 参数：`seconds`、`limit`、`expect`（空格/逗号分隔，覆盖内置词表）、`expect_all`、`ocr_limit`（**当前只在 `WatchSession.since()` 上，`registry.since` / `desktop_watch` / `WatchRequest` / `index.js` **尚未透传 `ocr_limit`** —— WIP 不一致点）。
- `since` 返回：`capture_scope`、`frames`、`sampled`、`total_kept`、`timeline`、`image_path`、`text_matches`、`text_other_count`、`expect_terms`、`text_hint`、`evidence_image`、`evidence_hint`；`expect_all=true` 时额外 `text_candidates`。

---

## 4. 实测数据（真机）

统一用例：相册管家「云空间 → 上传 → 上传图片 → 选一个 .txt → 回车」，应用弹出「1 个文件格式不支持，已忽略」。

| 版本 | `since()` 耗时 | 帧/采样 | 是否命中 toast 文字 | 上下文体积 | 备注 |
|---|---|---|---|---|---|
| v1 拼图（420 宽 tile） | ~10ms | 9 / 210 帧 | 图像里能看到 toast，但**文字读不出** | 拼图 ~0.13MB | 触发 v2 |
| v3 全屏 + `parse()` 全帧 OCR（16 帧） | **51,560 ms** | 16 / 2010 | ✅ `1 个文件格式不支持，已忽略`，score 0.747 | ~8.9KB（含 expect_all 全量） | 太慢 |
| v3 + `_detect_text` + 帧去重 | **17,842 ms** | 16 / 771 | ❌ 全乱码 `orts ciseplay neith thull CCR toxt` | ~1.8KB | 中文 OCR 崩了 |
| v3 + 960 宽降采样 | **15,755 ms** | 14 / 109 | ❌ 全乱码 | ~4.8KB（全量） | 缩小后 CJK 不可读 |
| v3 回退 `parse()` + 0.45 band + 去重 | **25,117 ms** | 16 / 104 | ✅ score 0.7309，`other_count=99` | ~8.9KB（全量）/ ~1.7KB（默认） | 能命中但慢 |
| v4 + `MAX_OCR_BANDS=8` 按 local 排序 | **21,123 ms** | 14 / 110 | ❌ `matches=[]` | ~1.65KB | **排序把 toast 帧挤出预算** |
| v4 + 时间序全量 OCR（当前 WIP） | 未真机计时（单元测试通过） | — | 待测 | — | 最后一次改动后**未重启内核** |

**上下文**：`expect_all=false` 时 payload 约 **1.6–1.8KB**；`text_other_count` 约 99–130（即 100+ 条无关 OCR 文字没进上下文。这点是达标的。

**速度**：15–51 秒，**明显偏慢**。瓶颈是每帧一次 `UIParser.parse()`（YOLO + OCR 并行，中文 OCR ~1.5s/帧）。已试过的优化与副作用：
- 帧指纹去重（32×12 二值感知哈希）→ 有效减少 OCR 次数，但静态帧被合并。
- 跳过 YOLO 用 `_detect_text` → 中文乱码，回退。
- 降采样到 960 → 中文乱码，回退。
- 按 local 分排序取 top-N OCR → **漏掉 toast**（全屏下 DSH 对话窗口自身重绘的 local 分比小 toast 更高）。
- 按时间取最新 N 帧 → 同样漏 toast。

---

## 5. 已解决的问题

| 问题 | 结论 |
|---|---|
| 拼图里 toast 字读不出 | v2 原分辨率裁剪解决过；v4 改走文字路线后不再是主要路径 |
| 深色/浅色弹窗过拟合 | 用户否决了 `fill>=0.55` 收敛。当前设计**不做颜色/形状判定** |
| 桌面右下角通知在窗口外 | 改全屏采集（`capture_desktop()` 优先） |
| 底部 22% 覆盖不到窗口内 toast（68%）和桌面通知（85–95%） | 改 `BOTTOM_BAND_FRACTION=0.45` |
| 吉祥物一直闪烁、关不掉 | **根因**：`eternity.py` 在内核启动时**无条件** `companion.install(self)`，绕过插件 `commandFeedback === true` 门禁。改为 `DSH_COMPANION_AUTOSTART` 环境变量 opt-in；重启后验证 `cursor.started=false`、启动日志无 companion 行、装饰窗口枚举为空。⚠️ 该修复在工作区未提交。 |
| `commandFeedback` 默认关但 companion 仍出现 | 同上，插件侧门禁本来是对的，是内核侧漏了门禁。 |

---

## 6. 未解决 / 待决策（**请重点给方案**）

### P0：速度
`since()` 15–51s，一次工具往返不可接受。候选方向（请评估、也可提别的）：
- **并行 OCR**：多帧并发跑 OCR（注意 `UIParser` 的 `_inference_lock` 在 DirectML 下串行）。
- **ROI OCR**：先用双分/差分定位「变化的局部矩形」，只 OCR 那一小块，而不是整个下半屏。
- **轻量 OCR / 缓存**：模型能否只加载一次并复用？是否可换成更快的中文 OCR 或只跑检测不跑识别之外的部分？
- **增量 OCR**：相邻帧只 OCR 差异区域。
- **调用方预算**：`ocr_limit` 已埋在 `WatchSession.since()`，但**尚未透传到 registry / desktop_watch / bridge / index.js**（WIP 不一致）。
- 是否可以接受「异步 since：先返回已 OCR 的部分，后台继续」？

### P0：命中稳定性
全屏采集把 DSH 自己的对话窗口、任务栏、桌面图标、文件对话框全拍进来：
- 100+ 条 OCR 文字里，真正 toast 只占 1–3 条。
- 任何「按分数/按时间截帧」的尝试都漏过 toast（实测两次 `matches=[]`）。
- **如何在不引入语义判定的前提下，保证 toast 帧一定被 OCR？** 还是说应该允许一层「弱筛选」（例如按空间位置排除 DSH 主窗口矩形 —— 但那是几何，不是语义）。
- 或者反过来：OCR 全部帧但用更便宜的 OCR，把「不漏」和「快」同时解决。

### P1：Keyword Signal 的质量
- 内置词表里 `"文件"、"格式"` 太宽，命中大量无关文字（文件名、MP4 文件、聊天里的「@文件或对话」）。
- CJK 用 `tokenize()` 按字切分导致过宽；目前靠「短语精确命中」加权，但 `matched_terms` 里仍会带噪声。
- 是否该用 n-gram / 中文分词 / 小型文本分类器 / log-template retrieval？
- `expect` 由调用方给，还是应该从「当前任务描述」自动生成？
- BM25 参数（`k1/b/threshold`）在短文本、CJK、词表固定语料下是否合理？`idf` 用候选集自身做语料，静态任务栏文字会把 idf 拉偏。

### P1：采集范围与去重
- 下半屏 45% 仍带大量噪声；`BAND_MAX_DIM=1600` 是否为最优？
- `_band_fingerprint`（32×12 二值哈希）是否会把「不同消息但整体亮度分布相近」的帧误合并？
- 是否需要「窗口矩形 + 桌面右下角」双 ROI，而不是整块下半屏？
- 全屏采集下，DSH 自己的 GUI 变化是否应该被排除？如何排除而不违反「不收敛」原则？

### P1：API / 上下文形态
- 现在同时返回 `image_path`（拼图）、`evidence_image`（band 放大）、`text_matches`、`timeline`；是否太多？
- `text_other_count` 只给数量，AI 要不要能「翻页」看未命中原文？
- 是否应该让 `since()` 直接返回一个**结论式摘要**（如 `matched: true, best_text: "..."`），还是严格只给候选、判断全交给 AI？（用户倾向后者）
- 时间线 `ts` 是相对 session 起点的秒数，是否够用？

### P2：工程一致性 / 其他
- `ocr_limit` 未透传（见上）。
- `companion` 的开关有**两处真相源**：插件 `commandFeedback` 与内核 `DSH_COMPANION_AUTOSTART`。是否应该统一成一个？吉祥物「闪烁」是否还有别的原因（如 `window_glow.py` 的 2s 呼吸、`cursor_overlay._loop` 每帧重绘 / 动作姿势反复切换）？用户明确说"闪烁很烦"，需要确认关掉后是否真的完全静止。
- `desktop_watch` 的 `@tool` 描述、`WatchRequest`、`index.js` 描述三处文案需与最终实现同步。
- 测试里 `test_observation_watch_20261105.py` 已有 20+ 条，但**缺少真机/集成级的速度与命中回归**（单测都用 mock OCR）。

### 约束（评审方案时请遵守）
1. 不做「这个是不是弹窗」的语义判定 —— 判断权归 AI。
2. 不因颜色/形状/位置过滤候选帧（已因此踩过两次坑）。
3. 全屏采集（桌面右下角通知必须在范围内）。
4. 输出默认以**文字**为主，图片只作兜底。
5. 不能让 `since()` 阻塞几十秒。
6. 内核是 on-demand 子进程；改 Python 后需重启内核才生效。

---

## 7. 复现步骤

```powershell
cd C:\Users\kennyyzhang\Documents\deepseek-harness\default-workspace\plugins\dsh-doraemon

# 1) 重启内核，加载工作区代码
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/reload-cursor-kernel.ps1 -StateDir "$PWD\.runtime\state"
# 然后触发一次 doraemon_health

# 2) 造不合格文件
Set-Content C:\Users\kennyyzhang\Desktop\dsh-invalid-upload-probe.txt 'not an image'

# 3) 绑定相册管家主窗口、启动 watch、走上传流程
#    （回放脚本：.runtime/desktop_workspace/live_replay.py，或直接调 doraemon_* 工具）

# 4) 读回
# doraemon_watch(action='since', seconds=60, limit=16, expect='失败 不支持 忽略 拦截 格式')
```

测试：
```powershell
$env:PYTHONPATH="$PWD\kernel"; .\.runtime\venv\Scripts\python.exe -m pytest test/ -q
```

---

## 8. 相关代码位置（绝对路径）

| 文件 | 关键符号 |
|---|---|
| `...\plugins\dsh-doraemon\kernel\dsh_desktop\watch.py` | `WatchSession._grab` / `_run` / `since`、`_bottom_band`、`_band_texts`、`_band_fingerprint`、`rank_text_candidates`、`DEFAULT_EXPECT_TERMS`、`_band_strip` |
| `...\plugins\dsh-doraemon\kernel\dsh_desktop\desktop_tools.py` | `desktop_watch()`（文件末尾） |
| `...\plugins\dsh-doraemon\kernel\dsh_desktop\dsh_bridge.py` | `WatchRequest` / `watch_action` / `/companion/start` |
| `...\plugins\dsh-doraemon\kernel\dsh_desktop\eternity.py` | `companion.install` 门禁 |
| `...\plugins\dsh-doraemon\kernel\dsh_desktop\ui_parser.py` | `UIParser.parse` / `_detect_text` / `_compress` |
| `...\plugins\dsh-doraemon\kernel\dsh_desktop\decide.py` | `tokenize` / BM25 打分（复用思路来源） |
| `...\plugins\dsh-doraemon\index.js` | `doraemon_watch` 工具定义、`commandFeedback` schema |
| `...\plugins\dsh-doraemon\cursor-state.js` | 吉祥物生命周期 `/mascot/state` |
| `...\plugins\dsh-doraemon\test\test_observation_watch_20261105.py` | 本轮全部单测 |
| `...\plugins\dsh-doraemon\docs\wb-cua缺陷记录.md` | 上游缺陷记录与设计-01 原始笔记（在 `C:\Users\kennyyzhang\Documents\deepseek-harness\docs\wb-cua缺陷记录.md`） |

---

## 9. 一句话总结

> 我们需要一个**「全屏采样 → 变化帧保留 → OCR 成文字 → 用预期词做低延迟命中筛选 → 只把命中的文字交给 AI 判断」**的弹窗监视器；目前功能链路通了（真机命中过 toast 原文、上下文压到 ~1.7KB），但**速度 15–51s 太慢、且任何截帧策略都会漏 toast**。请给出兼顾「不漏 / 快 / 省上下文」的方案。

---

## 10. 解决方案：差分 ROI（已实施并提交 `e95c6f4`）

### 根因判断

**瓶颈不是"OCR 慢"，是"OCR 的对象选错了"。**

v3/v4 对每个留帧的**整个下半屏 band**（约 1600×970）跑一次 `UIParser.parse()`。
于是：慢（16 帧 ≈ 25s）→ 被迫选帧控制预算 → 任何按分数/按时间的选帧都会漏 toast。
**慢和漏是同一个根因**，单独优化任何一头都会在另一头付出代价。

### 方案：OCR 变化区域，而不是整幅 band

留帧的那一刻，前后两帧本来就在手上。直接差分出"哪几个矩形变了"，
只 OCR 那些小区域：

```
留帧 → _diff_rois(prev_frame, curr_frame) → [(x,y,w,h) 变化矩形]
     → 只 OCR 这些裁剪（原分辨率）
     → 无 ROI 时（首帧/分辨率变化）回退到底部 band
```

对照文档第 6 节的约束：

| 约束 | 如何满足 |
|---|---|
| 不做"是不是弹窗"的语义判定 | 差分只回答"哪些像素变了"，不回答"是不是弹窗" |
| 不因颜色/形状/位置过滤候选帧 | **帧照留**，一个不少；只把 OCR 的对象从整屏换成变化区域 |
| 全屏采集 | 不变，`capture_desktop()` 优先 |
| 输出以文字为主 | 不变，只是文字来源更聚焦 |
| 不能阻塞几十秒 | 见下方实测面积 |
| 内核 on-demand | 改后需重启内核（同文档说明） |

### 实测（真实 toast 留帧序列，非合成数据）

| 帧 | ROI 数量 | ROI 面积 | band 面积 | 压缩比 |
|---|---|---|---|---|
| 对话框关闭（cut） | 5~6 | 26,368 ~ 122,140 | 192,000 | 1.6~7.3× |
| **toast 出现** | **1** | **5,760（144×40）** | 192,000 | **33.3×** |
| toast 停留 | 1 | 6,048（144×42） | 192,000 | 31.7× |
| **toast 消失** | **1** | **7,776（144×54）** | 192,000 | **24.7×** |

**合计 OCR 面积降到 15.1%**；真正含 toast 的那几帧降到 **3~4%**。

而且 ROI 正好裁在文字本体上，周围没有任务栏/图标/聊天文字，
中文识别少了一层噪声来源。

### 对第 6 节各问题的回应

- **P0 速度**：OCR 面积降到 15%（toast 帧 3%）。这是结构性的下降，
  不需要并行、不需要换 OCR 引擎、不需要降采样（降采样已实测会让中文变乱码）。
- **P0 命中稳定性**：**不再选帧**。所有变化区域都进 OCR 候选，
  所以"按分数/按时间截帧漏 toast"这条路径整个消失。
  `ocr_limit` 仍保留为延迟旋钮，预算紧张时 **ROI 目标优先于 band 目标**，且不按分数排序。
- **P1 采集范围**：ROI 天然取代"下半屏 45%"的猜测——toast 在 68% 还是 95% 都无所谓，
  只要它变了就会被裁出来。`BOTTOM_BAND_FRACTION` 只用于无差分时的兜底。
- **P1 全屏噪声**：任务栏、桌面图标、静态聊天文字**没有变化就不产生区域**，
  从源头不进 OCR，而不是 OCR 之后再靠词表过滤。
- **P2 工程一致性**：`ocr_limit` 已全链路透传；`since()` 新增
  `ocr{targets, runs, roi_targets, truncated}` 便于观测预算是否真的在起作用。

### 单测

`test/test_watch_diff_rois.py` 9 条：静止无 ROI、空/尺寸不匹配安全、
toast 单区域且保持原分辨率、微小变化过滤、整页重绘排除、
**多区域全部保留（不排名）**、贴边不越界、Frame 字段向后兼容。
全量 **238 passed, 1 skipped**。

### 仍需真机确认的一件事

上面的数字是**面积**，不是端到端耗时。需要在重启内核后按第 7 节复现一次，
记录 `since()` 的真实耗时与 `text_matches` 是否仍命中 toast 原文。
预期：OCR 次数由 `ocr.runs` 给出，单次区域更小，耗时应显著低于 25s；
若仍慢，下一步才是"OCR 引擎本身"的问题（那时再考虑并行/换引擎才有依据）。

**记录时间**：2026-11-05 17:35

---

## 11. 真机复核（2026-11-05 17:44–17:52，重启内核 pid 2472 后）

按第 7 节复现两次（云空间 → 上传不合格 .txt）。**结论：ROI 方案方向成立，但端到端没达标，且暴露两个新问题。**

| 指标 | 实测 | 文档第 10 节预期 |
|---|---|---|
| `ocr.roi_targets` | **37**（12 个留帧展开出的变化区域） | 有 ROI |
| `ocr.targets / runs` | 37 / **16**（`truncated: true`） | 全部变化区域都进 OCR |
| `since()` 耗时 | **25.5s**（默认）/ **62.8s**（`expect_all=true, ocr_limit=16`） | 显著低于 25s |
| `text_matches` | **[]**（两次都没命中 toast 原文） | 命中 |

### 新发现的问题

**① 预算截断又漏了 toast —— 换了地方，同一个坑。**
`since()` 里 `ordered = [ROI...] + [band...]`，`chosen = ordered[:budget]`（budget=16），取的是**时间上最早**的 16 个 ROI；toast 出现在较晚的帧，被挤出 OCR 预算。之前按 local 分排序漏、按最新 N 帧漏，现在按"ROI 展开后按时间序截断"漏。**根因仍是"存在预算 → 必须选目标"**。第 10 节说"不再选帧"只做到了帧层面，ROI 层面又引入了目标选择。

**② 存在超大 ROI，单次 OCR 耗时 23.8s / 16.4s（0 texts）。**
内核日志 `OCR: 23772ms, 0 texts` 与 `OCR: 16377ms, 0 texts`。`ROI_MAX_SCREEN_FRAC=0.30` 只排除 >30% 屏幕的矩形；接近阈值的大 ROI 仍然极慢，两次调用就吃掉 40s。**面积小 ≠ 快**：OCR 有固定开销，且大 ROI 会拖垮单次调用。第 10 节用"面积降到 15.1%"推断提速，实测不成立 —— 面积是代理指标，不是耗时。

**③ 中文识别仍不稳**：ROI 是原分辨率小裁剪，但 `UIParser.parse()` 对细长条 ROI 的输出多为乱码碎片（`"Diagnostic"`, `"re"`, `"747 K / 500 M"` 等），全量文字里没有出现「1 个文件格式不支持，已忽略」。

### 仍然成立的部分
- 全屏采集生效（`capture_scope: desktop`）。
- 吉祥物关闭生效：重启后 `cursor.started=false`，启动日志无 companion 行。
- 上下文仍然省：`expect_all=false` 时 payload ~1.7KB，`text_other_count` 66–81。
- 测试 238 passed 与文档一致。

### 给下一轮方案提的两个硬约束（补充第 6 节约束列表）
7. **任何"预算 / 排序 / 截断"都会漏掉目标消息**，除非能保证"晚出现的帧"一定进 OCR（例如按帧轮询、每帧至少保留一个目标，或干脆不截断）。
8. **不要用面积当速度的代理指标**；必须测端到端 `since()` 耗时，并约束单个 OCR 输入的最大边长/宽高比。

### 可选方向（未实施，供参考）
- 取消 ROI 预算截断，改为**按帧分配**配额（每帧 1–2 个 ROI，12 帧最多 24 个目标）。
- 给单个 ROI 加**最大边长**限制，超大 ROI 直接丢弃或切块（纯几何，不违反约束 2）。
- 把 OCR 从"每个 ROI 一次调用"改成"同一帧的多个 ROI 拼成一张图一次调用"，摊薄固定开销。
- 或者回到"每帧一次 OCR"，但只对**该帧的差分矩形**调用一次，而不是展开成 N 个目标。

> 本节为实测补充，未改代码；当前 HEAD 仍是 `7375f0a`（工作区干净）。

---

## 12. 二次修正：每帧一次拼合 OCR（`5ac4e65`）

第 11 节真机复核推翻了第 10 节的两个假设，本版按**真实基准**重写，而不是继续猜。

### 第 10 节错在哪

1. **把 ROI 展开成独立 OCR 目标**，12 帧变成 37 个目标，预算 16 又要截断，
   晚出现的 toast 被挤掉。所谓"不再选帧"只做到了帧层面，ROI 层面又引入了选择。
2. **拿面积当速度的代理指标**。实测 OCR 有固定开销，**调用次数才是主成本**；
   面积小不等于快，超大 ROI 单次甚至跑到 23.8s。

### 基准：同一批真实 toast 帧，四种调用方式

新增 `test/ocr_call_bench.py`（真实 UIParser，非 mock）：

| 方式 | toast 帧耗时 | 识别结果 |
|---|---|---|
| A `parse(band)` | 1338ms | 完整句 + 6 条噪声 |
| B `parse(单个 ROI)` | 2230ms | 「1个文件格式不支持」「已勿路」**断行乱码** |
| C `_detect_text(单个 ROI)` | 814ms | 同样断行乱码 |
| **D `_detect_text(ROI 拼合图)`** | **990ms** | **「1个文件格式不支持，已忽略」完整正确** |

**两条新认知**：

- **ROI 不是越小越好**。过窄的条带会让 OCR 断行误识别；
  拼合图的白边与 padding 反而让它读成完整一句。
- **每帧一次调用 + 只跑 OCR 不跑 YOLO**，比逐 ROI 调用快 2.2 倍，识别还更准。

### 本版改动

- `_roi_sheet()`：把一帧的多个变化区域**垂直拼成一张图**，白边分隔；
- `since()` 每帧**一次** `_detect_text(拼合图)`，不再展开成 N 个目标；
- **整页重绘帧跳过 OCR**（变化面积 >30% 屏）：帧照留、照进拼图，只是不花模型调用；
- band 路径保持原 `parse()` 与原调用签名，旧测试桩不受影响。

### 端到端结果（真实留帧 + 真实 UIParser）

| 指标 | 第 10 节版本 | 本版 |
|---|---|---|
| `since()` 耗时 | 25.5s / 62.8s | **7.28s** |
| OCR 目标数 | 37（截断到 16） | **6** |
| `truncated` | true | **false** |
| toast 命中 | **[]** | **「1个文件格式不支持，已忽略」score 0.782** |

注：该验证使用窗口尺寸的真实留帧；全屏 4K 下 ROI 更大、帧更多，
**仍需按第 7 节真机复核一次**。但"每帧一次调用 + 只跑 OCR + 跳过重绘"
是结构性改善，与分辨率无关。

**记录时间**：2026-11-05 18:55

---

## 13. 全屏 4K 真机复核（第 12 节遗留项，2026-11-05 19:00–19:10）

按第 7 节重启内核（pid 67220，加载 `5ac4e65` 之后的代码），用相册管家
「云空间 → 上传 → 上传图片 → 选不合格 .txt → 回车」触发 toast。
`capture_scope: desktop`，即 3840×2160 全屏采集。

### 结论：**结构性改善成立，全屏下命中且无截断；但耗时是全屏的 ~2 倍，且预算截断仍会漏 toast。**

| 指标 | 第 12 节（窗口尺寸留帧） | 本次全屏 4K |
|---|---|---|
| `since()` 耗时 | 7.28s | **12.6s / 19.5s**（两轮） |
| OCR 调用数 | 6 | **8 / 15** |
| `truncated` | false | **false** ✅ |
| `skipped_repaint` | — | 0（整页重绘判据未触发） |
| toast 命中 | score 0.782 | ✅ **「1个文件格式不支持，已忽略」score 0.7525** |
| 上下文 | — | ~1.9KB（`expect_all=false`） |

### 与第 12 节预期的差异及原因

1. **耗时没到 7.28s，而是 12.6–19.5s。** 差异来自**调用次数**，不是单次变慢：全屏下
   留帧 15（窗口尺寸留帧 6），每次 `_detect_text` 0.3–2.7s（平均 ~1.3s），
   与窗口尺寸的 ~1.2s/次基本一致 → 印证"调用次数才是成本"这个判断。
   全屏多出来的帧主要来自 **DSH 对话窗口自身的重绘**被全屏采集拍进来了。
   所以"与分辨率无关"只对**单次调用成本**成立，总耗时仍随帧数线性增长。

2. **整页重绘跳过没起作用**（`skipped_repaint: 0`）：`ROI_MAX_SCREEN_FRAC=0.30`
   要求变化面积 >30% 屏才跳过，而 DSH 窗口重绘通常只占屏的一小部分，
   够不到阈值。这条优化在全屏下基本空转。

3. **`ocr_limit` 缩小会重新漏 toast**（关键回归风险，实测）：

   | `ocr_limit` | `since()` | OCR runs | truncated | matches |
   |---|---|---|---|---|
   | 16（默认） | 12.8s | 9 | **false** | ✅ 命中 3 条（含完整句） |
   | 6 | 7.0s | 6 | true | **[]** |
   | 3 | 2.3s | 3 | true | **[]** |

   原因和第 11 节一样：`ordered[:budget]` 取时间上最早的目标，toast 在较晚帧，
   预算一收紧就被挤掉。**"存在预算 → 必须选目标 → 必漏**"这个坑到本版仍在，
   只是默认值 16 恰好够用（本次 9–15 个目标）。若某次操作留帧 >16，默认也会漏。

### 仍然成立 / 已达标的项

- ✅ 全屏采集、吉祥物关闭（重启后 `cursor.started=false`）。
- ✅ 命中正确、无截断、中文不再乱码（拼合图 + `_detect_text` 的组合有效）。
- ✅ 上下文 ~1.9KB，`text_other_count` 51（无关文字只报数量）。
- ✅ 全量 **238 passed, 1 skipped**（本地复核）。
- ✅ 相比第 10 节的 25.5s / 62.8s 且漏报，本版是明确的净改善。

### 给下一轮的开放问题（补充第 6、11 节）

1. **默认预算 16 是隐患**：留帧超过 16 就会静默漏。要么把默认值提到覆盖 48 帧环形
   缓冲的上限，要么改成"每帧至少保留一个目标"的配额分配，而不是全局 `[:budget]`。
2. **预算与延迟不可兼得**：想压到 7s 就得砍目标，一砍就漏。需要"更便宜地判断哪些帧
   不含目标"的**非语义**方法（例如变化区域里有没有文字用极轻量的检测，
   而不是靠时间/分数排序）。或者接受 12–20s，把它做成**异步 `since()`**。
3. **DSH 自身窗口的重绘**是全屏噪声主来源。是否可以在不违反"不收敛"约束的前提下，
   用纯几何方式排除 DSH 主窗口矩形（它本来就不是我们要监视的对象）？
4. `skipped_repaint` 阈值 30% 屏在全屏下几乎不触发，形同虚设；要么降阈值，
   要么改成"变化矩形数量 / 单帧最大 ROI 边长"这类与分辨率无关的相对判据。

### 复核环境

- HEAD `a7564ed`（工作区干净，仅本文档后续被修改）。
- 内核 pid 67220，已重启加载 `5ac4e65` 之后代码。
- 回放脚本：`.runtime/desktop_workspace/live_replay.py`、
  `.runtime/desktop_workspace/ocr_limit_probe.py`（未纳入 git）。
- 桌面测试文件 `dsh-invalid-upload-probe.txt` 用后已删除。

**记录时间**：2026-11-05 19:10

---

## 13. 产物生命周期（`40d8d07`）

用户问"这些帧不会一直保留吧"——分三层，其中**磁盘那层原来确实会无限累积**，已修。

| 层 | 内容 | 生命周期 |
|---|---|---|
| 内存 | 缩略图 + band + ROI 小图 | **48 帧环形缓冲**，超出即挤出；`stop()` 后随 session 释放 |
| 磁盘 | `since_*.jpeg` / `band_*.jpeg` | **各保留最近 12 张**，出图后顺带清理（本次修复） |
| 交给模型 | 命中文字 + 图片路径 | 默认只有文字（~1.9KB），图片仅兜底 |

修复前：文件名带微秒时间戳、只增不删，一次 `since()` 写两张，长期会持续累积。
修复后只清理由本模块生成的两类前缀，目录里其他文件不动。

单测 `test_watch_artifacts.py` 5 条：保留最新、不碰无关文件、两类独立清理、
目录缺失不抛错、上限有界。全量 **243 passed**。

**记录时间**：2026-11-05 19:20
