"""Input helpers operating on explicit window handles."""
from __future__ import annotations

import ctypes
import math
import random
import time

import numpy as np
import pyautogui
from pynput.mouse import Button, Controller

from . import window


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

    def right_click_screen(self, x, y, hwnd=None):
        self._feedback('right_click')
        self._human_move_to(x, y)
        failed = self._verify_pointer_target(x,y)
        if failed: return failed
        self.mouse.press(Button.right)
        self.mouse.release(Button.right)
        return {'success': True, 'dispatch': 'physical', 'x': x, 'y': y}

    def click_screen(self, x: int, y: int, clicks: int = 1) -> dict:
        """Click at absolute screen coordinates with human-like movement and timing."""
        self._feedback('click')
        self._human_move_to(x, y)
        failed = self._verify_pointer_target(x,y)
        if failed: return failed

        for i in range(clicks):
            if i > 0:
                time.sleep(random.uniform(0.10, 0.25))
            time.sleep(random.uniform(0.05, 0.15))
            self.mouse.press(Button.left)
            time.sleep(random.uniform(0.04, 0.10))
            self.mouse.release(Button.left)

        return {"success": True, "dispatch":"physical", "x": x, "y": y, "clicks": clicks}

    def _human_move_to(self, x: int, y: int) -> None:
        """Move mouse to (x, y) along a natural Bezier curve with jitter."""
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

        # Cubic Bezier control points for a slightly curved arc
        arc = distance * random.uniform(0.1, 0.25)
        perp = np.array([-(target[1] - cur[1]), target[0] - cur[0]])
        perp_len = np.linalg.norm(perp)
        if perp_len > 0:
            perp = perp / perp_len
        cp1 = cur + (target - cur) * random.uniform(0.2, 0.4) + perp * random.uniform(-arc, arc)
        cp2 = cur + (target - cur) * random.uniform(0.6, 0.8) + perp * random.uniform(-arc, arc)

        segments = max(int(distance / 15), 8)
        duration = min(max(distance / 800, 0.2), 0.7)

        for i in range(1, segments + 1):
            t = i / segments
            eased = t * t * (3 - 2 * t)
            pos = (1 - eased)**3 * cur + \
                  3 * (1 - eased)**2 * eased * cp1 + \
                  3 * (1 - eased) * eased**2 * cp2 + \
                  eased**3 * target
            jitter = np.array([random.uniform(-1.5, 1.5), random.uniform(-1.5, 1.5)])
            self.mouse.position = tuple(int(v) for v in pos + jitter)
            delay_factor = 1.0 + 0.5 * (1 - math.sin(math.pi * eased))
            time.sleep(max(0.001, (duration / segments) * delay_factor))

        # Settle on exact target
        self.mouse.position = (x, y)

        # ~30% chance of overshoot and micro-correction
        if random.random() < 0.3 and distance > 30:
            ox = x + random.randint(-4, 4)
            oy = y + random.randint(-4, 4)
            self.mouse.position = (ox, oy)
            time.sleep(random.uniform(0.02, 0.06))
            self.mouse.position = (x, y)

    def click_window(self, hwnd: int, x: int, y: int, *, activate: bool = True, clicks: int = 1) -> dict:
        """Click at client-area coordinates relative to a window."""
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
        self._feedback("key")
        pyautogui.keyDown(key)
        try:
            time.sleep(wait_time)
        finally:
            pyautogui.keyUp(key)

    def hotkey(self, *keys: str, hwnd=None):
        """Press a combination of keys simultaneously (e.g. hotkey('alt', 'left'))."""
        pyautogui.hotkey(*keys)

    def scroll(self, x: int, y: int, clicks: int, direction: str = "vertical", hwnd=None) -> dict:
        """Scroll mouse wheel at specified screen coordinates with human-like movement.

        Args:
            x, y: Absolute screen coordinates
            clicks: Scroll amount (positive=up/right, negative=down/left)
            direction: "vertical" or "horizontal"
        """
        self._human_move_to(x, y)
        time.sleep(random.uniform(0.05, 0.15))
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
        """Type text via clipboard (Ctrl+V) to support Unicode/CJK characters."""
        if not text:
            return {"success": False, "error": "Empty text"}

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
        time.sleep(0.1)
        return {"success": True, "length": len(text)}

    def swipe_screen(self, p1, p2, speed: float = 1.0, hwnd=None):
        """Natural swipe using cubic Bezier curve with easing and random jitter."""
        p1 = np.array(p1, dtype=float)
        p2 = np.array(p2, dtype=float)
        distance = float(np.linalg.norm(p2 - p1))

        # Generate slightly curved path via cubic Bezier with random control points
        offset = distance * 0.2
        perp = np.array([-(p2[1] - p1[1]), p2[0] - p1[0]])
        perp_len = np.linalg.norm(perp)
        if perp_len > 0:
            perp = perp / perp_len
        cp1 = p1 + (p2 - p1) * random.uniform(0.2, 0.4) + perp * random.uniform(-offset, offset)
        cp2 = p1 + (p2 - p1) * random.uniform(0.6, 0.8) + perp * random.uniform(-offset, offset)

        # Number of segments scales with distance
        segments = max(int(distance / 15), 8)
        # Base time with speed factor, capped
        base_time = distance / (200 * speed)
        total_time = random.uniform(max(0.08, base_time * 0.8), base_time * 1.2)

        self.mouse.position = (int(p1[0]), int(p1[1]))
        failed = self._verify_pointer_target(*p1)
        if failed: return failed
        time.sleep(random.uniform(0.01, 0.03))
        self.mouse.press(Button.left)
        try:

            for i in range(1, segments + 1):
                # Ease-in-out: slow at start/end, faster in middle
                t = i / segments
                eased = t * t * (3 - 2 * t)

                # Cubic Bezier interpolation
                b = (1 - eased)**3 * p1 + \
                    3 * (1 - eased)**2 * eased * cp1 + \
                    3 * (1 - eased) * eased**2 * cp2 + \
                    eased**3 * p2

                # Add small random jitter (±1-2 pixels)
                jitter = np.array([random.uniform(-1.5, 1.5), random.uniform(-1.5, 1.5)])
                pos = b + jitter

                self.mouse.position = (int(pos[0]), int(pos[1]))

                # Variable delay: longer at start/end, shorter in middle
                delay_factor = 1.0 + 0.5 * (1 - math.sin(math.pi * eased))
                step_delay = (total_time / segments) * delay_factor
                time.sleep(max(0.001, step_delay))

        finally:
            self.mouse.release(Button.left)


Input = InputService
