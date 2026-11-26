"""Screenshot capture by window handle."""
from __future__ import annotations

import logging
from contextlib import nullcontext
from dataclasses import dataclass

import cv2
import mss
import numpy as np

from . import window
from ..geometry import FrameGeometry

logger = logging.getLogger(__name__)


def window_geometry(hwnd):
    import ctypes
    import win32gui
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    x, y = win32gui.ClientToScreen(hwnd, (0, 0))
    client = win32gui.GetClientRect(hwnd)
    get_dpi = ctypes.windll.user32.GetDpiForWindow
    get_dpi.argtypes = [ctypes.c_void_p]
    get_dpi.restype = ctypes.c_uint
    dpi = int(get_dpi(hwnd)) or 96
    return {'window_rect': (left, top, right - left, bottom - top),
            'client_rect': (x, y, client[2] - client[0], client[3] - client[1]),
            'dpi': (dpi, dpi), 'virtual_origin': virtual_origin()}


def virtual_origin():
    import win32api
    return (win32api.GetSystemMetrics(76), win32api.GetSystemMetrics(77))


@dataclass
class CapturedFrame:
    image: np.ndarray
    geometry: FrameGeometry
    occluded: bool = False


def _cursor_guard():
    from ..cursor_overlay import peek_cursor_overlay
    overlay = peek_cursor_overlay()
    return overlay.capture_guard() if overlay is not None else nullcontext()


class CaptureService:
    """Stateless screenshot capture for a window client area."""

    def __init__(self, window_service: window.WindowService | None = None):
        self.window = window_service or window.WindowService()

    def capture(self, hwnd: int, *, activate: bool = True) -> np.ndarray | None:
        """Capture a window client area as a BGR image."""
        if not self.window.is_valid(hwnd):
            logger.error("Capture failed: invalid hwnd=%r", hwnd)
            return None

        try:
            if activate:
                self.window.ensure_foreground(hwnd)

            region = self.window.get_client_region(hwnd)
            if region is None:
                logger.error("Capture failed: hwnd=%d has no client region", hwnd)
                return None

            r = region.as_tuple()
            with _cursor_guard(), mss.mss() as sct:
                raw = sct.grab({"left": r[0], "top": r[1], "width": r[2], "height": r[3]})
            image = np.array(raw)

            return cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
        except Exception as e:
            # RDP 会话断开/锁屏时 BitBlt 会整体失败（mss 报 ScreenShotError），
            # 但 PrintWindow(PW_RENDERFULLCONTENT) 此时仍能出图 —— 断开不等于
            # 必须放弃端侧用例（2026-09-16/09-22 两次实测）。
            logger.warning("mss capture failed for hwnd=%d (%s) → PrintWindow fallback", hwnd, e)
            return self._capture_print_window(hwnd)

    def _capture_print_window(self, hwnd: int) -> np.ndarray | None:
        """PrintWindow 兜底：截整窗后裁到客户区（与 mss 路径同基准）。"""
        try:
            geometry = window_geometry(hwnd)
            left, top, _, _ = geometry['window_rect']
            image = self.print_window(hwnd)
            if image is None:
                return None
            region = self.window.get_client_region(hwnd)
            if region is not None:
                x = region.left - left
                y = region.top - top
                if min(x, y) < 0 or x + region.width > image.shape[1] or y + region.height > image.shape[0]:
                    raise RuntimeError('Client region changed during capture')
                image = image[y : y + region.height, x : x + region.width]
            if image.size == 0:
                return None
            return image
        except Exception as e:
            logger.error("PrintWindow capture failed for hwnd=%d: %s", hwnd, e)
            return None

    def print_window(self, hwnd):
        """Retain the whole-window origin and release GDI handles on every path."""
        import ctypes
        import win32gui
        import win32ui
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        width, height = right - left, bottom - top
        if min(width, height) <= 0:
            return None
        window_dc = win32gui.GetWindowDC(hwnd)
        source = memory = bitmap = None
        try:
            source = win32ui.CreateDCFromHandle(window_dc)
            memory = source.CreateCompatibleDC()
            bitmap = win32ui.CreateBitmap()
            bitmap.CreateCompatibleBitmap(source, width, height)
            memory.SelectObject(bitmap)
            ctypes.windll.user32.PrintWindow(hwnd, memory.GetSafeHdc(), 2)
            info = bitmap.GetInfo()
            bits = bitmap.GetBitmapBits(True)
            return np.frombuffer(bits, dtype=np.uint8).reshape(info['bmHeight'], info['bmWidth'], 4)[:, :, :3].copy()
        finally:
            if bitmap is not None:
                win32gui.DeleteObject(bitmap.GetHandle())
            if memory is not None:
                memory.DeleteDC()
            if source is not None:
                source.DeleteDC()
            win32gui.ReleaseDC(hwnd, window_dc)

    def capture_detailed(self, hwnd=None, *, whole_window=True):
        """Capture pixels together with their actual source coordinate system."""
        if hwnd is None:
            with _cursor_guard(), mss.mss() as sct:
                monitor = sct.monitors[0]
                raw = sct.grab(monitor)
            image = cv2.cvtColor(np.array(raw), cv2.COLOR_BGRA2BGR)
            source = tuple(monitor[key] for key in ('left', 'top', 'width', 'height'))
            return CapturedFrame(image, FrameGeometry(source, (image.shape[1], image.shape[0]),
                                                       virtual_origin=source[:2]))
        geometry = window_geometry(hwnd)
        image = None
        method = 'printwindow'
        if whole_window:
            try:
                image = self.print_window(hwnd)
            except Exception:
                logger.debug('Whole-window capture failed', exc_info=True)
        if image is not None and image.size and int(image.max()) > 8:
            source = geometry['window_rect']
            occluded = False
        else:
            # The screen and PrintWindow client fallbacks share one client basis.
            region = geometry['client_rect']
            try:
                with _cursor_guard(), mss.mss() as sct:
                    raw = sct.grab(dict(zip(('left', 'top', 'width', 'height'), region)))
                image = cv2.cvtColor(np.array(raw), cv2.COLOR_BGRA2BGR)
                method = 'screen'
                occluded = True
            except Exception:
                image = self._capture_print_window(hwnd)
                method = 'printwindow_client'
                occluded = False
            source = region
        if image is None or not image.size:
            raise RuntimeError('CAPTURE_EMPTY')
        if (image.shape[1], image.shape[0]) != source[2:]:
            raise RuntimeError('CAPTURE_GEOMETRY_CHANGED')
        return CapturedFrame(image, FrameGeometry(source, (image.shape[1], image.shape[0]),
                                                   method=method, **geometry), occluded)

    def capture_region(self, point, *, hwnd, radius=200):
        """Capture desktop composition around input, including separate popup HWNDs."""
        geometry = window_geometry(hwnd)
        x, y = (round(value) for value in point)
        with _cursor_guard(), mss.mss() as sct:
            desktop = sct.monitors[0]
            left = max(desktop['left'], x - radius)
            top = max(desktop['top'], y - radius)
            right = min(desktop['left'] + desktop['width'], x + radius)
            bottom = min(desktop['top'] + desktop['height'], y + radius)
            if right <= left or bottom <= top:
                raise RuntimeError('CLICK_OUTSIDE_DESKTOP')
            region = {'left': left, 'top': top, 'width': right - left, 'height': bottom - top}
            raw = sct.grab(region)
        image = cv2.cvtColor(np.array(raw), cv2.COLOR_BGRA2BGR)
        source = (left, top, right - left, bottom - top)
        return CapturedFrame(image, FrameGeometry(source, source[2:], crop=(0, 0, *source[2:]),
                                                   method='screen_region', **geometry), occluded=True)

    def capture_desktop(self, *, guard_cursor: bool = True) -> np.ndarray | None:
        """Capture the entire desktop (all monitors combined).

        guard_cursor=False 时不隐藏桌宠再截图。连拍（watch 10fps）必须走这条
        路：守卫每次都要 ShowWindow 隐藏 + 睡 32ms + 重新显示，10fps 就是每秒
        闪十次。省下来的隐藏由调用方自己把精灵所在矩形从 diff 里遮掉。
        """
        try:
            guard = _cursor_guard() if guard_cursor else nullcontext()
            with guard, mss.mss() as sct:
                monitor = sct.monitors[0]  # [0] = virtual screen spanning all monitors
                raw = sct.grab(monitor)
            return cv2.cvtColor(np.array(raw), cv2.COLOR_BGRA2BGR)
        except Exception as e:
            logger.error("Desktop capture failed: %s", e, exc_info=True)
            return None

    def save(self, hwnd: int, path: str, *, activate: bool = True) -> bool:
        """Capture and save screenshot to file. Returns True on success."""
        img = self.capture(hwnd, activate=activate)
        if img is None:
            return False
        return cv2.imwrite(path, img)
