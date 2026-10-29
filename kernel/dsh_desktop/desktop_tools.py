"""Desktop tools for DesktopCore — remote OmniParser, multi-window sessions, scoped file ops.

This module is imported by dsh_desktop.eternity for its side effect of registering
the "desktop" toolset. It follows the same security posture as desktop-harness-poc:

- Parser: remote OmniParser2 FastAPI service on the GPU box (no local OCR/YOLO).
- Windows: multiple windows can be bound in one session (multi-window switching),
  reusing DesktopCore's WindowService / CaptureService / InputService.
- Files: create/write/delete only files the agent created in this session,
  under a session workspace. No shell, no process kill, no arbitrary deletes.
- Disabled-by-default tools: run_powershell, close_window, launch(exe=...) stay
  out of this toolset; they are only reachable if explicitly re-enabled in code.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path, PureWindowsPath
from typing import Any

import cv2
import numpy as np
import psutil
import requests
import ctypes
import win32gui
import win32process

# Explicit signature: without argtypes the 64-bit buffer address is truncated and
# the call silently returns 0.
try:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
    _user32.SendMessageTimeoutW.argtypes = [
        ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t,
        ctypes.c_ssize_t, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p,
    ]
except Exception:  # noqa: BLE001 - non-Windows import path
    _user32 = None

from .controller import SOM_IMAGE_PATH_KEY
from .tool_decorator import tool
from tools.registry import registry, tool_result

# Agent automation environment: the cursor may legitimately rest at a screen
# corner between actions; PyAutoGUI's corner fail-safe aborts hotkeys there.
try:
    import pyautogui as _pyautogui
    _pyautogui.FAILSAFE = False
except Exception:
    pass

logger = logging.getLogger(__name__)


def _track_launched_app(pid: int, exe: str, name: str, scope_owner: str = "") -> None:
    try:
        _STATE._launched_apps[int(pid)] = {
            "exe": exe, "name": name, "t": time.time(), "scope_owner": scope_owner,
            "create_time": _process_create_time(int(pid)),
        }
    except Exception:
        pass


def _process_create_time(pid: int) -> float:
    try:
        return float(psutil.Process(int(pid)).create_time())
    except Exception:
        return 0.0


def ownership_snapshot() -> dict[str, Any]:
    """Snapshot who we launched, taken BEFORE cleanup clears the records."""
    launched: dict[int, float] = {}
    for pid, meta in list(getattr(_STATE, "_launched_apps", {}).items()):
        try:
            created = float(meta.get("create_time") or 0.0)
            launched[int(pid)] = created or _process_create_time(int(pid))
        except Exception:
            continue
    bound: set[int] = set()
    with _STATE._lock:
        for value in list(_STATE._windows.values()):
            try:
                pid = int(value.get("pid") or 0)
                if pid:
                    bound.add(pid)
            except Exception:
                continue
    return {"launched_pids": launched, "bound_pids": bound}


def task_residue_windows(windows: list[dict[str, Any]],
                         ownership: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Windows that this task can prove it created.

    "Baseline minus" is not proof of ownership: a window the user opens while the
    task runs is not residue. A window counts only when a launch record ties it
    to a process we started (pid + process create time), or when we bound it
    ourselves. Everything else is reported separately as a third-party window.
    """
    if ownership is None:
        ownership = ownership_snapshot()
    launched_pids: dict[int, float] = dict(ownership.get("launched_pids") or {})
    bound_pids: set[int] = set(ownership.get("bound_pids") or set())
    mine: list[dict[str, Any]] = []
    for win in windows or []:
        try:
            pid = int(win.get("pid") or 0)
            created = float(win.get("process_created") or 0.0)
        except Exception:
            continue
        owned = False
        if pid in bound_pids:
            owned = True
        elif pid in launched_pids:
            expected = launched_pids.get(pid) or 0.0
            owned = not expected or not created or abs(created - expected) <= 2.0
        if owned:
            mine.append(win)
    return mine


def cleanup_session_footprint(
    close_browser: bool = True, close_launched_apps: bool = True,
    unbind_windows: bool = True, close_explorers: bool = False,
    timeout_per_app: float = 4.0,
    baseline_windows: list[dict[str, Any]] | None = None,
    keep_window_keys: list[str] | None = None,
    scope_owner: str = "",
    scope_started_at: float = 0.0,
) -> dict[str, Any]:
    """Close what THIS session opened: Playwright browser (web_*), apps
    started via desktop_launch_app, and window bindings. Never touches windows
    the user opened themselves. Callable by the agent (desktop_cleanup tool) and
    by the session-ends hook (auto mode)."""
    report: dict[str, Any] = {
        "success": True, "closed_browser": False, "closed_apps": [],
        "failed_apps": [], "unbound_windows": 0, "closed_explorers": 0,
        "closed_windows": [], "kept_windows": [], "remaining_new_windows": [],
    }
    keep_keys = {str(key) for key in (keep_window_keys or []) if str(key)}
    baseline_keys = {
        str(item.get("window_key")) for item in (baseline_windows or [])
        if isinstance(item, dict) and item.get("window_key")
    }
    keep_pids = (
        {int(item.get("pid", 0)) for item in _list_visible_windows()
         if str(item.get("window_key") or "") in keep_keys and item.get("pid")}
        if keep_keys else set()
    )
    # 1. Playwright browser (chromium via web_*) — shared singleton; closing
    #    it releases the window the agent opened. Login cookies persist in
    #    the user-data profile, so closing is cheap for next time.
    if close_browser:
        try:
            from . import web_tools
            r = web_tools.web_close()
            note = str(r.get("note", ""))
            report["browser_status"] = (
                "closed" if bool(r.get("success")) and "已关闭" in note else
                "detached" if bool(r.get("success")) and "断开" in note else
                "not_running" if bool(r.get("success")) and "未在运行" in note else
                "unknown"
            )
            report["closed_browser"] = report["browser_status"] == "closed"
        except Exception as exc:
            report["browser_status"] = "failed"
            report["failed_apps"].append(f"受控浏览器: {type(exc).__name__}: {exc}")
    # 2. Apps this session launched
    if close_launched_apps:
        remaining_apps = {}
        for pid, meta in list(_STATE._launched_apps.items()):
            belongs_to_scope = (
                (meta.get("scope_owner") == scope_owner if scope_owner else not meta.get("scope_owner"))
                and float(meta.get("t", 0) or 0) >= float(scope_started_at or 0)
            )
            if not belongs_to_scope:
                remaining_apps[pid] = meta
                continue
            if not psutil.pid_exists(int(pid)):
                continue
            remaining_apps[pid] = meta
            name = meta.get("name") or meta.get("exe") or str(pid)
            if int(pid) in keep_pids:
                report["kept_windows"].append({"pid": int(pid), "name": name})
            else:
                # A process record alone cannot establish window ownership or
                # protect unsaved documents. Never terminate or kill it here.
                report["failed_apps"].append(f"{name}: process-only record cannot be safely closed")
        _STATE._launched_apps = remaining_apps
        # Some Windows launchers forward to an existing process then exit
        # immediately (notepad.exe is a common example). Close only the exact
        # newly observed HWNDs recorded by desktop_launch_app; never sweep every
        # process with the same executable name, which can kill user windows.
        try:
            import win32con
            import win32gui
            remaining = []
            for item in list(getattr(_STATE, "_launched_windows", []) or []):
                hwnd = int(item.get("hwnd", 0))
                belongs_to_scope = (
                    (item.get("scope_owner") == scope_owner if scope_owner else not item.get("scope_owner"))
                    and float(item.get("t", 0) or 0) >= float(scope_started_at or 0)
                )
                if not belongs_to_scope:
                    remaining.append(item)
                    continue
                if str(item.get("window_key") or "") in keep_keys:
                    remaining.append(item)
                    report["kept_windows"].append({"window_key": item.get("window_key"),
                                                   "title": item.get("title", "")})
                    continue
                targets = [(hwnd, str(item.get("window_key") or ""))]
                if not hwnd or not win32gui.IsWindow(hwnd):
                    # The sampled window can disappear while the app's real
                    # window lives on in the same process (Tk/Electron map their
                    # frame after the launcher's first probe). Ownership is still
                    # proven by the process identity recorded at launch, which the
                    # window_key carries as "hwnd:pid:create_time".
                    targets = _windows_of_launch(item)
                    if not targets:
                        continue
                for target_hwnd, target_key in targets:
                    try:
                        # HWNDs can be recycled after a window closes. Never send
                        # WM_CLOSE unless both handle and process identity match.
                        actual_key = _current_window_key(target_hwnd)
                        if not actual_key:
                            remaining.append(item)
                            report["failed_apps"].append(
                                f"{item.get('title') or item.get('exe')}: window identity unavailable")
                            continue
                        if target_key and actual_key != target_key:
                            continue
                        win32gui.PostMessage(target_hwnd, win32con.WM_CLOSE, 0, 0)
                        deadline = time.monotonic() + timeout_per_app
                        while time.monotonic() < deadline and win32gui.IsWindow(target_hwnd):
                            time.sleep(0.1)
                        if win32gui.IsWindow(target_hwnd):
                            remaining.append(item)
                            report["failed_apps"].append(
                                f"{item.get('title') or item.get('exe')}: window did not close")
                        else:
                            report["closed_apps"].append(item.get("title") or item.get("exe"))
                            report["closed_windows"].append({"window_key": actual_key,
                                                             "title": item.get("title", "")})
                    except Exception as exc:  # noqa: BLE001
                        remaining.append(item)
                        report["failed_apps"].append(
                            f"{item.get('title') or item.get('exe')}: {exc}")
            _STATE._launched_windows = remaining
        except Exception as exc:  # noqa: BLE001
            logger.debug("cleanup: close owned windows failed: %s", exc)
    # 3. Window bindings
    if unbind_windows:
        with _STATE._lock:
            owned = [key for key, value in _STATE._windows.items()
                     if (value.get("scope_owner") == scope_owner if scope_owner else not value.get("scope_owner"))]
            for key in owned:
                _STATE._windows.pop(key, None)
            n = len(owned)
        report["unbound_windows"] = n
    # 4. 文件资源管理器窗口（CabinetWClass）
    #    用例里"打开文件夹/选择文件/查看下载目录"会拉起资源管理器，它不是本会话
    #    launch 的进程（不在 _launched_apps 里），失败收尾时必须显式关掉，
    #    否则残留窗口会遮挡后续用例（2026-09-15 用户反馈）。
    if close_explorers:
        try:
            import win32con
            import win32gui
            closed: list[str] = []

            def _cb(hwnd: int, _param: Any) -> None:
                if not win32gui.IsWindowVisible(hwnd):
                    return
                if win32gui.GetClassName(hwnd) != "CabinetWClass":
                    return
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
                closed.append(win32gui.GetWindowText(hwnd)[:60])

            win32gui.EnumWindows(_cb, None)
            report["closed_explorers"] = len(closed)
            if closed:
                logger.info(
                    "[cleanup] 关闭文件资源管理器窗口 %d 个: %s",
                    len(closed), closed[:3],
                )
        except Exception as exc:
            logger.debug("cleanup: close explorers failed: %s", exc)
    browser_unverified = close_browser and report.get("browser_status") in {"detached", "unknown", "failed"}
    report["success"] = not report["failed_apps"] and not browser_unverified
    report["cleanup_complete"] = True
    report["cleanup_success"] = report["success"]
    report["hint"] = (
        "清理已执行完毕。不要再次调用清理，也不要为核查清理而继续枚举或关闭窗口；直接报告任务结果。"
        if report["success"] else
        "清理已执行一次但有窗口未能关闭。不要反复重试；报告失败项并结束任务。"
    )
    return report

DESKTOP_TOOLSET = "desktop"
MAX_INLINE_B64_CHARS = 8_000_000  # ~6 MB image
DEFAULT_PARSER_TIMEOUT = 30
_MAX_SESSION_FILES = 200
_MAX_DELETE_OPS = 100


# ── Settings resolution (env → external_agent settings) ──────────────────────────

def _config_parser_settings() -> tuple[str, str]:
    """Parser settings from the workbench UI (Config.parser, persisted in config.yaml)."""
    controller = AppControllerRef.current
    if controller is None:
        return "", ""
    try:
        pc = controller.config.parser
        return (pc.url or "").strip().rstrip("/"), (pc.token or "").strip()
    except AttributeError:
        return "", ""


def _resolve_parser_settings() -> tuple[str, str]:
    """Resolve PARSER_SERVICE_URL / PARSER_SERVICE_TOKEN.

    Priority: UI config (settings dialog → config.yaml) → env vars →
    external_agent/settings.py if importable. Empty values otherwise (tools report a
    config error instead of guessing).
    """
    url, token = _config_parser_settings()
    if url and token:
        return url, token
    url = url or (os.getenv("PARSER_SERVICE_URL") or "").strip().rstrip("/")
    token = token or (os.getenv("PARSER_SERVICE_TOKEN") or "").strip()
    if url and token:
        return url, token
    try:  # external_agent is an external project; settings may not be importable here
        from external_agent import settings as desktop_settings  # type: ignore
    except Exception:
        desktop_settings = None
    if desktop_settings is not None:
        url = url or str(getattr(desktop_settings, "PARSER_SERVICE_URL", "") or "").strip().rstrip("/")
        token = token or str(getattr(desktop_settings, "PARSER_SERVICE_TOKEN", "") or "").strip()
    return url, token


def parser_configured() -> bool:
    url, token = _resolve_parser_settings()
    return bool(url and token)


def _parser_headers(token: str) -> dict[str, str]:
    # parser_service.py validates the X-Auth-Token header (verified against source).
    return {"X-Auth-Token": token, "Accept": "application/json"}


def _encode_frame_jpeg(frame: np.ndarray, max_dim: int = 1366) -> tuple[bytes, int, int]:
    """Encode to JPEG (downscaled to max_dim). Returns (jpeg_bytes, w, h) of
    the ENCODED image — pixel coordinates in the service response refer to
    the image we actually uploaded, not the original frame (review #5)."""
    h, w = frame.shape[:2]
    if max(h, w) > max_dim:
        scale = max_dim / max(h, w)
        frame = cv2.resize(frame, (int(w * scale), int(h * scale)))
        h, w = frame.shape[:2]
    ok, buf = cv2.imencode(".jpeg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    if not ok:
        raise RuntimeError("jpeg encode failed")
    return buf.tobytes(), w, h


def _parse_remote_response(payload: Any, source_w: int, source_h: int) -> list[dict[str, Any]]:
    """Normalize parser JSON into DesktopCore-style ui_elements (normalized 0-1000 bbox).

    Accepts either a bare list of elements or {"elements": [...]} / {"parsed": [...]}.

    Coordinate-format detection is deterministic (key/flag first, value-range
    only where the contract is genuinely fractional — review #5 asked to stop
    guessing by magnitude):
      1. explicit "normalized": true  → 0-1 floats (×1000);
         "normalized": false          → pixels of the uploaded image;
      2. bbox_norm / norm_bbox keys    → normalized (≤1.0 → 0-1; ≤1000 →
                                          already 0-1000 scale);
      3. bbox / box keys               → ≤1.0 → 0-1 (our parser_service
                                          contract returns exactly this);
                                          otherwise PIXELS relative to the
                                          uploaded image (source_w/h must be
                                          the ENCODED dimensions).
    """
    items: Any = payload
    if isinstance(payload, dict):
        items = next(
            (payload[key] for key in ("elements", "parsed", "data")
             if isinstance(payload.get(key), list)),
            None,
        )
    if not isinstance(items, list):
        raise ValueError("parser response has no element list")
    elements: list[dict[str, Any]] = []
    for item in items[:150]:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or item.get("content") or "").strip()
        caption = str(item.get("caption") or item.get("semantic_caption") or "").strip()
        bbox = (item.get("bbox") or item.get("box")
                or item.get("bbox_norm") or item.get("norm_bbox"))
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            continue
        try:
            x1, y1, x2, y2 = (float(v) for v in bbox)
        except (TypeError, ValueError):
            continue
        max_val = max(abs(x1), abs(y1), abs(x2), abs(y2))
        norm_flag = item.get("normalized")
        has_norm_key = item.get("bbox_norm") is not None or item.get("norm_bbox") is not None

        def _to_1000(a: float, b: float, c: float, d: float) -> tuple[float, float, float, float]:
            """Interpret (a,b,c,d) per the declared format and map to 0-1000.

            The explicit `normalized` flag decides FIRST (review round-3:
            bbox + bbox_norm + normalized=false must honor the flag, not the
            presence of the alias key). Only where no flag exists do the key
            names / value ranges decide.
            """
            if norm_flag is False:
                # explicitly pixels (of the uploaded image) — any key name
                return (a / max(source_w, 1) * 1000, b / max(source_h, 1) * 1000,
                        c / max(source_w, 1) * 1000, d / max(source_h, 1) * 1000)
            if norm_flag is True or has_norm_key:
                # sender declared normalization: ≤1.0 → 0-1 scale; ≤1000 →
                # already 0-1000; anything larger is sender garbage → pixels.
                if max_val <= 1.0:
                    return a * 1000, b * 1000, c * 1000, d * 1000
                if max_val <= 1000:
                    return a, b, c, d
                return (a / max(source_w, 1) * 1000, b / max(source_h, 1) * 1000,
                        c / max(source_w, 1) * 1000, d / max(source_h, 1) * 1000)
            # plain bbox/box keys, no flag: our parser_service contract
            # returns 0-1 floats; anything >1 is pixels of the upload.
            if max_val <= 1.0:
                return a * 1000, b * 1000, c * 1000, d * 1000
            return (a / max(source_w, 1) * 1000, b / max(source_h, 1) * 1000,
                    c / max(source_w, 1) * 1000, d / max(source_h, 1) * 1000)

        nx1, ny1, nx2, ny2 = _to_1000(x1, y1, x2, y2)
        nx1, nx2 = sorted((max(0, min(1000, nx1)), max(0, min(1000, nx2))))
        ny1, ny2 = sorted((max(0, min(1000, ny1)), max(0, min(1000, ny2))))
        if nx2 - nx1 < 2 or ny2 - ny1 < 2:
            continue
        normalized = {"text": text[:200], "bbox": [round(nx1), round(ny1), round(nx2), round(ny2)]}
        element_type = str(item.get("type") or "").strip().lower()
        if element_type:
            normalized["type"] = element_type
        source = str(item.get("source") or "").strip()
        if source:
            normalized["source"] = source[:80]
        if caption:
            normalized["caption"] = caption[:200]
        elements.append(normalized)
    return elements


def _standard_window_chrome_caption(element: dict[str, Any]) -> str | None:
    """Use high-confidence Windows title-bar glyph+position hints over VL guesses."""
    if str(element.get("type") or "").lower() != "icon":
        return None
    bbox = element.get("bbox")
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return None
    try:
        x1, y1, x2, y2 = (float(v) for v in bbox)
    except (TypeError, ValueError):
        return None
    if x1 < 850 or y2 > 65:
        return None
    label = str(element.get("text") or "").strip().lower()
    if "close" in label or "关闭" in label or label in {"x", "×"}:
        return "关闭"
    if "minus" in label or "horizontal line" in label or "最小化" in label:
        return "最小化"
    if label in {"口", "square", "square icon"} or "maximize" in label or "还原" in label:
        return "最大化/还原"
    return None


class ParserClient:
    """Minimal client for the shared OmniParser2 FastAPI service."""

    def __init__(self) -> None:
        self._session = requests.Session()
        # The parser is an INTRANET service (e.g. 10.x.x.x). Never let it be
        # routed through the machine's system proxy: a locally running proxy
        # client (Clash/v2ray style, WinINET ProxyEnable=1) can't reach
        # internal networks and turns every call into a ProxyError.
        self._session.trust_env = False
        self._session.proxies = {"http": None, "https": None}

    def parse(
        self, frame: np.ndarray, engine: str = "omni", *, caption: bool = True,
    ) -> dict[str, Any]:
        url, token = _resolve_parser_settings()
        if not url or not token:
            return {"error": "PARSER_NOT_CONFIGURED: 需要 PARSER_SERVICE_URL 和 PARSER_SERVICE_TOKEN（env 或 external_agent settings）"}
        jpeg, enc_w, enc_h = _encode_frame_jpeg(frame)
        headers = _parser_headers(token)
        last_error = ""
        for attempt in (1, 2):
            try:
                resp = self._session.post(
                    f"{url}/parse",
                    files={"file": ("frame.jpeg", jpeg, "image/jpeg")},
                    params={"engine": engine, "caption": str(bool(caption)).lower()},
                    headers=headers,
                    timeout=DEFAULT_PARSER_TIMEOUT + 10 * attempt,
                )
                if resp.status_code == 401:
                    return {"error": "PARSER_AUTH_FAILED: 共享 token 无效或已轮换"}
                if resp.status_code != 200:
                    last_error = f"PARSER_HTTP_{resp.status_code}"
                    continue
                # Pixel-format bboxes refer to the ENCODED (possibly resized)
                # upload, so pass enc_w/enc_h — not the original frame dims.
                payload = resp.json()
                elements = _parse_remote_response(payload, enc_w, enc_h)
                result: dict[str, Any] = {
                    "elements": elements, "width": frame.shape[1], "height": frame.shape[0],
                }
                if isinstance(payload, dict):
                    for key in ("engine_used", "fallback", "fallback_error", "aux_model_called"):
                        if key in payload:
                            result[f"parse_{key}"] = payload[key]
                return result
            except (requests.RequestException, ValueError) as exc:
                last_error = f"PARSER_UNAVAILABLE: {exc.__class__.__name__}"
                time.sleep(0.5)
        return {"error": last_error or "PARSER_UNAVAILABLE"}

    def caption_icons(self, frame: np.ndarray, elements: list[dict[str, Any]]) -> dict[str, Any]:
        """Second pass: crop icon-like elements, collage them, get Qwen3-VL captions.

        Service contract (parser_service.py): boxes = JSON string of
        [{"id": int, "bbox": [x1,y1,x2,y2]}, ...] with bbox normalized 0-1
        (server multiplies by image w/h itself). Response:
        {ok, captions: [{id, content, cached}], ...}
        """
        # The service endpoint crops every supplied box. The main parser returns
        # text and overlay boxes alongside icons, so only send explicit icon
        # detections. Older/alternate parser payloads without type metadata keep
        # the historical all-box behavior for compatibility.
        has_types = any(bool(element.get("type")) for element in elements)
        has_sources = any(bool(element.get("source")) for element in elements)
        candidates = [
            (pos, element) for pos, element in enumerate(elements)
            if (not has_types or str(element.get("type") or "").lower() == "icon")
            and (not has_sources or element.get("source") == "box_yolo_content_yolo")
        ]
        if not candidates:
            return {"icons": [], "aux_model_called": False, "n_total": 0, "n_queried": 0}
        url, token = _resolve_parser_settings()
        if not url or not token:
            return {"error": "PARSER_NOT_CONFIGURED"}
        boxes = []
        for pos, element in candidates:
            box: dict[str, Any] = {"id": pos, "bbox": [round(v / 1000, 5) for v in element["bbox"]]}
            hint = str(element.get("text") or element.get("caption") or "").strip()
            if hint:
                box["hint"] = hint[:100]
            boxes.append(box)
        jpeg, _, _ = _encode_frame_jpeg(frame)
        try:
            resp = self._session.post(
                f"{url}/caption_icons",
                files={"file": ("frame.jpeg", jpeg, "image/jpeg")},
                data={"boxes": json.dumps(boxes, ensure_ascii=False)},
                headers=_parser_headers(token),
                timeout=DEFAULT_PARSER_TIMEOUT + 60,
            )
        except requests.RequestException as exc:
            return {"error": f"CAPTION_UNAVAILABLE: {exc.__class__.__name__}"}
        if resp.status_code == 404:
            return {"error": "CAPTION_UNSUPPORTED: 物理机 parser 服务未部署 /caption_icons 端点"}
        if resp.status_code != 200:
            return {"error": f"CAPTION_HTTP_{resp.status_code}: {resp.text[:120]}"}
        try:
            payload = resp.json()
        except ValueError:
            return {"error": "CAPTION_BAD_RESPONSE"}
        captions = payload.get("captions") if isinstance(payload, dict) else None
        if not isinstance(captions, list):
            return {"error": "CAPTION_BAD_RESPONSE"}
        icons: list[dict[str, Any]] = []
        for item in captions:
            if not isinstance(item, dict):
                continue
            try:
                index = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            icon = {"index": index, "caption": str(item.get("content") or "")[:120]}
            source = str(item.get("source") or "").strip()
            if source:
                icon["source"] = source
            icons.append(icon)
        return {
            "icons": icons,
            "aux_model_called": bool(payload.get("aux_model_called")),
            "n_total": payload.get("n_total", len(icons)),
            "n_cached": payload.get("n_cached", 0),
            "n_deterministic": payload.get("n_deterministic", 0),
            "n_queried": payload.get("n_queried", 0),
            "elapsed_ms": payload.get("elapsed_ms"),
        }



# ── Multi-window session state ───────────────────────────────────────────

class _desktopState:
    """Per-process state for the desktop toolset (one agent process)."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._windows: dict[str, dict[str, Any]] = {}  # window_id -> info
        self._parser: ParserClient | None = None
        self._uia_capabilities: dict[tuple[str, str], dict[str, Any]] = {}


    @property
    def parser(self) -> ParserClient:
        if self._parser is None:
            self._parser = ParserClient()
        return self._parser


_STATE = _desktopState()
# session cleanup tracking: apps the agent launched (pid -> meta), closed
# only by cleanup_session_footprint — never user-opened windows.
_STATE._launched_apps: dict[int, dict[str, Any]] = {}
_STATE._launched_windows: list[dict[str, Any]] = []
# last analyze result per window (for analyze→click auto-sedimentation):
# window_id -> {"elements": [{text, bbox}], "t": time, "exe": str, "page": str}
_STATE._last_analyze: dict[str, dict[str, Any]] = {}


def _safe_window_title(hwnd: int) -> str:
    """Window title without risking a hang.

    win32gui.GetWindowText sends WM_GETTEXT synchronously; if the target's UI
    thread is busy while a modal panel loads, that call never
    returns and the whole action hangs. SMTO_ABORTIFHUNG gives up instead.
    """
    if _user32 is None:  # pragma: no cover - only on a broken user32
        return win32gui.GetWindowText(hwnd)
    buffer = ctypes.create_unicode_buffer(512)
    try:
        result = _user32.SendMessageTimeoutW(
            hwnd, 0x000D, 512, ctypes.addressof(buffer),
            0x0002 | 0x0001, 50, None)  # SMTO_ABORTIFHUNG | SMTO_BLOCK, 50ms
    except Exception:  # noqa: BLE001
        return ""
    return buffer.value if result else ""


def _list_visible_windows(*, include_untitled: bool = False) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    def _cb(hwnd: int, _param: Any) -> None:
        # 最小化窗口不能跳过：实测用户常用应用全是最小化状态
        # （Edge/企业微信/终端都是 ico=1），一旦过滤掉，内核就"看不见"它们，
        # 清理和预检都建立在错误清单上。改为照实报告 + minimized 标注。
        if not win32gui.IsWindowVisible(hwnd):
            return
        # 不能因为"有 owner"就跳过：记事本的「是否保留未保存的更改?」、
        # 各种保存/确认对话框都是 owned popup（class=#32770），
        # 一律过滤会让内核"看不见"这类阻塞窗口 —— 而它们恰恰会挡住后续所有操作
        # （实测：这个弹窗让 f_create_save 连续失败，且 /windows 完全看不到它）。
        # 改为照实报告 + is_popup / owner_hwnd 标注，由调用方决定是否处理。
        owner_hwnd = win32gui.GetWindow(hwnd, 4)
        title = _safe_window_title(hwnd)
        if not title and not include_untitled:
            return
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        exe = ""
        process_created = 0.0
        try:
            process = psutil.Process(pid)
            exe = process.name()
            process_created = process.create_time()
        except Exception:
            pass
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        windows.append({
            "hwnd": hwnd,
            "window_key": f"{hwnd}:{pid}:{int(process_created * 1000)}",
            "title": title[:120],
            "exe": exe,
            "pid": pid,
            "process_created": process_created,
            "rect": [left, top, right, bottom],
        })
    win32gui.EnumWindows(_cb, None)
    return windows


def _window_info(hwnd: int) -> dict[str, Any] | None:
    for info in _list_visible_windows():
        if info["hwnd"] == hwnd:
            return info
    return None


def window_snapshot() -> dict[int, dict[str, Any]]:
    """Cheap picture of the top-level windows, keyed by hwnd."""
    return {info["hwnd"]: info for info in _list_visible_windows()}


def window_change(baseline: dict[int, dict[str, Any]]) -> dict[str, Any]:
    current = window_snapshot()
    added = [info for hwnd, info in current.items() if hwnd not in baseline]
    # A visible-window snapshot can miss a still-live HWND when title lookup
    # times out or the window is temporarily hidden. Only destruction is a
    # close; an uncertain IsWindow probe must not become confirmation.
    removed = []
    for hwnd, info in baseline.items():
        if hwnd in current:
            continue
        try:
            if not win32gui.IsWindow(hwnd):
                removed.append(info)
        except Exception:
            logging.warning("Could not verify whether window %s still exists", hwnd)
    return {"added": added, "removed": removed, "current": current}


def wait_window_change(baseline: dict[int, dict[str, Any]], budget_ms: int = 800,
                       poll_ms: int = 120) -> dict[str, Any]:
    """Poll until a window appears/disappears, or the budget runs out.

    A click may spawn a window in a separate process after a delay, so the
    clicked window's pixels do not change
    and a pixel-only verdict calls it a no-op. A new window is stronger evidence
    than any pixel delta, and it also tells the caller what to look at next.
    """
    deadline = time.monotonic() + max(0, budget_ms) / 1000.0
    while True:
        change = window_change(baseline)
        if change["added"] or change["removed"]:
            change["settled_ms"] = int((time.monotonic() - (deadline - budget_ms / 1000.0)) * 1000)
            return change
        if time.monotonic() >= deadline:
            change["settled_ms"] = budget_ms
            return change
        time.sleep(poll_ms / 1000.0)


@tool("查 UI 操作常识：控件类型该用什么动作、动作后该看什么信号、以及 Windows 桌面惯例"
      "（标题栏最右=关闭、对话框确认在左取消在右、没有最大化框就不能最大化）。"
      "开工前先查一次，比自己猜或者反复试要省事；条目来自微软官方规范并已在本地验证过。")
def desktop_ui_rules(query: str = "") -> dict:
    from .ui_rules import lookup, rule_entries

    rules = lookup(query) if str(query or "").strip() else rule_entries()[:8]
    return {"success": True, "query": query, "count": len(rules), "rules": rules,
            "hint": "rule.value 是「场景 → 预期 → 验证方式」；verify 用的都是本内核已有的信号"}


@tool("读取窗口的窗口能力：能否最大化/最小化（Win32 样式 + UIA WindowPattern 双判据）。"
      "固定尺寸的对话框没有最大化按钮，这时对「最大化」目标应该直接放弃而不是去点最小化。")
def desktop_window_caps(window_id: str) -> dict:
    controller = _controller()
    if controller is None:
        return {"error": "DESKTOP_NOT_READY: AppController 尚未初始化"}
    with _STATE._lock:
        info = _STATE._windows.get(window_id)
    if info is None:
        return {"success": False, "error": f"window_id 不存在: {window_id}"}
    hwnd = int(info["hwnd"])
    import win32con
    style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
    can_maximize = bool(style & win32con.WS_MAXIMIZEBOX)
    can_minimize = bool(style & win32con.WS_MINIMIZEBOX)
    uia_state = None
    try:
        from pywinauto.uia_defines import IUIA

        uia = IUIA()
        element = uia.iuia.ElementFromHandle(hwnd)
        pattern = element.GetCurrentPattern(uia.UIA_WindowPatternId)
        uia_state = {"can_maximize": bool(pattern.CurrentCanMaximize),
                     "can_minimize": bool(pattern.CurrentCanMinimize),
                     "visual_state": str(pattern.CurrentWindowVisualState)}
        can_maximize = can_maximize or uia_state["can_maximize"]
    except Exception:  # noqa: BLE001 - WindowPattern is optional
        pass
    return {"success": True, "window_id": window_id, "hwnd": hwnd,
            "can_maximize": can_maximize, "can_minimize": can_minimize,
            "style": {"maximize_box": bool(style & win32con.WS_MAXIMIZEBOX),
                      "minimize_box": bool(style & win32con.WS_MINIMIZEBOX),
                      "sys_menu": bool(style & win32con.WS_SYSMENU)},
            "uia_window_pattern": uia_state}


@tool("等一个由刚才动作引发的新窗口出现（新窗口可能几秒后才弹出）。"
      "先调用动作类工具，再调用本工具；它用动作前的窗口快照做基线，返回新增窗口的 hwnd/标题/进程，"
      "拿到后可以直接 bind 或 analyze 那个新窗口来判断结果。")
def desktop_wait_window(window_id: str = "", timeout_ms: int = 15000) -> dict:
    timeout_ms = max(200, min(int(timeout_ms or 15000), 60000))
    baseline = _WINDOW_BASELINE.get(window_id) if window_id else None
    if baseline is None:
        # No action snapshot to compare against: wait for ANY new window.
        baseline = window_snapshot()
    change = wait_window_change(baseline, budget_ms=timeout_ms)
    for info in change["added"]:
        with _STATE._lock:
            for window_id_known, bound in _STATE._windows.items():
                if bound["hwnd"] == info["hwnd"]:
                    info["window_id"] = window_id_known
    return {"success": bool(change["added"]), "added": change["added"],
            "removed": change["removed"], "waited_ms": change.get("settled_ms", timeout_ms),
            "hint": "用 added[0].hwnd 绑定新窗口，再 analyze 判断这次点击的结果"}


def _foreground_window_info(hwnd: int) -> dict[str, Any] | None:
    """Info for ANY top-level window, visible or not.

    The foreground window may legitimately be an INVISIBLE CEF input-proxy
    (IsWindowVisible=False) that still holds the real keyboard focus —
    _window_info() only enumerates visible windows and would refuse exactly
    the windows desktop_pick_foreground exists for (2026-09-07 live regression:
    DESKTOP_LOGIN_Monitor hwnd was rejected as 不可见或已关闭).
    """
    try:
        if not win32gui.IsWindow(hwnd):
            return None
        title = win32gui.GetWindowText(hwnd) or f"hwnd-{hwnd}"
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        try:
            exe = psutil.Process(pid).name()
        except Exception:
            exe = ""
        cls = ""
        try:
            cls = win32gui.GetClassName(hwnd)
        except Exception:
            pass
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        return {
            "hwnd": hwnd, "title": title[:120], "exe": exe, "pid": pid,
            "class": cls, "rect": [left, top, right, bottom],
            "visible": bool(win32gui.IsWindowVisible(hwnd)),
        }
    except Exception:
        return None


def _find_window_id_by_hwnd(hwnd: int) -> str | None:
    with _STATE._lock:
        for window_id, info in _STATE._windows.items():
            if info["hwnd"] == hwnd:
                return window_id
    return None


# ── Scoped file workspace ────────────────────────────────────────────────

def _session_workspace() -> Path:
    from .config import dsh_desktop_home
    base = dsh_desktop_home() / "desktop_workspace"
    base.mkdir(parents=True, exist_ok=True)
    return base


class _FileLedger:
    """Tracks files created by the agent in this session; delete is ledger-only."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.created: dict[str, dict[str, Any]] = {}
        self.delete_count = 0

    def register(self, path: Path, note: str) -> None:
        with self._lock:
            if len(self.created) >= _MAX_SESSION_FILES:
                raise RuntimeError("会话文件数量已达上限（200），请先清理不再需要的文件")
            self.created[str(path)] = {"size": path.stat().st_size if path.exists() else 0, "note": note[:200]}

    def is_mine(self, path: Path) -> bool:
        with self._lock:
            return str(path) in self.created

    def delete(self, path: Path) -> int:
        with self._lock:
            if self.delete_count >= _MAX_DELETE_OPS:
                raise RuntimeError("会话删除操作已达上限（100）")
            if str(path) not in self.created:
                raise PermissionError("DELETE_NOT_OWNED: 只能删除本会话中由 Agent 自己创建的文件")
            path.unlink(missing_ok=True)
            self.created.pop(str(path), None)
            self.delete_count += 1
            return len(self.created)


_LEDGER = _FileLedger()


def _safe_workspace_path(raw: str) -> Path:
    base = _session_workspace().resolve()
    p = (base / raw.lstrip("/\\")).resolve()
    if not p.is_relative_to(base):
        raise PermissionError("路径越出会话工作区")
    return p


# ── Tool implementations (standalone functions, not controller methods) ──

def _controller() -> "Any | None":
    """Return the process-wide AppController if one was created (set in setup())."""
    return getattr(AppControllerRef, "current", None)


def _settle(controller: Any, scale: float = 1.0, window_id: str = "") -> None:
    """Wait for the UI to settle after an input action.

    Used to be a blind `time.sleep(action_settle_delay)` (1.2s by default), which
    made every click cost ~1.6s no matter how fast the UI actually was. When the
    caller says which window it acted on, probe the pixels instead and return as
    soon as the screen stops moving — the config value becomes the ceiling, not the
    duration. `scale` keeps lighter actions (scroll, single key) cheaper.
    """
    try:
        delay = float(controller.config.workspace.action_settle_delay)
    except Exception:
        delay = 1.2
    ceiling = max(0.0, min(delay, 5.0)) * max(0.0, min(scale, 1.0))
    if ceiling <= 0:
        return
    if window_id:
        try:
            desktop_wait_stable(window_id, quiet_ms=180,
                            timeout_ms=int(ceiling * 1000), interval_ms=60, threshold=1.2,
                            max_changed_cells=10)
            return
        except Exception:  # noqa: BLE001 - fall back to the blind sleep
            logger.debug("_settle: stability probe failed, sleeping instead", exc_info=True)
    time.sleep(ceiling)


class AppControllerRef:
    """Holds a weak module-level reference to the active AppController."""

    current: Any = None


@tool("列出当前桌面上所有可见的顶层窗口（hwnd、标题、exe、pid、rect），供多窗口绑定使用。")
def desktop_list_windows() -> dict:
    windows = _list_visible_windows()
    with _STATE._lock:
        known = {info["hwnd"]: window_id for window_id, info in _STATE._windows.items()}
    for info in windows:
        info["window_id"] = known.get(info["hwnd"], "")
    return {"windows": windows, "count": len(windows)}


@tool("绑定一个窗口到会话（多窗口可同时绑定）。返回 window_id，后续 analyze/click/type 等都使用它。"
      "传 hwnd（整数，先用 desktop_list_windows 取）；也可只传 label/标题，按标题或进程名模糊匹配唯一窗口。")
def desktop_pick_window(hwnd: Any = None, label: str = "") -> dict:
    # 容错三件事（2026-09-15 实测 AI 连续 5 次失败的原因）：
    #   ① 完全不传 hwnd → 工具层抛 TypeError("missing a required argument: 'hwnd'")，
    #      这种报错对模型没有任何指导意义，它只会原样重试。改为返回可执行的指引 + 候选列表。
    #   ② 传字符串句柄（"984636" / "0x1F30CC"）或从 desktop_list_windows 复制来的 dict/list，
    #      统一归一化成 int。
    #   ③ 只给了标题字符串（如 "腾讯 desktop"）时当 label 用，按标题/进程名唯一匹配。
    raw = hwnd
    if isinstance(raw, dict):
        raw = raw.get("hwnd")
    elif isinstance(raw, (list, tuple)) and raw:
        raw = raw[0]
    if isinstance(raw, bool):          # True/False 当句柄属于误用
        raw = None
    cand: "int | None" = None
    if isinstance(raw, int) and raw > 0:
        cand = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            cand = int(raw.strip(), 0)
        except ValueError:
            label = label or raw.strip()
    if cand is None:
        key = (label or "").strip().lower()
        windows = _list_visible_windows()
        hit = [
            w for w in windows
            if key and (key in w["title"].lower() or key in w["exe"].lower())
        ]
        if len(hit) == 1:
            cand = hit[0]["hwnd"]
        else:
            pool = hit or windows
            return {
                "success": False,
                "error": ("缺少有效 hwnd。请先调用 desktop_list_windows 取得整数 hwnd，"
                          "再 desktop_pick_window(hwnd=<整数>)。"),
                "candidates": [
                    {"hwnd": w["hwnd"], "title": w["title"], "exe": w["exe"], "pid": w["pid"]}
                    for w in pool[:10]
                ],
                "count": len(pool),
            }
    info = _window_info(cand)
    if info is None:
        return {"success": False, "error": f"窗口不存在或不可见: hwnd={cand}"}
    window_id = f"win-{uuid.uuid4().hex[:10]}"
    with _STATE._lock:
        _STATE._windows[window_id] = {
            "hwnd": cand,
            "title": info["title"],
            "exe": info["exe"],
            "pid": info["pid"],
            "label": label[:60] or info["title"][:60],
            "bound_at": datetime.now().isoformat(timespec="seconds"),
        }
    return {
        "success": True,
        "window_id": window_id,
        "window": {k: v for k, v in _STATE._windows[window_id].items() if k != "hwnd"},
        "hint": "记住 window_id。多窗口任务时，可同时绑定多个窗口并用 desktop_switch_window 切换焦点。",
    }


@tool("解绑一个窗口。")
def desktop_unpick_window(window_id: str) -> dict:
    with _STATE._lock:
        info = _STATE._windows.pop(window_id, None)
    if info is None:
        return {"success": False, "error": f"window_id 不存在: {window_id}"}
    return {"success": True, "unbound": info.get("label", "")}


@tool("列出当前会话中已绑定的全部窗口（多窗口任务时的上下文）。")
def desktop_list_bound_windows() -> dict:
    with _STATE._lock:
        windows = [
            {"window_id": window_id, **{k: v for k, v in info.items() if k != "hwnd"}}
            for window_id, info in _STATE._windows.items()
        ]
    return {"windows": windows, "count": len(windows)}


@tool("激活已绑定窗口（还原+提到前台）——一切动作的前置动作，放在流程开头或切换窗口时调用。激活后 wait 秒（默认 1，懒加载的 Electron/webview 应用建议 2-3，等控件树展开）。后续 click/press/type 无需再激活。")
def set_topmost(hwnd: int, on: bool) -> None:
    """窗口持续置顶/恢复（HWND_TOPMOST / HWND_NOTOPMOST）。

    视觉被 IDE/Chromium 渲染层等高 z-order 窗口盖住、瞬时 TOPMOST→NOTOPMOST
    也压不过去时使用：保持 TOPMOST 期间永远在普通窗口之上，用完必须恢复。

    实测（2026-10-26）：未声明 argtypes 时 ctypes 把 HWND_TOPMOST(-1) 当 32 位
    整数传成 0xFFFFFFFF，SetWindowPos 直接失败且返回值被忽略——置顶一直没生效。
    这里显式用 64 位 HWND 并检查返回值。
    """
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    user32.SetWindowPos.restype = wintypes.BOOL
    SWP_NOSIZE, SWP_NOMOVE = 0x0001, 0x0002
    if not user32.SetWindowPos(wintypes.HWND(hwnd), wintypes.HWND(-1 if on else -2),
                               0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE):
        raise ctypes.WinError(ctypes.get_last_error())


def force_foreground(hwnd: int) -> bool:
    """强制窗口置前台（UATA win_uia/engine.py bring_to_foreground 同款）。

    裸 SetForegroundWindow 会被 Windows 前台锁静默拒绝（pywin32 报
    (3, 'SetForegroundWindow', '系统找不到指定的路径') 等）；
    标准套路：把当前线程附加到前台窗口与目标窗口的输入队列
    （AttachThreadInput）后再 SW_RESTORE + BringWindowToTop +
    SetForegroundWindow 抢 z-order。返回是否已真正置前台。
    """
    import ctypes
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    attached: list[int] = []
    current_thread = 0
    try:
        current_thread = kernel32.GetCurrentThreadId()
        thread_ids: list[int] = []
        foreground_hwnd = user32.GetForegroundWindow()
        tid = ctypes.c_uint32()
        if foreground_hwnd:
            user32.GetWindowThreadProcessId(foreground_hwnd, ctypes.byref(tid))
            thread_ids.append(tid.value)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(tid))
        thread_ids.append(tid.value)
        for t in thread_ids:
            if t and t != current_thread and user32.AttachThreadInput(current_thread, t, True):
                attached.append(t)
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        # 实测（2026-10-26）：高完整性应用（desktop 等）的最小化窗口，
        # 普通令牌下 ShowWindow/BringWindowToTop 全部无效（iconic 仍为 1，
        # rect 停在 -21333）；SwitchToThisWindow 能真正恢复并提到 z-order。
        try:
            user32.SwitchToThisWindow(hwnd, True)
        except Exception:
            pass
        user32.BringWindowToTop(hwnd)
        # external automation agent 套路：TOPMOST→NOTOPMOST 强制提 z-order——对付逻辑前台但
        # 被 TOPMOST 窗口（IDE 悬浮面板等）视觉遮挡的情况。
        # 走 set_topmost 以复用 64 位 HWND 签名（裸调用会把 -1 截成 32 位而失败）。
        try:
            set_topmost(hwnd, True)
            set_topmost(hwnd, False)
        except OSError:
            pass
        user32.SetForegroundWindow(hwnd)
        import time as _t
        _t.sleep(0.3)  # SetForegroundWindow 异步生效，稍候再校验
        return bool(user32.GetForegroundWindow() == hwnd)
    finally:
        for t in reversed(attached):
            try:
                user32.AttachThreadInput(current_thread, t, False)
            except Exception:
                pass


@tool("切换焦点到已绑定的窗口（多窗口/客户端弹窗场景：先切回主窗再操作）。"
      "SW_RESTORE + 强前台化（AttachThreadInput 兜底），并等待 wait 秒让前台切换生效；"
      "window_id 来自 desktop_pick_window / desktop_list_bound_windows。"
      "注意：此前该函数漏了 @tool 装饰器 → 未注册，模型一调就被自动修正成 "
      "desktop_pick_window（2026-09-16 现场反复出现），导致多窗口场景切不回主窗。")
def desktop_switch_window(window_id: str, wait: float = 1.0) -> dict:
    with _STATE._lock:
        info = _STATE._windows.get(window_id)
    if info is None:
        return {"success": False, "error": f"window_id 不存在: {window_id}"}
    hwnd = int(info["hwnd"])
    try:
        win32gui.ShowWindow(hwnd, 9)  # SW_RESTORE
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass  # 裸调用被前台锁拒绝时走强前台化
    if win32gui.GetForegroundWindow() != hwnd:
        try:
            force_foreground(hwnd)
        except Exception:
            pass
    if win32gui.GetForegroundWindow() != hwnd:
        # 高完整性窗口可能已恢复可见但拿不到前台（跨完整性前台锁）。
        # 截图/OCR/坐标点击只需要可见且置顶，不必是前台；如实返回 foreground=False。
        if win32gui.IsWindowVisible(hwnd) and not win32gui.IsIconic(hwnd):
            rect = win32gui.GetWindowRect(hwnd)
            return {"success": True, "window_id": window_id, "foreground": False,
                    "rect": {"left": rect[0], "top": rect[1],
                             "width": rect[2] - rect[0], "height": rect[3] - rect[1]},
                    "note": "窗口已恢复可见但未取得前台；"
                            "可继续截图/OCR/坐标点击，键入前可能仍需前台。"}
        return {"success": False,
                "error": "激活前台失败（已尝试 AttachThreadInput + SwitchToThisWindow）"}
    wait = max(0.0, min(float(wait), 10.0))
    if wait:
        time.sleep(wait)
    fg = win32gui.GetForegroundWindow()
    rect = None
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        rect = {"left": left, "top": top, "width": right - left, "height": bottom - top}
    except Exception:
        pass
    return {
        "success": True,
        "window_id": window_id,
        "foreground": fg == hwnd,
        "label": info.get("label", ""),
        "rect": rect,
        "note": "窗口已前台。后续动作不再激活；懒加载应用如控件不全，先 desktop_analyze 观察。",
    }


@tool("读取当前**展开的菜单**里的菜单项（name + bbox）。"
      "为什么需要单独一个工具：菜单项会占掉普通 UIA 遍历的节点预算，"
      "带工具栏的窗口（如记事本）在 300 个节点内常常遍历不到菜单项，"
      "于是出现\"菜单明明展开着，uia_tree 却只有 3 个 MenuItem\"。"
      "用法：先点开菜单（点 MenuItem \"文件\"），再调本工具拿准确 bbox，"
      "然后用 bbox 中心点击目标项。**不要用 OCR 猜菜单坐标**。")
def desktop_uia_menu(window_id: str) -> dict:
    controller = _controller()
    if controller is None:
        return {"success": False, "error": "DESKTOP_NOT_READY: AppController 尚未初始化"}
    with _STATE._lock:
        info = _STATE._windows.get(window_id)
    if info is None:
        return {"success": False, "error": f"window_id 不存在: {window_id}"}
    hwnd = int(info["hwnd"])
    if not controller.window.is_valid(hwnd):
        return {"success": False, "error": f"窗口句柄已失效: hwnd={hwnd}"}
    client = controller.window.get_client_region(hwnd)
    if client is None:
        return {"success": False, "error": "窗口客户区不可用"}
    ref = (client.left, client.top, client.left + client.width, client.top + client.height)
    try:
        from pywinauto import Desktop
        root = Desktop(backend="uia").window(handle=hwnd).wrapper_object()
    except Exception as exc:
        return {"success": False, "error": f"UIA 连接失败: {exc.__class__.__name__}"}
    items: list[dict] = []
    try:
        for c in root.descendants():
            try:
                ei = c.element_info
                ct = str(ei.control_type or "")
                if ct not in ("MenuItem", "Menu", "MenuBar"):
                    continue
                nm = str(ei.name or "").strip()
                if not nm:
                    continue
                r = ei.rectangle
                left, top, right, bottom = ref
                w = max(right - left, 1)
                h = max(bottom - top, 1)
                bbox = [
                    max(0, min(1000, round((r.left - left) / w * 1000))),
                    max(0, min(1000, round((r.top - top) / h * 1000))),
                    max(0, min(1000, round((r.right - left) / w * 1000))),
                    max(0, min(1000, round((r.bottom - top) / h * 1000))),
                ]
                items.append({"name": nm[:80], "control_type": ct, "bbox": bbox})
            except Exception:
                continue
    except Exception as exc:
        return {"success": False, "error": f"遍历失败: {exc.__class__.__name__}: {exc}"}
    return {
        "success": True,
        "count": len(items),
        "items": items,
        "hint": ("菜单项一般不在窗口内（是独立 popup），所以 bbox 可能被钳到边界；"
                 "若 bbox 宽度为 0，改用菜单项文字的 OCR 框，或用 pywinauto 的 "
                 "click_input()。"),
    }


@tool("对已绑定窗口截图并解析 UI 元素（零扰动：永不激活窗口、永不改变焦点）。默认引擎是插件强制配置的 local-deepseek（本机 YOLO 图标检测 + RapidOCR 文字 + 截图 + DeepSeek 图标语义），无需任何外部服务。caption=true（默认）启用图标描述；快速核对刚点击后的页面时可设 caption=false，跳过较慢的图标 caption，仍保留检测框和 OCR，常见响应更快。annotate=true（默认）生成供界面核对的标注图；不要因它关闭模型的图像读取。若页面没有变化，不要重复全量 analyze；先用 UIA 或 wait 检查。")
def _is_blank_frame(frame) -> bool:
    """True when a capture carries no usable content (minimized/occluded black)."""
    try:
        import numpy as np
        if frame is None or getattr(frame, "size", 0) == 0:
            return True
        return float(np.mean(frame)) < 4.0 and float(np.std(frame)) < 3.0
    except Exception:
        return False


def _desktop_origin() -> tuple[int, int]:
    """Top-left of the virtual screen; screen pixels are absolute from here."""
    try:
        import mss
        with mss.mss() as sct:
            monitor = sct.monitors[0]
            return int(monitor.get("left", 0)), int(monitor.get("top", 0))
    except Exception:
        return 0, 0


def _bbox_to_screen(bbox, frame_w: int, frame_h: int, origin: tuple[int, int]) -> list[int] | None:
    """Convert a 0-1000 bbox over the captured frame into absolute screen pixels."""
    try:
        if not bbox or len(bbox) != 4:
            return None
        ox, oy = origin
        return [ox + round(bbox[0] / 1000 * frame_w),
                oy + round(bbox[1] / 1000 * frame_h),
                ox + round(bbox[2] / 1000 * frame_w),
                oy + round(bbox[3] / 1000 * frame_h)]
    except Exception:
        return None


def _desktop_crop_rect(hwnd: int, region: str) -> tuple[int, int, int, int]:
    """Screen-space crop for a desktop-scope capture: (left, top, width, height).

    Parsing the whole 4K desktop wastes 3-4x the OCR time and pulls text from
    unrelated windows (another app's chat, the shell) into the element table.
    "auto" therefore crops to the target window plus any popup that overlaps it,
    which is exactly the area the caller may act on.
    """
    try:
        import win32gui
    except Exception:
        return (0, 0, 0, 0)
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    except Exception:
        return (0, 0, 0, 0)
    if region == "window" or right <= left or bottom <= top:
        r = (left, top, right, bottom)
    else:
        margin = 48
        exp = [left - margin, top - margin, right + margin, bottom + margin]
        target_area = max(1, (right - left) * (bottom - top))
        try:
            for win in _list_visible_windows(include_untitled=True):
                if int(win.get("hwnd") or 0) == int(hwnd):
                    continue
                wr = win.get("rect") or []
                if len(wr) != 4:
                    continue
                ww, wh = wr[2] - wr[0], wr[3] - wr[1]
                if ww <= 0 or wh <= 0:
                    continue
                # Only genuine popups: much smaller than the target and anchored
                # INSIDE it. Testing against the target rect (not the margin box)
                # keeps a stray window at the screen edge from widening the crop.
                if ww * wh > 0.4 * target_area:
                    continue
                if wr[2] < left or wr[0] > right or wr[3] < top or wr[1] > bottom:
                    continue
                exp = [min(exp[0], wr[0]), min(exp[1], wr[1]),
                       max(exp[2], wr[2]), max(exp[3], wr[3])]
        except Exception:
            pass
        r = (exp[0], exp[1], exp[2], exp[3])
    try:
        import mss
        with mss.mss() as sct:
            mon = sct.monitors[0]
            sl, st = int(mon["left"]), int(mon["top"])
            sr, sb = sl + int(mon["width"]), st + int(mon["height"])
    except Exception:
        sl = st = 0
        sr = sb = 10 ** 6
    clipped = (max(r[0], sl), max(r[1], st), min(r[2], sr) + 1, min(r[3], sb) + 1)
    return (clipped[0], clipped[1], clipped[2] - clipped[0], clipped[3] - clipped[1])


def _coordinate_contract(scope: str, w: int, h: int,
                         obs_w: Any = None, obs_h: Any = None) -> dict[str, Any]:
    """Say once, in the payload, what an element's click point is measured in.

    Three sizes exist at the same moment: the window (frame_size), the annotated
    observation the model may look at, and the screenshot on disk. Measured on a
    1626px-tall window, reading the observation as if it were the window produced a
    systematic y offset of 360px and two clicks that landed above their target. The
    fix is not a better prompt, it is one field that makes the relationship explicit.

    Do not publish an image-pixels-to-normalized formula here. `desktop_click_image_point`
    performs that conversion in code; putting the formula in the observation payload
    invited callers to re-scale `click_x`/`click_y`, which are already final.
    """

    contract: dict[str, Any] = {
        # The unit differs by scope and mixing them up is the misclick itself: a
        # window-scope click point is normalized 0-1000 for desktop_click, a
        # desktop-scope one is an absolute screen pixel for desktop_click_screen.
        "click_unit": "screen_pixels" if scope == "desktop" else "normalized_0_1000",
        "click_space": "screen" if scope == "desktop" else "window",
        "space": "screen" if scope == "desktop" else "window",
        "window_px": {"width": int(w), "height": int(h)},
        "rule": ("每个元素的 click_x/click_y 已是最终点击坐标，直接传给 desktop_click_screen，不要看图估算。"
                 if scope == "desktop" else
                 "每个元素的 click_x/click_y 已是最终点击坐标，直接传给 desktop_click，不要看图估算。"),
    }
    if isinstance(obs_w, (int, float)) and isinstance(obs_h, (int, float)) and obs_w and obs_h:
        # Size only, for orientation. No conversion formula: the click point is final.
        contract["observation_px"] = {"width": int(obs_w), "height": int(obs_h)}
    return contract


# Last observation per window, used only to judge where a click landed. Never used
# to move the click: coordinates come from the caller or the element table.
_LAST_OBS: dict[str, dict[str, Any]] = {}
_OBS_TTL_SECONDS = 120
_OBS_MAX_WINDOWS = 32


def _remember_observation(window_id: str, elements: list[dict], scope: str = "window") -> None:
    if scope != "window":
        _LAST_OBS.pop(window_id, None)
        return
    boxes: list[tuple[float, float, float, float, str]] = []
    for element in elements:
        box = element.get("bbox") or []
        if len(box) != 4:
            continue
        label = str(element.get("text") or element.get("caption") or "")[:24]
        boxes.append((float(box[0]), float(box[1]), float(box[2]), float(box[3]), label))
    _LAST_OBS[window_id] = {"boxes": boxes, "ts": time.time()}
    if len(_LAST_OBS) > _OBS_MAX_WINDOWS:
        for key in sorted(_LAST_OBS, key=lambda item: _LAST_OBS[item]["ts"])[:8]:
            _LAST_OBS.pop(key, None)


def _click_hit_note(window_id: str, x: float, y: float) -> dict[str, Any] | None:
    """Say where a click landed, in the same terms the caller thinks in.

    Before this, a click that landed 360px above its target still returned success,
    so the only feedback was "the screen did not change" — indistinguishable from a
    slow app, and it invited the caller to nudge the number and try again. Naming the
    nearest element (and its coordinates) makes the correction obvious.
    """

    observation = _LAST_OBS.get(window_id)
    if not observation or not observation["boxes"]:
        return None
    if time.time() - observation["ts"] > _OBS_TTL_SECONDS:
        return None
    nearest = None
    for x1, y1, x2, y2, label in observation["boxes"]:
        if x1 <= x <= x2 and y1 <= y <= y2:
            return {"hit": label or "(无文本元素)"}
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        distance = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
        # Tuple slot 3 is the distance. Comparing slot 2 flattened every miss to
        # "the element nearest the top of the screen".
        if nearest is None or distance < nearest[3]:
            nearest = (label, round(cx), round(cy), distance)
    note: dict[str, Any] = {
        "hit": None,
        "warning": "CLICK_HIT_EMPTY",
        "message": "这个点没落在上次 analyze 的任何元素上，多半是坐标算错了。"
                   "不要微调数字重试：重新 analyze，直接用元素自带的 click_x/click_y。",
    }
    if nearest:
        # Same naming as the observation payload: a bare x/y in this note was the
        # one place left where a final 0-1000 point could be mistaken for pixels.
        note["nearest"] = {"text": nearest[0], "click_x": nearest[1], "click_y": nearest[2],
                           "distance": round(nearest[3])}
    return note


def _element_is_addressable(element: dict) -> bool:
    """True when an OCR/YOLO element can be named, so a caller could target it.

    Icons arrive as `text: "icon"` with the real name in `caption`; when neither
    carries anything but a placeholder the element cannot be clicked by name, and
    150 of them are pure payload.
    """

    try:
        from .decide import PLACEHOLDERS
    except Exception:  # noqa: BLE001
        PLACEHOLDERS = {"icon", "ui_element", "unknown", "图标"}
    # "未知" is the captioner's own "no idea" marker and is not in the shared set.
    placeholder = {str(item).casefold() for item in PLACEHOLDERS} | {"未知"}

    def named(value: Any) -> bool:
        text = str(value or "").strip().casefold()
        # One-character labels are real targets in Chinese UIs ("是"/"否"), and the
        # punctuation that is not one is already listed in PLACEHOLDERS.
        return bool(text) and text not in placeholder

    return named(element.get("text")) or named(element.get("caption"))


def _overlapping_windows(hwnd: int, client_rect, limit: int = 3) -> list[dict[str, Any]]:
    """Visible top-level windows stacked above `hwnd` that cover its client area.

    The window-scope capture is a screen crop, so these windows are literally part of
    the picture the parser reads. Naming them turns "the buttons do not match what I
    see in my head" into a fact the caller can act on.
    """

    if client_rect is None:
        return []
    left, top = int(client_rect.left), int(client_rect.top)
    right, bottom = left + int(client_rect.width), top + int(client_rect.height)
    area = max(1, (right - left) * (bottom - top))
    found: list[dict[str, Any]] = []
    current = win32gui.GetTopWindow(0)
    guard = 0
    while current and guard < 400:
        guard += 1
        if current == hwnd:
            break  # everything walked so far is above the target
        try:
            if win32gui.IsWindowVisible(current) and not win32gui.IsIconic(current):
                cls = str(win32gui.GetClassName(current) or "")
                title = str(win32gui.GetWindowText(current) or "").strip()
                if cls not in ("Progman", "WorkerW", "Shell_TrayWnd", "Windows.UI.Core.CoreWindow") and title:
                    rect = win32gui.GetWindowRect(current)
                    overlap_x = max(0, min(rect[2], right) - max(rect[0], left))
                    overlap_y = max(0, min(rect[3], bottom) - max(rect[1], top))
                    if overlap_x > 0 and overlap_y > 0:
                        ratio = (overlap_x * overlap_y) / area
                        if ratio >= 0.02:
                            found.append({"title": title[:60], "class": cls[:40],
                                          "overlap": round(min(ratio, 1.0), 2)})
        except Exception:  # noqa: BLE001
            pass
        current = win32gui.GetWindow(current, 2)  # GW_HWNDNEXT
    found.sort(key=lambda item: item["overlap"], reverse=True)
    return found[:limit]


def desktop_analyze(
    window_id: str, engine: str = "", caption: bool = True,
    caption_icons: bool = False, annotate: bool = True,
    scope: str = "window", recover_black: bool = False, region: str = "auto",
    only_actionable: bool = False, max_elements: int = 0,
) -> dict:
    """
    Args:
        window_id: 已绑定窗口 ID。
        engine: 解析引擎。插件运行时强制 local-deepseek；omni/qwen 分支仅为离线兼容保留，
            不应作为默认路径（OmniParser 从未启用）。
        caption: 主解析是否生成图标语义描述；快速状态核对可设 false 跳过 caption。
        caption_icons: 对主解析检测到的图标做 auxiliary service/Qwen 语义二次校正。
        annotate: 生成带元素框和鼠标十字的标注图。
        scope: window = 只截目标窗口客户区（默认，精度最高）；desktop = 截桌面，
            用于看见独立窗口里的下拉菜单/浮层。
        recover_black: 截到近全黑图时允许激活窗口重截一次（会改变前台，默认关闭）。
        region: 仅 scope=desktop 生效。auto（默认）= 裁到目标窗口 + 与它重叠的弹层，
            只对这块区域做 OCR/YOLO；window = 只裁目标窗口；full = 整个桌面。
    """
    scope = str(scope or "window").strip().lower()
    if scope not in ("window", "desktop"):
        return {"error": "scope 仅支持 window 或 desktop"}
    region = str(region or "auto").strip().lower()
    if region not in ("auto", "window", "full"):
        return {"error": "region 仅支持 auto、window 或 full"}
    controller = _controller()
    if controller is None:
        return {"error": "DESKTOP_NOT_READY: AppController 尚未初始化"}
    if not engine:
        configured = getattr(getattr(getattr(controller, "config", None), "parser", None), "engine", "omni")
        engine = os.getenv("DSH_DESKTOP_PARSER_ENGINE") or (configured if isinstance(configured, str) else "omni")
    if engine not in ("omni", "qwen", "local", "local-deepseek"):
        return {"error": "engine 仅支持 omni、qwen、local 或 local-deepseek"}
    with _STATE._lock:
        info = _STATE._windows.get(window_id)
    if info is None:
        return {"error": f"window_id 不存在: {window_id}"}
    hwnd = int(info["hwnd"])
    total_started = time.perf_counter()
    # VISION capture must NEVER disturb focus (2026-09-07 live regression:
    # activating the main window while a CEF dropdown held focus collapsed
    # it — the agent clicked 下一步, analyze re-activated, and the page
    # looked like the click never happened). mss just grabs the window rect;
    # a truly occluded window shows its occluder in the shot, which is the
    # honest signal for the agent. Foreground-gated activation stays ONLY
    # in desktop_uia_tree, where lazy UIA genuinely needs it.
    stage_started = time.perf_counter()
    desktop_origin = _desktop_origin()
    crop_rect: tuple[int, int, int, int] | None = None
    try:
        if scope == "desktop":
            full = controller.capture.capture_desktop()
            if full is None:
                frame = None
            elif region == "full":
                frame = full
                crop_rect = (desktop_origin[0], desktop_origin[1], full.shape[1], full.shape[0])
            else:
                left, top, width, height = _desktop_crop_rect(hwnd, region)
                if width <= 0 or height <= 0:
                    frame = full
                    crop_rect = (desktop_origin[0], desktop_origin[1], full.shape[1], full.shape[0])
                else:
                    x0, y0 = max(0, left - desktop_origin[0]), max(0, top - desktop_origin[1])
                    x1 = min(full.shape[1], x0 + width)
                    y1 = min(full.shape[0], y0 + height)
                    frame = full[y0:y1, x0:x1]
                    crop_rect = (desktop_origin[0] + x0, desktop_origin[1] + y0, x1 - x0, y1 - y0)
            if crop_rect:
                # Parsing is done on the crop, so bbox -> screen needs this origin.
                desktop_origin = (crop_rect[0], crop_rect[1])
        else:
            frame = controller.capture.capture(hwnd, activate=False)
            if _is_blank_frame(frame) and recover_black:
                frame = controller.capture.capture(hwnd, activate=True)
    except Exception as exc:
        timing_ms = {"capture": round((time.perf_counter() - stage_started) * 1000)}
        timing_ms["total"] = round((time.perf_counter() - total_started) * 1000)
        return {"error": f"截图失败: {exc.__class__.__name__}", "timing_ms": timing_ms}
    timing_ms = {"capture": round((time.perf_counter() - stage_started) * 1000)}
    if frame is None:
        timing_ms["total"] = round((time.perf_counter() - total_started) * 1000)
        return {"error": "截图失败", "timing_ms": timing_ms}
    if _is_blank_frame(frame):
        # A black frame is not "nothing on screen": it means the capture is
        # unusable (minimized window, locked session, PrintWindow failure).
        # Returning it silently makes OCR invent nothing and wastes a step.
        timing_ms["total"] = round((time.perf_counter() - total_started) * 1000)
        return {"error": "CAPTURE_BLACK: 截图为空或近全黑",
                "hint": ("窗口可能已最小化/被锁屏。先 desktop_topmost 或激活窗口再截图；"
                         "或传 recover_black=true 允许本次激活重截。"),
                "scope": scope, "timing_ms": timing_ms}
    h, w = frame.shape[:2]
    stage_started = time.perf_counter()
    try:
        if engine in ("local", "local-deepseek"):
            from .deepseek_caption import adapt_local_elements
            parsed = {"elements": adapt_local_elements(controller.ui_parser.parse(frame)),
                      "parse_engine_used": engine}
        else:
            parsed = _STATE.parser.parse(frame, engine=engine, caption=caption)
    except Exception as exc:
        timing_ms["parse"] = round((time.perf_counter() - stage_started) * 1000)
        timing_ms["total"] = round((time.perf_counter() - total_started) * 1000)
        return {"error": f"解析失败: {exc.__class__.__name__}", "timing_ms": timing_ms}
    timing_ms["parse"] = round((time.perf_counter() - stage_started) * 1000)
    if "error" in parsed:

        timing_ms["total"] = round((time.perf_counter() - total_started) * 1000)
        return {**parsed, "timing_ms": timing_ms}
    elements = parsed["elements"]
    result: dict[str, Any] = {
        "window_id": window_id,
        "engine": engine,
        "caption_enabled": caption,
        "ui_elements": elements,
        "space": "desktop" if scope == "desktop" else "window",
        "frame_size": {"width": int(w), "height": int(h)},
        "region": {"mode": region if scope == "desktop" else "window",
                   "screen_rect": list(crop_rect) if crop_rect else None},
        # Three sizes coexist in this result — the window, the annotated
        # observation, and the screenshot on disk — and a caller measuring pixels
        # off the image used to have no way to tell them apart. Measured on a
        # 1626px-tall window, mixing them up produced a 360px y offset and two
        # clicks that landed above their target.
        "coordinate_contract": _coordinate_contract(scope, w, h),
    }
    # Which tool the element click point belongs to, said once at the top level:
    # `space` is the scope ("desktop"/"window") and its vocabulary did not match the
    # old per-element click.space ("screen"/"window"), which is how a screen-pixel
    # click ended up sent to desktop_click.
    result["click_space"] = "screen" if scope == "desktop" else "window"
    # Every element carries a ready-to-use click point, tagged with the space it
    # belongs to. Callers must not re-derive it: mixing normalized window coords
    # with screen pixels is the single most common source of misclicks.
    for index, element in enumerate(elements):
        box = element.get("bbox") or []
        element["id"] = index
        if len(box) != 4:
            continue
        ncx, ncy = round((box[0] + box[2]) / 2), round((box[1] + box[3]) / 2)
        if scope == "desktop":
            rect = _bbox_to_screen(box, w, h, desktop_origin)
            if rect:
                element["screen_rect"] = rect
                element["click"] = {"space": "screen",
                                    "x": round((rect[0] + rect[2]) / 2),
                                    "y": round((rect[1] + rect[3]) / 2)}
        else:
            element["click"] = {"space": "window", "x": ncx, "y": ncy}
    if scope == "desktop":
        result["desktop_origin"] = {"x": desktop_origin[0], "y": desktop_origin[1]}
        result["hint"] = ("region 已裁到操作区域；每个元素带 click_x/click_y，"
                          "直接传给 desktop_click_screen(click_x, click_y)，不要看图像素或 bbox 换算。")
    else:
        result["hint"] = ("每个元素带 click_x/click_y，直接传给 desktop_click(click_x, click_y)；"
                          "不要除以图片宽高，也不要按 bbox 换算。")
    timing_ms["caption_icons"] = 0

    for key in ("parse_engine_used", "parse_fallback", "parse_fallback_error", "parse_aux_model_called"):

        if key in parsed:
            result[key] = parsed[key]
    local_semantics = engine == "local-deepseek" and (caption or caption_icons)
    if (local_semantics or (caption_icons and engine in ("omni", "qwen"))) and elements:
        stage_started = time.perf_counter()
        captioned: dict[str, Any]
        try:
            if local_semantics:
                from .deepseek_caption import caption_icons as deepseek_caption_icons
                captioned = deepseek_caption_icons(frame, elements, context=str(info.get("title") or ""))
            else:
                captioned = _STATE.parser.caption_icons(frame, elements)
        except Exception as exc:
            captioned = {"error": f"CAPTION_FAILED: {exc.__class__.__name__}"}
        finally:
            timing_ms["caption_icons"] = round((time.perf_counter() - stage_started) * 1000)

        if "icons" in captioned:

            by_index = {
                icon.get("index"): icon for icon in captioned["icons"]
                if icon.get("index") is not None
            }
            for pos, element in enumerate(elements):
                icon = by_index.get(pos) or {}
                semantic = icon.get("caption")
                if semantic:
                    element["caption"] = semantic
                    if icon.get("source"):
                        element["caption_source"] = icon["source"]
            result["icon_captions_applied"] = len(by_index)
            if local_semantics:
                result["caption_deepseek_called"] = bool(captioned.get("deepseek_called"))
                if captioned.get("errors"):
                    result["icon_caption_error"] = "DEEPSEEK_CAPTION_PARTIAL_FAILURE"
            else:
                result["caption_aux_model_called"] = bool(captioned.get("aux_model_called"))
            result["caption_stats"] = {
                key: captioned[key]
                for key in ("n_total", "n_cached", "n_deterministic", "n_queried", "elapsed_ms")
                if key in captioned
            }
        else:
            caption_error = str(captioned.get("error", "unknown"))
            result["icon_caption_error"] = caption_error.split(":", 1)[0][:80]

    deterministic_captions = 0
    for element in elements:
        semantic = _standard_window_chrome_caption(element)
        if semantic:
            # Standard top-right Windows controls have stable spatial semantics;
            # do not let a small-crop VL guess invert minimize/maximize/close.
            element["caption"] = semantic
            element["caption_source"] = "window_chrome_geometry"
            deterministic_captions += 1
    if deterministic_captions:
        result["deterministic_captions"] = deterministic_captions
    stage_started = time.perf_counter()
    try:
        date_dir = Path(controller.config.workspace.screenshot_dir) / datetime.now().strftime("%Y-%m-%d")
        date_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = str(date_dir / f"shot_{hwnd}_{ts}.jpeg")
        max_dim = controller.config.workspace.screenshot_max_dim
        if max(w, h) > max_dim:
            scale = max_dim / max(w, h)
            small = cv2.resize(frame, (int(w * scale), int(h * scale)))
        else:
            small = frame
        # width/height describe image_path pixels, not the pre-resize capture.
        # ui_elements bbox values remain normalized 0-1000 over the client area.
        result["width"], result["height"] = small.shape[1], small.shape[0]
        result["source_width"], result["source_height"] = w, h
        # The observation size only exists here; complete the contract so a caller
        # reading pixels off `image_path` divides by the image, not the window.
        if isinstance(result.get("coordinate_contract"), dict):
            result["coordinate_contract"].update(
                _coordinate_contract(scope, w, h, result["width"], result["height"]))
        ok, buf = cv2.imencode(".jpeg", small)
        if not ok:
            raise RuntimeError("jpeg_encode_failed")
        Path(path).write_bytes(buf.tobytes())
        result["image_path"] = path
    except Exception as exc:
        timing_ms["save"] = round((time.perf_counter() - stage_started) * 1000)
        timing_ms["total"] = round((time.perf_counter() - total_started) * 1000)
        return {"error": f"截图保存失败: {exc.__class__.__name__}", "timing_ms": timing_ms}
    timing_ms["save"] = round((time.perf_counter() - stage_started) * 1000)

    timing_ms["annotate"] = 0
    if annotate:
        # Annotated "Set-of-Mark" copy for the human dashboard: colored boxes
        # + text labels + red crosshair at the mouse position. The chat UI
        # prefers this key over image_path; the model's own vision reads stay
        # on the clean screenshot. Drawing must never fail the analyze.
        stage_started = time.perf_counter()
        try:
            bbox_path = str(date_dir / f"shot_{hwnd}_{ts}_bbox.jpeg")
            controller._save_bbox_overlay(small, elements, bbox_path, hwnd=hwnd)
            result[SOM_IMAGE_PATH_KEY] = bbox_path
            try:
                result["mouse_position"] = controller._get_mouse_position(hwnd)
            except Exception:
                logger.debug("desktop_analyze: mouse position unavailable", exc_info=True)
        except Exception:
            logger.debug("desktop_analyze: overlay generation failed; clean screenshot only", exc_info=True)
        finally:
            timing_ms["annotate"] = round((time.perf_counter() - stage_started) * 1000)
    timing_ms["total"] = round((time.perf_counter() - total_started) * 1000)
    result["timing_ms"] = timing_ms
    # Remember where the elements were, so a click can say whether it landed on
    # one of them instead of reporting success for a point in empty space. Uses the
    # unfiltered list: a placeholder icon is still a legitimate click target.
    try:
        _remember_observation(window_id, elements, scope)
    except Exception:  # noqa: BLE001
        logger.debug("observation cache skipped", exc_info=True)
    # The window-scope capture is a screen crop of the client rect, so a window
    # lying on top is IN the picture and its text is in the element list. Say so,
    # instead of leaving the caller to wonder why the buttons do not match.
    if scope == "window":
        try:
            client = controller.window.get_client_region(hwnd)
            occluders = _overlapping_windows(hwnd, client)
        except Exception:  # noqa: BLE001
            occluders = []
        if occluders:
            result["occlusion"] = {
                "count": len(occluders),
                "windows": occluders,
                "note": ("这些窗口压在目标窗口上方，截图里的像素属于它们，元素表也可能混入它们的内容。"
                         "要看整层画面用 scope=desktop（region=auto 会把重叠弹层一起裁进来）。"),
            }
    # Payload trim happens last: the capture, the overlay and the click cache all
    # used the full list. Defaults are no-ops so internal callers are unaffected.
    if only_actionable or max_elements:
        kept = [element for element in elements if not only_actionable or _element_is_addressable(element)]
        if max_elements and max_elements > 0:
            kept = kept[:max_elements]
        result["ui_elements"] = kept
        result["elements_filter"] = {
            "only_actionable": bool(only_actionable),
            "max_elements": int(max_elements or 0),
            "scanned": len(elements),
            "kept": len(kept),
        }
    return result



def _click_target_ok(hwnd: int, x: int, y: int) -> tuple[bool, dict]:
    """点击穿透防护：检查归一化坐标最终会落到哪个窗口身上。

    为什么必须查（评审 N9 + 实测）：`click_normalized` 用的是**屏幕绝对坐标**。
    如果目标窗口不在前台，或者同进程开了多个窗口（实测 4 个记事本），
    点击就会落到**别的窗口**上 —— 表现为"点了没反应"，或者更糟：点到用户的应用里。

    判据：
      · 点所在顶层窗口 == 目标 hwnd          → 允许
      · 点所在窗口是目标进程的 owned popup   → 允许（对话框属于该应用）
      · 其余                                  → 拒绝 CLICK_TARGET_MISMATCH
    """
    import win32gui
    import win32process
    try:
        import psutil
    except Exception:
        psutil = None
    detail: dict = {}
    try:
        left, top, right, bottom = win32gui.GetClientRect(hwnd)
        pt_left, pt_top = win32gui.ClientToScreen(hwnd, (0, 0))
        width, height = right - left, bottom - top
        if width <= 0 or height <= 0:
            return True, {"skipped": "client rect unavailable"}
        sx = pt_left + int(int(x) / 1000 * width)
        sy = pt_top + int(int(y) / 1000 * height)
        detail.update({"screen": [sx, sy]})
        point_hwnd = win32gui.WindowFromPoint((sx, sy))
        root = win32gui.GetAncestor(point_hwnd, 2)  # GA_ROOT
        detail["point_hwnd"] = int(point_hwnd)
        detail["root_hwnd"] = int(root)
        if int(root) == int(hwnd) or int(point_hwnd) == int(hwnd):
            return True, detail
        _, tpid = win32process.GetWindowThreadProcessId(hwnd)
        _, ppid_ = win32process.GetWindowThreadProcessId(root)
        detail["target_pid"] = int(tpid)
        detail["point_pid"] = int(ppid_)
        if psutil is not None:
            try:
                detail["point_exe"] = psutil.Process(ppid_).name()
                detail["target_exe"] = psutil.Process(tpid).name()
            except Exception:
                pass
        # 同一进程的其他窗口：只允许"目标拥有它"的情况
        owner = win32gui.GetWindow(root, 4) if root else 0
        if int(ppid_) == int(tpid) and int(owner) == int(hwnd):
            return True, detail
        return False, detail
    except Exception as exc:  # noqa: BLE001
        detail["error"] = f"{type(exc).__name__}: {exc}"
        return True, detail   # 查不了就不拦（避免误伤）


@tool("在已绑定窗口点击。两种定位方式：① 传 goal（如 \"关闭\"、\"登录按钮\"、\"开始扫描\"）："
      "本地决策层直接从窗口的元素表里选出该元素再点，**元素表不需要进入模型上下文**，"
      "既省 token 也省一次分析；说不准时它会给候选而不是瞎点。② 传归一化 0-1000 坐标"
      "（取元素 bbox 中心）。优先用 goal；只有目标没有文字、或你已经有精确坐标时才用坐标。")
def desktop_click(window_id: str, x: int = -1, y: int = -1, clicks: int = 1,
              goal: str = "", source: str = "auto", caption: bool = False) -> dict:
    controller = _controller()
    if controller is None:
        return {"error": "DESKTOP_NOT_READY: AppController 尚未初始化"}
    with _STATE._lock:
        info = _STATE._windows.get(window_id)
    if info is None:
        return {"success": False, "error": f"window_id 不存在: {window_id}"}
    decided = None
    if str(goal or "").strip():
        from .decide import decide

        table = desktop_element_table(window_id, source, False, caption)
        if not table.get("success"):
            return table
        decision = decide(goal, table.get("elements", []))
        if decision.id is None:
            return {"success": False, "effect": "refused", "goal": goal,
                    "score": decision.score, "gap": decision.gap,
                    "reason": decision.reason, "candidates": decision.candidates[:5],
                    "error": "本地决策层没有找到可信元素（分数/差距不足），未点击"}
        picked = next(e for e in table["elements"] if e["id"] == decision.id)
        box = picked["bbox"]
        x = round((box[0] + box[2]) / 2)
        y = round((box[1] + box[3]) / 2)
        decided = {"id": picked["id"], "text": picked["text"], "bbox": box,
                   "score": round(decision.score, 3), "gap": round(decision.gap, 3),
                   "table_source": table.get("source"), "table_size": table.get("count")}
    if not 0 <= x <= 1000 or not 0 <= y <= 1000:
        return {"success": False, "error": "坐标必须在 0-1000，或者传 goal 让决策层定位"}
    ok, detail = _click_target_ok(int(info["hwnd"]), int(x), int(y))
    if not ok:
        return {"success": False, "effect": "refused",
                "error": "CLICK_TARGET_MISMATCH: 该点会落到别的窗口上，已拒绝点击"
                         "（目标不在前台，或同进程开了多个窗口）。"
                         "请先 activate 目标窗口，或改用 desktop_uia_invoke。",
                "click_guard": detail, "window_id": window_id}
    _point_cursor_at(int(info["hwnd"]), int(x), int(y))
    with controller._input_lock:
        result = controller.input.click_normalized(int(info["hwnd"]), int(x), int(y), clicks=int(clicks))
    _settle(controller, window_id=window_id)
    # click_normalized returns ABSOLUTE screen coords under "x"/"y" — the
    # agent reads them as normalized and thinks the click landed wrong
    # (2026-09-07 live: desktop_click(530,525) echoed x=988 → two wasted retry
    # rounds). Echo the normalized INPUT as x/y; move absolutes to screen_*.
    if isinstance(result, dict) and "x" in result:
        result["screen_x"], result["screen_y"] = result.pop("x"), result.pop("y")
        result["x"], result["y"] = int(x), int(y)
    result["window_id"] = window_id
    if decided:
        result["decided"] = decided
    # A click in empty space used to come back as plain success; name what was
    # under the point, or what the nearest element was.
    try:
        note = _click_hit_note(window_id, int(x), int(y))
    except Exception:  # noqa: BLE001
        note = None
    if note:
        result["hit_check"] = note
    return result


@tool("按**屏幕绝对像素**点击（不经过窗口归一化，不要求先绑定窗口）。"
      "坐标直接取自 desktop_analyze(scope=\"desktop\") 返回的 screen_rect 中心。"
      "用于点击弹出菜单、下拉框、浮层等超出目标窗口客户区的元素——这类弹层是独立窗口，"
      "按窗口归一化点击会点到别处。点击后请用 desktop_analyze(scope=\"desktop\") 复核。")
def desktop_click_screen(x: int, y: int, clicks: int = 1) -> dict:
    controller = _controller()
    if controller is None:
        return {"error": "DESKTOP_NOT_READY: AppController 尚未初始化"}
    try:
        gx, gy = int(x), int(y)
        n = max(1, min(int(clicks), 3))
    except (TypeError, ValueError):
        return {"success": False, "error": "x/y 必须是整数，clicks 为 1-3"}
    ox, oy = _desktop_origin()
    try:
        import mss
        with mss.mss() as sct:
            mon = sct.monitors[0]
            left, top = int(mon["left"]), int(mon["top"])
            right, bottom = left + int(mon["width"]), top + int(mon["height"])
    except Exception:
        left = top = 0
        right = bottom = 10 ** 6
    if not (left <= gx <= right and top <= gy <= bottom):
        return {"success": False,
                "error": f"SCREEN_POINT_OUT_OF_BOUNDS: ({gx},{gy}) 不在虚拟屏幕 "
                         f"[{left},{top}]-[{right},{bottom}] 内",
                "hint": "坐标必须是屏幕绝对像素；desktop_analyze(scope=\"desktop\") 的 "
                        "screen_rect 已经带 desktop_origin 偏移，直接用。"}
    with controller._input_lock:
        result = controller.input.click_screen(gx, gy, n)
    return {"success": bool(result.get("success", False)),
            "window_id": "", "space": "screen", "x": gx, "y": gy, "clicks": n,
            "dispatch": "screen", "raw": result,
            "note": "已按屏幕像素点击；结果需要复核，不要据 success 直接断定业务生效。",
            "desktop_origin": {"x": ox, "y": oy}}


@tool("向已绑定窗口投递后台鼠标消息（PostMessage）：不移动物理鼠标、不抢前台，"
      "窗口被遮挡时也能投递。Chromium/Electron 的页面内容经常忽略这类消息，"
      "所以 success 只代表消息已发出；到底有没有生效要由调用方的 effect 判断。"
      "经典 Win32/WPF 控件通常有效。")
def desktop_click_message(window_id: str, x: int, y: int, clicks: int = 1) -> dict:
    controller = _controller()
    if controller is None:
        return {"error": "DESKTOP_NOT_READY: AppController 尚未初始化"}
    with _STATE._lock:
        info = _STATE._windows.get(window_id)
    if info is None:
        return {"success": False, "error": f"window_id 不存在: {window_id}"}
    if not 0 <= x <= 1000 or not 0 <= y <= 1000:
        return {"success": False, "error": "坐标必须在 0-1000"}
    hwnd = int(info["hwnd"])
    region = controller.window.get_client_region(hwnd)
    if region is None:
        return {"success": False, "error": "窗口客户区不可用"}
    px = int(x / 1000 * region.width)
    py = int(y / 1000 * region.height)
    import win32api
    import win32con
    lparam = win32api.MAKELONG(px, py)
    try:
        win32gui.PostMessage(hwnd, win32con.WM_MOUSEMOVE, 0, lparam)
        for _ in range(max(1, min(int(clicks), 3))):
            win32gui.PostMessage(hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lparam)
            win32gui.PostMessage(hwnd, win32con.WM_LBUTTONUP, 0, lparam)
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": f"POSTMESSAGE_FAILED: {exc.__class__.__name__}"}
    return {"success": True, "window_id": window_id, "x": int(x), "y": int(y),
            "client_px": [px, py], "dispatch": "message",
            "note": "消息已投递，但不保证被目标控件处理；请用后续观察确认。"}


_CURSOR = None


def _cursor_overlay():
    global _CURSOR
    if _CURSOR is None:
        from .cursor_overlay import CursorOverlay
        _CURSOR = CursorOverlay()
        _CURSOR.start()
    return _CURSOR


def _point_cursor_at(hwnd: int, x: int, y: int) -> None:
    """Move the cosmetic cursor before a physical click (opt-in: only after desktop_cursor)."""
    overlay = _CURSOR
    if overlay is None:
        return
    try:
        controller = _controller()
        region = controller.window.get_client_region(hwnd) if controller else None
        if region is None:
            return
        overlay.move(region.left + int(x / 1000 * region.width),
                     region.top + int(y / 1000 * region.height))
    except Exception:  # noqa: BLE001 - cosmetics never block an action
        pass


@tool("显示/移动一个装饰性光标（透明置顶、鼠标穿透、不抢焦点）：只影响观感，不移动物理鼠标。"
      "传 window_id + 0-1000 坐标，或直接传屏幕像素 x/y；visible=false 隐藏。")
def desktop_cursor(window_id: str = "", x: int = -1, y: int = -1, visible: bool = True) -> dict:
    overlay = _cursor_overlay()
    if not visible:
        overlay.hide()
        return {"success": True, "visible": False, "error": overlay.error}
    if x < 0 or y < 0:
        return {"success": False, "error": "visible=true 时必须给 x/y"}
    if window_id:
        controller = _controller()
        if controller is None:
            return {"error": "DESKTOP_NOT_READY: AppController 尚未初始化"}
        with _STATE._lock:
            info = _STATE._windows.get(window_id)
        if info is None:
            return {"success": False, "error": f"window_id 不存在: {window_id}"}
        region = controller.window.get_client_region(int(info["hwnd"]))
        if region is None:
            return {"success": False, "error": "窗口客户区不可用"}
        sx = region.left + int(x / 1000 * region.width)
        sy = region.top + int(y / 1000 * region.height)
    else:
        sx, sy = int(x), int(y)
    overlay.move(sx, sy)
    return {"success": True, "visible": True, "screen": [sx, sy], "error": overlay.error}


_GLOW = None
_TONES: dict[int, str] = {}


def _glow():
    global _GLOW
    if _GLOW is None:
        from .window_glow import WindowGlow
        _GLOW = WindowGlow()
        _GLOW.start()
    return _GLOW


def _window_tone(hwnd: int) -> str:
    """Pick the glow tone that contrasts with the window's own brightness."""
    tone = _TONES.get(hwnd)
    if tone:
        return tone
    tone = "light"
    try:
        frame = _print_window_frame(hwnd)
        if frame is not None and getattr(frame, "size", 0):
            tone = "dark" if float(np.asarray(frame).mean()) < 110 else "light"
    except Exception:  # noqa: BLE001 - fall back to the light-UI tone
        pass
    _TONES[hwnd] = tone
    return tone


@tool("给正在被操作的窗口加一圈低饱和蓝色呼吸光晕（透明、鼠标穿透、不抢焦点）："
      "颜色按窗口明暗自适应（OKLCH 调色），层级插在目标窗口正上方，被遮挡时一起被挡住。"
      "每个动作都会自动刷新；停止操作约 4 秒后自动消失；on=false 立即隐藏。")
def desktop_highlight(window_id: str, on: bool = True) -> dict:
    glow = _glow()
    if not on:
        glow.hide()
        return {"success": True, "visible": False, "error": glow.error}
    with _STATE._lock:
        info = _STATE._windows.get(window_id)
    if info is None:
        return {"success": False, "error": f"window_id 不存在: {window_id}"}
    glow.show(int(info["hwnd"]), _window_tone(int(info["hwnd"])))
    return {"success": True, "visible": True, "window_id": window_id,
            "tone": _window_tone(int(info["hwnd"])), "error": glow.error}


def bound_window_info(window_id: str) -> dict:
    """Read-only view of a bound window, for the bridge's capability ledger."""
    with _STATE._lock:
        info = _STATE._windows.get(window_id)
    return dict(info) if isinstance(info, dict) else {}


def _restore_if_minimized(hwnd: int) -> bool:
    """A minimized window has no pixels and rejects input; restore it first."""
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, 9)  # SW_RESTORE
            time.sleep(0.4)
            return True
    except Exception:  # noqa: BLE001 - best effort
        pass
    return False


def _uia_control_at(controller, hwnd: int, x: int, y: int,
                    time_budget_ms: int = 900, max_nodes: int = 1500):
    """Hit-test inside the bound window's own UIA subtree.

    Deliberately NOT Desktop.from_point: that is a screen-global hit test which
    returns whatever window is on top, so a covered target resolves to the
    covering window's control. Walking this window's descendants ignores z-order.

    Cost control (measured: 1.9-3.3s on a 50-control app, most of it wasted):
      * only control types that can actually be invoked get a rectangle() call —
        panes/text/images are containers, and each rectangle() is a COM round trip
      * a wall-clock budget and a node cap stop the walk on huge trees
      * repeated clicks at the same point reuse the last answer for 3s
    """
    cache_key = (hwnd, int(x), int(y))
    cached = _HIT_CACHE.get(cache_key)
    if cached and time.monotonic() - cached[0] < 3.0:
        return cached[1]
    client = controller.window.get_client_region(hwnd)
    if client is None:
        return None
    sx = client.left + int(x / 1000 * client.width)
    sy = client.top + int(y / 1000 * client.height)
    try:
        from pywinauto import Desktop
        window = Desktop(backend="uia").window(handle=hwnd).wrapper_object()
    except Exception:  # noqa: BLE001
        return None
    deadline = time.monotonic() + time_budget_ms / 1000.0
    best = None
    best_area = None
    seen = 0
    try:
        for ctrl in window.descendants():
            seen += 1
            if seen > max_nodes or time.monotonic() > deadline:
                break
            try:
                control_type = str(getattr(ctrl.element_info, "control_type", "") or "")
            except Exception:  # noqa: BLE001
                continue
            if control_type in _HIT_SKIP_TYPES:
                continue
            try:
                rect = ctrl.rectangle()
            except Exception:
                continue
            if not (rect.left <= sx <= rect.right and rect.top <= sy <= rect.bottom):
                continue
            area = max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
            if area > 0 and (best_area is None or area < best_area):
                best, best_area = ctrl, area
    except Exception:  # noqa: BLE001
        return None
    _HIT_CACHE[cache_key] = (time.monotonic(), best)
    return best


def _invoke_control(ctrl) -> str | None:
    for pattern, call in (
        ("invoke", lambda: ctrl.iface_invoke.Invoke()),
        ("toggle", lambda: ctrl.iface_toggle.Toggle()),
        ("select", lambda: ctrl.iface_selection_item.Select()),
        ("expand", lambda: ctrl.iface_expand_collapse.Expand()),
    ):
        try:
            call()
            return pattern
        except Exception:  # noqa: BLE001 - unsupported pattern, try the next one
            continue
    return None


_UIA_STATE_CACHE: dict[tuple[int, str], tuple] = {}
# Controls whose state we can read back reliably. A silent no-op on these is
# safe to retry physically (selecting a tab twice is idempotent); a plain Button
# is NOT, because a second press could submit twice.
_READABLE_STATES = {"TabItem", "RadioButton", "CheckBox", "ListItem", "TreeItem"}

# Element tables are expensive for the OCR source (a full analyze), so repeated
# decisions on the same window reuse one briefly. Keyed by (window_id, source).
_TABLE_CACHE: dict[tuple[str, str], tuple[float, list[dict], str]] = {}
_TABLE_TTL_SECONDS = 20.0

# Control types worth invoking. Containers can never be invoked and only cost a
# COM round trip; Custom/Text/Image stay in because frameworks do expose invokable
# elements under those types (WorkBuddy's clickable region is not a Button).
_HIT_SKIP_TYPES = {"Pane", "Group", "Document", "Window", "TitleBar", "ScrollBar", "MenuBar"}
_HIT_CACHE: dict[tuple[int, int, int], tuple[float, object]] = {}

# Window snapshot taken right before each action, so a later wait can tell what the
# action spawned (keyed by the window the action was aimed at).
_WINDOW_BASELINE: dict[str, dict[int, dict]] = {}


def _state_signature(state: dict) -> tuple:
    if not state or not state.get("found"):
        return ()
    return (state.get("selected"), state.get("toggle"), state.get("enabled"), state.get("value"))


def _actionable_state(state: dict) -> bool:
    """Only expose selected/toggle, and only for control types where that is the truth."""
    if not state or not state.get("found"):
        return False
    if str(state.get("type") or "") not in _READABLE_STATES:
        return False
    return state.get("selected") is not None or state.get("toggle") is not None


@tool("按坐标点击的自动路径：窗口最小化会先还原；给了控件名先走 UIA 语义调用；"
      "没给名字就在绑定窗口自己的 UIA 子树里命中测试（不受遮挡影响）；"
      "都不行才回落到物理点击。UIA 不抛错但控件状态没变（静默失效）时，"
      "对可判读控件（Tab/Radio/Check）自动降级为物理点击。")
def desktop_click_auto(window_id: str, x: int, y: int, clicks: int = 1, name: str = "") -> dict:
    controller = _controller()
    if controller is None:
        return {"error": "DESKTOP_NOT_READY: AppController 尚未初始化"}
    with _STATE._lock:
        info = _STATE._windows.get(window_id)
    if info is None:
        return {"success": False, "error": f"window_id 不存在: {window_id}"}
    hwnd = int(info["hwnd"])
    restored = _restore_if_minimized(hwnd)
    name = (name or "").strip()
    uia_note = ""
    if name:
        key = (hwnd, name.lower())
        before = _UIA_STATE_CACHE.get(key)
        semantic = desktop_uia_invoke(window_id, name)
        if semantic.get("success"):
            state = (desktop_uia_state(window_id, name) or {}).get("control") or {}
            signature = _state_signature(state)
            if signature:
                _UIA_STATE_CACHE[key] = signature
            silently_ignored = bool(
                before is not None and signature and before == signature
                and _actionable_state(state) and int(clicks) == 1
            )
            if not silently_ignored:
                return {**semantic,
                        "dispatch_used": f"uia_{semantic.get('action_used', 'invoke')}",
                        "restored": restored,
                        "uia_state": _state_signature(state) and {
                            "selected": state.get("selected"), "type": state.get("type")},
                        "uia_verified": bool(signature)}
            uia_note = ("UIA 调用没有抛错，但控件状态未变化（静默失效），已降级为物理点击。")
    else:
        control = _uia_control_at(controller, hwnd, x, y)
        if control is not None:
            used = _invoke_control(control)
            if used:
                try:
                    rect = control.rectangle()
                    box = [rect.left, rect.top, rect.right, rect.bottom]
                except Exception:  # noqa: BLE001
                    box = None
                return {"success": True, "window_id": window_id, "dispatch_used": f"uia_hit_{used}",
                        "action_used": used, "control": {"name": str(control.window_text() or "")[:80],
                                                         "type": str(getattr(control.element_info, "control_type", "")),
                                                         "rect": box},
                        "restored": restored,
                        "note": "在绑定窗口的 UIA 子树里命中了可调用控件，没有移动鼠标。"}
    physical = desktop_click(window_id, x, y, clicks)
    payload = dict(physical) if isinstance(physical, dict) else {"result": physical}
    payload["dispatch_used"] = "sendinput_after_uia_noop" if uia_note else "sendinput"
    payload["restored"] = restored
    if uia_note:
        payload["uia_attempt"] = uia_note
    elif name:
        payload["uia_attempt"] = "failed; fell back to physical click"
    return payload


@tool("当坐标来自 desktop_analyze 保存图上的像素位置时使用此工具：按给定 image_width/image_height 将像素点中心换算为窗口客户区 0-1000 坐标后点击。不要将桌面像素直接传给 desktop_click。若坐标来自 ui_elements bbox 中心，应直接调用 desktop_click。")
def desktop_click_image_point(
    window_id: str,
    image_x: int,
    image_y: int,
    image_width: int,
    image_height: int,
    clicks: int = 1,
) -> dict[str, Any]:
    """Click a pixel coordinate from the exact saved desktop_analyze image."""
    if type(image_width) is not int or type(image_height) is not int or image_width <= 0 or image_height <= 0:
        return {"success": False, "error": "image_width/image_height must be positive integers"}
    if type(image_x) is not int or type(image_y) is not int:
        return {"success": False, "error": "image_x/image_y must be integer image pixels"}
    if not (0 <= image_x < image_width and 0 <= image_y < image_height):
        return {"success": False, "error": "image pixel is outside image_width/image_height bounds"}
    x = min(1000, max(0, round((image_x + 0.5) * 1000 / image_width)))
    y = min(1000, max(0, round((image_y + 0.5) * 1000 / image_height)))
    result = desktop_click(window_id, x, y, clicks=clicks)
    if isinstance(result, dict):
        result["image_point"] = {"x": image_x, "y": image_y, "width": image_width, "height": image_height}
        result["coordinate_conversion"] = "saved_image_pixel_center_to_client_normalized_0_1000"
    return result


def _sendinput_click_center(hwnd: int) -> bool:
    """用 SendInput 向目标窗口注入一次真实左键点击，**选一个未被遮挡的点**。

    为什么需要它（实测 2026-10-27）：
      内核与目标同为 High（S-1-16-12288）时，以下三种方式全部
      **返回成功但 GetForegroundWindow() 不变**（Windows 静默忽略）：
        · SwitchToThisWindow
        · keybd_event(VK_MENU) + SetForegroundWindow
        · AttachThreadInput + SetForegroundWindow
      只有 SendInput 注入的事件被系统当作**真实用户输入**，前台才真正切换。

    为什么不能固定点"客户区中心"（实测踩过）：
      企业微信 rect=(493,490,2183,1951)，其中点 (1338,612) 落在占位的
      IDE 窗口范围内 → SendInput 点到了 IDE 上 → 企业微信仍然没被激活。
      所以必须先用 WindowFromPoint 确认这个点真的落在目标窗口上，
      候选点按「中心 → 左上 → 右上 → 左下 → 右下 → 左中 → 右中」依次试。

    注意：结构体在函数内定义——本模块顶层没有 import ctypes/wintypes，
    大量函数是函数内 `import ctypes`，模块级定义结构体会 NameError。
    """
    import ctypes as _ct
    from ctypes import wintypes as _wt

    class MOUSEINPUT(_ct.Structure):
        _fields_ = [
            ("dx", _wt.LONG), ("dy", _wt.LONG),
            ("mouseData", _wt.DWORD), ("dwFlags", _wt.DWORD),
            ("time", _wt.DWORD), ("dwExtraInfo", _ct.POINTER(_ct.c_ulong)),
        ]

    class INPUT(_ct.Structure):
        _fields_ = [("type", _wt.DWORD), ("mi", MOUSEINPUT)]

    user32 = _ct.windll.user32
    try:
        rect = _wt.RECT()
        if not user32.GetWindowRect(_wt.HWND(hwnd), _ct.byref(rect)):
            return False
        left, top = rect.left, rect.top
        width, height = rect.right - rect.left, rect.bottom - rect.top
        if width <= 4 or height <= 4:
            return False
        # 纵向取标题栏下方约 1/12（避开标题栏按钮），横向取若干候选
        y_top = top + max(40, height // 12)
        y_mid = top + height // 2
        candidates = [
            (left + width // 2, y_top),
            (left + width // 8, y_top),
            (left + width - width // 8, y_top),
            (left + width // 8, y_mid),
            (left + width - width // 8, y_mid),
            (left + width // 2, y_mid),
            (left + width // 2, top + height - max(40, height // 12)),
        ]

        user32.WindowFromPoint.restype = _wt.HWND
        user32.GetAncestor.argtypes = [_wt.HWND, _wt.UINT]
        user32.GetAncestor.restype = _wt.HWND
        GA_ROOT = 2

        chosen = None
        for (x, y) in candidates:
            try:
                pt = _wt.POINT(int(x), int(y))
                hit = user32.WindowFromPoint(pt)
                if not hit:
                    continue
                root = user32.GetAncestor(_wt.HWND(hit), GA_ROOT) or hit
                if int(root) == int(hwnd):
                    chosen = (int(x), int(y))
                    break
            except Exception:
                continue
        if chosen is None:
            # 全部候选点都被遮挡：不点击，避免点到别的窗口上做错事
            return False

        user32.SetCursorPos(chosen[0], chosen[1])
        time.sleep(0.05)
        down = INPUT(0, MOUSEINPUT(0, 0, 0, 0x0002, 0, None))   # LEFTDOWN
        up = INPUT(0, MOUSEINPUT(0, 0, 0, 0x0004, 0, None))     # LEFTUP
        user32.SendInput.argtypes = [_wt.UINT, _ct.POINTER(INPUT), _ct.c_int]
        user32.SendInput.restype = _wt.UINT
        sent = user32.SendInput(1, _ct.byref(down), _ct.sizeof(INPUT))
        time.sleep(0.02)
        sent += user32.SendInput(1, _ct.byref(up), _ct.sizeof(INPUT))
        return sent == 2
    except Exception:
        return False


def _unlock_foreground(hwnd: int) -> bool:
    """绕过 Windows 前台锁（三级降级）。

    实测（2026-10-27）：内核与目标同为 High 时，前两级都「假成功」——
    返回 TRUE 但 GetForegroundWindow() 不变。第三级 SendInput 才真正生效。

    所以每一级都必须用 `_require_foreground(hwnd) is None` 校验实际结果，
    不能只看 API 返回值。这是本函数的核心教训。

    注意：本模块顶层没有 import ctypes/wintypes（各函数内部各自 import），
    所以这里必须显式导入，否则 NameError。实测踩过一次：
    `_sendinput_click_center` 单独调用成功，但经过 `_unlock_foreground`
    时在第 1 行 `wintypes.HWND(hwnd)` 就抛异常，导致 activate 永远失败。
    """
    import ctypes as _ct
    from ctypes import wintypes as _wt
    import win32con as _wc

    user32 = _ct.windll.user32
    target = _wt.HWND(hwnd)

    # 1) SwitchToThisWindow（最快，多数情况下够用）
    try:
        user32.SwitchToThisWindow(target, True)
        time.sleep(0.12)
        if _require_foreground(hwnd) is None:
            return True
    except Exception:
        pass

    # 2) 轻敲 ALT 拿前台额度 + SetForegroundWindow
    try:
        user32.keybd_event(_wc.VK_MENU, 0, 0, 0)
        user32.keybd_event(_wc.VK_MENU, 0, _wc.KEYEVENTF_KEYUP, 0)
        time.sleep(0.04)
        user32.SetForegroundWindow(target)
        time.sleep(0.12)
        if _require_foreground(hwnd) is None:
            return True
    except Exception:
        pass

    # 3) SendInput 真实点击（实测唯一能突破前台锁的方式）
    if _sendinput_click_center(hwnd):
        time.sleep(0.25)
        if _require_foreground(hwnd) is None:
            return True
    return False


@tool("确保目标窗口或同进程 owned popup 是前台；已满足时不激活，失败不发送键盘事件。type/press_key 前使用。")
def desktop_ensure_focus(window_id: str, *, tries: int = 3, settle_ms: int = 250) -> dict:
    import win32con
    hwnd, used, actual = 0, 0, 0
    detail = {"expected_hwnd": 0, "actual_hwnd": 0, "actual_exe": "", "actual_title": "", "tries_used": 0}
    try:
        info = bound_window_info(window_id)
        if not info:
            raise ValueError("window_id 不存在")
        hwnd = int(info["hwnd"])
        detail["expected_hwnd"] = hwnd
        if not 1 <= int(tries) <= 10 or not 0 <= int(settle_ms) <= 2000:
            raise ValueError("tries 必须是 1–10，settle_ms 必须是 0–2000")
        controller = _controller()
        for attempt in range(int(tries) + 1):
            if _require_foreground(hwnd) is None:
                return {"success": True, "hwnd": hwnd, "title": info.get("title", ""),
                        "tries_used": max(1, used)}
            if not win32gui.IsWindow(hwnd):
                raise ValueError("窗口已关闭或句柄失效")
            if attempt == int(tries):
                break
            used += 1
            try:
                # 裸 SetForegroundWindow 在 foreground lock 下经常无效（实测被
                # explorer/Program Manager 占前台时连试 3 次都失败）；走 controller 的
                # AttachThreadInput 路径，不另写一套抢前台逻辑。
                if controller is not None and controller._force_foreground(hwnd):
                    time.sleep(int(settle_ms) / 1000)
                    continue
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                if _unlock_foreground(hwnd):
                    time.sleep(int(settle_ms) / 1000)
                    continue
                win32gui.SetForegroundWindow(hwnd)
            except Exception as exc:
                logger.debug("focus attempt %s failed: %s", used, exc)
            time.sleep(int(settle_ms) / 1000)
    except Exception as exc:
        detail["reason"] = f"{type(exc).__name__}: {exc}"
    try:
        actual = win32gui.GetForegroundWindow()
        detail["actual_title"] = win32gui.GetWindowText(actual)
        actual_info = _window_info(actual) or {}
        detail["actual_exe"] = actual_info.get("exe", "")
    except Exception as exc:
        detail["inspection_error"] = f"{type(exc).__name__}: {exc}"
    detail.update(actual_hwnd=actual, tries_used=used)
    return {"success": False, "error": "FOCUS_FAILED", "detail": detail,
            "hint": f"前台被 {detail['actual_exe']} 占着（标题 {detail['actual_title']}）。"
                    "可先 desktop_switch_window / 关闭遮挡窗口再试。"}


@tool("向已绑定窗口粘贴文本（中文安全，最多2000字符），不清空已有草稿、不按回车，不要再补Ctrl+V。必须与切窗、提交串行执行。success只代表输入动作已调用，需核验目标输入框。非前台先尝试恢复，失败拒绝；失败后先检查已有草稿，禁止盲目重复粘贴。")
def desktop_type_text(window_id: str, text: str) -> dict:
    controller = _controller()
    if controller is None:
        return {"error": "DESKTOP_NOT_READY: AppController 尚未初始化"}
    with _STATE._lock:
        info = _STATE._windows.get(window_id)
    if info is None:
        return {"success": False, "error": f"window_id 不存在: {window_id}"}
    if not text or len(text) > 2000:
        return {"success": False, "error": "文本为空或超过 2000 字符"}
    hwnd = int(info["hwnd"])
    with controller._input_lock:
        # 回滚（2026-10-26）：不在这里强制抢前台。上午验证过的路径是
        # act()/调用方负责激活，本函数只核验前台；强制 ensure-focus 会让
        # 没有前台额度的内核把整条链卡死在 FOCUS_FAILED。
        blocked = _require_foreground(hwnd)
        if blocked:
            return blocked
        result = controller.input.type_text(text)
    result["window_id"] = window_id
    result["effect_verified"] = False
    result["note"] = "输入动作不等于送达：请核对草稿；本工具已粘贴，勿再补 Ctrl+V，勿盲目重试。"
    return result


@tool("绑定当前前台窗口（无论它属于哪个进程、是否可见）。专治 CEF/多进程应用的输入子窗：如某些应用的登录页使用独立顶层输入代理窗，与主窗无 owner/父子关系，键盘工具会拒绝向主窗键入——此时用本工具直接绑定前台的这个输入窗，再用 desktop_type_keys/desktop_press_key 操作它。返回 window_id。")
def desktop_pick_foreground(label: str = "") -> dict:
    import win32gui
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        return {"success": False, "error": "当前没有前台窗口"}
    info = _foreground_window_info(hwnd)
    if info is None:
        # 实测（2026-10-27）：占位的 IDE 进程有访问保护，查它的 pid/exe 会失败，
        # _foreground_window_info 返回 None。以前这里直接报"已销毁"，导致
        # /windows/foreground 的 hwnd 恒为 None —— 调用方无法判断"前台是不是目标"，
        # 于是 activate 被反复重试、结果永远判为失败（而实际上激活是成功的）。
        # 正确做法：信息拿不全也要把 hwnd 返回，调用方至少能比对。
        try:
            title = win32gui.GetWindowText(hwnd)[:60]
        except Exception:
            title = ""
        info = {"title": title, "exe": "", "pid": 0, "class": "", "visible": True,
                "info_partial": True}
    window_id = f"win-{uuid.uuid4().hex[:10]}"
    with _STATE._lock:
        _STATE._windows[window_id] = {
            "hwnd": hwnd,
            "title": info["title"],
            "exe": info["exe"],
            "pid": info["pid"],
            "class": info.get("class", ""),
            "visible": info.get("visible", True),
            "label": label[:60] or info["title"][:60],
            "bound_at": datetime.now().isoformat(timespec="seconds"),
        }
        if info.get("info_partial"):
            _STATE._windows[window_id]["info_partial"] = True
    entry = _STATE._windows[window_id]
    return {
        "success": True,
        "window_id": window_id,
        # hwnd 必须返回：调用方要判断"前台是不是我绑定的那个窗口"。
        # 之前刻意剔除了它，导致 /windows/foreground 的 hwnd 恒为 None，
        # "确认前台"的循环永远不成立、每次白激活 3 次（实测让 f_create_save
        # 的耗时从 43s 涨到 114s，且把菜单点没了）。评审 Q1 建议 4。
        "window": {k: v for k, v in entry.items()},
        "hint": ("已绑定当前前台窗口（它此刻真正持有键盘焦点）。"
                 "立即用 desktop_type_keys/desktop_press_key 对它键入；"
                 "若它是弹出的输入子窗，输入完成后记得切回主窗 desktop_switch_window。"),
    }


@tool("任务收尾的最后一个桌面操作：关闭本会话启动的浏览器和应用，并解绑窗口；不会关闭用户之前打开的窗口。返回 cleanup_complete 后不要重复清理。")
def desktop_cleanup(close_browser: bool = True, close_launched_apps: bool = True,
                unbind_windows: bool = True) -> dict:
    return cleanup_session_footprint(
        close_browser=close_browser,
        close_launched_apps=close_launched_apps,
        unbind_windows=unbind_windows,
    )


@tool("向已绑定窗口发送单键（enter/tab/escape/backspace/delete/home/end/pageup/pagedown/方向键）或组合键（'ctrl+a' 全选、'ctrl+c'、'ctrl+v'，用 + 拼接）。组合键只用这类常见快捷键——冷门组合（win+*/ctrl+f*/alt+方向 等）可能被客户端 hook 或 VM 下不可靠，不要用。危险组合键（ctrl+w/alt+f4/win+l/win+r）被拒绝。Enter 只允许 count=1，必须与粘贴串行并核验是否送达；其他单键支持 count 连发（如 count=20 连按 backspace 快速清空输入框）。清空输入框推荐：先 ctrl+a 全选再 delete，或 count=20 连发 backspace——不要逐字符一次一键（每键一轮对话，极慢）。零激活（键直接发给当前前台窗口）：要求目标已前台，否则拒绝。字母/数字输入用 desktop_type_keys。按键后会短暂等待（enter/tab/escape 等可能触发页面跳转，等待时间较长；方向键/翻页等待较短），再返回结果。")
def desktop_press_key(window_id: str, key: str, count: int = 1) -> dict:
    controller = _controller()
    if controller is None:
        return {"error": "DESKTOP_NOT_READY: AppController 尚未初始化"}
    with _STATE._lock:
        info = _STATE._windows.get(window_id)
    if info is None:
        return {"success": False, "error": f"window_id 不存在: {window_id}"}
    allowed = {
        "enter", "tab", "escape", "backspace", "space", "delete", "home", "end",
        "pageup", "pagedown", "up", "down", "left", "right",
    }
    # 组合键策略（对齐 external automation agent agent_actions）：格式校验 + 危险键 blocklist，
    # 不再是小白名单——LLM 写错格式（ctrl_esc / "ctrl a"）直接报错并给正确写法，
    # 防止 pyautogui 静默失败导致 agent 死循环重试。
    modifier_keys = {"ctrl", "alt", "shift", "win"}
    # 危险组合：关窗口/锁屏/运行对话框（external automation agent confirm.py 的 CONFIRM 拦截集）
    combo_blocklist = {"ctrl+w", "alt+f4", "win+l", "win+r", "ctrl+alt+delete"}
    function_keys = {f"f{i}" for i in range(1, 25)}
    key_aliases = {"control": "ctrl", "esc": "escape", "meta": "win"}
    raw = str(key or "").strip()
    # 格式 lint：下划线/空格分隔的"疑似组合键"——拦截并附正确写法
    import re as _re
    if _re.fullmatch(r"[a-z]+[\s_]+[a-z0-9]+", raw.lower()) and "+" not in raw:
        guess = "+".join(_re.split(r"[\s_]+", raw.lower()))
        return {
            "success": False,
            "error": f"疑似组合键格式错误: {raw!r} —— 组合键用 + 拼接，如 {guess!r}",
        }
    parts = [key_aliases.get(p.strip().lower(), p.strip().lower())
             for p in raw.split("+") if p.strip()]
    key_norm = "+".join(parts)
    is_combo = len(parts) > 1
    # ── 组合键白名单（用户要求：只保留少量必要组合，其余一律不允许）──────
    # 理由：组合键是"绕过界面语义"的捷径。用多了会让 agent 变成"按快捷键的脚本"，
    # 而不是"看懂界面再操作"——后者才是 CUA 的目标。
    # 只保留三类：
    #   ctrl+a/c/v  文本编辑三件套（全选/复制/粘贴），几乎没有等价的界面操作
    #   ctrl+z      撤销，失败恢复用
    #   其余一律拒绝，要求改点界面元素
    ALLOWED_COMBOS = {
        "ctrl+a", "ctrl+c", "ctrl+v", "ctrl+x", "ctrl+z",
        # ctrl+n 是"新建文档"的唯一键盘路径：记事本/编辑器菜单在 UIA 里常常
        # 不可达（Notepad++ 的菜单栏是自绘的），只能靠点工具栏首个图标或这条键。
        # 它是可逆的（新建的空文档，不写盘、不覆盖任何文件），所以放行；
        # 保存类组合（ctrl+s / ctrl+shift+s）仍然单独评估，不在这里放开。
        "ctrl+n",
    }
    if is_combo:
        if key_norm not in ALLOWED_COMBOS:
            return {
                "success": False,
                "error": (f"组合键 {key_norm!r} 不在白名单内。只允许 "
                          f"{sorted(ALLOWED_COMBOS)}；其他操作请点击界面元素，"
                          "不要用快捷键绕过界面语义。"),
                "allowed_combos": sorted(ALLOWED_COMBOS),
            }
        if count != 1:
            return {"success": False, "error": "组合键不支持 count 连发（count=1）"}
    elif key_norm not in allowed:
        return {"success": False, "error": f"仅支持按键: {sorted(allowed)} 或组合键（'ctrl+a' 等，+ 拼接）"}
    if isinstance(count, bool) or not isinstance(count, int) or count < 1 or count > 50:
        return {"success": False, "error": "count 需为 1-50 的整数"}
    if key_norm == "enter" and count != 1:
        return {"success": False, "error": "Enter 可能触发提交，禁止连发；每次 count=1 并核验结果"}
    hwnd = int(info["hwnd"])
    with controller._input_lock:
        blocked = _require_foreground(hwnd)
        if blocked:
            return {**blocked, "action_performed": False, "effect_verified": False}
        if is_combo:
            controller.input.hotkey(*parts)
        else:
            for _ in range(count):
                controller.input.press_key(key_norm, 0.15)
    # enter/tab/escape can trigger navigation or dialogs; others are cheap.
    _settle(controller, scale=1.0 if key_norm in ("enter", "tab", "escape") else 0.3)
    return {
        "success": True, "key": key_norm, "count": count, "window_id": window_id,
        "action_performed": True, "effect_verified": False,
        "note": "按键已调用，不代表提交成功或消息送达；核验目标会话后再计数，状态不明勿重发。",
    }


@tool("向当前前台窗口逐字符键入英文/数字/符号（SendInput，不激活、不切换窗口——专为输入法敏感的输入框设计，如下拉搜索框）。⚠️ 输入法必须是英文态：中文态下英文字符会被 IME 组词框截获（丢失/变拼音/混入杂字符）——键入后必须复查框内值，发现缺字或异常：点任务栏输入法图标切英文，或改用 desktop_type_text（剪贴板粘贴，不走键盘、天然绕过 IME，中文内容必须用它）。要求目标窗口已是前台（通常刚用 desktop_click 点过输入框）；若不是前台会直接报错而不乱打。")
def desktop_type_keys(window_id: str, text: str, interval: float = 0.03) -> dict:
    controller = _controller()
    if controller is None:
        return {"error": "DESKTOP_NOT_READY: AppController 尚未初始化"}
    text = str(text or "")
    if not text:
        return {"success": False, "error": "text 不能为空"}
    if len(text) > 500:
        return {"success": False, "error": "单次最多 500 字符（长文本请用 desktop_type_text 粘贴）"}
    with _STATE._lock:
        info = _STATE._windows.get(window_id)
    if info is None:
        return {"success": False, "error": f"window_id 不存在: {window_id}"}
    hwnd = int(info["hwnd"])
    with controller._input_lock:
        # Foreground check inside the input lock (see desktop_type_text note).
        blocked = _require_foreground(hwnd)
        if blocked:
            return blocked
        try:
            import pyautogui
            pyautogui.write(text, interval=max(0.0, min(float(interval), 0.3)))
        except Exception as exc:
            return {"success": False, "error": f"键入失败: {exc}"}
    return {
        "success": True,
        "chars": len(text),
        "note": "已逐字符键入，未触碰窗口焦点。若字符未出现，检查输入法（中文输入法会吞字母，先切英文）。",
    }


# ── Scoped file tools ────────────────────────────────────────────────────

@tool("在 Agent 专属工作区内新建文件（含父目录），并登记为可删除文件。返回绝对路径。")
def desktop_create_file(name: str, content: str = "") -> dict:
    if not name or any(ch in name for ch in "\\/:*?\"<>|"):
        return {"success": False, "error": "文件名非法（不允许路径分隔符或特殊字符）"}
    path = _safe_workspace_path(name)
    if path.exists():
        return {"success": False, "error": f"文件已存在: {name}，请换名或先删除"}
    if len(content.encode("utf-8")) > 2_000_000:
        return {"success": False, "error": "内容超过 2MB"}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    try:
        _LEDGER.register(path, note="desktop_create_file")
    except RuntimeError as exc:
        path.unlink(missing_ok=True)
        return {"success": False, "error": str(exc)}
    return {"success": True, "path": str(path), "size": path.stat().st_size, "deletable": True}


@tool("向本会话创建的文件追加或覆盖写入内容。文件必须仍在会话登记表中。")
def desktop_write_file(path: str, content: str, append: bool = False) -> dict:
    try:
        p = _safe_workspace_path(path)
    except PermissionError as exc:
        return {"success": False, "error": str(exc)}
    if not _LEDGER.is_mine(p):
        return {"success": False, "error": "只能写入本会话由 Agent 创建的文件"}
    if len(content.encode("utf-8")) > 2_000_000:
        return {"success": False, "error": "内容超过 2MB"}
    try:
        if append:
            with p.open("a", encoding="utf-8") as f:
                f.write(content)
        else:
            p.write_text(content, encoding="utf-8")
    except Exception as exc:
        return {"success": False, "error": str(exc)}
    return {"success": True, "path": str(p), "size": p.stat().st_size}


@tool("删除一个文件。只允许删除本会话中由 Agent 自己创建的文件（安全边界）。")
def desktop_delete_file(path: str) -> dict:
    try:
        p = _safe_workspace_path(path)
    except PermissionError as exc:
        return {"success": False, "error": str(exc)}
    try:
        remaining = _LEDGER.delete(p)
    except PermissionError as exc:
        return {"success": False, "error": str(exc)}
    except RuntimeError as exc:
        return {"success": False, "error": str(exc)}
    return {"success": True, "deleted": str(p), "remaining_tracked": remaining}


@tool("列出本会话由 Agent 创建且仍可删除的文件。")
def desktop_list_my_files() -> dict:
    files = []
    for raw, meta in _LEDGER.created.items():
        p = Path(raw)
        files.append({"path": raw, "exists": p.exists(), "size": meta.get("size", 0), "note": meta.get("note", "")})
    return {"files": files, "count": len(files)}


@tool("检查远端 OmniParser 服务配置与连通性（不发送截图）。")
def desktop_parser_status() -> dict:
    from requests import Session
    url, _token = _resolve_parser_settings()
    configured = parser_configured()
    ui_url, ui_token = _config_parser_settings()
    if ui_url and ui_token:
        token_source = "ui_config"
    elif os.getenv("PARSER_SERVICE_TOKEN"):
        token_source = "env"
    else:
        token_source = "external_agent.settings" if configured else "missing"
    result: dict[str, Any] = {
        "configured": configured,
        "url": url or None,
        "token_source": token_source,
        "hint": "可在 设置 → Desktop 服务 中修改地址与 Token（保存后立即生效）。",
    }
    if configured:
        try:
            s = Session()
            # Intranet service — bypass the system proxy (see ParserClient).
            s.trust_env = False
            s.proxies = {"http": None, "https": None}
            resp = s.get(f"{url}/health", timeout=5)
            result["reachable"] = resp.status_code == 200
            result["status_code"] = resp.status_code
        except requests.RequestException as exc:
            result["reachable"] = False
            result["error"] = exc.__class__.__name__
    return result


# ── App discovery (installed / running probe) ────────────────────────────

_LAUNCH_ROOTS = tuple(dict.fromkeys(str(Path(p).resolve()) for p in (
    os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), r"Microsoft\Windows\Start Menu"),
    os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu"),
    os.path.join(os.environ.get("PUBLIC", r"C:\Users\Public"), "Desktop"),
    os.path.join(os.environ.get("USERPROFILE", ""), "Desktop"),
) if p))


def _norm_query(name: str) -> str:
    return re.sub(r"[\s_\-（）()]+", "", (name or "").lower())


_SYSTEM_APP_DIRS = [
    os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32"),
    os.environ.get("SystemRoot", r"C:\Windows"),
]

# 3.8：lnk 校验对象从「lnk 所在目录」收紧到「解析后的 target_exe」。
# 允许标准安装位置；Downloads/Temp 等可写目录仍不允许。
_LAUNCH_TARGET_ROOTS = tuple(dict.fromkeys(str(Path(p).resolve()) for p in (
    os.environ.get("ProgramFiles", r"C:\Program Files"),
    os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    os.environ.get("ProgramData", r"C:\ProgramData"),
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"),
    *_SYSTEM_APP_DIRS,
) if p))


def _split_windows_args(args: str) -> list[str]:
    """按 Windows 规则拆分 lnk 参数（不用 shell=True，不引入注入面）。"""
    if not (args or "").strip():
        return []
    argc = ctypes.c_int()
    ctypes.windll.shell32.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
    argv = ctypes.windll.shell32.CommandLineToArgvW(args, ctypes.byref(argc))
    if not argv:
        raise OSError("CommandLineToArgvW failed")
    try:
        return [argv[i] for i in range(argc.value)]
    finally:
        ctypes.windll.kernel32.LocalFree(argv)


def _is_console_exe(path: str) -> bool:
    """读 PE Subsystem（3=console）。console 程序需要 CREATE_NEW_CONSOLE 才会有窗口。"""
    try:
        with open(path, "rb") as fh:
            fh.seek(0x3C)
            pe = int.from_bytes(fh.read(4), "little")
            fh.seek(pe + 0x5C)
            return int.from_bytes(fh.read(2), "little") == 3
    except OSError:
        return False


def _popen_launch(target_exe: str, lnk_args: str = ""):
    """CreateProcess 启动：立刻拿 pid，1.5 秒内退出则视为启动器转发/失败。

    返回 (proc, early_result)；early_result 非 None 表示进程已早退，调用方直接返回它。
    """
    import subprocess
    cmd = [target_exe] + _split_windows_args(lnk_args)
    flags = subprocess.CREATE_NEW_CONSOLE if _is_console_exe(target_exe) else 0
    # Windows may route a new cmd console into an existing Windows Terminal
    # process, obscuring ownership of its outer window. An explicit console
    # host keeps the console window in this launch's process tree.
    is_system_exe = any(target_exe.lower().startswith(d.lower() + os.sep) for d in _SYSTEM_APP_DIRS)
    if os.path.basename(target_exe).lower() == "cmd.exe" and flags and is_system_exe:
        console_host = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "conhost.exe")
        cmd.insert(0, console_host)
    proc = subprocess.Popen(cmd, close_fds=True, creationflags=flags)
    try:
        proc.wait(timeout=1.5)
        return None, {"success": False, "launch_uncertain": True, "exited_early": True,
                      "exit_code": proc.returncode, "pid": proc.pid,
                      "error": f"LAUNCH_EXITED_EARLY: 进程 1.5 秒内退出（退出码 {proc.returncode}）",
                      "hint": "可能是启动器转发或启动失败。先用 desktop_list_windows 检查；不要重复启动。"}
    except subprocess.TimeoutExpired:
        return proc, None

# Optional default target for environments that need one specific app.
# The generic release contains no product-specific paths or executable names.
def _configured_desktop_client() -> str:
    return (os.environ.get("DSH_DESKTOP_TARGET_EXE") or "").strip()

# Windows built-ins never appear in Uninstall keys nor as Start-Menu .lnk.
_KNOWN_SYSTEM_APPS = {
    "notepad": "notepad.exe", "notepad++": "notepad++.exe", "记事本": "notepad.exe",
    "calc": "calc.exe", "计算器": "calc.exe", "mspaint": "mspaint.exe", "画图": "mspaint.exe",
    "explorer": "explorer.exe", "文件资源管理器": "explorer.exe",
    "cmd": "cmd.exe", "powershell": "powershell.exe", "regedit": "regedit.exe", "注册表": "regedit.exe",
}


def _find_system_binary(name: str) -> str | None:
    """Locate a Windows built-in executable (System32-first) by simple name."""
    target = _KNOWN_SYSTEM_APPS.get((name or "").strip().lower()) or _KNOWN_SYSTEM_APPS.get((name or "").strip())
    if target is None:  # alias substring match, e.g. 注册表编辑器 → regedit
        q_norm = (name or "").strip().lower()
        for alias, exe in _KNOWN_SYSTEM_APPS.items():
            if len(alias) >= 2 and (alias in q_norm or q_norm in alias):
                target = exe
                break
    candidates = [target] if target else []
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_\-]{0,40}", (name or "").strip()):
        candidates.append(name.strip() + ".exe")
        candidates.append(name.strip())
    for cand in candidates:
        if not cand:
            continue
        for d in _SYSTEM_APP_DIRS:
            p = os.path.join(d, cand)
            if os.path.isfile(p):
                return p
    return None


def _scan_shortcuts(query: str, limit: int = 24) -> list[dict[str, str]]:
    """Find .lnk/.url shortcuts whose filename fuzzily matches the query."""
    hits: list[dict[str, str]] = []
    if not query:
        return hits
    seen: set[str] = set()
    for root in _LAUNCH_ROOTS:
        base = Path(root)
        if not base.is_dir():
            continue
        count = 0
        for path in base.rglob("*"):
            count += 1
            if count > 6000:
                break
            if path.suffix.lower() not in (".lnk", ".url"):
                continue
            if query in _norm_query(path.stem):
                resolved = str(path.resolve())
                if resolved in seen:
                    continue
                seen.add(resolved)
                hits.append({"name": path.stem, "path": resolved})
                if len(hits) >= limit:
                    return hits
    return hits


def _exe_from_display_icon(value: str) -> str:
    """DisplayIcon 形如 'C:\\path\\app.exe,0' / '"C:\\path\\app.exe",0' / 'C:\\path\\app.ico'。
    解析出可执行文件路径；不是 .exe 就返回空。绝不使用 UninstallString。"""
    v = (value or "").strip()
    if v.startswith('"') and '"' in v[1:]:
        v = v[1:v.index('"', 1)]  # 带引号路径在第二个引号处结束，引号内逗号保留
    v = re.sub(r",\s*-?\d+\s*$", "", v).strip()  # 仅去掉末尾图标索引 ,0 / ,-1
    return v if v.lower().endswith(".exe") else ""


def _scan_registry_apps(query: str, limit: int = 10) -> list[dict[str, str]]:
    """Match installed programs by DisplayName in the Uninstall registry keys.

    HKCU 单用户安装里 InstallLocation 经常为空，可执行路径常在 DisplayIcon
    （实测 WorkBuddy：InstallLocation 为空，DisplayIcon=...\\WorkBuddyAI.exe,0）。
    """
    import winreg
    hits: list[dict[str, str]] = []
    hives = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for hive, sub in hives:
        try:
            with winreg.OpenKey(hive, sub) as key:
                index = 0
                while len(hits) < limit:
                    try:
                        sub_name = winreg.EnumKey(key, index)
                    except OSError:
                        break
                    index += 1
                    try:
                        with winreg.OpenKey(key, sub_name) as item:
                            try:
                                display = str(winreg.QueryValueEx(item, "DisplayName")[0])
                            except OSError:
                                continue
                            if query in _norm_query(display):
                                try:
                                    loc = str(winreg.QueryValueEx(item, "InstallLocation")[0] or "")
                                except OSError:
                                    loc = ""
                                try:
                                    icon = str(winreg.QueryValueEx(item, "DisplayIcon")[0] or "")
                                except OSError:
                                    icon = ""
                                hits.append({"name": display[:100],
                                             "install_location": loc[:200],
                                             "display_icon": icon[:260],
                                             "exe": _exe_from_display_icon(icon)})
                    except OSError:
                        continue
        except OSError:
            continue
    return hits


# ── 危险快捷方式识别 ────────────────────────────────────────────────────
# 实测教训（2026-10-23）：用 "电脑管家" 调 desktop_find_app，
# 返回的第一个快捷方式是「卸载电脑管家.lnk」，直接启动就弹出了卸载器。
# 卸载/安装/修复类程序会改动系统，绝不能与主程序混在同一个候选列表里。
_DANGER_APP_PATTERNS = (
    "uninst", "uninstall", "卸载", "安装", "setup", "install",
    "repair", "修复", "remove", "清理工具", "重置",
)


def _is_dangerous_shortcut(path: str, name: str) -> str:
    """返回命中的危险词（空串表示安全）。"""
    hay = f"{path or ''} {name or ''}".lower()
    return next((w for w in _DANGER_APP_PATTERNS if w in hay), "")


@tool("探测某个应用是否已安装/正在运行（按名称模糊匹配：运行中进程、可见窗口、开始菜单/桌面快捷方式、注册表卸载条目）。当窗口列表里找不到目标应用时，先用本工具探测，再决定启动本地应用还是走网页版。")
def desktop_find_app(name: str) -> dict:
    if not name or not name.strip():
        return {"success": False, "error": "name 不能为空"}
    query = _norm_query(name)
    windows = [
        {"title": w["title"], "exe": w["exe"], "pid": w["pid"]}
        for w in _list_visible_windows()
        if query in _norm_query(w["title"]) or query in _norm_query(w["exe"])
    ][:10]
    running: list[str] = []
    try:
        for proc in psutil.process_iter(["name"]):
            pname = (proc.info.get("name") or "")
            if query in _norm_query(Path(pname).stem):
                running.append(pname[:80])
                if len(running) >= 8:
                    break
    except Exception:
        pass
    installed = _scan_registry_apps(query)
    shortcuts_raw = _scan_shortcuts(query)
    # 危险快捷方式（卸载/安装/修复）单独隔离，默认不出现在可启动列表里。
    safe_shortcuts: list = []
    blocked_shortcuts: list = []
    for s in shortcuts_raw:
        reason = _is_dangerous_shortcut(s.get("path", ""), s.get("name", ""))
        if reason:
            blocked_shortcuts.append({**s, "blocked_reason": reason})
        else:
            safe_shortcuts.append(s)
    shortcuts = safe_shortcuts
    system_binary = _find_system_binary(name)
    # DisplayIcon 解析出的 exe 只有落在既有启动白名单目录内才作为可启动候选；
    # 其余路径仅作信息展示，不放宽 desktop_launch_app 的安全边界，也不跳过 lnk 扫描。
    registry_launchable = [
        item["exe"] for item in installed
        if item.get("exe") and any(item["exe"].lower().startswith(d.lower() + os.sep)
                                   for d in (*_LAUNCH_ROOTS, *_SYSTEM_APP_DIRS))
    ]
    if windows or running:
        verdict = "installed_running"
    elif installed or shortcuts or system_binary:
        verdict = "installed_not_running"
    else:
        verdict = "likely_not_installed"
    hints = {
        "installed_running": "应用已在运行：直接 desktop_list_windows 找到对应窗口并 desktop_pick_window。",
        "installed_not_running": "应用已安装但未运行：可用 desktop_launch_app 打开上面返回的 shortcut_path（或系统内置 binary），再 desktop_list_windows 等待窗口出现。",
        "likely_not_installed": "本机大概率未安装：改用浏览器打开该应用的网页版（如有），或询问用户。",
    }
    return {
        "success": True,
        "query": name,
        "verdict": verdict,
        "running_processes": sorted(set(running)),
        "windows": windows,
        "installed": installed,
        "shortcuts": shortcuts,
        # 危险快捷方式（卸载/安装/修复）不参与启动，但如实告知：
        # 让 agent 明白候选为什么变少，而不是去猜或换关键词乱试。
        "blocked_shortcuts": blocked_shortcuts,
        "system_binary": system_binary,
        "registry_launchable": registry_launchable,
        "hint": hints[verdict],
    }


def _current_window_key(hwnd: int) -> str:
    """Stable identity for a live window; empty means identity is unavailable."""
    try:
        if not win32gui.IsWindow(hwnd):
            return ""
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        created = psutil.Process(pid).create_time()
        return f"{hwnd}:{pid}:{int(created * 1000)}" if created else ""
    except Exception:
        return ""


def _windows_of_launch(item: dict) -> list[tuple[int, str]]:
    """Live windows belonging to the process we launched.

    The recorded handle can die while the app keeps running (a Tk or Electron
    app maps its frame after the launcher's first probe). The window_key keeps
    "pid:create_time_ms", which survives the handle changing, so ownership can
    still be proven without ever touching another process's windows.
    """
    parts = str(item.get("window_key") or "").split(":")
    if len(parts) != 3:
        return []
    try:
        pid, created_ms = int(parts[1]), int(parts[2])
    except ValueError:
        return []
    out: list[tuple[int, str]] = []
    try:
        for win in _list_visible_windows(include_untitled=True):
            try:
                if int(win.get("pid") or 0) != pid:
                    continue
                if abs(int(round(float(win.get("process_created") or 0) * 1000)) - created_ms) > 2:
                    continue
                key = str(win.get("window_key") or "")
                if key:
                    out.append((int(win["hwnd"]), key))
            except Exception:
                continue
    except Exception:
        return []
    return out


def _launch_process_snapshot() -> dict[int, tuple[str, float]]:
    result: dict[int, tuple[str, float]] = {}
    for process in psutil.process_iter(["pid", "name", "create_time"]):
        try:
            info = process.info
            if info.get("name") and info.get("create_time"):
                result[int(info["pid"])] = (str(info["name"]).lower(), float(info["create_time"]))
        except (psutil.Error, KeyError, TypeError, ValueError):
            continue
    return result


def _window_process_ids(hwnd: int, pid: int) -> set[int]:
    """Include hosted AppX children, whose top-level frame belongs to Windows."""
    pids = {int(pid)} if pid else set()
    try:
        def collect(child, _):
            _, child_pid = win32process.GetWindowThreadProcessId(child)
            if child_pid:
                pids.add(int(child_pid))
        win32gui.EnumChildWindows(hwnd, collect, None)
    except Exception:
        pass
    return pids


@tool("启动已安装的应用，并等待可归属的新窗口；无法确认时返回 launch_uncertain，不要盲目重复启动。")
def desktop_launch_app(shortcut_path: str, *, scoped: bool = False, scope_owner: str = "") -> dict:
    p = Path(shortcut_path or "").expanduser()
    if p.suffix.lower() not in (".lnk", ".exe"):
        return {"success": False, "error": "仅允许启动 .lnk 快捷方式或系统内置 .exe"}
    try:
        resolved = str(p.resolve())
    except OSError as exc:
        return {"success": False, "error": f"路径无效: {exc}"}
    if not os.path.isfile(resolved):
        return {"success": False, "error": f"快捷方式不存在: {resolved}"}
    lowered = resolved.lower()
    if p.suffix.lower() == ".lnk" and not any(lowered.startswith(root.lower() + os.sep) for root in _LAUNCH_ROOTS):
        return {"success": False, "error": "只允许启动开始菜单/桌面目录内的快捷方式（安全边界）"}
    if p.suffix.lower() == ".exe" and not any(lowered.startswith(d.lower() + os.sep) for d in _SYSTEM_APP_DIRS):
        return {"success": False, "error": "只允许启动 Windows 系统目录内的 exe（安全边界）"}
    target_exe, lnk_args = resolved, ""
    if p.suffix.lower() == ".lnk":
        try:
            import win32com.client as _wcom
            shortcut = _wcom.Dispatch("WScript.Shell").CreateShortCut(resolved)
            target_exe = shortcut.TargetPath or ""
            lnk_args = str(shortcut.Arguments or "").strip()
        except Exception as exc:
            return {"success": False, "error": f"快捷方式解析失败: {exc}"}
        if not target_exe or not any(target_exe.lower().startswith(d.lower() + os.sep) for d in _LAUNCH_TARGET_ROOTS):
            return {"success": False, "error": f"TARGET_NOT_ALLOWED: 快捷方式目标不在允许目录: {target_exe}"}

    started = time.monotonic()
    wall_started = time.time()
    baseline = {w.get("window_key") for w in _list_visible_windows(include_untitled=True)}
    try:
        before_processes = _launch_process_snapshot()
    except Exception as exc:
        return {"success": False, "error": f"LAUNCH_BASELINE_UNAVAILABLE: {exc}"}
    basename = os.path.basename(target_exe).lower()
    aliases = {basename}
    if basename == "calc.exe":
        aliases.update({"calculatorapp.exe", "calculator.exe"})
    system_target = any(target_exe.lower().startswith(d.lower() + os.sep) for d in _SYSTEM_APP_DIRS)
    appx_stub = system_target and basename in {"calc.exe", "notepad.exe"}
    proc = None
    early = None
    method = "shell" if appx_stub else "popen"
    try:
        if appx_stub:
            # ShellExecute routes system stubs through Windows packaged-app activation.
            if lnk_args:
                os.startfile(target_exe, "open", lnk_args)
            else:
                os.startfile(target_exe)
        else:
            proc, early = _popen_launch(target_exe, lnk_args)
    except OSError as exc:
        return {"success": False, "error": f"LAUNCH_FAILED: {type(exc).__name__}: {exc}",
                "duration_ms": round((time.monotonic() - started) * 1000)}
    direct_pid = int(proc.pid) if proc is not None else int((early or {}).get("pid") or 0)
    deadline = time.monotonic() + 8.0
    ambiguous: dict[str, tuple[dict, set[int]]] = {}
    while time.monotonic() < deadline:
        current_processes = _launch_process_snapshot()
        owned_pids = set()
        direct = current_processes.get(direct_pid)
        expected_direct_names = aliases | ({"conhost.exe"} if basename == "cmd.exe" else set())
        if direct and direct[0] in expected_direct_names and direct[1] >= wall_started - 0.1:
            owned_pids.add(direct_pid)
            try:
                owned_pids.update(
                    child.pid for child in psutil.Process(direct_pid).children(recursive=True)
                    if child.pid in current_processes and current_processes[child.pid][1] >= wall_started - 0.1
                )
            except psutil.Error:
                pass
        # Packaged apps may be hosted in a shared frame. A new child process
        # with a known executable and start time is evidence for this launch.
        for pid, (name, created) in current_processes.items():
            if name in aliases and (pid not in before_processes or before_processes[pid][1] != created) and created >= wall_started - 0.1:
                owned_pids.add(pid)
        visible = _list_visible_windows(include_untitled=True)
        by_hwnd = {int(w.get("hwnd") or 0): w for w in visible}
        candidates: dict[str, tuple[dict, set[int]]] = {}
        for win in visible:
            hwnd, pid = int(win.get("hwnd") or 0), int(win.get("pid") or 0)
            if not hwnd:
                continue
            matched_pids = _window_process_ids(hwnd, pid) & owned_pids & set(current_processes)
            if not matched_pids:
                continue
            try:
                root = int(win32gui.GetAncestor(hwnd, 2) or hwnd)
                if root == hwnd and win32gui.GetClassName(hwnd) == "Windows.UI.Core.CoreWindow":
                    continue  # Wait for the AppX host frame to exist.
            except Exception:
                continue
            target = by_hwnd.get(root)
            if not target:
                continue
            key = str(target.get("window_key") or "")
            if not key or key in baseline or _current_window_key(root) != key:
                continue
            if key in candidates:
                candidates[key][1].update(matched_pids)
            else:
                candidates[key] = (target, set(matched_pids))
        # One process can expose a titled app frame plus an untitled helper
        # window (Tk and Electron both do). When the candidates all belong to a
        # single process and exactly one of them has a title, that is the frame.
        if len(candidates) > 1:
            pids = {pid for _win, pidset in candidates.values() for pid in pidset}
            titled = [(win, pidset) for win, pidset in candidates.values()
                      if (win.get("title") or "").strip()]
            if len(pids) == 1 and len(titled) == 1:
                candidates = {titled[0][0].get("window_key"): titled[0]}
        if len(candidates) == 1:
            win, matched = next(iter(candidates.values()))
            item = {k: win.get(k) for k in ("hwnd", "window_key", "title", "exe", "pid")}
            item.update(scope_owner=scope_owner, t=time.time())
            with _STATE._lock:
                _STATE._launched_windows.append(item)
            return {"success": True, "launched": resolved, "launch_method": method,
                    "tracked_windows": [item], "tracked_pid": min(matched),
                    "launch_identity": {"pid": min(matched), "create_time": current_processes[min(matched)][1]},
                    "duration_ms": round((time.monotonic() - started) * 1000),
                    "hint": "已确认新窗口和进程身份；任务结束时仅关闭登记的窗口。"}
        if len(candidates) > 1:
            # Several new windows can belong to one launch: a console host, a
            # splash screen and the real frame. Failing on the first ambiguous
            # sample made slow-starting apps unregistered (2026-10-27: a Tk app
            # mapping its window at ~2.5s never got tracked). Remember the
            # snapshot and let transient windows settle until the deadline.
            ambiguous = candidates
            time.sleep(0.2)
            continue
        time.sleep(0.2)
    if ambiguous:
        picked = sorted(ambiguous.values(),
                        key=lambda pair: (not pair[0].get("title"), pair[0].get("hwnd") or 0))
        return {"success": False, "launched": resolved, "launch_method": method,
                "launch_uncertain": True,
                "error": "LAUNCH_AMBIGUOUS: 观察到多个新窗口，无法唯一确认归属",
                "candidates": [{k: win.get(k) for k in ("hwnd", "title", "exe", "pid")}
                               for win, _pids in picked[:5]],
                "duration_ms": round((time.monotonic() - started) * 1000),
                "hint": "先看 candidates 里哪个是真正的应用窗口，再手动绑定；不要盲目重试。"}
    return {"success": False, "launched": resolved, "launch_method": method,
            "launch_uncertain": True, "error": "LAUNCH_UNCONFIRMED: 未能唯一确认本次启动的窗口",
            "duration_ms": round((time.monotonic() - started) * 1000),
            "hint": "启动请求可能已被转发或仍在加载；先检查窗口，切勿盲目重试。"}


@tool("在工作知识库中检索（BM25 召回）：kb=业务知识/控制台元素/用例逻辑，corrections=错题本，success_paths=成功路径。执行不熟悉的任务前先查一次。")
def desktop_search_kb(query: str, top_k: int = 4, source: str = "all") -> dict:
    from . import knowledge as kb
    if not query or not query.strip():
        return {"success": False, "error": "query 不能为空"}
    if source == "all":
        sources = kb.VALID_SOURCES
    elif source in kb.VALID_SOURCES:
        sources = (source,)
    else:
        return {"success": False, "error": f"source 仅支持: all/{'/'.join(kb.VALID_SOURCES)}"}
    try:
        hits = kb.search(query, sources=sources, top_k=top_k)
    except Exception as exc:
        return {"success": False, "error": f"检索失败: {exc}"}
    return {
        "success": True,
        "query": query,
        "hits": hits,
        "hint": "先读 hits 里的经验/知识再动手；如与当前场景一致，按其中的步骤顺序执行。",
    }


# ── Clipboard (DLP scenario core, ported from external_agent exec_clipboard) ─────

def _bound_hwnd(window_id: str) -> tuple[int, dict[str, Any]] | tuple[None, dict]:
    controller = _controller()
    if controller is None:
        return None, {"error": "DESKTOP_NOT_READY: AppController 尚未初始化"}
    with _STATE._lock:
        info = _STATE._windows.get(window_id)
    if info is None:
        return None, {"error": f"window_id 不存在: {window_id}"}
    return int(info["hwnd"]), {}


def window_label(window_id: str) -> str:
    """Human-readable label for a bound window id ('' when unknown).

    Used by the knowledge reviewer to turn window ids into semantic titles.
    """
    try:
        with _STATE._lock:
            info = _STATE._windows.get(window_id)
    except Exception:
        return ""
    if not info:
        return ""
    return str(info.get("label") or info.get("title") or info.get("exe") or "")


def _is_effectively_foreground(hwnd: int) -> bool:
    """True when hwnd (or a popup belonging to the same app) is foreground.

    Delegates to game.window.is_effectively_foreground — the single source of
    truth also used by WindowService.ensure_foreground (mouse/keyboard/capture
    activation gate), so this check and the actual activation-skip logic can
    never drift apart.
    """
    from .game.window import is_effectively_foreground
    return is_effectively_foreground(hwnd)


def _focus_state(hwnd: int) -> dict[str, Any] | None:
    """读目标窗口的键盘焦点状态（用于判定"点击是否真的落到了目标上"）。

    为什么需要：点击输入框/搜索框的真实效果只是「光标出现 + 焦点转移」，
    像素差异极小。实测 2026-10-27：
      点企业微信搜索框 → visual_delta=0.06（实际弹出了建议菜单）
      点聊天输入框     → visual_delta=0.22（实际光标进去了）
      输入文字         → visual_delta=0.07（实际文字进了输入框）
    纯像素判据必然报 suspected_noop，上层据此重试会造成重复输入。

    为什么用 GetGUIThreadInfo 而不是 UIA 的焦点属性：
      · `element_info.has_keyboard_focus` 属性不存在（AttributeError）
      · `element.has_keyboard_focus()` 恒返回 False（UIA provider 不报）
      · `GetGUIThreadInfo(前台线程)` 实测可靠

    注意：GetGUIThreadInfo 只对**前台线程**返回有效值（非前台线程 hwndActive
    和 hwndFocus 都是 0，这是 Windows 行为，不是 bug）。所以本函数先确认目标
    窗口是不是前台，不是就直接返回 None —— 那种情况下焦点必然在别处。

    返回：
      {"is_foreground": bool, "hwnd_active": int, "hwnd_focus": int,
       "focus_in_target": bool}
      focus_in_target = hwndActive 或 hwndFocus 属于目标窗口的子树（GA_ROOT 相等）
    或 None（拿不到前台/线程信息）
    """
    import ctypes as _ct
    from ctypes import wintypes as _wt

    class GUITHREADINFO(_ct.Structure):
        _fields_ = [
            ("cbSize", _wt.DWORD), ("flags", _wt.DWORD),
            ("hwndActive", _wt.HWND), ("hwndFocus", _wt.HWND),
            ("hwndCapture", _wt.HWND), ("hwndMenuOwner", _wt.HWND),
            ("hwndMoveSize", _wt.HWND), ("hwndCaret", _wt.HWND),
            ("rcCaret", _wt.RECT),
        ]

    try:
        import win32gui
        import win32process

        user32 = _ct.windll.user32
        fg = win32gui.GetForegroundWindow()
        if not fg:
            return None
        fg_tid = win32process.GetWindowThreadProcessId(fg)[0]
        if not fg_tid:
            return None
        user32.GetGUIThreadInfo.argtypes = [_wt.DWORD, _ct.POINTER(GUITHREADINFO)]
        user32.GetGUIThreadInfo.restype = _wt.BOOL
        gti = GUITHREADINFO()
        gti.cbSize = _ct.sizeof(GUITHREADINFO)
        if not user32.GetGUIThreadInfo(fg_tid, _ct.byref(gti)):
            return None

        def _belongs(h: int) -> bool:
            if not h:
                return False
            try:
                return int(win32gui.GetAncestor(int(h), 2)) == int(hwnd)  # GA_ROOT
            except Exception:
                return False

        active = int(gti.hwndActive or 0)
        focus = int(gti.hwndFocus or 0)
        return {
            "is_foreground": int(fg) == int(hwnd),
            "hwnd_active": active,
            "hwnd_focus": focus,
            "focus_in_target": _belongs(active) or _belongs(focus),
        }
    except Exception:
        return None


def _require_foreground(hwnd: int) -> dict | None:
    """Foreground CHECK for keyboard actions — never activates.

    Mirrors external automation agent buddy design: activate is a standalone step at the START of
    a flow (desktop_switch_window); press/type/paste actions are pure SendInput and
    must not re-activate windows (activation perturbs embedded webviews and
    could type into the wrong window anyway). Refuse with guidance instead.
    """
    import win32gui
    if _is_effectively_foreground(hwnd):
        return None
    fg_hwnd = 0
    fg_title = ""
    fg_exe = ""
    try:
        fg_hwnd = win32gui.GetForegroundWindow()
        fg_title = win32gui.GetWindowText(fg_hwnd)[:60]
        fg_exe = _window_info(fg_hwnd) or {}
        fg_exe = fg_exe.get("exe", "")
    except Exception:
        pass
    return {
        "success": False,
        "error": (
            "目标窗口不是前台窗口（键入会打到别的窗口）。"
            "三条路任选：① desktop_click 点一下目标输入框（真实点击会激活+放置光标）；"
            "② desktop_switch_window 激活绑定窗；"
            "③ 若前台窗口就是你想输入的子窗（常见于 CEF/多进程应用，如 desktop 登录窗 "
            "DESKTOP_LOGIN_Monitor 属于另一进程、与主窗无亲缘关系），"
            "直接 desktop_pick_foreground 绑定它再键入。"
        ),
        "foreground_title": fg_title,
        "foreground_hwnd": fg_hwnd,
        "foreground_exe": fg_exe,
    }


def _set_clipboard_text_raw(text: str) -> None:
    import win32clipboard
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, text)
    finally:
        win32clipboard.CloseClipboard()


@tool("把文本放入系统剪贴板（只放不粘贴）。用于跨应用复制流程的第一步，或让目标应用自己点粘贴按钮。")
def desktop_set_clipboard_text(text: str) -> dict:
    if not isinstance(text, str) or len(text) > 100_000:
        return {"success": False, "error": "text 为空或超过 100000 字符"}
    try:
        _set_clipboard_text_raw(text)
    except Exception as exc:
        return {"success": False, "error": f"写入剪贴板失败: {exc}"}
    return {"success": True, "chars": len(text), "preview": text[:80]}


@tool("读取系统剪贴板文本。用于验证复制结果（如复制后确认内容、跨窗口取词）。")
def desktop_get_clipboard_text() -> dict:
    import win32clipboard
    try:
        win32clipboard.OpenClipboard()
        try:
            if not win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                return {"success": True, "text": None, "note": "剪贴板当前没有文本"}
            data = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()
    except Exception as exc:
        return {"success": False, "error": f"读取剪贴板失败: {exc}"}
    return {"success": True, "text": (data or "")[:5000], "chars": len(data or "")}


@tool("把一个或多个文件/文件夹写入剪贴板（CF_HDROP，等价于资源管理器里复制文件），并立即粘贴到已绑定窗口（内置 Ctrl+V）。发送文件到聊天/邮件输入框的标准做法。零激活：要求目标窗口已前台（否则拒绝，先 desktop_switch_window）。粘贴后不要再按 Ctrl+V。")
def desktop_paste_clipboard_file(window_id: str, path: str = "", paths: list[str] | None = None) -> dict:
    import struct
    import win32clipboard

    raw_paths = ([path] if path else []) + list(paths or [])
    if not raw_paths:
        return {"success": False, "error": "需要提供 path 或 paths"}
    if len(raw_paths) > 20:
        return {"success": False, "error": "一次最多 20 个文件"}
    files: list[str] = []
    for raw in raw_paths[:20]:
        p = Path(str(raw).strip().strip('"'))
        try:
            resolved = str(p.expanduser().resolve())
        except OSError as exc:
            return {"success": False, "error": f"路径无效: {raw} ({exc})"}
        if not os.path.exists(resolved):
            return {"success": False, "error": f"文件/文件夹不存在: {resolved}"}
        files.append(resolved)

    hwnd, err = _bound_hwnd(window_id)
    if hwnd is None:
        return err
    controller = _controller()

    header = struct.pack("<IiiII", 20, 0, 0, 0, 1)  # DROPFILES(pFiles=20, fWide=1)
    payload = header + ("\0".join(files) + "\0\0").encode("utf-16le")
    try:
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32clipboard.CF_HDROP, payload)
            drop_effect = win32clipboard.RegisterClipboardFormat("Preferred DropEffect")
            win32clipboard.SetClipboardData(drop_effect, struct.pack("<I", 1))  # copy
        finally:
            win32clipboard.CloseClipboard()
    except Exception as exc:
        return {"success": False, "error": f"写入剪贴板失败: {exc}"}

    with controller._input_lock:
        # Foreground check inside the input lock (see desktop_type_text note).
        blocked = _require_foreground(hwnd)
        if blocked:
            return blocked
        time.sleep(0.25)
        controller.input.hotkey("ctrl", "v")
    return {
        "success": True,
        "pasted": files,
        "window_id": window_id,
        "hint": "已自动粘贴。不要重复 Ctrl+V；随后 re-analyze 确认文件出现在输入区。",
    }


# ── Mouse extras (right click / swipe / scroll, window-scoped) ───────────

@tool("在已绑定窗口内按 0-1000 归一化坐标右键单击（弹出上下文菜单用）。点击后会等待片刻让菜单动画渲染完成再返回。")
def desktop_right_click(window_id: str, x: int, y: int) -> dict:
    controller = _controller()
    if controller is None:
        return {"error": "DESKTOP_NOT_READY: AppController 尚未初始化"}
    with _STATE._lock:
        info = _STATE._windows.get(window_id)
    if info is None:
        return {"success": False, "error": f"window_id 不存在: {window_id}"}
    if not 0 <= x <= 1000 or not 0 <= y <= 1000:
        return {"success": False, "error": "坐标必须在 0-1000"}
    hwnd = int(info["hwnd"])
    region = controller.window.get_client_region(hwnd)
    if region is None:
        return {"success": False, "error": "窗口客户区不可用"}
    abs_x = region.left + int(x / 1000 * region.width)
    abs_y = region.top + int(y / 1000 * region.height)
    from pynput.mouse import Button
    with controller._input_lock:
        controller._force_foreground(hwnd)
        time.sleep(0.15)
        mouse = controller.input.mouse
        mouse.position = (abs_x, abs_y)
        time.sleep(0.05)
        mouse.press(Button.right)
        mouse.release(Button.right)
    _settle(controller)  # context menu popup/animation needs time to render
    return {"success": True, "window_id": window_id, "pos": [x, y]}


@tool("在已绑定窗口内拖拽/滑动（swipe）：从 (x1,y1) 按住拖到 (x2,y2)，0-1000 归一化坐标，自然轨迹模拟。用于滑块、拖拽上传、选区等。拖拽后会等待片刻让结果渲染完成再返回。")
def desktop_swipe(window_id: str, x1: int, y1: int, x2: int, y2: int, speed: float = 1.0) -> dict:
    controller = _controller()
    if controller is None:
        return {"error": "DESKTOP_NOT_READY: AppController 尚未初始化"}
    with _STATE._lock:
        info = _STATE._windows.get(window_id)
    if info is None:
        return {"success": False, "error": f"window_id 不存在: {window_id}"}
    coords = (x1, y1, x2, y2)
    if any(not 0 <= v <= 1000 for v in coords):
        return {"success": False, "error": "坐标必须在 0-1000"}
    hwnd = int(info["hwnd"])
    region = controller.window.get_client_region(hwnd)
    if region is None:
        return {"success": False, "error": "窗口客户区不可用"}
    with controller._input_lock:
        controller._force_foreground(hwnd)
        ax1 = region.left + int(x1 / 1000 * region.width)
        ay1 = region.top + int(y1 / 1000 * region.height)
        ax2 = region.left + int(x2 / 1000 * region.width)
        ay2 = region.top + int(y2 / 1000 * region.height)
        controller.input.swipe_screen((ax1, ay1), (ax2, ay2), speed=float(min(max(speed, 0.2), 5.0)))
    _settle(controller)
    return {"success": True, "window_id": window_id, "from": [x1, y1], "to": [x2, y2]}


@tool("在屏幕绝对像素坐标间拖拽，可跨越窗口（例如把桌面文件拖进应用、桌面图标整理）。"
      "与 desktop_swipe 的区别：不绑定窗口、不做 0-1000 归一化，直接用屏幕像素，"
      "所以起点可以在桌面或另一个应用里。走自然贝塞尔轨迹并带抖动，结束后回中。")
def desktop_drag_screen(x1: int, y1: int, x2: int, y2: int, speed: float = 1.0) -> dict:
    controller = _controller()
    if controller is None:
        return {"error": "DESKTOP_NOT_READY: AppController 尚未初始化"}
    for name, value in (("x1", x1), ("y1", y1), ("x2", x2), ("y2", y2)):
        if not isinstance(value, int) or value < 0 or value > 20000:
            return {"success": False, "error": f"{name} 必须是屏幕像素整数"}
    # Long drags were taking distance/200 seconds (2500px -> ~13s). For file
    # drags the human-like slowdown buys nothing, so the default target is ~2s.
    distance = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
    speed = float(speed)
    if speed <= 1.0:
        speed = max(1.0, min(5.0, distance / 400.0))
    with controller._input_lock:
        controller.input.swipe_screen((x1, y1), (x2, y2), speed=float(min(max(speed, 0.2), 5.0)))
    _settle(controller)
    return {"success": True, "from": [x1, y1], "to": [x2, y2],
            "coordinate_space": "screen_pixels",
            "note": "拖拽已执行；请截图确认目标是否收到文件或发生了预期变化。"}


@tool("把已绑定窗口设为持续置顶（TOPMOST）或恢复普通层级。窗口被其他窗口遮挡、"
      "而下一步要点它时先置顶；用完必须恢复，否则会一直压住别的窗口。")
def desktop_topmost(window_id: str, on: bool = True) -> dict:
    controller = _controller()
    if controller is None:
        return {"error": "DESKTOP_NOT_READY: AppController 尚未初始化"}
    with _STATE._lock:
        info = _STATE._windows.get(window_id)
    if info is None:
        return {"success": False, "error": f"window_id 不存在: {window_id}"}
    hwnd = int(info["hwnd"])
    try:
        set_topmost(hwnd, bool(on))
    except Exception as exc:  # noqa: BLE001 - reported to the caller
        return {"success": False, "error": f"TOPMOST_FAILED: {exc.__class__.__name__}"}
    return {"success": True, "window_id": window_id, "hwnd": hwnd, "topmost": bool(on)}


@tool("内部用：对已绑定窗口取一次视觉指纹（不激活、不改变焦点），"
      "用于判断上一个动作到底有没有让界面发生变化。返回 48x48 灰度签名——"
      "比 8x8 更能抓住局部变化（一个按钮、一行文字），又不会被整屏平均掉。")
def desktop_effect_probe(window_id: str) -> dict:
    controller = _controller()
    if controller is None:
        return {"error": "DESKTOP_NOT_READY: AppController 尚未初始化"}
    with _STATE._lock:
        info = _STATE._windows.get(window_id)
    if info is None:
        return {"success": False, "error": f"window_id 不存在: {window_id}"}
    hwnd = int(info["hwnd"])
    try:
        frame = controller.capture.capture(hwnd, activate=False)
    except Exception as exc:  # noqa: BLE001 - reported to the caller
        return {"success": False, "error": f"CAPTURE_FAILED: {exc.__class__.__name__}"}
    if frame is None or getattr(frame, "size", 0) == 0:
        return {"success": False, "error": "CAPTURE_EMPTY"}
    try:
        small = cv2.resize(frame, (48, 48), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": f"FINGERPRINT_FAILED: {exc.__class__.__name__}"}
    return {"success": True, "signature": [int(v) for v in gray.reshape(-1)], "grid": 48}


def _control_state(ctrl) -> dict:
    """Read a UIA control's observable state without touching it."""
    state: dict = {"found": True}
    try:
        state["name"] = str(ctrl.window_text() or "")[:120]
    except Exception:  # noqa: BLE001
        state["name"] = ""
    try:
        state["type"] = str(getattr(ctrl.element_info, "control_type", ""))
    except Exception:  # noqa: BLE001
        state["type"] = ""
    for field, getter in (
        ("selected", lambda: bool(ctrl.is_selected())),
        ("enabled", lambda: bool(ctrl.is_enabled())),
        ("value", lambda: str(ctrl.get_value())[:200]),
        ("toggle", lambda: int(ctrl.get_toggle_state())),
    ):
        try:
            state[field] = getter()
        except Exception:  # noqa: BLE001 - pattern simply unsupported
            continue
    try:
        rect = ctrl.rectangle()
        state["rect"] = [rect.left, rect.top, rect.right, rect.bottom]
    except Exception:  # noqa: BLE001
        pass
    return state


@tool("读取某个控件当前的 UIA 状态（选中/展开/开关/可用/文本），用于动作之后的读回验证："
      "像素没变化时，用控件状态判断动作到底生效没有。name 支持包含匹配。")
def desktop_uia_state(window_id: str, name: str = "") -> dict:
    controller = _controller()
    if controller is None:
        return {"error": "DESKTOP_NOT_READY: AppController 尚未初始化"}
    with _STATE._lock:
        info = _STATE._windows.get(window_id)
    if info is None:
        return {"success": False, "error": f"window_id 不存在: {window_id}"}
    try:
        from pywinauto import Desktop
        window = Desktop(backend="uia").window(handle=int(info["hwnd"])).wrapper_object()
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": f"UIA 连接失败: {exc.__class__.__name__}"}
    wanted = (name or "").strip().lower()
    best = None
    best_key = None
    try:
        for ctrl in window.descendants():
            try:
                label = (ctrl.window_text() or "").strip()
            except Exception:  # noqa: BLE001
                continue
            # Containers (Document/Pane) expose the whole page text as their name;
            # they would swallow every substring query, so skip the long ones.
            if not label or len(label) > 200:
                continue
            low = label.lower()
            if wanted:
                if low == wanted:
                    score = 0
                elif low.startswith(wanted):
                    score = 1
                elif wanted in low:
                    score = 2
                else:
                    continue
            else:
                score = 3
            key = (score, len(label))
            if best_key is None or key < best_key:
                best, best_key = ctrl, key
                if score == 0:
                    break
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": f"UIA 遍历失败: {exc.__class__.__name__}"}
    if best is None:
        return {"success": True, "window_id": window_id, "control": {"found": False}}
    return {"success": True, "window_id": window_id, "control": _control_state(best)}


@tool("构建当前窗口的元素表（UIA 优先；UIA 稀疏时回落 OCR 并与 UIA 合并），供本地决策层选元素用。"
      "返回 [{id, text, type, bbox}]：type 只有 UIA 档才有。caption=true 时会给图标加语义标注，"
      "这让没有名字的图标按钮（关闭/最小化这类）也能按名字被选中。")
def desktop_element_table(window_id: str, source: str = "auto", fresh: bool = False,
                      caption: bool = False) -> dict:
    from .decide import PLACEHOLDERS  # same placeholder policy as the benchmark

    controller = _controller()
    if controller is None:
        return {"error": "DESKTOP_NOT_READY: AppController 尚未初始化"}
    with _STATE._lock:
        info = _STATE._windows.get(window_id)
    if info is None:
        return {"success": False, "error": f"window_id 不存在: {window_id}"}

    def valid(text: str, box) -> bool:
        text = str(text or "").strip()
        if len(text) < 2 or text.lower() in PLACEHOLDERS:
            return False
        if not isinstance(box, (list, tuple)) or len(box) != 4:
            return False
        area = max(0, box[2] - box[0]) * max(0, box[3] - box[1])
        return 0 < area <= 400_000

    def from_uia() -> list[dict]:
        try:
            tree = _desktop_uia_tree_once(window_id, max_depth=9, max_nodes=400)
        except Exception:  # noqa: BLE001
            return []
        rows = []
        for control in tree.get("controls", []):
            text, box = control.get("name"), control.get("bbox")
            if not valid(text, box):
                continue
            rows.append({"id": len(rows), "text": str(text)[:80],
                         "type": str(control.get("control_type") or ""), "bbox": [int(v) for v in box]})
        return rows

    def from_ocr() -> list[dict]:
        analysis = desktop_analyze(window_id, caption=caption, annotate=False)
        rows = []
        for element in analysis.get("ui_elements", []):
            raw_text = str(element.get("text") or "").strip()
            box = element.get("bbox")
            caption_text = str(element.get("caption") or "").strip()
            is_icon = bool(caption_text) and caption_text not in ("未知", "unknown")
            # An icon's own text is the placeholder "icon"; the caption is what makes
            # a nameless close/minimise button addressable by name. Validate the text
            # we will actually index, not the placeholder.
            text = caption_text if is_icon else raw_text
            if not valid(text, box):
                continue
            row = {"id": len(rows), "text": text[:80], "type": "",
                   "bbox": [int(v) for v in box], "from": str(element.get("source") or "")}
            if is_icon:
                row["icon"] = True
            rows.append(row)
        return rows

    wanted = (source or "auto").strip().lower()
    if wanted not in ("auto", "uia", "ocr"):
        return {"success": False, "error": "source 仅支持 auto/uia/ocr"}

    # OCR costs a full analyze (~5s). Repeated decisions on the same window reuse
    # the table for a short window; a stale table is worse than a slow one, so the
    # TTL stays small and every action-ish call can pass fresh=True.
    now = time.monotonic()
    cached = _TABLE_CACHE.get((window_id, wanted))
    if cached and 0 <= now - cached[0] < _TABLE_TTL_SECONDS and not fresh:
        return {"success": True, "window_id": window_id, "source": cached[2],
                "count": len(cached[1]), "elements": cached[1], "cached": True,
                "age_ms": int((now - cached[0]) * 1000)}

    rows: list[dict] = []
    used = "uia"
    if wanted in ("auto", "uia"):
        rows = from_uia()
        used = "uia"
    # 5 was too low: an app that exposes only its nav tabs gave 9 UIA elements and
    # silently dropped every card label that OCR could see. 15 keeps WorkBuddy-class
    # windows on UIA while pushing thin-a11y apps to OCR. Captioned icons are the
    # point of a merge, so caption=True forces it.
    if wanted == "ocr" or (wanted == "auto" and (len(rows) < 15 or caption)):
        ocr_rows = from_ocr()
        if wanted == "auto" and rows:
            # Keep UIA rows (they carry control types) and add OCR labels only where
            # UIA had nothing at that spot.
            known = {row["text"].strip().lower() for row in rows}
            for row in ocr_rows:
                if row["text"].strip().lower() not in known:
                    rows.append({**row, "id": len(rows), "from": "ocr"})
            used = "uia+ocr"
        else:
            rows = ocr_rows
            used = "ocr"
    _TABLE_CACHE[(window_id, wanted)] = (now, rows, used)
    return {"success": True, "window_id": window_id, "source": used, "count": len(rows),
            "elements": rows, "cached": False}


@tool("等窗口画面稳定下来（连续 quiet_ms 没有明显变化）再返回，用来替代固定睡眠："
      "网页/Electron 渲染完就能立刻继续，流式输出时会一直等。纯像素判断，对 Qt、自绘界面同样有效；"
      "只做本地探测（每次约 50ms），不消耗模型 token。用均值 + 变化格子数双判据，"
      "闪烁光标不算变化，局部加载动画算变化。")
def desktop_wait_stable(window_id: str, quiet_ms: int = 600, timeout_ms: int = 20000,
                    threshold: float = 1.2, max_changed_cells: int = 10,
                    interval_ms: int = 150) -> dict:
    controller = _controller()
    if controller is None:
        return {"error": "DESKTOP_NOT_READY: AppController 尚未初始化"}
    with _STATE._lock:
        info = _STATE._windows.get(window_id)
    if info is None:
        return {"success": False, "error": f"window_id 不存在: {window_id}"}
    quiet_ms = max(200, min(5000, int(quiet_ms)))
    timeout_ms = max(1000, min(120_000, int(timeout_ms)))
    threshold = max(0.1, min(50.0, float(threshold)))
    max_changed_cells = max(0, min(1000, int(max_changed_cells)))
    interval_ms = max(50, min(1000, int(interval_ms)))
    hwnd = int(info["hwnd"])
    restored = _restore_if_minimized(hwnd)
    # 48x48 keeps small local animations visible instead of averaging them away.
    grid = 48

    start = time.monotonic()
    previous = None
    quiet_since = None
    probes = 0
    max_delta = 0.0
    max_cells = 0
    last_delta = 0.0
    last_cells = 0
    while True:
        now = time.monotonic()
        elapsed_ms = (now - start) * 1000
        if elapsed_ms > timeout_ms:
            return {"success": True, "stable": False, "timed_out": True, "probes": probes,
                    "waited_ms": int(elapsed_ms), "max_delta": round(max_delta, 2),
                    "max_changed_cells": max_cells, "restored": restored,
                    "note": "超过等待上限画面仍在变化：可能还在加载或持续输出，可以先看一眼再决定。"}
        try:
            frame = controller.capture.capture(hwnd, activate=False)
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"CAPTURE_FAILED: {exc.__class__.__name__}"}
        if frame is None or getattr(frame, "size", 0) == 0:
            return {"success": False, "error": "CAPTURE_EMPTY"}
        small = cv2.resize(frame, (grid, grid), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32)
        probes += 1
        if previous is None:
            previous = gray
            quiet_since = now
        else:
            diff = np.abs(gray - previous)
            last_delta = float(diff.mean())
            last_cells = int((diff > 15).sum())
            max_delta = max(max_delta, last_delta)
            max_cells = max(max_cells, last_cells)
            previous = gray
            still = last_delta <= threshold and last_cells <= max_changed_cells
            if still:
                if quiet_since is None:
                    quiet_since = now
                elif (now - quiet_since) * 1000 >= quiet_ms:
                    return {"success": True, "stable": True, "probes": probes,
                            "waited_ms": int((now - start) * 1000),
                            "quiet_for_ms": int((now - quiet_since) * 1000),
                            "last_delta": round(last_delta, 2),
                            "last_changed_cells": last_cells,
                            "max_delta": round(max_delta, 2),
                            "max_changed_cells": max_cells,
                            "restored": restored}
            else:
                quiet_since = None
        time.sleep(interval_ms / 1000)


def _print_window_frame(hwnd: int):
    """Render a window into a bitmap with PrintWindow (works while covered)."""
    import ctypes
    import win32gui
    import win32ui

    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        return None
    window_dc = win32gui.GetWindowDC(hwnd)
    try:
        source = win32ui.CreateDCFromHandle(window_dc)
        memory = source.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        try:
            bitmap.CreateCompatibleBitmap(source, width, height)
            memory.SelectObject(bitmap)
            # PW_RENDERFULLCONTENT (2) asks DWM-composited windows to render too.
            ctypes.windll.user32.PrintWindow(hwnd, memory.GetSafeHdc(), 2)
            info = bitmap.GetInfo()
            bits = bitmap.GetBitmapBits(True)
            frame = np.frombuffer(bits, dtype=np.uint8).reshape(
                info["bmHeight"], info["bmWidth"], 4)
            return frame[:, :, :3].copy()
        finally:
            win32gui.DeleteObject(bitmap.GetHandle())
            memory.DeleteDC()
            source.DeleteDC()
    finally:
        win32gui.ReleaseDC(hwnd, window_dc)


@tool("抓取已绑定窗口自身的画面（PrintWindow）：窗口被别的窗口挡住时，仍能拿到目标窗口的内容。"
      "若返回全黑（DirectComposition / 最小化），自动回落到屏幕截图，并把 occluded 标为 true。")
def desktop_capture_window(window_id: str) -> dict:
    from datetime import datetime
    from pathlib import Path

    controller = _controller()
    if controller is None:
        return {"error": "DESKTOP_NOT_READY: AppController 尚未初始化"}
    with _STATE._lock:
        info = _STATE._windows.get(window_id)
    if info is None:
        return {"success": False, "error": f"window_id 不存在: {window_id}"}
    hwnd = int(info["hwnd"])
    restored = _restore_if_minimized(hwnd)

    frame = None
    method = "printwindow"
    try:
        frame = _print_window_frame(hwnd)
    except Exception:  # noqa: BLE001 - fall back to the screen path
        frame = None
    occluded = False
    if frame is None or int(frame.max()) <= 8:
        try:
            frame = controller.capture.capture(hwnd, activate=False)
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"CAPTURE_FAILED: {exc.__class__.__name__}"}
        method = "screen"
        occluded = True
    if frame is None or getattr(frame, "size", 0) == 0:
        return {"success": False, "error": "CAPTURE_EMPTY"}

    root = Path(getattr(controller.config.workspace, "screenshot_dir", "") or ".")
    date_dir = root / datetime.now().strftime("%Y-%m-%d")
    try:
        date_dir.mkdir(parents=True, exist_ok=True)
        path = date_dir / f"capture_{hwnd}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.png"
        cv2.imwrite(str(path), frame)
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": f"SAVE_FAILED: {exc.__class__.__name__}"}
    height, width = frame.shape[:2]
    return {"success": True, "window_id": window_id, "image_path": str(path),
            "width": int(width), "height": int(height),
            "captured_with": method, "occluded": occluded, "restored": restored,
            "note": ("PrintWindow 拿到了窗口自己的画面，与是否被遮挡无关。"
                     if method == "printwindow" else
                     "PrintWindow 失败（全黑），这是屏幕截图，可能包含遮挡物。")}


@tool("在已绑定窗口内滚动滚轮：先移动到 (x,y)（0-1000 归一化），clicks>0 向上(vertical)/向右(horizontal)、<0 向下/向左。目标窗口不在前台时拒绝，不派发滚轮。")
def desktop_scroll(window_id: str, x: int, y: int, clicks: int = 3, direction: str = "vertical") -> dict:
    controller = _controller()
    if controller is None:
        return {"error": "DESKTOP_NOT_READY: AppController 尚未初始化"}
    with _STATE._lock:
        info = _STATE._windows.get(window_id)
    if info is None:
        return {"success": False, "error": f"window_id 不存在: {window_id}"}
    if not 0 <= x <= 1000 or not 0 <= y <= 1000:
        return {"success": False, "error": "坐标必须在 0-1000"}
    if direction not in ("vertical", "horizontal"):
        return {"success": False, "error": "direction 仅支持 vertical / horizontal"}
    if abs(int(clicks)) > 30:
        return {"success": False, "error": "单次滚动最多 30 格"}
    hwnd = int(info["hwnd"])
    with controller._input_lock:
        # S1：激活后必须核验前台；核验不通过就拒绝，绝不把滚轮发给别的窗口。
        if not _is_effectively_foreground(hwnd):
            controller._force_foreground(hwnd)
        if not _is_effectively_foreground(hwnd):
            return {"success": False, "error": "SCROLL_TARGET_MISMATCH: 目标窗口不在前台，未派发滚轮",
                    "hint": "先 desktop_switch_window / 解除遮挡；其他窗口的内容不应被滚动。"}
        # S2：激活/最小化恢复后客户区可能已变化，必须在此之后重新取坐标。
        region = controller.window.get_client_region(hwnd)
        if region is None:
            return {"success": False, "error": "窗口客户区不可用"}
        abs_x = region.left + int(x / 1000 * region.width)
        abs_y = region.top + int(y / 1000 * region.height)
        result = controller.input.scroll(abs_x, abs_y, int(clicks), direction)
    _settle(controller, scale=0.4)  # lazy-loaded lists may need a moment to render
    # scroll() echoes ABSOLUTE coords under "x"/"y" — same normalization as
    # desktop_click: echo the input coords, move absolutes to screen_*.
    if isinstance(result, dict) and "x" in result:
        result["screen_x"], result["screen_y"] = result.pop("x"), result.pop("y")
        result["x"], result["y"] = int(x), int(y)
    result["window_id"] = window_id
    result["direction"] = direction
    return result


# ── UIA control tree (pywinauto, coordinates aligned with desktop_click) ─────

_UIA_MESSAGE_LIKE_TYPES = {"text", "listitem", "dataitem", "document"}
_UIA_SEMANTIC_TYPES = _UIA_MESSAGE_LIKE_TYPES | {
    "button", "edit", "menuitem", "tabitem", "checkbox", "combobox",
    "treeitem", "hyperlink", "radiobutton",
}


def _uia_coverage(controls: list[dict[str, Any]]) -> dict[str, int]:
    named = [node for node in controls if str(node.get("name") or "").strip()]
    semantic = [
        node for node in named
        if str(node.get("control_type") or "").casefold() in _UIA_SEMANTIC_TYPES
    ]
    message_like = [
        node for node in named
        if str(node.get("control_type") or "").casefold() in _UIA_MESSAGE_LIKE_TYPES
    ]
    return {
        "control_count": len(controls),
        "named_control_count": len(named),
        "named_semantic_control_count": len(semantic),
        # This is an accessibility-tree candidate count, not proof of visible
        # messages or successful delivery; virtualized/custom surfaces vary.
        "message_like_candidate_count": len(message_like),
    }


def _uia_profile_key(info: dict[str, Any], hwnd: int) -> tuple[str, str]:
    exe = os.path.basename(str(info.get("exe") or "")).casefold()
    window_class = str(info.get("class") or "").casefold()
    if not window_class:
        try:
            window_class = str(win32gui.GetClassName(hwnd) or "").casefold()
        except Exception:
            window_class = ""
    return exe, window_class


def _record_uia_capability(
    key: tuple[str, str], coverage: dict[str, int], truncated: bool,
) -> str:
    if not key[0]:
        return "unknown"
    profiles = getattr(_STATE, "_uia_capabilities", None)

    if profiles is None:
        profiles = {}
        _STATE._uia_capabilities = profiles
    now = time.time()
    with _STATE._lock:
        for old_key, value in list(profiles.items()):
            if now - float(value.get("updated_at", 0)) > 24 * 60 * 60:
                profiles.pop(old_key, None)
        profile = profiles.setdefault(key, {
            "status": "unknown", "sparse_samples": 0, "updated_at": now,
        })
        profile["updated_at"] = now
        if not truncated:
            is_sparse = (
                coverage["named_semantic_control_count"] < 4
                and coverage["message_like_candidate_count"] == 0
            )
            if is_sparse:
                profile["sparse_samples"] = int(profile.get("sparse_samples", 0)) + 1
                if profile["sparse_samples"] >= 2:
                    profile["status"] = "sparse"
            else:
                profile["sparse_samples"] = 0
                profile["status"] = "rich"
        if len(profiles) > 64:
            oldest = min(profiles, key=lambda item: profiles[item].get("updated_at", 0))
            profiles.pop(oldest, None)
        return str(profile.get("status", "unknown"))


_BROWSER_EXES = {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe",
                 "opera.exe", "vivaldi.exe"}


def _ensure_profile_foreground(hwnd: int, tries: int = 2) -> dict:
    """profile 专用：确认窗口前台；抢不到就如实返回，不返回骨架数据。"""
    controller = _controller()
    for attempt in range(max(1, tries)):
        if _is_effectively_foreground(hwnd):
            return {"success": True}
        if attempt and controller is not None:
            try:
                controller._force_foreground(hwnd)
            except Exception:  # noqa: BLE001 - 激活失败由最终前台判定体现
                pass
        time.sleep(0.2)
    if _is_effectively_foreground(hwnd):
        return {"success": True}
    fg = win32gui.GetForegroundWindow()
    info = _window_info(fg) or {}
    return {"success": False, "foreground": {"hwnd": fg,
                                             "exe": info.get("exe", ""),
                                             "title": info.get("title", "")}}


@tool("只读应用画像：一次 UIA 扫描推导窗口模型、文本/定位/输入通道与关闭方式，供调用方选择通道。"
      "不跑 OCR、不自动切换策略、不按应用名写死判定。")
def desktop_app_profile(window_id: str) -> dict:
    info = bound_window_info(window_id)
    if not info:
        return {"success": False, "error": f"window_id 不存在: {window_id}"}
    hwnd = int(info["hwnd"])
    # 坑 2（2026-10-26 实测）：Chromium/Electron 窗口不在前台时 UIA 只返回骨架
    # （CodeBuddy 前台 300 控件 → 非前台 2 个），用骨架数据判型会把 uia_rich
    # 误判成 unknown/browser。这里先确认前台；抢不到就明确拒绝，绝不猜。
    started = time.monotonic()
    focus = _ensure_profile_foreground(hwnd)
    if not focus["success"]:
        return {"success": False, "error": "NOT_FOREGROUND",
                "window_id": window_id, "hwnd": hwnd,
                "foreground": focus["foreground"],
                "hint": "窗口不在前台，UIA 只会返回骨架，拒绝据此判型。"
                        "请先让目标窗口到前台（或由调用方激活）后重试；不要用骨架数据做策略选择。",
                "scan_ms": round((time.monotonic() - started) * 1000)}
    tree = _desktop_uia_tree_once(window_id, max_depth=8, max_nodes=600)
    scan_ms = round((time.monotonic() - started) * 1000)
    if not tree.get("success"):
        return {"success": False, "error": tree.get("error", "UIA 扫描失败"),
                "scan_ms": scan_ms}
    controls = tree.get("controls") or []
    coverage = tree.get("uia_coverage") or _uia_coverage(controls)
    total = max(1, coverage.get("control_count", len(controls)))
    # 已确认前台仍只读到 ≤2 个控件 = 骨架（实测 CodeBuddy 非 Unrestricted 完整性时
    # 即使前台也只暴露 Window+Pane）。骨架不足以判型，明确拒绝而不是给 unknown。
    if coverage.get("control_count", len(controls)) <= 2:
        return {"success": False, "error": "UIA_SKELETON",
                "window_id": window_id, "hwnd": hwnd,
                "evidence": {"uia_control_count": coverage.get("control_count", len(controls)),
                             "uia_named_count": coverage.get("named_control_count", 0),
                             "scan_ms": scan_ms},
                "hint": "窗口已在前台但 UIA 只返回骨架（可能完整性级别/权限导致 provider 不可读）。"
                        "不要据此判型；改用截图+OCR/YOLO 通道完成任务。"}
    named = coverage.get("named_control_count", 0)
    ratio = round(named / total, 2)
    value_count = sum(1 for c in controls if str(c.get("value") or "").strip())
    edit_count = sum(1 for c in controls if str(c.get("control_type") or "") == "Edit")
    bbox_usable = sum(1 for c in controls
                      if isinstance(c.get("bbox"), list) and len(c["bbox"]) == 4
                      and c["bbox"][2] > c["bbox"][0] and c["bbox"][3] > c["bbox"][1])
    # 手册规则：sparse 由「控件数≤5 且命名率<0.5」触发；语义控件少但控件不少的
    # 情况应落到 unknown（例如 10 个控件只有 1 个有名），不能一律判 sparse。
    sparse = total <= 5 and ratio < 0.5
    exe = PureWindowsPath(str(info.get("exe") or ""))
    window_class = str(win32gui.GetClassName(int(info["hwnd"])) or "") if win32gui.IsWindow(int(info["hwnd"])) else ""
    # 只按 exe 判定浏览器：Electron 应用（CodeBuddy 等）也用 Chrome_WidgetWin_1，
    # 按窗口类判定会把编辑器误报成浏览器。
    is_browser = exe.name.casefold() in _BROWSER_EXES
    pid = int(info.get("pid") or 0)
    try:
        pid = win32process.GetWindowThreadProcessId(int(info["hwnd"]))[1]
        same_pid = sum(1 for w in _list_visible_windows()
                       if w.get("pid") == pid and w.get("hwnd") != int(info["hwnd"]))
    except Exception:
        same_pid = 0
    if self_uwp := str(info.get("exe") or "").casefold().endswith("applicationframehost.exe"):
        relation, close_hint = "host+content", "click_close_button"
    else:
        relation, close_hint = ("1:N" if same_pid else "1:1"), "wm_close"

    if is_browser:
        model, text_c, pos_c, input_c = "browser", "ocr_required", "ocr_bbox", "ocr_coord"
        advice = "浏览器窗口：优先用 Playwright 的 web_* 通道（web_read/web_click），不要用 UIA 逐控件点击。"
    elif sparse or (total <= 5 and ratio < 0.5):
        model, text_c, pos_c, input_c = "uia_sparse", "ocr_required", "ocr_bbox", "ocr_coord"
        advice = "UIA 控件极少且无可用价值：定位与文本都走 OCR，输入需坐标点击后键入；不要用 uia 批量动作。"
    elif ratio >= 0.7 and value_count == 0 and edit_count == 0:
        model, text_c, pos_c, input_c = "uia_named_only", "ocr_required", "uia_bbox", "ocr_coord"
        advice = "UIA 有名字和 bbox 但没有 value/Edit：点击用 UIA bbox，读文本走 OCR。"
    elif ratio >= 0.7 and (value_count >= 1 or edit_count >= 1):
        model, text_c, pos_c, input_c = "uia_rich", "value_pattern", "uia_bbox", "uia_edit"
        advice = "UIA 完整：定位、读值、输入都走 UIA；可直接用 uia 批量动作。"
    else:
        model, text_c, pos_c, input_c = "unknown", "ocr_required", "ocr_bbox", "ocr_coord"
        advice = "UIA 覆盖不足以下结论：先试 OCR 定位与读取，再按结果收紧策略。"
    return {
        "success": True,
        "window_model": model,
        "text_channel": text_c,
        "position_channel": pos_c,
        "input_channel": input_c,
        "process_window_relation": relation,
        "close_hint": close_hint,
        "evidence": {
            "exe": str(info.get("exe") or ""), "class": window_class,
            "window_pid": pid,
            "uia_control_count": coverage.get("control_count", len(controls)),
            "uia_named_count": named,
            "uia_named_ratio": ratio,
            "uia_bbox_usable_count": bbox_usable,
            "uia_value_count": value_count,
            "uia_edit_count": edit_count,
            "uia_capability": "sparse" if sparse else "rich",
            "scan_ms": scan_ms,
        },
        "advice": advice,
    }


def _uia_target_matches(target_hint: str, controls: list[dict[str, Any]]) -> bool:
    query = _norm_query(target_hint)
    if not query:
        return False
    for node in controls:
        name = _norm_query(str(node.get("name") or ""))
        auto_id = _norm_query(str(node.get("auto_id") or ""))
        # Single-character targets must match a whole field. Short Chinese labels
        # and numeric values are common UIA names, not unreliable OCR fragments.
        if query in (name, auto_id):
            return True
        if len(query) >= 2 and (query in name or query in auto_id):
            return True
        if len(query) >= 3 and any(len(label) >= 4 and label in query for label in (name, auto_id)):
            return True
    return False


# Control types that accept an action: click, type, toggle, select or expand.
# Deliberately excludes containers (Pane/Group/Custom/Window/TitleBar) and
# decoration (Image/Separator/ScrollBar), so `only_actionable=true` cuts a
# 236-control Explorer tree down to the ~60 things that take an action.
_UIA_ACTIONABLE_TYPES = frozenset({
    "button", "splitbutton", "edit", "document", "menuitem", "tabitem",
    "checkbox", "radiobutton", "combobox", "listitem", "treeitem", "hyperlink",
    "slider", "spinner", "calendar", "dataitem", "headeritem", "thumb",
})


def _uia_compact_control(node: dict[str, Any]) -> dict[str, Any]:
    """One control reduced to what an action needs: identity, type, click point.

    Measured on a 236-control Explorer window: 132 B/control before, ~45 after.
    The dropped fields are derivable (`index` is array order, `name_length` follows
    from `name`, `bbox` center is what click uses) or almost always empty
    (`auto_id` was blank on 220 of 236 nodes, `*_truncated` is false on nearly all).

    The point is named `click_x`/`click_y`, not bare `x`/`y`: the latter was read as
    screenshot pixels and normalized a second time in live use.
    """

    out: dict[str, Any] = {}
    name = str(node.get("name") or "")
    if name:
        out["name"] = name
    out["type"] = str(node.get("control_type") or "")
    box = node.get("bbox")
    if isinstance(box, (list, tuple)) and len(box) == 4:
        out["click_x"] = (int(box[0]) + int(box[2])) // 2
        out["click_y"] = (int(box[1]) + int(box[3])) // 2
    elif "x" in node and "y" in node:
        out["click_x"], out["click_y"] = node["x"], node["y"]
    if node.get("auto_id"):
        out["auto_id"] = node["auto_id"]
    value = node.get("value")
    if value:
        out["value"] = value
    # Flags only when true: `false` repeated on every control is pure overhead.
    if node.get("name_truncated"):
        out["name_truncated"] = True
    if node.get("value_truncated"):
        out["value_truncated"] = True
    return out


def _uia_parse_types(control_types: str) -> list[str]:
    return [part.strip().casefold() for part in re.split(r"[,;\s]+", str(control_types or "")) if part.strip()]


def _uia_filter_controls(
    controls: list[dict[str, Any]],
    *,
    control_types: str = "",
    text_contains: str = "",
    only_actionable: bool = False,
    limit: int = 0,
) -> dict[str, Any]:
    """Narrow a walked tree before it reaches the model.

    Filtering is a payload decision, not a collection one: the tree is still walked
    whole, so a filter cannot hide a control that a deeper `max_depth` would find.
    `scanned` reports what the walk saw; `matched` what survived the filter before
    `limit` paged it.
    """

    wanted_types = _uia_parse_types(control_types)
    needle = str(text_contains or "").casefold()
    kept: list[dict[str, Any]] = []
    for node in controls:
        control_type = str(node.get("control_type") or "").casefold()
        if wanted_types and control_type not in wanted_types:
            continue
        if only_actionable and control_type not in _UIA_ACTIONABLE_TYPES:
            continue
        if needle:
            haystack = f"{node.get('name') or ''}\n{node.get('value') or ''}".casefold()
            if needle not in haystack:
                continue
        kept.append(node)
    matched = len(kept)
    if limit and limit > 0:
        kept = kept[:limit]
    return {
        "controls": kept,
        "matched": matched,
        "scanned": len(controls),
        "filter": {
            "control_types": wanted_types,
            "text_contains": text_contains or "",
            "only_actionable": bool(only_actionable),
            "limit": int(limit or 0),
            "matched": matched,
            "scanned": len(controls),
        },
    }


def _uia_walk(

    element, window_rect: tuple[int, int, int, int], depth: int, max_depth: int,
    nodes: list[dict[str, Any]], budget: int, truncation: dict[str, bool],
) -> None:
    if depth > max_depth:
        truncation["max_depth"] = True
        return
    if len(nodes) >= budget:
        truncation["max_nodes"] = True
        return

    try:
        info = element.element_info
        rect = info.rectangle
    except Exception:
        return
    if rect.right <= window_rect[0] or rect.bottom <= window_rect[1]:
        return  # off-screen
    left, top, right, bottom = window_rect
    w = max(right - left, 1)
    h = max(bottom - top, 1)
    nx1 = max(0, min(1000, round((rect.left - left) / w * 1000)))
    ny1 = max(0, min(1000, round((rect.top - top) / h * 1000)))
    nx2 = max(0, min(1000, round((rect.right - left) / w * 1000)))
    ny2 = max(0, min(1000, round((rect.bottom - top) / h * 1000)))
    if nx2 - nx1 >= 2 and ny2 - ny1 >= 2:
        # name 不能截到 80：编辑区/文本框的 name 就是内容本身，
        # 截断会让"文本是否送达"永远读不到尾部（实测：输入 290 字符，
        # 读到的永远是前 80 个，用 in 判断尾部标记必然误判失败）。
        # 改成保留完整值 + 显式标记，让调用方自己决定要不要截断展示。
        full_name = str(info.name or "")
        node = {
            "index": len(nodes),
            "control_type": str(info.control_type or "")[:30],
            "name": full_name[:2000],
            "name_truncated": len(full_name) > 2000,
            "name_length": len(full_name),
            "auto_id": str(info.automation_id or "")[:60],
            "bbox": [nx1, ny1, nx2, ny2],
        }
        # 编辑类控件必须读 ValuePattern，不能读 Name（评审 Q3 建议 5）。
        # 实测：系统记事本输入 'MARK2-193742' 后
        #   Name='文本编辑器'（内容为空时）或 Name=内容（非空时）
        #   iface_value.CurrentValue 恒等于真实正文 —— 这才是可信的文本来源。
        # 之前只读 Name，导致"输入是否送达"永远判不出来（一律 suspected_noop）。
        ctype = str(info.control_type or "")
        if ctype in ("Edit", "Document"):
            try:
                val = element.iface_value.CurrentValue
                if val is not None:
                    sval = str(val)
                    node["value"] = sval[:4000]
                    node["value_length"] = len(sval)
                    node["value_truncated"] = len(sval) > 4000
            except Exception:
                pass
        nodes.append(node)
    try:
        children = element.children()
    except Exception:
        return
    if depth >= max_depth:
        if children:
            truncation["max_depth"] = True
            if len(nodes) >= budget:
                truncation["max_nodes"] = True
        return

    for child in children:
        if len(nodes) >= budget:
            truncation["max_nodes"] = True
            return
        _uia_walk(child, window_rect, depth + 1, max_depth, nodes, budget, truncation)
        if truncation["max_nodes"]:
            return



def _desktop_uia_tree_once(window_id: str, max_depth: int = 7, max_nodes: int = 300) -> dict:

    controller = _controller()
    if controller is None:
        return {"error": "DESKTOP_NOT_READY: AppController 尚未初始化"}
    with _STATE._lock:
        info = _STATE._windows.get(window_id)
    if info is None:
        return {"success": False, "error": f"window_id 不存在: {window_id}"}
    hwnd = int(info["hwnd"])
    if not controller.window.is_valid(hwnd):
        return {"success": False, "error": f"窗口句柄已失效: hwnd={hwnd}"}
    max_depth = max(2, min(int(max_depth), 10))
    # 上限从 300 提到 1500：实测记事本这类带工具栏的窗口，300 个节点
    # 会在遍历到菜单项之前就用完预算，导致"菜单明明展开着，UIA 却只有 3 个
    # MenuItem（文件/编辑/查看）"。这是评审指出的 max_nodes 钳制问题。
    max_nodes = max(20, min(int(max_nodes), 1500))
    # collect-type: lazy Electron UIA needs foreground, but skip when a
    # same-app popup already effectively has it (don't collapse it)
    if not _is_effectively_foreground(hwnd):
        controller._force_foreground(hwnd)
    time.sleep(0.15)
    try:
        from pywinauto import Desktop
        # Normalize against the CLIENT region — the same basis as
        # desktop_click/capture (get_client_region). GetWindowRect includes the
        # title bar + borders, which systematically shifted every bbox by
        # (~8px, ~31px) relative to what desktop_click expects (review #3).
        client = controller.window.get_client_region(hwnd)
        if client is None:
            return {"success": False, "error": "窗口客户区不可用（窗口可能已最小化）"}
        ref_rect = (client.left, client.top, client.left + client.width, client.top + client.height)
        root = Desktop(backend="uia").window(handle=hwnd).wrapper_object()
    except Exception as exc:
        return {"success": False, "error": f"UIA 连接失败: {exc.__class__.__name__}: {exc}"}
    nodes: list[dict[str, Any]] = []
    truncation = {"max_depth": False, "max_nodes": False}
    wakeup_attempt = 0
    while True:
        nodes.clear()
        truncation = {"max_depth": False, "max_nodes": False}
        try:
            _uia_walk(root, ref_rect, 0, max_depth, nodes, max_nodes, truncation)
        except Exception as exc:
            if not nodes:
                return {"success": False, "error": f"UIA 遍历失败: {exc.__class__.__name__}"}
        coverage = _uia_coverage(nodes)
        # Chromium/Electron switches its accessibility tree on in response to the
        # first UIA client (WM_GETOBJECT): the first walk sees a hollow shell and
        # the next one sees the real tree. Measured on an Electron app:
        # 10 nodes / 0 named (910ms) -> 138 nodes / 99 named (701ms).
        hollow = coverage["control_count"] < 15 or coverage["named_semantic_control_count"] == 0
        if not hollow or wakeup_attempt >= 1:
            break
        wakeup_attempt += 1
        time.sleep(0.25)
    if not nodes:
        return {"success": False, "error": "UIA_EMPTY: 无法读取控件（窗口可能已关闭或 UIA provider 不可用）",
                "hint": "确认窗口状态；未将空遍历记录为 sparse 能力。"}
    truncation_reasons = [name for name, reached in truncation.items() if reached]
    truncated = bool(truncation_reasons)
    coverage = _uia_coverage(nodes)
    capability = _record_uia_capability(
        _uia_profile_key(info, hwnd), coverage, truncated,
    )
    return {
        "success": True,
        "window_id": window_id,
        "title": info.get("title", ""),
        "truncated": truncated,
        "truncation_reasons": truncation_reasons,
        "max_depth": max_depth,
        "max_nodes": max_nodes,
        "uia_coverage": coverage,
        "uia_capability": capability,
        "a11y_wakeup_retries": wakeup_attempt,
        "controls": nodes,
        "hint": (
            "控件树因深度/节点上限被截断；提供 target_hint 并设置 max_attempts=3 可渐进扩展；"

            "如果树完整但目标缺失，或 uia_capability=sparse，应转用 desktop_analyze。"
            if truncated or capability == "sparse" else
            "用 index 对应的 name/auto_id 判断语义；compact 模式点击用 click_x/click_y，"
            "完整模式用 bbox 中心走 desktop_click（同坐标系）。"
        ),
    }


@tool(
    "读取已绑定窗口的 UIA 控件树；报告截断原因和内容覆盖摘要。可提供 target_hint 启用目标匹配，"
    "最多三次渐进扩深（每次+5）/扩节点；完整树未找到目标、消息区域 UIA 覆盖稀疏或仍被截断时，"
    "在同一次工具调用内回退到 OmniParser。target_kind 可选 control、message、visual。"
    "compact=true 时每个控件只留 name/type/click_x/click_y（有值才带 value/auto_id），体积约为完整字段的三分之一；"
    "control_types（如 Button,Edit）、text_contains、only_actionable 用于在返回前过滤。"
)
def desktop_uia_tree(
    window_id: str,
    max_depth: int = 7,
    max_nodes: int = 300,

    target_hint: str = "",
    target_kind: str = "control",
    max_attempts: int = 3,
    compact: bool = False,
    control_types: str = "",
    text_contains: str = "",
    only_actionable: bool = False,
) -> dict:

    target_kind = str(target_kind or "control").strip().lower()

    if target_kind not in {"control", "message", "visual"}:
        return {"success": False, "error": "target_kind 必须是 control、message 或 visual"}
    if target_kind == "visual":
        analysis = desktop_analyze(window_id, caption=False, annotate=False)
        return {
            "success": "error" not in analysis,
            "method": "omniparser",
            "reason": "visual_target_requested",
            "analysis": analysis,
        }

    try:
        depth = max(2, min(int(max_depth), 10))
        # 600 is the ceiling because a compact control is ~60-80 B: the whole of a
        # 236-control Explorer window now costs about what 120 full records did, and
        # an escalated attempt can double the budget without becoming unreadable.
        node_limit = max(20, min(int(max_nodes), 600))
        attempt_limit = max(1, min(int(max_attempts), 3))
        if not target_hint:
            attempt_limit = 1
    except (TypeError, ValueError):
        return {"success": False, "error": "max_depth、max_nodes、max_attempts 必须是整数"}

    with _STATE._lock:

        info = _STATE._windows.get(window_id)
    if info is None:
        return {"success": False, "error": f"window_id 不存在: {window_id}"}
    hwnd = int(info["hwnd"])
    profile_key = _uia_profile_key(info, hwnd)
    known_profile = getattr(_STATE, "_uia_capabilities", {}).get(profile_key, {})
    if target_kind == "message" and known_profile.get("status") == "sparse":
        analysis = desktop_analyze(window_id, caption=False, annotate=False)
        return {
            "success": "error" not in analysis,
            "method": "omniparser",
            "reason": "known_sparse_uia_content_surface",
            "uia_attempts": [],
            "uia_capability": "sparse",
            "analysis": analysis,
        }

    attempts: list[dict[str, Any]] = []
    last_tree: dict[str, Any] | None = None
    target_found = False
    filtering = bool(_uia_parse_types(control_types) or text_contains or only_actionable)
    for _attempt in range(attempt_limit):
        # A filter is applied after the walk, so the walk needs headroom: with
        # only_actionable on a 236-control window the first 120 nodes are mostly
        # panes, and a budget of 120 would return a handful of buttons and pretend
        # the rest do not exist.
        scan_limit = min(max(node_limit * 8, 600), 4000) if filtering else node_limit
        tree = _desktop_uia_tree_once(window_id, max_depth=depth, max_nodes=scan_limit)
        last_tree = tree
        if not tree.get("success"):
            attempts.append({"error": tree.get("error", "UIA_FAILED")})
            break

        controls = tree.get("controls") or []
        target_found = bool(target_hint) and _uia_target_matches(target_hint, controls)
        attempts.append({
            "max_depth": tree.get("max_depth", depth),
            "max_nodes": tree.get("max_nodes", node_limit),
            "control_count": len(controls),
            "truncated": bool(tree.get("truncated")),
            "truncation_reasons": tree.get("truncation_reasons", []),
            "uia_coverage": tree.get("uia_coverage", _uia_coverage(controls)),
            "target_found": target_found,
        })
        if target_found or not tree.get("truncated"):
            break

        reasons = set(tree.get("truncation_reasons") or [])
        next_depth = min(depth + 5, 10) if "max_depth" in reasons else depth
        next_nodes = min(max(node_limit * 2, node_limit + 20), 600) if "max_nodes" in reasons else node_limit
        if next_depth == depth and next_nodes == node_limit:
            break
        depth, node_limit = next_depth, next_nodes

    if last_tree is None:
        return {"success": False, "error": "UIA 探查未产生结果"}
    if last_tree.get("success"):
        coverage = last_tree.get("uia_coverage") or _uia_coverage(last_tree.get("controls") or [])
        sparse_content = target_kind == "message" and coverage.get("message_like_candidate_count", 0) == 0
        needs_visual_fallback = bool(target_hint and not target_found) or sparse_content

    else:
        coverage = {}
        needs_visual_fallback = True

    if target_found or not needs_visual_fallback:
        tree = last_tree or {}
        selection = _uia_filter_controls(
            tree.get("controls") or [],
            control_types=control_types,
            text_contains=text_contains,
            only_actionable=only_actionable,
            limit=node_limit,
        )
        public = selection["controls"]
        out = {
            **tree,
            "method": "uia",
            "target_found": target_found,
            "uia_attempts": attempts,

            "uia_coverage": coverage,
            "control_count": len(public),
            "uia_filter": selection["filter"],
            "click_space": "window",
            "click_unit": "normalized_0_1000",
        }
        if compact:
            # The model only ever reads the compact form; `bbox` is kept on the
            # internal tree because desktop_element_table and goal= positioning
            # index it. Compacting in place instead of here silently emptied the
            # element table once already.
            compacted = [_uia_compact_control(node) for node in public]
            out["controls"] = compacted
            out["fields"] = "name/type/click_x/click_y, plus value or auto_id when present"
            out["payload_bytes"] = len(json.dumps(compacted, ensure_ascii=False, separators=(",", ":")))
            out["payload_bytes_full"] = len(json.dumps(public, ensure_ascii=False, separators=(",", ":")))
            out["hint"] = ("每个控件只有 name/type/click_x/click_y（有值时才带 value/auto_id）："
                           "click_x/click_y 已是可直接传给 desktop_click 的窗口归一化点击点，"
                           "不要除以图片宽高，也不要按 bbox 自己算中心。"
                           "控件多时用 control_types / text_contains / only_actionable 收窄。")
        else:
            out["controls"] = public
        return out

    analysis = desktop_analyze(window_id, caption=False, annotate=False)
    return {
        "success": "error" not in analysis,
        "method": "uia+omniparser" if last_tree.get("success") else "omniparser",
        "reason": (
            "uia_content_surface_sparse"
            if target_kind == "message" and not coverage.get("message_like_candidate_count", 0)
            else "uia_target_not_found"
        ),
        "uia_attempts": attempts,
        "uia_capability": last_tree.get("uia_capability", "unknown"),
        "uia_coverage": coverage,
        "analysis": analysis,
    }



@tool("用 UIA 语义直接操作控件：不动鼠标、不抢焦点，窗口被遮挡或最小化也能用。"
      "按 name（可加 control_type）定位，自动选择 Invoke / Toggle / SetValue / Select / Expand。"
      "value 用于输入框；action=auto|invoke|toggle|set|select|expand。")
def desktop_uia_invoke(window_id: str, name: str = "", control_type: str = "", value: str = "",
                   action: str = "auto", automation_id: str = "") -> dict:
    controller = _controller()
    if controller is None:
        return {"error": "DESKTOP_NOT_READY: AppController 尚未初始化"}
    with _STATE._lock:
        info = _STATE._windows.get(window_id)
    if info is None:
        return {"success": False, "error": f"window_id 不存在: {window_id}"}
    name = (name or "").strip()
    automation_id = (automation_id or "").strip()
    if not name and not automation_id:
        return {"success": False, "error": "name 与 automation_id 至少要给一个"}
    wanted_type = (control_type or "").strip().lower()
    hwnd = int(info["hwnd"])
    try:
        from pywinauto import Desktop
        window = Desktop(backend="uia").window(handle=hwnd).wrapper_object()
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": f"UIA 连接失败: {exc.__class__.__name__}"}

    def describe(ctrl) -> dict:
        try:
            ci = ctrl.element_info
            rect = ci.rectangle
            return {"name": str(ci.name or ""), "automation_id": str(ci.automation_id or ""),
                    "control_type": str(ci.control_type or ""),
                    "rect": [rect.left, rect.top, rect.right, rect.bottom]}
        except Exception:  # noqa: BLE001
            return {"name": "", "automation_id": "", "control_type": "", "rect": None}

    exact: list = []
    fuzzy: list = []
    try:
        for ctrl in window.descendants():
            try:
                label = (ctrl.window_text() or "").strip()
                ctype = str(getattr(ctrl.element_info, "control_type", "") or "").lower()
                cid = str(getattr(ctrl.element_info, "automation_id", "") or "").strip()
            except Exception:
                continue
            if wanted_type and ctype != wanted_type:
                continue
            # Exact identity only: a fuzzy hit is never executed, because
            # "保存" must not resolve to "保存并提交".
            id_ok = bool(automation_id) and cid == automation_id
            name_ok = bool(name) and label == name
            if automation_id and not name:
                if id_ok:
                    exact.append(ctrl)
                continue
            if id_ok and (not name or name_ok):
                exact.append(ctrl)
                continue
            if name_ok and not automation_id:
                exact.append(ctrl)
                continue
            if not automation_id and name and (name.lower() in label.lower()):
                fuzzy.append(ctrl)
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": f"UIA 遍历失败: {exc.__class__.__name__}"}

    if not exact:
        payload = {"success": False, "error": "UIA_TARGET_NOT_FOUND",
                   "queried": {"name": name, "automation_id": automation_id,
                               "control_type": control_type},
                   "escalation": {"recommended": "analyze", "reason": "no_exact_match"}}
        if fuzzy:
            payload["candidates"] = [describe(c) for c in fuzzy[:5]]
            payload["hint"] = "以下只是模糊候选，未被点击；请用完整 name 或 automation_id 精确指定。"
        return payload
    if len(exact) > 1:
        return {"success": False, "error": "UIA_TARGET_AMBIGUOUS",
                "queried": {"name": name, "automation_id": automation_id,
                            "control_type": control_type},
                "candidates": [describe(c) for c in exact[:8]],
                "hint": "命中多个控件，未执行任何动作；请用 automation_id 或更精确的 name 消歧。"}

    best = exact[0]
    matched = describe(best)

    used = None
    attempts: list[str] = []

    def attempt(pattern: str, call) -> None:
        nonlocal used
        if used is not None or action not in ("auto", pattern):
            return
        try:
            call()
            used = pattern
        except Exception as exc:  # noqa: BLE001 - pattern may simply be unsupported
            attempts.append(f"{pattern}:{exc.__class__.__name__}")

    if value:
        attempt("set", lambda: best.iface_value.SetValue(value))
    attempt("invoke", lambda: best.iface_invoke.Invoke())
    attempt("toggle", lambda: best.iface_toggle.Toggle())
    attempt("select", lambda: best.iface_selection_item.Select())
    attempt("expand", lambda: best.iface_expand_collapse.Expand())

    if used is None:
        return {"success": False, "error": "UIA_PATTERN_UNAVAILABLE", "attempts": attempts,
                "control": matched,
                "escalation": {"recommended": "pixel", "reason": "no_supported_uia_pattern"}}
    time.sleep(0.12)
    return {"success": True, "window_id": window_id, "action_used": used,
            "control": matched,
            "attempts": attempts,
            "note": "UIA 语义调用已提交给控件本身，没有移动鼠标。"}


# ── Registration ─────────────────────────────────────────────────────────

# A batch builds its element table from one scan and then reuses the wrappers.
# UIA trees are not stable: entering a digit in Calculator turns clearButton
# into clearEntryButton, and lists rebuild their rows on scroll. Acting on a
# stale wrapper either throws deep inside pywinauto or, worse, clicks the
# cached bbox after the layout moved and hits a different control. So every
# step re-validates identity and geometry, and a mismatch triggers one rescan.
_UI_BATCH_BBOX_TOLERANCE = 8  # normalized 0-1000 units


class _StaleElementError(RuntimeError):
    """Raised when a cached element no longer matches the live UIA tree."""


class _BatchTargetMissing(ValueError):
    """Raised when a step target is absent or ambiguous in the current table."""


def _uia_box_from_rect(rect, region):
    return [round((rect.left - region.left) / region.width * 1000),
            round((rect.top - region.top) / region.height * 1000),
            round((rect.right - region.left) / region.width * 1000),
            round((rect.bottom - region.top) / region.height * 1000)]


def _uia_live_check(element, region, action):
    """Confirm the cached element is still the same control in the same place."""
    wrapper = element["wrapper"]
    # A destroyed UIA element raises from any property read; there is no
    # wrapper.exists() on UIAWrapper (that is a WindowSpecification method).
    try:
        info = wrapper.element_info
        name = str(info.name or "")
        auto_id = str(info.automation_id or "")
        control_type = str(info.control_type or "")
        rect = info.rectangle
    except Exception as exc:  # noqa: BLE001 - 控件销毁时 pywinauto 抛的异常类型不稳定
        raise _StaleElementError(f"ELEMENT_GONE: {type(exc).__name__}: {exc}") from exc
    if (auto_id != element["auto_id"] or name != element["name"]
            or control_type != element["control_type"]):
        raise _StaleElementError(
            f"ELEMENT_REPLACED: 控件标识已变化（{element['auto_id']!r}/{element['name']!r}"
            f" → {auto_id!r}/{name!r}）")
    if action == "click":
        box = _uia_box_from_rect(rect, region)
        drift = max(abs(a - b) for a, b in zip(box, element["bbox"]))
        if drift > _UI_BATCH_BBOX_TOLERANCE:
            raise _StaleElementError(f"ELEMENT_MOVED: 控件位移 {drift}（归一化单位），缓存坐标不可信")


def _uia_batch_elements(root, region, max_depth, max_nodes, deadline):
    pending, elements = [(root, 0)], []
    while pending and len(elements) < max_nodes:
        if time.monotonic() >= deadline:
            raise TimeoutError("UIA_BATCH_TIMEOUT: 扫描超时")
        element, depth = pending.pop()
        info, rect = element.element_info, element.element_info.rectangle
        if rect.width() > 0 and rect.height() > 0:
            box = [round((rect.left - region.left) / region.width * 1000),
                   round((rect.top - region.top) / region.height * 1000),
                   round((rect.right - region.left) / region.width * 1000),
                   round((rect.bottom - region.top) / region.height * 1000)]
            elements.append({"name": str(info.name or ""), "auto_id": str(info.automation_id or ""),
                             "control_type": str(info.control_type or ""), "bbox": box,
                             "wrapper": element})
        if depth < max_depth:
            pending.extend((child, depth + 1) for child in reversed(element.children()))
    return elements


def _uia_batch_step(controller, hwnd, region, table, step, remaining):
    from pywinauto.uia_defines import NoPatternInterfaceError
    action = step.get("action")
    if action == "wait":
        ms = int(step["ms"])
        if ms < 0 or ms / 1000 > remaining:
            raise ValueError("wait 超过剩余时间或为负数")
        time.sleep(ms / 1000)
        return "wait"
    if action not in ("click", "type"):
        raise ValueError("action 必须是 click、type 或 wait")
    key = "auto_id" if step.get("auto_id") else "name"
    matches = table.get((key, step.get(key, "")), [])
    if len(matches) != 1:
        raise _BatchTargetMissing("TARGET_NOT_UNIQUE: 控件不存在或不唯一")
    element, pattern = matches[0], None
    wrapper = element["wrapper"]
    _uia_live_check(element, region, action)
    if not wrapper.is_enabled():
        raise ValueError("控件不可用")
    if action == "type":
        text = step.get("text")
        if not isinstance(text, str) or len(text) > 2000:
            raise ValueError("text 必须是最多 2000 字符的字符串")
        try:
            pattern = wrapper.iface_value
        except NoPatternInterfaceError:
            pattern = None
        if pattern is not None:
            pattern.SetValue(text)
            return "uia_value"
        wrapper.set_focus()
        blocked = _require_foreground(hwnd)
        if blocked:
            raise ValueError(str(blocked["error"]))
        sent = controller.input.type_text(text)
        method = "paste"
    else:
        if element["control_type"] != "Button":
            try:
                pattern = wrapper.iface_invoke
            except NoPatternInterfaceError:
                pattern = None
        if pattern is not None:
            pattern.Invoke()
            return "uia_click"
        if controller.window.get_client_region(hwnd) != region:
            raise ValueError("窗口位置/大小已改变，元素表失效")
        box = element["bbox"]
        x, y = round((box[0] + box[2]) / 2), round((box[1] + box[3]) / 2)
        ok, guard = _click_target_ok(hwnd, x, y)
        if not 0 <= x <= 1000 or not 0 <= y <= 1000 or not ok or guard.get("error") or guard.get("skipped"):
            raise ValueError(f"CLICK_TARGET_MISMATCH: {guard}")
        sent = controller.input.click_normalized(hwnd, x, y, activate=False)
        method = "coord_click"
    if not sent.get("success"):
        raise ValueError(str(sent.get("error")))
    return method


@tool("仅向已绑定窗口发送 WM_CLOSE 并等待关闭；不结束进程、不强关、无兜底。")
def desktop_close_window_soft(window_id: str, timeout_s: float = 8.0) -> dict:
    import win32con
    controller = _controller()
    if controller is None:
        return {"success": False, "error": "DESKTOP_NOT_READY"}
    info = bound_window_info(window_id)
    if not info:
        return {"success": False, "error": f"window_id 不存在: {window_id}"}
    hwnd = int(info["hwnd"])
    if not win32gui.IsWindow(hwnd):
        return {"success": True, "closed": True, "already_closed": True, "hwnd": hwnd}
    try:
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
    except Exception as exc:  # noqa: BLE001 - 明确回报系统拒绝，不回落强杀
        return {"success": False, "error": f"WM_CLOSE_FAILED: {type(exc).__name__}: {exc}", "hwnd": hwnd}
    deadline = time.monotonic() + max(0.5, min(float(timeout_s), 30.0))
    while win32gui.IsWindow(hwnd) and time.monotonic() < deadline:
        time.sleep(0.2)
    closed = not win32gui.IsWindow(hwnd)
    return {"success": closed, "closed": closed, "hwnd": hwnd,
            **({} if closed else {"error": "CLOSE_TIMEOUT: 窗口未在时限内关闭（可能等待用户确认）"})}


@tool("读一次元素表，在内存里连续执行 click/type/wait，最后返回逐步结果。"
      "适合同窗口连点/连输；翻页、切标签等内容大幅变化时不要使用。失败立即停止，不重试。")
def desktop_uia_batch(window_id: str, steps: list[dict], *, max_depth: int = 10,
                  max_nodes: int = 600, timeout_s: float = 30.0) -> dict:
    started = time.monotonic()
    result = {"success": False, "elements_scanned": 0, "steps": [],
              "total_ms": 0, "failed_index": None, "uia_scans": 0}
    current = None
    try:
        from pywinauto import Desktop
        if not isinstance(steps, list) or not 1 <= len(steps) <= 100:
            raise ValueError("steps 必须包含 1–100 个动作")
        if not 0 < float(timeout_s) <= 120:
            raise ValueError("timeout_s 必须在 (0, 120] 内")
        deadline = started + float(timeout_s)
        controller, info = _controller(), bound_window_info(window_id)
        if controller is None or not info:
            raise ValueError("DESKTOP_NOT_READY 或 window_id 不存在")
        hwnd = int(info["hwnd"])
        with controller._input_lock:
            if not controller.window.is_valid(hwnd):
                raise ValueError("窗口已关闭或句柄失效")
            if not _is_effectively_foreground(hwnd):
                controller._force_foreground(hwnd)
            region = controller.window.get_client_region(hwnd)
            if region is None or region.width <= 0 or region.height <= 0:
                raise ValueError("窗口客户区不可用")

            def scan():
                """(Re)build the element table from the live UIA tree."""
                nonlocal region
                fresh = controller.window.get_client_region(hwnd)
                if fresh is not None and fresh.width > 0 and fresh.height > 0:
                    region = fresh
                root = Desktop(backend="uia").window(handle=hwnd).wrapper_object()
                found = _uia_batch_elements(root, region, max(1, min(int(max_depth), 10)),
                                            max(20, min(int(max_nodes), 1500)), deadline)
                result["uia_scans"] += 1
                result["elements_scanned"] = len(found)
                index = {}
                for element in found:
                    for key in ("auto_id", "name"):
                        if element[key]:
                            index.setdefault((key, element[key]), []).append(element)
                return index

            table = scan()
            for i, step in enumerate(steps):
                tick = time.monotonic()
                current = {"i": i, "action": step.get("action") if isinstance(step, dict) else None,
                           "target": step.get("auto_id") or step.get("name", "") if isinstance(step, dict) else "",
                           "ok": False}
                result["steps"].append(current)
                if tick >= deadline:
                    raise TimeoutError("UIA_BATCH_TIMEOUT: 动作超时")
                if not controller.window.is_valid(hwnd):
                    raise ValueError("窗口已关闭或句柄失效")
                try:
                    current["method"] = _uia_batch_step(controller, hwnd, region, table, step,
                                                        deadline - tick)
                except (_StaleElementError, _BatchTargetMissing) as stale:
                    # One rescan absorbs the ordinary case: the previous step
                    # changed the tree (a digit turns clear into clearEntry).
                    if current.get("rescans"):
                        raise
                    current["rescans"] = 1
                    current["stale_reason"] = str(stale)
                    table = scan()
                    current["method"] = _uia_batch_step(controller, hwnd, region, table, step,
                                                        deadline - tick)
                if not controller.window.is_valid(hwnd):
                    raise ValueError("窗口已关闭或句柄失效")
                if time.monotonic() > deadline:
                    raise TimeoutError("UIA_BATCH_TIMEOUT: 动作超时，未重放")
                current.update(ok=True, elapsed_ms=round((time.monotonic() - tick) * 1000))
        result["success"] = True
    except Exception as exc:
        result.update(error=f"{type(exc).__name__}: {exc}", hint="检查窗口和已完成步骤；不自动重试。")
        if current is not None:
            current.update(error=result["error"], elapsed_ms=round((time.monotonic() - tick) * 1000))
            result["failed_index"] = current["i"]
    result["total_ms"] = round((time.monotonic() - started) * 1000)
    return result


def register_desktop_tools() -> int:
    """Register every @tool function in this module under the 'desktop' toolset."""
    count = 0
    for name, func in list(globals().items()):
        if not name.startswith("desktop_") or not callable(func):
            continue
        meta = getattr(func, "_tool_meta", None)
        if not isinstance(meta, dict):
            continue
        from .tool_decorator import _build_schema
        schema = _build_schema(func)
        registry.register(
            name=name,
            toolset=DESKTOP_TOOLSET,
            schema=schema,
            handler=lambda args, _func=func, **_kw: tool_result(_func(**args)),
            override=True,
        )
        count += 1
    logger.info("Desktop tools registered: %d", count)
    return count


@tool("截取整个桌面（所有显示器合成一图），可选跑 OCR/YOLO 返回元素。"
      "**用途：当动作结果不确定时，用全屏图判断真实状态。**"
      "窗口级 capture 走 PrintWindow，会把目标窗渲染出来——被其他窗口"
      "（弹窗、更新提示、输入法候选）挡住时完全看不出来；只有全屏截图能看见遮挡。"
      "典型场景：type 报 suspected_noop / confirmed 但回读不到文本，"
      "此时应先 capture_desktop 看是不是有弹窗抢了焦点。")
def desktop_capture_desktop(analyze: bool = False) -> dict:
    controller = _controller()
    if controller is None:
        return {"success": False, "error": "DESKTOP_NOT_READY: AppController 尚未初始化"}
    try:
        result = controller.capture_desktop(analyze=bool(analyze))
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": f"DESKTOP_CAPTURE_FAILED: {exc}"}
    if result.get("error"):
        return {"success": False, "error": result["error"]}
    # 顺带报告当前前台窗口，"看图"时必须知道焦点在哪
    try:
        import win32gui
        fg_hwnd = win32gui.GetForegroundWindow()
        result["foreground"] = {
            "hwnd": int(fg_hwnd),
            "title": win32gui.GetWindowText(fg_hwnd)[:80],
            "class": win32gui.GetClassName(fg_hwnd)[:40],
        }
    except Exception:  # noqa: BLE001
        result["foreground"] = None
    result["success"] = True
    result["hint"] = ("全屏图能看到窗口遮挡。若目标窗被别的窗口挡住，先关掉遮挡再操作；"
                      "同时对照 foreground 判断键盘输入会打到哪个窗口。")
    return result

@tool("按 exe 名关闭多余窗口，保留最多 keep 个。用于收尾：测试会反复启动浏览器/编辑器，"
      "必须清理，否则残留窗口会挡住后续任务、也让用户桌面越来越乱。"
      "默认只处理本会话启动过的 exe（不碰用户自己的窗口）。")
def desktop_cleanup_windows_by_exe(exe_keyword: str = "", keep: int = 0,
                                   scope_owner: str = "") -> dict:
    """Close windows THIS session launched and can still prove ownership of.

    exe_keyword only narrows the registered set; it can never select a window
    the session did not register, so a user's own windows are out of reach even
    when they share an executable name. Windows whose identity can no longer be
    re-verified are kept and reported. Save-confirmation dialogs are never
    answered here: the window stays open and the result says cleanup is
    incomplete.
    """
    import time as _t

    import win32con
    import win32gui

    exe_kw = (exe_keyword or "").strip().lower()

    registered = list(getattr(_STATE, "_launched_windows", []) or [])
    if not registered:
        return {"success": True, "matched": 0, "closed": 0, "remaining": 0,
                "failed": [], "note": "本任务没有登记过窗口，未关闭任何东西"}

    wins: list[tuple[int, str]] = []
    for item in registered:
        hwnd = int(item.get("hwnd", 0) or 0)
        if not hwnd:
            continue
        if scope_owner and str(item.get("scope_owner") or "") != scope_owner:
            continue
        exe = str(item.get("exe") or "").lower()
        if exe_kw and exe_kw not in exe:
            continue
        if not win32gui.IsWindow(hwnd):
            continue
        wins.append((hwnd, str(item.get("title") or "")))

    if not wins:
        return {"success": True, "matched": 0, "closed": 0, "remaining": 0,
                "failed": [], "note": "登记窗口已不存在或与过滤条件不符"}

    to_close = wins[int(keep):] if keep > 0 else wins
    closed, failed = 0, []
    for hwnd, title in to_close:
        item = next((i for i in registered if int(i.get("hwnd", 0) or 0) == hwnd), None)
        label = (title or (item or {}).get("title") or (item or {}).get("exe") or str(hwnd))[:40]
        # HWNDs get recycled after a window closes; only close when the recorded
        # identity still matches, otherwise we would close someone else's window.
        actual = _current_window_key(hwnd)
        if not actual:
            failed.append(f"{label}: 窗口身份不可读，已保留")
            continue
        if item is not None and str(item.get("window_key") or "") and actual != str(item["window_key"]):
            failed.append(f"{label}: 句柄已被其它窗口复用，已保留")
            continue
        try:
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        except Exception as exc:  # noqa: BLE001
            failed.append(f"{label}: WM_CLOSE 失败 {type(exc).__name__}")
            continue
        deadline = _t.monotonic() + 4.0
        while _t.monotonic() < deadline and win32gui.IsWindow(hwnd):
            _t.sleep(0.1)
        if win32gui.IsWindow(hwnd):
            # A save-confirmation dialog keeps the window alive on purpose: we
            # never answer it for the user.
            failed.append(f"{label}: 未关闭（可能有保存确认框或未响应），已保留现场")
        else:
            closed += 1
    return {
        "success": not failed,
        "matched": len(wins),
        "closed": closed,
        "remaining": len(wins) - closed,
        "failed": failed[:5],
        "note": ("只关闭本任务登记且身份匹配的窗口；遇到保存确认框保留现场，"
                 "由调用方决定是否保存。"),
    }
