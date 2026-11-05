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

# A message popup is TEXT. Handing every retained frame to the model as an
# image would burn context; run the kernel's existing OCR on the bottom band
# of each retained frame instead and return deduplicated TEXT candidates. The
# model decides which (if any) is the message it was waiting for. Images remain
# available only as a fallback when OCR is ambiguous.
# Full-desktop sampling makes "the bottom band" relative to the SCREEN, not to
# the bound window. An in-app toast near the bottom of a window that sits in
# the upper half of a 4K screen lands at ~68% of screen height; a desktop
# notification in the corner lands at ~85-95%. 22% covered neither reliably.
# Keep the whole lower half: OCR text is cheap, a missed message is not.
BOTTOM_BAND_FRACTION = 0.45
BAND_MAX_DIM = 1600          # keep source-resolution text readable, bound memory
BAND_ZOOM = 3
BAND_STRIP_MAX_DIM = 1600
# Each distinct band costs one OCR pass (~1.5s). Callers can trade coverage
# for latency with ocr_limit; the default covers every distinct band in the
# window (a 48-frame ring with de-duplication typically leaves <16).
MAX_OCR_BANDS = 16

# --- Difference ROIs -------------------------------------------------------
# OCR'ing the whole bottom band is what made since() cost 15-51s, and it is
# also why frames had to be "chosen" (by score or recency) — which then dropped
# the toast. Diffing consecutive frames instead tells us exactly WHICH
# rectangles changed, so we OCR those small crops instead of a 1600x970 band.
# Every changed rectangle is kept: nothing is ranked or selected, so recall no
# longer depends on a frame surviving a budget. Static chrome (taskbar, desktop
# icons, other apps' text) produces no rectangle and therefore never reaches
# OCR at all, which is what removes the 100+ irrelevant strings.
ROI_MIN_AREA = 400            # physical px; below this is cursor/noise
ROI_MAX_SCREEN_FRAC = 0.30    # a rect this large is a page repaint, not a popup
ROI_MAX_PER_FRAME = 6         # safety valve only; normally 1-3
ROI_PAD = 8                   # include the text's own padding/anti-aliasing


@dataclass
class Frame:
    ts: float
    image: np.ndarray          # BGR, already downscaled for the sheet
    global_score: float
    local_score: float
    bottom_band: np.ndarray | None = None         # source-resolution bottom band
    rois: list = None                             # changed crops, source resolution


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


# Keyword Signal (vLLM semantic-router style): a deterministic, low-latency
# FIRST-PASS filter over OCR text. Callers describe what they are waiting for
# ("失败", "被拦截", "不支持"); we return only matching strings plus a count of
# the rest. Full OCR text is available on demand, but by default the model
# never pays context for taskbar clocks, weather and dialog chrome.
# https://github.com/vllm-project/semantic-router/blob/main/website/i18n/zh-Hans/docusaurus-plugin-content-docs/version-v0.3/tutorials/signal/heuristic/keyword.md
DEFAULT_EXPECT_TERMS = (
    "失败", "错误", "不支持", "忽略", "拦截", "拒绝", "无法", "异常", "警告",
    "超时", "取消", "缺少", "无效", "冲突", "重复", "已满", "成功", "完成",
    "已上传", "已保存", "上传", "下载", "格式", "文件",
    "failed", "error", "unsupported", "ignored", "blocked", "denied",
    "timeout", "invalid", "warning", "cancel", "success", "complete",
)
BM25_K1 = 1.2
BM25_B = 0.75
EXPECT_THRESHOLD = 0.12


def _phrase_hits(text: str, terms: tuple[str, ...]) -> list[str]:
    low = text.lower()
    return [t for t in terms if t and str(t).lower() in low]


def rank_text_candidates(candidates: list[dict], expect: tuple[str, ...],
                         threshold: float = EXPECT_THRESHOLD) -> dict:
    """BM25 + exact phrase signal over OCR strings.

    Short CJK strings are tokenised per character (same as decide.tokenize),
    which alone over-matches; an exact phrase hit therefore carries a strong
    bonus. Returns matched rows (with score and hit terms) and only a COUNT of
    the rest, so an irrelevant clock/weather string costs no context.
    """
    from .decide import tokenize
    terms = tuple(str(t).strip() for t in expect if str(t).strip())
    if not candidates:
        return {"matches": [], "other_count": 0, "expect": list(terms)}
    docs = [tokenize(str(c.get("text") or "")) for c in candidates]
    q_tokens = [tok for term in terms for tok in tokenize(term)]
    q_tokens = list(dict.fromkeys(q_tokens))
    if not q_tokens:
        return {"matches": [], "other_count": len(candidates), "expect": list(terms)}
    total = len(docs)
    lengths = [max(1, len(d)) for d in docs]
    avgdl = sum(lengths) / total
    df: dict[str, int] = {}
    for d in docs:
        for tok in set(d):
            df[tok] = df.get(tok, 0) + 1
    import math
    idf = {t: math.log(1.0 + (total - df.get(t, 0) + 0.5) / (df.get(t, 0) + 0.5))
           for t in q_tokens}
    weight = sum(idf.values()) or 1.0
    scored = []
    for c, doc in zip(candidates, docs):
        text = str(c.get("text") or "")
        tf: dict[str, int] = {}
        for tok in doc:
            tf[tok] = tf.get(tok, 0) + 1
        score = 0.0
        for t in q_tokens:
            f = tf.get(t, 0)
            if not f:
                continue
            score += idf.get(t, 0.0) * (f * (BM25_K1 + 1)) / (
                f + BM25_K1 * (1 - BM25_B + BM25_B * len(doc) / avgdl))
        score /= weight
        hits = _phrase_hits(text, terms)
        if hits:
            score += 0.5 * min(1.0, len(hits) / 2.0)
        scored.append((round(score, 4), c, hits))
    matches = [{"ts": c["ts"], "text": c["text"], "score": s,
                "matched_terms": hits, "global": c.get("global"), "local": c.get("local")}
               for s, c, hits in scored if s >= threshold and hits]
    matches.sort(key=lambda r: -r["score"])
    return {"matches": matches, "other_count": len(candidates) - len(matches),
            "expect": list(terms)}


def _band_texts(band: np.ndarray | None) -> list[str]:
    """Run the kernel's existing OCR on one bottom band.

    Returns just the strings: the caller (model) decides whether any of them
    is the message it was waiting for. OCR failures are non-fatal; an empty
    list means "no text here", not "no popup".
    """
    if band is None or band.size == 0:
        return []
    try:
        from . import desktop_tools
        controller = desktop_tools._controller()
        parser = getattr(controller, "ui_parser", None) if controller else None
        if parser is None:
            return []
        # TEXT ONLY. Running parse() (YOLO + OCR in parallel) on every frame
        # cost ~3s each -> 51s for 16 frames. A message popup has no icons to
        # detect; _detect_text alone is enough and much cheaper.
        # Use the SAME pipeline as desktop_analyze: it normalises boxes and
        # de-noises OCR. Calling _detect_text directly on a downscaled band
        # produced garbage for CJK ("1个文件格式不支持" -> "orts ciseplay...").
        # Speed comes from the frame de-duplication in since(), not from
        # shrinking the image.
        if hasattr(parser, "parse"):
            items = parser.parse(band) or []
        else:
            return []
    except Exception:  # noqa: BLE001 - OCR is an enhancement, never a gate
        return []
    texts = []
    for item in items:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        low = text.lower()
        if low in {"icon", "ui_element", "unknown", "图标", "?"}:
            continue
        if text not in texts:
            texts.append(text[:120])
    return texts


def _band_fingerprint(band: np.ndarray | None) -> bytes:
    """Cheap perceptual hash so a static toast held for 2s is OCR'd ONCE."""
    if band is None or band.size == 0:
        return b"none"
    small = cv2.resize(band, (32, 12), interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    return (gray > gray.mean()).tobytes()


def _bottom_band(frame: np.ndarray) -> np.ndarray | None:
    """Pure geometric crop of the bottom band, at source resolution.

    NO detection, NO filtering, NO judgement. Every retained frame is a
    candidate; deciding whether one of them contains the message the caller
    cares about is the model's job, not the sampler's. This is deliberate:
    toasts differ in colour, position (centre vs bottom-right), shape and app,
    so any heuristic here would encode assumptions that lose real messages.
    """
    h, w = frame.shape[:2]
    if h < 80 or w < 200:
        return None
    y0 = int(h * (1.0 - BOTTOM_BAND_FRACTION))
    band = frame[y0:, :].copy()
    # Bound memory in the 48-frame ring without losing text detail: a 4K
    # desktop's 22% band is ~475px tall, which is already cheap; only shrink
    # when the width is extreme.
    if band.shape[1] > BAND_MAX_DIM:
        scale = BAND_MAX_DIM / band.shape[1]
        band = cv2.resize(band, (BAND_MAX_DIM, max(1, round(band.shape[0] * scale))),
                          interpolation=cv2.INTER_AREA)
    return band


def _diff_rois(prev: np.ndarray | None, curr: np.ndarray | None) -> list:
    """Crops of `curr` that actually changed versus `prev`, at source resolution.

    This is a GEOMETRIC operation, not a semantic one: it never asks "is this a
    popup", it only asks "which pixels are different". That keeps it inside the
    no-semantic-judgement constraint while removing the need to OCR a whole
    half-screen band for every retained frame.
    """
    if prev is None or curr is None or prev.size == 0 or curr.size == 0:
        return []
    if prev.shape != curr.shape:
        return []                      # resolution changed; caller falls back to the band
    h, w = curr.shape[:2]
    small = (max(1, w // 2), max(1, h // 2))
    try:
        a = cv2.resize(prev, small, interpolation=cv2.INTER_AREA)
        b = cv2.resize(curr, small, interpolation=cv2.INTER_AREA)
        diff = cv2.absdiff(a, b)
        gray = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)
        mask = (gray > 25).astype(np.uint8) * 255
        # Merge the letters of a message into one rectangle instead of dozens.
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE,
                                cv2.getStructuringElement(cv2.MORPH_RECT, (15, 5)))
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    except Exception:  # noqa: BLE001 - diffing is an optimisation, never a gate
        return []

    screen_area = float(w * h)
    found: list[tuple[float, np.ndarray]] = []
    for c in contours:
        x, y, cw, ch = cv2.boundingRect(c)
        area = float(cw * ch * 4)      # back to full-resolution area
        if area < ROI_MIN_AREA or area > screen_area * ROI_MAX_SCREEN_FRAC:
            continue
        x0 = max(0, x * 2 - ROI_PAD)
        y0 = max(0, y * 2 - ROI_PAD)
        x1 = min(w, (x + cw) * 2 + ROI_PAD)
        y1 = min(h, (y + ch) * 2 + ROI_PAD)
        if x1 - x0 < 8 or y1 - y0 < 8:
            continue
        found.append((area, curr[y0:y1, x0:x1].copy()))
    found.sort(key=lambda item: -item[0])
    return [crop for _, crop in found[:ROI_MAX_PER_FRAME]]


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
        self._prev_frame: np.ndarray | None = None
        self.sampled = 0
        self.kept = 0
        self.started_at = time.time()
        self.error: str | None = None
        self.scope: str = "window"

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

    def _grab(self):
        """Prefer a FULL-DESKTOP capture.

        A failed upload can surface a system-level notification in the desktop's
        bottom-right corner, which lives outside any bound window's client
        rect. Sampling only that window would never see it. Fall back to the
        window crop only when a desktop grab is unavailable.
        """
        desktop = getattr(self._capture, "capture_desktop", None)
        if callable(desktop):
            frame = desktop()
            if frame is not None and getattr(frame, "size", 0):
                return frame, "desktop"
        frame = self._capture.capture(self.hwnd, activate=False)
        return frame, "window"

    def _run(self) -> None:
        interval = 1.0 / FPS
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                frame, scope = self._grab()
                self.scope = scope
                if frame is not None and getattr(frame, "size", 0):
                    sig = _signature(frame)
                    g, l = _scores(self._prev, sig)
                    self._prev = sig
                    self.sampled += 1
                    if g >= GLOBAL_THRESHOLD or l >= LOCAL_THRESHOLD:
                        small = _fit(frame, 480)
                        # Every retained frame is a candidate. Keep its bottom
                        # band at source resolution for OCR; no filtering here.
                        band = _bottom_band(frame)
                        # Diff against the previous SAMPLED frame (not the
                        # previous retained one), so the rectangles describe
                        # what just appeared, not everything that drifted.
                        rois = _diff_rois(self._prev_frame, frame)
                        with self._lock:
                            self.frames.append(Frame(time.time(), small, g, l, band, rois))
                            self.kept += 1
                    self._prev_frame = frame
            except Exception as exc:  # noqa: BLE001 - a sampler must never kill the kernel
                self.error = f"{type(exc).__name__}: {exc}"
            self._stop.wait(max(0.0, interval - (time.monotonic() - t0)))

    def since(self, seconds: float, limit: int = 8,
              expect: tuple[str, ...] | None = None, expect_all: bool = False,
              ocr_limit: int = MAX_OCR_BANDS) -> dict:
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
            "capture_scope": self.scope,
            "frames": len(selected), "total_kept": self.kept,
            "sampled": self.sampled, "truncated": truncated,
            "timeline": rows,
            "legend": ("Frames are time-ordered; global>=12 catches page switches, "
                       "local>=8 catches small toasts. Do not rank by score."),
        }
        if selected:
            result["image_path"] = _contact_sheet(selected)
        # Text-first: OCR every retained frame's bottom band, deduplicate, and
        # hand the STRINGS to the model. No heuristic decides what a popup is.
        candidates: list[dict] = []
        seen: set[str] = set()
        # Dedupe identical bands, then OCR ALL of them in time order. Ranking by
        # local score was tried and silently dropped the toast: with full-desktop
        # sampling the DSH chat window's own repaints score higher than a small
        # toast. Recall wins; ocr_limit is the explicit latency knob.
        # OCR the CHANGED CROPS, not a half-screen band per frame. A frame with
        # no usable diff (resolution change, first frame) falls back to its
        # band, so this can never see less than the previous implementation.
        targets: list[tuple] = []
        for f in selected:
            if f.rois:
                for roi in f.rois:
                    targets.append((f, roi, "roi"))
            elif f.bottom_band is not None:
                targets.append((f, f.bottom_band, "band"))
        unique_targets: list[tuple] = []
        seen_bands: set[bytes] = set()
        for f, img, kind in targets:
            fp = _band_fingerprint(img)
            if fp in seen_bands:
                continue
            seen_bands.add(fp)
            unique_targets.append((f, img, kind))
        budget = max(1, min(int(ocr_limit), MAX_OCR_BANDS))
        # ROIs first if a budget ever bites: they are the actual change points.
        # Never re-rank by score (that dropped the toast twice already).
        ordered = ([t for t in unique_targets if t[2] == "roi"]
                   + [t for t in unique_targets if t[2] != "roi"])
        chosen = ordered[:budget]
        result["ocr"] = {"targets": len(unique_targets), "runs": len(chosen),
                         "roi_targets": sum(1 for t in unique_targets if t[2] == "roi"),
                         "truncated": len(ordered) > len(chosen)}
        for f, img, kind in chosen:
            for text in _band_texts(img):
                key = text.strip().lower()
                if not key or key in seen:
                    continue
                seen.add(key)
                candidates.append({"ts": round(f.ts - self.started_at, 2),
                                   "text": text,
                                   "global": round(f.global_score, 2),
                                   "local": round(f.local_score, 2),
                                   "source": kind})
        terms = tuple(expect) if expect else DEFAULT_EXPECT_TERMS
        ranked = rank_text_candidates(candidates, terms)
        result["text_matches"] = ranked["matches"]
        result["text_other_count"] = ranked["other_count"]
        result["expect_terms"] = ranked["expect"]
        result["text_hint"] = (
            "text_matches 是 OCR 文字中命中预期词的条目（BM25 + 短语匹配），"
            "按分数排序；text_other_count 是未命中的条数，默认不返回其全文以省上下文。"
            "请据此判断是否出现了你要等的那条消息；没有就明确说没出现。"
            "预期词可用 expect 覆盖，需要全部原文时设 expect_all=true。")
        if expect_all:
            result["text_candidates"] = candidates
        # Images are a FALLBACK only, for when OCR is unreadable or ambiguous.
        band_frames = [f for f in selected if f.bottom_band is not None]
        if band_frames:
            result["evidence_image"] = _band_strip(band_frames)
            result["evidence_hint"] = ("仅当 OCR 文字不足以判断时才看这张图："
                                       "各帧底部带的放大拼图，时间序。")
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


def _band_strip(frames: list[Frame]) -> str:
    """Fallback evidence: enlarged bottom bands of ALL retained frames, time
    ordered. No detection or filtering — this exists only for the rare case
    where OCR text is unreadable and the model needs to look at pixels.
    """
    from datetime import datetime
    tiles = []
    for f in frames:
        band = f.bottom_band
        if band is None or band.size == 0:
            continue
        big = cv2.resize(band, None, fx=BAND_ZOOM, fy=BAND_ZOOM,
                         interpolation=cv2.INTER_CUBIC)
        blur = cv2.GaussianBlur(big, (0, 0), 1.0)
        sharp = cv2.addWeighted(big, 1.4, blur, -0.4, 0)
        label_h = 26
        tile = np.full((sharp.shape[0] + label_h, sharp.shape[1], 3), 245, np.uint8)
        tile[label_h:] = sharp
        cv2.putText(tile, f"t+{f.ts - frames[0].ts:.1f}s local={f.local_score:.1f}",
                    (6, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (30, 30, 30), 1)
        tiles.append(tile)
    if not tiles:
        return ""
    width = max(t.shape[1] for t in tiles)
    sheet = np.full((sum(t.shape[0] for t in tiles), width, 3), 245, np.uint8)
    y = 0
    for tile in tiles:
        sheet[y:y + tile.shape[0], :tile.shape[1]] = tile
        y += tile.shape[0]
    if max(sheet.shape[:2]) > BAND_STRIP_MAX_DIM:
        s = BAND_STRIP_MAX_DIM / max(sheet.shape[:2])
        sheet = cv2.resize(sheet, (int(sheet.shape[1] * s), int(sheet.shape[0] * s)))
    out = _output_dir()
    path = out / f"band_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.jpeg"
    cv2.imwrite(str(path), sheet, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
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

    def since(self, window_id: str, seconds: float, limit: int,
              expect: tuple[str, ...] | None = None, expect_all: bool = False,
              ocr_limit: int = MAX_OCR_BANDS) -> dict:
        with self._lock:
            session = self._sessions.get(window_id)
        if not session:
            return {"success": False, "error": "WATCH_NOT_RUNNING: start a watch first"}
        return session.since(seconds, limit, expect, expect_all, ocr_limit)

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
