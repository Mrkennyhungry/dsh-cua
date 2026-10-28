"""Local decision layer: pick one element for a stated goal, without an LLM.

The contract is deliberately small so a different implementation (a small
discriminator model, or the LLM) can be swapped in behind it and compared:

    decide(goal, elements) -> Decision(id, confidence, candidates)

`elements` is the table the agent already has: id, text, type, bbox. The
heuristic below needs no model, no network and no tokens; it exists to answer
"is a local decision layer even necessary?" before paying for one.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

CLICK_VERBS = ("点击", "点一下", "点开", "打开", "进入", "选择", "切换", "按下", "确认", "提交",
               "click", "open", "select", "press", "switch", "toggle")
TYPE_VERBS = ("输入", "填写", "填入", "录入", "粘贴", "搜索", "type", "fill", "enter", "search")

PLACEHOLDERS = {"icon", "ui_element", "unknown", "图标", "口", "○", "(", ")", "|"}

BUTTON_TYPES = {"button", "tabitem", "link", "hyperlink", "menuitem", "listitem",
                "treeitem", "radiobutton", "checkbox", "image", "icon", "tab"}
EDIT_TYPES = {"edit", "document", "textarea", "combobox", "text"}

# Windows convention, and it survives caption drift: caption buttons live in a
# strip at the top-right, and from left to right they are minimize / maximize /
# close. Captions for these icons were measured to move between runs, so position
# is the more trustworthy signal than the caption text.
TITLEBAR_KINDS = {
    "close": ("关闭", "关掉", "退出", "close", "quit"),
    "maximize": ("最大化", "还原", "maximize", "restore"),
    "minimize": ("最小化", "minimize"),
}
TITLEBAR_STRIP_Y = 120      # normalized: top 12% of the window
TITLEBAR_RIGHT_X = 700      # normalized: the right 30% of the width
TITLEBAR_BONUS = 0.35

# Pairs that share characters but mean the opposite. BM25 on CJK characters makes
# 最大化/最小化 nearly identical (they differ in one character), which is how
# "最大化" on a window without a maximise box resolved to the minimise button.
ANTONYMS = (
    ("最大", "最小"), ("展开", "折叠"), ("展开", "收起"), ("打开", "关闭"),
    ("确认", "取消"), ("确定", "取消"), ("是", "否"), ("开始", "停止"),
    ("启用", "禁用"), ("上一", "下一"), ("前进", "后退"), ("登录", "注销"),
    ("全选", "全不选"), ("选中", "取消选中"),
)
ANTONYM_PENALTY = 0.6

_CJK = re.compile(r"[\u4e00-\u9fff]")
_WORD = re.compile(r"[a-z0-9_]+")


def tokenize(text: str) -> list[str]:
    """CJK per character + latin per word, matching how the rest of the kernel indexes."""
    lowered = str(text or "").lower()
    return _CJK.findall(lowered) + _WORD.findall(lowered)


def normalise(text: str) -> str:
    return re.sub(r"[\s\u3000:：,，。.、·\-_/|()（）\[\]【】\"'“”‘’]+", "", str(text or "")).lower()


@dataclass
class Decision:
    id: int | None
    confidence: float
    candidates: list[dict] = field(default_factory=list)
    reason: str = ""
    score: float = 0.0
    gap: float = 0.0
    ambiguous: bool = False

    def as_dict(self) -> dict:
        return {"id": self.id, "confidence": round(self.confidence, 3),
                "score": round(self.score, 4), "gap": round(self.gap, 4),
                "ambiguous": self.ambiguous, "reason": self.reason,
                "candidates": self.candidates}


def _verb_prior(goal: str, element_type: str) -> tuple[float, str]:
    kind = str(element_type or "").lower()
    goal_l = goal.lower()
    wants_edit = any(verb in goal_l for verb in TYPE_VERBS)
    wants_click = any(verb in goal_l for verb in CLICK_VERBS)
    if wants_edit and kind in EDIT_TYPES:
        return 0.18, "type-fit"
    if wants_click and kind in BUTTON_TYPES:
        return 0.12, "click-fit"
    if wants_edit and kind in BUTTON_TYPES:
        return -0.12, "type-clash"
    if wants_click and kind in EDIT_TYPES:
        return -0.10, "type-clash"
    return 0.0, ""


def _antonym_penalty(goal: str, text: str) -> float:
    """Penalise a candidate whose meaning is the opposite of the goal.

    "最大化" and "最小化" share three of four characters, so plain BM25 scores the
    minimise button at 0.62 for a maximise goal. Sharing characters is not evidence
    when the distinguishing character says the opposite.
    """
    goal_l, text_l = normalise(goal), normalise(text)
    penalty = 0.0
    for left, right in ANTONYMS:
        if (left in goal_l and right in text_l and left not in text_l) or \
           (right in goal_l and left in text_l and right not in text_l):
            penalty = ANTONYM_PENALTY
            break
    return penalty


def _titlebar_kind(goal: str) -> str:
    lowered = goal.lower()
    for kind, words in TITLEBAR_KINDS.items():
        if any(word in lowered for word in words):
            return kind
    return ""


def _titlebar_bonus(goal: str, elements: list[dict], candidates: list[dict]) -> dict[int, float]:
    """Rank caption buttons by Windows position when the goal names one.

    The rightmost button in the top-right strip is close, the one to its left is
    maximize/restore, the one before that minimize. Applied only to candidates
    already in that strip, so it reorders them rather than inventing an answer.
    """
    kind = _titlebar_kind(goal)
    if not kind:
        return {}
    by_id = {item.get("id", index): item for index, item in enumerate(elements)}
    strip = []
    for candidate in candidates:
        item = by_id.get(candidate["id"])
        if not item:
            continue
        box = item.get("bbox") or [0, 0, 0, 0]
        if len(box) != 4:
            continue
        if box[3] <= TITLEBAR_STRIP_Y and box[0] >= TITLEBAR_RIGHT_X:
            strip.append((candidate["id"], box))
    if not strip:
        return {}
    strip.sort(key=lambda row: -row[1][2])  # rightmost first
    order = {"close": 0, "maximize": 1, "minimize": 2}
    return {element_id: TITLEBAR_BONUS for position, (element_id, _) in enumerate(strip[:3])
            if position == order[kind]}


def decide(goal: str, elements: list[dict], *, top_k: int = 5,
           min_score: float = 0.35, min_gap: float = 0.10,
           caps: dict | None = None) -> Decision:
    """Rank elements for `goal`. Pure function: no I/O, no model, deterministic.

    Two failure signals, both measured on real windows:
      * `min_score` (absolute): a goal matching nothing lands at 0.16-0.22 while a
        real match is 0.59+, so a low absolute score means "no such element".
      * `min_gap` (top1 vs top2): wrong picks sat 0.003-0.18 behind the runner-up
        while correct ones had a median gap of 0.32. Gating on `gap < 0.10` fires
        on ~22% of cases and catches ~89% of the errors.
    """
    goal_tokens = tokenize(goal)
    if not goal_tokens or not elements:
        return Decision(None, 0.0, [], "empty goal or element table")

    # Capability gate: a window without a maximise box has no maximise action at
    # all, so the goal is not "hard", it is impossible. Measured on a fixed-size
    # panel whose only top-right buttons are minimise and close: plain matching
    # resolved "最大化" to the minimise button (score 0.62) and would have clicked it.
    wanted_kind = _titlebar_kind(goal)
    if caps and wanted_kind == "maximize" and caps.get("can_maximize") is False:
        return Decision(None, 0.0, [], "window_cannot_maximize")

    docs = [tokenize(str(item.get("text") or "")) for item in elements]
    total = len(docs)
    lengths = [max(1, len(doc)) for doc in docs]
    average_length = sum(lengths) / total
    df: dict[str, int] = {}
    for doc in docs:
        for token in set(doc):
            df[token] = df.get(token, 0) + 1
    idf = {token: math.log((total + 1) / (count + 0.5)) for token, count in df.items()}
    goal_weight = sum(idf.get(token, math.log(total + 1)) for token in goal_tokens) or 1.0
    k1, b = 1.2, 0.75  # standard BM25; the length norm is what stops long labels winning

    goal_norm = normalise(goal)
    scored: list[dict] = []
    for index, item in enumerate(elements):
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        doc = docs[index]
        if not doc:
            continue
        score = 0.0
        for token in goal_tokens:
            if token not in doc:
                continue
            tf = doc.count(token)
            score += idf.get(token, 0.0) * (tf * (k1 + 1)) / (
                tf + k1 * (1 - b + b * lengths[index] / average_length))
        score /= goal_weight
        text_norm = normalise(text)
        if goal_norm and text_norm:
            if text_norm == goal_norm:
                score += 0.30
            elif goal_norm in text_norm or text_norm in goal_norm:
                score += 0.10
        prior, why = _verb_prior(goal, item.get("type", ""))
        antonym = _antonym_penalty(goal, text)
        if antonym:
            score = max(0.0, score - antonym)
            why = "antonym"
        # A type prior may only reorder real text matches; on its own it would let
        # "click anything" look like a hit for a goal that matches nothing.
        if score > 0:
            score += prior
        else:
            why = ""
        bbox = item.get("bbox") or [0, 0, 0, 0]
        area = max(0, bbox[2] - bbox[0]) * max(0, bbox[3] - bbox[1])
        if area <= 0:
            score -= 0.5
        elif area > 400_000:  # >40% of a 1000x1000 normalised frame: a container
            score -= 0.25
        scored.append({"id": item.get("id", index), "text": text[:60],
                       "type": item.get("type", ""), "score": round(score, 4),
                       "why": why})

    # Title-bar prior: only reorders candidates that already scored, so a goal that
    # matches nothing still refuses instead of clicking the nearest corner.
    bonus = _titlebar_bonus(goal, elements, scored)
    if bonus:
        for row in scored:
            extra = bonus.get(row["id"])
            # Only boost real text matches: a strip button whose caption drifted to
            # "最大化" must not win a 关闭 goal on position alone.
            if extra and row["score"] > 0:
                row["score"] = round(row["score"] + extra, 4)
                row["why"] = f"{row['why']}+titlebar" if row["why"] else "titlebar"
        scored.sort(key=lambda row: -row["score"])

    scored.sort(key=lambda row: -row["score"])
    if not scored:
        return Decision(None, 0.0, [], "no scored element")
    best = scored[0]
    second = scored[1]["score"] if len(scored) > 1 else 0.0
    # Confidence = how much the winner stands out, not its absolute score.
    gap = best["score"] - max(0.0, second)
    confidence = min(1.0, max(0.0, gap / (abs(best["score"]) + 1e-6)))
    if best["score"] <= 0:
        return Decision(None, 0.0, scored[:top_k], "no positive match")
    if best["score"] < min_score:
        return Decision(None, confidence, scored[:top_k], "below threshold",
                        score=best["score"], gap=gap)
    # A pick that barely beats the runner-up is where the wrong answers live; the
    # caller can escalate those instead of trusting them.
    return Decision(best["id"], confidence, scored[:top_k], best["why"] or "text-match",
                    score=best["score"], gap=gap, ambiguous=gap < min_gap)
