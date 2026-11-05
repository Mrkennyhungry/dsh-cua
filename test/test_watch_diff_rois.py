"""_diff_rois：只 OCR 变化区域，而不是整幅下半屏。

这条路径要同时满足三件事：不漏（所有变化区域都留）、快（区域远小于 band）、
省上下文（静态内容根本不产生区域）。单测锁定这些性质。

跑法：
    PYTHONPATH=kernel ./.runtime/venv/Scripts/python.exe -m pytest test/test_watch_diff_rois.py -q
"""

from __future__ import annotations

import numpy as np
import pytest

from dsh_desktop import watch


def _blank(w: int = 800, h: int = 600, value: int = 200) -> np.ndarray:
    return np.full((h, w, 3), value, dtype=np.uint8)


def test_identical_frames_produce_no_roi():
    a = _blank()
    assert watch._diff_rois(a, a.copy()) == []


def test_none_and_empty_are_safe():
    a = _blank()
    assert watch._diff_rois(None, a) == []
    assert watch._diff_rois(a, None) == []
    assert watch._diff_rois(a, np.zeros((0, 0, 3), dtype=np.uint8)) == []


def test_shape_mismatch_returns_empty_so_caller_falls_back_to_band():
    assert watch._diff_rois(_blank(800, 600), _blank(400, 300)) == []


def test_small_toast_region_is_captured_at_source_resolution():
    before = _blank()
    after = before.copy()
    # 底部一条 toast：约 300x40，远小于整屏
    after[520:560, 250:550] = 40
    rois = watch._diff_rois(before, after)
    assert len(rois) == 1, f"toast 应产生且只产生一个区域，实际 {len(rois)}"
    roi = rois[0]
    # 关键：ROI 是原图裁剪，不能再被缩到不可读
    assert roi.shape[0] >= 40, "ROI 高度必须覆盖 toast 本体"
    assert roi.shape[1] >= 300, "ROI 宽度必须覆盖 toast 本体"
    assert (roi == 40).any(), "ROI 里必须包含变化后的像素"
    # 没变化的部分不应被拉进来太多
    assert roi.shape[0] * roi.shape[1] < 800 * 600 * 0.1


def test_tiny_change_below_min_area_is_ignored():
    before = _blank()
    after = before.copy()
    after[100:105, 100:105] = 30          # 25px，光标级噪声
    assert watch._diff_rois(before, after) == []


def test_full_page_repaint_is_not_treated_as_a_popup():
    before = _blank(value=200)
    after = _blank(value=20)              # 整屏都变
    rois = watch._diff_rois(before, after)
    assert rois == [], "整页重绘不应被当成局部消息区域"


def test_multiple_regions_are_all_kept_not_ranked():
    before = _blank()
    after = before.copy()
    after[520:560, 250:550] = 40          # toast
    after[40:90, 620:760] = 60            # 另一处变化
    rois = watch._diff_rois(before, after)
    assert len(rois) == 2, "两处变化都必须保留，不能只挑一个"


def test_rois_never_exceed_frame_bounds():
    before = _blank()
    after = before.copy()
    after[580:600, 760:800] = 20          # 贴边变化，检查 padding 不越界
    for roi in watch._diff_rois(before, after):
        assert roi.shape[0] > 0 and roi.shape[1] > 0
        assert roi.shape[0] <= 600 and roi.shape[1] <= 800


def test_frame_rois_default_is_none_for_backward_compat():
    frame = watch.Frame(ts=0.0, image=_blank(), global_score=0.0, local_score=0.0)
    assert frame.rois is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
