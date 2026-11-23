# DSH 默认电脑操作协议

本文是重构后的目标契约，供实现、联调和验收使用。文中的新接口需要逐阶段实现。现有插件的工具和返回格式仍以当前代码为准。

## 1. 面向模型的入口

默认 DSH 会话只注册下面六个电脑操作工具。底层继续使用 DOM、UIA、截图、OCR、视觉定位和物理输入，由运行时根据目标和可用证据确定执行路径。

| 工具 | 建议公开描述 |
| --- | --- |
| `doraemon_open` | 打开应用或网址，或激活已有目标，返回目标和初始观察。 |
| `doraemon_observe` | 列出桌面目标，或读取指定目标的当前元素、文字和可选截图。 |
| `doraemon_act` | 使用一次观察中的元素引用或截图坐标执行短动作序列，返回执行进度和最新观察。 |
| `doraemon_wait` | 等待目标中的文字、元素、窗口或状态满足条件。 |
| `doraemon_status` | 查询运行时健康或指定操作的实际进度，可短暂跟随。 |
| `doraemon_stop` | 取消当前会话的操作，返回已发出的输入和实际停止进度。 |

模型只需决定目标、动作和期望结果。公开参数收敛为任务信息，执行引擎选择、输入锁、缓存和采集器调度由内核管理。

默认契约不引入额外规划模型。远程图片描述默认调用次数为零。宿主模型直接使用语义观察和真正送入模型的图片。

## 2. DSH 适配约束

保留根目录 `AGENTS.md` 中的 Host、Client、打包和生命周期契约。

- Host 继续导出 `name = 'doraemon'`、`inject = ['tools', 'settings']`、schemastery `Config` 和同步 `apply`。
- 工具、资源和后台任务在 `apply` 内注册，通过 `ctx.effect` 清理。
- `defineTool.parameters` 使用 DSH 的 plain-object 参数规格。字段使用 `type`、`required`、`description`、`enum` 等框架实际支持的属性。
- 实现第一阶段检查安装中的 `@deepseek-ai/dsh-tools` 源码和版本，确认嵌套 object、array、items 和属性约束的实际写法。
- 内核再次校验互斥字段、范围、状态机和引用归属。框架参数校验与内核校验共同构成入口验证。
- 动作步骤以结构化数组传递。只有证实当前 DSH 版本确实无法传递嵌套结构后，才记录框架限制并讨论替代适配方式。
- 每次桥接调用传递 `exec.signal`。`exec.callId` 作为 Host 重试关联键传入内核。
- 非法参数抛出异常。运行故障返回 `success: false` 和稳定错误码。

P0 使用安装中的 dsh-tools 0.2.2-rc.3 的 schema.ts 和 pipeline.ts 验证了以下嵌套写法。对象声明 properties 和 additionalProperties，必填标记放在属性上。复杂互斥及数值范围由 runtime 校验。

```js
parameters: {
  target_id: { type: 'string', required: true, description: '本会话的目标 ID' },
  observation_id: { type: 'string', required: true, description: '动作依据的观察 ID' },
  steps: {
    type: 'array', required: true,
    items: {
      type: 'object', additionalProperties: false,
      properties: {
        action: { type: 'string', required: true, enum: ['click', 'input'] },
        ref: { type: 'string' },
        text: { type: 'string' },
      },
    },
  },
}
```

此处枚举用于演示 DSL。完整动作集合见第 7 节。test/dsh-contract.test.js 经实际 ToolExecutionPipeline 验证嵌套必填、枚举、额外字段拒绝和取消，3 个用例通过。

## 3. 标识、归属和保存期

### 3.1 目标与观察

| 字段 | 含义 |
| --- | --- |
| `target_id` | 当前会话可操作的窗口、浏览器标签页或桌面目标 |
| `observation_id` | 某目标在某次采集中的观察，拥有本次元素引用和图片帧 |
| `ref` | 本次观察中的元素引用，例如 `e7` |
| `frame_id` | 一张实际送出的图片及其坐标映射 |
| `operation_id` | 一个被内核接受并登记的操作 |

ID 为不透明字符串。内部归属包含 owner/session 和 runtime epoch。内核重启后，旧目标、观察和帧 ID 返回 `RUNTIME_RESTARTED` 或 `OBSERVATION_EXPIRED`，并引导重新列举目标。

owner 由真实 DSH 执行上下文 exec.agent.session.id 派生，Host 负责传递稳定会话标识。框架 pipeline 保留传入的 exec.callId 和 AbortSignal。P4 验证缺省上下文、跨重试和 Host 重载时的映射与恢复。

同一 URL 可以对应多个标签页。浏览器目标身份绑定浏览器实例、tab 和 frame。Windows 目标身份绑定 hwnd、进程身份与进程生命周期，处理 hwnd 复用。

元素引用只在对应观察的目标中解释。跨观察复用 `e7` 会得到校验失败。数组位置与引用生成顺序均不构成跨观察稳定身份。

同一会话新观察产生后，旧观察在保存期内仍可读取。执行是否允许使用其中的引用，由实时身份校验决定。

### 3.2 初始保存参数

以下为首版调优起点，均通过内部配置调整。

| 项目 | 初始值 |
| --- | --- |
| 每 owner/target 保存的观察 | 最近 8 次 |
| 观察及图片空闲保存时间 | 5 分钟 |
| 已结束操作结果保存时间 | 30 分钟 |
| 每 owner 已结束操作记录上限 | 200 |
| 幂等请求关联记录保存时间 | 24 小时 |

活跃操作引用的数据在操作期间固定保留。worker 已终止而结果无法确认的 `unknown` 属于需核对现场的终态，按已结束操作的保存期回收。worker 仍运行的未知操作继续占用其真实执行资源。超过容量时先回收已结束且最久未访问的完整结果，并在幂等保存期内保留请求键、指纹、操作身份和派发摘要。结果已回收的同键重试返回记录已过期及核对现场提示。

owner 只能读取自身操作和观察。全局健康摘要可以包含设备忙碌情况，输出保持紧凑。

## 4. 统一结果结构

```json
{
  "success": true,
  "operation_id": "op_42",
  "status": "completed",
  "target_id": "t_5",
  "dispatch": "sent",
  "observation_changed": true,
  "expectation": { "status": "met" },
  "outcome": "expectation_met",
  "observation": {
    "observation_id": "o_19",
    "target_id": "t_5",
    "title": "订单",
    "elements": [],
    "text": "已保存"
  }
}
```

工具按场景省略不适用字段。`observe` 和无操作的健康查询无需产生 `operation_id`。

| 字段 | 固定词汇与解释 |
| --- | --- |
| `status` | `accepted`、`running`、`completed`、`failed`、`cancelling`、`cancelled`、`unknown` |
| `dispatch` | `none`、`sent`、`partial`、`unknown` |
| `expectation.status` | `not_requested`、`pending`、`met`、`timeout`、`unknown` |
| `outcome` | `opened`、`dispatched`、`expectation_met`、`interrupted`、`cancelled`、`unknown` |
| `observation_changed` | `true`、`false` 或 `null`，最后一种表示采集不足以比较 |

`success` 表示本次工具请求是否成功处理。`accepted`、`running` 和需要核对现场的 `unknown` 可以返回 `success: true`。这些结果使用 `message` 和 `next_action` 描述后续处理，保持 `error` 字段缺省。

输入已发出、画面已改变、期望已满足和任务已完成是四个独立事实。`outcome: dispatched` 只证明输入被发送。`expectation_met` 证明给定条件成立。业务任务是否全部完成由主模型结合用户请求判断。

同步 `open` 与 `act` 默认附带一次短后观察。模型可以直接继续，常规动作无需固定追加一次 `observe`。

运行失败示例遵循现有 `toolOk()` 的失败语义。

```json
{
  "success": false,
  "operation_id": "op_43",
  "status": "failed",
  "error": {
    "code": "STALE_REF",
    "message": "目标元素已经变化，请使用返回的新观察。",
    "retryable": true
  },
  "dispatch": "none",
  "observation": { "observation_id": "o_20", "target_id": "t_5" }
}
```

## 5. `doraemon_open`

### 参数

| 参数 | 类型 | 规则 |
| --- | --- | --- |
| `url` | string | 打开网址 |
| `app` | string | 已注册应用标识或可执行文件绝对路径 |
| `args` | string[] | 启动参数，只能与 `app` 一起使用 |
| `target_id` | string | 激活已有窗口或标签页 |
| `connect` | string | 显式连接已有浏览器的 CDP endpoint，或使用约定的 `configured` 配置值 |
| `image` | boolean | 显式决定初始观察是否附图，缺省使用观察策略 |

支持四种明确调用。只有 `url` 时，在专用持久浏览器中新建标签页。只有 `target_id` 时激活该目标。`target_id` 与 `url` 一起提供时，在指定浏览器标签页中导航。`app` 用于启动应用，与上述字段互斥。`connect` 单独用于连接已有浏览器，也与上述字段互斥。

连接模式返回发现的标签页摘要，并对当前活动页返回初始观察。没有可用页面时返回目标目录。连接方式不可用时返回 `BACKEND_UNAVAILABLE` 与实际诊断。附着窗口由真实 browser/tab 身份加入目标目录，后续使用 `target_id` 精确选择。endpoint 来自本机已配置值或用户指定值。

```json
{ "url": "https://example.com/orders" }
```

结果包含 `target_id`、`outcome: opened` 和初始 `observation`。应用启动或导航较慢时，在同步预算到达后返回 `operation_id` 与 `running`，通过 `status` 跟随。

不同调用即使 URL 相同也可以创建不同标签页。已有目标通过 `target_id` 激活或导航。同一 callId 的传输重试复用原 operation，避免重复创建窗口或标签页。

## 6. `doraemon_observe`

### 参数

| 参数 | 类型 | 规则 |
| --- | --- | --- |
| `target_id` | string | 缺省列举可用窗口、浏览器标签页和桌面目标 |
| `query` | string | 提取与当前任务相关的文字和元素，例如“保存按钮和错误提示” |
| `region` | object | 使用先前帧定义局部采集范围 |
| `cursor` | string | 继续读取已返回观察的下一页文字和元素，与 query/region/image 互斥 |
| `image` | boolean | `true` 强制附图，`false` 明确只要语义，缺省自动决定 |

`query` 用于采集过滤和排序。内核使用可解释的文本匹配与结构信息，返回查询摘要和截断信息。

无目标调用返回紧凑目标列表，每项包含 `target_id`、`kind`、`title`、可用的 `url` 或应用名，以及 `active`。同时返回当前桌面目标 ID。无目标且 `image: true` 时可附桌面图，该图归属返回的桌面目标。

局部范围使用已送出图片中的像素。

```json
{
  "target_id": "t_5",
  "region": { "frame_id": "f_7", "x": 420, "y": 100, "width": 500, "height": 360 },
  "query": "导出选项",
  "image": true
}
```

### 观察结构

```json
{
  "observation_id": "o_21",
  "target_id": "t_5",
  "captured_at": "2026-11-23T09:00:00.000Z",
  "revision": "rev_9",
  "title": "编辑订单",
  "url": "https://example.com/orders/42",
  "elements": [
    { "ref": "e1", "role": "textbox", "name": "备注", "value": "原备注", "editable": true },
    { "ref": "e2", "role": "checkbox", "name": "加急", "checked": false, "enabled": true },
    { "ref": "e3", "role": "combobox", "name": "地区", "value": "杭州", "options": ["杭州", "上海"] },
    { "ref": "e4", "role": "button", "name": "保存", "enabled": true }
  ],
  "text": "订单 42",
  "truncated": { "elements": false, "text": false },
  "frames": [ { "frame_id": "f_8", "width": 1366, "height": 768 } ],
  "evidence": { "semantic_at": "2026-11-23T09:00:00.000Z", "image_at": "2026-11-23T09:00:00.020Z" }
}
```

供 AI 阅读的元素字段保留任务所需的 role、name、value、enabled、checked、selected、editable、options，以及必要的 bounds。密码或框架未提供的值按实际采集能力处理。选项很多时输出 `options_truncated` 并允许 query 定向读取。

公开 bounds 仅在同时送出对应图片时提供，形式为 `{frame_id, x, y, width, height}`，单位是该图片的像素。模型仅凭 ref 执行时可以省略 bounds。归一化、屏幕和客户区坐标保留在内部表示中。

内部引用表保留完整定位器、frame、原始元素身份、后端来源和采集版本。文本展示截断与执行定位器独立存储。DOM nth 索引须绑定具体 frame 和原始匹配集合，优先持有稳定元素身份。

观察可以组合不同采集器，但输出保留各来源时间和版本。只有身份和几何验证通过的元素才允许执行。采集器超时通过 `warnings` 说明可用范围。

### 初始输出预算

| 项目 | 初始值 |
| --- | --- |
| 元素数 | 80 |
| 正文字符数 | 6000 |
| 全窗口图片长边 | 1366 像素 |
| 局部图片 | 尽量保留原始像素细节，受资源大小限制 |
| 新目标或新布局 | 紧凑语义加图片 |
| 语义稳定的连续操作 | 增量语义，视觉状态显著变化时补图 |

达到预算时明确返回截断标记、可用的 `next_cursor` 和 query/region 提示。cursor 绑定已保存的 observation、过滤条件与读取位置，分页使用同一 observation_id，避免重新扫描。传入 target_id 时必须与 cursor 目标一致。缓存过期返回 `OBSERVATION_EXPIRED`。完整采集本身达到内部上限时返回 `source_truncated`，给出滚动或局部 query 的继续路径。

更改 query、region 或主动请求新图时创建新观察。旧 cursor 始终读取原观察，响应保留 captured_at，避免把分页结果误解为当前现场。

OCR/YOLO 按需惰性启用。基础语义与截图尽量同批获取，避免同一轮重复 UIA 扫描。

## 7. `doraemon_act`

### 参数

| 参数 | 类型 | 规则 |
| --- | --- | --- |
| `target_id` | string | 必填，与观察目标一致 |
| `observation_id` | string | 必填，本批动作的依据 |
| `steps` | object[] | 必填，有序短序列，首版上限 8 |
| `expect` | object | 可选，在指定步骤后核对结果 |

模型通过元素引用或图片点选择目标。两者互斥。

```json
{ "ref": "e4" }
```

```json
{ "frame_id": "f_8", "x": 1020, "y": 650 }
```

### 动作词汇

| `action` | 字段 | 语义 |
| --- | --- | --- |
| `click` | `target`，可选 `button`、`count` | button 缺省 left，count 缺省 1，允许 1 或 2 |
| `input` | `target`、`text`、`mode` | mode 必填 `replace` 或 `insert`，分别替换全部内容或在插入点输入 |
| `key` | `keys` | 键名数组，例如 `["CTRL", "S"]`，作用于目标当前焦点 |
| `scroll` | `target`、`direction`、`amount` | direction 为 up/down/left/right，amount 为正整数滚轮刻度，缺省 3 |
| `drag` | `from`、`to` | 起点和终点均为引用或帧点 |
| `select` | `target`、`values` | values 为精确选项标签数组，单选目标只接受一项 |
| `check` | `target`、`checked` | 将复选框设置为指定布尔状态，已符合时返回 already_satisfied |
| `hover` | `target` | 悬停并触发可能出现的菜单或提示 |
| `attach` | `target`、`files` | files 为本机绝对路径数组，目标为可识别的文件输入或上传控件 |
| `close` | 无 | 关闭当前 `target_id` 对应目标 |

`target` 的内部路由依据引用来源与目标能力确定。浏览器 DOM、原生 UIA 和视觉操作共用上述动作语义。遇到能力缺失返回 `UNSUPPORTED_ACTION`，并附当前观察与可行恢复信息。

`key` 执行前确认目标已激活或具有受控的浏览器焦点。禁止将目标丢失后的键盘输入自动发送到另一个窗口。

拖动的两个点必须属于同一观察和兼容坐标空间。跨窗口物理拖动使用桌面 target 的同一帧坐标，内核在全程持有输入资格时验证两端几何。同一目标中的跨帧拖动由内核验证映射后执行。

### 批处理与边界

```json
{
  "target_id": "t_5",
  "observation_id": "o_21",
  "steps": [
    { "action": "input", "target": { "ref": "e1" }, "text": "请下午送达", "mode": "replace" },
    { "action": "check", "target": { "ref": "e2" }, "checked": true },
    { "action": "click", "target": { "ref": "e4" } }
  ],
  "expect": { "kind": "text", "text": "已保存", "transient": true, "after_step": 2, "timeout_ms": 3000 }
}
```

步骤索引统一从 0 开始。序列适合填写同一屏表单。每一步派发前校验身份、可用性和输入租约。完整 DOM/UIA 身份仍有效时允许重新获取当前几何。视觉点依赖对应帧的布局版本，遇到滚动、导航、窗口位置或 DPI 变化立即失效。

`expect.timeout_ms` 缺省 5000，首版范围 100 至 30000。`expect.after_step` 必须落在当前 steps 的有效索引内，缺省最后一步。条件满足后再继续执行该步骤之后的剩余动作，条件超时即停止剩余动作。

导航、模态窗口、新目标、目标消失、引用失效、期望失败或派发状态未知都会终止剩余步骤。返回已完成步骤、新观察和恢复原因。导航发生在最后一步时可以正常结束，随后观察新页面。

```json
{
  "success": true,
  "operation_id": "op_44",
  "status": "completed",
  "outcome": "interrupted",
  "dispatch": "partial",
  "completed_steps": [0, 1],
  "stopped_at": 2,
  "reason": "modal_opened",
  "observation": { "observation_id": "o_22", "target_id": "t_5" }
}
```

边界中断可正常报告 `interrupted`。动作本身失败时返回 `success: false` 和对应错误码。未知步骤单列 `uncertain_step`，保留 `operation_id` 供查询。已完成步骤不自动重放。

## 8. 条件与 `doraemon_wait`

### 有限条件词汇

| `kind` | 字段 | 满足条件 |
| --- | --- | --- |
| `text` | `text`，可选 `match` | 当前目标出现文字，match 为 contains 或 exact，缺省 contains |
| `element` | `ref`，可选 `state` | 引用对应元素满足状态，缺省 visible |
| `window` | `title`，可选 `match` | 当前 owner 可发现匹配窗口，match 缺省 contains |
| `state` | `state` | 当前目标状态满足 ready、closed 或 url_changed |

元素 state 允许 visible、hidden、enabled、disabled、checked、unchecked、focused。隐藏条件按元素身份判断，元素从树中移除可以满足 hidden。窗口条件返回匹配后的 `target_id` 和新观察。

`url_changed` 以关联观察的 URL 为基准。`ready` 由明确的页面或原生目标就绪规则实现，并返回判断依据。页面就绪首版使用 DOMContentLoaded 加一次可用语义采集，原生目标使用窗口存活、响应且一次 UIA/截图采集完成。

条件只描述可检查的现场事实。任意任务目标仍交给主模型理解。首版每次一个条件，组合任务由后续工具调用表达。

### wait 参数

| 参数 | 类型 | 规则 |
| --- | --- | --- |
| `target_id` | string | text/element/state 必填，window 条件可省略 |
| `observation_id` | string | element 和 url_changed 必填，其他条件可选 |
| `condition` | object | 必填，使用上述有限词汇 |
| `timeout_ms` | integer | 缺省 5000，首版范围 100 至 30000 |

```json
{
  "target_id": "t_5",
  "condition": { "kind": "text", "text": "导出完成" },
  "timeout_ms": 10000
}
```

等待采用有界采样或底层事件订阅，等待期间释放物理输入资源。达到同步预算先返回 `running`，后续查询同一 `operation_id`。到期返回 `EXPECTATION_TIMEOUT`、最后观察和已检查的条件。

### 防止丢失瞬时 toast

动作触发后才调用 wait，可能已经错过 toast。因此 `act.expect` 支持 `transient: true`，且只用于 text 条件。

1. `after_step` 缺省为最后一步，指定需要触发该提示的步骤。
2. 内核在派发该步骤之前创建有界 watcher，读取基线并确认 sampler 已启动。
3. watcher 记录派发时间，检测派发后新出现或重新出现的匹配文本。基线中已经存在的同名文本单独记录，避免误判为本次动作结果。
4. 首版采样间隔从 100 毫秒起步，优先使用可用的 UIA/DOM 事件。记录实际采样次数和事件时间，按真实运行数据调整。
5. 条件满足、超时、取消或目标丢失立即停止 watcher。返回 `matched_at`、`evidence` 和最终观察，最终画面允许已经没有该 toast。

watcher 在动作前启动是协议不变量。100 毫秒采样与默认超时属于调优参数，极短提示的识别能力需真实任务验证。

独立 wait 可以观察调用后的瞬时提示。依赖某个点击产生的短提示时，模型使用该点击的 `act.expect`。

## 9. `doraemon_status`

| 参数 | 类型 | 规则 |
| --- | --- | --- |
| `operation_id` | string | 缺省返回运行时健康与当前 owner 活跃操作摘要 |
| `follow_ms` | integer | 缺省 0，首版范围 0 至 10000 |

健康结果包含 kernel epoch、浏览器 worker、UIA worker、输入资源忙碌状态、可用采集器与当前 owner 活跃操作数。返回紧凑状态，不触发桌面全量采集。

指定操作返回已登记状态、completed_steps、uncertain_step、期望进度和最终结果。`follow_ms` 等待状态变化或超时，返回后可以继续 follow。遵循调用方 AbortSignal。

这些记录对应电脑运行时操作。每次状态查询均基于记录及真实 worker 状态。

## 10. `doraemon_stop`

| 参数 | 类型 | 规则 |
| --- | --- | --- |
| `operation_id` | string | 缺省取消当前 owner 的所有活跃操作，指定时仅取消该操作 |

stop 立即撤销后续输入资格，移除尚未执行的队列步骤，通知等待器和桥接请求取消。已发送的输入保留在结果中。

```json
{
  "success": true,
  "operations": [
    {
      "operation_id": "op_45",
      "status": "cancelling",
      "dispatch": "partial",
      "completed_steps": [0],
      "in_flight_step": 1,
      "remaining_steps_cancelled": [2, 3],
      "worker_running": true
    }
  ]
}
```

阻塞 UIA 调用可能继续运行，直到 API 返回或 worker 被回收。此时保持 `cancelling` 或 `unknown`，并继续报告 `worker_running`。worker 后续返回必须再次验证取消令牌，禁止继续派发输入。

任务取消与浏览器生命周期分开管理。stop 结束该 owner 操作并清理相应 watcher。目标关闭通过显式 close 动作表达。

## 11. 坐标与图片传递

每个 frame 在内核保存以下元数据。

- 实际送出图片的宽高和缩放比。
- 原始截图区域、裁剪区域和捕获方式。
- client、window、screen 之间的转换关系。
- 所属窗口、显示器、DPI、多显示器原点与布局 revision。

模型只输出送出图片中的像素坐标和 `frame_id`。截图缩放、局部 crop、窗口边框、客户区偏移、DPI 和负屏幕坐标均由内核转换。转换前重新确认几何版本。

现有 UIA/OCR 观察中已有归一化坐标约定，实现时先确认各路径。统一 frame 契约时保留明确转换，核对 PrintWindow 整窗与客户区捕获的不同几何来源。

P2 的 FrameGeometry 保存 source_rect、client_rect、window_rect、crop、image_size、model_rect、scale、dpi、virtual_origin 和 method。原生捕获的 coordinate_space 为 screen_physical。Playwright 使用 scale=css 截图并将 coordinate_space 记为 browser_css，映射后的点由同一 browser worker 在对应 page 的 viewport CSS 坐标派发。DOM 截图与点击使用浏览器实际坐标系统，避免重复应用 Windows DPI。模型仍只使用送出图片像素。

FrameRegistry 默认最多保留 128 帧、300 秒，记录 owner、target 与捕获时 revision。目标关闭、窗口属性消失、几何变化、页面导航和滚动均在映射前重验。窗口生命周期属性随 HWND 销毁而清除，用于识别同进程复用句柄。浏览器 tab_id 来自 CDP Target 身份，frame 使用真实对象和 document 身份，跨 CDP 连接复用同一 tab 对象。

### 图片交付验收

P0 已核验 DSH 0.2.2-rc.3 中 dsh-tools/contracts.ts、dsh-llm/types.ts、dsh-tool-fs/read-image.ts 以及 pi-ai/context.ts、deepseek/serialize.ts 的实际合同。

output.render 同步返回内容数组。execute 通过 attachments 服务保存图片并返回持久附件元数据，renderer 输出如下内容。

```js
{ type: 'image', attachment: {
  attachmentId, mediaType, bytes, width, height, name,
} }
```

attachments.saveImage 接收 Uint8Array data、mediaType 和可选 name，返回上述附件。获取图片的 fetch 传递 exec.signal，保存前后检查取消。saveImage 当前没有 signal 参数。renderer 保持纯同步，图片字节读取在 execute 完成。provider 从附件服务取回字节后构建原生多模态内容。

实际 DSH 新会话通过内置 read_image 分别向 wb/cn:hy4std 和 wb/cn:deepseek-v4.1-flash 送入 800×500 PNG。两次独立随机字符读取各 1/1 正确，来源和数值见 ACCEPTANCE 的 P0 记录。P5 按该合同接入 doraemon 的真实截图。原始 provider HTTP 请求体及图片独立价格尚未取得，证据为真实工具调用轨迹、provider 序列化源码及只存在于像素中的随机字符读取结果。

目前 `index.js` 的通用 renderer 只输出 JSON 文本。新协议的 `frames` 元数据还需要真实的 DSH 图片内容传递。

1. 查明当前安装 DSH 的工具 renderer、attachment 和模型多模态消息契约。
2. 使用最小截图验证 renderer 输出，检查实际送往模型的请求是否包含可读取的图片内容。
3. 让两类目标 Flash 模型读取截图中的小文字或定位按钮，验证图片可见且坐标与送出尺寸一致。
4. 分别测试全图、局部图、模型切换、过期资源和工具取消。

现有 bridge 普通 JSON 响应上限为 262144 字节，请求上限为 65536 字节。图片使用框架支持的原生传递方式或有界资源读取机制，避免普通 JSON 塞入大段 base64。

资源句柄绑定 owner、frame 和 TTL。Host 内部获取图片时携带 bridge 身份信息并遵循取消信号。图片在发送期间固定保留，结束后按 TTL 清理。实际资源传输可以拥有独立的大小上限。

默认六工具保持完整，内部图片获取由 Host 适配层完成。截图路径可用于本地诊断，图片交付验收必须证明模型真正读到了图像。

## 12. 并发、幂等与不确定结果

### 执行资源

- 每个目标一个串行操作队列，观察采集与写操作遵循目标 revision 的一致性规则。
- 所有 owner 共用一把物理输入锁，鼠标、键盘和依赖前台焦点的调用在锁内派发。
- 专用浏览器 worker 持有 Playwright 对象并满足线程亲和性。多个 tab 的执行仍由该 worker 调度。
- DOM 操作如果会抢占前台或影响真实焦点，同样进入物理输入协调路径。
- 所有路径统一锁顺序，先取得目标执行资格，再短时取得输入锁。等待状态时释放输入锁。
- 独立 wait 注册观察订阅后释放目标写入资格，避免等待器阻塞可以满足条件的后续动作。每次采样读取一致 revision，目标发生变化时重新核对引用。
- 操作取消或超时撤销未来输入资格。worker 在每次实际派发前再次核对操作代次。

### 幂等关联

Host 以 `exec.callId` 生成或关联稳定请求键。内核在首个可能产生副作用的步骤之前登记操作、请求指纹和初始状态。

操作登记使用 stateDir 下的轻量持久 journal，保存 owner、稳定请求键、operation_id、epoch、步骤派发阶段和已确认结果。优先使用项目可复用的数据访问或标准库 sqlite3，并将旧自主 Agent 的 SessionDB 初始化退出默认依赖。持久日志覆盖写前登记、派发中、已读回的崩溃边界。UIA/DOM/系统输入与本地日志间存在无法原子提交的窗口，该窗口恢复为未知结果。

在幂等保存期内，同 owner、同请求键且请求指纹相同的重试返回原操作，跨 kernel epoch 仍查询同一关联记录。相同键携带不同内容返回 `IDEMPOTENCY_CONFLICT`。旧目标、ref 和 frame 继续遵循 epoch 失效规则。超过幂等保存期的请求先核对现场，Host 不自动重发历史动作。

调用超时或桥接断开后，Host 优先查询已知操作 ID。已登记且状态未决时跟随原操作，避免直接重发点击、提交或输入。动作派发前后分别记录 step 状态。

首个响应丢失、Host 尚未取得 operation_id 时，通过内部 bridge 查询 owner 与请求键找回原操作。该查询由 Host 完成，保持六个公开工具的数量。

进程崩溃后的业务副作用可能无法从本地记录完整确认。新 epoch 的状态返回 `unknown` 或 `RUNTIME_RESTARTED`，模型先观察业务现场再决定恢复。实现与文档对崩溃边界如实表达。

取消状态查询只结束该次 follow。显式 stop 或原始 open/act/wait 的取消才撤销对应操作的后续执行资格。只读 observe 取消释放本次采集，持续不响应的底层调用纳入 worker 资源上限。复用 callId 的后续查询不会重新恢复已取消的输入资格。

## 13. 持久浏览器约定

默认浏览器使用 `stateDir` 下的专用持久 profile，由一个 browser worker 独占写入。profile 包含登录会话、站点数据和下载相关持久信息。

首次登录、登录失效和站点验证可以交给用户在该窗口完成。完成后继续使用相同 profile。一次操作结束、模型换会话和普通插件重载均应保留持久数据。

已有专用 profile 优先原址复用。需要搬迁时，先确认浏览器进程已经关闭，再做一次性完整复制、校验和备份，完成后原子切换路径。迁移失败保持旧 profile 可恢复。

标签页由目标注册表维护稳定身份。多个 DSH owner 可以获得各自的目标映射，底层 tab 所有权和关闭行为明确登记。操作完成只结束自身操作，关闭窗口需显式请求或插件卸载资源策略触发。

连接用户当前浏览器作为显式选择的可选能力，取决于该浏览器是否具备可用连接方式。默认 URL 路径始终使用专用持久浏览器。

## 14. 稳定错误码

| 错误码 | 恢复方式 |
| --- | --- |
| `TARGET_NOT_FOUND` | 重新列举或 open |
| `OBSERVATION_EXPIRED` | observe 后重新构造剩余动作 |
| `STALE_REF` | 使用返回的新观察选择元素 |
| `STALE_FRAME` | 获取新图并重新定位 |
| `RUNTIME_RESTARTED` | 重新发现目标并核对现场 |
| `UNSUPPORTED_ACTION` | 根据能力说明改用可用动作 |
| `EXPECTATION_TIMEOUT` | 检查最终观察与实际业务状态 |
| `TARGET_LOST` | 检查关闭、跳转或新窗口 |
| `IDEMPOTENCY_CONFLICT` | 调用方修正请求键复用 |
| `OPERATION_NOT_FOUND` | 检查 epoch、保存期和 owner 后核对现场 |
| `RUNTIME_BUSY` | 跟随已知操作或稍后查询状态 |
| `BACKEND_UNAVAILABLE` | 返回可用采集能力和降级后的观察 |
| `BRIDGE_UNAVAILABLE` | Host 重连并查询原操作 |

非法枚举、互斥参数冲突和越界输入由参数异常表达。错误的 `retryable` 只说明具备恢复路径，恢复步骤基于当前观察和 operation 状态执行。

## 15. 协议验收清单

- 新 DSH 会话默认恰好六个入口，status 健康查询可用。
- open 后取得可用 target 和初始观察，正常 act 带短后观察。
- Flash 模型能读取真实图片，并使用图片像素完成一次坐标点击。
- DOM iframe、同 URL 多 tab、UIA hwnd 复用均能保持身份正确。
- 长定位器保留完整信息，展示截断不影响执行。
- replace、insert、checkbox、select、多步骤表单均符合动作语义。
- 视觉点在滚动、缩放、DPI 和窗口移动后被正确判为失效。
- toast watcher 在点击派发前启动，采到短提示后正常清理。
- 导航和模态中断返回准确的 completed_steps 与新观察。
- 同 callId 重试复用结果，桥接断开后先核对原操作。
- 两个 owner 同时操作时物理输入串行，取消后剩余输入停止。
- UIA worker 卡住时 stop 如实返回 cancelling/unknown。
- 插件重载与浏览器重启后持久登录仍可用，旧 profile 迁移可恢复。
- 图片资源、观察缓存、watcher 与结果记录均遵守上限和保存期。

## 16. 实现阶段必须确认的外部能力

以下项目需要在阶段零以当前安装版本和真实模型调用验证，并将结论写回实施记录。

1. DSH 嵌套结构化参数的实际描述方式。
2. DSH 原生图片 renderer、资源传递和 provider 适配契约。
3. 两个目标模型对同图分辨率、文字密度、图片计费和动作协议的表现。
4. 旧专用浏览器 profile 的位置、占用状态及迁移方式。
5. Host `exec.callId` 在桥接重试和会话取消中的实际稳定性。
6. DSH session owner 的稳定来源，以及上下文缺省与 Host 重载后的对应关系。

协议规定身份、坐标、取消和副作用记录的正确性。输出预算、同步预算、采样频率及图片分辨率通过实测调整。首版 open/act/wait 同步预算建议 5000 毫秒，普通动作尽量同次返回读回，长操作到期返回可查询 handle。调优时计入新增 status 所需的模型轮次与延迟。observe 同样有界执行，慢采集登记只读 operation 并通过 status 查询，健康查询自身保持轻量。
