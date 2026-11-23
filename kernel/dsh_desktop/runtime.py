"""Shared desktop services with on-demand vision and no planning model."""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass, fields, replace
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import psutil
import win32gui
import win32process

from .contracts import IMAGE_PATH_KEY, SOM_IMAGE_PATH_KEY
from .game import capture, input as input_mod, window
from .game.window_picker import WindowPicker, WindowPickerOverlay, _resolve_real_pid
from .tool_decorator import tool
from .config import WorkspaceConfig, ParserConfig
from .targets import TargetRegistry, FrameRegistry, RuntimeFault
from .browser import BrowserRuntime
from .window_identity import WindowIdentities
from .observations import ObservationService

logger = logging.getLogger(__name__)


@dataclass
class RuntimeConfig:
    workspace: WorkspaceConfig
    parser: ParserConfig

    @classmethod
    def from_home(cls, home: Path):
        import yaml

        path = home / 'config.yaml'
        raw = yaml.safe_load(path.read_text(encoding='utf-8')) if path.is_file() else {}
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ValueError('Runtime config must contain a mapping')
        workspace = {'screenshot_dir': str(home / 'screenshots'), 'weights_dir': str(home / 'weights')}
        allowed = {'screenshot_dir', 'weights_dir', 'screenshot_max_dim', 'action_settle_delay'}
        workspace.update({key: value for key, value in (raw.get('workspace') or {}).items() if key in allowed})
        for key in ('screenshot_dir', 'weights_dir'):
            value = Path(workspace[key]).expanduser()
            workspace[key] = str(value if value.is_absolute() else home / value)
        parser = {'engine': 'local-deepseek'}
        allowed = {field.name for field in fields(ParserConfig)}
        parser.update({key: value for key, value in (raw.get('parser') or {}).items() if key in allowed})
        return cls(WorkspaceConfig(**workspace), ParserConfig(**parser))


def _overlay_label_text(element: dict) -> str:
    """Choose a detector label unless a verified deterministic caption is better."""
    text = str(element.get("text") or element.get("label") or "").strip()
    caption = str(element.get("caption") or "").strip()
    if element.get("caption_source") in {"window_chrome_geometry", "deterministic"} and caption:
        return caption
    if text.lower() in {"", "icon", "unknown", "图标", "按钮", "an icon of chinese"}:
        return caption or text
    return text


class DesktopRuntime:
    """Own the services used by both DSH and the optional standalone adapter."""

    def __init__(self, config, *, home=None, state_dir=None):
        self.config = config
        self.epoch = uuid.uuid4().hex
        self.home = Path(home or os.environ.get('DSH_DESKTOP_HOME') or Path(config.workspace.screenshot_dir).parent).resolve()
        self.state_dir = Path(state_dir or os.environ.get('DSH_DESKTOP_STATE_DIR') or Path.home() / '.dsh' / 'desktop').resolve()
        self.targets = TargetRegistry(self.epoch)
        self.frames = FrameRegistry(self.epoch)
        self.browser = BrowserRuntime(self)
        self.window_identities = WindowIdentities(self.epoch)
        self.observations = ObservationService(self)
        self.window = window.WindowService()
        self.capture = capture.CaptureService(self.window)
        self.input = input_mod.InputService(self.window)
        self._input_lock = threading.Lock()
        self._operations_lock = threading.Lock()
        self._operations = None
        self._parser_lock = threading.Lock()
        self._ui_parser = None
        self.parser_initializations = 0
        self._vision_parsers = {}
        self._screenshot_dir = Path(config.workspace.screenshot_dir)
        self._window_picker = WindowPicker()
        self._window_picker_overlay = WindowPickerOverlay(self._window_picker)
        self._picked_hwnd = None
        self._picked_info = None

    @property
    def operations(self):
        with self._operations_lock:
            if self._operations is None:
                from .operations import OperationManager
                self._operations = OperationManager(self)
            return self._operations

    def act(self, owner, request_key, arguments, *, budget_ms=5000, cancelled=None):
        from .actions import ActionDispatcher
        dispatcher = ActionDispatcher(self)
        dispatcher.validate(arguments)
        return self.operations.submit(owner, request_key, 'act', arguments,
                                      lambda context: dispatcher.run(context, arguments), budget_ms=budget_ms,
                                      cancelled=cancelled)

    @property
    def ui_parser(self):
        with self._parser_lock:
            if self._ui_parser is None:
                from .ui_parser import UIParser
                self._ui_parser = UIParser(self.config.workspace.weights_dir,
                                           screenshot_max_dim=self.config.workspace.screenshot_max_dim)
                self.parser_initializations += 1
            return self._ui_parser

    def setup(self):
        from .desktop_tools import AppControllerRef
        AppControllerRef.current = self

    def perceive(self, image, backend):
        if backend not in ('ocr', 'yolo'):
            raise ValueError('Unknown local vision backend')
        with self._parser_lock:
            if backend not in self._vision_parsers:
                from .ui_parser import UIParser
                parser = UIParser(self.config.workspace.weights_dir,
                                  enable_ocr=backend == 'ocr', enable_yolo=backend == 'yolo')
                if backend == 'yolo' and parser.yolo_session is None:
                    raise RuntimeFault('BACKEND_UNAVAILABLE', 'Local icon weights are unavailable. Run scripts/bootstrap.ps1')
                self._vision_parsers[backend] = parser
                self.parser_initializations += 1
            parser = self._vision_parsers[backend]
            return parser._detect_text(image) if backend == 'ocr' else parser._detect_icons(image)

    def initialized_backends(self):
        backends = ['windows', 'capture', 'input']
        web = sys.modules.get('dsh_desktop.web_tools')
        if web is not None and web._pw_context is not None:
            backends.append('web')
        if self._ui_parser is not None or self._vision_parsers:
            backends.append('vision')
        return backends

    def stop_all(self):
        from . import input_gate
        input_gate.emergency_stop()

    def shutdown(self, timeout=5):
        from . import companion, desktop_tools, watch
        from .cursor_overlay import stop_cursor_overlay
        self.stop_all()
        if self._operations is not None:
            self._operations.shutdown()
        companion.shutdown()
        stop_cursor_overlay()
        desktop_tools._CURSOR = None
        self._window_picker_overlay.stop()
        watch.registry().stop_all()
        self.window_identities.shutdown()
        self.observations.shutdown()
        # Playwright cleanup must run on its owning bridge worker.
        try:
            self.browser.shutdown()
        except Exception:
            logger.warning('Browser cleanup did not finish within shutdown budget', exc_info=True)
        if desktop_tools.AppControllerRef.current is self:
            desktop_tools.AppControllerRef.current = None

    def _native_metadata(self, info):
        return {**info, **capture.window_geometry(info['hwnd']),
                'active': win32gui.GetForegroundWindow() == info['hwnd']}

    def target_directory(self, owner):
        from . import desktop_tools
        import win32api
        monitors = [dict(win32api.GetMonitorInfo(handle)) for handle, _, _ in win32api.EnumDisplayMonitors()]
        self.targets.adopt(owner, 'desktop', 'desktop', None,
                           {'title': 'Desktop', 'monitors': monitors, 'virtual_origin': capture.virtual_origin()})
        for info in desktop_tools._list_visible_windows():
            try:
                identity = self.window_identities.acquire(info['hwnd'], info['window_key'])
                self.targets.adopt(owner, 'window', identity, info['hwnd'], self._native_metadata(info))
            except RuntimeFault as error:
                logger.debug('Window discovery could not establish identity: %s', error)
        # Refresh previously adopted records so the directory cannot return a
        # destroyed HWND or a detached frame as a live target.
        for target in self.targets.records(owner):
            try:
                self.resolve_target(owner, target.target_id)
            except RuntimeFault as error:
                if error.code != 'TARGET_CLOSED':
                    raise
        return self.targets.directory(owner)

    def open_target(self, owner, *, target_id=None, hwnd=None, url=None, call_id=None, connect=None):
        from . import desktop_tools
        if connect:
            return self.browser.connect(owner, connect)
        if url is not None:
            return self.browser.open(owner, url, call_id, target_id)
        if target_id:
            return self.resolve_target(owner, target_id)
        if hwnd is not None:
            info = desktop_tools._window_info(int(hwnd))
            if info is None:
                raise RuntimeFault('TARGET_CLOSED', 'Window is unavailable')
            identity = self.window_identities.acquire(int(hwnd), info['window_key'])
            return self.targets.adopt(owner, 'window', identity, int(hwnd), self._native_metadata(info))
        self.target_directory(owner)
        return next(target for target in self.targets.records(owner) if target.kind == 'desktop')

    def resolve_target(self, owner, target_id):
        from . import desktop_tools
        target = self.targets.get(owner, target_id)
        if target.kind == 'browser':
            return self.browser.call(self.browser.resolve, owner, target_id)
        if target.kind == 'window':
            info = desktop_tools._window_info(target.handle)
            if info is None or not self.window_identities.matches(target.handle, target.identity, info['window_key']):
                target.closed = True
                raise RuntimeFault('TARGET_CLOSED', 'Window closed or its handle was reused')
            self.targets.refresh(target, self._native_metadata(info))
        if target.kind == 'desktop':
            import win32api
            monitors = [dict(win32api.GetMonitorInfo(handle)) for handle, _, _ in win32api.EnumDisplayMonitors()]
            self.targets.refresh(target, {'title': 'Desktop', 'monitors': monitors, 'virtual_origin': capture.virtual_origin()})
        return target

    def capture_target(self, owner, target_id, *, max_dim=1366, crop=None):
        target = self.resolve_target(owner, target_id)
        revision = target.revision
        if target.kind == 'browser':
            image, geometry, target = self.browser.capture(owner, target_id)
        else:
            captured = self.capture.capture_detailed(target.handle)
            image, geometry = captured.image, captured.geometry
            target = self.resolve_target(owner, target_id)
        if target.revision != revision:
            raise RuntimeFault('TARGET_CHANGED', 'Target changed during capture. Retry observation')
        if crop is not None:
            # Geometry validates the source-relative crop before array slicing.
            geometry = replace(geometry, crop=tuple(crop))
            x, y, width, height = crop
            image = image[y:y + height, x:x + width]
        height, width = image.shape[:2]
        if max_dim and max(height, width) > max_dim:
            scale = max_dim / max(height, width)
            image = cv2.resize(image, (max(1, round(width * scale)), max(1, round(height * scale))))
        geometry = replace(geometry, image_size=(image.shape[1], image.shape[0]))
        frame = self.frames.add(target, geometry, revision=revision)
        return image, frame

    def map_point(self, owner, target_id, frame_id, x, y):
        target = self.resolve_target(owner, target_id)
        frame = self.frames.get(owner, frame_id, target)
        return frame.geometry.to_screen(x, y)

    def release_owner(self, owner):
        if self._operations is not None:
            self._operations.stop(owner)
        self.browser.release(owner)
        cleanup = self.observations.release(owner)
        self.frames.release(owner)
        self.targets.release(owner)
        return cleanup

    @tool("List all visible windows on the desktop. Returns hwnd, title, exe, pid, and rect for each window.")
    def list_windows(self) -> dict:
        windows = self._window_picker.enum_visible_windows()
        return {"windows": windows, "count": len(windows)}


    @tool("Find a visible window by title or exe name (fuzzy match). Returns the first matching window.")
    def find_window(self, title: str = "", exe: str = "") -> dict:
        """
        Args:
            title: Window title to search for (case-insensitive substring).
            exe: Executable name to search for, e.g. 'chrome.exe'.
        """
        if not title and not exe:
            return {"error": "Provide title or exe to search for"}
        result = self._window_picker.find_window(title=title, exe=exe)
        if result is None:
            return {"found": False, "error": f"No window found matching title='{title}' exe='{exe}'"}
        return {"found": True, "window": result}


    def pick_window(self, hwnd: int) -> dict:
        """Bind to a specific window by HWND."""
        if not self.window.is_valid(hwnd):
            return {"success": False, "error": f"Invalid window handle: {hwnd}"}

        title = win32gui.GetWindowText(hwnd)
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        # Resolve real PID for UWP/hosted windows
        pid = _resolve_real_pid(hwnd, pid)
        exe = ""
        try:
            exe = psutil.Process(pid).name()
        except Exception:
            pass

        self._picked_hwnd = hwnd
        exe_path = ""
        try:
            exe_path = psutil.Process(pid).exe()
        except Exception:
            pass
        self._picked_info = {
            "hwnd": hwnd,
            "title": title,
            "pid": pid,
            "exe": exe,
            "exe_path": exe_path,
        }
        logger.info("Picked window: hwnd=%d, title=%r, exe=%r, exe_path=%r", hwnd, title, exe, exe_path)
        return {"success": True, "window": self._picked_info}


    def unpick_window(self) -> dict:
        """Unbind the currently picked window."""
        if self._picked_hwnd is None:
            return {"success": True, "message": "No window was picked"}

        prev = self._picked_info
        self._picked_hwnd = None
        self._picked_info = None
        logger.info("Unpicked window: %r", prev)
        return {"success": True, "window": prev}


    def get_picked_window(self) -> dict | None:
        """Return info about the currently picked window, or None."""
        if self._picked_hwnd is None:
            return None
        # Check if still valid
        if not self.window.is_valid(self._picked_hwnd):
            logger.info("Picked window hwnd=%d is no longer valid, auto-unpicking", self._picked_hwnd)
            self._picked_hwnd = None
            self._picked_info = None
            return None
        return self._picked_info


    @property
    def overlay_active(self) -> bool:
        """Whether the overlay picker is currently running."""
        return self._window_picker_overlay.is_active


    def show_overlay_picker(self) -> dict:
        """Launch the interactive overlay window picker.

        Non-blocking: runs in a background thread. When the user picks a window,
        it is automatically bound via pick_window(). The frontend should poll
        GET /api/pick to detect the result.
        """
        if self._window_picker_overlay.is_active:
            return {"success": False, "error": "Picker overlay already active"}

        def on_picked(hwnd: int | None):
            if hwnd:
                self.pick_window(hwnd)
            logger.info("Overlay picker finished: hwnd=%s", hwnd)

        self._window_picker_overlay.show(callback=on_picked)
        return {"success": True, "message": "Overlay picker launched"}


    @tool("Capture the entire desktop (all monitors). Optionally run OCR + YOLO detection. Returns image_path and dimensions.")
    def capture_desktop(self, analyze: bool = False) -> dict:
        """
        Args:
            analyze: If True, run OCR + YOLO detection and return ui_elements with bbox overlay.
        """
        frame = self.capture.capture_desktop()
        if frame is None:
            return {"error": "Desktop capture failed"}

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
        name = f"desktop_{ts}"
        path = str(date_dir / f"{name}.jpeg")
        ok, buf = cv2.imencode(".jpeg", compressed)
        if ok:
            with open(path, "wb") as f:
                f.write(buf.tobytes())

        result = {
            "width": compressed.shape[1],
            "height": compressed.shape[0],
            # Physical-pixel size of the source frame. Callers that draw markers
            # using GetCursorPos / screen coordinates need this to convert them
            # into the compressed image's coordinate space.
            "source_width": int(w),
            "source_height": int(h),
            IMAGE_PATH_KEY: path,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }

        if analyze:
            parsed = self.ui_parser.parse(frame)
            logger.info("capture_desktop: analyze found %d ui_elements", len(parsed))

            bbox_path = str(date_dir / f"{name}_bbox.jpeg")
            self._save_bbox_overlay(compressed, parsed, bbox_path)

            # Absolute mouse position on the virtual screen
            try:
                point, source = self._screen_pointer_position()
                mouse_pos: dict = {"absolute": list(point) if point else None, "source": source}
            except Exception:
                mouse_pos = {"absolute": None}

            result["ui_elements"] = parsed
            result[SOM_IMAGE_PATH_KEY] = bbox_path
            result["mouse_position"] = mouse_pos
        else:
            logger.info("capture_desktop: %dx%d -> %s", w, h, path)

        return result


    def _screen_pointer_position(self):
        if self.input.virtual:
            from .cursor_overlay import peek_cursor_overlay
            overlay = peek_cursor_overlay()
            return (overlay._target if overlay is not None else None), "virtual"
        return win32gui.GetCursorPos(), "physical"


    def _get_mouse_position(self, hwnd: int) -> dict:
        """Get normalized mouse position [0,1000] relative to window client area."""
        try:
            point, source = self._screen_pointer_position()
            if point is None:
                return {"normalized": None, "source": source}
            sx, sy = point
            if not self.window.is_valid(hwnd):
                return {"normalized": None, "error": "Invalid window"}

            ox, oy = win32gui.ClientToScreen(hwnd, (0, 0))
            rect = win32gui.GetClientRect(hwnd)
            width = rect[2]
            height = rect[3]

            if width <= 0 or height <= 0:
                return {"normalized": None, "error": "Zero-size window"}

            # Cursor relative to client area
            cx = sx - ox
            cy = sy - oy

            # Normalize to [0, 1000]
            nx = int(cx / width * 1000) if width > 0 else None
            ny = int(cy / height * 1000) if height > 0 else None

            return {"normalized": [nx, ny] if nx is not None and ny is not None else None,
                    "absolute": [sx, sy], "source": source}
        except Exception as e:
            logger.debug("Failed to get mouse position: %s", e)
            return {"normalized": None, "error": str(e)}


    def _save_bbox_overlay(self, image, elements: list, path: str, *,
                           hwnd: int | None = None) -> None:
        """Draw normalized [0,1000] bboxes onto image and save to path."""
        from PIL import Image, ImageDraw, ImageFont
        import colorsys

        h, w = image.shape[:2]
        overlay = image.copy()

        # Try to load a font that supports CJK characters
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont
        try:
            font = ImageFont.truetype("msyh.ttc", 14)  # Microsoft YaHei on Windows
        except Exception:
            font = ImageFont.load_default()

        # Convert BGR to RGB for PIL
        rgb = cv2.cvtColor(overlay, cv2.COLOR_BGR2RGB)
        pil_img = Image.fromarray(rgb)
        draw = ImageDraw.Draw(pil_img)

        # Track placed labels to avoid overlaps
        placed_labels: list[tuple[int, int, int, int]] = []

        def get_text_size(text):
            """Get text width and height."""
            if hasattr(font, 'getbbox'):
                bbox = font.getbbox(text)
                return bbox[2] - bbox[0], bbox[3] - bbox[1]
            else:
                # Fallback for older PIL versions
                return font.getsize(text)

        def check_overlap(rect1, rect2):
            """Check if two rectangles overlap."""
            x1, y1, x2, y2 = rect1
            x3, y3, x4, y4 = rect2
            return not (x2 < x3 or x4 < x1 or y2 < y3 or y4 < y1)

        def find_label_position(px1, py1, px2, py2, text_w, text_h):
            """Find a non-overlapping position for the label."""
            # Try positions: above, below, right, left of bbox
            positions = [
                (px1, max(py1 - text_h - 2, 0)),  # Above
                (px1, min(py2 + 2, h - text_h)),  # Below
                (px2 + 2, py1),  # Right
                (max(px1 - text_w - 2, 0), py1),  # Left
            ]

            for label_x, label_y in positions:
                label_rect = (label_x, label_y, label_x + text_w, label_y + text_h)
                # Check if this position overlaps with any placed label
                overlap = False
                for placed_rect in placed_labels:
                    if check_overlap(label_rect, placed_rect):
                        overlap = True
                        break

                if not overlap:
                    # Also check if label goes outside image bounds
                    if 0 <= label_x and label_x + text_w <= w and 0 <= label_y and label_y + text_h <= h:
                        return label_x, label_y, label_rect

            # If all positions overlap, skip this label
            return None

        def generate_color(index, total):
            """Generate distinct colors using HSL color space."""
            if total <= 1:
                # Single element: use green
                return (0, 255, 0)
            # Distribute hues evenly around the color wheel
            hue = index / total
            # Use high saturation and lightness for visibility
            r, g, b = colorsys.hsv_to_rgb(hue, 0.9, 1.0)
            return (int(r * 255), int(g * 255), int(b * 255))

        # Generate colors for all elements
        total_elements = len(elements)
        for idx, el in enumerate(elements):
            x1, y1, x2, y2 = el["bbox"]
            px1, py1 = int(x1 / 1000 * w), int(y1 / 1000 * h)
            px2, py2 = int(x2 / 1000 * w), int(y2 / 1000 * h)

            # Generate a distinct color for this element
            color = generate_color(idx, total_elements)
            draw.rectangle([px1, py1, px2, py2], outline=color, width=1)

            label = _overlay_label_text(el)
            center = el.get("center")
            if label or center:
                # Truncate long text to avoid clutter
                display = label[:12] if label else ""
                if center:
                    cx, cy = center
                    center_text = f"({cx}, {cy})"
                    display = f"{display} {center_text}" if display else center_text

                if display:
                    text_w, text_h = get_text_size(display)
                    result = find_label_position(px1, py1, px2, py2, text_w, text_h)

                    if result:
                        label_x, label_y, label_rect = result
                        draw.text((label_x, label_y), display, fill=color, font=font)
                        placed_labels.append(label_rect)

        # Draw mouse cursor position as red crosshair
        if hwnd is not None and self.window.is_valid(hwnd):
            try:
                mouse = self._get_mouse_position(hwnd)
                nx, ny = mouse.get("normalized", (None, None))
                if nx is not None and ny is not None:
                    # Convert normalized [0,1000] to compressed image coords
                    cx = int(nx / 1000 * w)
                    cy = int(ny / 1000 * h)
                    if 0 <= cx < w and 0 <= cy < h:
                        cs = 10
                        draw.line([(cx - cs, cy), (cx + cs, cy)], fill=(255, 0, 0), width=2)
                        draw.line([(cx, cy - cs), (cx, cy + cs)], fill=(255, 0, 0), width=2)
                        draw.ellipse([cx - 4, cy - 4, cx + 4, cy + 4], fill=(255, 0, 0))
            except Exception:
                pass

        # Convert back to BGR and save
        overlay = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
        ok, buf = cv2.imencode(".jpeg", overlay)
        if ok:
            with open(path, "wb") as f:
                f.write(buf.tobytes())


    def _force_foreground(self, hwnd: int) -> bool:
        """Ensure the window is foreground; no-op when it already is.

        Activation side effects (ShowWindow/SetWindowPos) only run when the
        window is NOT already foreground — repeated key/mouse actions on an
        active window no longer perturb embedded webviews (popup collapse).
        """
        return self.window.ensure_foreground(hwnd)
