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

    def test_bottom_band_is_a_pure_geometric_crop(self):
        """No detection, no filtering: colour/position must not affect it."""
        for bg, fg in [((240, 240, 240), (45, 48, 60)),   # dark pill
                       ((60, 60, 60), (245, 245, 245))]:  # light pill
            frame = np.full((600, 1000, 3), bg, np.uint8)
            cv2.rectangle(frame, (350, 530), (650, 566), fg, -1)
            band = watch._bottom_band(frame)
            self.assertIsNotNone(band)
            self.assertEqual(band.shape[1], 1000)
            self.assertGreater(band.shape[0], 100)   # ~22% of 600
            self.assertTrue(np.any(band != bg))

    def test_bottom_right_notification_is_kept_in_the_band(self):
        # Desktop-level notification outside any bound window: full-desktop
        # sampling plus a full-width band crop keeps it, with no corner bias.
        frame = np.full((900, 1600, 3), 235, np.uint8)
        cv2.rectangle(frame, (1180, 730), (1540, 790), (45, 48, 60), -1)
        band = watch._bottom_band(frame)
        self.assertIsNotNone(band)
        self.assertTrue(np.any(band[:, 1180:1540] < 90))

    def test_session_prefers_full_desktop_capture(self):
        cap = Mock()
        cap.capture_desktop.return_value = np.full((90, 160, 3), 200, np.uint8)
        cap.capture.return_value = np.full((40, 40, 3), 100, np.uint8)
        session = watch.WatchSession("win-x", 1, cap)
        frame, scope = session._grab()
        self.assertEqual(scope, "desktop")
        self.assertEqual(frame.shape[0], 90)
        self.assertFalse(cap.capture.called)

    def test_session_falls_back_to_window_capture(self):
        cap = Mock(spec=["capture"])
        cap.capture.return_value = np.full((40, 40, 3), 100, np.uint8)
        session = watch.WatchSession("win-x", 1, cap)
        frame, scope = session._grab()
        self.assertEqual(scope, "window")
        self.assertTrue(cap.capture.called)

    def test_band_texts_uses_the_existing_ocr_and_deduplicates(self):
        parser = Mock(spec=["parse"])
        parser.parse.return_value = [
            {"text": "1 个文件格式不支持，已忽略", "bbox": [10, 10, 400, 40]},
            {"text": "1 个文件格式不支持，已忽略", "bbox": [10, 12, 400, 42]},
            {"text": "icon", "bbox": [1, 1, 9, 9]},
        ]
        controller = Mock()
        controller.ui_parser = parser
        from dsh_desktop import desktop_tools
        with patch.object(desktop_tools, "_controller", return_value=controller):
            texts = watch._band_texts(np.zeros((60, 800, 3), np.uint8))
        self.assertTrue(parser.parse.called)
        self.assertEqual(texts, ["1 个文件格式不支持，已忽略"])

    def test_ocr_covers_every_distinct_band_in_time_order(self):
        """No score-based ranking may silently drop a toast frame."""
        session = watch.WatchSession("win-x", 1, Mock())
        ocrd = []

        def fake_texts(band):
            dark_cols = np.where(band[0, :, 0] < 100)[0]
            ocrd.append(int(dark_cols[0] // 100) if len(dark_cols) else -1)
            return ["noise"]

        with patch.object(watch, "_band_texts", side_effect=fake_texts):
            for i in range(6):
                band = np.full((60, 800, 3), 240, np.uint8)
                band[:, i * 100:(i + 1) * 100] = 30
                score = 90.0 if i == 2 else 10.0
                session.frames.append(watch.Frame(time.time() + i, band, 0.2, score, band))
            session.since(10, 8, expect=("失败",))
        self.assertEqual(ocrd, [0, 1, 2, 3, 4, 5], "all distinct bands, time ordered")

    def test_identical_bands_are_ocrd_only_once(self):
        session = watch.WatchSession("win-x", 1, Mock())
        band = np.full((60, 800, 3), 240, np.uint8)
        calls = []

        def fake_texts(b):
            calls.append(1)
            return ["same message"]

        with patch.object(watch, "_band_texts", side_effect=fake_texts):
            for i in range(5):
                session.frames.append(watch.Frame(time.time() + i, band, 0.2, 40.0, band))
            session.since(10, 8, expect_all=True)
        self.assertEqual(len(calls), 1)

    def test_band_texts_never_raises_when_ocr_unavailable(self):
        from dsh_desktop import desktop_tools
        with patch.object(desktop_tools, "_controller", return_value=None):
            self.assertEqual(watch._band_texts(np.zeros((60, 800, 3), np.uint8)), [])

    def test_since_returns_keyword_matches_not_a_verdict(self):
        from pathlib import Path
        session = watch.WatchSession("win-x", 1, Mock())
        band = np.full((60, 800, 3), 240, np.uint8)
        with patch.object(watch, "_band_texts", return_value=["上传失败，请重试"]):
            session.frames.append(watch.Frame(time.time(), band, 0.2, 40.0, band))
            r = session.since(10, 8, expect=("失败",))
        self.assertEqual(r["text_matches"][0]["text"], "上传失败，请重试")
        self.assertEqual(r["text_matches"][0]["matched_terms"], ["失败"])
        # The sampler must NOT claim a popup was detected.
        self.assertNotIn("toast", r)
        self.assertNotIn("detected", r)
        self.assertIn("evidence_image", r)
        self.addCleanup(Path(r["evidence_image"]).unlink, missing_ok=True)

    def test_keyword_signal_keeps_the_message_and_drops_taskbar_noise(self):
        candidates = [
            {"ts": 1.0, "text": "29°C", "global": 0.4, "local": 17.0},
            {"ts": 1.0, "text": "Q搜索", "global": 0.4, "local": 17.0},
            {"ts": 2.0, "text": "1 个文件格式不支持，已忽略", "global": 0.9, "local": 93.0},
            {"ts": 2.0, "text": "2026/11/5 16:37", "global": 0.9, "local": 93.0},
        ]
        r = watch.rank_text_candidates(candidates, ("不支持", "忽略", "失败"))
        self.assertEqual(len(r["matches"]), 1)
        self.assertIn("不支持", r["matches"][0]["text"])
        self.assertEqual(r["matches"][0]["matched_terms"], ["不支持", "忽略"])
        self.assertEqual(r["other_count"], 3)
        # The unmatched taskbar strings are NOT returned as text.
        self.assertNotIn("text_candidates", r)

    def test_expect_can_be_overridden_and_still_matches(self):
        candidates = [{"ts": 1.0, "text": "上传被拦截，请重试", "global": 0.2, "local": 40.0}]
        r = watch.rank_text_candidates(candidates, ("拦截",))
        self.assertEqual(len(r["matches"]), 1)
        self.assertEqual(r["expect"], ["拦截"])

    def test_no_expect_terms_returns_only_a_count(self):
        r = watch.rank_text_candidates(
            [{"ts": 1.0, "text": "anything", "global": 0.2, "local": 40.0}], ())
        self.assertEqual(r["matches"], [])
        self.assertEqual(r["other_count"], 1)

    def test_since_honours_expect_all(self):
        session = watch.WatchSession("win-x", 1, Mock())
        band = np.full((60, 800, 3), 240, np.uint8)
        with patch.object(watch, "_band_texts", return_value=["29°C"]):
            session.frames.append(watch.Frame(time.time(), band, 0.2, 40.0, band))
            short = session.since(10, 8, expect=("失败",))
            full = session.since(10, 8, expect=("失败",), expect_all=True)
        self.assertEqual(short["text_other_count"], 1)
        self.assertNotIn("text_candidates", short)
        self.assertEqual(full["text_candidates"][0]["text"], "29°C")

    def test_since_deduplicates_the_same_message_across_frames(self):
        session = watch.WatchSession("win-x", 1, Mock())
        band = np.full((60, 800, 3), 240, np.uint8)
        with patch.object(watch, "_band_texts", return_value=["same message"]):
            for i in range(5):
                session.frames.append(watch.Frame(time.time() + i, band, 0.2, 40.0, band))
            r = session.since(10, 8, expect_all=True)
        self.assertEqual(len(r["text_candidates"]), 1)

    def test_two_second_toast_is_recoverable_after_it_vanished(self):
        """Design-01 end-to-end contract.

        A toast appears at t=1s and disappears at t=3s. since() at t=6s must
        still return BOTH boundary frames, in time order, kept by the LOCAL
        score alone (a global mean would dilute the small bar away). This is
        the failure mode the design note measured as 0.12-0.31 global vs
        10-25 local.
        """
        session = watch.WatchSession("win-toast", 1, Mock())
        t0 = time.time()

        def frame(toast: bool) -> np.ndarray:
            img = np.full((400, 700, 3), 235, np.uint8)
            cv2.rectangle(img, (40, 30), (300, 80), (90, 90, 90), -1)
            if toast:
                cv2.rectangle(img, (120, 340), (580, 380), (55, 58, 70), -1)
            return img

        prev = None
        for rel, toast in [(0.0, False), (0.5, False), (1.0, True),
                           (1.5, True), (2.5, True), (3.0, False), (3.5, False)]:
            sig = watch._signature(frame(toast))
            g, l = watch._scores(prev, sig)
            prev = sig
            if g >= watch.GLOBAL_THRESHOLD or l >= watch.LOCAL_THRESHOLD:
                session.frames.append(watch.Frame(t0 + rel, frame(toast), g, l))

        r = session.since(10, 8)
        self.assertGreaterEqual(r["frames"], 2)
        self.assertTrue(any(row["local"] >= watch.LOCAL_THRESHOLD
                            and row["global"] < watch.GLOBAL_THRESHOLD
                            for row in r["timeline"]),
                        "a toast must be retainable by the local score alone")
        ts = [row["ts"] for row in r["timeline"]]
        self.assertEqual(ts, sorted(ts), "frames must stay time-ordered")

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


class CompanionOptInTests(unittest.TestCase):
    """The mascot must stay off unless commandFeedback is explicitly enabled."""

    def test_kernel_startup_does_not_autostart_companion(self):
        import inspect
        from dsh_desktop import eternity
        src = inspect.getsource(eternity.Eternity)
        self.assertIn("DSH_COMPANION_AUTOSTART", src)
        # install() must be inside the gated block, not unconditional.
        gated = src.split("DSH_COMPANION_AUTOSTART", 1)[1]
        self.assertIn("companion.install", gated[:400])

    def test_autostart_defaults_to_off(self):
        import os
        previous = os.environ.pop("DSH_COMPANION_AUTOSTART", None)
        try:
            self.assertNotIn("DSH_COMPANION_AUTOSTART", os.environ)
        finally:
            if previous is not None:
                os.environ["DSH_COMPANION_AUTOSTART"] = previous


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
