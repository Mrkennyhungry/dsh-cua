"""UI 操作常识：控件类型 → 动作 → 验证方式，以及 Windows 桌面 UI 惯例。

这些条目不是从数据集里抄的轨迹，而是从微软官方规范整理的**可验证规则**（来源见
每条 source）。它们被写进知识库的 index 源，供决策层和模型检索，也让"我这样点对
不对"变成可以查的问题。

为什么需要：
  * UIA 的 Button **没有必支持的 pattern**，点了不报错也不代表生效
  * TabItem 只支持 SelectionItem，**不支持 Invoke**（我们用 Select() 踩过静默失效）
  * Edit 只"条件支持" Value，输入后必须回读
  * 固定尺寸窗口没有最大化框，对"最大化"目标应直接放弃，而不是找最像的按钮

来源：
  UIA 控件类型 https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-controltypesoverview
  控件×模式映射 https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-controlpatternmapping
  标题栏规范   https://learn.microsoft.com/en-us/windows/apps/design/basics/titlebar-design
  对话框规范   https://learn.microsoft.com/en-us/windows/apps/develop/ui/controls/dialogs-and-flyouts/dialogs
"""
from __future__ import annotations

import json
import re
from pathlib import Path

MCS = "https://learn.microsoft.com/en-us/windows/win32/winauto"
DESIGN = "https://learn.microsoft.com/en-us/windows/apps/design"

# 控件类型 → 可用动作 → 验证方式。verify 用本内核已有的信号，便于程序判定。
CONTROL_PATTERNS = [
    ("Button", "Invoke", "no_required_pattern", "像素变化或窗口增删；无变化则回退真实鼠标点击",
     f"{MCS}/uiauto-controlpatternmapping"),
    ("TabItem", "SelectionItem.Select", "must", "IsSelected 转移（不支持 Invoke，别用 invoke）",
     f"{MCS}/uiauto-controlpatternmapping"),
    ("RadioButton", "SelectionItem.Select", "must", "同组仅一个 IsSelected=true",
     f"{MCS}/uiauto-controlpatternmapping"),
    ("CheckBox", "Toggle.Toggle", "must", "ToggleState On↔Off",
     f"{MCS}/uiauto-controlpatternmapping"),
    ("ListItem", "SelectionItem.Select", "must", "IsSelected=true", f"{MCS}/uiauto-controlpatternmapping"),
    ("TreeItem", "SelectionItem.Select / ExpandCollapse", "must", "IsSelected 或 ExpandCollapseState 变化",
     f"{MCS}/uiauto-controlpatternmapping"),
    ("ComboBox", "ExpandCollapse.Expand", "must", "ExpandCollapseState=Expanded 且出现 ListItem",
     f"{MCS}/uiauto-controlpatternmapping"),
    ("Edit", "Value.SetValue", "conditional", "回读 Value.Value 等于输入串（条件支持，必须回读）",
     f"{MCS}/uiauto-controlpatternmapping"),
    ("MenuItem", "Invoke", "conditional", "菜单收起 + 目标状态变化", f"{MCS}/uiauto-controlpatternmapping"),
    ("Image", "无", "does_not_support", "不可 Invoke；只能按坐标点击", f"{MCS}/uiauto-controlpatternmapping"),
    ("Window", "WindowPattern", "must", "WindowVisualState 或 BoundingRectangle 变化",
     f"{MCS}/uiauto-controlpatternmapping"),
    ("ScrollBar", "Scroll / RangeValue", "conditional", "ScrollPercent 变化", f"{MCS}/uiauto-controlpatternmapping"),
]

# Windows 桌面惯例：位置/顺序本身就是语义。
CONVENTIONS = [
    ("标题栏最右侧按钮", "关闭窗口", "window_removed", f"{DESIGN}/basics/titlebar-design"),
    ("标题栏次右按钮（存在时）", "最大化 ↔ 还原", "BoundingRectangle 变化", f"{DESIGN}/basics/titlebar-design"),
    ("标题栏第三按钮（存在时）", "最小化", "窗口 IsIconic 变为真", f"{DESIGN}/basics/titlebar-design"),
    ("标题栏空白双击", "最大化 ↔ 还原", "BoundingRectangle 变化", f"{DESIGN}/basics/titlebar-design"),
    ("窗口标题图标单击", "弹出系统菜单", "出现 Menu 元素，窗口未关闭", f"{DESIGN}/basics/titlebar-design"),
    ("窗口标题图标双击", "关闭窗口", "窗口从列表消失", f"{DESIGN}/basics/titlebar-design"),
    ("对话框最左按钮", "主操作（确认/保存）", "对话框消失 + 目标状态变化",
     f"{DESIGN}/develop/ui/controls/dialogs-and-flyouts/dialogs"),
    ("对话框最右按钮", "取消/安全动作", "对话框消失且主窗口内容未变",
     f"{DESIGN}/develop/ui/controls/dialogs-and-flyouts/dialogs"),
    ("模态对话框存在时", "父窗口不可交互", "点父窗口区域无任何像素/UIA 变化", f"{DESIGN}/develop/ui/controls/dialogs-and-flyouts/dialogs"),
    ("窗口没有 WS_MAXIMIZEBOX", "不存在最大化动作", "对「最大化」目标直接拒绝，不要找近似按钮",
     "Win32 窗口样式"),
    ("目标控件名与目标只差一个反义字", "视为反义", "拒绝或升级，不要点（最大化 vs 最小化）",
     "本地实测：字级 BM25 会把两者算成 0.62"),
    ("固定尺寸面板", "标题栏只有最小化+关闭", "只能取消/关闭，不能最大化", "Win32 窗口样式"),
]

_SLUG = re.compile(r"[^0-9a-zA-Z\u4e00-\u9fff]+")


def _slug(text: str, limit: int = 40) -> str:
    return (_SLUG.sub("-", text).strip("-")[:limit]) or "rule"


# Web 自动化的通用规则（跨任务、跨站点）。每一条都来自一次真实事故，
# 不是凭空立的规范；source 写明是哪种翻车换来的。
WEB_RULES: tuple[tuple[str, str, str, str], ...] = (
    (
        "web:selector-from-collector",
        "选择器只能来自 web_snapshot / web_dom 的采集结果，禁止按截图或习惯猜按钮文案",
        "按钮文案与采集结果逐字相等",
        "实测两次：底部按钮叫「添加」不是「应用」，删除确认按钮叫「删除」不是「确定」，"
        "靠猜的两次点击都超时失败",
    ),
    (
        "web:dom-pure-css",
        "web_dom 用 document.querySelectorAll（纯 CSS）；:has-text() / :text-is() 只有 Playwright locator 支持",
        "dom 返回错误而不是 0 个元素",
        "实测：dom 查询带 :has-text() 时静默返回 0，把“语法不对”误判成“元素不存在”，"
        "导致一次回放的 verify 假失败",
    ),
    (
        "web:input-by-placeholder",
        "input 的 text 恒为空（placeholder 不在 innerText），必须按 placeholder 属性定位",
        "input[placeholder*=...] 命中 1 个且 value 可读",
        "实测：同一弹窗里三个搜索框（搜索账户组/请输入登录账号/搜索适用对象），按文本找不到任何一个",
    ),
    (
        "web:nth-is-visible-index",
        "click 的 nth 是“可见元素列表内的序号”，优先直接使用快照给出的 nth",
        "点击后目标元素的文案与预期一致",
        "实测：快照只列可见元素并按顺序编号，而 Playwright 的 nth 含隐藏元素，两边口径不一致会点错",
    ),
    (
        "web:settle-after-click",
        "每次点击后先 web_settle 再判断，禁止点完立刻查询",
        "元素总连续 quiet_ms 不变",
        "实测：点完立刻查拿到旧状态，导致对同一页面反复做出错误判断",
    ),
    (
        "web:search-needs-enter",
        "表格/列表内的搜索框，输入后必须按 Enter 才触发过滤",
        "行数下降（例：16 行 → 2 行）",
        "实测：只输入不回车，表格 16 行保持不变，后续按行定位必然找错",
    ),
    (
        "web:check-not-coordinates",
        "复选框/勾选用 check 动作（Playwright check），不要算坐标去点",
        "is_checked 为 true 且右侧已选列表出现该对象",
        "实测：行高 51px、勾选框 16px，按坐标算错一位就会勾错人",
    ),
    (
        "web:destructive-needs-two-checks",
        "销毁性按钮必须“文案精确相等 + rect 落在目标行内”双重校验，禁止用通用 tag + 索引定位",
        "文案 == 删除 且 rect.y 在目标行区间内",
        "实测：按 button 索引点“删除”，结果点到无关链接，页面直接跳到购买页",
    ),
    (
        "web:click-success-is-not-success",
        "点击返回 success 只代表“动作已发出”，不代表生效；必须有可判读的 verify",
        "verify 观测到预期变化（列表出现/消失、value 变化等）",
        "实测：点「应用」和「确定」都返回 success=true，但什么都没发生",
    ),
    (
        "web:success-tier-t3",
        "写入知识库的成功路径至少要有 T3 证据（动作前没有、动作后有）",
        "负对照成立（如删除后行数减少且名称消失）",
        "设计结论：T0（点击返回）无证据价值，T1（界面变化）可伪造，T3 才有判别力",
    ),
    (
        "web:write-needs-gate-and-verify",
        "写操作必须 allow_mutating=true 且提供 verify；闸门默认拒绝",
        "mutations.jsonl 有对应记录且 verify 非空",
        "设计结论：没有验证方式的写操作比点错按钮更难排查",
    ),
    (
        "web:dialog-submit-exact-match",
        "弹窗/抽屉内的提交按钮判定用精确相等（添加/新建/创建/完成），不能用“包含”",
        "下拉触发器（如「添加适用范围」）不被拦截",
        "实测：用“包含 添加”判定，把下拉触发器当提交按钮拦掉，流程直接卡死",
    ),
    (
        "web:overlay-scope-selector",
        "弹窗/抽屉内的元素要带容器作用域（如 div.dialog-panel ...），同名元素靠作用域区分",
        "选择器命中 1 个可见元素",
        "实测：顶栏与抽屉各有一个「根域」下拉，裸选择器 count=2，点不到或点错",
    ),
    (
        "web:no-kernel-restart-during-replay",
        "回放过程中不要重启内核（会重置页面上下文，抽屉/弹窗丢失）；重启后必须重校验前置条件",
        "回放开始与每步前的前置条件仍成立",
        "实测：回放中重启内核后抽屉消失，流程从第 3 步直接断掉",
    ),
    (
        "web:kb-two-stores",
        "成功路径要同时写内核知识库与 DSH 知识库：内核为权威（结构化可回放），DSH 为检索门面",
        "两处都能按任务词检索到",
        "实测：只写内核库时，对话中的 knowledge_search 命中不到",
    ),
    (
        "web:t3-needs-reload",
        "T3 负对照必须在**重载页面后**复核：刷新窗口内的 DOM 与陈旧 DOM 都会给出错误读数",
        "重载 → 重新进入目标页/目录 → 状态仍符合预期",
        "实测：删除后未重载就判“名称消失”成了假阳性；另一次未重载就判“残留还在”，"
        "两次读数都是陈旧 DOM",
    ),    (
        "web:replay-protocol",
        "回放 = 前置校验 → 逐步执行并 verify → 任一步失败立刻停下交回 agent；不得为了跑通临时改选择器",
        "每步都有观测值的 verify 记录",
        "实测：三次回放分别暴露了内核 bug、实现偏离、说明不足三类问题，靠逐步 verify 才定位出来",
    ),
)


def rule_entries() -> list[dict]:
    """All seeded rules as front-matter dicts (kind=ui_rule)."""
    entries = []
    for control_type, action, support, verify, source in CONTROL_PATTERNS:
        entries.append({
            "kind": "ui_rule",
            "key": f"control:{control_type}",
            "value": f"{control_type} → {action}（{support}）→ 验证：{verify}",
            "applies_to": ["uia", "win32"],
            "verify": verify,
            "support": support,
            "source": source,
        })
    for key, value, verify, source in WEB_RULES:
        entries.append({
            "kind": "ui_rule",
            "key": key,
            "value": value,
            "applies_to": ["web"],
            "verify": verify,
            "support": "must",
            "source": source,
        })
    for scene, expected, verify, source in CONVENTIONS:
        entries.append({
            "kind": "ui_rule",
            "key": f"convention:{scene}",
            "value": f"{scene} → {expected} → 验证：{verify}",
            "applies_to": ["win32"],
            "verify": verify,
            "source": source,
        })
    return entries


def seed_knowledge(home: Path) -> int:
    """Write the rules into <home>/knowledge/index/ui-rules as markdown."""
    folder = Path(home) / "knowledge" / "index" / "ui-rules"
    folder.mkdir(parents=True, exist_ok=True)
    written = 0
    for entry in rule_entries():
        path = folder / f"{_slug(entry['key'])}.md"
        body = (f"---\n{json.dumps(entry, ensure_ascii=False)}\n---\n\n"
                f"# {entry['key']}\n\n{entry['value']}\n\n来源：{entry['source']}\n")
        if path.is_file() and path.read_text(encoding="utf-8") == body:
            continue
        path.write_text(body, encoding="utf-8")
        written += 1
    return written


def lookup(query: str, limit: int = 5) -> list[dict]:
    """Cheap keyword lookup over the rules themselves (no index, no model)."""
    terms = [t for t in re.split(r"[\s的]+", str(query or "").lower()) if t]
    if not terms:
        return []
    scored = []
    for entry in rule_entries():
        haystack = f"{entry['key']} {entry['value']}".lower()
        hits = sum(1 for term in terms if term in haystack)
        if hits:
            scored.append((hits, entry))
    scored.sort(key=lambda row: -row[0])
    return [entry for _, entry in scored[:limit]]
