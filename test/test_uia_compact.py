"""Payload-shape checks for `desktop_uia_tree(compact=True)` and its filters.

The complaint these pin: one UIA control used to cost ~132 bytes of JSON, most of it
derivable (`index`, `name_length`, the `bbox` center) or always empty (`auto_id: ""`,
`name_truncated: false`). A 236-control Explorer window was ~31 KB the model had to
read before it could click anything.

The boundary matters as much as the size: `bbox` must survive on the internal tree
because `desktop_element_table` (and therefore `goal=` positioning) indexes it.
Compacting in place emptied that table once, silently.
"""

import json
import unittest
from unittest import mock

from dsh_desktop import desktop_tools
from dsh_desktop.desktop_tools import (
    _uia_compact_control,
    _uia_filter_controls,
)


def node(**overrides):
    base = {
        "index": 7,
        "control_type": "Button",
        "name": "保存",
        "name_truncated": False,
        "name_length": 2,
        "auto_id": "",
        "bbox": [640, 300, 760, 340],
    }
    base.update(overrides)
    return base


class CompactControlTests(unittest.TestCase):
    def test_keeps_only_actionable_fields(self):
        out = _uia_compact_control(node())
        self.assertEqual(out, {"name": "保存", "type": "Button", "x": 700, "y": 320})
        for dropped in ("index", "name_length", "bbox", "auto_id", "name_truncated"):
            self.assertNotIn(dropped, out)

    def test_click_point_is_bbox_center(self):
        out = _uia_compact_control(node(bbox=[0, 0, 1000, 500]))
        self.assertEqual((out["x"], out["y"]), (500, 250))

    def test_value_and_auto_id_survive_when_present(self):
        out = _uia_compact_control(
            node(control_type="Edit", name="地址栏", auto_id="TextBox", value="C:\\Windows"))
        self.assertEqual(out["value"], "C:\\Windows")
        self.assertEqual(out["auto_id"], "TextBox")

    def test_flags_only_when_true(self):
        plain = _uia_compact_control(node())
        self.assertNotIn("name_truncated", plain)
        cut = _uia_compact_control(node(name_truncated=True, value_truncated=True))
        self.assertTrue(cut["name_truncated"])
        self.assertTrue(cut["value_truncated"])

    def test_empty_name_is_omitted(self):
        out = _uia_compact_control(node(name="", control_type="Pane"))
        self.assertNotIn("name", out)
        self.assertEqual(out["type"], "Pane")


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.controls = [
            node(name="保存", control_type="Button", bbox=[0, 0, 100, 100]),
            node(name="地址栏", control_type="Edit", bbox=[100, 0, 200, 100], value="C:\\"),
            node(name="", control_type="Pane", bbox=[0, 0, 1000, 1000]),
            node(name="", control_type="Image", bbox=[0, 0, 10, 10]),
            node(name="名称", control_type="Edit", bbox=[0, 100, 100, 200], value="文件.txt"),
        ]

    def test_control_types_is_case_insensitive(self):
        result = _uia_filter_controls(self.controls, control_types="button, EDIT")
        self.assertEqual([n["control_type"] for n in result["controls"]], ["Button", "Edit", "Edit"])

    def test_text_contains_searches_name_and_value(self):
        self.assertEqual(len(_uia_filter_controls(self.controls, text_contains="文件")["controls"]), 1)
        self.assertEqual(len(_uia_filter_controls(self.controls, text_contains="c:\\")["controls"]), 1)

    def test_only_actionable_drops_containers_and_decoration(self):
        result = _uia_filter_controls(self.controls, only_actionable=True)
        self.assertEqual([n["control_type"] for n in result["controls"]],
                         ["Button", "Edit", "Edit"])

    def test_limit_pages_after_matching_and_reports_both_counts(self):
        result = _uia_filter_controls(self.controls, control_types="Edit", limit=1)
        self.assertEqual(len(result["controls"]), 1)
        self.assertEqual(result["filter"]["matched"], 2)
        self.assertEqual(result["filter"]["scanned"], 5)

    def test_no_filter_keeps_everything(self):
        result = _uia_filter_controls(self.controls)
        self.assertEqual(len(result["controls"]), 5)
        self.assertEqual(result["filter"]["control_types"], [])


FAKE_TREE = {
    "success": True,
    "window_id": "win-test",
    "title": "System32 - 文件资源管理器",
    "truncated": False,
    "truncation_reasons": [],
    "max_depth": 7,
    "max_nodes": 120,
    "uia_coverage": {"control_count": 3, "named_control_count": 2,
                     "named_semantic_control_count": 2, "message_like_candidate_count": 0},
    "uia_capability": "rich",
    "controls": [
        node(name="保存", control_type="Button", bbox=[0, 0, 100, 100]),
        node(name="", control_type="Pane", bbox=[0, 0, 1000, 1000]),
        node(name="地址栏", control_type="Edit", bbox=[100, 100, 300, 140], value="C:\\"),
    ],
}


class CompactBoundaryTests(unittest.TestCase):
    """The public payload shrinks; the tree the element table reads does not."""

    def call(self, **kwargs):
        info = {"hwnd": 1234, "title": "System32 - 文件资源管理器", "exe": "explorer.exe", "class": "CabinetWClass"}
        with mock.patch.dict(desktop_tools._STATE._windows, {"win-test": info}, clear=False), \
             mock.patch.object(desktop_tools, "_desktop_uia_tree_once", return_value=dict(FAKE_TREE)), \
             mock.patch.object(desktop_tools, "_is_effectively_foreground", return_value=True):
            return desktop_tools.desktop_uia_tree("win-test", **kwargs)

    def test_compact_public_payload_has_no_bbox(self):
        result = self.call(compact=True)
        self.assertTrue(result["success"])
        self.assertTrue(all("bbox" not in control for control in result["controls"]))
        self.assertTrue(all("x" in control and "y" in control for control in result["controls"]))

    def test_internal_tree_keeps_bbox_for_the_element_table(self):
        self.call(compact=True)
        self.assertEqual(FAKE_TREE["controls"][0]["bbox"], [0, 0, 100, 100])
        self.assertEqual(FAKE_TREE["controls"][0]["control_type"], "Button")

    def test_order_of_magnitude_is_reported(self):
        result = self.call(compact=True)
        self.assertLess(result["payload_bytes"], result["payload_bytes_full"])
        # Three controls are too few to show the full ratio, but the shape is right:
        # the compact form must be smaller than the full one for every real tree.
        compacted = json.dumps(result["controls"], ensure_ascii=False, separators=(",", ":"))
        self.assertEqual(result["payload_bytes"], len(compacted))

    def test_full_mode_keeps_the_old_shape(self):
        result = self.call(compact=False)
        self.assertEqual(result["controls"][0]["bbox"], [0, 0, 100, 100])
        self.assertIn("index", result["controls"][0])
        self.assertNotIn("payload_bytes", result)

    def test_filter_applies_to_the_public_payload_only(self):
        result = self.call(compact=True, control_types="Button")
        self.assertEqual([n["type"] for n in result["controls"]], ["Button"])
        self.assertEqual(result["uia_filter"]["matched"], 1)
        self.assertEqual(result["uia_filter"]["scanned"], 3)
        self.assertEqual(len(FAKE_TREE["controls"]), 3)

    def test_filter_that_matches_nothing_returns_an_empty_list_not_an_error(self):
        result = self.call(compact=True, text_contains="没有这个控件")
        self.assertTrue(result["success"])
        self.assertEqual(result["controls"], [])


class CoordinateContractTests(unittest.TestCase):
    """The three-sizes problem, pinned as a field instead of a prompt sentence."""

    def test_contract_names_the_unit_and_the_window(self):
        contract = desktop_tools._coordinate_contract("window", 2148, 1255)
        self.assertEqual(contract["click_unit"], "normalized_0_1000")
        self.assertEqual(contract["space"], "window")
        self.assertEqual(contract["window_px"], {"width": 2148, "height": 1255})
        self.assertNotIn("observation_px", contract)

    def test_contract_carries_the_observation_scale_when_known(self):
        contract = desktop_tools._coordinate_contract("window", 2148, 1255, 1366, 798)
        self.assertEqual(contract["observation_px"], {"width": 1366, "height": 798})
        self.assertEqual(contract["from_observation_px"]["y"], "y_px / 798 * 1000")

    def test_desktop_scope_reports_screen_space(self):
        self.assertEqual(desktop_tools._coordinate_contract("desktop", 100, 100)["space"], "screen")


class ObservationCompactionTests(unittest.TestCase):
    """analyze elements shrink at the HTTP edge, not inside desktop_analyze."""

    def setUp(self):
        self.raw = {
            "success": True,
            "ui_elements": [
                {"text": "保存", "bbox": [0, 0, 100, 100], "type": "text", "source": "local_ocr",
                 "id": 0, "click": {"space": "window", "x": 50, "y": 50}},
                {"text": "关闭", "bbox": [900, 0, 1000, 40], "type": "icon", "source": "local_yolo",
                 "id": 1, "caption": "关闭", "click": {"space": "window", "x": 950, "y": 20}},
            ],
        }

    def test_drops_duplicate_geometry_and_keeps_a_click_point(self):
        from dsh_desktop.dsh_bridge import _compact_observation

        out = _compact_observation(self.raw)
        first = out["ui_elements"][0]
        self.assertEqual((first["x"], first["y"]), (50, 50))
        self.assertNotIn("bbox", first)
        self.assertNotIn("id", first)
        self.assertNotIn("click", first)
        self.assertEqual(out["ui_elements"][1]["caption"], "关闭")

    def test_original_input_is_not_mutated(self):
        from dsh_desktop.dsh_bridge import _compact_observation

        _compact_observation(self.raw)
        self.assertIn("bbox", self.raw["ui_elements"][0])

    def test_falls_back_to_the_bbox_center_when_click_is_absent(self):
        from dsh_desktop.dsh_bridge import _compact_observation

        raw = {"ui_elements": [{"text": "x", "bbox": [10, 20, 30, 60]}]}
        element = _compact_observation(raw)["ui_elements"][0]
        self.assertEqual((element["x"], element["y"]), (20, 40))

    def test_reports_both_sizes(self):
        from dsh_desktop.dsh_bridge import _compact_observation

        out = _compact_observation(self.raw)
        self.assertLess(out["payload_bytes"], out["payload_bytes_full"])


if __name__ == "__main__":
    unittest.main()
