"""候选可见性：词表用于"多的时候怎么取舍"，不该在候选少时藏起非命中文字。

跑法：
    PYTHONPATH=kernel ./.runtime/venv/Scripts/python.exe -m pytest test/test_watch_candidates_visibility.py -q
"""

from __future__ import annotations

import time
from unittest.mock import Mock, patch

import numpy as np
import pytest

from dsh_desktop import watch


def _session_with(texts: list[str], roi_count: int = 1) -> watch.WatchSession:
    session = watch.WatchSession("vis", 1, Mock())
    roi = np.full((40, 200, 3), 60, np.uint8)
    for i in range(roi_count):
        session.frames.append(watch.Frame(
            ts=time.time() + i, image=np.zeros((8, 8, 3), np.uint8),
            global_score=1.0, local_score=12.0,
            bottom_band=None, rois=[roi], roi_area_frac=0.001))
    session._texts = texts  # type: ignore[attr-defined]
    return session


def _run(session: watch.WatchSession, **kwargs) -> dict:
    with patch.object(watch, "_band_texts",
                      side_effect=lambda *a, **k: session._texts):  # type: ignore[attr-defined]
        return session.since(60, 16, **kwargs)


def test_non_matching_text_visible_when_few_candidates():
    """「已复制到剪贴板」不含任何错误词，但它可能正是要等的消息。"""
    session = _session_with(["上传失败", "已复制到剪贴板"])
    result = _run(session)
    texts = [c["text"] for c in result.get("text_candidates", [])]
    assert "已复制到剪贴板" in texts
    assert result["text_candidates_scope"] == "all"
    assert len(result["text_matches"]) == 1


def test_matches_only_when_many_candidates():
    many = [f"无关文字 {i}" for i in range(30)] + ["上传失败"]
    session = _session_with(many)
    result = _run(session)
    assert result["text_candidates_scope"] == "matches_only"
    assert "text_candidates" not in result
    assert result["text_other_count"] >= 30


def test_expect_all_forces_full_output_even_when_many():
    many = [f"无关文字 {i}" for i in range(30)] + ["上传失败"]
    session = _session_with(many)
    result = _run(session, expect_all=True)
    assert result["text_candidates_scope"] == "all"
    assert len(result["text_candidates"]) == len(many)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
