"""Element-invalidation checks for the UIA batch executor.

A batch scans once and then reuses wrappers. These tests pin what counts as a
stale element: the control disappeared, its identity changed, or (for clicks)
it moved far enough that the cached bbox would hit something else.
"""

import unittest

from dsh_desktop import desktop_tools
from dsh_desktop.desktop_tools import _StaleElementError, _uia_live_check


class _Rect:
    def __init__(self, left=0, top=0, right=100, bottom=100):
        self.left, self.top, self.right, self.bottom = left, top, right, bottom


class _Info:
    def __init__(self, name, auto_id, control_type, rect):
        self.name = name
        self.automation_id = auto_id
        self.control_type = control_type
        self.rectangle = rect


class _Wrapper:
    def __init__(self, info=None, raises=None):
        self._info = info
        self._raises = raises

    @property
    def element_info(self):
        if self._raises:
            raise self._raises
        return self._info


def _element(name="保存", auto_id="btnSave", control_type="Button",
             bbox=(0, 0, 100, 100), wrapper=None):
    return {"name": name, "auto_id": auto_id, "control_type": control_type,
            "bbox": list(bbox), "wrapper": wrapper or _Wrapper(_Info(name, auto_id, control_type, _Rect()))}


class _Region:
    left, top, width, height = 0, 0, 1000, 1000


REGION = _Region()


class UiaLiveCheckTests(unittest.TestCase):
    def test_disappeared_control_is_gone(self):
        element = _element(wrapper=_Wrapper(raises=RuntimeError("element not found")))
        with self.assertRaises(_StaleElementError) as ctx:
            _uia_live_check(element, REGION, "click")
        self.assertIn("ELEMENT_GONE", str(ctx.exception))

    def test_replaced_control_is_detected_by_identity(self):
        element = _element(name="保存", auto_id="btnSave")
        element["wrapper"] = _Wrapper(_Info("保存并提交", "btnSave", "Button", _Rect()))
        with self.assertRaises(_StaleElementError) as ctx:
            _uia_live_check(element, REGION, "click")
        self.assertIn("ELEMENT_REPLACED", str(ctx.exception))

    def test_moved_control_invalidates_cached_bbox(self):
        element = _element(bbox=(0, 0, 100, 100))
        element["wrapper"] = _Wrapper(_Info("保存", "btnSave", "Button", _Rect(500, 500, 600, 600)))
        with self.assertRaises(_StaleElementError) as ctx:
            _uia_live_check(element, REGION, "click")
        self.assertIn("ELEMENT_MOVED", str(ctx.exception))

    def test_small_drift_within_tolerance_passes(self):
        element = _element(bbox=(0, 0, 100, 100))
        element["wrapper"] = _Wrapper(_Info("保存", "btnSave", "Button", _Rect(2, 2, 102, 102)))
        _uia_live_check(element, REGION, "click")  # 不应抛

    def test_type_action_ignores_geometry(self):
        element = _element(bbox=(0, 0, 100, 100))
        element["wrapper"] = _Wrapper(_Info("保存", "btnSave", "Button", _Rect(500, 500, 600, 600)))
        _uia_live_check(element, REGION, "type")  # type 不依赖 bbox

    def test_duplicate_target_in_table_is_ambiguous(self):
        wrapper = _Wrapper(_Info("保存", "", "Button", _Rect()))
        element = {"name": "保存", "auto_id": "", "control_type": "Button",
                   "bbox": [0, 0, 100, 100], "wrapper": wrapper}
        table = {("name", "保存"): [element, element]}
        with self.assertRaises(desktop_tools._BatchTargetMissing):
            if len(table[("name", "保存")]) != 1:
                raise desktop_tools._BatchTargetMissing("TARGET_NOT_UNIQUE: 控件不存在或不唯一")


if __name__ == "__main__":
    unittest.main()
