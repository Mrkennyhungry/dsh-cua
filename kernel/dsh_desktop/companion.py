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
from pathlib import Path

logger = logging.getLogger(__name__)

_HOTKEY_PICK = "<ctrl>+<alt>+w"
_HOTKEY_STOP = "<ctrl>+<alt>+q"

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
        """Draw the menu with Tk.

        A Win32 TrackPopupMenu was tried first and never appeared: the mascot is
        WS_EX_NOACTIVATE (so clicking it never steals focus), and a popup menu
        requires its owner window to be activated. A Tk toplevel has no such
        requirement and is what the window picker already uses.
        """
        import tkinter as tk

        from . import desktop_tools as dt

        windows = [w for w in dt._list_visible_windows() if str(w.get("title", "")).strip()][:12]
        apps = _installed_apps()
        entries: list[tuple[str, str, object]] = [("header", "选择要操作的窗口", None)]
        for win in windows:
            entries.append(("window", f"{str(win.get('title'))[:38]}  ({win.get('exe', '')})", win))
        if apps:
            entries.append(("header", "启动应用并锁定", None))
            for app in apps:
                entries.append(("app", app["name"], app))
        locked = dt.locked_window_id()
        if locked:
            info = dt.bound_window_info(locked) or {}
            entries.append(("header", f"已锁定: {str(info.get('title', ''))[:26]}", None))
            entries.append(("action", "解除锁定", _ID_UNLOCK))
        entries.append(("action", "点选窗口…  (Ctrl+Alt+W)", _ID_PICK))
        entries.append(("action", "急停  (Ctrl+Alt+Q)", _ID_STOP))

        anchor = self._menu_anchor()

        def show() -> None:
            root = tk.Tk()
            root.overrideredirect(True)
            root.attributes("-topmost", True)
            root.configure(bg="#1c1c1e")
            frame = tk.Frame(root, bg="#1c1c1e", bd=0)
            frame.pack(fill="both", expand=True, padx=1, pady=1)
            choice: dict[str, object] = {"value": None}

            def pick(value) -> None:
                choice["value"] = value
                root.destroy()

            for kind, label, payload in entries:
                if kind == "header":
                    tk.Label(frame, text=label, bg="#1c1c1e", fg="#8e8e93", anchor="w",
                             font=("Segoe UI", 9), padx=10, pady=3).pack(fill="x")
                    continue
                row = tk.Label(frame, text=label, bg="#1c1c1e", fg="#f2f2f7", anchor="w",
                               font=("Segoe UI", 10), padx=12, pady=6, cursor="hand2")
                row.pack(fill="x")
                value = payload if kind == "action" else (kind, payload)
                row.bind("<Button-1>", lambda _e, v=value: pick(v))
                row.bind("<Enter>", lambda _e, w=row: w.configure(bg="#2c2c2e"))
                row.bind("<Leave>", lambda _e, w=row: w.configure(bg="#1c1c1e"))
            root.bind("<Escape>", lambda _e: root.destroy())
            root.bind_all("<Escape>", lambda _e: root.destroy())
            root.bind("<FocusOut>", lambda _e: root.destroy())
            root.after(30000, root.destroy)  # never leave a stuck panel behind
            root.update_idletasks()
            width, height = root.winfo_reqwidth(), root.winfo_reqheight()
            # Keep the whole panel on screen: docked at the right edge the naive
            # anchor pushed it hundreds of pixels past the monitor.
            screen_w = root.winfo_screenwidth()
            screen_h = root.winfo_screenheight()
            x = min(max(0, anchor[0]), max(0, screen_w - width - 8))
            y = min(max(0, anchor[1]), max(0, screen_h - height - 8))
            root.geometry(f"{width}x{height}+{x}+{y}")
            for delay in (60, 200, 500):
                root.after(delay, lambda: (root.lift(), root.attributes("-topmost", True),
                                           root.focus_force()))
            root.mainloop()
            return choice["value"]

        selected = show()
        if selected is None:
            return
        if isinstance(selected, int):
            self._apply(selected, {}, dt)
            return
        kind, payload = selected
        self._apply(-1, {"__direct__": (kind, payload)}, dt)

    def _menu_anchor(self) -> tuple[int, int]:
        import win32gui

        hwnd = int(getattr(self._overlay, "_hwnd", 0) or 0)
        try:
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        except Exception:
            return (200, 200)
        return (left - 40, bottom - 8)

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
        if "__direct__" in actions:
            kind, payload = actions["__direct__"]
        else:
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
