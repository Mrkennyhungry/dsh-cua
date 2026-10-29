import unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch
from dsh_desktop.controller import AppController
from dsh_desktop.game.input import InputService
class PhysicalPointerReportingTest(unittest.TestCase):
    def test_observation_tracks_real_mouse_not_commander(self):
        c=AppController.__new__(AppController); c.input=InputService.__new__(InputService); c.window=Mock()
        with patch('win32gui.GetCursorPos',return_value=(300,250)), patch('win32gui.ClientToScreen',return_value=(100,50)), patch('win32gui.GetClientRect',return_value=(0,0,400,400)):
            result=c._get_mouse_position(123)
        self.assertEqual(result['source'],'physical')
        self.assertEqual(result['normalized'],[500,500])
if __name__=='__main__':unittest.main()
