import unittest

from dsh_desktop.desktop_tools import _uia_target_matches


class UiaTargetMatchTests(unittest.TestCase):
    def test_short_chinese_and_numeric_targets(self):
        nodes = [{"name": "等于", "auto_id": "equalButton"},
                 {"name": "显示为 46", "auto_id": "CalculatorResults"}]
        self.assertTrue(_uia_target_matches("等于", nodes))
        self.assertTrue(_uia_target_matches("46", nodes))
        self.assertFalse(_uia_target_matches("47", nodes))

    def test_single_character_requires_exact_field(self):
        nodes = [{"name": "一", "auto_id": "num1Button"},
                 {"name": "记忆加法", "auto_id": "MemPlus"}]
        self.assertTrue(_uia_target_matches("一", nodes))
        self.assertFalse(_uia_target_matches("加", nodes))
        self.assertFalse(_uia_target_matches("", nodes))
