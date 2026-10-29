"""Breathing blue glow around the window the agent is operating.

Colour comes from OKLCH (the approach Retroma uses): one low-chroma blue accent
whose lightness adapts to the target window (darker on light UIs, lighter on dark
UIs). The frame is a click-through layered window inserted directly above the
target in z-order — so if another app covers the target, it covers the glow too.
"""
from __future__ import annotations

import ctypes
import math
import threading
import time
from ctypes import wintypes

import numpy as np

from .cursor_overlay import (AC_SRC_ALPHA, AC_SRC_OVER, BLENDFUNCTION, BITMAPINFO,
                             BITMAPINFOHEADER, SW_HIDE, ULW_ALPHA, WS_EX_LAYERED,
                             WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW, WS_EX_TRANSPARENT,
                             WS_POPUP)
from .oklch import oklch_to_rgb

PAD = 10
IDLE_SECONDS = 2.0
PERIOD_SECONDS = 2.0
ALPHA_MIN, ALPHA_MAX = 150, 235
HUE = 252.0          # blue
CHROMA = 0.085       # deliberately muted
SWP_NOACTIVATE = 0x0010
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002


def oklch_to_rgb(lightness: float, chroma: float, hue: float) -> tuple[int, int, int]:
    """Kept as a module alias so older imports keep working."""
    from .oklch import oklch_to_rgb as convert
    return convert(lightness, chroma, hue)


class WindowGlow:
    def __init__(self) -> None:
        self._thread = None
        self._ready = threading.Event()
        self._hwnd = 0
        self._error = None
        self._lock = threading.Lock()
        self._target = 0
        self._last_activity = 0.0
        self._tone = "light"

    # ── public API ───────────────────────────────────────────────────────
    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(target=self._run, daemon=True, name="desktop-glow")
            self._thread.start()
        self._ready.wait(timeout=3)

    def show(self, hwnd: int, tone: str = "light") -> None:
        with self._lock:
            self._target = int(hwnd)
            self._tone = "dark" if tone == "dark" else "light"
            self._last_activity = time.monotonic()

    def hide(self) -> None:
        with self._lock:
            self._target = 0

    @property
    def error(self):
        return self._error

    # ── internals ────────────────────────────────────────────────────────
    def _run(self) -> None:
        try:
            self._create_window()
            self._ready.set()
            self._loop()
        except Exception as exc:  # noqa: BLE001 - cosmetics never break a task
            self._error = f"{exc.__class__.__name__}: {exc}"
            self._ready.set()

    def _create_window(self) -> None:
        user32 = ctypes.windll.user32
        hinstance = ctypes.windll.kernel32.GetModuleHandleW(None)
        class_name = "DesktopWindowGlow"
        proc_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                                       wintypes.WPARAM, wintypes.LPARAM)
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                          wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = ctypes.c_ssize_t

        def wndproc(hwnd, msg, wparam, lparam):
            if msg == 0x0010:
                user32.DestroyWindow(hwnd)
                return 0
            if msg == 0x0084:
                return -1  # HTTRANSPARENT
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._proc = proc_type(wndproc)
        wndclass = type("WNDCLASSW", (ctypes.Structure,), {
            "_fields_": [("style", wintypes.UINT), ("lpfnWndProc", proc_type),
                         ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                         ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                         ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                         ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)],
        })
        instance = wndclass()
        instance.lpfnWndProc = self._proc
        instance.hInstance = hinstance
        instance.lpszClassName = class_name
        user32.RegisterClassW(ctypes.byref(instance))
        ex_style = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
        self._hwnd = user32.CreateWindowExW(ex_style, class_name, "glow", WS_POPUP,
                                            0, 0, PAD * 2, PAD * 2, None, None, hinstance, None)
        if not self._hwnd:
            raise ctypes.WinError()
        self._user32 = user32
        self._gdi32 = ctypes.windll.gdi32
        self._cache = None  # (rect, mem, dib, bits, size)

    def _frame(self, width: int, height: int, tone: str) -> np.ndarray:
        import cv2

        # OKLCH: darker stroke on light UIs, lighter stroke on dark UIs.
        lightness = 0.58 if tone == "light" else 0.74
        r, g, b = oklch_to_rgb(lightness, CHROMA, HUE)
        color = (b, g, r)  # BGRA order below
        canvas = np.zeros((height + PAD * 2, width + PAD * 2, 4), dtype=np.uint8)
        outer = (PAD - 5, PAD - 5, PAD + width + 4, PAD + height + 4)
        inner = (PAD, PAD, PAD + width - 1, PAD + height - 1)
        cv2.rectangle(canvas, outer[:2], outer[2:], (*color, 95), 10, cv2.LINE_AA)
        cv2.rectangle(canvas, inner[:2], inner[2:], (*color, 210), 4, cv2.LINE_AA)
        return canvas

    def _ensure_bitmap(self, rect, tone) -> None:
        left, top, right, bottom = rect
        width, height = max(1, right - left), max(1, bottom - top)
        key = (rect, tone)
        if self._cache and self._cache[0] == key:
            return
        self._release_cache()
        bitmap = self._frame(width, height, tone)
        user32, gdi32 = self._user32, self._gdi32
        screen = user32.GetDC(None)
        mem = gdi32.CreateCompatibleDC(screen)
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = bitmap.shape[1]
        bmi.bmiHeader.biHeight = -bitmap.shape[0]
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bits = ctypes.c_void_p()
        dib = gdi32.CreateDIBSection(mem, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        gdi32.SelectObject(mem, dib)
        ctypes.memmove(bits, bitmap.ctypes.data, bitmap.nbytes)
        self._cache = (key, mem, dib, bits, (bitmap.shape[1], bitmap.shape[0]))
        user32.ReleaseDC(None, screen)

    def _release_cache(self) -> None:
        if not self._cache:
            return
        _, mem, dib, _, _ = self._cache
        try:
            self._gdi32.DeleteObject(dib)
            self._gdi32.DeleteDC(mem)
        finally:
            self._cache = None

    def _render(self, rect, tone: str, alpha: int) -> None:
        self._ensure_bitmap(rect, tone)
        if not self._cache:
            return
        _, mem, _, _, (width, height) = self._cache
        user32, gdi32 = self._user32, self._gdi32
        screen = user32.GetDC(None)
        try:
            blend = BLENDFUNCTION(AC_SRC_OVER, 0, max(0, min(255, alpha)), AC_SRC_ALPHA)
            dst = wintypes.POINT(rect[0] - PAD, rect[1] - PAD)
            src = wintypes.POINT(0, 0)
            size = wintypes.SIZE(width, height)
            user32.UpdateLayeredWindow(self._hwnd, screen, ctypes.byref(dst), ctypes.byref(size),
                                       mem, ctypes.byref(src), 0, ctypes.byref(blend), ULW_ALPHA)
        finally:
            user32.ReleaseDC(None, screen)

    def _place_above(self, target: int) -> None:
        """Sit directly above the target, but below whatever already covers it.

        SetWindowPos(hwnd, X) puts hwnd *after* X in z-order (below it), so to sit
        above the target we must insert after the window that precedes the target.
        """
        GW_HWNDPREV = 3
        prev = self._user32.GetWindow(target, GW_HWNDPREV)
        if prev == self._hwnd:
            return
        insert_after = prev if prev else 0  # 0 == HWND_TOP
        self._user32.SetWindowPos(self._hwnd, insert_after, 0, 0, 0, 0,
                                  SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    def _loop(self) -> None:
        user32 = ctypes.windll.user32
        visible = False
        last_rect = None
        last_place = 0.0
        while True:
            now = time.monotonic()
            with self._lock:
                target, last, tone = self._target, self._last_activity, self._tone
            if not target or now - last > IDLE_SECONDS:
                if visible:
                    user32.ShowWindow(self._hwnd, SW_HIDE)
                    visible, last_rect = False, None
                    self._release_cache()
            else:
                rect = None
                try:
                    r = wintypes.RECT()
                    user32.GetWindowRect(target, ctypes.byref(r))
                    rect = (r.left, r.top, r.right, r.bottom)
                except Exception:  # noqa: BLE001
                    rect = None
                if rect and rect[2] > rect[0] and rect[3] > rect[1]:
                    breathe = 0.5 + 0.5 * math.sin(2 * math.pi * now / PERIOD_SECONDS)
                    alpha = int(ALPHA_MIN + (ALPHA_MAX - ALPHA_MIN) * breathe)
                    try:
                        self._render(rect, tone, alpha)
                        if not visible:
                            user32.ShowWindow(self._hwnd, 4)  # SW_SHOWNOACTIVATE
                            visible = True
                        if rect != last_rect or now - last_place > 0.5:
                            self._place_above(target)
                            last_place = now
                        last_rect = rect
                    except Exception as exc:  # noqa: BLE001 - keep the loop alive, surface the reason
                        self._error = f"{exc.__class__.__name__}: {exc}"
            time.sleep(0.08)
