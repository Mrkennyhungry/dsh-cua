"""Companion mode: click the docked mascot to pick a window or launch an app.

The mascot lives in the kernel process, so this module can call the desktop tools
directly. Selecting an entry binds that window and locks the kernel to it
(window scope lock), which is what makes "只在某个窗口下执行" enforceable.

Hotkeys (global, best effort):
    Ctrl+Alt+W  open the window picker overlay (click the target window)
    Ctrl+Alt+Q  emergency stop: refuse all input and stop running sessions
"""
from __future__ import annotations

import logging
import threading
import time
from ctypes import wintypes
from pathlib import Path

logger = logging.getLogger(__name__)

_HOTKEY_PICK = "<ctrl>+<alt>+w"
_HOTKEY_STOP = "<ctrl>+<alt>+q"

_MF_STRING = 0x0000
_MF_SEPARATOR = 0x0800
_MF_GRAYED = 0x0001
_TPM_RETURNCMD = 0x0100

_ID_PICK = 900
_ID_UNLOCK = 901
_ID_STOP = 902
_ID_RESUME = 903


_SYSTEM_FOLDERS = {"accessibility", "system tools", "windows tools",
                   "windows powershell", "administrative tools"}


def _installed_apps(limit: int = 8) -> list[dict]:
    """Start-menu shortcuts: the same .lnk targets desktop_launch_app accepts.

    Sampling one shortcut per folder keeps the menu varied: sorting purely by path
    filled it with the Accessibility folder before anything the user installed.
    """
    roots = [
        Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs",
        Path("C:/ProgramData/Microsoft/Windows/Start Menu/Programs"),
    ]
    seen: set[str] = set()
    per_folder: dict[str, dict] = {}
    for root in roots:
        if not root.is_dir():
            continue
        try:
            for path in sorted(root.rglob("*.lnk")):
                name = path.stem.strip()
                if (not name or name.lower() in seen
                        or name.lower() in {"administrative tools", "windows tools",
                                            "windows system tools", "system tools"}):
                    continue
                relative = path.parent.relative_to(root)
                folder = relative.parts[0].lower() if relative.parts else ""
                if folder in _SYSTEM_FOLDERS:
                    continue
                # An uninstaller one click away from a mascot is a bad idea: reuse the
                # same danger check the launcher tool applies.
                try:
                    from . import desktop_tools as _dt

                    if _dt._is_dangerous_shortcut(str(path), name):
                        continue
                except Exception:  # noqa: BLE001 - a missing filter only widens the list
                    pass
                seen.add(name.lower())
                per_folder.setdefault(folder, {"name": name[:40], "path": str(path)})
        except OSError:
            continue
    return list(per_folder.values())[:limit]


class Companion:
    def __init__(self) -> None:
        self._eternity = None
        self._overlay = None
        self._hotkeys = None
        self._lock = threading.Lock()

    # ── lifecycle ────────────────────────────────────────────────────────
    def install(self, eternity) -> None:
        """Attach to the overlay and start the global hotkeys (idempotent)."""
        from .cursor_overlay import get_cursor_overlay

        with self._lock:
            if self._overlay is not None:
                return
            self._eternity = eternity
            overlay = get_cursor_overlay()
            overlay.set_companion(True, self.open_menu)
            self._overlay = overlay
        self._start_hotkeys()

    def _start_hotkeys(self) -> None:
        try:
            from pynput import keyboard
        except Exception as exc:  # noqa: BLE001 - hotkeys are optional
            logger.warning("global hotkeys unavailable: %s", exc)
            return
        try:
            listener = keyboard.GlobalHotKeys({
                _HOTKEY_PICK: self.pick_interactively,
                _HOTKEY_STOP: self.stop_everything,
            })
            listener.daemon = True
            listener.start()
            self._hotkeys = listener
            logger.info("global hotkeys active: Ctrl+Alt+W pick, Ctrl+Alt+Q stop")
        except Exception as exc:  # noqa: BLE001
            logger.warning("global hotkeys failed to start: %s", exc)

    # ── actions ──────────────────────────────────────────────────────────
    def open_menu(self) -> None:
        """Show the picker menu next to the mascot (called from a worker thread)."""
        try:
            self._menu()
        except Exception as exc:  # noqa: BLE001 - a broken menu must not kill the overlay
            logger.warning("companion menu failed: %s", exc)

    def _menu(self) -> None:
        import ctypes

        from . import desktop_tools as dt

        user32 = ctypes.windll.user32
        user32.CreatePopupMenu.restype = wintypes.HMENU
        user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
        user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                          ctypes.c_int, wintypes.HWND, ctypes.c_void_p]
        user32.DestroyMenu.argtypes = [wintypes.HMENU]

        hwnd = int(getattr(self._overlay, "_hwnd", 0) or 0)
        menu = user32.CreatePopupMenu()
        if not menu:
            return
        actions: dict[int, tuple] = {}
        next_id = 1

        windows = [w for w in dt._list_visible_windows()
                   if str(w.get("title", "")).strip()][:12]
        user32.AppendMenuW(menu, _MF_STRING | _MF_GRAYED, 0, "选择要操作的窗口")
        for win in windows:
            label = f"{str(win.get('title'))[:44]}  ({win.get('exe', '')})"
            user32.AppendMenuW(menu, _MF_STRING, next_id, label)
            actions[next_id] = ("window", win)
            next_id += 1
        user32.AppendMenuW(menu, _MF_SEPARATOR, 0, None)

        apps = _installed_apps()
        if apps:
            user32.AppendMenuW(menu, _MF_STRING | _MF_GRAYED, 0, "启动应用并锁定")
            for app in apps:
                user32.AppendMenuW(menu, _MF_STRING, next_id, app["name"])
                actions[next_id] = ("app", app)
                next_id += 1
            user32.AppendMenuW(menu, _MF_SEPARATOR, 0, None)

        user32.AppendMenuW(menu, _MF_STRING, _ID_PICK, "点选窗口…  (Ctrl+Alt+W)")
        locked = dt.locked_window_id()
        if locked:
            info = dt.bound_window_info(locked) or {}
            user32.AppendMenuW(menu, _MF_STRING | _MF_GRAYED, 0,
                               f"已锁定: {str(info.get('title', ''))[:30]}")
            user32.AppendMenuW(menu, _MF_STRING, _ID_UNLOCK, "解除锁定")
        from . import input_gate
        if input_gate.stop_engaged():
            user32.AppendMenuW(menu, _MF_STRING, _ID_RESUME, "解除急停")
        else:
            user32.AppendMenuW(menu, _MF_STRING, _ID_STOP, "急停  (Ctrl+Alt+Q)")

        x, y = self._menu_anchor(hwnd)
        try:
            user32.SetForegroundWindow(hwnd)
        except Exception:
            pass
        choice = user32.TrackPopupMenu(menu, _TPM_RETURNCMD, x, y, 0, hwnd, None)
        user32.DestroyMenu(menu)
        try:
            user32.PostMessageW(hwnd, 0x0000, 0, 0)  # WM_NULL, standard menu cleanup
        except Exception:
            pass
        if not choice:
            return
        self._apply(choice, actions, dt)

    def _menu_anchor(self, hwnd: int) -> tuple[int, int]:
        import win32gui

        try:
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        except Exception:
            return (200, 200)
        return (left, max(0, top + (bottom - top) - 40))

    def _apply(self, choice: int, actions: dict, dt) -> None:
        from . import input_gate

        if choice == _ID_PICK:
            self.pick_interactively()
            return
        if choice == _ID_UNLOCK:
            dt.unlock_window()
            self._say("unlock")
            return
        if choice == _ID_STOP:
            self.stop_everything()
            return
        if choice == _ID_RESUME:
            input_gate.clear_stop()
            self._say("idle")
            return
        kind, payload = actions.get(choice, (None, None))
        if kind == "window":
            bound = dt.desktop_pick_window(hwnd=int(payload["hwnd"]))
            if bound.get("window_id"):
                locked = dt.lock_window(bound["window_id"])
                logger.info("companion: locked %s -> %s", payload.get("title"), locked.get("success"))
                self._say("done", 0.6)
            else:
                logger.warning("companion: bind failed for %s", payload.get("hwnd"))
        elif kind == "app":
            started = dt.desktop_launch_app(str(payload["path"]))
            logger.info("companion: launched %s -> %s", payload.get("name"), started.get("success"))
            if started.get("success"):
                self._bind_launched(payload.get("name", ""), dt)

    def _bind_launched(self, name: str, dt) -> None:
        """Wait briefly for the launched app's window, then bind and lock it."""
        needle = name.lower()[:20]
        for _ in range(30):
            time.sleep(0.25)
            for win in dt._list_visible_windows():
                title = str(win.get("title", "")).lower()
                exe = str(win.get("exe", "")).lower()
                if needle and (needle in title or needle in exe):
                    bound = dt.desktop_pick_window(hwnd=int(win["hwnd"]))
                    if bound.get("window_id"):
                        dt.lock_window(bound["window_id"])
                        self._say("done", 0.6)
                    return
        logger.info("companion: launched app %r but no window matched yet", name)

    def pick_interactively(self, timeout_s: float = 60.0) -> None:
        """Show the full-screen overlay picker and lock whatever the user clicks."""
        from . import desktop_tools as dt

        eternity = self._eternity
        controller = getattr(eternity, "controller", None)
        if controller is None:
            logger.warning("companion: controller not ready")
            return
        controller.show_overlay_picker()
        deadline = time.monotonic() + max(5.0, timeout_s)
        while time.monotonic() < deadline:
            picked = controller.get_picked_window()
            if picked:
                bound = dt.desktop_pick_window(hwnd=int(picked["hwnd"]))
                if bound.get("window_id"):
                    dt.lock_window(bound["window_id"])
                    logger.info("companion: picked %s", picked.get("title"))
                    self._say("done", 0.6)
                return
            if not getattr(controller, "overlay_active", False):
                time.sleep(0.4)
                picked = controller.get_picked_window()
                if not picked:
                    logger.info("companion: picker cancelled")
                    return
            time.sleep(0.3)

    def stop_everything(self) -> None:
        """Emergency stop: no more input, and running sessions are asked to stop."""
        from . import input_gate

        input_gate.emergency_stop()
        eternity = self._eternity
        stopped = 0
        for session_id in list(getattr(eternity, "_sessions", {})):
            try:
                if eternity.stop_session(session_id):
                    stopped += 1
            except Exception:  # noqa: BLE001 - stop must reach every session it can
                continue
        logger.warning("companion: emergency stop, sessions stopped=%d", stopped)
        self._say("error", 1.5)

    def _say(self, action: str, duration: float = 0.8) -> None:
        try:
            if self._overlay is not None:
                self._overlay.action(action, duration=duration)
        except Exception:  # noqa: BLE001 - cosmetics only
            pass


_INSTANCE: Companion | None = None
_INSTANCE_LOCK = threading.Lock()


def get_companion() -> Companion:
    global _INSTANCE
    with _INSTANCE_LOCK:
        if _INSTANCE is None:
            _INSTANCE = Companion()
        return _INSTANCE


def install(eternity) -> Companion:
    companion = get_companion()
    companion.install(eternity)
    return companion
