"""Web 元素的组件语义：组件族、可执行动作、验证方式、决策层类型。

为什么单独一个模块：这些规则桌面侧不需要，但 web 侧有三处要用同一份——
组件目录采集（scripts/web_catalog.py）、决策层（web_decide.py）、以及快照输出。
散在三个地方会漂移。

设计依据见 docs/web-decide-design.md：
    * 类型必须映射成 decide.py 认识的类型，否则它的动词先验失效
    * 动作要保守推导：宁可少给一个能力，也不要把容器当成可点按钮
"""
from __future__ import annotations

import json
import re
from pathlib import Path

def _load_component_catalog(path: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Load optional private component signatures; public builds have none."""
    try:
        catalog = json.loads(path.read_text(encoding="utf-8"))
        prefixes = {str(entry.get("family", "")).strip() for entry in catalog.get("entries", [])
                    if isinstance(entry, dict)} - {""}
        content = tuple(x for x in catalog.get("content_prefixes", []) if isinstance(x, str) and x)
        return tuple(sorted(prefixes, key=len, reverse=True)), content
    except (OSError, ValueError, TypeError, AttributeError):
        return (), ()


PREFIXES, CONTENT_PREFIXES = _load_component_catalog(
    Path(__file__).resolve().parents[2] / "private-knowledge" / "console-components.json"
)

# decide.py 只认识这些（见 decide.py 的 BUTTON_TYPES / EDIT_TYPES）
_DECIDE_BUTTON = {"button", "tabitem", "link", "hyperlink", "menuitem", "listitem",
                  "treeitem", "checkbox", "radiobutton", "buttoncontrol"}
_DECIDE_EDIT = {"edit", "document", "textarea", "combobox", "text"}


def family(class_names) -> str:
    """类名 → 组件族：去掉 BEM 修饰（__x / --x），取前三段。

    同一组件的 BEM 元素和修饰符归入同一个族，避免一个组件被拆成多条记录。
    """
    for name in class_names or []:
        if not isinstance(name, str):
            continue
        for prefix in PREFIXES:
            if not name.startswith(prefix):
                continue
            base = re.split(r"--|__", name, maxsplit=1)[0]
            parts = [part for part in base.split("-") if part]
            return "-".join(parts[:3])
    return ""


def is_content_class(class_names) -> bool:
    for name in class_names or []:
        if isinstance(name, str) and any(name.startswith(p) for p in CONTENT_PREFIXES):
            return True
    return False


def decide_type(element: dict) -> str:
    """web 元素 → decide.py 认识的类型名。

    映射错了的后果很具体：goal 里带"输入/搜索"时，decide() 会按 EDIT_TYPES 加分，
    元素类型不对就永远选不中那个搜索框。
    """
    tag = (element.get("tag") or "").lower()
    role = (element.get("role") or "").lower()
    cls = element.get("cls") or []
    joined = " ".join(c for c in cls if isinstance(c, str))

    if tag == "input":
        return "Edit"
    if tag == "textarea":
        return "Document"
    if tag == "select":
        return "ComboBox"
    if tag == "a":
        return "Hyperlink"
    if tag == "button":
        return "Button"
    if role in ("tab",):
        return "TabItem"
    if role in ("menuitem",):
        return "MenuItem"
    if role in ("checkbox",):
        return "CheckBox"
    if role in ("switch",) or "switch" in joined:
        return "CheckBox"
    if role in ("button", "link"):
        return "Button"
    if "tab" in joined:
        return "TabItem"
    if "pagination" in joined or "pager" in joined:
        return "Button"
    if tag in ("li", "tr"):
        return "ListItem"
    return "ListItem" if element.get("selector", "").startswith("text=") else "Button"


def actions(element: dict) -> list[str]:
    """保守推导可执行动作。空列表 = 只能定位，不能操作。"""
    tag = (element.get("tag") or "").lower()
    role = (element.get("role") or "").lower()
    cls = [c for c in (element.get("cls") or []) if isinstance(c, str)]
    joined = " ".join(cls)
    kind = decide_type(element)

    if is_content_class(cls) and kind not in ("Button", "Hyperlink"):
        # 内容区（数字卡片/图表）默认只读：能点也不给点击能力，
        # 避免"模型觉得该点就点了"。
        return []

    out: list[str] = []
    if tag in ("a", "button", "summary") or role in ("button", "tab", "menuitem", "link"):
        out.append("click")
    elif tag == "input":
        input_type = (element.get("input_type") or "").lower()
        if input_type in ("checkbox",):
            out.append("check")
        elif input_type in ("radio",):
            out.append("check")
        else:
            out.append("type")
    elif tag == "textarea":
        out.append("type")
    elif tag == "select":
        out.append("select")
    elif tag in ("li", "tr"):
        out.append("click")
    elif "switch" in joined or "checkbox" in joined:
        out.append("toggle" if "switch" in joined else "check")
    elif "pagination" in joined or "pager" in joined:
        out.append("click")
    elif role == "checkbox":
        out.append("check")
    return out


def verify_for(element: dict) -> str:
    """动作后应看到的变化——没有验证的动作等于没做。"""
    tag = (element.get("tag") or "").lower()
    kind = decide_type(element)
    cls = [c for c in (element.get("cls") or []) if isinstance(c, str)]
    joined = " ".join(cls)

    if kind in ("Edit", "Document"):
        return "value_changed"
    if kind in ("CheckBox",):
        return "state_changed"
    if "menu" in joined and tag in ("a", "li"):
        return "url_changed"
    if kind in ("Hyperlink",):
        return "url_changed"
    if kind == "TabItem" or "tab" in joined:
        return "text_changed"
    if "pagination" in joined:
        return "text_changed"
    if tag == "tr":
        return "row_selected"
    return "text_changed"


def describe_element(element: dict) -> dict:
    """给一个快照元素补上 component / actions / verify / decide_type / bbox。"""
    cls = element.get("cls") or []
    fam = family(cls)
    rect = element.get("rect") or [0, 0, 0, 0]
    return {
        **element,
        "component": fam,
        "decide_type": decide_type(element),
        "actions": actions(element),
        "verify": verify_for(element),
        "bbox": rect,
        "readonly": is_content_class(cls),
    }
