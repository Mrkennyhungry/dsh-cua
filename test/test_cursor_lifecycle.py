import os,sys,time
from pathlib import Path
from unittest.mock import patch
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'kernel'))
from dsh_desktop.cursor_overlay import CursorOverlay
class CommanderLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.c=CursorOverlay(); self.c._layout=lambda:((400,20),(100,200))
    def test_idle_actions_cannot_create_a_residual_character(self):
        self.c.action('click',900,900); self.c.move(800,800)
        self.assertFalse(self.c.visible)
        self.assertIsNone(self.c._target)
    def test_commander_docks_and_never_follows_action_coordinates(self):
        self.c.begin_task('one'); target=self.c._target
        self.c._born-=3
        self.c.action('click',900,900)
        self.c.move(800,800)
        self.assertEqual(self.c._target,target)
        self.assertEqual(self.c._resolved_action(time.monotonic()),'click')
        self.c.end_task('one'); self.c.action('click')
        self.assertFalse(self.c.visible)
    def test_entrance_then_thinking(self):
        self.c.begin_task('one')
        self.assertEqual(self.c._resolved_action(self.c._born+.5),'enter')
        self.assertEqual(self.c._resolved_action(self.c._born+3),'thinking')
    def test_real_actions_are_not_masked_by_the_entrance(self):
        self.c.begin_task('one'); self.c.action('right_click')
        self.assertEqual(self.c._resolved_action(time.monotonic()),'right_click')
    def test_multiple_owners_and_completion(self):
        self.c.begin_task('one'); self.c.begin_task('two')
        self.c.end_task('one'); self.assertTrue(self.c.visible)
        self.c.end_task('two'); self.assertFalse(self.c.visible)
    def test_lease_expiration_prevents_residue(self):
        self.c.begin_task('one',lease_seconds=1)
        self.c._expire_tasks(time.monotonic()+2)
        self.assertFalse(self.c.visible); self.assertFalse(self.c._tasks)
    def test_process_exit_prevents_residue(self):
        self.c.begin_task('one',client_pid=os.getpid())
        with patch('psutil.Process',side_effect=__import__('psutil').NoSuchProcess(123)):
            self.c._expire_tasks(time.monotonic()+1)
        self.assertFalse(self.c.visible)
    def test_task_disables_companion_clicks_before_the_next_frame(self):
        from unittest.mock import Mock
        from dsh_desktop.cursor_overlay import GWL_EXSTYLE, WS_EX_TRANSPARENT
        self.c._primary_dock=lambda:(400,20)
        self.c.set_companion(True,lambda:None)
        self.c._clickable_now=True
        self.c._hwnd=123
        user32=Mock()
        user32.GetWindowLongW.return_value=0
        self.c._user32=user32
        self.c.begin_task('one')
        self.assertFalse(self.c._clickable_now)
        self.assertFalse(self.c.companion_interactive())
        user32.SetWindowLongW.assert_called_once_with(123,GWL_EXSTYLE,WS_EX_TRANSPARENT)
    def test_capture_guard_restores_after_failure(self):
        with patch('dsh_desktop.cursor_overlay.time.sleep'):
            with self.assertRaises(RuntimeError):
                with self.c.capture_guard(): raise RuntimeError('capture')
        self.assertEqual(self.c._capture_depth,0)
if __name__=='__main__':unittest.main()
