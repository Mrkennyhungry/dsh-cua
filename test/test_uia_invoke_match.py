"""desktop_uia_invoke must execute an exact, unique match only.

The old code took the first fuzzy hit, so asking for 保存 could press
保存并提交. These tests pin: exact identity, ambiguity reported (never the
first candidate), automation_id as the disambiguator.
"""

import unittest
from unittest import mock

from dsh_desktop import desktop_tools


class _Rect:
    left, top, right, bottom = 0, 0, 20, 10


class _Info:
    def __init__(self, name, auto_id, control_type):
        self.name = name
        self.automation_id = auto_id
        self.control_type = control_type
        self.rectangle = _Rect()


class _Pattern:
    def __init__(self, sink):
        self._sink = sink

    def Invoke(self):  # noqa: N802 - mirrors the UIA pattern name
        self._sink.append("invoke")


class _Control:
    def __init__(self, name, auto_id="", control_type="Button"):
        self._name = name
        self.element_info = _Info(name, auto_id, control_type)
        self.pressed = []
        self.iface_invoke = _Pattern(self.pressed)

    def window_text(self):
        return self._name


class _Wrapper:
    def __init__(self, controls):
        self._controls = controls

    def descendants(self):
        return list(self._controls)


class _Window:
    def __init__(self, controls):
        self._controls = controls

    def wrapper_object(self):
        return _Wrapper(self._controls)


class _Desktop:
    def __init__(self, controls):
        self._controls = controls

    def __call__(self, *args, **kwargs):
        return self

    def window(self, handle=None):
        return _Window(self._controls)


def _patch_uia(controls):
    return mock.patch("pywinauto.Desktop", _Desktop(controls))


class UiaInvokeMatchTests(unittest.TestCase):
    def setUp(self):
        self._saved = dict(desktop_tools._STATE._windows)
        desktop_tools._STATE._windows["w1"] = {"hwnd": 123}
        self.addCleanup(setattr, desktop_tools._STATE, "_windows", self._saved)
        patcher = mock.patch.object(desktop_tools, "_controller", lambda: object())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_duplicate_name_is_ambiguous_and_nothing_is_pressed(self):
        a = _Control("保存", "btnSaveA")
        b = _Control("保存", "btnSaveB")
        with _patch_uia([a, b]):
            result = desktop_tools.desktop_uia_invoke("w1", "保存")
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "UIA_TARGET_AMBIGUOUS")
        self.assertEqual(len(result["candidates"]), 2)
        self.assertEqual(a.pressed + b.pressed, [])

    def test_save_does_not_resolve_to_save_and_submit(self):
        only = _Control("保存并提交", "btnSaveSubmit")
        with _patch_uia([only]):
            result = desktop_tools.desktop_uia_invoke("w1", "保存")
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "UIA_TARGET_NOT_FOUND")
        self.assertEqual(result["candidates"][0]["name"], "保存并提交")
        self.assertEqual(only.pressed, [])
        self.assertIn("未被点击", result["hint"])

    def test_automation_id_disambiguates_and_reports_real_identity(self):
        a = _Control("保存", "btnSaveA")
        b = _Control("保存", "btnSaveB")
        with _patch_uia([a, b]):
            result = desktop_tools.desktop_uia_invoke("w1", automation_id="btnSaveB")
        self.assertTrue(result["success"])
        self.assertEqual(result["control"]["automation_id"], "btnSaveB")
        self.assertEqual(b.pressed, ["invoke"])
        self.assertEqual(a.pressed, [])

    def test_exact_name_beats_duplicate_fuzzy_noise(self):
        noisy = _Control("保存草稿", "btnDraft")
        exact = _Control("保存", "btnSave")
        with _patch_uia([noisy, exact]):
            result = desktop_tools.desktop_uia_invoke("w1", "保存")
        self.assertTrue(result["success"])
        self.assertEqual(result["control"]["automation_id"], "btnSave")
        self.assertEqual(noisy.pressed, [])


if __name__ == "__main__":
    unittest.main()
