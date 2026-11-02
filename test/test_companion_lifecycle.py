import os
import unittest
from unittest import mock

from dsh_desktop.companion import Companion


class CompanionLifecycleTests(unittest.TestCase):
    def test_shutdown_detaches_dsh_ui_and_allows_restart(self):
        companion = Companion()
        overlay = mock.Mock()
        hotkeys = mock.Mock()
        mouse = mock.Mock()
        process = mock.Mock()
        process.poll.return_value = None
        companion._overlay = overlay
        companion._hotkeys = hotkeys
        companion._mouse_listener = mouse
        companion._ui_process = process
        companion.watch_client(os.getpid())
        self.assertEqual(companion._client_pid, os.getpid())

        with mock.patch('dsh_desktop.desktop_tools.unlock_window') as unlock:
            companion.shutdown()

        overlay.set_companion.assert_called_once_with(False)
        overlay.hide.assert_called_once_with()
        hotkeys.stop.assert_called_once_with()
        mouse.stop.assert_called_once_with()
        process.terminate.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=2)
        unlock.assert_called_once_with()
        self.assertIsNone(companion._overlay)
        self.assertIsNone(companion._eternity)
        self.assertIsNone(companion._ui_process)
        self.assertIsNone(companion._client_pid)

        restarted_overlay = mock.Mock()
        with mock.patch('dsh_desktop.cursor_overlay.get_cursor_overlay', return_value=restarted_overlay), \
             mock.patch.object(companion, '_start_hotkeys'), \
             mock.patch.object(companion, '_start_outside_click_listener'), \
             mock.patch.object(companion, '_start_ui_server'), \
             mock.patch.object(companion, '_prewarm'):
            companion.install(object())
        restarted_overlay.set_companion.assert_called_once_with(True, companion.open_menu)


if __name__ == '__main__':
    unittest.main()
