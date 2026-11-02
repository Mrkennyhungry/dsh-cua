"""Keyboard actions must refuse when a sibling window of the same process is in front.

Reproduced 2026-11-02 on Windows 11 Notepad: two top-level windows, one process.
The bound window was behind its sibling, is_effectively_foreground() answered
"foreground", the paste landed in the sibling, and desktop_type reported success.
"""

import inspect
import unittest
from contextlib import ExitStack
from unittest import mock

from dsh_desktop import desktop_tools
from dsh_desktop.game import window as window_module

TARGET = 100
SIBLING = 200
OTHER_APP = 300
CHILD = 400
POPUP = 500


class PatchedForeground:
    """Context manager: fake the Win32 foreground facts for both checks."""

    def __init__(self, fg, ancestors=None, owner_links=False):
        self.fg = fg
        self.ancestors = ancestors or {}
        self.owner_links = owner_links
        self.stack = ExitStack()

    def __enter__(self):
        self.stack.enter_context(
            mock.patch.object(window_module.win32gui, "GetForegroundWindow", return_value=self.fg))
        self.stack.enter_context(
            mock.patch.object(window_module.win32gui, "GetAncestor",
                              side_effect=lambda hwnd, flag: self.ancestors.get(hwnd, hwnd)))
        self.stack.enter_context(mock.patch.object(window_module, "_has_owner_link",
                                                   return_value=self.owner_links))
        self.stack.enter_context(
            mock.patch.object(desktop_tools, "_is_effectively_foreground",
                              side_effect=lambda hwnd: self.fg in (hwnd, SIBLING)))
        self.stack.enter_context(
            mock.patch.object(desktop_tools, "_window_info", return_value={"exe": "Notepad.exe"}))
        # Two windows of one application share a PID; another app does not.
        pids = {TARGET: 111, SIBLING: 111, CHILD: 111, POPUP: 111, OTHER_APP: 222}
        self.stack.enter_context(
            mock.patch.object(desktop_tools.win32process, "GetWindowThreadProcessId",
                              side_effect=lambda h, *a: (1, pids.get(h, 999))))
        return self

    def __exit__(self, *exc):
        self.stack.close()
        return False


class IsForegroundTargetTests(unittest.TestCase):
    def test_exact_window_is_accepted(self):
        with PatchedForeground(fg=TARGET):
            self.assertTrue(window_module.is_foreground_target(TARGET))

    def test_child_window_of_target_is_accepted(self):
        with PatchedForeground(fg=CHILD, ancestors={CHILD: TARGET}):
            self.assertTrue(window_module.is_foreground_target(TARGET))

    def test_owned_popup_is_accepted(self):
        with PatchedForeground(fg=POPUP, owner_links=True):
            self.assertTrue(window_module.is_foreground_target(TARGET))

    def test_sibling_window_of_same_process_is_refused(self):
        with PatchedForeground(fg=SIBLING):
            self.assertTrue(window_module.is_effectively_foreground(TARGET),
                            "the loose rule is expected to keep accepting this")
            self.assertFalse(window_module.is_foreground_target(TARGET))

    def test_other_application_is_refused(self):
        with PatchedForeground(fg=OTHER_APP):
            self.assertFalse(window_module.is_foreground_target(TARGET))

    def test_no_foreground_window_is_refused(self):
        with PatchedForeground(fg=0):
            self.assertFalse(window_module.is_foreground_target(TARGET))


class RequireForegroundTests(unittest.TestCase):
    def test_strict_refuses_a_sibling_window(self):
        with PatchedForeground(fg=SIBLING):
            blocked = desktop_tools._require_foreground(TARGET, strict=True)
        self.assertIsNotNone(blocked)
        self.assertFalse(blocked["success"])
        self.assertIn("不是前台窗口", blocked["error"])
        self.assertIn("同一个程序的另一个窗口", blocked["error"])

    def test_strict_accepts_the_target_itself(self):
        with PatchedForeground(fg=TARGET):
            self.assertIsNone(desktop_tools._require_foreground(TARGET, strict=True))

    def test_loose_check_stays_permissive_for_activation_and_capture(self):
        with PatchedForeground(fg=SIBLING):
            self.assertIsNone(desktop_tools._require_foreground(TARGET))

    def test_other_application_reports_the_foreground_window(self):
        with PatchedForeground(fg=OTHER_APP):
            blocked = desktop_tools._require_foreground(TARGET, strict=True)
        self.assertEqual(blocked["foreground_hwnd"], OTHER_APP)
        self.assertFalse(blocked["foreground_is_same_process"])


class KeyboardCallSitesTests(unittest.TestCase):
    """The gate only helps if every keyboard action asks for the strict mode."""

    KEYBOARD = ("desktop_type_text", "desktop_press_key", "desktop_type_keys")

    def test_named_keyboard_tools_use_the_strict_gate(self):
        for name in self.KEYBOARD:
            source = inspect.getsource(getattr(desktop_tools, name))
            self.assertIn("_require_foreground(hwnd, strict=True)", source, name)

    def test_every_strict_call_site_is_a_keyboard_action(self):
        source = inspect.getsource(desktop_tools)
        self.assertEqual(source.count("_require_foreground(hwnd, strict=True)"), 5,
                         "paste text, press key, type keys, paste file and the UIA "
                         "fallback are the keyboard call sites")

    def test_activation_ladder_keeps_the_loose_check(self):
        source = inspect.getsource(desktop_tools)
        self.assertGreaterEqual(source.count("if _require_foreground(hwnd) is None:"), 1,
                                "activation escalation must stay permissive")


if __name__ == "__main__":
    unittest.main()
