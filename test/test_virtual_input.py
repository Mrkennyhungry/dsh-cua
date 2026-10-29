"""Regression: physical input is mandatory; no silent virtual mode or restore."""
import os
import unittest
from unittest.mock import Mock, patch
from dsh_desktop.game.input import InputService, Button

class PhysicalInputPolicyTests(unittest.TestCase):
    def setUp(self):
        self.service=InputService.__new__(InputService)
        self.service.mouse=Mock()
        self.service._human_move_to=Mock()
        self.service._feedback=Mock()
        self.service._verify_pointer_target=Mock(return_value=None)
        self.pause=patch('dsh_desktop.game.input.time.sleep')
        self.pause.start(); self.addCleanup(self.pause.stop)
    def test_legacy_environment_cannot_reenable_virtual_input(self):
        for value in ('virtual','physical',''):
            with patch.dict(os.environ,{'DSH_DESKTOP_INPUT_MODE':value}):
                self.assertFalse(self.service.virtual)
    def test_click_moves_physical_pointer_and_does_not_restore(self):
        self.service.click_screen(100,200)
        self.service._human_move_to.assert_called_once_with(100,200)
        self.service.mouse.press.assert_called_once_with(Button.left)
        self.service.mouse.release.assert_called_once_with(Button.left)
        self.assertNotIn('position', self.service.mouse.__dict__)
    def test_right_click_is_a_physical_event_at_target(self):
        self.service.right_click_screen(80,90)
        self.service._human_move_to.assert_called_once_with(80,90)
        self.service.mouse.press.assert_called_once_with(Button.right)
    def test_failed_pointer_movement_never_clicks_wrong_location(self):
        self.service._verify_pointer_target.return_value={'success':False,'error':'move failed'}
        result=self.service.click_screen(100,200)
        self.assertFalse(result['success'])
        self.service.mouse.press.assert_not_called()
    @patch('dsh_desktop.game.input.ctypes.windll.user32.mouse_event')
    def test_wheel_uses_detents_not_double_scaled_delta(self,scroll):
        self.service.scroll(80,90,-3)
        self.service._human_move_to.assert_called_once_with(80,90)
        self.assertEqual(scroll.call_count,3)
        self.assertTrue(all(call.args[0]==0x0800 and call.args[3]==(2**32-120) for call in scroll.call_args_list))
if __name__=='__main__':unittest.main()
