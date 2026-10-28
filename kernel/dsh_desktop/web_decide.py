"""Web 场景的本地决策层：目标文本 → 元素。

`dsh_desktop/decide.py` 保持原样（桌面与 web 共用），这里只做三件 web 特有的事：

1. **类型映射**：web 元素（tag/role/class）→ `decide()` 认识的类型名，
   否则它的动词先验（"输入"要 Edit、"点击"要 Button）全部失效。
2. **族收窄**：goal 里带组件名词（菜单/表格/开关…）时，先按组件族过滤候选集，
   再交给 `decide()` 排序。这比给权重更可靠——同名元素直接被排除在外。
3. **能力门控**：goal 要"输入"但元素只能点，直接拒绝，不退而求其次。

设计见 docs/web-decide-design.md。
"""
from __future__ import annotations

from .decide import decide as _decide
from .web_components import describe_element

# goal 里的组件名词 → 组件族关键字。命中就收窄候选集。
COMPONENT_HINTS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("菜单", "左侧", "导航"), ("menu", "nav")),
    (("页签", "标签页", "选项卡"), ("tab",)),
    (("表格", "列表"), ("table", "grid", "list")),
    (("开关",), ("switch", "toggle")),
    (("勾选", "复选框", "多选"), ("checkbox",)),
    (("搜索框", "输入框", "文本框"), ("input", "search", "edit")),
    (("按钮",), ("btn", "button")),
    (("分页", "下一页", "上一页", "页码"), ("pagination", "pager")),
    (("弹窗", "对话框", "提示框"), ("dialog", "modal", "bubble", "guide", "drawer")),
    (("卡片", "指标", "数字"), ("card", "metric", "stat")),
    (("树", "组织架构树"), ("tree",)),
)

# goal 动词 → 需要的动作能力
INTENT_VERBS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("type", ("输入", "填写", "填入", "录入", "搜索", "查询", "type", "fill", "enter", "search")),
    ("select", ("选择", "选中", "下拉", "select", "choose")),
    ("check", ("勾选", "勾上", "取消勾选", "check", "tick")),
    ("toggle", ("开关", "切换", "打开开关", "关闭开关", "toggle")),
    ("click", ("点击", "打开", "进入", "回到", "点一下", "click", "open")),
)

DANGER_WORDS = ("删除", "移除", "修复", "下发", "卸载", "格式化", "清空", "重置", "禁用")
COMPONENT_BONUS_CONTENT = 0.15   # 内容族类名易变，加权减半

# goal 里的动词/组件名词会在 BM25 里变成"文档里不存在的字"，把分数稀释掉。
# decide() 是按 CJK 单字切分的："切到终端信息页签" 里 切/到/页/签 四个字不存在于
# 元素文本 "终端信息" 中，goal_weight 被这四字拉高，分数从 0.6 掉到 0.3 以下。
# 桌面场景 goal 短（"确定"）不受影响，所以这层清洗只放在 web 侧。
_STRIP_PREFIXES = ("切换到", "切到", "跳转到", "跳转", "转到", "点开", "点击", "点一下",
                   "打开", "进入", "回到", "前往", "查看", "看", "切换到", "选择",
                   "click", "open", "go to", "switch to", "navigate to")
_STRIP_SUFFIXES = ("页签", "标签页", "选项卡", "按钮", "菜单项", "菜单", "输入框",
                   "搜索框", "文本框", "下拉框", "表格", "开关", "复选框")


def query_variants(goal: str) -> list[str]:
    """原始 goal 优先，其次去掉动词/组件名词后的"目标名词"。"""
    goal = (goal or "").strip()
    variants = [goal]
    core = goal
    for _ in range(2):  # 允许同时有前缀和后缀
        changed = False
        for prefix in _STRIP_PREFIXES:
            if core.lower().startswith(prefix):
                core = core[len(prefix):].strip(" 　的")
                changed = True
                break
        for suffix in _STRIP_SUFFIXES:
            if core.endswith(suffix) and len(core) > len(suffix):
                core = core[: -len(suffix)].strip(" 　的")
                changed = True
                break
        if not changed:
            break
    if core and core != goal:
        variants.append(core)
    return variants


def infer_intent(goal: str) -> str:
    lowered = (goal or "").lower()
    for intent, verbs in INTENT_VERBS:
        if any(verb in lowered for verb in verbs):
            return intent
    return ""


def component_filter(goal: str) -> tuple[str, ...]:
    """goal 里提到的组件族关键字；空表示不做收窄。"""
    for nouns, hints in COMPONENT_HINTS:
        if any(noun in goal for noun in nouns) and "菜单" in goal:
            return hints
        if any(noun in goal for noun in nouns):
            return hints
    return ()


def build_rows(elements: list[dict]) -> list[dict]:
    """元素 → decide() 能吃的表（id/text/type/bbox），同时保留原元素索引。"""
    rows = []
    for index, raw in enumerate(elements):
        described = raw if "decide_type" in raw else describe_element(raw)
        text = (described.get("text") or "").strip()
        rows.append({"_index": index,
                     "_element": described,
                     "id": index,
                     "text": text,
                     "type": described.get("decide_type") or "Button",
                     "bbox": described.get("bbox") or [0, 0, 0, 0]})
    return rows


def decide_web(goal: str, elements: list[dict], *, top_k: int = 5,
               min_score: float = 0.35, min_gap: float = 0.10) -> dict:
    """返回 {decision, element, narrowed, intent, reason}；不做任何点击。"""
    goal = (goal or "").strip()
    if not goal:
        return {"ok": False, "reason": "goal_empty"}
    if not elements:
        return {"ok": False, "reason": "no_elements"}

    rows = build_rows(elements)
    # 空文本元素基本没法靠文本匹配（图标按钮靠 aria/title 已经进 text），
    # 但仍保留在表里：族收窄之后可能就剩它一个。
    intent = infer_intent(goal)
    hints = component_filter(goal)

    narrowed = False
    pool = rows
    if hints:
        subset = [row for row in rows
                  if any(hint in (row["_element"].get("component") or "").lower() for hint in hints)]
        if subset:
            pool, narrowed = subset, True

    decision = _decide(goal, pool, top_k=top_k, min_score=min_score, min_gap=min_gap)
    # goal 是短语时（"切到终端信息页签"），清洗后的查询往往才是真正匹配的那句。
    # 取分数更高的一次，但要求两次指向同一元素时才算确认。
    for variant in query_variants(goal)[1:]:
        extra = _decide(variant, pool, top_k=top_k, min_score=min_score, min_gap=min_gap)
        if extra.id is not None and (decision.id is None or (extra.score or 0) > (decision.score or 0)):
            if decision.id is not None and extra.id != decision.id:
                decision.ambiguous = True
            decision = extra

    chosen = None
    if decision.id is not None:
        chosen = next((row for row in pool if row["id"] == decision.id), None)

    element = None
    reason = decision.reason or ""
    if chosen is not None:
        element = chosen["_element"]
        # 能力门控：goal 要输入、元素只能点 → 直接拒绝，别点错了事后再解释
        if intent and intent not in (element.get("actions") or []) and intent != "click":
            return {"ok": False, "reason": f"element_cannot_{intent}",
                    "decision": _summary(decision), "narrowed": narrowed,
                    "intent": intent, "element": _brief(element)}
        if any(word in goal for word in DANGER_WORDS) and not element.get("actions"):
            return {"ok": False, "reason": "dangerous_without_capability",
                    "decision": _summary(decision), "narrowed": narrowed,
                    "intent": intent, "element": _brief(element)}

    return {"ok": chosen is not None and decision.id is not None,
            "reason": reason or ("matched" if chosen is not None else "no_match"),
            "decision": _summary(decision),
            "intent": intent,
            "narrowed": narrowed,
            "pool": len(pool),
            "element": _brief(element) if element else None}


def _summary(decision) -> dict:
    return {"id": decision.id,
            "confidence": round(float(decision.confidence or 0.0), 3),
            "score": round(float(decision.score or 0.0), 3),
            "gap": round(float(decision.gap or 0.0), 3),
            "ambiguous": bool(decision.ambiguous),
            "reason": decision.reason,
            "candidates": [{"id": c[0] if isinstance(c, (list, tuple)) else getattr(c, "id", None),
                            "text": (c[1] if isinstance(c, (list, tuple)) and len(c) > 1 else ""),
                            "score": round(float(c[2]), 3) if isinstance(c, (list, tuple)) and len(c) > 2 else None}
                           for c in (decision.candidates or [])][:5]}


def _brief(element: dict) -> dict:
    return {"selector": element.get("selector"),
            "text": (element.get("text") or "")[:40],
            "component": element.get("component"),
            "actions": element.get("actions"),
            "verify": element.get("verify"),
            "bbox": element.get("bbox")}
