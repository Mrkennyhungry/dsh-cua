"""Blocker detection must not fire on document text.

Scintilla exposes the editor buffer as its UIA element name, so a document line
containing "...CREATE-OK" used to be reported as a blocking dialog button.
"""

import unittest

from dsh_desktop.dsh_bridge import _is_blocker_label

WORDS = ("确定", "取消", "关闭", "接受", "拒绝", "OK", "Cancel", "Close")


class BlockerLabelTests(unittest.TestCase):
    def test_document_text_with_embedded_keyword_is_not_a_blocker(self):
        self.assertFalse(_is_blocker_label("DSH-CUA-CREATE-OK", WORDS))
        self.assertFalse(_is_blocker_label("DSH-DORAEMON 0.4.2 field note: OK", WORDS))
        self.assertFalse(_is_blocker_label("please press OK to continue with the migration", WORDS))

    def test_short_dialog_actions_are_blockers(self):
        self.assertTrue(_is_blocker_label("确定", WORDS))
        self.assertTrue(_is_blocker_label("关闭", WORDS))
        self.assertTrue(_is_blocker_label("OK", WORDS))
        self.assertTrue(_is_blocker_label("OK!", WORDS))

    def test_short_suffix_form_is_accepted(self):
        self.assertTrue(_is_blocker_label("确定并继续", WORDS))
        self.assertTrue(_is_blocker_label("关闭窗口", WORDS))

    def test_empty_and_long_labels_are_ignored(self):
        self.assertFalse(_is_blocker_label("", WORDS))
        self.assertFalse(_is_blocker_label("   ", WORDS))


if __name__ == "__main__":
    unittest.main()
