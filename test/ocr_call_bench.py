"""OCR 调用方式的真实基准：面积不是成本，调用次数和模型组合才是。

对真实 toast 帧分别测：
  A. parse(整幅 band)          —— v3/v4 的做法
  B. parse(单个 ROI)           —— e95c6f4 的做法（37 次调用）
  C. _detect_text(单个 ROI)    —— 只跑 OCR，不跑 YOLO
  D. _detect_text(ROI 拼合图)  —— 每帧一次调用，摊薄固定开销

跑法（dsh-doraemon 目录下）：
    PYTHONPATH=kernel ./.runtime/venv/Scripts/python.exe test/ocr_call_bench.py
"""

from __future__ import annotations

import time
from pathlib import Path

import cv2

from dsh_desktop import watch
from dsh_desktop.ui_parser import UIParser

WEIGHTS = r"C:\Users\kennyyzhang\AppData\Local\dsh-desktop\weights"
FRAMES = Path(r"C:\Users\kennyyzhang\Documents\deepseek-harness\out\watch-toast")


def timed(label: str, fn) -> None:
    t0 = time.time()
    try:
        items = fn() or []
    except Exception as exc:  # noqa: BLE001
        print(f"  {label:34s} 失败 {type(exc).__name__}: {exc}")
        return
    texts = [str(i.get("text") or "").strip() for i in items if i.get("text")]
    texts = [t for t in texts if t and t.lower() not in {"icon", "ui_element"}]
    joined = " | ".join(texts)[:150]
    print(f"  {label:34s} {(time.time()-t0)*1000:7.0f}ms  {len(texts):3d} 条  {joined}")


def main() -> None:
    parser = UIParser(weights_dir=WEIGHTS)
    files = sorted(FRAMES.glob("*.png"))
    prev = None
    for f in files:
        img = cv2.imread(str(f))
        if prev is None:
            prev = img
            continue
        rois = watch._diff_rois(prev, img)
        if not rois:
            prev = img
            continue
        print(f"\n=== {f.name} ===")
        band = watch._bottom_band(img)
        if band is not None:
            timed("A parse(band)", lambda: parser.parse(band))
        timed("B parse(最大 ROI)", lambda: parser.parse(rois[0]))
        timed("C _detect_text(最大 ROI)", lambda: parser._detect_text(rois[0]))
        # D：把这一帧的所有 ROI 垂直拼成一张图，一次调用
        sheet = watch._roi_sheet(rois) if hasattr(watch, "_roi_sheet") else None
        if sheet is not None:
            timed("D _detect_text(ROI 拼合图)", lambda: parser._detect_text(sheet))
        prev = img
        if f.name.startswith("t008"):  # 重点看 toast 帧，后面的帧重复性高
            pass


if __name__ == "__main__":
    main()
