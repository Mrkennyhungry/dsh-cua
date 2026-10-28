"""Authenticated task-level DSH bridge over the existing execution kernel."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from pathlib import Path

import requests as http_client
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import desktop_tools
from . import ledger

# Playwright is thread-affine inside web_tools: every web call must run on the
# same worker or it re-attaches over CDP each time.
_WEB_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dsh-web")

# In-flight web actions. A single-worker executor cannot cancel a job that is
# already running, so when the HTTP wait expires the honest answer is "still
# running, result unknown" — not "failed". This registry is the minimum needed
# to let a caller ask again instead of re-issuing a write.
_INFLIGHT: dict[str, dict] = {}
_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT_KEEP = 50


def _inflight_start(action_id: str, action: str) -> None:
    with _INFLIGHT_LOCK:
        _INFLIGHT[action_id] = {"action_id": action_id, "action": action,
                                "state": "running", "started": time.time(),
                                "finished": None, "result": None}
        if len(_INFLIGHT) > _INFLIGHT_KEEP:
            for key in sorted(_INFLIGHT, key=lambda k: _INFLIGHT[k]["started"])[:-_INFLIGHT_KEEP]:
                _INFLIGHT.pop(key, None)


def _inflight_finish(action_id: str, state: str, result=None) -> None:
    with _INFLIGHT_LOCK:
        entry = _INFLIGHT.get(action_id)
        if entry is not None:
            entry.update(state=state, finished=time.time(), result=result)


def _inflight_get(action_id: str) -> dict | None:
    with _INFLIGHT_LOCK:
        entry = _INFLIGHT.get(action_id)
        return dict(entry) if entry else None


def _compact_observation(result):
    """Shrink an analyze result at the HTTP edge, where only the model consumes it.

    Each element arrived as text/bbox/type/source/id/click{space,x,y} — 132 B for a
    control whose actionable content is "this button, click here". `bbox` and `click`
    are the same point computed twice, `id` is array order, `click.space` repeats the
    document-level `space`.

    This must stay at the edge: `desktop_element_table` and the `goal=` resolver read
    the full element dicts straight from `desktop_analyze`, and an earlier attempt to
    strip fields inside that function silently emptied the element table.
    """

    if not isinstance(result, dict):
        return result
    elements = result.get("ui_elements")
    if not isinstance(elements, list):
        return result
    compact = []
    for element in elements:
        if not isinstance(element, dict):
            continue
        item = {"text": element.get("text", "")}
        for key in ("type", "source", "caption"):
            if element.get(key):
                item[key] = element[key]
        click = element.get("click")
        if isinstance(click, dict) and "x" in click and "y" in click:
            item["x"], item["y"] = click["x"], click["y"]
        else:
            box = element.get("bbox")
            if isinstance(box, (list, tuple)) and len(box) == 4:
                item["x"] = (int(box[0]) + int(box[2])) // 2
                item["y"] = (int(box[1]) + int(box[3])) // 2
        compact.append(item)
    out = dict(result)
    out["ui_elements"] = compact
    out["payload_bytes"] = len(json.dumps(compact, ensure_ascii=False, separators=(",", ":")))
    out["payload_bytes_full"] = len(json.dumps(elements, ensure_ascii=False, separators=(",", ":")))
    return out


class TaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task: str = Field(min_length=1, max_length=20_000)
    request_id: str = Field(min_length=1, max_length=160, pattern=r"^[a-zA-Z0-9_.:-]+$")

    @field_validator("task")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Task must not be blank")
        return value


class SteerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=10_000)

    @field_validator("message")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Message must not be blank")
        return value


class WindowBindRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hwnd: int | None = Field(default=None, ge=1)
    label: str = Field(default="", max_length=200)


class WindowForegroundRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(default="", max_length=200)


class ActivateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    wait: float = Field(default=1.0, ge=0, le=10)


class AnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    engine: str = Field(default="", max_length=32)
    caption: bool = True
    caption_icons: bool = False
    annotate: bool = True
    # window = 目标窗口客户区（默认）；desktop = 整个桌面，能看见独立弹出层。
    scope: str = Field(default="window", pattern="^(window|desktop)$")
    recover_black: bool = False
    # scope=desktop 时的解析范围：auto = 窗口 + 重叠弹层（默认），window = 只窗口，full = 全桌面。
    region: str = Field(default="auto", pattern="^(auto|window|full)$")


class CaptureBound(BaseModel):
    model_config = ConfigDict(extra="forbid")
    """Actions may pin the screenshot they were derived from (stale-capture guard).

    window_wait_ms asks the action to watch for a window it spawns; apps frequently
    open their result in a separate process, seconds later.
    """
    capture_id: str | None = Field(default=None, max_length=64)
    window_wait_ms: int = Field(default=600, ge=0, le=30000)


class ClickRequest(CaptureBound):
    x: int = Field(default=-1, ge=-1, le=1000)
    y: int = Field(default=-1, ge=-1, le=1000)
    clicks: int = Field(default=1, ge=1, le=3)
    button: str = Field(default="left", pattern="^(left|right)$")
    goal: str = Field(default="", max_length=200)
    source: str = Field(default="auto", pattern="^(auto|uia|ocr)$")
    caption: bool = False


class ScreenClickRequest(BaseModel):
    """Absolute screen pixels: no window normalization, no binding required."""
    model_config = ConfigDict(extra="forbid")
    x: int = Field(ge=-32768, le=65535)
    y: int = Field(ge=-32768, le=65535)
    clicks: int = Field(default=1, ge=1, le=3)


class ClickMessageRequest(CaptureBound):
    x: int = Field(ge=0, le=1000)
    y: int = Field(ge=0, le=1000)
    clicks: int = Field(default=1, ge=1, le=3)


class ClickAutoRequest(CaptureBound):
    x: int = Field(ge=0, le=1000)
    y: int = Field(ge=0, le=1000)
    clicks: int = Field(default=1, ge=1, le=3)
    name: str = Field(default="", max_length=200)


class TypeRequest(CaptureBound):
    """键入请求。

    评审 N6 的修复：这里原本没有 press_enter 字段，调用方传了会被 pydantic
    静默丢弃 —— 于是"URL 被当搜索词"被误诊成"Ctrl+L 不生效"，还进一步
    被当成 D5（收缩快捷键）的证据。静默丢字段比报错危险得多。
    """
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=2000)
    press_enter: bool = False
    @field_validator("text")
    @classmethod
    def nonblank_type(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Text must not be blank")
        return value


class EnsureFocusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tries: int = Field(default=3, ge=1, le=10)
    settle_ms: int = Field(default=250, ge=0, le=2000)


class CursorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    window_id: str = Field(default="", max_length=40)
    x: int = Field(default=-1, ge=-1, le=20000)
    y: int = Field(default=-1, ge=-1, le=20000)
    visible: bool = True


class KeysRequest(CaptureBound):
    text: str = Field(min_length=1, max_length=500)
    interval: float = Field(default=0.03, ge=0, le=1)


class KeyRequest(CaptureBound):
    key: str = Field(min_length=1, max_length=40)
    count: int = Field(default=1, ge=1, le=50)


class ScrollRequest(CaptureBound):
    x: int = Field(default=500, ge=0, le=1000)
    y: int = Field(default=500, ge=0, le=1000)
    clicks: int = Field(default=3, ge=-30, le=30)  # S3：与工具层 desktop_scroll 的 ±30 统一
    direction: str = Field(default="vertical", pattern="^(vertical|horizontal)$")


class SwipeRequest(CaptureBound):
    x1: int = Field(ge=0, le=1000)
    y1: int = Field(ge=0, le=1000)
    x2: int = Field(ge=0, le=1000)
    y2: int = Field(ge=0, le=1000)
    speed: float = Field(default=1.0, ge=0.1, le=5)


class UiaRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_depth: int = Field(default=7, ge=1)
    max_nodes: int = Field(default=300, ge=10, le=1500)
    target_hint: str = Field(default="", max_length=120)
    target_kind: str = Field(default="control", pattern="^(control|message|visual)$")
    max_attempts: int = Field(default=3, ge=1, le=3)
    # The HTTP surface is what the model reads, so it gets the compact payload by
    # default; internal callers of desktop_uia_tree keep the full field set.
    compact: bool = True
    control_types: str = Field(default="", max_length=200)
    text_contains: str = Field(default="", max_length=120)
    only_actionable: bool = False

    @field_validator("max_depth")
    @classmethod
    def clamp_depth(cls, value: int) -> int:
        return min(value, 10)


class PageCacheRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: str = Field(pattern="^(lookup|navigate)$")
    target: str = Field(min_length=1, max_length=120)
    control_type: str = Field(min_length=1, max_length=40)
    expected: str = Field(min_length=1, max_length=120)
    method: str = Field(default="click", pattern="^(click|invoke)$")
    enabled: bool = False


class UiaBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    steps: list[dict] = Field(min_length=1, max_length=100)
    max_depth: int = Field(default=10, ge=1, le=99)
    max_nodes: int = Field(default=600, ge=20, le=1500)
    timeout_s: float = Field(default=30.0, gt=0, le=120)


class AppFindRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)


class AppLaunchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    shortcut_path: str = Field(min_length=1, max_length=1000)
    scoped: bool = False


class DragRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x1: int = Field(ge=0, le=20000)
    y1: int = Field(ge=0, le=20000)
    x2: int = Field(ge=0, le=20000)
    y2: int = Field(ge=0, le=20000)
    speed: float = Field(default=1.0, ge=0.2, le=5)


class ClipboardTextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=100_000)


class PasteFileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=1000)


class CleanupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    close_browser: bool = True
    close_launched_apps: bool = True
    keep_window_keys: list[str] = Field(default_factory=list, max_length=100)
    keep_browser: bool = False


class WaitStableRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quiet_ms: int = Field(default=600, ge=200, le=5000)
    timeout_ms: int = Field(default=20000, ge=1000, le=120000)
    threshold: float = Field(default=1.2, gt=0, le=50)
    max_changed_cells: int = Field(default=10, ge=0, le=1000)
    interval_ms: int = Field(default=150, ge=50, le=1000)


class UiaStateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(default="", max_length=200)


class DecideRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: str = Field(min_length=1, max_length=500)
    source: str = Field(default="auto", pattern="^(auto|uia|ocr)$")
    top_k: int = Field(default=5, ge=1, le=20)
    fresh: bool = False
    caption: bool = False
    escalate: bool = False
    capture_id: str | None = Field(default=None, max_length=64)


class ElementTableRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(default="auto", pattern="^(auto|uia|ocr)$")
    fresh: bool = False
    caption: bool = False


class RuleQuery(BaseModel):
    q: str = Field(default="", max_length=200)


class WaitWindowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timeout_ms: int = Field(default=15000, ge=200, le=60000)


WEB_ACTIONS = ("open", "snapshot", "click", "type", "select", "key", "scroll",
               "hover", "extract", "wait", "status", "close")


class WebReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_elements: int = Field(default=80, ge=1, le=200)
    include_text: bool = True
    max_text_chars: int = Field(default=3000, ge=0, le=20000)


class WebActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str = Field(pattern="^(open|snapshot|click|type|select|key|scroll|hover|extract|wait|status|close|dom|frames|decide|check|set_files|settle|lease|batch)$")
    url: str = Field(default="", max_length=2000)
    selector: str = Field(default="", max_length=500)
    text: str = Field(default="", max_length=8000)
    value: str = Field(default="", max_length=200)
    key: str = Field(default="", max_length=40)
    pixels: int = Field(default=600, ge=-20000, le=20000)
    max_elements: int = Field(default=60, ge=10, le=200)
    max_chars: int = Field(default=4000, ge=100, le=20000)
    timeout_ms: int = Field(default=30000, ge=1000, le=120000)
    press_enter: bool = False
    key_name: str = Field(default="", max_length=200)
    scope: str = Field(default="", max_length=200)
    include_classes: bool = False
    frame: int = Field(default=0, ge=0, le=50)
    goal: str = Field(default="", max_length=200)
    nth: int | None = Field(default=None, ge=0, le=200)
    allow_mutating: bool = False
    verify: str = Field(default="", max_length=300)
    checked: bool = True
    # 上传文件：DOM 级 set_input_files，不弹系统对话框
    files: list[str] = Field(default_factory=list, max_length=10)
    # 单写者租约：owner 标识调用方会话，写动作必须持租约
    owner: str = Field(default="", max_length=120)
    # commit="local"：声明该步是"表单内提交"（不落库），batch 里不占用唯一写步骤名额；
    # 内核会按弹窗文本校验，含销毁性词则拒绝（不轻信声明）。
    commit: str = Field(default="", max_length=10)
    # 破坏性操作必须声明目标文本；内核验证它在页面上恰好 1 个可见匹配
    target_text: str = Field(default="", max_length=120)
    op: str = Field(default="status", max_length=20)
    ttl_s: int = Field(default=120, ge=10, le=3600)
    force: bool = False
    # ── batch：一次调用按顺序执行多步 ──────────────────────────────────────
    # 白名单动作 + 最多一个写步骤（必须是最后一步）；只用于 trusted 路径回放。
    steps: list[dict] = Field(default_factory=list, max_length=40)
    stop_on_error: bool = True
    evidence: str = Field(default="dom", pattern="^(none|dom|image)$")
    budget_ms: int = Field(default=150000, ge=1000, le=300000)


class ActByGoalRequest(BaseModel):
    """按目标自动执行：定位 + 用对应通道动作 + 自动复核。"""
    model_config = ConfigDict(extra="forbid")
    goal: str = Field(min_length=1, max_length=120)
    action: str = Field(default="click", max_length=10)
    text: str = Field(default="", max_length=2000)
    window_id: str = Field(default="", max_length=64)
    press_enter: bool = False


class LocateRequest(BaseModel):
    """分层定位请求。"""
    model_config = ConfigDict(extra="forbid")
    goal: str = Field(min_length=1, max_length=120)
    window_id: str = Field(default="", max_length=64)


class CleanupWindowsRequest(BaseModel):
    """关闭本任务登记过的窗口的请求；exe_keyword 只做缩小范围，不能选中未登记的窗口。"""
    model_config = ConfigDict(extra="forbid")
    exe_keyword: str = Field(default="", max_length=40)
    keep: int = Field(default=0, ge=0, le=20)
    scope_owner: str = Field(default="", max_length=128)


class ObserveRequest(BaseModel):
    """统一观察请求：一次拿齐窗口 + UIA + OCR。"""
    model_config = ConfigDict(extra="forbid")
    window_id: str = Field(default="", max_length=64)
    include_ocr: bool = True
    include_shot: bool = False          # 截图慢，默认不要；仅在 advice 建议时补
    include_dom: bool = True            # 浏览器窗口额外采 Playwright DOM（UIA/OCR 对网页覆盖差）
    caption: bool = False               # 图标 caption，更慢
    max_depth: int = Field(default=8, ge=1, le=20)
    max_nodes: int = Field(default=200, ge=10, le=500)
    fuse_radius_px: float = Field(default=40.0, ge=5.0, le=500.0)
    dom_timeout_s: float = Field(default=8.0, ge=1.0, le=60.0)


class DesktopCaptureRequest(BaseModel):
    """全屏截图请求。analyze=True 时同时跑 OCR/YOLO 返回元素。"""
    model_config = ConfigDict(extra="forbid")
    analyze: bool = False


class KbSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=4, ge=1, le=20)
    source: str = Field(default="all", max_length=40)


class KbRememberRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(min_length=1, max_length=40)
    key: str = Field(min_length=1, max_length=200)
    value: str = Field(min_length=1, max_length=2000)
    scope: str = Field(default="", max_length=200)
    meta: dict | None = None


class KbResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(min_length=1, max_length=40)
    key: str = Field(min_length=1, max_length=200)
    scope: str = Field(default="", max_length=200)


class KbListRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(default="", max_length=40)
    limit: int = Field(default=200, ge=1, le=1000)


class KbReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=300)


class ImagePointRequest(CaptureBound):
    image_x: int = Field(ge=0)
    image_y: int = Field(ge=0)
    image_width: int = Field(gt=0, le=100_000)
    image_height: int = Field(gt=0, le=100_000)
    clicks: int = Field(default=1, ge=1, le=3)


class TopmostRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    on: bool = True


class InvokeRequest(CaptureBound):
    name: str = Field(default="", max_length=200)
    control_type: str = Field(default="", max_length=40)
    value: str = Field(default="", max_length=2000)
    action: str = Field(default="auto", pattern="^(auto|invoke|toggle|set|select|expand)$")
    automation_id: str = Field(default="", max_length=200)


class FileCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    content: str = Field(default="", max_length=200_000)


class FileWriteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=1000)
    content: str = Field(default="", max_length=200_000)
    append: bool = False


class FileDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=1000)


# The last screen signature per window: the action that just ran produced an
# "after" signature, so the next action reuses it as its "before" instead of
# paying another probe (each probe is capped at 2.5s, and a busy window hits that
# cap). Consecutive actions on one window therefore pay one probe, not two.
_LAST_SIGNATURE: dict[str, tuple[float, list | None]] = {}


def create_router(eternity) -> APIRouter:
    token = os.getenv("DSH_DESKTOP_BRIDGE_TOKEN", "")
    lock = threading.RLock()
    tasks: dict[str, dict] = {}
    requests: dict[tuple[str, str], str] = {}
    lifecycle_scopes: dict[str, dict] = {}

    def authorize(request: Request) -> str:
        if len(token) < 32:
            raise HTTPException(503, "Bridge disabled: set DSH_DESKTOP_BRIDGE_TOKEN (32+ characters)")
        if request.headers.get("origin"):
            raise HTTPException(403, "Browser-origin requests are not allowed")
        supplied = request.headers.get("authorization", "")
        if not hmac.compare_digest(supplied.encode(), ("Bearer " + token).encode()):
            raise HTTPException(401, "Invalid bridge token")
        owner = request.headers.get("x-dsh-owner", "")
        if len(owner) != 64 or any(c not in "0123456789abcdef" for c in owner):
            raise HTTPException(400, "A SHA256 DSH owner identity is required")
        path = request.url.path.removeprefix("/api/dsh")
        lifecycle_paths = ("/windows", "/apps", "/web", "/drag", "/files", "/clipboard", "/desktop", "/cursor", "/kb")
        if any(path == prefix or path.startswith(prefix + "/") for prefix in lifecycle_paths):
            lifecycle_begin(owner)
        return owner

    router = APIRouter(prefix="/api/dsh", dependencies=[Depends(authorize)])

    def owned(task_id: str, owner: str) -> dict:
        with lock:
            task = tasks.get(task_id)
            if task is None or task["owner"] != owner:
                raise HTTPException(404, "Task not found")
            return task

    def status(task: dict) -> dict:
        sid = task["session_id"]
        running = eternity.is_running(sid)
        raw = None if running else eternity.wait_for_session(sid, timeout=0)
        result = None
        if isinstance(raw, dict):
            from .eternity import _run_end_details
            result = {**_run_end_details(raw), "final_response": str(raw.get("final_response") or "")[:12_000]}
        messages = eternity.get_session_messages(sid, limit=10).get("messages", [])
        progress = [{"role": str(m.get("role") or ""), "content": str(m.get("content") or "")[:1200]}
                    for m in messages if isinstance(m, dict)]
        return {"task_id": task["task_id"], "session_id": sid,
                "status": "running" if running else "finished" if result is not None else "unknown",
                "result": result, "progress": progress, "created_at": task["created_at"]}

    @router.get("/health")
    def health():
        # `bridge.pid` 必须是**内核自己的** pid：
        # 插件在 elevated 启动路径下拿不到 child.pid（计划任务里进程不是它 spawn 的），
        # 只能从 health 里取。之前没有这个字段，导致 /health 返回 pid=null，
        # plugin 的 attach() 记录下来也是 null，排查时无法直接定位进程。
        # 实测 2026-10-27：`desktop_health` 返回 "pid": null。
        return {"status": "ok", "protocol": 1, "scheduler": "dsh", "parser": eternity.config.parser.engine,
                "execution_enabled": bool(os.getenv("DEEPSEEK_API_KEY", "").strip()),
                "bridge": {"pid": os.getpid()},
                "ledger": ledger.summary(5)}

    @router.post("/tasks")
    def start(req: TaskRequest, owner: str = Depends(authorize)):
        digest = hashlib.sha256(req.task.encode()).hexdigest()
        with lock:
            existing = requests.get((owner, req.request_id))
            if existing:
                task = tasks[existing]
                if task["digest"] != digest:
                    raise HTTPException(409, "request_id already used for a different task")
                return {"task_id": existing, "session_id": task["session_id"], "reused": True}
            if not os.getenv("DEEPSEEK_API_KEY", "").strip():
                raise HTTPException(503, "Set DEEPSEEK_API_KEY before executing tasks")
            if len(tasks) >= 2000:
                raise HTTPException(429, "Bridge task registry is full; restart only after all tasks finish")
            if any(eternity.is_running(t["session_id"]) for t in tasks.values()):
                raise HTTPException(409, "Desktop is busy; wait for the active task")
            if any(s.get("is_running") for s in eternity.list_sessions(limit=100)):
                raise HTTPException(409, "Desktop already has an active session")
            task_id = uuid.uuid4().hex
            try:
                sid = eternity.create_session(task=req.task, source="dsh")
            except (ValueError, RuntimeError):
                raise HTTPException(409, "Execution kernel could not start the task") from None
            tasks[task_id] = {"task_id": task_id, "session_id": sid, "owner": owner,
                              "digest": digest, "created_at": time.time()}
            requests[(owner, req.request_id)] = task_id
            return {"task_id": task_id, "session_id": sid, "reused": False}

    @router.get("/tasks/{task_id}")
    def get_status(task_id: str, owner: str = Depends(authorize)):
        return status(owned(task_id, owner))

    @router.post("/tasks/{task_id}/stop")
    def stop(task_id: str, owner: str = Depends(authorize)):
        task = owned(task_id, owner)
        requested = eternity.stop_session(task["session_id"])
        return {"stop_requested": requested, **status(task)}

    @router.post("/tasks/{task_id}/steer")
    def steer(task_id: str, req: SteerRequest, owner: str = Depends(authorize)):
        task = owned(task_id, owner)
        if not eternity.is_running(task["session_id"]):
            raise HTTPException(409, "Task finished; steering must not restart desktop actions")
        if not eternity.steer_session(task["session_id"], req.message):
            raise HTTPException(409, "Task could not accept steering")
        return {"status": "steered"}

    @router.get("/tasks/{task_id}/events")
    async def events(task_id: str, request: Request, owner: str = Depends(authorize)):
        task = owned(task_id, owner)

        async def generate():
            yield "data: " + json.dumps({"event": "bridge_status", "data": status(task)}, ensure_ascii=False) + "\n\n"
            async for event in eternity.get_session_stream(task["session_id"]):
                if await request.is_disconnected():
                    break
                yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
            yield "data: " + json.dumps({"event": "bridge_status", "data": status(task)}, ensure_ascii=False) + "\n\n"

        return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @router.get("/tasks/{task_id}/image")
    def image(task_id: str, path: str, owner: str = Depends(authorize)):
        task = owned(task_id, owner)
        root = Path(eternity.config.workspace.screenshot_dir).resolve()
        image_path = Path(path).resolve()
        if not image_path.is_relative_to(root) or image_path.suffix.lower() not in {".png", ".jpeg", ".jpg"}:
            raise HTTPException(403, "Image outside screenshot directory")
        messages = eternity.get_session_messages(task["session_id"], limit=500).get("messages", [])
        refs = set()
        for message in messages:
            content = message.get("content")
            if isinstance(content, str):
                try:
                    content = json.loads(content)
                except (ValueError, TypeError):
                    content = None
            if not isinstance(content, dict):
                continue
            for key in ("image_path", "SoM_image_path"):
                value = content.get(key)
                if isinstance(value, str):
                    refs.add(str(Path(value).resolve()))
        if str(image_path) not in refs:
            raise HTTPException(403, "Image is not referenced by this task")
        if not image_path.is_file():
            raise HTTPException(404, "Image not found")
        return FileResponse(image_path)

    # ── window / analysis / action surface ────────────────────────────────
    # The DSH side is already a fully trusted local client (loopback + token +
    # owner), so it drives the same desktop_* primitives the in-kernel agent uses.
    # Results keep the tool's own {"success": false, "error": ...} shape: a
    # rejected action is data the model can read, not an HTTP failure.
    def invoke(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - normalised for the client
            raise HTTPException(500, f"{exc.__class__.__name__}: {exc}") from None

    captures: dict[str, str] = {}

    def fingerprint(window_id: str, timeout: float = 2.5):
        """Screen signature, bounded in time.

        PrintWindow blocks while the target's UI thread is busy — a modal panel
        opened by the click can hold that thread for a minute or more (measured:
        90s in some applications). A probe that cannot answer in 2.5s is "unknown", not
        something worth stalling the whole action for.
        """
        pool = ThreadPoolExecutor(max_workers=1)
        future = pool.submit(desktop_tools.desktop_effect_probe, window_id)
        try:
            probe = future.result(timeout=timeout)
        except Exception:  # noqa: BLE001 - timeout or probe failure: unknown
            return None
        finally:
            pool.shutdown(wait=False)
        if not isinstance(probe, dict) or not probe.get("success"):
            return None
        signature = probe.get("signature")
        return signature if isinstance(signature, list) else None

    def visual_delta(before, after):
        """Mean brightness delta plus how many cells changed noticeably.

        The mean alone hides a small button flipping state, so a localized change
        is also counted; either signal counts as "the screen moved".
        """
        if not before or not after or len(before) != len(after):
            return None
        diffs = [abs(a - b) for a, b in zip(before, after)]
        mean = sum(diffs) / len(diffs)
        cells = sum(1 for value in diffs if value > 15)
        return {"mean": mean, "cells": cells}

    # Last observed UIA state per (window, control): lets a readback detect change
    # without paying for a second walk before every action.
    uia_state: dict[tuple[str, str], dict] = {}

    def readback(window_id: str, name: str) -> dict:
        try:
            probe = desktop_tools.desktop_uia_state(window_id, name)
        except Exception:  # noqa: BLE001 - readback is evidence, not a gate
            return {}
        control = probe.get("control") if isinstance(probe, dict) else None
        return control if isinstance(control, dict) and control.get("found") else {}

    def act(window_id: str, fn, *args, capture_id=None, control_name=None,
            window_wait_ms: int = 600, action_timeout_s: float = 15.0,
            expect_text: str | None = None, **kwargs):
        """Run an action and report whether the window visibly changed.

        effect: confirmed (pixels moved, the target control's UIA state changed, or
        a new window appeared), suspected_noop (none of those), unverifiable (no
        probe), refused (stale capture_id).

        A new top-level window counts as confirmation on its own: apps frequently
        open their result in a separate process several seconds later, so the clicked window's pixels never move
        even though the click clearly worked.
        """
        if capture_id and capture_id != captures.get(window_id):
            effect = "refused"
            try:
                info = desktop_tools.bound_window_info(window_id)
                ledger.record(str(info.get("exe") or ""), _action_name(fn), effect,
                              title=str(info.get("title") or ""), detail="stale capture_id")
            except Exception:  # noqa: BLE001 - the ledger must never block an action
                pass
            return {"success": False, "effect": effect,
                    "error": "capture_id 与最近一次 analyze 不一致：界面可能已变化，请重新 analyze",
                    "escalation": {"recommended": "analyze", "reason": "stale_capture"}}
        logging.info('act:start %s', _action_name(fn))
        try:
            desktop_tools.desktop_highlight(window_id, True)  # show which app is being driven
        except Exception:  # noqa: BLE001 - cosmetics never block an action
            pass
        logging.info('act:highlight-done')
        windows_before = desktop_tools.window_snapshot()
        logging.info('act:snapshot-done %d', len(windows_before))
        desktop_tools._WINDOW_BASELINE[window_id] = windows_before
        cached_signature = _LAST_SIGNATURE.get(window_id)
        if cached_signature and time.monotonic() - cached_signature[0] < 60:
            before = cached_signature[1]
        else:
            before = fingerprint(window_id)
        logging.info('act:fingerprint-done')
        # Bounded execution: UIA calls block while the target's UI thread is busy
        # (a modal panel may keep the call blocked for a long time).
        # The input itself is already dispatched, so a timeout means "unknown",
        # not "failed" — and the window check below usually resolves it.
        pool = ThreadPoolExecutor(max_workers=1)
        timed_out = False
        try:
            result = pool.submit(invoke, fn, *args, **kwargs).result(timeout=action_timeout_s)
        except FuturesTimeoutError:
            timed_out = True
            result = {"success": None,
                      "error": f"动作已派发，但 {action_timeout_s:.0f}s 内未返回"
                               "（目标 UI 线程可能被弹窗或加载占住）"}
        finally:
            pool.shutdown(wait=False)
        logging.info('act:action-done timed_out=%s', timed_out)
        time.sleep(0.12)
        # Window first: a spawned window is stronger evidence than pixels, and it
        # lets us skip the pixel probe entirely — which is exactly the case where
        # that probe would block on a busy UI thread.
        window_change = desktop_tools.wait_window_change(windows_before,
                                                     budget_ms=max(0, int(window_wait_ms)))
        added, removed = window_change["added"], window_change["removed"]
        logging.info('act:window-check-done added=%d', len(added))
        change = None
        acted_gone = any(w['hwnd'] == (desktop_tools.bound_window_info(window_id) or {}).get('hwnd') for w in removed)
        if not added and not acted_gone:
            after = fingerprint(window_id)
            change = visual_delta(before, after)
            if after:
                _LAST_SIGNATURE[window_id] = (time.monotonic(), after)
        evidence = None
        if change is None:
            effect, escalation = "unverifiable", {"recommended": "analyze", "reason": "capture_unavailable"}
        elif change["mean"] >= 1.2 or change["cells"] >= 10:
            effect, escalation = "confirmed", None
        else:
            # Pixels did not move. That is often legitimate (the tab was already
            # selected), so ask the control itself before calling it a no-op.
            effect, escalation = "suspected_noop", {"recommended": "analyze", "reason": "no_visual_change"}
            # 文本类动作专用判据：像素不动不代表没输入（插入点可能在可视区外，
            # 或只有几个像素变化）。直接读控件文本里有没有刚输入的内容。
            # 实测：type 明明写进去了，却因 visual_delta=0 被判 suspected_noop，
            # 上层据此重试会造成重复输入 —— 假阴性比假阳性更隐蔽。
            if expect_text:
                try:
                    tree = desktop_tools.desktop_uia_tree(window_id, 10, 400) or {}
                    # 优先用编辑类控件的 ValuePattern 值判定（评审 Q3 建议 5）。
                    # 实测：记事本输入后 Name 可能是"文本编辑器"，而
                    # value 恒等于真实正文；只比对 Name 会永远判 noop。
                    vals = [str(c.get("value") or "")
                            for c in (tree.get("controls") or [])
                            if c.get("value")]
                    if vals:
                        joined = "\n".join(vals)
                        hit = expect_text in joined
                        effect = "confirmed" if hit else "suspected_noop"
                        escalation = None if hit else escalation
                        evidence = {"text_readback": True, "via": "value_pattern",
                                    "value_len": len(joined),
                                    "matched": hit,
                                    "value_sample": joined[:120]}
                    else:
                        # 回退：非编辑控件（或读不到 value）时用名称计数。
                        # 必须用"出现次数增加"而不是"包含"：短标记的后缀
                        # 可能与历史内容重合，单纯 in 会把上一次的文本当本次送达。
                        blob = json.dumps(tree, ensure_ascii=False, default=str)
                        key = (window_id, "__textcount__")
                        seen_before = uia_state.get(key)
                        count_now = blob.count(expect_text)
                        if seen_before is None:
                            if count_now > 0:
                                effect = "confirmed"
                                escalation = None
                                evidence = {"text_readback": True,
                                            "matched": expect_text[:40], "weak": True}
                        elif count_now > seen_before:
                            effect = "confirmed"
                            escalation = None
                            evidence = {"text_readback": True,
                                        "matched": expect_text[:40],
                                        "count_before": seen_before,
                                        "count_after": count_now}
                        uia_state[key] = count_now
                except Exception as exc:  # noqa: BLE001
                    # 不要静默吞掉：回读失败本身就是信息（之前吞掉后，
                    # evidence 里没有 text_readback，我以为是没进分支）。
                    logging.warning("text readback failed: %s", exc)
                    evidence = {"text_readback": False,
                                "readback_error": f"{type(exc).__name__}: {exc}"[:160]}
                    pass
            target = (control_name or "").strip()
            if target:
                state = readback(window_id, target)
                if state:
                    previous = uia_state.get((window_id, target.lower()))
                    uia_state[(window_id, target.lower())] = state
                    if previous is None:
                        evidence = {"uia": state, "uia_changed": None}
                    else:
                        changed = {key: state[key] for key in state
                                   if previous.get(key) != state[key]}
                        evidence = {"uia": state, "uia_changed": bool(changed),
                                    **({"uia_fields": sorted(changed)} if changed else {})}
                        if changed:
                            effect, escalation = "confirmed", None
            # 纯点击（既没有 expect_text 也没有 control_name）的结构性证据。
            # 为什么需要：点输入框/搜索框的真实效果只是「光标出现 + 焦点转移」，
            # 像素差异极小。实测 2026-10-27：
            #   点企业微信搜索框 → visual_delta=0.06（实际弹出了建议菜单）
            #   点聊天输入框   → visual_delta=0.22（实际光标进去了）
            #   输入文字       → visual_delta=0.07（实际文字进了输入框）
            # 纯像素判据必然报 suspected_noop，上层据此重试 → 重复输入。
            # 判据：GetGUIThreadInfo 拿到的 active/focus 是否落在目标窗口子树里，
            # 且与上一次记录的不同 —— 变了就是这次点击真的作用到了目标 UI。
            # （UIA 的 has_keyboard_focus 不可用：属性不存在，方法恒 False。）
            if not expect_text and not target and effect == "suspected_noop":
                try:
                    hwnd_now = int((desktop_tools.bound_window_info(window_id) or {}).get("hwnd") or 0)
                    focus_state = desktop_tools._focus_state(hwnd_now) if hwnd_now else None
                    if focus_state and focus_state.get("focus_in_target"):
                        state_key = (window_id, "__focus__")
                        # 把 is_foreground 也纳入状态：点击最常见的真实效果就是
                        # 「把窗口提到前台」，而很多应用（如 desktop）的 active/focus
                        # 都停在顶层窗口上，点不同子控件时这两个值不变。
                        current = (
                            bool(focus_state["is_foreground"]),
                            focus_state["hwnd_active"],
                            focus_state["hwnd_focus"],
                        )
                        previous_focus = uia_state.get(state_key)
                        uia_state[state_key] = current
                        if previous_focus != current:
                            effect, escalation = "confirmed", None
                            evidence = {
                                "focus_evidence": True,
                                "is_foreground": focus_state["is_foreground"],
                                "hwnd_active": focus_state["hwnd_active"],
                                "hwnd_focus": focus_state["hwnd_focus"],
                                "focus_changed": previous_focus is not None,
                                "became_foreground": bool(
                                    previous_focus is not None
                                    and not previous_focus[0]
                                    and focus_state["is_foreground"]
                                ),
                            }
                except Exception as exc:  # noqa: BLE001
                    # 不静默吞：读焦点失败本身是信息（可能前台线程拿不到）
                    logging.warning("focus readback failed: %s", exc)
        if added:
            effect, escalation = "confirmed", None
        # The acted-on window disappearing IS the effect (a close button worked).
        # Pixels cannot confirm that: there is no window left to fingerprint.
        if acted_gone:
            effect, escalation = "confirmed", None
        payload = dict(result) if isinstance(result, dict) else {"result": result}
        # ── 失败时自动诊断"是什么挡住了我" ──────────────────────────────
        # 用户的原则：给出任务就该完成，不该要求用户事先说明"会有弹窗"。
        # 所以不预设策略，而是在动作没生效时自己查清阻碍。
        if not payload.get("success", True) or effect in ("suspected_noop", "unverifiable"):
            try:
                payload["obstruction"] = _diagnose_obstruction(
                    int((desktop_tools.bound_window_info(window_id) or {}).get("hwnd") or 0),
                    window_id=window_id)
            except Exception as exc:  # noqa: BLE001
                payload["obstruction_error"] = f"{type(exc).__name__}: {exc}"[:120]
        # ── 不确定时自动截全屏 ────────────────────────────────────────────
        # 窗口级截图会掩盖遮挡（PrintWindow 把目标窗渲染出来），所以
        # 一旦结果不确定，必须看整个屏幕才知道真实发生了什么。
        # 实测教训：Notepad++ 的 update 弹窗挡在前面，窗口截图"看起来正常"，
        # 但键盘一直打到弹窗上；查了四轮才靠全屏图看清。
        if effect in ("suspected_noop", "unverifiable"):
            try:
                shot = desktop_tools.desktop_capture_desktop(analyze=False)
                if shot.get("success"):
                    payload["desktop_screenshot"] = shot.get("image_path") or shot.get("path")
                    payload["foreground_now"] = shot.get("foreground")
            except Exception as exc:  # noqa: BLE001
                payload["desktop_screenshot_error"] = f"{type(exc).__name__}: {exc}"[:120]
        payload["effect"] = effect
        # Say which window was acted on: the same goal ("关闭") resolves to a
        # different button depending on the bound window, and closing the wrong one
        # is exactly the mistake worth catching in the log.
        try:
            acted = desktop_tools.bound_window_info(window_id) or {}
            payload["acted_window"] = {"window_id": window_id,
                                       "title": str(acted.get("title") or ""),
                                       "exe": str(acted.get("exe") or "")}
        except Exception:  # noqa: BLE001
            payload["acted_window"] = {"window_id": window_id}
        payload["windows_added"] = [{"hwnd": w["hwnd"], "title": w["title"], "exe": w["exe"]}
                                    for w in added][:5]
        payload["windows_removed"] = [{"hwnd": w["hwnd"], "title": w["title"]}
                                      for w in removed][:5]
        payload["window_evidence"] = "new_window" if added else ("window_closed" if acted_gone else None)
        if timed_out:
            payload["timed_out"] = True
        payload["visual_delta"] = None if change is None else round(change["mean"], 2)
        payload["visual_cells"] = None if change is None else change["cells"]
        if evidence:
            payload["evidence"] = evidence
        if capture_id:
            payload["capture_id"] = capture_id
        if escalation:
            payload["escalation"] = escalation
        try:
            info = desktop_tools.bound_window_info(window_id)
            if info:  # only real bound windows belong in the capability ledger
                ledger.record(str(info.get("exe") or ""), _action_name(fn), effect,
                              title=str(info.get("title") or ""),
                              detail=str(payload.get("error") or "")[:120])
        except Exception:  # noqa: BLE001 - the ledger must never block an action
            pass
        return payload

    def _action_name(fn) -> str:
        name = str(getattr(fn, "__name__", "action"))
        return name[len("desktop_"):] if name.startswith("desktop_") else name

    @router.get("/windows")
    def windows():
        return invoke(desktop_tools.desktop_list_windows)

    @router.post("/windows/bind")
    def bind_window(req: WindowBindRequest, owner: str = Depends(authorize)):
        result = invoke(desktop_tools.desktop_pick_window, hwnd=req.hwnd, label=req.label)
        if result.get("success") and result.get("window_id"):
            with desktop_tools._STATE._lock:
                info = desktop_tools._STATE._windows.get(result["window_id"])
                if info is not None:
                    info["scope_owner"] = owner
        return result

    @router.post("/windows/foreground")
    def bind_foreground(req: WindowForegroundRequest, owner: str = Depends(authorize)):
        result = invoke(desktop_tools.desktop_pick_foreground, label=req.label)
        if result.get("success") and result.get("window_id"):
            with desktop_tools._STATE._lock:
                info = desktop_tools._STATE._windows.get(result["window_id"])
                if info is not None:
                    info["scope_owner"] = owner
        return result

    @router.get("/windows/bound")
    def bound_windows():
        return invoke(desktop_tools.desktop_list_bound_windows)

    @router.post("/windows/{window_id}/unbind")
    def unbind_window(window_id: str):
        return invoke(desktop_tools.desktop_unpick_window, window_id)

    @router.post("/windows/{window_id}/activate")
    def activate_window(window_id: str, req: ActivateRequest):
        return invoke(desktop_tools.desktop_switch_window, window_id, req.wait)

    @router.post("/windows/{window_id}/app-profile")
    def app_profile(window_id: str):
        return invoke(desktop_tools.desktop_app_profile, window_id)

    @router.post("/windows/{window_id}/close-window")
    def close_window_soft(window_id: str):
        return invoke(desktop_tools.desktop_close_window_soft, window_id)

    @router.post("/windows/{window_id}/ensure-focus")
    def ensure_focus(window_id: str, req: EnsureFocusRequest):
        return invoke(desktop_tools.desktop_ensure_focus, window_id, tries=req.tries, settle_ms=req.settle_ms)

    @router.post("/windows/{window_id}/analyze")
    def analyze_window(window_id: str, req: AnalyzeRequest):
        result = invoke(desktop_tools.desktop_analyze, window_id, engine=req.engine, caption=req.caption,
                        caption_icons=req.caption_icons, annotate=req.annotate,
                        scope=req.scope, recover_black=req.recover_black, region=req.region)
        result = _compact_observation(result)
        if isinstance(result, dict) and not result.get("error"):
            capture_id = uuid.uuid4().hex
            captures[window_id] = capture_id
            result["capture_id"] = capture_id
        return result

    @router.post("/windows/{window_id}/uia")
    def window_uia(window_id: str, req: UiaRequest):
        result = invoke(
            desktop_tools.desktop_uia_tree, window_id,
            max_depth=req.max_depth, max_nodes=req.max_nodes,
            target_hint=req.target_hint, target_kind=req.target_kind,
            max_attempts=req.max_attempts,
            compact=req.compact, control_types=req.control_types,
            text_contains=req.text_contains, only_actionable=req.only_actionable,
        )
        if isinstance(result, dict) and isinstance(result.get("analysis"), dict):
            result = {**result, "analysis": _compact_observation(result["analysis"])}
        return result

    @router.post("/windows/{window_id}/page-cache")
    def window_page_cache(window_id: str, req: PageCacheRequest):
        """Verified navigation cache. Lookup is advisory; navigate rechecks live UIA."""
        from . import page_cache
        import win32gui

        info = desktop_tools.bound_window_info(window_id) or {}
        hwnd = int(info.get("hwnd") or 0)
        if not hwnd or not win32gui.IsWindow(hwnd) or win32gui.GetForegroundWindow() != hwnd:
            return {"success": False, "error": "CACHE_WINDOW_NOT_FOREGROUND"}
        if req.target == req.expected:
            return {"success": False, "error": "CACHE_EXPECTED_MUST_DIFFER"}
        def cache_signature():
            pool = ThreadPoolExecutor(max_workers=1)
            try:
                return pool.submit(page_cache.window_signature, hwnd).result(timeout=2.5)
            finally:
                pool.shutdown(wait=False, cancel_futures=True)

        try:
            context, origin = page_cache.window_context(hwnd, info)
            width, height = context["size"]
            before_sig = cache_signature()
            if before_sig is None:
                raise ValueError("fingerprint unavailable")
            key = page_cache.make_key(context, before_sig, req.target, req.control_type, req.expected, req.method)
            cached = page_cache.lookup(key) if req.enabled else None
        except Exception as exc:
            return {"success": False, "error": "CACHE_OBSERVATION_FAILED", "detail": str(exc)[:160]}
        if req.op == "lookup":
            return {"success": True, "cache_hit": bool(cached),
                    "candidate": ({"target": req.target, "control_type": req.control_type,
                                   "bbox": cached.get("bbox"), "verified_runs": cached.get("verified_runs")}
                                  if cached else None),
                    "advisory_only": True, "hint": "Revalidate live target before any action."}

        # No cached result can substitute for a live exact and unambiguous UIA
        # match. Inspect the complete descendants, not a truncated UIA tree.
        def scan():
            return page_cache.scan_uia(hwnd, req.target, req.control_type, req.expected)

        try:
            targets, before_expected = scan()
            if len(targets) != 1 or not targets[0]["enabled"] or before_expected:
                raise ValueError("target ambiguous/disabled or expected already present")
            r = targets[0]["rect"]
            bbox = [round((r[0]-origin[0])*1000/width), round((r[1]-origin[1])*1000/height),
                    round((r[2]-origin[0])*1000/width), round((r[3]-origin[1])*1000/height)]
            if req.method == "click" and (bbox[2] <= bbox[0] or bbox[3] <= bbox[1]
                                          or any(v < 0 or v > 1000 for v in bbox)):
                raise ValueError("target has no usable in-window rectangle")
            if cached and cached.get("bbox") != (bbox if req.method == "click" else None):
                page_cache.invalidate(key)
                cached = None
            # The page may change while UIA is being scanned.
            if not page_cache.visually_stable(before_sig, cache_signature()) or win32gui.GetForegroundWindow() != hwnd:
                raise ValueError("page changed during live verification")
        except Exception as exc:
            if cached:
                page_cache.invalidate(key)
            return {"success": False, "cache_hit": bool(cached), "error": "CACHE_PRECONDITION_FAILED",
                    "detail": str(exc)[:160], "action_performed": False}

        try:
            if req.method == "click":
                x, y = round((bbox[0] + bbox[2])/2), round((bbox[1] + bbox[3])/2)
                result = act(window_id, desktop_tools.desktop_click, window_id, x, y, 1)
            else:
                result = act(window_id, desktop_tools.desktop_uia_invoke, window_id, req.target,
                             req.control_type, "", "auto", control_name=req.target)
        except Exception as exc:
            if cached:
                page_cache.invalidate(key)
            return {"success": False, "cache_hit": bool(cached), "cache_written": False,
                    "action_performed": None, "error": "CACHE_ACTION_UNCERTAIN",
                    "detail": str(exc)[:160], "hint": "Read back current state; do not blindly retry."}
        try:
            _, after_expected = scan()
            verified = bool(result.get("success")) and result.get("effect") != "refused" and after_expected == 1
        except Exception:
            verified = False
        written = False
        if verified and req.enabled:
            try:
                page_cache.store_verified(key, bbox if req.method == "click" else None)
                written = True
            except Exception as exc:
                logging.warning("page cache write failed: %s", exc)
        elif not verified and cached:
            page_cache.invalidate(key)
        return {"success": verified, "cache_hit": bool(cached), "cache_written": written,
                "verified": verified,
                "action_performed": (True if result.get("success") is True else
                                     False if result.get("effect") == "refused" else None),
                "action_result": result,
                **({} if verified else {"error": "CACHE_POSTCONDITION_UNVERIFIED"})}

    @router.post("/windows/{window_id}/uia-batch")
    def window_uia_batch(window_id: str, req: UiaBatchRequest):
        return invoke(desktop_tools.desktop_uia_batch, window_id, req.steps, max_depth=req.max_depth,
                      max_nodes=req.max_nodes, timeout_s=req.timeout_s)

    @router.post("/windows/{window_id}/invoke")
    def window_invoke(window_id: str, req: InvokeRequest):
        return act(window_id, desktop_tools.desktop_uia_invoke, window_id, req.name, req.control_type,
                   req.value, req.action, req.automation_id,
                   capture_id=req.capture_id, control_name=req.name or req.automation_id)

    @router.post("/windows/{window_id}/click")
    def window_click(window_id: str, req: ClickRequest):
        if req.button == "right":
            return act(window_id, desktop_tools.desktop_right_click, window_id, req.x, req.y,
                       capture_id=req.capture_id)
        return act(window_id, desktop_tools.desktop_click, window_id, req.x, req.y, req.clicks,
                   goal=req.goal, source=req.source, caption=req.caption,
                   window_wait_ms=req.window_wait_ms,
                   capture_id=req.capture_id)

    @router.post("/windows/{window_id}/click-message")
    def window_click_message(window_id: str, req: ClickMessageRequest):
        return act(window_id, desktop_tools.desktop_click_message, window_id, req.x, req.y, req.clicks,
                   capture_id=req.capture_id)

    @router.post("/windows/{window_id}/click-auto")
    def window_click_auto(window_id: str, req: ClickAutoRequest):
        return act(window_id, desktop_tools.desktop_click_auto, window_id, req.x, req.y, req.clicks, req.name,
                   window_wait_ms=req.window_wait_ms,
                   capture_id=req.capture_id, control_name=req.name)

    @router.post("/screen/click")
    def screen_click(req: ScreenClickRequest):
        """Click at absolute screen pixels (popups live outside the client rect)."""
        return invoke(desktop_tools.desktop_click_screen, req.x, req.y, req.clicks)

    @router.post("/cleanup/windows")
    def cleanup_windows(req: CleanupWindowsRequest):
        """关闭**本任务登记过**的窗口。

        只能命中 desktop_launch_app 记录过、且句柄身份仍匹配的窗口；
        exe_keyword 仅用于在登记集合内缩小范围。遇到保存确认框会保留现场并
        报告失败，不会替用户选择"不保存"。
        """
        return invoke(desktop_tools.desktop_cleanup_windows_by_exe,
                      exe_keyword=req.exe_keyword, keep=int(req.keep),
                      scope_owner=req.scope_owner)

    @router.post("/windows/{window_id}/uia-menu")
    def window_uia_menu(window_id: str):
        """读取当前展开的菜单项（专门工具，不占 uia_tree 的节点预算）。

        为什么需要：记事本这类带工具栏的窗口，uia_tree 在 300/1500 个节点内
        常常遍历不到菜单项，于是"菜单明明展开着，uia_tree 却只有 3 个 MenuItem"。
        """
        return invoke(desktop_tools.desktop_uia_menu, window_id)

    @router.post("/windows/{window_id}/capture")
    def window_capture(window_id: str):
        return invoke(desktop_tools.desktop_capture_window, window_id)

    @router.post("/act_by_goal")
    def act_by_goal(req: ActByGoalRequest):
        """按目标自动选择通道执行：定位到之后用对应方式点/输入。

        agent 只说"输入搜索词"，内核自己决定用 Playwright 还是 UIA 还是 OCR。
        执行方式与定位来源必须一致，否则会"用 A 的坐标打 B 的界面"：
          playwright → web_click/web_type（selector，最精确）
          uia        → 窗口坐标点击（bbox 中心）
          ocr        → 窗口坐标点击（bbox 中心）
        """
        from . import web_tools as web   # act_by_goal 需要用 Playwright 通道
        loc = locate_element(LocateRequest(goal=req.goal, window_id=req.window_id))
        if not loc.get("success"):
            return {"success": False, "goal": req.goal, "stage": "locate",
                    "source": loc.get("source"), "hint": loc.get("hint"),
                    "fallback_reason": loc.get("fallback_reason"),
                    "locate": {k: v for k, v in loc.items() if k != "advice"}}

        source = loc.get("source")
        hit = loc.get("hit") or {}
        wid = (req.window_id or "").strip()
        executed: dict = {}
        verified: dict = {}

        if source == "playwright":
            selector = hit.get("selector") or ""
            # 必须走 _WEB_EXECUTOR：Playwright 线程亲和（同 observe 的理由）
            if req.action == "type":
                _f = _WEB_EXECUTOR.submit(
                    web.web_type, selector, req.text or "",
                    bool(req.press_enter), 10000, int(hit.get("frame") or 0))
            else:
                _f = _WEB_EXECUTOR.submit(
                    web.web_click, selector, 10000, int(hit.get("frame") or 0),
                    hit.get("nth"), False, "")
            try:
                executed = _f.result(timeout=45) or {}
            except FuturesTimeoutError:
                executed = {"success": False, "error": "web action timeout"}
        elif source == "uia":
            bbox = hit.get("bbox") or []
            if len(bbox) != 4:
                return {"success": False, "goal": req.goal, "stage": "execute",
                        "error": {"code": "NO_BBOX", "hint": "UIA 命中但没有 bbox，无法定位点击位置"}}
            if not wid:
                return {"success": False, "goal": req.goal, "stage": "execute",
                        "error": {"code": "NO_WINDOW", "hint": "需要 window_id 才能点 UIA 元素"}}
            executed = _click_bbox_with_retry(wid, bbox)
            if req.action == "type" and executed.get("success"):
                executed["type_result"] = desktop_tools.desktop_type_text(wid, req.text or "")
        elif source == "ocr":
            bbox = hit.get("bbox") or []
            if len(bbox) != 4:
                return {"success": False, "goal": req.goal, "stage": "execute",
                        "error": {"code": "NO_BBOX", "hint": "OCR 命中但没有 bbox"}}
            if not wid:
                return {"success": False, "goal": req.goal, "stage": "execute",
                        "error": {"code": "NO_WINDOW", "hint": "需要 window_id 才能点 OCR 元素"}}
            # OCR bbox 宽度不稳定（实测同一词 24/88 两种），必须多位置重试
            executed = _click_bbox_with_retry(wid, bbox)
            if req.action == "type" and executed.get("success"):
                executed["type_result"] = desktop_tools.desktop_type_text(wid, req.text or "")
        else:
            return {"success": False, "goal": req.goal, "stage": "execute",
                    "error": {"code": "UNKNOWN_SOURCE", "hint": f"未知来源 {source!r}"}}

        # 执行后自动复核：重新定位一次，看还能不能找到（点了会消失/变化）
        time.sleep(0.8)
        after = locate_element(LocateRequest(goal=req.goal, window_id=req.window_id))
        verified = {
            "still_findable": bool(after.get("success")),
            "after_source": after.get("source"),
        }
        ok = bool(executed.get("success")) or bool(executed.get("clicked"))
        return {
            "success": ok,
            "goal": req.goal,
            "source": source,
            "hit": hit,
            "executed": executed,
            "verify": verified,
            "alignment": loc.get("alignment"),
        }

    @router.post("/locate")
    def locate_element(req: LocateRequest):
        """按作用域分层定位：网页内用 Playwright，浏览器壳/系统用 UIA，最后 OCR 兜底。

        为什么不是"失败了才降级"：三者看到的"东西"本来就不同。
          · Playwright 只看得见网页内容（实测 149/149 有 selector）
          · UIA 在浏览器里只看得见浏览器壳（地址栏/标签页），看不见网页内容
          · OCR 看得见像素，但要自己判断能否点击
        所以按作用域选源，比按失败顺序降级更准。
        """
        goal = (req.goal or "").strip()
        if not goal:
            return {"success": False, "error": {"code": "GOAL_REQUIRED", "hint": "goal 不能为空"}}

        wid = (req.window_id or "").strip()
        obs = observe_window(ObserveRequest(
            window_id=wid, include_ocr=True, include_dom=True,
            include_shot=False, max_nodes=300))
        if not obs.get("success"):
            return obs

        needle = goal.lower()
        hit = None
        source = ""
        reasons: list[str] = []

        # ── 第 1 层：Playwright（网页内容，唯一可靠）──
        dom_els = (obs.get("dom") or {}).get("elements") or []
        if dom_els:
            for e in dom_els:
                t = (e.get("text") or "")
                if needle in t.lower() and (e.get("selector") or "").strip():
                    hit = {"text": t[:60], "selector": e.get("selector"),
                           "nth": e.get("nth"), "frame": e.get("frame"),
                           "in_overlay": e.get("in_overlay")}
                    source = "playwright"
                    break
            if not hit:
                reasons.append(f"DOM 有 {len(dom_els)} 个元素但无匹配")
        else:
            reasons.append("当前窗口没有 DOM（非浏览器或未连上 Playwright）")

        # ── 第 2 层：UIA（浏览器壳 / 系统控件）──
        if not hit:
            ctrls = (obs.get("uia") or {}).get("controls") or []
            for c in ctrls:
                nm = (c.get("name") or "")
                if needle in nm.lower():
                    hit = {"name": nm[:60], "control_type": c.get("control_type"),
                           "bbox": c.get("bbox"), "auto_id": c.get("auto_id")}
                    source = "uia"
                    break
            if not hit:
                reasons.append(f"UIA 有 {len(ctrls)} 个控件但无匹配")

        # ── 第 3 层：OCR（视觉兜底）──
        if not hit:
            oels = (obs.get("ocr") or {}).get("elements") or []
            for e in oels:
                t = (e.get("text") or "")
                if needle in t.lower():
                    hit = {"text": t[:60], "bbox": e.get("bbox")}
                    source = "ocr"
                    break
            if not hit:
                reasons.append(f"OCR 有 {len(oels)} 个文本块但无匹配")

        result = {
            "success": bool(hit),
            "goal": goal,
            "source": source,
            "hit": hit,
            "fallback_reason": " → ".join(reasons) if reasons else "",
            "alignment": obs.get("alignment"),
            "advice": obs.get("advice"),
            "duration_ms": obs.get("duration_ms"),
        }

        # ── 全失败：给可执行的下一步，而不是一句"没找到" ──
        if not hit:
            result["success"] = False
            # 提示必须对得上窗口类型。之前不分情况一律说"浏览器窗口"，
            # 结果在记事本里也提示"绑定错了浏览器"——完全误导。
            is_browser = bool(dom_els) or any(
                k in ((obs.get("windows") or [{}])[0].get("exe") or "").lower()
                for k in ("msedge", "chrome", "firefox", "browser"))
            fg = (obs.get("alignment") or {}).get("same_browser")
            if is_browser and fg is False:
                result["hint"] = ("绑定的窗口不是 Playwright 操作的那个浏览器，"
                                  "两边页面不同。先绑定正确的窗口再尝试。")
            elif is_browser and not dom_els:
                result["hint"] = ("浏览器窗口但没有 DOM：先用 web_open 连上，"
                                  "或改用窗口坐标点击（UIA/OCR）。")
            else:
                result["hint"] = (f"这看起来是原生应用窗口（非浏览器）。"
                                  f"界面上现有 UIA {len((obs.get('uia') or {}).get('controls') or [])} 个控件、"
                                  f"OCR {len((obs.get('ocr') or {}).get('elements') or [])} 段文字，"
                                  "都没有匹配目标。可尝试：①换个更短的关键词；"
                                  "②若目标是菜单项，先展开父菜单（点一下父级再找）；"
                                  "③补截图确认界面上真实文字。")
        return result

    @router.post("/desktop/capture")
    def desktop_capture(req: DesktopCaptureRequest):
        """全屏截图：何时"不确定"就看它。

        窗口级 capture 走 PrintWindow，会把目标窗渲染出来——被弹窗/更新提示/
        输入法候选挡住时完全看不出来（实测踩过：Notepad++ 的 update 弹窗挡在前面，
        窗口截图里目标窗"看起来正常"，但键盘其实打到了弹窗上）。
        全屏截图能看到真实遮挡，配合返回的 foreground 就能判断焦点归属。
        """
        return invoke(desktop_tools.desktop_capture_desktop, analyze=bool(req.analyze))

    @router.post("/observe")
    def observe_window(req: ObserveRequest):
        """统一观察：一次返回 窗口列表 + UIA 控件树 + OCR 元素（可选截图）。

        用户指出的问题：uia / analyze / windows 是三个独立调用，
        AI 每次只能看到一种视图，只能靠猜或多轮往返拼凑。
        这三者本是同一时刻的三种视图，必须一起判断。

        默认不含截图（耗时 3-15s），但会给出"是否建议补截图"的判断：
          · UIA 覆盖差（有名控件过少）→ 建议
          · OCR 与 UIA 明显冲突（同一位置文字不一致）→ 建议
          · 文本含 / \ 等 UIA 会丢的字符 → 建议
        """
        started = time.time()
        out: dict = {"success": True, "window_id": req.window_id or "", "at": time.strftime("%Y-%m-%dT%H:%M:%S")}

        # ① 窗口列表（最便宜，永远给）
        try:
            wins = desktop_tools.desktop_list_windows() or {}
            out["windows"] = [
                {"hwnd": w.get("hwnd"), "title": w.get("title"), "exe": w.get("exe"),
                 "rect": w.get("rect"), "window_id": w.get("window_id")}
                for w in (wins.get("windows") or [])
            ]
        except Exception as exc:  # noqa: BLE001
            out["windows"] = []
            out["windows_error"] = f"{type(exc).__name__}: {exc}"[:120]

        wid = (req.window_id or "").strip()
        if not wid:
            fg = desktop_tools.desktop_pick_foreground("") or {}
            wid = fg.get("window_id") or ""
            out["auto_picked"] = bool(wid)
            if fg.get("window"):
                out["foreground"] = fg["window"]

        # ①.5 对齐校验：绑定的窗口是不是 Playwright 正在操作的那个浏览器？
        # 用户桌面可能同时有多个 Edge（自己的 + dsh-cua 的），按顺序取第一个
        # 会读到别人的页面 —— 上一轮实验的 UIA 数据就是这样错位的。
        playwright_pids: set[int] = set()
        try:
            import psutil as _ps
            for proc in _ps.process_iter(["pid", "name", "cmdline"]):
                nm = (proc.info.get("name") or "").lower()
                if "msedge" not in nm and "chrome" not in nm:
                    continue
                cmd = " ".join(proc.info.get("cmdline") or [])
                if "remote-debugging-port=9333" in cmd:
                    playwright_pids.add(int(proc.pid))
        except Exception:
            pass
        if wid:
            bound_pid = None
            for w in (out.get("windows") or []):
                if str(w.get("hwnd")) == str((out.get("foreground") or {}).get("hwnd")):
                    bound_pid = w.get("pid")
            if bound_pid is None:
                # 从前台拿不到就用绑定信息
                try:
                    info = desktop_tools.bound_window_info(wid) or {}
                    bound_pid = info.get("pid")
                except Exception:
                    bound_pid = None
            out["alignment"] = {
                "bound_window_pid": bound_pid,
                "playwright_pids": sorted(playwright_pids)[:6],
                "same_browser": (bound_pid in playwright_pids) if bound_pid else None,
            }
            if bound_pid and playwright_pids and bound_pid not in playwright_pids:
                out["alignment"]["warning"] = (
                    "绑定的窗口不是你正在用 Playwright 操作的那个浏览器"
                    f"（绑定 pid={bound_pid}，Playwright pid={sorted(playwright_pids)[:3]}）。"
                    "两边页面可能不同，DOM 与 UIA 数据会错位 —— 请绑定正确的窗口。"
                )

        uia_controls: list = []
        # ② UIA 控件树
        if wid:
            try:
                u = desktop_tools.desktop_uia_tree(wid, max_depth=int(req.max_depth), max_nodes=int(req.max_nodes)) or {}
                uia_controls = u.get("controls") or []
                out["uia"] = {
                    "control_count": len(uia_controls),
                    "named_count": sum(1 for c in uia_controls if (c.get("name") or "").strip()),
                    "capability": u.get("uia_capability"),
                    "coverage": u.get("uia_coverage"),
                    "controls": uia_controls[: int(req.max_nodes)],
                }
            except Exception as exc:  # noqa: BLE001
                out["uia"] = {"error": f"{type(exc).__name__}: {exc}"[:120]}

            # ③ OCR 元素（可选，慢）
            if req.include_ocr:
                try:
                    an = desktop_tools.desktop_analyze(wid, caption=bool(req.caption), annotate=False) or {}
                    els = an.get("ui_elements") or []
                    out["ocr"] = {
                        "element_count": len(els),
                        "engine": an.get("parse_engine_used"),
                        "timing_ms": an.get("timing_ms"),
                        "elements": els[: int(req.max_nodes)],
                    }
                except Exception as exc:  # noqa: BLE001
                    out["ocr"] = {"error": f"{type(exc).__name__}: {exc}"[:120]}

            # ④ 浏览器窗口额外采 DOM（Playwright）：UIA/OCR 对浏览器内网页覆盖很差，
            #    DOM 才是权威。三个视图合并判断，比任何单一来源都可靠。
            dom_els: list = []
            if req.include_dom:
                try:
                    from . import web_tools as _web
                    # 两个硬约束（评审 N2）：
                    #   1) 必须走 _WEB_EXECUTOR —— Playwright 线程亲和，
                    #      在 FastAPI 线程池里直接调会跨线程 attach，并可能
                    #      泄漏 sync_playwright 导致后续每次调用都报
                    #      "Sync API inside the asyncio loop"（实测确认，7/12 上传失败出自这里）
                    #   2) 用 web_peek_snapshot —— 观察不得启动浏览器
                    _fut = _WEB_EXECUTOR.submit(
                        _web.web_peek_snapshot, int(req.max_nodes), False)
                    try:
                        snap = _fut.result(timeout=float(req.dom_timeout_s)) or {}
                    except FuturesTimeoutError:
                        snap = {"success": False, "available": False, "error": "dom timeout"}
                    if snap.get("success"):
                        dom_els = snap.get("elements") or []
                        out["dom"] = {
                            "element_count": len(dom_els),
                            "url": snap.get("url"),
                            "title": snap.get("title"),
                            "page_sig": snap.get("page_sig"),
                            "elements": dom_els[: int(req.max_nodes)],
                        }
                    else:
                        out["dom"] = {"error": str(snap.get("error") or "")[:120], "available": False}
                except Exception as exc:  # noqa: BLE001
                    out["dom"] = {"error": f"{type(exc).__name__}: {exc}"[:120], "available": False}

            # ⑤ 融合视图：按 bbox 中心把 OCR 文字挂到最近的 UIA 控件上
            fused: list = []
            ocr_els = (out.get("ocr") or {}).get("elements") or []
            for c in uia_controls:
                bb = c.get("bbox") or []
                name = (c.get("name") or "").strip()
                item = {"source": "uia", "control_type": c.get("control_type"),
                        "name": name, "bbox": bb, "auto_id": c.get("auto_id", "")}
                if bb and len(bb) == 4:
                    cx, cy = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
                    best, best_d = None, 1e9
                    for e in ocr_els:
                        eb = e.get("bbox") or []
                        if len(eb) != 4:
                            continue
                        ex, ey = (eb[0] + eb[2]) / 2, (eb[1] + eb[3]) / 2
                        d = ((cx - ex) ** 2 + (cy - ey) ** 2) ** 0.5
                        if d < best_d:
                            best, best_d = e, d
                    if best is not None and best_d <= float(req.fuse_radius_px):
                        item["ocr_text"] = (best.get("text") or "")[:120]
                        item["sources"] = ["uia", "ocr"]
                        # UIA 丢字符时，OCR 才是可信的
                        if item["ocr_text"] and name and item["ocr_text"] not in name and name not in item["ocr_text"]:
                            item["conflict"] = True
                fused.append(item)
            out["fused"] = fused

            # ⑤ 建议：什么时候必须补截图
            advice = {"screenshot_recommended": False, "reason": ""}
            named = (out.get("uia") or {}).get("named_count") or 0
            total = (out.get("uia") or {}).get("control_count") or 0
            if total and named / max(1, total) < 0.4:
                advice = {"screenshot_recommended": True,
                          "reason": f"UIA 覆盖率低（有名 {named}/{total}），界面多为自绘控件"}
            elif any(c.get("conflict") for c in fused):
                advice = {"screenshot_recommended": True,
                          "reason": "OCR 与 UIA 文本冲突（UIA 可能丢字符），需看图裁决"}
            elif not total:
                advice = {"screenshot_recommended": True,
                          "reason": "读不到 UIA 控件树，只能靠视觉"}
            out["advice"] = advice

        if req.include_shot:
            try:
                shot = desktop_tools.desktop_capture_desktop(analyze=False) or {}
                out["screenshot"] = shot.get("image_path") or shot.get("path")
            except Exception as exc:  # noqa: BLE001
                out["screenshot_error"] = f"{type(exc).__name__}: {exc}"[:120]

        out["duration_ms"] = int((time.time() - started) * 1000)
        return out


    @router.post("/windows/{window_id}/wait-stable")
    def window_wait_stable(window_id: str, req: WaitStableRequest):
        return invoke(desktop_tools.desktop_wait_stable, window_id, req.quiet_ms, req.timeout_ms,
                      req.threshold, req.max_changed_cells, req.interval_ms)

    @router.post("/windows/{window_id}/uia-state")
    def window_uia_state(window_id: str, req: UiaStateRequest):
        return invoke(desktop_tools.desktop_uia_state, window_id, req.name)

    def llm_choose(goal: str, candidates: list[dict]) -> dict | None:
        """Escalation: let the model arbitrate a short shortlist, not the whole table.

        The local layer only gets here when its own score is below the gate, so the
        model sees ~8 rows instead of 100+, and it is explicitly allowed to answer
        "none" — the common failure is a goal that matches nothing at all.
        """
        key = (os.getenv("DEEPSEEK_API_KEY") or "").strip()
        if not key:
            return {"id": None, "error": "no DEEPSEEK_API_KEY in the kernel environment"}
        if not candidates:
            return {"id": None, "error": "empty shortlist"}
        shortlist = "\n".join(f"{row['id']}\t{row['text']}" for row in candidates[:8])
        prompt = ("下面是一个界面里最可能的几个元素（编号 / 文字）。按目标挑一个；"
                  "如果都不匹配，只回答 none。\n\n"
                  f"{shortlist}\n\n目标：{goal}\n\n只回答编号或 none。")
        try:
            response = http_client.post(
                "https://api.deepseek.com/chat/completions",
                json={"model": "deepseek-flash", "max_tokens": 2000, "temperature": 0,
                      "messages": [{"role": "user", "content": prompt}]},
                headers={"Authorization": f"Bearer {key}"}, timeout=60)
            data = response.json()
            answer = str(data["choices"][0]["message"].get("content") or "").strip()
            tokens = int(data.get("usage", {}).get("total_tokens", 0))
        except Exception as exc:  # noqa: BLE001 - escalation is best effort, but say why
            return {"id": None, "error": f"{exc.__class__.__name__}: {exc}"[:200]}
        if not answer or "none" in answer.lower():
            return {"id": None, "tokens": tokens, "answer": "none"}
        match = re.search(r"\d+", answer)
        if not match:
            return {"id": None, "tokens": tokens, "answer": answer[:40]}
        chosen = int(match.group())
        if chosen not in {row["id"] for row in candidates[:8]}:
            return {"id": None, "tokens": tokens, "answer": f"unknown id {chosen}"}
        return {"id": chosen, "tokens": tokens, "answer": "picked"}

    @router.post("/windows/{window_id}/elements")
    def window_elements(window_id: str, req: ElementTableRequest):
        """The element table itself: lets a caller align ids with /decide's cache."""
        return invoke(desktop_tools.desktop_element_table, window_id, req.source, req.fresh, req.caption)

    @router.post("/rules")
    def rule_lookup(req: RuleQuery):
        """UI operating conventions: control type -> action -> how to verify it."""
        from .ui_rules import lookup, rule_entries

        query = req.q.strip()
        rules = lookup(query) if query else rule_entries()[:8]
        return {"success": True, "query": query, "count": len(rules), "rules": rules}

    @router.post("/windows/{window_id}/wait-window")
    def window_wait(window_id: str, req: WaitWindowRequest):
        """Wait for a window spawned by the last action on this window.

        The action itself only polls briefly; an app that opens its result in a new
        process (3-5s later) is caught here instead of being mistaken for a no-op.
        """
        return invoke(desktop_tools.desktop_wait_window, window_id, req.timeout_ms)

    @router.post("/windows/{window_id}/decide")
    def window_decide(window_id: str, req: DecideRequest):
        """Local decision layer: turn a goal into one element, without an LLM.

        escalate=true hands a below-threshold goal to the model with a shortlist —
        that is the hybrid path: local first, model only when local is unsure.
        """
        from .decide import decide

        if req.capture_id and req.capture_id != captures.get(window_id):
            return {"success": False, "error": "capture_id 与最近一次 analyze 不一致，请重新 analyze"}
        table = invoke(desktop_tools.desktop_element_table, window_id, req.source, req.fresh, req.caption)
        if not table.get("success"):
            return table
        caps = invoke(desktop_tools.desktop_window_caps, window_id)
        decision = decide(req.goal, table.get("elements", []), top_k=req.top_k,
                         caps=caps if caps.get("success") else None)
        payload = decision.as_dict()
        best = payload["candidates"][0] if payload["candidates"] else None

        escalated = None
        # Escalate when local is unsure: no pick at all, or a pick that barely beat
        # the runner-up (that is where the wrong answers were measured).
        wants_escalation = payload["id"] is None or payload.get("ambiguous")
        if wants_escalation and req.escalate:
            escalated = llm_choose(req.goal, payload["candidates"])
            if escalated and escalated.get("id") is not None:
                best = next((row for row in payload["candidates"]
                             if row["id"] == escalated["id"]), best)
                payload["id"] = escalated["id"]

        return {"success": payload["id"] is not None, "goal": req.goal,
                "source": "llm" if (escalated and escalated.get("id") is not None) else table.get("source"),
                "table_source": table.get("source"), "table_size": table.get("count"),
                "table_cached": table.get("cached", False),
                "id": payload["id"], "confidence": payload["confidence"],
                "score": payload.get("score"), "gap": payload.get("gap"),
                "ambiguous": payload.get("ambiguous", False),
                "reason": payload["reason"],
                "caps": {k: (caps or {}).get(k) for k in ("can_maximize", "can_minimize")},
                "escalated": bool(escalated),
                "escalation": escalated,
                "picked": {"id": best["id"], "text": best["text"], "type": best["type"],
                           "bbox": next((e["bbox"] for e in table["elements"]
                                         if e["id"] == best["id"]), None)} if best else None,
                "candidates": payload["candidates"]}

    @router.post("/windows/{window_id}/highlight")
    def window_highlight(window_id: str, req: TopmostRequest):
        return invoke(desktop_tools.desktop_highlight, window_id, req.on)

    @router.post("/windows/{window_id}/type")
    def window_type(window_id: str, req: TypeRequest):
        # expect_text 让 act() 在像素不变时用 UIA 回读判定，而不是直接判 noop
        result = act(window_id, desktop_tools.desktop_type_text, window_id, req.text,
                     capture_id=req.capture_id, expect_text=req.text)
        # press_enter 必须真的送达：之前字段不存在，传了被静默丢弃，
        # 于是"输入 URL 后没回车"被误读成"快捷键不生效"（评审 N6）。
        if req.press_enter and result.get("success"):
            time.sleep(0.3)
            act(window_id, desktop_tools.desktop_press_key, window_id, "enter",
                capture_id=None)
            result["press_enter"] = True
        return result

    @router.post("/cursor")
    def cursor(req: CursorRequest):
        return invoke(desktop_tools.desktop_cursor, req.window_id, req.x, req.y, req.visible)

    @router.post("/windows/{window_id}/keys")
    def window_keys(window_id: str, req: KeysRequest):
        return act(window_id, desktop_tools.desktop_type_keys, window_id, req.text, req.interval,
                   capture_id=req.capture_id, expect_text=req.text)

    @router.post("/windows/{window_id}/key")
    def window_key(window_id: str, req: KeyRequest):
        return act(window_id, desktop_tools.desktop_press_key, window_id, req.key, req.count,
                   capture_id=req.capture_id)

    @router.post("/windows/{window_id}/scroll")
    def window_scroll(window_id: str, req: ScrollRequest):
        return act(window_id, desktop_tools.desktop_scroll, window_id, req.x, req.y, req.clicks, req.direction,
                   capture_id=req.capture_id)

    @router.post("/windows/{window_id}/swipe")
    def window_swipe(window_id: str, req: SwipeRequest):
        return act(window_id, desktop_tools.desktop_swipe, window_id, req.x1, req.y1, req.x2, req.y2, req.speed,
                   capture_id=req.capture_id)

    # ── app discovery / launch ────────────────────────────────────────────
    # Launching through the kernel matters: a bare CreateProcess on an Electron
    # app can exit immediately, while the kernel resolves shortcuts the same way
    # the desktop does.
    @router.post("/apps/find")
    def find_app(req: AppFindRequest):
        return invoke(desktop_tools.desktop_find_app, req.name)

    @router.post("/apps/launch")
    def launch_app(req: AppLaunchRequest, owner: str = Depends(authorize)):
        if req.scoped:
            return invoke(desktop_tools.desktop_launch_app, req.shortcut_path, scoped=True, scope_owner=owner)
        return invoke(desktop_tools.desktop_launch_app, req.shortcut_path, scope_owner=owner)

    # ── screen-level drag, clipboard, cleanup, diagnostics, files ──────────
    @router.post("/drag")
    def drag(req: DragRequest):
        return invoke(desktop_tools.desktop_drag_screen, req.x1, req.y1, req.x2, req.y2, req.speed)

    @router.post("/clipboard/text")
    def set_clipboard(req: ClipboardTextRequest):
        return invoke(desktop_tools.desktop_set_clipboard_text, req.text)

    @router.get("/clipboard/text")
    def get_clipboard():
        return invoke(desktop_tools.desktop_get_clipboard_text)

    @router.post("/windows/{window_id}/paste-file")
    def paste_file(window_id: str, req: PasteFileRequest):
        return invoke(desktop_tools.desktop_paste_clipboard_file, window_id, path=req.path)

    @router.post("/windows/{window_id}/click-image-point")
    def click_image_point(window_id: str, req: ImagePointRequest):
        return act(window_id, desktop_tools.desktop_click_image_point, window_id, req.image_x, req.image_y,
                   req.image_width, req.image_height, req.clicks, capture_id=req.capture_id)

    @router.post("/windows/{window_id}/topmost")
    def window_topmost(window_id: str, req: TopmostRequest):
        return invoke(desktop_tools.desktop_topmost, window_id, req.on)

    @router.post("/cleanup")
    def cleanup(req: CleanupRequest, owner: str = Depends(authorize)):
        """End the caller's desktop scope and verify the visible-window delta."""
        with lock:
            scope = lifecycle_scopes.get(owner)
        if scope is None:
            lifecycle_begin(owner)
            with lock:
                scope = lifecycle_scopes.get(owner)
        from . import web_tools
        browser_before = scope.get("browser", {"running": False, "page_keys": []})
        try:
            browser_result = _WEB_EXECUTOR.submit(
                web_tools.web_lifecycle_cleanup, browser_before,
                keep_browser=req.keep_browser or not req.close_browser,
            ).result(timeout=45)
        except Exception as exc:  # noqa: BLE001
            browser_result = {"status": "failed", "closed_pages": 0,
                              "detail": f"{type(exc).__name__}: {exc}"}

        keep_keys = {str(key) for key in req.keep_window_keys if str(key)}
        before_cleanup = desktop_tools.desktop_list_windows().get("windows", [])
        # Snapshot ownership BEFORE cleanup clears the launch/binding records,
        # otherwise the residue check would have no evidence left to use.
        ownership = desktop_tools.ownership_snapshot()
        if req.keep_browser:
            keep_keys.update(
                str(item.get("window_key")) for item in before_cleanup
                if str(item.get("exe", "")).lower() in {"msedge.exe", "chrome.exe", "chromium.exe"}
                and item.get("window_key")
            )
        result = invoke(
            desktop_tools.cleanup_session_footprint,
            close_browser=False,
            close_launched_apps=req.close_launched_apps,
            unbind_windows=True,
            baseline_windows=scope.get("windows", []),
            keep_window_keys=sorted(keep_keys),
            scope_owner=owner,
            scope_started_at=float(scope.get("started_at", 0) or 0),
        )
        after = desktop_tools.desktop_list_windows().get("windows", [])
        baseline_keys = {
            str(item.get("window_key")) for item in scope.get("windows", [])
            if item.get("window_key")
        }
        after_by_key = {str(item.get("window_key")): item for item in after if item.get("window_key")}
        after_keys = set(after_by_key)
        kept_after = [after_by_key[key] for key in sorted(keep_keys & after_keys)]
        appeared = [after_by_key[key] for key in sorted(after_keys - baseline_keys - keep_keys)]
        # "Appeared after the baseline" is not ownership: the user may open a
        # window while the task runs. Only windows we can prove we created count
        # as residue; the rest are reported separately and do not fail cleanup.
        residue = desktop_tools.task_residue_windows(appeared, ownership)
        residue_keys = {str(item.get("window_key")) for item in residue}
        third_party = [item for item in appeared if str(item.get("window_key")) not in residue_keys]
        if not isinstance(result, dict):
            result = {"success": False, "error": "cleanup returned an invalid result"}
        result.update({
            "browser": browser_result,
            "baseline_window_count": len(scope.get("windows", [])),
            "windows_before_cleanup": len(before_cleanup),
            "windows_after_cleanup": len(after),
            "closed_task_windows": result.get("closed_windows", []),
            "kept_task_windows": kept_after,
            "remaining_new_windows": residue,
            "third_party_windows": third_party,
            "cleanup_complete": True,
        })
        browser_ok = browser_result.get("status") != "failed"
        result["cleanup_success"] = bool(result.get("success", False)) and browser_ok and not residue
        result["success"] = result["cleanup_success"]
        result["hint"] = (
            "任务窗口已核验；保留项按请求留下。不要重复清理，直接报告结果。"
            if result["cleanup_success"] else
            "清理流程已结束，但本任务创建的窗口仍有残留或浏览器清理失败。"
            "不要重试；如实报告 remaining_new_windows / browser。")
        with lock:
            lifecycle_scopes.pop(owner, None)
        return result

    def lifecycle_begin(owner: str) -> dict:
        with lock:
            existing = lifecycle_scopes.get(owner)
            if existing is not None:
                return {"success": True, "already_started": True,
                        "baseline_window_count": len(existing.get("windows", []))}
        from . import web_tools
        windows = desktop_tools.desktop_list_windows().get("windows", [])
        try:
            browser = _WEB_EXECUTOR.submit(web_tools.web_lifecycle_snapshot).result(timeout=20)
        except Exception:
            browser = {"running": False, "page_keys": []}
        scope = {"windows": windows, "browser": browser, "started_at": time.time()}
        with lock:
            # Preserve the first observation if concurrent tools race at task start.
            scope = lifecycle_scopes.setdefault(owner, scope)
        return {"success": True, "already_started": False,
                "baseline_window_count": len(scope.get("windows", []))}

    @router.post("/lifecycle/begin")
    def begin_lifecycle(owner: str = Depends(authorize)):
        return lifecycle_begin(owner)

    @router.get("/parser/status")
    def parser_status():
        return invoke(desktop_tools.desktop_parser_status)

    @router.post("/files/create")
    def file_create(req: FileCreateRequest):
        return invoke(desktop_tools.desktop_create_file, req.name, req.content)

    @router.post("/files/write")
    def file_write(req: FileWriteRequest):
        return invoke(desktop_tools.desktop_write_file, req.path, req.content, req.append)

    @router.post("/files/delete")
    def file_delete(req: FileDeleteRequest):
        return invoke(desktop_tools.desktop_delete_file, req.path)

    @router.get("/files")
    def file_list():
        return invoke(desktop_tools.desktop_list_my_files)

    # ── Web automation (Playwright lives inside web_tools) ───────────────

    # settle 是只读动作（轮询元素数至稳定），批量里必须能用它替代"猜时间"的 wait：
    # 固定 sleep 2.5s 在不同机器/页面上都不稳，settle 是确定性等待（标准 B1）。
    BATCH_ACTIONS = {"click", "type", "key", "hover", "check", "scroll", "wait",
                     "select", "focus", "dom", "settle"}

    def _batch_error(code: str, hint: str, index: int | None = None) -> dict:
        payload = {"success": False, "action": "batch",
                   "error": {"code": code, "hint": hint}, "error_message": hint}
        if index is not None:
            payload["stopped_at"] = index
        return payload

    def _run_batch(req: WebActionRequest):
        """按顺序执行 steps，返回每步结果 + 便宜证据。

        硬约束（DSH 0058 §3）：
          * 白名单动作；不允许 open/close/batch 嵌套；
          * **最多一个写步骤，且必须是最后一步** —— 否则前面步骤静默偏离时，
            batch 会在调用方看到中间状态之前把错误数据提交；
          * batch 级与 step 级都置 allow_mutating 才放行（防夹带）；
          * 写步骤必须持租约（owner 与 acquire 时一致）。
        """
        from . import web_tools as web

        steps = req.steps or []
        if not steps:
            return _batch_error("BATCH_EMPTY", "steps 不能为空")
        for index, step in enumerate(steps):
            action = str(step.get("action") or "")
            if action not in BATCH_ACTIONS:
                return _batch_error(
                    "BATCH_ACTION_NOT_ALLOWED",
                    f"第 {index} 步 action={action!r} 不在白名单 {sorted(BATCH_ACTIONS)}", index)

        # 声明 commit="local" 的步骤不占"唯一写步骤"名额（它只改本地表单状态），
        # 但仍需 allow_mutating + verify + 审计，且内核会校验声明的真实性。
        writes = [index for index, step in enumerate(steps)
                  if step.get("allow_mutating") and (step.get("commit") or "") != "local"]
        if len(writes) > 1:
            return _batch_error("BATCH_MULTIPLE_WRITES",
                                f"一批最多一个写步骤，实际 {len(writes)} 个（index={writes}）")
        if writes and writes[0] != len(steps) - 1:
            return _batch_error("BATCH_WRITE_NOT_LAST",
                                f"写步骤必须在最后一步，实际在第 {writes[0]} 步", writes[0])
        if writes and not req.allow_mutating:
            return _batch_error("BATCH_MUTATING_FLAG_MISSING",
                                "batch 级也要 allow_mutating=True（防夹带）", writes[0])
        if writes and not req.owner.strip():
            return _batch_error("LEASE_REQUIRED",
                                "写步骤必须带 owner 并先 acquire 租约", writes[0])

        batch_id = f"b-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
        before = web.web_evidence() if req.evidence != "none" else {}
        status_before = web.web_status() if req.evidence != "none" else {}
        started_all = time.monotonic()
        deadline = started_all + req.budget_ms / 1000
        records: list[dict] = []
        stopped: int | None = None

        for index, step in enumerate(steps):
            if time.monotonic() > deadline:
                records.append({"index": index, "action": step.get("action"), "ok": False,
                                "duration_ms": 0,
                                "error": {"code": "BATCH_BUDGET_EXCEEDED",
                                          "hint": f"超过 budget_ms={req.budget_ms}"}})
                stopped = index
                break
            # wait 的两种形态：带 selector（等到元素出现）/ 带 seconds（纯等待）。
            # 纯等待以前会被当成"wait 但没 selector"直接失败（DSH 0060 Bug 1）。
            if step["action"] == "wait" and not str(step.get("selector") or "").strip():
                seconds = float(step.get("seconds") or 0)
                started = time.monotonic()
                wanted = max(0.0, min(seconds, 60.0))
                remaining_s = max(0.0, deadline - started)
                slept = min(wanted, remaining_s)
                time.sleep(slept)
                if slept < wanted:
                    # A started step must not outlive the batch budget either:
                    # report the cut instead of silently sleeping past it.
                    records.append({"index": index, "action": "wait", "ok": False,
                                    "duration_ms": int((time.monotonic() - started) * 1000),
                                    "error": {"code": "BATCH_BUDGET_EXCEEDED",
                                              "hint": (f"wait {wanted:.1f}s 被预算截断为 {slept:.1f}s"
                                                       f"（budget_ms={req.budget_ms}）")}})
                    stopped = index
                    break
                records.append({"index": index, "action": "wait", "ok": True,
                                "duration_ms": int((time.monotonic() - started) * 1000)})
                continue

            payload = {key: value for key, value in step.items()
                       if key in WebActionRequest.model_fields}
            payload["action"] = step["action"]
            if step.get("allow_mutating"):
                payload["owner"] = payload.get("owner") or req.owner
            payload.setdefault("verify", step.get("verify", ""))
            # A step must never outlive the batch budget: otherwise the batch
            # reports a timeout while the step is still acting on the page.
            remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
            step_timeout = int(payload.get("timeout_ms") or remaining_ms or 0)
            payload["timeout_ms"] = max(1, min(step_timeout or remaining_ms, remaining_ms))
            started = time.monotonic()
            try:
                result = _web_dispatch(WebActionRequest(**payload))
            except HTTPException as exc:
                result = {"success": False,
                          "error": {"code": "STEP_HTTP_422", "hint": str(exc.detail)}}
            except Exception as exc:  # noqa: BLE001
                result = {"success": False,
                          "error": {"code": "STEP_FAILED",
                                    "hint": f"{exc.__class__.__name__}: {exc}"}}
            if isinstance(result, dict):
                # 与单动作路径共用同一套 error 归一化（DSH 0062：不要两套口径）
                result = _finalise_web_result(result)
            duration = int((time.monotonic() - started) * 1000)
            ok = bool(result.get("success", "error" not in result)) if isinstance(result, dict) else True
            record: dict = {"index": index, "action": step["action"], "ok": ok,
                            "duration_ms": duration}
            if isinstance(result, dict):
                if result.get("mutation"):
                    record["mutation"] = result["mutation"]
                if step["action"] in ("dom", "snapshot", "frames", "status"):
                    record["result"] = result
            if not ok:
                # 失败必须说清楚：哪一步、什么动作、哪个 selector、内核原始错误。
                # 之前把扁平 error 直接塞进来，调用方看到 code=ERROR / hint=""（DSH 0060 Bug 2）。
                raw = result if isinstance(result, dict) else {}
                raw_error = raw.get("error")
                if isinstance(raw_error, dict):
                    code = str(raw_error.get("code") or "STEP_FAILED")
                    hint = str(raw_error.get("hint") or "")
                else:
                    code = str(raw.get("error_code") or "STEP_FAILED")
                    hint = str(raw_error or "")
                selector = str(step.get("selector") or "")
                message = f"step {index} {step['action']}: {hint or '内核未给出原因'}"
                if selector:
                    message += f"（selector={selector!r}）"
                record["error"] = {"code": code, "hint": message, "step": index,
                                   "action": step["action"], "selector": selector}
                record["error"]["raw"] = str(hint or "")[:200]
                # A dispatched-but-unconfirmed action is not a failure we can
                # retry: report it as unknown and stop, because the page may
                # already be mid-transition.
                if code in ("RESULT_UNKNOWN", "WEB_TIMEOUT") or raw.get("click_dispatched"):
                    record["outcome"] = "unknown"
                    record["hint"] = ("动作已派发但结果未知：先 web_snapshot 观察页面实际状态，"
                                      "不要重试这一步")
            records.append(record)
            if record.get("outcome") == "unknown":
                stopped = index
                break
            if not ok and req.stop_on_error:
                stopped = index
                break

        evidence: dict = {}
        if req.evidence != "none" and before.get("success"):
            after = web.web_evidence()
            status_after = web.web_status()

            def _first_url(status: dict) -> str:
                tabs = status.get("tabs") or []
                return str((tabs[0] or {}).get("url") or "") if tabs else ""

            evidence = {
                "url_changed": _first_url(status_before) != _first_url(status_after),
                "dom_delta": {
                    "elements_before": before.get("elements"),
                    "elements_after": after.get("elements"),
                    # 含 input/select 的 value：只改输入框时 innerText 不变
                    "text_hash_changed": before.get("text_hash") != after.get("text_hash"),
                },
                "active_element": after.get("active_element", ""),
            }

        payload: dict = {"success": stopped is None, "batch_id": batch_id, "steps": records,
                         "evidence": evidence, "stopped_at": stopped,
                         "duration_ms": int((time.monotonic() - started_all) * 1000)}
        if any(record.get("outcome") == "unknown" for record in records):
            payload["result_unknown"] = True
            payload["hint"] = ("有步骤已派发但结果未确认：先 web_snapshot 观察，"
                               "不要重跑整批")
        first_error = next((record["error"] for record in records
                            if not record["ok"] and record.get("error")), None)
        if first_error:
            payload["error"] = first_error
            payload["error_message"] = first_error["hint"]
        return payload

    def _web_dispatch(req: WebActionRequest):
        from . import knowledge as kb
        from . import web_tools as web

        action = req.action
        if action == "batch":
            return _run_batch(req)
        # Knowledge-base seam: a semantic key can stand in for selector/url, so
        # callers do not have to remember brittle selectors.
        if req.key.strip():
            if action == "open" and not req.url.strip():
                record = kb.resolve("web_page", req.key, req.scope)
                if record and record.get("value"):
                    req = req.model_copy(update={"url": str(record["value"])})
            elif action in ("click", "type", "select", "hover", "wait") and not req.selector.strip():
                record = kb.resolve("web_selector", req.key, req.scope)
                if record and record.get("value"):
                    req = req.model_copy(update={"selector": str(record["value"])})
        if action == "open":
            if not req.url.strip():
                raise HTTPException(422, "open 需要 url")
            return web.web_open(req.url.strip(), req.timeout_ms)
        if action == "lease":
            return _normalise_error_shape(web.web_lease(req.op, req.owner, req.ttl_s, req.force))
        if action == "settle":
            return web.web_settle(600, req.timeout_ms, req.frame)
        if action == "set_files":
            if not req.selector.strip():
                raise HTTPException(422, "set_files 需要 selector")
            return web.web_set_files(req.selector.strip(), list(req.files or []),
                                     req.timeout_ms, req.frame, req.nth)
        if action == "check":
            if not req.selector.strip():
                raise HTTPException(422, "check 需要 selector")
            return web.web_check(req.selector.strip(), req.nth, req.checked, 10000, req.frame)
        if action == "decide":
            if not req.goal.strip():
                raise HTTPException(422, "decide 需要 goal")
            return web.web_decide_action(req.goal.strip(), req.max_elements)
        if action == "frames":
            return web.web_frames(req.max_elements)
        if action == "dom":
            if not req.selector.strip():
                raise HTTPException(422, "dom 需要 selector")
            return web.web_dom_query(req.selector.strip(), req.max_elements, req.frame)
        if action == "snapshot":
            return web.web_snapshot(req.max_elements, req.include_classes)
        if action == "click":
            if not req.selector.strip():
                raise HTTPException(422, "click 需要 selector")
            # commit 与 target_text 必须一起透传：
            #   commit="local" 要在内核侧按弹窗内容校验（不能只在 batch 调度器里信它）
            #   target_text 是破坏性操作的唯一匹配断言依据
            return web.web_click(req.selector.strip(), 10000, req.frame, req.nth,
                                 req.allow_mutating, req.verify, req.owner,
                                 req.commit, req.target_text)
        if action == "type":
            if not req.selector.strip():
                raise HTTPException(422, "type 需要 selector")
            return web.web_type(req.selector.strip(), req.text, req.press_enter, 10000, req.frame)
        if action == "select":
            if not req.selector.strip() or not req.value:
                raise HTTPException(422, "select 需要 selector 与 value")
            return web.web_select_option(req.selector.strip(), req.value, 10000, req.frame)
        if action == "key":
            if not req.key_name.strip():
                raise HTTPException(422, "key 需要 key_name（如 Enter / Control+a）")
            return web.web_press_key(req.key_name.strip())
        if action == "scroll":
            return web.web_scroll(req.pixels, req.frame)
        if action == "hover":
            if not req.selector.strip():
                raise HTTPException(422, "hover 需要 selector")
            return web.web_hover(req.selector.strip(), req.timeout_ms, req.frame)
        if action == "extract":
            return web.web_extract(req.selector.strip(), req.max_chars, req.frame)
        if action == "wait":
            if not req.selector.strip():
                raise HTTPException(422, "wait 需要 selector")
            return web.web_wait(req.selector.strip(), req.timeout_ms)
        if action == "status":
            return web.web_status()
        if action == "close":
            return web.web_close()
        raise HTTPException(422, f"未知 action: {action}")

    @router.post("/web/read")
    def read_web(req: WebReadRequest):
        from . import web_tools as web
        started = time.monotonic()
        try:
            result = _WEB_EXECUTOR.submit(web.web_read, req.max_elements, req.include_text,
                                          req.max_text_chars).result(timeout=15)
        except Exception as exc:
            result = {"success": False, "error": f"WEB_READ_FAILED: {type(exc).__name__}: {exc}",
                      "hint": "确认浏览器已运行；本入口不会启动浏览器。"}
        result["duration_ms"] = round((time.monotonic() - started) * 1000)
        return result

    @router.post("/web")
    def web_action(req: WebActionRequest):
        """统一出口：把失败结果规范成 error: {code, hint}。

        之前两种形状并存（闸门/租约返回扁平 error_code，普通失败返回字符串 error），
        调用方要写两套判断（验收脚本就在这上面崩过）。现在一律：
          error = {"code": ..., "hint": ...}，同时保留 error_message 字符串给人和日志。
        """
        return _finalise_web_result(_web_action_impl(req))
    def _web_action_impl(req: WebActionRequest):
        action_id = f"wa-{uuid.uuid4().hex[:12]}"
        # Wait longer than the batch budget so the kernel decides when to stop,
        # not the HTTP client. Otherwise the client gives up first and the worker
        # keeps executing with the original budget while nobody is listening.
        wait_s = req.timeout_ms / 1000 + 30
        if req.action == "batch":
            wait_s = max(wait_s, req.budget_ms / 1000 + 15)
        _inflight_start(action_id, req.action)

        def _run():
            try:
                payload = _web_dispatch(req)
            except Exception as exc:  # noqa: BLE001 - recorded, then re-raised
                _inflight_finish(action_id, "failed", {"error": f"{type(exc).__name__}: {exc}"})
                raise
            ok = (bool(payload.get("success", "error" not in payload))
                  if isinstance(payload, dict) else True)
            _inflight_finish(action_id, "done" if ok else "failed",
                             {"success": ok,
                              "error": (payload or {}).get("error") if isinstance(payload, dict) else None})
            return payload

        future = _WEB_EXECUTOR.submit(_run)
        try:
            result = future.result(timeout=wait_s)
        except HTTPException:
            raise
        except TimeoutError:
            # The job is still queued or running on the single worker. We cannot
            # cancel it, so we must not claim failure or rollback.
            return {"success": False, "action": req.action, "action_id": action_id,
                    "result_unknown": True, "still_running": True,
                    "error": {"code": "WEB_STILL_RUNNING",
                              "hint": (f"等待 {wait_s:.0f}s 仍未返回，动作可能仍在执行。"
                                       f"用 GET /web/actions/{action_id} 查询；"
                                       "不要重复提交同一个写操作。")}}
        except Exception as exc:  # noqa: BLE001
            _inflight_finish(action_id, "failed", {"error": str(exc)[:200]})
            return {"success": False, "action": req.action, "action_id": action_id,
                    "error": f"WEB_FAILED: {exc.__class__.__name__}: {exc}"}
        payload = result if isinstance(result, dict) else {"result": result}
        success = bool(payload.get("success", "error" not in payload))
        try:
            ledger.record("chromium", f"web_{req.action}", "confirmed" if success else "error",
                          title="controlled browser", detail=str(payload.get("error") or "")[:120])
        except Exception:  # noqa: BLE001
            pass
        return {"success": success,
                "action": req.action, "tool": f"web_{req.action}",
                "action_id": action_id, **payload}

    @router.get("/web/actions/{action_id}")
    def web_action_status(action_id: str):
        """Ask whether one web action is still running, finished or unknown."""
        entry = _inflight_get(action_id)
        if entry is None:
            return {"success": False,
                    "error": {"code": "ACTION_NOT_FOUND",
                              "hint": "未知或已过期的 action_id（只保留最近 50 条）"}}
        if entry.get("state") == "running":
            entry["result"] = None
            entry["hint"] = "仍在执行：不要重复提交同一个写操作，先观察页面状态。"
        return {"success": True, **entry}

    # ── Knowledge base (search / structured index) ───────────────────────

    @router.post("/kb/search")
    def kb_search(req: KbSearchRequest):
        from . import knowledge as kb

        if req.source == "all":
            sources = kb.VALID_SOURCES
        elif req.source in kb.VALID_SOURCES:
            sources = (req.source,)
        else:
            raise HTTPException(422, f"source 仅支持 all/{'/'.join(kb.VALID_SOURCES)}")
        try:
            hits = kb.search(req.query, sources=sources, top_k=req.top_k)
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"KB_SEARCH_FAILED: {exc.__class__.__name__}: {exc}"}
        return {"success": True, "query": req.query, "hits": hits}

    @router.post("/kb/remember")
    def kb_remember(req: KbRememberRequest):
        from . import knowledge as kb

        try:
            path = kb.remember(req.kind, req.key, req.value, req.scope, req.meta)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"KB_WRITE_FAILED: {exc.__class__.__name__}: {exc}"}
        return {"success": True, "path": path, "kind": req.kind, "key": req.key, "scope": req.scope}

    @router.post("/kb/resolve")
    def kb_resolve(req: KbResolveRequest):
        from . import knowledge as kb

        record = kb.resolve(req.kind, req.key, req.scope)
        if record is None:
            return {"success": True, "found": False, "kind": req.kind, "key": req.key, "scope": req.scope}
        return {"success": True, "found": True, **record}

    @router.post("/kb/list")
    def kb_list(req: KbListRequest):
        from . import knowledge as kb

        return {"success": True, "entries": kb.list_index(req.kind, req.limit)}

    @router.post("/kb/read")
    def kb_read(req: KbReadRequest):
        from . import knowledge as kb

        try:
            text = kb.read_file(req.source, req.name)
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"{exc.__class__.__name__}: {exc}"}
        return {"success": True, "source": req.source, "name": req.name, "text": text}

    return router


def _pixel_sig(window_id: str):
    """窗口的像素签名（便宜、可靠）。用于判断"点完到底变没变"。"""
    try:
        probe = desktop_tools.desktop_effect_probe(window_id)
        if isinstance(probe, dict) and probe.get("success"):
            sig = probe.get("signature")
            return sig if isinstance(sig, list) else None
    except Exception:
        pass
    return None


def _sig_differs(a, b) -> bool:
    if not a or not b or len(a) != len(b):
        return a != b
    try:
        return sum(1 for x, y in zip(a, b) if abs(float(x) - float(y)) > 2.0) > 0
    except Exception:
        return a != b


def _click_bbox_with_retry(window_id: str, bbox: list, allow_retry: bool = False) -> dict:
    """点击 bbox 中心。**默认只点一次**。

    为什么默认单点（评审 Q3 建议 2）：
      原实现会依次尝试 6 个位置，只要像素有变化就停。但菜单一旦展开，
      第一次点击就已经生效；如果像素探测没看出来，第二次点击会把菜单
      **关掉** —— 于是"点文件菜单"变成"点开又点关"，比不点还糟。
      实测：f_create_save 的 menu_ok 连续失败，耗时从 43s 涨到 114s。

    重试保留为显式选项（allow_retry=True 且只额外试 1 个点），
    默认关闭：让 act() 的 effect 判定 + 失败诊断来决定下一步，
    而不是由点击函数自己猜。
    """
    if not isinstance(bbox, list) or len(bbox) != 4:
        return {"success": False, "error": "bbox 无效", "tries": []}
    x1, y1, x2, y2 = [int(v) for v in bbox]
    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
    before = _pixel_sig(window_id)
    r = desktop_tools.desktop_click(window_id, cx, cy)
    time.sleep(0.55)
    after = _pixel_sig(window_id)
    changed = _sig_differs(before, after)
    tries = [{"x": cx, "y": cy, "effect": r.get("effect"), "changed": changed}]

    if allow_retry and not changed:
        # 只在"确实没变化"时补点一次右边缘（OCR 框可能偏窄）
        rx = x2 - 2
        r2 = desktop_tools.desktop_click(window_id, rx, cy)
        time.sleep(0.55)
        a2 = _pixel_sig(window_id)
        tries.append({"x": rx, "y": cy, "effect": r2.get("effect"),
                      "changed": _sig_differs(before, a2)})
        changed = tries[-1]["changed"]
        if changed:
            return {"success": True, "clicked_at": [rx, cy], "attempt": 2,
                    "pixel_changed": True, "tries": tries}

    # success 必须把"像素有变化"也算成功。
    # 踩过的坑：desktop_tools.desktop_click 的返回里没有 effect 字段（那是 act() 才加的），
    # 于是 bool(None)=False 把"菜单已经展开"判成了失败 —— 任务因此重试 3 次，
    # 每次都把菜单点开又关掉，最终 6/6 全失败（评审 M10 同类：判定器自伤）。
    ok = changed or bool(r.get("success")) or bool(r.get("effect"))
    return {"success": ok, "clicked_at": [cx, cy],
            "attempt": len(tries), "pixel_changed": changed, "tries": tries}


def _is_blocker_label(label: str, words: tuple[str, ...]) -> bool:
    """True only for short labels that ARE a dialog action, not text that contains one.

    Editors publish their document as the control name, so a body line like
    "DSH-CUA-CREATE-OK" used to match the substring "OK" and got reported as a
    blocking dialog. Matching is therefore whole-label (after stripping spacing
    and punctuation) or a short suffix form such as 确定并继续.
    """
    def squash(text: str) -> str:
        return re.sub(r"[\s\W_]+", "", text or "").lower()

    clean = squash(label)
    if not clean:
        return False
    for word in words:
        token = squash(word)
        if not token:
            continue
        if clean == token:
            return True
        if clean.startswith(token) and len(clean) <= len(token) + 4:
            return True
    return False


def _diagnose_obstruction(target_hwnd: int, window_id: str = "") -> dict:
    """动作失败时自动查"是什么挡住了我"。

    用户的原则：给出任务就该完成，不该要求用户事先说明"会有弹窗"。
    所以这里只在失败之后调用，而不是预先列举策略。

    返回三部分判定依据：
      foreground  现在前台是谁、是不是目标窗口
      overlaps    哪些窗口压在目标窗口中心之上（几何判断）
      blockers    目标窗口里"像阻挡器"的控件（对话框按钮/横幅措辞）
    """
    import win32gui
    import win32process

    out: dict = {"foreground": None, "overlaps": [], "blockers": [], "advice": ""}

    try:
        fg = win32gui.GetForegroundWindow()
        _, fg_pid = win32process.GetWindowThreadProcessId(fg)
        try:
            import psutil
            fg_exe = psutil.Process(fg_pid).name()
        except Exception:
            fg_exe = ""
        out["foreground"] = {
            "hwnd": int(fg),
            "title": win32gui.GetWindowText(fg)[:80],
            "exe": fg_exe,
            "is_target": int(fg) == int(target_hwnd),
        }
    except Exception:
        pass

    try:
        tgt = win32gui.GetWindowRect(target_hwnd)
        cx, cy = (tgt[0] + tgt[2]) // 2, (tgt[1] + tgt[3]) // 2
        for w in desktop_tools._list_visible_windows():
            if int(w.get("hwnd")) == int(target_hwnd):
                continue
            r = w.get("rect") or []
            if len(r) != 4:
                continue
            if r[0] <= cx <= r[2] and r[1] <= cy <= r[3]:
                out["overlaps"].append({
                    "hwnd": w.get("hwnd"),
                    "title": (w.get("title") or "")[:60],
                    "exe": w.get("exe"),
                    "minimized": w.get("minimized"),
                })
    except Exception:
        pass

    BLOCKER_WORDS = ("确定", "取消", "关闭", "接受", "拒绝", "稍后", "不更新",
                     "以后再说", "Update", "Available", "更新", "隐私", "同意",
                     "OK", "Cancel", "Close", "Not now")
    # Only interactive labels can block. A document body exposed as a control
    # name must never look like a dialog: Scintilla publishes its text as the
    # element name, so a line containing "...CREATE-OK" matched the substring
    # rule and the whole editor pane was reported as a blocker (2026-10-27).
    BLOCKER_TYPES = {"Button", "Hyperlink", "SplitButton", "MenuItem",
                     "RadioButton", "CheckBox", "TabItem"}
    if window_id:
        try:
            u = desktop_tools.desktop_uia_tree(window_id, max_depth=10, max_nodes=300) or {}
            for c in (u.get("controls") or []):
                nm = (c.get("name") or "").strip()
                if not nm or len(nm) > 40:
                    continue
                if str(c.get("control_type") or "") not in BLOCKER_TYPES:
                    continue
                if not _is_blocker_label(nm, BLOCKER_WORDS):
                    continue
                out["blockers"].append({
                    "control_type": c.get("control_type"),
                    "name": nm[:50],
                    "bbox": c.get("bbox"),
                })
        except Exception:
            pass

    fg_is_target = (out.get("foreground") or {}).get("is_target")
    if not fg_is_target and out.get("foreground"):
        f = out["foreground"]
        out["advice"] = ("前台不是目标窗口（当前是 %r %r），键盘输入会打到它上面。"
                         "先激活目标窗口，或关掉占位的那个。"
                         % (f.get("exe"), (f.get("title") or "")[:30]))
    elif out.get("blockers"):
        names = [b["name"] for b in out["blockers"][:4]]
        out["advice"] = "目标窗口里有阻挡控件 %r，很可能是弹窗/横幅，先处理它再继续。" % (names,)
    elif out.get("overlaps"):
        top = out["overlaps"][0]
        out["advice"] = ("有窗口压在目标上：%r（%s）。先关掉或移开它。"
                         % ((top.get("title") or "")[:40], top.get("exe")))
    else:
        out["advice"] = "没有发现明显遮挡；可能是选择器失效或元素未渲染，建议重新 observe。"
    return out


def _finalise_web_result(result):
    """把所有 /web 失败结果规范成 error: {code, hint}。

    两种历史形状：闸门/租约返回扁平 error_code + 字符串 error；普通失败只返回字符串
    error。调用方（含验收脚本）按 error.code 判断，遇到字符串会直接崩。
    这里统一，并保留 error_message 字符串给人和日志。
    """
    if not isinstance(result, dict) or result.get("success"):
        return result
    raw = result.get("error")
    code = result.get("error_code")
    hint = ""
    if isinstance(raw, dict):
        code = code or raw.get("code")
        hint = raw.get("hint") or ""
    elif isinstance(raw, str):
        hint = raw
    if not hint:
        hint = result.get("hint") or ""
    if not code:
        # 从错误文本里推断一个稳定的 code，便于调用方分支处理
        lowered = hint.lower()
        if "元素未找到" in hint or "不可点击" in hint:
            code = "ELEMENT_NOT_FOUND"
        elif "超出" in hint or "count=" in hint or "可见匹配" in hint:
            code = "AMBIGUOUS_SELECTOR"
        elif "selector" in lowered:
            code = "BAD_SELECTOR"
        elif "不可读" in hint or "导航" in hint:
            code = "PAGE_NOT_READABLE"
        else:
            code = "ERROR"
    # 保留原始附加字段（batch 的 step / action / selector / raw 就靠这里透出）。
    merged = dict(raw) if isinstance(raw, dict) else {}
    merged.update({"code": code, "hint": hint})
    result["error"] = merged
    result["error_message"] = hint
    return result


def _normalise_error_shape(result: dict) -> dict:
    """把扁平 error_code 归一化成设计稿要求的 error: {code, hint}。

    设计稿（docs/web-batch-and-lease-design.md §2.2）里错误形状是嵌套对象，
    而历史代码用的是扁平 error_code + 字符串 error。调用方（含验收脚本）
    按设计稿判断 code，不归一化就会读到 None。
    """
    if not isinstance(result, dict):
        return result
    code = result.get("error_code")
    if not code:
        return result
    raw = result.get("error")
    hint = result.get("hint") or (raw if isinstance(raw, str) else "")
    result["error"] = {"code": code, "hint": hint}
    return result
