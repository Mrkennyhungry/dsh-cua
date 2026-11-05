"""Design-01: always-on frame sampling, on-demand reading.

A toast lives 2-3 s while a tool round trip costs 1-2 s, so a single "capture
now" cannot prove a transient message never appeared. Sample the bound window
in the background at ~10 fps, keep only frames that CHANGED, and let the caller
ask `since(seconds=N)` for a contact sheet of that window of time.

Two change scores are mandatory. A global mean over a 64x36 grid is diluted by
a small bottom toast (measured 0.12-0.31 while page switches are 9-18), so a
"top 1% cells" local score is computed alongside it. Either score can retain a
frame. Score magnitude is used ONLY to decide whether to keep a frame; frames
are returned in time order, never sorted by score (a toast's local score is an
order of magnitude below a page switch but is exactly what we need to see).
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass

import cv2
import numpy as np

FPS = 10
RING_SIZE = 48
GRID = (36, 64)          # (rows, cols) for the global mean
GLOBAL_THRESHOLD = 12.0
LOCAL_THRESHOLD = 8.0
SHEET_MAX_DIM = 1920
SHEET_COLS = 4
JPEG_QUALITY = 82


@dataclass
class Frame:
    ts: float
    image: np.ndarray          # BGR, already downscaled for the sheet
    global_score: float
    local_score: float


def _signature(frame: np.ndarray) -> np.ndarray:
    """64x36 HSV-ish grid of mean brightness (cheap, resize does the binning)."""
    small = cv2.resize(frame, (GRID[1], GRID[0]), interpolation=cv2.INTER_AREA)
    return cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32)


def _scores(prev: np.ndarray | None, cur: np.ndarray) -> tuple[float, float]:
    if prev is None:
        return 0.0, 0.0
    diff = np.abs(cur - prev).reshape(-1)
    mean = float(diff.mean())
    # Top 1% of cells: a small toast changes few cells but changes them a lot.
    k = max(1, diff.size // 100)
    local = float(np.partition(diff, -k)[-k:].mean())
    return mean, local


class WatchSession:
    def __init__(self, window_id: str, hwnd: int, capture) -> None:
        self.window_id = window_id
        self.hwnd = hwnd
        self._capture = capture
        self.frames: deque[Frame] = deque(maxlen=RING_SIZE)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._prev: np.ndarray | None = None
        self.sampled = 0
        self.kept = 0
        self.started_at = time.time()
        self.error: str | None = None

    @property
    def active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.active:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"dsh-watch-{self.window_id}")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        self._thread = None

    def _run(self) -> None:
        interval = 1.0 / FPS
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                frame = self._capture.capture(self.hwnd, activate=False)
                if frame is not None and getattr(frame, "size", 0):
                    sig = _signature(frame)
                    g, l = _scores(self._prev, sig)
                    self._prev = sig
                    self.sampled += 1
                    if g >= GLOBAL_THRESHOLD or l >= LOCAL_THRESHOLD:
                        small = _fit(frame, 480)
                        with self._lock:
                            self.frames.append(Frame(time.time(), small, g, l))
                            self.kept += 1
            except Exception as exc:  # noqa: BLE001 - a sampler must never kill the kernel
                self.error = f"{type(exc).__name__}: {exc}"
            self._stop.wait(max(0.0, interval - (time.monotonic() - t0)))

    def since(self, seconds: float, limit: int = 8) -> dict:
        cutoff = time.time() - max(0.1, float(seconds))
        with self._lock:
            selected = [f for f in self.frames if f.ts >= cutoff]
        # Always time-ordered. Never sort by score (design decision).
        truncated = len(selected) > limit
        selected = selected[-limit:] if truncated else selected
        rows = [{"ts": round(f.ts - self.started_at, 2),
                 "global": round(f.global_score, 2),
                 "local": round(f.local_score, 2)} for f in selected]
        result: dict = {
            "success": True, "window_id": self.window_id,
            "frames": len(selected), "total_kept": self.kept,
            "sampled": self.sampled, "truncated": truncated,
            "timeline": rows,
            "legend": ("Frames are time-ordered; global>=12 catches page switches, "
                       "local>=8 catches small toasts. Do not rank by score."),
        }
        if selected:
            result["image_path"] = _contact_sheet(selected)
        if self.error:
            result["sampler_error"] = self.error
        return result


def _fit(frame: np.ndarray, max_dim: int) -> np.ndarray:
    h, w = frame.shape[:2]
    if max(h, w) <= max_dim:
        return frame.copy()
    scale = max_dim / max(h, w)
    return cv2.resize(frame, (max(1, round(w * scale)), max(1, round(h * scale))))


def _output_dir() -> "Path":
    """Keep watch artifacts inside the kernel workspace, never a protected
    profile directory. Tests and hosts can override with DSH_WATCH_DIR."""
    import os
    from pathlib import Path
    override = os.environ.get("DSH_WATCH_DIR")
    if override:
        out = Path(override)
    else:
        try:
            from . import desktop_tools
            ctrl = desktop_tools._controller()
            root = getattr(getattr(getattr(ctrl, "config", None), "workspace", None),
                           "screenshot_dir", None)
            out = Path(root) / "watch" if root else Path.home() / ".dsh" / "watch"
        except Exception:
            out = Path.home() / ".dsh" / "watch"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _contact_sheet(frames: list[Frame]) -> str:
    from pathlib import Path
    from datetime import datetime
    tiles = []
    for f in frames:
        tile = _fit(f.image, 420)
        cv2.putText(tile, f"t+{f.ts - frames[0].ts:.1f}s g{f.global_score:.1f} l{f.local_score:.1f}",
                    (6, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 220, 255), 1)
        tiles.append(tile)
    tw = max(t.shape[1] for t in tiles)
    th = max(t.shape[0] for t in tiles)
    cols = min(SHEET_COLS, len(tiles))
    rows = (len(tiles) + cols - 1) // cols
    sheet = np.full((rows * th, cols * tw, 3), 245, np.uint8)
    for i, tile in enumerate(tiles):
        y, x = divmod(i, cols)
        sheet[y * th:y * th + tile.shape[0], x * tw:x * tw + tile.shape[1]] = tile
    if max(sheet.shape[:2]) > SHEET_MAX_DIM:
        s = SHEET_MAX_DIM / max(sheet.shape[:2])
        sheet = cv2.resize(sheet, (int(sheet.shape[1] * s), int(sheet.shape[0] * s)))
    out = _output_dir()
    path = out / f"since_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.jpeg"
    cv2.imwrite(str(path), sheet, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
    return str(path)


class WatchRegistry:
    def __init__(self) -> None:
        self._sessions: dict[str, WatchSession] = {}
        self._lock = threading.Lock()

    def start(self, window_id: str, hwnd: int, capture) -> dict:
        with self._lock:
            old = self._sessions.pop(window_id, None)
            if old:
                old.stop()
            session = WatchSession(window_id, hwnd, capture)
            self._sessions[window_id] = session
            session.start()
        return {"success": True, "window_id": window_id, "active": True,
                "fps": FPS, "ring_size": RING_SIZE,
                "thresholds": {"global": GLOBAL_THRESHOLD, "local": LOCAL_THRESHOLD}}

    def stop(self, window_id: str) -> dict:
        with self._lock:
            session = self._sessions.pop(window_id, None)
        if not session:
            return {"success": True, "active": False, "kept": 0}
        session.stop()
        return {"success": True, "active": False, "sampled": session.sampled,
                "kept": session.kept}

    def since(self, window_id: str, seconds: float, limit: int) -> dict:
        with self._lock:
            session = self._sessions.get(window_id)
        if not session:
            return {"success": False, "error": "WATCH_NOT_RUNNING: start a watch first"}
        return session.since(seconds, limit)

    def status(self, window_id: str) -> dict:
        with self._lock:
            session = self._sessions.get(window_id)
        if not session:
            return {"active": False}
        return {"active": session.active, "sampled": session.sampled,
                "kept": session.kept, "buffered": len(session.frames),
                "error": session.error}


_REGISTRY = WatchRegistry()


def registry() -> WatchRegistry:
    return _REGISTRY
