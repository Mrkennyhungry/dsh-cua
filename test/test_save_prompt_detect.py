"""Save-prompt detection for close-window.

Win11 Notepad draws the prompt inside the window, so a top-level-dialog check
misses it; the 保存 + 不保存 pair is what identifies a real prompt.
"""

import sys
import types
import unittest
from unittest import mock

from dsh_desktop import desktop_tools


class FakeChild:
    def __init__(self, text, control_type="Button"):
        self._text = text
        self.element_info = types.SimpleNamespace(control_type=control_type)

    def window_text(self):
        return self._text


class FakeWindow:
    def __init__(self, children, exists=True):
        self._children = children
        self._exists = exists

    def exists(self, timeout=0):
        return self._exists

    def descendants(self):
        return list(self._children)


def run_with(children, exists=True):
    fake_desktop = types.SimpleNamespace(window=lambda handle: FakeWindow(children, exists))
    fake_module = types.ModuleType("pywinauto")
    fake_module.Desktop = lambda backend="uia": fake_desktop
    with mock.patch.dict(sys.modules, {"pywinauto": fake_module}):
        return desktop_tools._detect_save_prompt(12345)


class SavePromptDetectionTests(unittest.TestCase):
    def test_chinese_prompt_is_detected(self):
        result = run_with([FakeChild("保存"), FakeChild("不保存"), FakeChild("取消")])
        self.assertTrue(result["save_prompt"])
        self.assertIn("不保存", result["prompt_buttons"])

    def test_english_prompt_is_detected(self):
        result = run_with([FakeChild("Save"), FakeChild("Don't Save"), FakeChild("Cancel")])
        self.assertTrue(result["save_prompt"])

    def test_toolbar_save_button_alone_is_not_a_prompt(self):
        result = run_with([FakeChild("保存"), FakeChild("加粗"), FakeChild("撤消")])
        self.assertFalse(result["save_prompt"])

    def test_no_buttons_at_all(self):
        self.assertFalse(run_with([])["save_prompt"])

    def test_window_that_disappeared(self):
        self.assertFalse(run_with([FakeChild("保存"), FakeChild("不保存")], exists=False)["save_prompt"])

    def test_text_controls_do_not_count(self):
        result = run_with([FakeChild("保存", "Text"), FakeChild("不保存", "Text")])
        self.assertFalse(result["save_prompt"])


if __name__ == "__main__":
    unittest.main()
