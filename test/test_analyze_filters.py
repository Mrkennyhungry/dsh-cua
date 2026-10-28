"""Filters for analyze output, and the occlusion report that goes with it.

Two separate concerns live here:

- Trimming the element list (`only_actionable`, `max_elements`) must never touch the
  capture. The user's original reason for the full-window shot is that anything
  covering the target — a dialog, a dropdown in its own top-level window — has to be
  visible from the topmost perspective. So a filter may drop unaddressable elements;
  it may not change one pixel of the picture.
- When something *does* cover the target, say so. The window-scope capture is a screen
  crop, so the occluder is in the picture and its text is in the element list; without
  a report the caller sees buttons that belong to another window and cannot tell.
"""

import unittest
from unittest import mock

from dsh_desktop import desktop_tools


class ElementAddressabilityTests(unittest.TestCase):
    def test_plain_text_is_addressable(self):
        self.assertTrue(desktop_tools._element_is_addressable({"text": "保存"}))

    def test_icon_with_a_caption_is_addressable(self):
        self.assertTrue(desktop_tools._element_is_addressable({"text": "icon", "caption": "关闭"}))

    def test_placeholder_without_caption_is_not(self):
        for element in ({"text": "icon"}, {"text": "未知"}, {"text": ""},
                        {"text": "icon", "caption": "未知"}, {}):
            self.assertFalse(desktop_tools._element_is_addressable(element), element)

    def test_single_character_text_counts_as_addressable(self):
        # One-character labels are real targets in Chinese UIs ("是"/"否").
        self.assertTrue(desktop_tools._element_is_addressable({"text": "是"}))


class _Rect:
    def __init__(self, left, top, width, height):
        self.left, self.top = left, top
        self.width, self.height = width, height


class _FakeWin32:
    """Just enough of win32gui for the z-order walk."""

    def __init__(self, chain, windows, target):
        self.chain = chain
        self.windows = windows  # hwnd -> dict(rect, title, class, visible, iconic)
        self.target = target

    def GetTopWindow(self, _):
        return self.chain[0] if self.chain else 0

    def GetWindow(self, hwnd, _):
        try:
            index = self.chain.index(hwnd)
        except ValueError:
            return 0
        return self.chain[index + 1] if index + 1 < len(self.chain) else 0

    def IsWindowVisible(self, hwnd):
        return self.windows.get(hwnd, {}).get("visible", True)

    def IsIconic(self, hwnd):
        return self.windows.get(hwnd, {}).get("iconic", False)

    def GetWindowText(self, hwnd):
        return self.windows.get(hwnd, {}).get("title", "")

    def GetClassName(self, hwnd):
        return self.windows.get(hwnd, {}).get("class", "")

    def GetWindowRect(self, hwnd):
        return self.windows.get(hwnd, {}).get("rect", (0, 0, 0, 0))


TARGET = 100
ABOVE_FULL = 201
ABOVE_SLIVER = 202
BELOW = 203


class OcclusionTests(unittest.TestCase):
    def probe(self, windows, chain=(ABOVE_FULL, ABOVE_SLIVER, TARGET, BELOW)):
        fake = _FakeWin32(list(chain), windows, TARGET)
        with mock.patch.object(desktop_tools, "win32gui", fake):
            return desktop_tools._overlapping_windows(TARGET, _Rect(100, 100, 1000, 800))

    def test_window_covering_the_client_area_is_reported(self):
        found = self.probe({
            ABOVE_FULL: {"rect": (0, 0, 1200, 1000), "title": "下载 - 文件资源管理器", "class": "CabinetWClass"},
        })
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["title"], "下载 - 文件资源管理器")
        self.assertEqual(found[0]["overlap"], 1.0)

    def test_slivers_and_hidden_windows_are_ignored(self):
        found = self.probe({
            ABOVE_SLIVER: {"rect": (100, 100, 140, 200), "title": "细条", "class": "X"},  # 0.4%
            ABOVE_FULL: {"rect": (0, 0, 1200, 1000), "title": "大窗", "class": "Y", "visible": False},
        })
        self.assertEqual(found, [])

    def test_windows_below_the_target_are_not_occluders(self):
        found = self.probe({
            BELOW: {"rect": (0, 0, 1200, 1000), "title": "在下层", "class": "Z"},
        })
        self.assertEqual(found, [])

    def test_desktop_chrome_is_skipped(self):
        found = self.probe({
            ABOVE_FULL: {"rect": (0, 0, 1200, 1000), "title": "Program Manager", "class": "Progman"},
        })
        self.assertEqual(found, [])

    def test_results_are_sorted_by_overlap(self):
        found = self.probe({
            ABOVE_FULL: {"rect": (100, 100, 800, 900), "title": "一半", "class": "A"},
            ABOVE_SLIVER: {"rect": (0, 0, 1100, 900), "title": "几乎全盖", "class": "B"},
        })
        self.assertEqual([item["title"] for item in found], ["几乎全盖", "一半"])

    def test_no_client_rect_means_no_claim(self):
        self.assertEqual(desktop_tools._overlapping_windows(TARGET, None), [])


if __name__ == "__main__":
    unittest.main()
