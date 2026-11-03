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

    def test_dismiss_hides_without_stopping_restore_hotkey(self):
        companion = Companion()
        overlay = mock.Mock()
        overlay.visible = False  # task flag may be false while companion is shown
        overlay._companion = True
        companion._overlay = overlay
        hotkeys = mock.Mock()
        companion._hotkeys = hotkeys
        with mock.patch('win32gui.EnumWindows'):
            companion.dismiss()
        overlay.set_companion.assert_called_once_with(False)
        overlay.hide.assert_called_once_with()
        hotkeys.stop.assert_not_called()
        overlay.visible = False
        overlay._companion = False
        companion.toggle_mascot()
        overlay.set_companion.assert_called_with(True, companion.open_menu)

    def test_open_menu_is_refused_while_a_task_is_running(self):
        companion = Companion()
        overlay = mock.Mock()
        overlay.companion_interactive.return_value = False
        companion._overlay = overlay
        companion._menu = mock.Mock()
        companion.open_menu()
        companion._menu.assert_not_called()


if __name__ == '__main__':
    unittest.main()
