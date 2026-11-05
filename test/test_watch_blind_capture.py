"""桌面抓取盲区、窗口补通道与采样寿命。

WinUI / DirectComposition 应用的合成层内容，GDI 桌面截图拍不到（Win11 记事本
实测：编辑区全白，PrintWindow 正常）。这里锁定三件事：

1. _detect_blind 只在「桌面那块是纯色 + 窗口自己有内容 + 两者差很多」时成立；
2. 判定为盲之后，_blind_rois 会把窗口内的变化矩形并进来；
3. ttl 到点只停线程，已经采到的帧仍然读得到。

跑法：
    PYTHONPATH=kernel ./.runtime/venv/Scripts/python.exe -m pytest test/test_watch_blind_capture.py -q
"""

from __future__ import annotations

import time
from unittest.mock import Mock, patch

import numpy as np
import pytest

from dsh_desktop import watch


def _blank(w: int = 400, h: int = 300, value: int = 255) -> np.ndarray:
    return np.full((h, w, 3), value, dtype=np.uint8)


def _with_text(w: int = 400, h: int = 300) -> np.ndarray:
    img = _blank(w, h)
    for i in range(6):
        img[40 + i * 20:50 + i * 20, 30:330] = 20      # 模拟几行文字
    return img


def _session(capture=None) -> watch.WatchSession:
    return watch.WatchSession("blind", 1, capture or Mock())


def test_blind_when_desktop_is_flat_but_window_has_content():
    session = _session()
    with patch("win32gui.GetWindowRect", return_value=(0, 0, 400, 300)):
        assert session._detect_blind(_blank(), _with_text()) is True


def test_not_blind_when_desktop_matches_the_window():
    session = _session()
    window = _with_text()
    with patch("win32gui.GetWindowRect", return_value=(0, 0, 400, 300)):
        assert session._detect_blind(window.copy(), window) is False


def test_blind_detection_never_raises_on_bad_input():
    session = _session()
    assert session._detect_blind(None, _with_text()) is False
    assert session._detect_blind(_blank(), None) is False


def test_blind_session_adds_window_rois():
    """判定为盲之后，窗口内的变化必须能进 rois，否则 WinUI 窗口永远读不到字。"""
    session = _session()
    session.sampled = 5                # 不是探测帧，走补通道分支
    session._blind = True
    before = _blank()
    after = before.copy()
    after[120:170, 60:340] = 30        # 窗口内出现一条提示
    session._prev_window = before
    with patch.object(session, "_grab_window", return_value=after):
        rois = session._blind_rois(_blank(), [])
    assert len(rois) == 1
    assert rois[0].shape[0] >= 50 and rois[0].shape[1] >= 280


def test_non_blind_session_skips_the_window_channel():
    session = _session()
    session.sampled = 5
    session._blind = False
    with patch.object(session, "_grab_window", return_value=_with_text()):
        assert session._blind_rois(_blank(), ["desktop-roi"]) == ["desktop-roi"]


def test_ttl_stops_sampling_but_keeps_collected_frames():
    capture = Mock()
    capture.capture_desktop.return_value = _with_text()
    session = watch.WatchSession("ttl", 1, capture, ttl=0.2)
    session.start()
    time.sleep(0.6)

    assert session.expired is True
    assert session.active is False, "到点必须停线程，不能一直 10fps 采样"
    # 线程停了，但 this 会话仍可读：since 不该报 WATCH_NOT_RUNNING
    result = session.since(60, 4)
    assert result["success"] is True
    assert result["expired"] is True


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
