# -*- coding: utf-8 -*-
"""Round 2 of the 2026-11-05 audit: UIA merge, cleanup, diagnostics, watch."""
import os
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np

from dsh_desktop import desktop_tools, watch


class UiaMergeTests(unittest.TestCase):
    """BUG-03: one shared UIA filter, used by both links, without activation."""

    def test_shared_helper_skips_containers_and_placeholders(self):
        tree = {"controls": [
            {"name": "保存", "control_type": "Button", "bbox": [10, 10, 80, 30]},
            {"name": "x", "control_type": "Button", "bbox": [10, 10, 80, 30]},   # too short
            {"name": "整个页面正文", "control_type": "Pane", "bbox": [0, 0, 1000, 900]},
            {"name": "icon", "control_type": "Image", "bbox": [10, 10, 30, 30]},
        ]}
        with patch.object(desktop_tools, "_desktop_uia_tree_once", return_value=tree) as call:
            rows = desktop_tools._uia_named_items("win-x")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], "保存")
        self.assertIn("click_x", rows[0])

    def test_analyze_uses_activate_false(self):
        # The whole point: a vision observation must never steal foreground.
        with patch.object(desktop_tools, "_desktop_uia_tree_once",
                          return_value={"controls": []}) as call:
            desktop_tools._uia_named_items("win-x", activate=False)
        self.assertFalse(call.call_args.kwargs.get("activate", True))

    def test_element_table_uses_the_shared_helper(self):
        with patch.object(desktop_tools, "_uia_named_items",
                          return_value=[{"id": 0, "text": "保存", "type": "Button",
                                         "bbox": [10, 10, 80, 30]}]) as helper:
            with patch.object(desktop_tools, "_controller", return_value=Mock()), \
                 patch.object(desktop_tools, "desktop_analyze",
                              return_value={"ui_elements": []}), \
                 patch.object(desktop_tools, "_STATE") as state:
                state._lock = __import__("threading").Lock()
                state._windows = {"win-x": {"hwnd": 1}}
                # source="uia" avoids the OCR fallback entirely.
                result = desktop_tools.desktop_element_table("win-x", "uia", True, False)
        self.assertTrue(helper.called)
        self.assertEqual(result["source"], "uia")


class WatchTests(unittest.TestCase):
    """Design-01: dual scores, ring buffer, time ordering, bounded sheet."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._env = patch.dict(os.environ, {"DSH_WATCH_DIR": self.tmp.name})
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_static_frames_keep_nothing(self):
        frame = np.full((80, 120, 3), 100, np.uint8)
        sig = watch._signature(frame)
        g, l = watch._scores(sig, sig)
        self.assertEqual((g, l), (0.0, 0.0))

    def test_small_bright_region_triggers_local_not_global(self):
        a = np.full((360, 640, 3), 20, np.uint8)
        b = a.copy()
        b[330:350, 100:300] = 240          # a bottom "toast"
        g, l = watch._scores(watch._signature(a), watch._signature(b))
        self.assertLess(g, watch.GLOBAL_THRESHOLD)
        self.assertGreater(l, watch.LOCAL_THRESHOLD)

    def test_full_page_change_triggers_global(self):
        a = np.full((360, 640, 3), 20, np.uint8)
        b = np.full((360, 640, 3), 220, np.uint8)
        g, l = watch._scores(watch._signature(a), watch._signature(b))
        self.assertGreater(g, watch.GLOBAL_THRESHOLD)

    def test_since_returns_time_ordered_not_score_ordered(self):
        session = watch.WatchSession("win-x", 1, Mock())
        session.started_at = 0.0
        now = time.time()
        img = np.zeros((20, 20, 3), np.uint8)
        session.frames.append(watch.Frame(now - 3, img, 100.0, 100.0))  # high score, older
        session.frames.append(watch.Frame(now - 2, img, 0.2, 11.0))     # toast, newer
        session.frames.append(watch.Frame(now - 1, img, 0.1, 9.0))      # toast, newest
        r = session.since(10, 8)
        self.assertEqual(r["frames"], 3)
        self.assertEqual(r["timeline"][0]["global"], 100.0)  # still time-ordered
        self.assertEqual(r["timeline"][-1]["local"], 9.0)

    def test_ring_buffer_is_bounded(self):
        session = watch.WatchSession("win-x", 1, Mock())
        self.assertEqual(session.frames.maxlen, watch.RING_SIZE)
        for i in range(watch.RING_SIZE + 20):
            session.frames.append(watch.Frame(float(i), np.zeros((2, 2, 3), np.uint8), 0, 0))
        self.assertLessEqual(len(session.frames), watch.RING_SIZE)

    def test_since_without_session_is_actionable(self):
        self.assertIn("WATCH_NOT_RUNNING", watch.registry().since("never", 5, 8)["error"])

    def test_contact_sheet_is_bounded_jpeg(self):
        from pathlib import Path
        frames = [watch.Frame(1.0 + i, np.full((300, 400, 3), i * 20, np.uint8), 0.2, 12.0)
                  for i in range(4)]
        path = Path(watch._contact_sheet(frames))
        self.addCleanup(path.unlink, missing_ok=True)
        img = cv2.imread(str(path))
        self.assertIsNotNone(img)
        self.assertLessEqual(max(img.shape[:2]), watch.SHEET_MAX_DIM)
        self.assertLess(path.stat().st_size, 4_500_000)


class DiagnosticMarkerTests(unittest.TestCase):
    """BUG-08: markers must land inside a downscaled desktop capture."""

    def test_capture_reports_source_dimensions(self):
        # controller.capture_desktop must expose the pre-resize size so the
        # diagnostic can convert physical cursor pixels into image pixels.
        import inspect
        from dsh_desktop import controller
        src = inspect.getsource(controller.AppController.capture_desktop)
        self.assertIn('"source_width"', src)
        self.assertIn('"source_height"', src)

    def test_to_image_scales_physical_coords(self):
        # 3840x2160 physical -> 1366x768 stored. A cursor at (1920, 900)
        # must map inside the stored image, not off-canvas.
        width, height = 1366, 768
        fx, fy = width / 3840.0, height / 2160.0
        x, y = int(1920 * fx), int(900 * fy)
        self.assertEqual((x, y), (683, 320))
        self.assertLess(x, width)
        self.assertLess(y, height)


class CleanupReportShapeTests(unittest.TestCase):
    """BUG-01: cleanup reports task-derived windows it closed."""

    def test_report_has_closed_opened_windows_key(self):
        with patch.object(desktop_tools, "ownership_snapshot",
                          return_value={"launched_pids": {}, "bound_pids": set()}), \
             patch.object(desktop_tools, "_STATE"):
            # close_launched_apps=False skips the sweep; just assert the key exists.
            r = desktop_tools.cleanup_session_footprint(
                close_browser=False, close_launched_apps=False, unbind_windows=False)
        self.assertIn("closed_opened_windows", r)
        self.assertEqual(r["closed_opened_windows"], [])

    def test_task_derived_window_is_closed_and_reported(self):
        """Live QQGallery case: double-clicking a thumbnail opened a same-pid
        "图片查看器" window that was never in _launched_windows. Cleanup must
        close it and name it in closed_opened_windows."""
        import win32con
        import win32gui

        baseline = [{"window_key": "100:999:1", "hwnd": 100}]  # pre-existing
        viewer_hwnd = 777001                                    # task-derived
        calls: list[int] = []

        def fake_enum(cb, _param):
            cb(viewer_hwnd, None)

        def fake_visible(hwnd):
            return hwnd == viewer_hwnd

        def fake_post(hwnd, msg, wparam, lparam):
            calls.append(hwnd)

        fake_gui = Mock()
        fake_gui.IsWindowVisible.side_effect = fake_visible
        fake_gui.GetWindowText.return_value = "图片查看器 - 腾讯相册管家"
        fake_gui.GetClassName.return_value = "Chrome_WidgetWin_1"
        fake_gui.EnumWindows.side_effect = fake_enum
        fake_gui.PostMessage.side_effect = fake_post
        fake_gui.IsWindow.side_effect = lambda h: h not in calls
        fake_gui.GetAncestor.return_value = viewer_hwnd
        fake_gui.WindowFromPoint.return_value = viewer_hwnd

        with patch.object(desktop_tools, "ownership_snapshot",
                          return_value={"launched_pids": {}, "bound_pids": {23404}}), \
             patch.object(desktop_tools, "_current_window_key",
                          return_value="777001:23404:1"), \
             patch.object(desktop_tools, "win32process") as proc, \
             patch.dict("sys.modules", {"win32gui": fake_gui, "win32con": win32con}), \
             patch.object(desktop_tools, "_STATE"):
            proc.GetWindowThreadProcessId.return_value = (0, 23404)
            r = desktop_tools.cleanup_session_footprint(
                close_browser=False, close_launched_apps=True, unbind_windows=False,
                baseline_windows=baseline, timeout_per_app=0.1)
        self.assertEqual(calls, [viewer_hwnd])
        self.assertEqual(len(r["closed_opened_windows"]), 1)
        self.assertIn("图片查看器", str(r["closed_windows"]))


if __name__ == "__main__":
    unittest.main()
