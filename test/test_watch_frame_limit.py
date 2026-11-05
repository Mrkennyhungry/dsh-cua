"""帧级上限与零预算：limit 只限制"给模型看多少帧"，不决定"哪几帧有机会被 OCR"。

旧实现 `selected[-limit:]` 在预算逻辑之前按"最新 N 帧"截断，窗口早期的小面积
toast 会整帧消失。现在帧级也走「小面积变化优先 + 时间均匀抽样」。

另：ocr_limit=0 必须真的是 0 次 OCR（只看画面），不能被 max(1, …) 夹成 1。

跑法：
    PYTHONPATH=kernel ./.runtime/venv/Scripts/python.exe -m pytest test/test_watch_frame_limit.py -q
"""

from __future__ import annotations

import time
from unittest.mock import Mock, patch

import numpy as np
import pytest

from dsh_desktop import watch


def _frame(ts: float, frac: float) -> watch.Frame:
    roi = np.full((40, 200, 3), 60, np.uint8)
    return watch.Frame(ts=ts, image=np.zeros((8, 8, 3), np.uint8),
                       global_score=1.0, local_score=12.0,
                       bottom_band=None, rois=[roi], roi_area_frac=frac)


def _session(frames: list) -> watch.WatchSession:
    session = watch.WatchSession("lim", 1, Mock())
    for frame in frames:
        session.frames.append(frame)
    return session


def _run(session: watch.WatchSession, **kwargs) -> tuple[dict, list]:
    calls: list = []

    def fake_band_texts(img, *args, **ks):
        calls.append(img)
        return ["上传失败"]

    with patch.object(watch, "_band_texts", side_effect=fake_band_texts):
        return session.since(120, 8, **kwargs), calls


def test_limit_is_a_display_bound_not_an_ocr_bound():
    """30 个大面积重绘帧里夹一条早期小面积 toast：limit=8 也不能把它整帧丢掉。"""
    now = time.time()
    frames = [_frame(now - 100 + i * 3, 0.20) for i in range(30)]
    toast = _frame(now - 97, 0.004)          # 时间窗最早那批里的小面积提示
    frames.insert(1, toast)
    session = _session(frames)

    result, _ = _run(session)
    shown = [row["ts"] for row in result["timeline"]]

    assert len(result["timeline"]) == 8, "limit 仍然要限制展示帧数"
    assert round(toast.ts - session.started_at, 2) in shown, \
        "小面积 toast 必须留在展示的 8 帧里，不能被「取最新 N 帧」挤掉"
    assert result["frames_in_window"] == len(frames)
    assert result["truncated"] is True
    assert "frames_warning" in result, "帧被裁掉时必须告诉调用方可以复核"


def test_frame_selection_keeps_time_coverage():
    frames = [_frame(1000.0 + i, 0.30) for i in range(40)]   # 全是大面积
    kept, truncated = watch._select_frames(frames, 8)
    ts = [f.ts for f in kept]
    assert truncated and len(kept) == 8
    assert ts == sorted(ts), "返回必须按时间序"
    assert ts[0] < 1005.0 and ts[-1] > 1035.0, "首尾帧都要有机会"


def test_ocr_limit_zero_runs_no_ocr():
    """ocr_limit=0 是「只看画面」：不出文字，但仍要有时间线与证据图。"""
    now = time.time()
    session = _session([_frame(now - 5, 0.01)])

    result, calls = _run(session, ocr_limit=0)

    assert calls == [], "ocr_limit=0 不应触发任何 OCR"
    assert result["ocr"]["runs"] == 0
    assert result["ocr"]["truncated"] is False
    assert "ocr_limit=0" in result["ocr"]["warning"]
    assert result["frames"] == 1
    assert "image_path" in result


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
