"""Cosmetic pointer overlay: a click-through, always-on-top layered window.

Draws a classic arrow pointer (not a ring) and glides it towards its target with
exponential easing at ~60fps, so the movement reads as smooth rather than jumpy.
Uses physical screen pixels via UpdateLayeredWindow, so it lands exactly where the
kernel's coordinates are and never steals focus or input. Purely visual.
"""
from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes

import numpy as np

WS_EX_LAYERED = 0x80000
WS_EX_TRANSPARENT = 0x20
WS_EX_TOOLWINDOW = 0x80
WS_EX_TOPMOST = 0x8
WS_EX_NOACTIVATE = 0x08000000
WS_POPUP = 0x80000000
ULW_ALPHA = 0x2
AC_SRC_OVER = 0
AC_SRC_ALPHA = 1
SW_SHOWNOACTIVATE = 4
SW_HIDE = 0

# Classic pointer outline in a 13 x 19 unit box, anchored at its tip (0, 0).
ARROW_POINTS = [(0, 0), (0, 16.5), (4.1, 12.4), (6.8, 18.8),
                (9.3, 17.7), (6.6, 11.4), (12.1, 11.4)]
SCALE = 2.6
MARGIN = 5
SUPERSAMPLE = 4
EASING = 0.3
FRAME_SECONDS = 0.016


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_byte), ("BlendFlags", ctypes.c_byte),
                ("SourceConstantAlpha", ctypes.c_byte), ("AlphaFormat", ctypes.c_byte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def _pointer_bitmap() -> np.ndarray:
    """BGRA arrow pointer: white body, deep slate-blue outline, soft shadow."""
    import cv2

    from .oklch import oklch_to_rgb

    r, g, b = oklch_to_rgb(0.38, 0.035, 252.0)  # deep slate blue, same hue family
    outline = (b, g, r, 255)
    points = np.array([[px * SCALE, py * SCALE] for px, py in ARROW_POINTS], dtype=np.float32)
    width = int(points[:, 0].max()) + MARGIN + 6
    height = int(points[:, 1].max()) + MARGIN + 6
    factor = SUPERSAMPLE
    canvas = np.zeros((height * factor, width * factor, 4), dtype=np.uint8)
    body = (points + MARGIN) * factor

    # Soft drop shadow first so the pointer stays readable on light backgrounds.
    shadow = (body + 2.2 * factor).astype(np.int32)
    cv2.fillPoly(canvas, [shadow], (8, 10, 14, 105))
    canvas[:, :, 3] = cv2.GaussianBlur(canvas[:, :, 3], (0, 0), 1.6 * factor)

    cv2.fillPoly(canvas, [body.astype(np.int32)], (255, 255, 255, 255))
    cv2.polylines(canvas, [body.astype(np.int32)], True, outline,
                  max(1, int(1.15 * factor)), cv2.LINE_AA)
    return cv2.resize(canvas, (width, height), interpolation=cv2.INTER_AREA)


class CursorOverlay:
    def __init__(self) -> None:
        self._thread = None
        self._hwnd = 0
        self._ready = threading.Event()
        self._error = None
        self._lock = threading.Lock()
        self._target = None  # (x, y) in physical screen pixels, or None when hidden
        self._visible = False
        self._shown = False

    # ── public API ───────────────────────────────────────────────────────
    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(target=self._run, daemon=True, name="desktop-cursor")
            self._thread.start()
        self._ready.wait(timeout=3)

    def move(self, x: int, y: int) -> None:
        with self._lock:
            self._target = (int(x), int(y))
            self._visible = True

    def hide(self) -> None:
        with self._lock:
            self._visible = False

    @property
    def visible(self) -> bool:
        return self._visible

    @property
    def error(self):
        return self._error

    # ── window plumbing ──────────────────────────────────────────────────
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
        class_name = "DesktopCursorOverlay"
        proc_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                                       wintypes.WPARAM, wintypes.LPARAM)
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                          wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = ctypes.c_ssize_t

        def wndproc(hwnd, msg, wparam, lparam):
            if msg == 0x0010:  # WM_CLOSE
                user32.DestroyWindow(hwnd)
                return 0
            if msg == 0x0084:  # WM_NCHITTEST -> transparent to the mouse
                return -1  # HTTRANSPARENT
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc = proc_type(wndproc)
        wc = type("WNDCLASSW", (ctypes.Structure,), {
            "_fields_": [("style", wintypes.UINT), ("lpfnWndProc", proc_type),
                         ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                         ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                         ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                         ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)],
        })
        instance = wc()
        instance.lpfnWndProc = self._wndproc
        instance.hInstance = hinstance
        instance.lpszClassName = class_name
        user32.RegisterClassW(ctypes.byref(instance))
        ex_style = (WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW
                    | WS_EX_TOPMOST | WS_EX_NOACTIVATE)
        self._hwnd = user32.CreateWindowExW(ex_style, class_name, "cursor", WS_POPUP,
                                            0, 0, 64, 64, None, None, hinstance, None)
        if not self._hwnd:
            raise ctypes.WinError()
        self._bitmap = _pointer_bitmap()
        self._user32 = user32
        self._gdi32 = ctypes.windll.gdi32
        self._canvas = None
        self._last_paint = None

    def _ensure_canvas(self) -> None:
        if self._canvas is not None:
            return
        user32, gdi32 = self._user32, self._gdi32
        height, width = self._bitmap.shape[:2]
        screen = user32.GetDC(None)
        mem = gdi32.CreateCompatibleDC(screen)
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = width
        bmi.bmiHeader.biHeight = -height
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0
        bits = ctypes.c_void_p()
        dib = gdi32.CreateDIBSection(mem, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        gdi32.SelectObject(mem, dib)
        ctypes.memmove(bits, self._bitmap.ctypes.data, self._bitmap.nbytes)
        user32.ReleaseDC(None, screen)
        self._canvas = (mem, dib, width, height)

    def _paint(self, x: int, y: int) -> None:
        self._ensure_canvas()
        mem, _, width, height = self._canvas
        user32 = self._user32
        screen = user32.GetDC(None)
        try:
            blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
            dst = wintypes.POINT(x - MARGIN, y - MARGIN)
            src = wintypes.POINT(0, 0)
            size = wintypes.SIZE(width, height)
            user32.UpdateLayeredWindow(self._hwnd, screen, ctypes.byref(dst), ctypes.byref(size),
                                       mem, ctypes.byref(src), 0, ctypes.byref(blend), ULW_ALPHA)
        finally:
            user32.ReleaseDC(None, screen)

    def _loop(self) -> None:
        user32 = self._user32
        msg = wintypes.MSG()
        pos = None
        while True:
            with self._lock:
                target, visible = self._target, self._visible
            if not visible or target is None:
                if self._shown:
                    user32.ShowWindow(self._hwnd, SW_HIDE)
                    self._shown = False
                    pos = None
                    self._last_paint = None
            else:
                tx, ty = target
                if pos is None:
                    pos = [float(tx), float(ty)]
                else:
                    dx, dy = tx - pos[0], ty - pos[1]
                    if abs(dx) < 0.6 and abs(dy) < 0.6:
                        pos = [float(tx), float(ty)]
                    else:
                        pos[0] += dx * EASING
                        pos[1] += dy * EASING
                painted = (int(round(pos[0])), int(round(pos[1])))
                if painted != self._last_paint:
                    self._paint(painted[0], painted[1])
                    self._last_paint = painted
                if not self._shown:
                    user32.ShowWindow(self._hwnd, SW_SHOWNOACTIVATE)
                    self._shown = True
            if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            time.sleep(FRAME_SECONDS)
