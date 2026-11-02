"""Pre-input gates: session state (lock screen / UAC / RDP) and a cancellable action lease.

Two failure modes this closes:

1. Input dispatched into a desktop nobody is looking at. When the session is
   locked, a UAC prompt owns the secure desktop, or an RDP session is
   disconnected, SendInput still "succeeds" but the keystroke goes nowhere (or
   into the secure desktop). The action then reports success and the caller
   believes the app was driven.
2. Input dispatched *after* the caller gave up. The bridge runs each action in a
   worker thread with a timeout; before this module the thread kept running and
   could deliver a click/keystroke seconds after the timeout was reported, on a
   UI that had already moved on.

The gate is checked at the start of every input action; because it reads the
thread-bound token, a cancelled action refuses even if its thread is still alive.
"""
from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time
import uuid
from ctypes import wintypes

logger = logging.getLogger(__name__)

# How long after its timeout an action may still be considered live. The cancel
# call is what matters; the deadline is the second net for a thread that somehow
# escaped cancellation.
_DEADLINE_GRACE_S = 5.0

_lock = threading.RLock()
_deadlines: dict[str, float] = {}
_cancelled: dict[str, float] = {}
_tls = threading.local()

# Cancelled tokens stay alive briefly so an orphaned worker thread keeps being
# refused; they are pruned here instead of on end_action (which runs while that
# thread may still be alive).
_CANCEL_RETENTION_S = 600.0

# WTS_CONNECTSTATE_CLASS values that mean "a user is attached and looking".
_LIVE_SESSION_STATES = {0, 1, 3}  # Active, Connected, Shadow

DESKTOP_READOBJECTS = 0x0001
WTS_CURRENT_SESSION = 0xFFFFFFFF
WTS_CONNECT_STATE = 8


def _refusal(code: str, message: str, **extra) -> dict:
    return {"success": False, "error": f"{code}: {message}", "input_refused": True, **extra}


def session_blocker() -> dict | None:
    """Return a refusal when input cannot reach the interactive desktop."""
    if sys.platform != "win32":
        return None

    # A locked session or an open UAC prompt switches the input desktop to a
    # secure one; opening it is refused for a normal process.
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.OpenInputDesktop.restype = wintypes.HANDLE
        user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        user32.CloseDesktop.argtypes = [wintypes.HANDLE]
        desktop = user32.OpenInputDesktop(0, False, DESKTOP_READOBJECTS)
        if not desktop:
            err = ctypes.get_last_error()
            return _refusal(
                "SESSION_SECURE_DESKTOP",
                "输入桌面不可用：锁屏或 UAC 提示占用了安全桌面，输入不会送到目标应用"
                f"（WinError={err}）。解锁/处理完提权提示后重试。",
                desktop_error=err,
            )
        user32.CloseDesktop(desktop)
    except (AttributeError, OSError):
        pass

    # Disconnected RDP sessions keep a live window station but receive no input.
    try:
        wtsapi32 = ctypes.WinDLL("wtsapi32", use_last_error=True)
        wtsapi32.WTSQuerySessionInformationW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.c_int,
            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD),
        ]
        wtsapi32.WTSFreeMemory.argtypes = [ctypes.c_void_p]
        buffer = ctypes.c_void_p()
        size = wintypes.DWORD()
        if wtsapi32.WTSQuerySessionInformationW(
                None, WTS_CURRENT_SESSION, WTS_CONNECT_STATE, ctypes.byref(buffer), ctypes.byref(size)):
            try:
                state = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_int))[0]
            finally:
                wtsapi32.WTSFreeMemory(buffer)
            if state not in _LIVE_SESSION_STATES:
                remote = ctypes.WinDLL("user32").GetSystemMetrics(0x1000)  # SM_REMOTESESSION
                return _refusal(
                    "SESSION_NOT_ACTIVE",
                    f"当前会话状态为 {state}（0=已连接 4=已断开），输入不会生效"
                    + ("；这是 RDP 会话，断开连接后请保持窗口打开或改在控制台执行" if remote else ""),
                    connect_state=state,
                )
    except (AttributeError, OSError):
        pass

    return None


def begin_action(timeout_s: float) -> str:
    """Register an input lease for one action and return its token."""
    token = uuid.uuid4().hex
    now = time.monotonic()
    with _lock:
        _deadlines[token] = now + max(0.0, float(timeout_s)) + _DEADLINE_GRACE_S
        for stale in [t for t, at in _cancelled.items() if now - at > _CANCEL_RETENTION_S]:
            _cancelled.pop(stale, None)
    return token


def bind(token: str) -> None:
    """Bind the token to the calling thread, so the gate can see it."""
    _tls.token = token


def unbind() -> None:
    _tls.token = None


def end_action(token: str) -> None:
    """Drop the lease. A cancelled token keeps its refusal until it is pruned."""
    with _lock:
        _deadlines.pop(token, None)


def cancel_action(token: str) -> None:
    """Mark the action cancelled: its thread may still run, but it cannot input."""
    with _lock:
        _cancelled[token] = time.monotonic()
        _deadlines.pop(token, None)
    logger.warning("input lease %s cancelled; further input from it is refused", token[:8])


def run_bound(token: str, fn, *args, **kwargs):
    """Run fn with the token bound to this thread."""
    bind(token)
    try:
        return fn(*args, **kwargs)
    finally:
        unbind()


def current_token() -> str | None:
    return getattr(_tls, "token", None)


def guard() -> dict | None:
    """Refusal for the current thread's action, or None when input may proceed."""
    token = current_token()
    if token:
        with _lock:
            if token in _cancelled:
                return _refusal(
                    "INPUT_CANCELLED",
                    "该动作已被取消（超时或用户中止），不再发送任何输入；请重新观察界面后再决定",
                )
            deadline = _deadlines.get(token)
        if deadline is not None and time.monotonic() > deadline:
            return _refusal("INPUT_DEADLINE_EXCEEDED", "动作超出时限，输入已作废")
    return session_blocker()
