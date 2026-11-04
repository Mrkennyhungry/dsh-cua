"""Input helpers operating on explicit window handles."""
from __future__ import annotations

import ctypes
import math
import time

import numpy as np
import pyautogui
from pynput.mouse import Button, Controller

from . import window

_CF_UNICODETEXT = 13


def _read_clipboard_text() -> str | None:
    """Current clipboard text, or None when it holds something else (or is busy)."""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    # On 64-bit Windows an HGLOBAL is a pointer-sized value. Without argtypes
    # ctypes passes it as a 32-bit C int and a handle above 2**31 raises
    # "ArgumentError: int too long to convert". _write_clipboard_text sets the
    # same argtypes, but the process-wide ctypes prototype is only configured
    # once that function has run: type_text reads the clipboard FIRST, so a
    # fresh kernel always hit the crash on its first paste. Declare them here
    # too, and pass the handle as an explicit c_void_p.
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.GetClipboardData.argtypes = [ctypes.c_uint]
    user32.GetClipboardData.restype = ctypes.c_void_p
    if not user32.OpenClipboard(None):
        return None
    try:
        handle = user32.GetClipboardData(_CF_UNICODETEXT)
        if not handle:
            return None
        handle = ctypes.c_void_p(handle)
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    except (AttributeError, OSError, OverflowError, ValueError, ctypes.ArgumentError):
        return None
    finally:
        user32.CloseClipboard()


def _write_clipboard_text(text: str) -> bool:
    """Put text on the clipboard. Returns False when the clipboard is unavailable."""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    encoded = text.encode("utf-16-le") + b"\x00\x00"
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.restype = ctypes.c_void_p
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    buffer = kernel32.GlobalAlloc(0x0042, len(encoded))  # MOVEABLE | ZEROINIT
    if not buffer:
        return False
    pointer = kernel32.GlobalLock(buffer)
    if not pointer:
        kernel32.GlobalFree(buffer)
        return False
    ctypes.memmove(pointer, encoded, len(encoded))
    kernel32.GlobalUnlock(buffer)
    if not user32.OpenClipboard(None):
        kernel32.GlobalFree(buffer)
        return False
    try:
        user32.EmptyClipboard()
        return bool(user32.SetClipboardData(_CF_UNICODETEXT, buffer))
    finally:
        user32.CloseClipboard()


class InputService:
    """Stateless mouse and keyboard input service."""

    def __init__(self, window_service: window.WindowService | None = None):
        self.window = window_service or window.WindowService()
        self.mouse = Controller()

    @property
    def virtual(self):
        # Kept for controller compatibility. Input is always physical.
        return False

    def _feedback(self, action):
        try:
            from ..cursor_overlay import peek_cursor_overlay
            overlay = peek_cursor_overlay()
            if overlay is not None: overlay.action(action)
        except Exception:
            pass

    def _verify_pointer_target(self, x, y):
        try:
            actual = tuple(map(int,self.mouse.position))
            if abs(actual[0]-int(x))<=2 and abs(actual[1]-int(y))<=2: return None
        except Exception:
            actual = None
        return {'success':False,'dispatch':'physical','error':'PHYSICAL_POINTER_MOVE_FAILED: no button or wheel event was sent',
                'expected':[int(x),int(y)],'actual':actual}

    def _gate(self):
        """Refuse input the session cannot receive, or input from a cancelled action."""
        from .. import input_gate

        return input_gate.guard()
    def right_click_screen(self, x, y, hwnd=None):
        blocked = self._gate()
        if blocked:
            return blocked

        self._feedback('right_click')
        self._human_move_to(x, y)
        failed = self._verify_pointer_target(x,y)
        if failed: return failed
        self.mouse.press(Button.right)
        self.mouse.release(Button.right)
        return {'success': True, 'dispatch': 'physical', 'x': x, 'y': y}

    def click_screen(self, x: int, y: int, clicks: int = 1) -> dict:
        """Click at absolute screen coordinates with human-like movement and timing."""
        blocked = self._gate()
        if blocked:
            return blocked

        self._feedback('click')
        self._human_move_to(x, y)
        failed = self._verify_pointer_target(x,y)
        if failed: return failed

        for i in range(clicks):
            if i > 0:
                time.sleep(0.12)   # fixed double-click gap, inside the OS threshold
            time.sleep(0.05)
            self.mouse.press(Button.left)
            time.sleep(0.04)
            self.mouse.release(Button.left)

        return {"success": True, "dispatch":"physical", "x": x, "y": y, "clicks": clicks}

    def _human_move_to(self, x: int, y: int) -> None:
        """Move the pointer to (x, y) along a fixed Bezier arc.

        Deterministic on purpose: the old path added ±1.5px jitter to every step
        and a 30% chance of a random overshoot/correction. Two identical actions
        then ended on slightly different pixels, could trigger hover menus on the
        way, and made "the same script reproduces the same result" impossible.
        The eased curve is kept so the move still looks human, not teleporting.
        """
        target = np.array([x, y], dtype=float)
        try:
            cur = np.array(self.mouse.position, dtype=float)
            if cur.shape != (2,):
                raise ValueError(f"unexpected cursor position: {self.mouse.position!r}")
        except Exception:
            # Cursor position unavailable (e.g. service context) — move directly.
            self.mouse.position = (x, y)
            return
        distance = float(np.linalg.norm(target - cur))
        if distance < 1:
            return

        # Fixed control points: a shallow arc, same shape for the same endpoints.
        arc = distance * 0.15
        perp = np.array([-(target[1] - cur[1]), target[0] - cur[0]])
        perp_len = np.linalg.norm(perp)
        if perp_len > 0:
            perp = perp / perp_len
        cp1 = cur + (target - cur) * 0.3 - perp * arc
        cp2 = cur + (target - cur) * 0.7 - perp * arc

        segments = max(int(distance / 15), 8)
        duration = min(max(distance / 800, 0.2), 0.7)

        for i in range(1, segments + 1):
            t = i / segments
            eased = t * t * (3 - 2 * t)
            pos = (1 - eased)**3 * cur + \
                  3 * (1 - eased)**2 * eased * cp1 + \
                  3 * (1 - eased) * eased**2 * cp2 + \
                  eased**3 * target
            self.mouse.position = tuple(int(v) for v in pos)
            delay_factor = 1.0 + 0.5 * (1 - math.sin(math.pi * eased))
            time.sleep(max(0.001, (duration / segments) * delay_factor))

        # Settle on the exact target; no overshoot pass.
        self.mouse.position = (x, y)

    def click_window(self, hwnd: int, x: int, y: int, *, activate: bool = True, clicks: int = 1) -> dict:
        """Click at client-area coordinates relative to a window."""
        blocked = self._gate()
        if blocked:
            return blocked

        region = self.window.get_client_region(hwnd)
        if region is None:
            return {"success": False, "error": "Window client region not available"}

        abs_x = region.left + x
        abs_y = region.top + y
        if activate:
            self.window.ensure_foreground(hwnd)
            time.sleep(0.2)
        return self.click_screen(abs_x, abs_y, clicks=clicks)

    def click_normalized(self, hwnd: int, x: int, y: int, *, activate: bool = True, clicks: int = 1) -> dict:
        """Click at normalized [0, 1000] coordinates within a window client area."""
        blocked = self._gate()
        if blocked:
            return blocked

        if not self.window.is_valid(hwnd):
            return {"success": False, "error": "Invalid window handle"}

        if activate:
            self.window.ensure_foreground(hwnd)
            time.sleep(0.2)
        # Activation may restore or move a minimized window; convert only after
        # the final client rect is known, never with stale pre-activation geometry.
        region = self.window.get_client_region(hwnd)
        if region is None:
            return {"success": False, "error": "Window client region not available"}
        abs_x = region.left + int(x / 1000 * region.width)
        abs_y = region.top + int(y / 1000 * region.height)
        return self.click_screen(abs_x, abs_y, clicks=clicks)

    def mouse_down_window(self, hwnd: int, x: int, y: int, *, activate: bool = True) -> dict:
        """Press mouse button at client-area coordinates."""
        blocked = self._gate()
        if blocked:
            return blocked

        region = self.window.get_client_region(hwnd)
        if region is None:
            return {"success": False, "error": "Window client region not available"}

        if activate:
            self.window.ensure_foreground(hwnd)
            time.sleep(0.2)
        pyautogui.mouseDown(region.left + x, region.top + y)
        return {"success": True, "x": region.left + x, "y": region.top + y}

    def mouse_up(self):
        pyautogui.mouseUp()

    def mouse_move_screen(self, x: int, y: int, hwnd=None):
        """Move mouse with human-like curved path and jitter."""
        self._human_move_to(x, y)

    def press_key(self, key: str, wait_time: float = 0.2, hwnd=None):
        blocked = self._gate()
        if blocked:
            return blocked

        self._feedback("key")
        pyautogui.keyDown(key)
        try:
            time.sleep(wait_time)
        finally:
            pyautogui.keyUp(key)

    def hotkey(self, *keys: str, hwnd=None):
        """Press a combination of keys simultaneously (e.g. hotkey('alt', 'left'))."""
        blocked = self._gate()
        if blocked:
            return blocked

        pyautogui.hotkey(*keys)

    def scroll(self, x: int, y: int, clicks: int, direction: str = "vertical", hwnd=None) -> dict:
        """Scroll mouse wheel at specified screen coordinates with human-like movement.

        Args:
            x, y: Absolute screen coordinates
            clicks: Scroll amount (positive=up/right, negative=down/left)
            direction: "vertical" or "horizontal"
        """
        blocked = self._gate()
        if blocked:
            return blocked

        self._human_move_to(x, y)
        time.sleep(0.08)
        failed = self._verify_pointer_target(x,y)
        if failed: return failed

        # A number of desktop apps handle each wheel event as one step even
        # when its delta represents several detents. Send one real detent per
        # event so a request for N clicks is not silently reduced to N/chunk.
        sign = 1 if clicks >= 0 else -1
        mouse_event = ctypes.windll.user32.mouse_event
        mouse_event.argtypes = [ctypes.c_uint32,ctypes.c_uint32,ctypes.c_uint32,ctypes.c_uint32,ctypes.c_size_t]
        mouse_event.restype = None
        for index in range(abs(clicks)):
            delta = sign * 120  # WHEEL_DELTA on Windows
            mouse_event(0x1000 if direction == 'horizontal' else 0x0800,
                        0,0,ctypes.c_uint32(delta).value,0)
            if index + 1 < abs(clicks):
                time.sleep(0.02)

        return {"success": True, "x": x, "y": y, "clicks": clicks}

    def type_text(self, text: str, hwnd=None) -> dict:
        """Type text via clipboard (Ctrl+V) to support Unicode/CJK characters.

        The caller's clipboard is saved and put back afterwards: pasting must not
        silently replace whatever the user had copied. Only text can be restored
        (images/files stay lost), and the result says which case happened.
        """
        blocked = self._gate()
        if blocked:
            return blocked

        if not text:
            return {"success": False, "error": "Empty text"}

        previous_clipboard = _read_clipboard_text()

        CF_UNICODETEXT = 13
        encoded = text.encode("utf-16-le") + b"\x00\x00"
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        kernel32.GlobalAlloc.restype = ctypes.c_void_p
        kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalUnlock.restype = ctypes.c_bool
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalFree.restype = ctypes.c_void_p
        kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
        user32.OpenClipboard.argtypes = [ctypes.c_void_p]
        user32.EmptyClipboard.restype = ctypes.c_bool
        user32.SetClipboardData.restype = ctypes.c_void_p
        user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
        user32.CloseClipboard.restype = ctypes.c_bool

        buf = kernel32.GlobalAlloc(0x0042, len(encoded))  # GMEM_MOVEABLE | GMEM_ZEROINIT
        if not buf:
            return {"success": False, "error": "GlobalAlloc failed"}
        ptr = kernel32.GlobalLock(buf)
        if not ptr:
            kernel32.GlobalFree(buf)
            return {"success": False, "error": "GlobalLock failed"}
        ctypes.memmove(ptr, encoded, len(encoded))
        kernel32.GlobalUnlock(buf)

        if not user32.OpenClipboard(None):
            kernel32.GlobalFree(buf)
            return {"success": False, "error": "OpenClipboard failed"}
        user32.EmptyClipboard()
        ok = user32.SetClipboardData(CF_UNICODETEXT, buf)
        user32.CloseClipboard()
        if not ok:
            kernel32.GlobalFree(buf)
            return {"success": False, "error": "SetClipboardData failed"}

        pyautogui.hotkey("ctrl", "v")
        # Give the target a moment to actually consume the clipboard before it is
        # replaced: several apps read it lazily on WM_PASTE, and restoring at
        # 0.1s made the paste come out empty (observed 2026-11-02).
        time.sleep(0.25)
        restored = False
        if previous_clipboard is not None:
            restored = _write_clipboard_text(previous_clipboard)
            time.sleep(0.05)
        return {"success": True, "length": len(text),
                "clipboard_restored": restored,
                **({} if previous_clipboard is not None
                   else {"clipboard_note": "原剪贴板不是文本，无法还原（粘贴内容仍留在剪贴板）"})}

    def swipe_screen(self, p1, p2, speed: float = 1.0, hwnd=None):
        """Drag from p1 to p2 along a fixed Bezier arc, releasing exactly on p2.

        Deterministic like _human_move_to: no random control points, no per-step
        jitter, fixed duration. It also used to release up to 1.5px away from p2,
        which matters for drag targets with small drop zones.
        """
        blocked = self._gate()
        if blocked:
            return blocked

        p1 = np.array(p1, dtype=float)
        p2 = np.array(p2, dtype=float)
        distance = float(np.linalg.norm(p2 - p1))

        offset = distance * 0.2
        perp = np.array([-(p2[1] - p1[1]), p2[0] - p1[0]])
        perp_len = np.linalg.norm(perp)
        if perp_len > 0:
            perp = perp / perp_len
        cp1 = p1 + (p2 - p1) * 0.3 - perp * offset
        cp2 = p1 + (p2 - p1) * 0.7 - perp * offset

        segments = max(int(distance / 15), 8)
        base_time = distance / (200 * max(0.1, speed))
        total_time = max(0.08, base_time)

        self.mouse.position = (int(p1[0]), int(p1[1]))
        failed = self._verify_pointer_target(*p1)
        if failed: return failed
        time.sleep(0.02)
        self.mouse.press(Button.left)
        try:
            for i in range(1, segments + 1):
                t = i / segments
                eased = t * t * (3 - 2 * t)
                b = (1 - eased)**3 * p1 + \
                    3 * (1 - eased)**2 * eased * cp1 + \
                    3 * (1 - eased) * eased**2 * cp2 + \
                    eased**3 * p2
                self.mouse.position = (int(b[0]), int(b[1]))
                delay_factor = 1.0 + 0.5 * (1 - math.sin(math.pi * eased))
                time.sleep(max(0.001, (total_time / segments) * delay_factor))
            self.mouse.position = (int(p2[0]), int(p2[1]))
        finally:
            self.mouse.release(Button.left)


Input = InputService
