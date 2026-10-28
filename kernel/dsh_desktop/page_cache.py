"""Small persistent cache of independently verified UIA navigation steps.

Contains no screenshots or OCR content. Cache hits are hints, never authority:
callers must revalidate the live target before dispatching an action.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

from .config import dsh_desktop_home

_LOCK = threading.RLock()
_MAX_ENTRIES = 128
_VERSION = 3


def _validate_signature(signature: list[int]) -> None:
    if len(signature) != 48 * 48 or any(not isinstance(x, int) or not 0 <= x <= 255 for x in signature):
        raise ValueError("invalid screen fingerprint")


def fingerprint(signature: list[int]) -> str:
    """Perceptual page candidate key; never proof of target identity."""
    import cv2
    import numpy as np
    _validate_signature(signature)
    image = np.asarray(signature, dtype=np.uint8).reshape(48, 48)
    small = cv2.resize(image, (9, 8), interpolation=cv2.INTER_AREA)
    bits = np.packbits(small[:, 1:] > small[:, :-1]).tobytes()
    return hashlib.sha256(bits).hexdigest()


def visually_stable(before: list[int], after: list[int]) -> bool:
    """Permit only tiny screenshot noise while the live UIA check runs."""
    _validate_signature(before)
    _validate_signature(after)
    diff = [abs(a - b) for a, b in zip(before, after)]
    return max(diff, default=0) <= 3 and sum(d != 0 for d in diff) <= 8


def make_key(context: dict, signature: list[int], target: str, control_type: str,
             expected: str, action: str) -> str:
    payload = [context, fingerprint(signature), target, control_type, expected, action]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _path() -> Path:
    return dsh_desktop_home() / "verified-page-cache.json"


def _read() -> dict:
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
        if raw.get("version") == _VERSION and isinstance(raw.get("entries"), dict):
            return raw["entries"]
    except (OSError, ValueError, AttributeError):
        pass
    return {}


def _write(entries: dict) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"version": _VERSION, "entries": entries}, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def lookup(key: str) -> dict | None:
    with _LOCK:
        value = _read().get(key)
        if not isinstance(value, dict) or value.get("verified_runs", 0) < 1:
            return None
        return dict(value)


def store_verified(key: str, bbox: list[int] | None) -> None:
    """Call only after independent before/after UIA validation succeeds."""
    with _LOCK:
        entries = _read()
        old = entries.get(key) or {}
        entries[key] = {"bbox": bbox, "verified_runs": int(old.get("verified_runs", 0)) + 1,
                        "last_verified": int(time.time())}
        if len(entries) > _MAX_ENTRIES:
            for k in sorted(entries, key=lambda k: entries[k].get("last_verified", 0))[:-_MAX_ENTRIES]:
                del entries[k]
        _write(entries)


def invalidate(key: str) -> None:
    with _LOCK:
        entries = _read()
        if key in entries:
            del entries[key]
            _write(entries)


def window_context(hwnd: int, info: dict) -> tuple[dict, tuple[int, int]]:
    import ctypes
    from ctypes import wintypes
    import psutil
    import win32gui
    from . import desktop_tools
    rect = win32gui.GetClientRect(hwnd)
    origin = win32gui.ClientToScreen(hwnd, (rect[0], rect[1]))
    width, height = rect[2] - rect[0], rect[3] - rect[1]
    if width <= 0 or height <= 0:
        raise ValueError("empty client area")
    path = psutil.Process(int(info["pid"])).exe()
    stat = os.stat(path)
    title = desktop_tools._safe_window_title(hwnd)
    if not title:
        raise ValueError("window title unavailable")
    get_dpi = ctypes.windll.user32.GetDpiForWindow
    get_dpi.argtypes = [wintypes.HWND]
    get_dpi.restype = wintypes.UINT
    dpi = int(get_dpi(hwnd))
    if not dpi:
        raise ValueError("window DPI unavailable")
    context = {"exe": hashlib.sha256(path.lower().encode()).hexdigest(),
               "version": [stat.st_size, stat.st_mtime_ns],
               "title": hashlib.sha256(title.encode()).hexdigest(),
               "class": win32gui.GetClassName(hwnd), "size": [width, height],
               "dpi": dpi}
    return context, origin


def scan_uia(hwnd: int, target: str, control_type: str, expected_name: str) -> tuple[list, int]:
    from pywinauto import Desktop
    root = Desktop(backend="uia").window(handle=hwnd).wrapper_object()
    targets, expected = [], 0
    for control in root.descendants():
        try:
            element = control.element_info
            name = str(element.name or "").strip()
            typ = str(element.control_type or "")
            if name not in (target, expected_name):
                continue
            if not control.is_visible():
                continue
            if name == expected_name:
                expected += 1
            if name == target and typ == control_type:
                r = element.rectangle
                targets.append({"rect": [r.left, r.top, r.right, r.bottom],
                                "enabled": bool(control.is_enabled())})
        except Exception:
            # A failed read makes absence and uniqueness unverifiable.
            raise ValueError("UIA element unavailable")
    return targets, expected


def window_signature(hwnd: int) -> list[int]:
    """Fingerprint the window itself, excluding desktop overlays and cursors."""
    import cv2
    from . import desktop_tools
    controller = desktop_tools._controller()
    if controller is None:
        raise ValueError("controller unavailable")
    frame = controller.capture._capture_print_window(hwnd)
    if frame is None or not getattr(frame, "size", 0) or float(frame.mean()) < 1.0:
        raise ValueError("window capture unavailable")
    small = cv2.resize(frame, (48, 48), interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    return [int(v) for v in gray.reshape(-1)]
