"""web_click must never click a substitute button.

Two behaviours are pinned here:
  1. A missing target returns diagnostics instead of pressing a lookalike
     confirm button (保存/确定/提交 are not interchangeable).
  2. A failure that happens after the click was dispatched reports
     RESULT_UNKNOWN and does not click anything a second time.
"""

import unittest
from unittest import mock

from dsh_desktop import web_tools


class _Match:
    """Stands in for a Playwright Locator: locator.first is itself a Locator."""

    def __init__(self, label="详情", click_error=None, visible=True, wait_error=None):
        self.label = label
        self.click_error = click_error
        self.clicks = 0
        self.visible = visible
        self.wait_error = wait_error

    def is_visible(self):
        return self.visible

    def inner_text(self, timeout=None):
        return self.label

    def evaluate(self, _js):
        return False

    def wait_for(self, **_kwargs):
        if self.wait_error:
            raise self.wait_error

    def click(self, timeout=None):
        self.clicks += 1
        if self.click_error:
            raise self.click_error


class _Locator:
    def __init__(self, match):
        self._match = match

    @property
    def first(self):
        return self._match

    def all(self):
        return [self._match]

    def count(self):
        return 1


class _Page:
    def __init__(self, match):
        self.frames = []
        self._locator = _Locator(match)

    def locator(self, _selector):
        return self._locator

    def wait_for_load_state(self, *_args, **_kwargs):
        return None


def _run(match):
    page = _Page(match)
    patches = [
        mock.patch.object(web_tools, "_pick_page", lambda *a, **k: page),
        mock.patch.object(web_tools, "_stale_nav_warning", lambda *a, **k: None),
        mock.patch.object(web_tools, "_page_evidence", lambda *a, **k: None),
        mock.patch.object(web_tools, "_note_page_state", lambda *a, **k: None),
        mock.patch.object(web_tools, "_diagnose", lambda *a, **k: ""),
        mock.patch.object(web_tools, "assert_unique_target", lambda *a, **k: None),
        mock.patch.object(web_tools, "_lease_guard", lambda *a, **k: None),
        mock.patch.object(web_tools, "_is_destructive", lambda *a, **k: ""),
    ]
    for patcher in patches:
        patcher.start()
    try:
        return web_tools.web_click('text="详情"')
    finally:
        for patcher in reversed(patches):
            patcher.stop()


class WebClickNoFallbackTests(unittest.TestCase):
    def test_confirm_equivalence_helper_is_gone(self):
        self.assertFalse(hasattr(web_tools, "_equivalent_confirm_click"))

    def test_missing_target_reports_without_clicking(self):
        match = _Match(wait_error=TimeoutError("locator.wait_for: timeout"))
        result = _run(match)
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "ELEMENT_NOT_FOUND")
        self.assertEqual(match.clicks, 0)
        self.assertNotIn("equivalent_click", result)

    def test_failure_after_dispatch_is_result_unknown_and_not_retried(self):
        match = _Match(click_error=RuntimeError("Target closed"))
        result = _run(match)
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "RESULT_UNKNOWN")
        self.assertEqual(match.clicks, 1)  # no second click anywhere

    def test_successful_click_reports_once(self):
        match = _Match()
        result = _run(match)
        self.assertTrue(result["success"])
        self.assertEqual(match.clicks, 1)


if __name__ == "__main__":
    unittest.main()
