"""Task-scoped, click-through commander, docked independently of physical input."""
from __future__ import annotations

import ctypes
import os
import threading
import time
from contextlib import contextmanager
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
MARGIN = 0
SUPERSAMPLE = 4
EASING = 0.3
FRAME_SECONDS = 0.016

_INSTANCE = None
_INSTANCE_LOCK = threading.Lock()


def peek_cursor_overlay():
    return _INSTANCE


def get_cursor_overlay():
    """Shared visual feedback without importing controller/agent registries."""
    global _INSTANCE
    with _INSTANCE_LOCK:
        if _INSTANCE is None:
            _INSTANCE = CursorOverlay()
            _INSTANCE.start()
        return _INSTANCE


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
    from .mascot_theme import render
    return render()


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
        self._theme, self._palette, self._scale = "whale-maid", "ocean", 1
        self._action, self._action_until = "idle", 0.0
        self._tasks = set()
        self._capture_depth = 0
        self._leases = {}
        self._born = 0.0
        self._origin = None
        self._next_owner_check = 0.0
        self._capture_excluded = False

    def _layout(self):
        """Use the visible DSH composer area as a best-effort entrance origin."""
        from .mascot_theme import WIDTH, HEIGHT
        import win32gui, win32process, win32api, psutil
        candidates = []
        def inspect(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd): return
            try:
                pid = win32process.GetWindowThreadProcessId(hwnd)[1]
                if psutil.Process(pid).name().lower() == 'deepseek harness.exe': candidates.append(hwnd)
            except Exception: pass
        win32gui.EnumWindows(inspect, None)
        foreground = win32gui.GetForegroundWindow()
        dsh = foreground if foreground in candidates else (candidates[0] if candidates else None)
        monitor = win32api.MonitorFromWindow(dsh or foreground, 2)
        left, top, right, bottom = win32api.GetMonitorInfo(monitor)['Work']
        dock = (max(left, right-WIDTH-16), top+16)
        origin = dock
        if dsh:
            x1,y1,x2,y2=win32gui.GetWindowRect(dsh)
            origin=(max(left,min(right-WIDTH,(x1+x2-WIDTH)//2)),
                    max(top,min(bottom-HEIGHT,y2-HEIGHT-45)))
        return dock,origin

    @contextmanager
    def capture_guard(self):
        with self._lock:
            self._capture_depth += 1
        try:
            if self._hwnd:
                ctypes.windll.user32.ShowWindow(self._hwnd, SW_HIDE)
            time.sleep(FRAME_SECONDS * 2)
            yield
        finally:
            with self._lock:
                self._capture_depth -= 1
                self._shown = False

    def begin_task(self, owner, lease_seconds=None, client_pid=None):
        """Task lifetime is separate from input; remote clients use expiring leases."""
        now = time.monotonic()
        try: layout = self._layout()
        except Exception: layout = ((40,40),(40,40))
        process_created = None
        if client_pid:
            try:
                import psutil
                process_created = psutil.Process(client_pid).create_time()
            except Exception: process_created = None
        with self._lock:
            first = not self._tasks
            self._tasks.add(str(owner))
            self._leases[str(owner)] = (now+lease_seconds if lease_seconds else None, client_pid, process_created)
            if first:
                self._target,self._origin = layout
                self._born = now
                self._action_until = 0
            self._visible = True

    def end_task(self, owner):
        with self._lock:
            self._tasks.discard(str(owner))
            self._leases.pop(str(owner),None)
            if not self._tasks:
                self._visible = False
                self._action_until = 0

    def _resolved_action(self, now):
        if now < self._action_until and self._action not in ('thinking','idle'): return self._action
        if self._tasks and now-self._born < .9: return 'enter'
        return self._action if now < self._action_until else ("thinking" if self._tasks else "idle")

    def _expire_tasks(self, now):
        if now < self._next_owner_check: return
        self._next_owner_check = now+.25
        import psutil
        for owner,(deadline,pid,created) in list(self._leases.items()):
            expired = deadline is not None and now >= deadline
            if pid:
                try:
                    actual_created = psutil.Process(pid).create_time()
                    expired = expired or (created is not None and actual_created != created)
                except psutil.NoSuchProcess: expired = True
                except psutil.AccessDenied: expired = expired or not psutil.pid_exists(pid)
            if expired:
                self._leases.pop(owner,None)
                self._tasks.discard(owner)
        if not self._tasks: self._visible = False

    # ── public API ───────────────────────────────────────────────────────
    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(target=self._run, daemon=True, name="desktop-cursor")
            self._thread.start()
        self._ready.wait(timeout=3)

    def move(self, x: int, y: int) -> None:
        # Compatibility no-op: the commander never follows the pointer.
        return

    def configure(self, theme="whale-maid", palette="ocean", scale=1):
        from .cursor_theme import render
        render(theme, "idle", 0, palette, scale)
        with self._lock:
            self._theme, self._palette, self._scale = theme, palette, scale

    def action(self, action, x=None, y=None, duration=0.8):
        from .mascot_theme import ACTIONS
        if action not in ACTIONS: raise ValueError("unknown cursor action")
        with self._lock:
            if not self._tasks: return
            self._action, self._action_until = action, time.monotonic() + duration
            self._visible = True

    def hide(self) -> None:
        with self._lock:
            self._visible = False
        if self._hwnd:
            ctypes.windll.user32.ShowWindow(self._hwnd, SW_HIDE)

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
        finally:
            if self._hwnd:
                try:
                    ctypes.windll.user32.ShowWindow(self._hwnd,SW_HIDE)
                    ctypes.windll.user32.DestroyWindow(self._hwnd)
                except Exception: pass

    def _create_window(self) -> None:
        user32 = ctypes.windll.user32
        ctypes.windll.kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
        ctypes.windll.kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        hinstance = ctypes.windll.kernel32.GetModuleHandleW(None)
        user32.DestroyWindow.argtypes = [wintypes.HWND]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.CreateWindowExW.argtypes = [wintypes.DWORD,wintypes.LPCWSTR,wintypes.LPCWSTR,wintypes.DWORD,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,wintypes.HWND,wintypes.HMENU,wintypes.HINSTANCE,ctypes.c_void_p]
        user32.GetDC.argtypes = [wintypes.HWND]
        user32.GetDC.restype = wintypes.HDC
        user32.ReleaseDC.argtypes = [wintypes.HWND,wintypes.HDC]
        user32.ShowWindow.argtypes = [wintypes.HWND,ctypes.c_int]
        user32.UpdateLayeredWindow.argtypes = [wintypes.HWND,wintypes.HDC,ctypes.POINTER(wintypes.POINT),ctypes.POINTER(wintypes.SIZE),wintypes.HDC,ctypes.POINTER(wintypes.POINT),wintypes.DWORD,ctypes.POINTER(BLENDFUNCTION),wintypes.DWORD]
        class_name = f"DshDoraemonCommander_{os.getpid()}_{id(self)}"
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
            if msg == 0x0021: return 3  # WM_MOUSEACTIVATE -> MA_NOACTIVATE
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
        self._hwnd = user32.CreateWindowExW(ex_style, class_name, "Doraemon Commander", WS_POPUP,
                                            0, 0, 64, 64, None, None, hinstance, None)
        if not self._hwnd:
            raise ctypes.WinError()
        try:
            user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND,wintypes.DWORD]
            self._capture_excluded = bool(user32.SetWindowDisplayAffinity(self._hwnd,0x11))
        except Exception: pass
        self._bitmap = _pointer_bitmap()
        self._user32 = user32
        self._gdi32 = ctypes.windll.gdi32
        self._gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
        self._gdi32.CreateCompatibleDC.restype = wintypes.HDC
        self._gdi32.CreateDIBSection.argtypes = [wintypes.HDC,ctypes.POINTER(BITMAPINFO),wintypes.UINT,ctypes.POINTER(ctypes.c_void_p),wintypes.HANDLE,wintypes.DWORD]
        self._gdi32.CreateDIBSection.restype = wintypes.HBITMAP
        self._gdi32.SelectObject.argtypes = [wintypes.HDC,wintypes.HANDLE]
        self._gdi32.SelectObject.restype = wintypes.HANDLE
        self._gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
        self._gdi32.DeleteDC.argtypes = [wintypes.HDC]
        self._canvas = None
        self._last_paint = None
        self._last_render = None

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
        self._old_bitmap = gdi32.SelectObject(mem, dib)
        ctypes.memmove(bits, self._bitmap.ctypes.data, self._bitmap.nbytes)
        self._bits = bits
        user32.ReleaseDC(None, screen)
        self._canvas = (mem, dib, width, height)

    def _paint(self, x: int, y: int) -> None:
        if self._canvas is not None and self._canvas[2:] != (self._bitmap.shape[1], self._bitmap.shape[0]):
            mem, dib, _, _ = self._canvas
            self._gdi32.SelectObject(mem, self._old_bitmap)
            self._gdi32.DeleteObject(dib)
            self._gdi32.DeleteDC(mem)
            self._canvas = None
        self._ensure_canvas()
        pixels = self._bitmap.copy()
        pixels[:, :, :3] = ((pixels[:, :, :3].astype(np.uint16) * pixels[:, :, 3:4] + 127) // 255).astype(np.uint8)
        ctypes.memmove(self._bits, pixels.ctypes.data, pixels.nbytes)
        mem, _, width, height = self._canvas
        user32 = self._user32
        screen = user32.GetDC(None)
        try:
            blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
            dst = wintypes.POINT(x - MARGIN, y - MARGIN)
            src = wintypes.POINT(0, 0)
            size = wintypes.SIZE(width, height)
            if not user32.UpdateLayeredWindow(self._hwnd, screen, ctypes.byref(dst), ctypes.byref(size),
                                             mem, ctypes.byref(src), 0, ctypes.byref(blend), ULW_ALPHA):
                raise ctypes.WinError()
        finally:
            user32.ReleaseDC(None, screen)

    def _loop(self) -> None:
        user32 = self._user32
        msg = wintypes.MSG()
        pos = None
        while True:
            with self._lock:
                now = time.monotonic()
                self._expire_tasks(now)
                target, visible = self._target, self._visible and not self._capture_depth
                action = self._resolved_action(now)
                style = (self._theme, self._palette, self._scale)
                greeting = '终于轮到我玩电脑了！' if self._tasks and now-self._born < 2 else None
                progress = min(1.0,max(0.0,(now-self._born)/.9))
                if visible and self._origin and target and progress < 1:
                    t = progress*progress*(3-2*progress)
                    target = (self._origin[0]+(target[0]-self._origin[0])*t,
                              self._origin[1]+(target[1]-self._origin[1])*t)
            from .mascot_theme import render
            frame = int(time.monotonic() * 4) % 4
            render_key = (action, frame, greeting)
            # Re-rendering at 60 fps costs CPU for nothing: the sprite only
            # changes when the action/frame/palette does.
            if render_key != self._last_render:
                self._bitmap = render(*render_key)
                self._last_render = render_key
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
                paint_key = (painted, action, frame, style)
                if paint_key != self._last_paint:
                    self._paint(painted[0], painted[1])
                    self._last_paint = paint_key
                if not self._shown:
                    user32.ShowWindow(self._hwnd, SW_SHOWNOACTIVATE)
                    self._shown = True
            if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            time.sleep(FRAME_SECONDS)
