"""预算不足时的目标选择：不能再把 toast 挤掉。

历史上按 local 分排序、取最新 N 帧、取最早 N 个 ROI 都漏过 toast。
本版改为「小面积变化帧优先 + 时间均匀抽样」，这里锁定该行为。

跑法：
    PYTHONPATH=kernel ./.runtime/venv/Scripts/python.exe -m pytest test/test_watch_target_selection.py -q
"""

from __future__ import annotations

import numpy as np
import pytest

from dsh_desktop import watch


def _target(ts: float, frac: float):
    frame = watch.Frame(ts=ts, image=np.zeros((4, 4, 3), np.uint8),
                        global_score=0.0, local_score=0.0, roi_area_frac=frac)
    return (frame, frame.image, "roi_sheet")


def test_no_truncation_when_within_budget():
    targets = [_target(float(i), 0.01) for i in range(5)]
    chosen, truncated = watch._select_targets(targets, budget=16)
    assert not truncated and len(chosen) == 5


def test_small_change_frames_survive_budget():
    """大量整页变化 + 一条 toast：toast 必须留下。"""
    big = [_target(float(i), 0.20) for i in range(30)]     # 大面积重绘
    toast = _target(15.0, 0.004)                            # 小面积 toast
    chosen, truncated = watch._select_targets(big + [toast], budget=8)
    assert truncated
    assert toast in chosen, "小面积变化帧不能被预算挤掉"


def test_time_coverage_is_even_not_just_latest_or_earliest():
    targets = [_target(float(i), 0.30) for i in range(40)]  # 全是大面积
    chosen, _ = watch._select_targets(targets, budget=8)
    ts = [c[0].ts for c in chosen]
    assert ts == sorted(ts), "返回必须按时间序"
    assert ts[0] < 5.0, "最早的帧也要有机会"
    assert ts[-1] > 35.0, "最新的帧也要有机会"


def test_small_frames_evenly_sampled_when_they_alone_exceed_budget():
    small = [_target(float(i), 0.002) for i in range(20)]
    chosen, truncated = watch._select_targets(small, budget=5)
    assert truncated and len(chosen) == 5
    ts = [c[0].ts for c in chosen]
    assert ts == sorted(ts) and ts[0] < 2.0 and ts[-1] > 17.0


def test_zero_budget_returns_nothing():
    chosen, truncated = watch._select_targets([_target(1.0, 0.01)], budget=0)
    assert chosen == [] and truncated


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
