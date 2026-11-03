"""Companion mode: click the docked mascot to pick a window or launch an app.

The mascot lives in the kernel process, so this module can call the desktop tools
directly. Selecting an entry binds that window and locks the kernel to it
(window scope lock), which is what makes "只在某个窗口下执行" enforceable.

Hotkeys (global, best effort):
    Ctrl+Alt+W  open the window picker overlay (click the target window)
    Ctrl+Alt+Q  emergency stop: refuse all input and stop running sessions
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import logging
import os
import queue
import subprocess
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_ID_PICK = 900
_ID_UNLOCK = 901
_ID_STOP = 902
_ID_RESUME = 903
_ID_CLOSE = 904


_SYSTEM_FOLDERS = {"accessibility", "system tools", "windows tools",
                   "windows powershell", "administrative tools"}


class _Win32Hotkeys:
    """Small RegisterHotKey message loop; no global keyboard hook is involved."""

    def __init__(self, actions: dict[str, object]) -> None:
        self._actions = actions
        self._thread = None
        self._thread_id = 0
        self._stop = False

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="companion-win32-hotkeys")
        self._thread.start()

    def stop(self) -> None:
        self._stop = True
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, 0x0012, 0, 0)  # WM_QUIT

    def _run(self) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        self._thread_id = kernel32.GetCurrentThreadId()
        registered: list[int] = []
        try:
            # MOD_ALT | MOD_CONTROL | MOD_NOREPEAT
            modifiers = 0x0001 | 0x0002 | 0x4000
            keys = list(self._actions)
            for hotkey_id, char in enumerate(keys, start=1):
                if not user32.RegisterHotKey(None, hotkey_id, modifiers, ord(char.upper())):
                    raise ctypes.WinError()
                registered.append(hotkey_id)
            message = wintypes.MSG()
            while not self._stop:
                result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result <= 0:
                    break
                if message.message == 0x0312:  # WM_HOTKEY
                    index = int(message.wParam) - 1
                    char = keys[index] if 0 <= index < len(keys) else ""
                    action = self._actions.get(char)
                    if action is not None:
                        threading.Thread(target=action, daemon=True,
                                         name=f"companion-hotkey-{char}").start()
        except Exception:  # noqa: BLE001 - hotkeys are optional, never kill the kernel
            logger.warning("Win32 hotkey loop failed", exc_info=True)
        finally:
            for hotkey_id in registered:
                user32.UnregisterHotKey(None, hotkey_id)


def _installed_apps(limit: int = 32) -> list[dict]:
    """Start-menu shortcuts accepted by desktop_launch_app.

    Take one entry per folder first for variety, then fill the second-level page
    with the remaining applications. Uninstallers and system utility folders stay
    out of the one-click launcher.
    """
    roots = [
        Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs",
        Path("C:/ProgramData/Microsoft/Windows/Start Menu/Programs"),
    ]
    seen: set[str] = set()
    per_folder: dict[str, dict] = {}
    remaining: list[dict] = []
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
                try:
                    from . import desktop_tools as _dt

                    if _dt._is_dangerous_shortcut(str(path), name):
                        continue
                except Exception:  # noqa: BLE001 - a missing filter only widens the list
                    pass
                seen.add(name.lower())
                item = {"name": name[:60], "path": str(path)}
                if folder not in per_folder:
                    per_folder[folder] = item
                else:
                    remaining.append(item)
        except OSError:
            continue
    return (list(per_folder.values()) + remaining)[:limit]


def _ui_executable() -> Path:
    """Published Arc picker, kept out of the source package and git history."""
    return (Path(__file__).resolve().parents[2] / ".runtime" / "companion-ui"
            / "DshDoraemon.Companion.exe")


class Companion:
    def __init__(self) -> None:
        self._eternity = None
        self._overlay = None
        self._hotkeys = None
        self._mouse_listener = None
        self._menu_open = False
        self._lock = threading.Lock()
        self._menu_guard = threading.Lock()
        self._ui_process = None
        self._ui_responses: queue.Queue = queue.Queue()
        self._client_pid: int | None = None
        self._apps_cache: tuple[float, list[dict]] = (0.0, [])
        self._windows_cache: tuple[float, list[dict]] = (0.0, [])

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
        self._start_outside_click_listener()
        self._start_ui_server()
        threading.Thread(target=self._prewarm, daemon=True, name="companion-prewarm").start()

    def watch_client(self, client_pid: int) -> None:
        """Tie companion UI to the DSH process, including abnormal client exits."""
        import psutil

        pid = int(client_pid)
        if pid <= 0:
            raise ValueError("client_pid must be positive")
        if self._client_pid == pid:
            return
        self._client_pid = pid

        def watch() -> None:
            while self._client_pid == pid and psutil.pid_exists(pid):
                time.sleep(1.0)
            if self._client_pid == pid:
                logger.info("DSH client %s exited; shutting down companion UI", pid)
                self.shutdown()

        threading.Thread(target=watch, daemon=True, name="companion-client-watch").start()

    def shutdown(self) -> None:
        """Detach all DSH-owned UI while allowing the kernel to stay resident."""
        with self._lock:
            overlay = self._overlay
            self._overlay = None
            self._eternity = None
            self._client_pid = None
        if overlay is not None:
            try:
                overlay.set_companion(False)
                overlay.hide()
            except Exception:  # noqa: BLE001 - shutdown must continue
                logger.debug("failed to hide companion overlay", exc_info=True)
        for listener in (self._hotkeys, self._mouse_listener):
            try:
                if listener is not None:
                    listener.stop()
            except Exception:  # noqa: BLE001
                logger.debug("failed to stop companion listener", exc_info=True)
        self._hotkeys = None
        self._mouse_listener = None
        process = self._ui_process
        self._ui_process = None
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=2)
            except Exception:  # noqa: BLE001 - include a hung Arc process
                try:
                    process.kill()
                except Exception:
                    pass
        self._menu_open = False
        try:
            from . import desktop_tools as dt

            dt.unlock_window()
        except Exception:  # noqa: BLE001
            logger.debug("failed to clear companion window lock", exc_info=True)

    def dismiss(self) -> None:
        """Hide only the mascot; keep its restore hotkey and the kernel alive."""
        overlay = self._overlay
        self._menu_open = False
        if overlay is not None:
            overlay.set_companion(False)
            overlay.hide()
        try:
            import win32con
            import win32gui

            def close_picker(hwnd, _):
                if win32gui.GetClassName(hwnd).startswith("Avalonia-") and win32gui.IsWindowVisible(hwnd):
                    win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
                return True

            win32gui.EnumWindows(close_picker, None)
        except Exception:  # noqa: BLE001 - hiding the mascot must still work
            logger.debug("failed to close picker while hiding mascot", exc_info=True)

    def toggle_mascot(self) -> None:
        overlay = self._overlay
        logger.info("companion: toggle mascot requested")
        if overlay is None:
            return
        # _visible is a task flag and may be False while companion mode still
        # keeps the window on screen. The companion flag is the real toggle state.
        if getattr(overlay, "_companion", False):
            self.dismiss()
        else:
            overlay.set_companion(True, self.open_menu)

    def _start_ui_server(self) -> None:
        """Start the Arc runtime once; each click then only shows a window."""
        if self._ui_process is not None and self._ui_process.poll() is None:
            return
        executable = _ui_executable()
        if not executable.is_file():
            logger.error("Arc companion UI is not built: %s (run scripts/build-companion-ui.ps1)",
                         executable)
            return
        try:
            self._ui_process = subprocess.Popen(
                [str(executable), "--server", str(os.getpid())],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except OSError as exc:
            logger.warning("Arc companion UI failed to start: %s", exc)
            self._ui_process = None
            return

        def read_responses() -> None:
            assert self._ui_process is not None and self._ui_process.stdout is not None
            for line in self._ui_process.stdout:
                if line.strip():
                    self._ui_responses.put(line)

        def read_errors() -> None:
            assert self._ui_process is not None and self._ui_process.stderr is not None
            for line in self._ui_process.stderr:
                if line.strip():
                    logger.warning("Arc companion UI: %s", line.strip())

        threading.Thread(target=read_responses, daemon=True, name="companion-ui-out").start()
        threading.Thread(target=read_errors, daemon=True, name="companion-ui-err").start()
        logger.info("Arc companion UI server started (pid=%s)", self._ui_process.pid)

    def _prewarm(self) -> None:
        from . import desktop_tools as dt

        try:
            self._window_items(dt)
            self._app_items()
            logger.info("companion picker data prewarmed")
        except Exception:  # noqa: BLE001 - a cache miss later is still valid
            logger.debug("companion prewarm failed", exc_info=True)

    def _app_items(self) -> list[dict]:
        now = time.monotonic()
        if now - self._apps_cache[0] > 60 or not self._apps_cache[1]:
            self._apps_cache = (now, _installed_apps())
        return self._apps_cache[1]

    def _window_items(self, dt) -> list[dict]:
        now = time.monotonic()
        if now - self._windows_cache[0] > 1.5 or not self._windows_cache[1]:
            items = [
                {"hwnd": int(w["hwnd"]), "title": str(w.get("title", "")),
                 "exe": str(w.get("exe", "")), "pid": int(w.get("pid") or 0)}
                for w in dt._list_visible_windows() if str(w.get("title", "")).strip()
            ][:50]
            self._windows_cache = (now, items)
        return self._windows_cache[1]

    def _start_outside_click_listener(self) -> None:
        """Close the visible Arc page on a click outside it, regardless of focus."""
        try:
            from pynput import mouse

            listener = mouse.Listener(on_click=self._on_global_mouse)
            listener.daemon = True
            listener.start()
            self._mouse_listener = listener
        except Exception as exc:  # noqa: BLE001 - the picker can still use Esc
            logger.warning("outside-click listener unavailable: %s", exc)

    def _on_global_mouse(self, x: int, y: int, button, pressed: bool) -> None:
        if not pressed or not self._menu_open:
            return
        try:
            import win32con
            import win32gui

            visible: list[tuple[int, tuple[int, int, int, int]]] = []

            def collect(hwnd, _):
                if (win32gui.GetClassName(hwnd).startswith("Avalonia-")
                        and win32gui.IsWindowVisible(hwnd)):
                    visible.append((hwnd, win32gui.GetWindowRect(hwnd)))
                return True

            win32gui.EnumWindows(collect, None)
            inside = any(left <= x < right and top <= y < bottom
                         for _, (left, top, right, bottom) in visible)
            if visible and not inside:
                for hwnd, _ in visible:
                    win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        except Exception:  # noqa: BLE001 - never let a UI helper break the hook
            logger.debug("outside-click handling failed", exc_info=True)

    def _start_hotkeys(self) -> None:
        try:
            listener = _Win32Hotkeys({
                "w": self.pick_interactively,
                "q": self.stop_everything,
                "m": self.toggle_mascot,
            })
            listener.start()
            self._hotkeys = listener
            logger.info("global hotkeys active: Ctrl+Alt+W pick, Ctrl+Alt+Q stop, Ctrl+Alt+M mascot")
        except Exception as exc:  # noqa: BLE001
            logger.warning("global hotkeys failed to start: %s", exc)

    # ── actions ──────────────────────────────────────────────────────────
    def open_menu(self) -> None:
        """Show the Arc picker next to the mascot (called from a worker thread)."""
        overlay = self._overlay
        if overlay is None or not overlay.companion_interactive():
            return
        if not self._menu_guard.acquire(blocking=False):
            return
        # This must happen before any cache work or IPC: the sprite acknowledges
        # the click immediately even on the first, not-yet-warm invocation.
        self._say("thinking", 5.0)
        self._menu_open = True
        logger.info("companion: picker requested")
        try:
            self._menu()
        except Exception as exc:  # noqa: BLE001 - a broken menu must not kill the overlay
            logger.warning("companion menu failed: %s", exc, exc_info=True)
            self._say("error", 1.5)
        finally:
            self._menu_open = False
            self._menu_guard.release()

    def _menu(self) -> None:
        """Send one request to the warm Arc process and apply its JSON response."""
        from . import desktop_tools as dt
        from . import input_gate

        self._start_ui_server()
        process = self._ui_process
        if process is None or process.poll() is not None or process.stdin is None:
            logger.warning("Arc companion UI is unavailable")
            self._say("error", 1.5)
            return
        selected_count = len(dt.locked_window_ids())
        anchor = self._menu_anchor()
        request = {
            "anchor_x": anchor[0],
            "anchor_y": anchor[1],
            "locked_label": (f"已授权 {selected_count} 个窗口/应用 · 点击清空"
                             if selected_count else ""),
            "stopped": input_gate.stop_engaged(),
            "windows": self._window_items(dt),
            "apps": self._app_items(),
        }
        while not self._ui_responses.empty():
            self._ui_responses.get_nowait()  # discard a response orphaned by a timeout
        process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
        process.stdin.flush()
        try:
            line = self._ui_responses.get(timeout=45)
        except queue.Empty:
            logger.warning("Arc companion UI did not answer in 45s")
            self._say("error", 1.5)
            return
        result = json.loads(line)
        kind = result.get("kind")
        if kind == "action":
            action = {"pick": _ID_PICK, "unlock": _ID_UNLOCK,
                      "stop": _ID_STOP, "resume": _ID_RESUME,
                      "close": _ID_CLOSE}.get(result.get("action"))
            if action is not None:
                self._apply(action, {}, dt)
        elif kind in {"window", "app"}:
            self._apply(-1, {"__direct__": (kind, result)}, dt)
        elif kind in {"windows", "apps"}:
            for item in result.get("items") or []:
                self._apply(-1, {"__direct__": (kind[:-1], item)}, dt)
            self._say("done", 0.6)
        elif kind == "error":
            logger.warning("Arc companion UI failed: %s", result.get("error"))
            self._say("error", 1.5)
        else:
            self._say("idle", 0.2)

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
        if choice == _ID_CLOSE:
            self.dismiss()
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
                tracked = (started.get("tracked_windows") or [{}])[0]
                if tracked.get("hwnd"):
                    bound = dt.desktop_pick_window(hwnd=int(tracked["hwnd"]))
                    if bound.get("window_id"):
                        dt.lock_window(bound["window_id"])
                        self._say("done", 0.6)
                        return
                self._bind_launched(payload.get("name")
                                    or Path(str(payload["path"])).stem, dt)

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


def shutdown() -> None:
    get_companion().shutdown()
