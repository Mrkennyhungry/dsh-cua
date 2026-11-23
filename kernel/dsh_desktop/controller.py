"""App controller — multi-app agent-facing services."""
from __future__ import annotations

import base64
import logging
import subprocess
import time
from datetime import datetime
from pathlib import Path

import cv2
import psutil
import win32gui
import win32process

from .tool_decorator import tool, register_all_tools

from .config import Config, AppConfig
from .file_search import search_files
from .game import process
from .mem_track import mem_tag
from .powershell import PowerShellService


logger = logging.getLogger(__name__)

from .contracts import IMAGE_PATH_KEY, SOM_IMAGE_PATH_KEY, extract_image_path
from .runtime import DesktopRuntime, _overlay_label_text


class AppController(DesktopRuntime):
    """Bundled app services for multiple app instances.

    Usage:
        ac = AppController(config)"""

    TOOLSET = "app_controller"

    def __init__(self, config: Config):
        super().__init__(config)
        self._processes: dict[str, process.AppProcessManager] = {}
        self.powershell = PowerShellService()

    # ── Per-app helpers ────────────────────────────────────────────────

    def _get_process(self, app: str) -> process.AppProcessManager:
        if app not in self._processes:
            ac = self.config.apps.get(app, AppConfig())
            self._processes[app] = process.AppProcessManager(
                self.config.get_app_config(app), timeout=ac.launch_timeout,
            )
        return self._processes[app]

    # ── Process status ─────────────────────────────────────────────────

    def is_app_running(self, app: str) -> bool:
        return self._get_process(app).is_app_running

    def is_launcher_running(self, app: str) -> bool:
        return self._get_process(app).is_launcher_running

    # ── Window discovery ────────────────────────────────────────────────

    def find_app_window(self, app: str) -> int | None:
        p = self.config.get_app_config(app)
        return self.window.find_by_path_and_class(p.app_path)

    def find_launcher_window(self, app: str) -> int | None:
        p = self.config.get_app_config(app)
        if not p.launcher_path:
            return None
        return self.window.find_by_path_and_class(p.launcher_path)

    # ── Window picker ───────────────────────────────────────────────────


    @tool("Close a window. Sends WM_CLOSE first (graceful), then terminates, then kills as last resort.")
    def close_window(self, hwnd: int) -> dict:
        """
        Args:
            hwnd: Window handle to close.
        """
        if not self.window.is_valid(hwnd):
            return {"success": False, "error": f"Invalid window handle: {hwnd}"}

        title = win32gui.GetWindowText(hwnd)
        _, pid = win32process.GetWindowThreadProcessId(hwnd)

        # Stage 1: WM_CLOSE
        try:
            win32gui.PostMessage(hwnd, 0x0010, 0, 0)  # WM_CLOSE
            logger.info("close_window: WM_CLOSE sent to hwnd=%d '%s'", hwnd, title)
        except Exception:
            pass

        # Wait for graceful close
        for _ in range(20):
            time.sleep(0.25)
            if not self.window.is_valid(hwnd):
                return {"success": True, "method": "wm_close", "title": title, "pid": pid}

        # Stage 2: terminate process
        try:
            proc = psutil.Process(pid)
            proc.terminate()
            proc.wait(timeout=5)
            logger.info("close_window: terminated pid=%d", pid)
            return {"success": True, "method": "terminate", "title": title, "pid": pid}
        except psutil.TimeoutExpired:
            pass
        except Exception as e:
            logger.warning("close_window: terminate failed: %s", e)

        # Stage 3: kill
        try:
            proc = psutil.Process(pid)
            proc.kill()
            proc.wait(timeout=3)
            logger.info("close_window: killed pid=%d", pid)
            return {"success": True, "method": "kill", "title": title, "pid": pid}
        except Exception as e:
            return {"success": False, "error": f"Failed to close: {e}"}


    # ── Agent tool primitives ──────────────────────────────────────────

    @tool("List all configured apps with their details (name, app_path, launcher_path, launch_timeout). Use before launch(app=...).")
    def list_apps(self) -> dict:
        apps = []
        for name, ac in self.config.apps.items():
            apps.append({
                "name": name,
                "app_path": ac.app_path,
                "launcher_path": ac.launcher_path,
                "launch_timeout": ac.launch_timeout,
            })
        apps.sort(key=lambda a: str(a["name"]))
        return {"apps": apps}

    @tool("Capture a window, run OCR + YOLO detection, save screenshot. Returns image_path, OCR elements with normalized [0,1000] bbox, and dimensions.")
    def analyze(self, hwnd: int) -> dict:
        """
        Args:
            hwnd: Window handle to capture.
        """
        if not self.window.is_valid(hwnd):
            return {"error": f"Invalid window handle: {hwnd}"}

        frame = self.capture.capture(hwnd)
        if frame is None:
            logger.info("analyze: capture failed")
            return {"error": "Capture failed"}

        h, w = frame.shape[:2]
        max_dim = self.config.workspace.screenshot_max_dim
        if w > max_dim or h > max_dim:
            scale = max_dim / max(w, h)
            compressed = cv2.resize(frame, (int(w * scale), int(h * scale)))
        else:
            compressed = frame

        date_dir = self._screenshot_dir / datetime.now().strftime("%Y-%m-%d")
        date_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        name = f"{hwnd}_{ts}"
        path = str(date_dir / f"{name}.jpeg")
        ok, buf = cv2.imencode(".jpeg", compressed)
        if ok:
            with open(path, "wb") as f:
                f.write(buf.tobytes())

        parsed = self.ui_parser.parse(frame)
        logger.info("analyze: found %d ui_elements", len(parsed))

        bbox_path = str(date_dir / f"{name}_bbox.jpeg")
        self._save_bbox_overlay(compressed, parsed, bbox_path, hwnd=hwnd)

        # Get mouse position relative to window client area
        mouse_pos = self._get_mouse_position(hwnd)

        # Memory tracking after first analyze
        if not hasattr(self, '_analyze_mem_logged'):
            mem_tag("after first analyze")
            self._analyze_mem_logged = True

        return {
            "width": compressed.shape[1],
            "height": compressed.shape[0],
            "ui_elements": parsed,
            IMAGE_PATH_KEY: path,
            SOM_IMAGE_PATH_KEY: bbox_path,
            "mouse_position": mouse_pos,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }


    # Adapters cap a single inline image at ~5 MB. A 4K PNG is ~7.8 MB and was
    # silently dropped, leaving the caller with only a warning. Re-encode at
    # generation time instead of relying on the caller to shrink it.
    INLINE_IMAGE_MAX_BYTES = 4_500_000
    INLINE_IMAGE_MAX_DIM = 1920

    @tool("Read an image file from disk and return base64 content for vision model analysis. Use with image_path from analyze().")
    def read_image(self, path: str) -> dict:
        """
        Args:
            path: Path to the image file.
        """
        p = Path(path)
        if not p.exists():
            logger.info("read_image: file not found")
            return {"error": f"File not found: {path}"}

        image_bytes = p.read_bytes()
        suffix = p.suffix.lower().lstrip(".")
        mime = "jpeg" if suffix in ("jpg", "jpeg") else suffix

        if len(image_bytes) > self.INLINE_IMAGE_MAX_BYTES:
            # Downscale to a 1920 long edge and encode JPEG. Keep trying a lower
            # quality rather than ever returning a payload the adapter rejects.
            frame = cv2.imread(str(p), cv2.IMREAD_UNCHANGED)
            if frame is None:
                return {"error": f"IMAGE_DECODE_FAILED: {path}"}
            # JPEG cannot hold an alpha channel; drop it (or promote grayscale).
            if frame.ndim == 2:
                frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
            elif frame.shape[2] == 4:
                frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
            h, w = frame.shape[:2]
            if max(h, w) > self.INLINE_IMAGE_MAX_DIM:
                scale = self.INLINE_IMAGE_MAX_DIM / max(h, w)
                frame = cv2.resize(frame, (max(1, round(w * scale)), max(1, round(h * scale))))
            for quality in (85, 70, 55):
                ok, buf = cv2.imencode(".jpeg", frame,
                                       [int(cv2.IMWRITE_JPEG_QUALITY), quality])
                if ok and len(buf) <= self.INLINE_IMAGE_MAX_BYTES:
                    image_bytes, mime = buf.tobytes(), "jpeg"
                    break
            else:
                return {"error": f"IMAGE_TOO_LARGE: {len(image_bytes)} bytes after re-encode"}
            logger.info("read_image: re-encoded %d -> %d bytes (mime=%s)",
                        p.stat().st_size, len(image_bytes), mime)
        else:
            logger.info("read_image: mime=%s, size=%d bytes", mime, len(image_bytes))

        image_b64 = base64.b64encode(image_bytes).decode()
        return {
            "_multimodal": True,
            "text_summary": f"{path}",
            "content": [
                {"type": "text", "text": f"Screenshot from path: {path}"},
                {"type": "image_url", "image_url": {"url": f"data:image/{mime};base64,{image_b64}"}},
            ],
        }

    @tool("Click at normalized [0,1000] coordinates. (0,0)=top-left, (1000,1000)=bottom-right. Set clicks=2 for double-click.")
    def click(self, x: int, y: int, hwnd: int, clicks: int = 1, reason: str = "") -> dict:
        """
        Args:
            x: X coordinate in [0, 1000].
            y: Y coordinate in [0, 1000].
            hwnd: Window handle.
            clicks: Number of clicks (default 1, 2 for double-click).
            reason: Optional reason for logging.
        """
        if not self.window.is_valid(hwnd):
            return {"success": False, "error": f"Invalid window handle: {hwnd}"}

        with self._input_lock:
            result = self.input.click_normalized(hwnd, x, y, clicks=clicks)
        return result

    def _require_foreground(self, hwnd: int) -> dict | None:
        """Foreground CHECK for keyboard actions — never activates.

        Focus design: activate (switch_to_window_tool) is a standalone step at
        the START; press/type are pure keybd_event and must not re-activate.

        Delegates to game.window.is_effectively_foreground — the single
        source of truth also used by ensure_foreground (the activation-skip
        gate for mouse/capture), so this check can't drift out of sync with
        what actually gets activated.
        """
        import win32gui
        from .game.window import is_effectively_foreground
        if is_effectively_foreground(hwnd):
            return None
        fg_title = ""
        try:
            fg_title = win32gui.GetWindowText(win32gui.GetForegroundWindow())[:60]
        except Exception:
            pass
        return {
            "success": False,
            "error": (
                "TARGET_NOT_FOREGROUND: 目标窗口不是前台窗口（按键会打到别的窗口）。"
                "先用 switch_to_window_tool 激活它，或点击目标输入框后重试。"
            ),
            "foreground_title": fg_title,
        }

    @tool("Press a key (pure keybd_event, NO window activation). Requires the window to be foreground already — activate it first (switch_to_window_tool) or click its input. Supports pyautogui key names.")
    def press_key(self, key: str, hwnd: int, wait_time: float = 0.2) -> dict:
        """
        Args:
            key: Key name (e.g. 'enter', 'escape', 'w', 'f1', 'space').
            hwnd: Window handle.
            wait_time: How long to hold the key in seconds (default 0.2).
        """
        if not self.window.is_valid(hwnd):
            return {"success": False, "error": f"Invalid window handle: {hwnd}"}
        blocked = None if self.input.virtual else self._require_foreground(hwnd)
        if blocked:
            return blocked

        with self._input_lock:
            result = self.input.press_key(key, wait_time, hwnd=hwnd)
            if isinstance(result, dict) and result.get("success") is False: return result
        return {"success": True, "key": key}

    # VM environment: composite hotkeys are unreliable (host key events leak in),
    # so only copy/paste-class shortcuts are allowed. Everything else is refused
    # with guidance to click the UI element instead.
    HOTKEY_ALLOWLIST = {
        ("ctrl", "c"): "copy selected text",
        ("ctrl", "v"): "paste clipboard text",
        ("ctrl", "a"): "select all text in focused input",
    }

    @tool("Press a combination of keys. VM-safe allowlist ONLY: Ctrl+C, Ctrl+V, Ctrl+A. Other combos (Alt+*, Win+*, F-keys…) are refused — click the UI element instead, or use type_text. Example: keys=['ctrl','c'].")
    def hotkey(self, keys: list[str], hwnd: int) -> dict:
        """
        Args:
            keys: List of key names, e.g. ['ctrl', 'c'].
            hwnd: Window handle.
        """
        if not self.window.is_valid(hwnd):
            return {"success": False, "error": f"Invalid window handle: {hwnd}"}

        normalized = tuple(str(k).lower().strip() for k in (keys or []) if str(k).strip())
        if normalized not in self.HOTKEY_ALLOWLIST:
            pretty = "+".join(k.capitalize() for k in normalized) if normalized else "(empty)"
            return {
                "success": False,
                "error": (
                    f"HOTKEY_BLOCKED: 组合键 {pretty} 在虚拟机环境不可靠，已被禁用。"
                    "请改为点击界面上的对应菜单/按钮元素完成同样操作；"
                    "复制粘贴文本可用 Ctrl+C / Ctrl+V。"
                ),
                "allowed": ["ctrl+c", "ctrl+v", "ctrl+a"],
            }
        blocked = None if self.input.virtual else self._require_foreground(hwnd)
        if blocked:
            return blocked

        with self._input_lock:
            result = self.input.hotkey(*normalized, hwnd=hwnd)
            if isinstance(result, dict) and result.get("success") is False: return result
        return {"success": True, "keys": list(normalized), "purpose": self.HOTKEY_ALLOWLIST[normalized]}

    @tool("Scroll the mouse wheel at a position. Coordinates normalized [0,1000]. Positive=up/right, negative=down/left.")
    def scroll(self, x: int, y: int, clicks: int, hwnd: int,
               direction: str = "vertical", reason: str = "") -> dict:
        """
        Args:
            x: X coordinate in [0, 1000].
            y: Y coordinate in [0, 1000].
            clicks: Scroll amount. Positive=up/right, negative=down/left.
            hwnd: Window handle.
            direction: 'vertical' (default) or 'horizontal'.
            reason: Optional reason.
        """
        if not self.window.is_valid(hwnd):
            return {"success": False, "error": f"Invalid window handle: {hwnd}"}

        region = self.window.get_client_region(hwnd)
        if region is None:
            return {"success": False, "error": "Window client region not available"}

        abs_x = region.left + int(x / 1000 * region.width)
        abs_y = region.top + int(y / 1000 * region.height)

        blocked = None if self.input.virtual else self._require_foreground(hwnd)
        if blocked:
            return blocked
        with self._input_lock:
            result = self.input.scroll(abs_x, abs_y, clicks, direction, hwnd=hwnd)
        return result

    @tool("Type text into the focused input via clipboard paste (Ctrl+V). Supports Unicode/CJK. NO activation — requires the window to be foreground already (activate first or click the input).")
    def type_text(self, text: str, hwnd: int) -> dict:
        """
        Args:
            text: The text string to type.
            hwnd: Window handle.
        """
        if not self.window.is_valid(hwnd):
            return {"success": False, "error": f"Invalid window handle: {hwnd}"}
        blocked = None if self.input.virtual else self._require_foreground(hwnd)
        if blocked:
            return blocked

        with self._input_lock:
            time.sleep(0.1)
            result = self.input.type_text(text, hwnd=hwnd)
        return result

    @tool("Drag from one point to another using natural mouse simulation. Coordinates normalized [0,1000].", name="drag")
    def swipe_screen(self, x1: int, y1: int, x2: int, y2: int, hwnd: int, speed: float = 1.0) -> dict:
        """
        Args:
            x1: Start X coordinate (0-1000).
            y1: Start Y coordinate (0-1000).
            x2: End X coordinate (0-1000).
            y2: End Y coordinate (0-1000).
            hwnd: Window handle.
            speed: Swipe speed multiplier (default 1.0).
        """
        if not self.window.is_valid(hwnd):
            return {"success": False, "error": f"Invalid window handle: {hwnd}"}

        region = self.window.get_client_region(hwnd)
        if region is None:
            return {"success": False, "error": "Window client region not available"}

        with self._input_lock:
            if not self.input.virtual: self._force_foreground(hwnd)
            abs_x1 = region.left + int(x1 / 1000 * region.width)
            abs_y1 = region.top + int(y1 / 1000 * region.height)
            abs_x2 = region.left + int(x2 / 1000 * region.width)
            abs_y2 = region.top + int(y2 / 1000 * region.height)
            result = self.input.swipe_screen((abs_x1, abs_y1), (abs_x2, abs_y2), speed=speed, hwnd=hwnd)
            if isinstance(result, dict) and result.get("success") is False: return result
        return {"success": True, "from": [x1, y1], "to": [x2, y2]}

    @tool("Move mouse cursor to normalized [0,1000] coordinates. Brings the window to foreground.")
    def move_mouse(self, x: int, y: int, hwnd: int) -> dict:
        """
        Args:
            x: X coordinate in [0, 1000].
            y: Y coordinate in [0, 1000].
            hwnd: Window handle.
        """
        if not self.window.is_valid(hwnd):
            return {"success": False, "error": f"Invalid window handle: {hwnd}"}

        region = self.window.get_client_region(hwnd)
        if region is None:
            return {"success": False, "error": "Window client region not available"}

        with self._input_lock:
            if not self.input.virtual: self._force_foreground(hwnd)
            abs_x = region.left + int(x / 1000 * region.width)
            abs_y = region.top + int(y / 1000 * region.height)
            self.input.mouse_move_screen(abs_x, abs_y, hwnd=hwnd)
        return {"success": True, "x": x, "y": y}

    @tool("Start a program and wait for its window. Returns hwnd. Use app='name' for registered apps or exe='path' for direct launch.")
    def launch(self, app: str | None = None, exe: str | None = None) -> dict:
        """
        Args:
            app: Registered app name (uses launcher flow).
            exe: Absolute path to an executable to start directly.
        """
        if exe and not app:
            # Direct exe mode: start and find window by exe name
            exe_name = Path(exe).name
            logger.info("launch: starting exe directly: %s", exe)
            try:
                subprocess.Popen([exe])
            except Exception as e:
                return {"error": f"Failed to start executable: {e}"}

            # Wait for a window with matching exe
            deadline = time.time() + 30
            while time.time() < deadline:
                result = self._window_picker.find_window(exe=exe_name)
                if result:
                    hwnd = result["hwnd"]
                    self._force_foreground(hwnd)
                    return {"status": "ready", "hwnd": hwnd, "title": result["title"], "exe": exe_name}
                time.sleep(1)
            return {"error": f"Process started but no window found within 30s for '{exe_name}'"}

        if not app:
            return {"error": "Either 'app' or 'exe' must be provided"}

        # App mode: launcher flow
        if self.is_app_running(app):
            # App already running, find its window
            hwnd = self.find_app_window(app)
            if hwnd:
                return {"status": "already_running", "hwnd": hwnd, "message": f"{app} is already running"}
            return {"status": "already_running", "message": f"{app} process is running but window not found"}

        if not self.is_launcher_running(app):
            logger.info("launch: starting launcher for %s", app)
            err = self._start_launcher(app)
            if err is not None:
                return {"error": f"Failed to start launcher: {err}"}

        hwnd = self._wait_for_launcher_window(app, timeout=30)
        if hwnd is None:
            return {"error": f"Launcher started but window not detected within 30s for '{app}'"}

        self._force_foreground(hwnd)
        title = win32gui.GetWindowText(hwnd)
        return {
            "status": "launcher_ready",
            "hwnd": hwnd,
            "title": title,
            "message": "Launcher is ready. Use analyze(hwnd) to find Start button, click it, then use find_window(exe=...) to find the app window.",
        }

    @tool("Wait/sleep for a specified duration.")
    def wait(self, seconds: float, reason: str = "") -> dict:
        """
        Args:
            seconds: Number of seconds to wait.
            reason: Optional reason for logging.
        """
        time.sleep(seconds)
        return {"status": "waited", "seconds": seconds}

    @staticmethod
    def _text_similarity(a: str, b: str) -> float:
        """Simple character-level similarity ratio (0.0-1.0)."""
        if not a or not b:
            return 0.0
        a_lower, b_lower = a.lower(), b.lower()
        if a_lower in b_lower or b_lower in a_lower:
            return 1.0
        matches = sum(1 for x, y in zip(a_lower, b_lower) if x == y)
        return matches / max(len(a_lower), len(b_lower))

    @tool("Poll the screen via OCR until text appears or timeout. More efficient than wait()+analyze() loops.")
    def wait_for(self, text: str, hwnd: int,
                 timeout: float = 90, interval: float = 5,
                 threshold: float = 0.7) -> dict:
        """
        Args:
            text: Target text to search for (fuzzy match).
            hwnd: Window handle to poll.
            timeout: Maximum seconds to wait (default 90).
            interval: Seconds between polls (default 5).
            threshold: Minimum similarity ratio for fuzzy match (default 0.7).
        """
        t0 = time.time()

        while True:
            result = self.analyze(hwnd=hwnd)

            if "error" not in result:
                elements = result.get("ui_elements", [])
                best_match = None
                best_score = 0.0
                for item in elements:
                    detected = item.get("text", "")
                    if not detected:
                        continue
                    score = self._text_similarity(text, detected)
                    if score > best_score:
                        best_score = score
                        best_match = item

                if best_match and best_score >= threshold:
                    elapsed = time.time() - t0
                    return {
                        "found": True,
                        "text": best_match.get("text"),
                        "similarity": round(best_score, 3),
                        "element": best_match,
                        "elapsed": round(elapsed, 1),
                    }

            elapsed = time.time() - t0
            if elapsed >= timeout:
                return {"found": False, "error": f"timeout after {elapsed:.0f}s", "elapsed": round(elapsed, 1)}

            time.sleep(interval)

    @tool("Search for files on the system by name. Supports wildcards (* and ?).", name="find_files", param_aliases={"directory": "path", "folder": "path"})
    def search_files(self, query: str, path: str | None = None, limit: int = 20) -> dict:
        """
        Args:
            query: Filename pattern, e.g. '*.exe', 'config*'.
            path: Directory to search in (default: user home).
            limit: Maximum results to return (default 20).
        """
        return search_files(query=query, path=path, limit=limit)

    @tool("Execute a PowerShell command and return output. Use for system administration, file operations, or querying Windows APIs.")
    def run_powershell(self, command: str, timeout: float = 30) -> dict:
        """
        Args:
            command: PowerShell command to execute.
            timeout: Maximum seconds to wait (default 30).
        """
        return self.powershell.execute(command, timeout=timeout)

    @tool("Read text content from a file with line numbers and pagination. Use offset/limit for large files.", param_aliases={"file_path": "path", "filepath": "path", "filename": "path"})
    def read_file(self, path: str, offset: int = 0, limit: int = 500, encoding: str = "utf-8") -> dict:
        """
        Args:
            path: Absolute path to the file.
            offset: Line number to start from (0-indexed, default 0).
            limit: Maximum lines to return (default 500).
            encoding: File encoding (default utf-8).
        """
        p = Path(path)
        if not p.exists():
            return {"error": f"File not found: {path}"}
        if not p.is_file():
            return {"error": f"Not a file: {path}"}
        try:
            lines = p.read_text(encoding=encoding).splitlines(keepends=True)
            total = len(lines)
            selected = lines[offset:offset + limit]
            content = "".join(selected)
            result = {
                "path": str(p),
                "content": content,
                "total_lines": total,
                "offset": offset,
                "lines_returned": len(selected),
            }
            if offset + limit < total:
                result["hint"] = f"Use offset={offset + limit} to continue reading."
            return result
        except UnicodeDecodeError:
            return {"error": f"Cannot decode {path} as {encoding}, try a different encoding"}
        except Exception as e:
            return {"error": str(e)}

    @tool("Write text content to a file. Creates the file if it doesn't exist, overwrites if it does.", param_aliases={"file_path": "path", "filepath": "path", "filename": "path"})
    def write_file(self, path: str, content: str, encoding: str = "utf-8") -> dict:
        """
        Args:
            path: Absolute path to the file.
            content: Text content to write.
            encoding: File encoding (default utf-8).
        """
        p = Path(path)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding=encoding)
            return {"path": str(p), "size": p.stat().st_size, "success": True}
        except Exception as e:
            return {"error": str(e), "success": False}

    @tool("Find and replace text in a file. old_string must match exactly (unless replace_all=True). Use for targeted edits instead of rewriting the entire file.", param_aliases={"file_path": "path", "filepath": "path", "filename": "path", "old_text": "old_string", "new_text": "new_string"})
    def edit_file(self, path: str, old_string: str, new_string: str, replace_all: bool = False, encoding: str = "utf-8") -> dict:
        """
        Args:
            path: Absolute path to the file.
            old_string: Text to find.
            new_string: Replacement text.
            replace_all: Replace all occurrences (default False, requires unique match).
            encoding: File encoding (default utf-8).
        """
        p = Path(path)
        if not p.exists():
            return {"error": f"File not found: {path}", "success": False}
        if not p.is_file():
            return {"error": f"Not a file: {path}", "success": False}
        try:
            content = p.read_text(encoding=encoding)
            count = content.count(old_string)
            if count == 0:
                return {"error": f"old_string not found in {path}", "success": False}
            if count > 1 and not replace_all:
                return {"error": f"old_string found {count} times, not unique. Add more context or set replace_all=True.", "success": False}
            if replace_all:
                new_content = content.replace(old_string, new_string)
            else:
                new_content = content.replace(old_string, new_string, 1)
            p.write_text(new_content, encoding=encoding)
            return {"path": str(p), "replacements": count if replace_all else 1, "success": True}
        except UnicodeDecodeError:
            return {"error": f"Cannot decode {path} as {encoding}", "success": False}
        except Exception as e:
            return {"error": str(e), "success": False}

    @tool("Register an app executable for future use with launch(app=...). Persisted to config.", name="register_app")
    def register_app_tool(
        self,
        name: str,
        app_path: str,
        launcher_path: str | None = None,
        launch_timeout: int = 120,
    ) -> dict:
        """
        Args:
            name: Unique identifier for the app (e.g. 'nikke', 'mygame').
            app_path: Absolute path to the executable.
            launcher_path: Optional path to the launcher executable.
            launch_timeout: Timeout in seconds for launch (default 120).
        """
        ac = self.config.register_app(name, app_path, launcher_path, launch_timeout)
        return {
            "success": True,
            "name": name,
            "app_path": ac.app_path,
            "launcher_path": ac.launcher_path,
            "launch_timeout": ac.launch_timeout,
            "message": f"App '{name}' registered and persisted",
        }

    @tool("Remove a previously registered app.", name="unregister_app")
    def unregister_app_tool(self, name: str) -> dict:
        """
        Args:
            name: Name of the app to remove.
        """
        if self.config.delete_app(name):
            return {"success": True, "name": name, "message": f"App '{name}' removed"}
        return {"success": False, "error": f"App '{name}' not found"}

    # ── Tool registration ───────────────────────────────────────────────

    def register_tools(self) -> None:
        """Register all @tool-decorated methods into the hermes tool registry."""
        register_all_tools(self)

    # ── Private helpers ─────────────────────────────────────────────────


    def _start_launcher(self, app: str) -> str | None:
        """Start the launcher process.

        Returns:
            None on success, or an error message string on failure.
        """
        pm = self._get_process(app)
        if not pm.launcher:
            return "No launcher configured"
        return pm.launcher.start()

    def _wait_for_launcher_window(self, app: str, timeout: float = 30) -> int | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            hwnd = self.find_launcher_window(app)
            if hwnd:
                return hwnd
            time.sleep(1)
        return None
