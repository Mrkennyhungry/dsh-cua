import os
import subprocess
import sys
import time
import unittest
import uuid
import psutil

import win32api
import win32con
import win32gui
import win32process


def child_window(unique):
    title = 'DSH temporary unsaved editor ' + unique
    prompt = 'DSH temporary save prompt ' + unique
    message = win32con.WM_APP + 17

    def wndproc(hwnd, msg, wparam, lparam):
        if msg == win32con.WM_CLOSE:
            win32gui.MessageBox(hwnd, 'This temporary test document has unsaved changes.', prompt,
                                win32con.MB_OKCANCEL | win32con.MB_ICONQUESTION)
            return 0
        if msg == message:
            win32gui.DestroyWindow(hwnd)
            return 0
        if msg == win32con.WM_DESTROY:
            win32gui.PostQuitMessage(0)
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    wc = win32gui.WNDCLASS()
    wc.hInstance = win32api.GetModuleHandle(None)
    wc.lpszClassName = 'DshTemporaryUnsavedEditor' + unique
    wc.lpfnWndProc = wndproc
    klass = win32gui.RegisterClass(wc)
    hwnd = win32gui.CreateWindow(klass, title, win32con.WS_OVERLAPPEDWINDOW,
                                 100, 100, 480, 180, 0, 0, wc.hInstance, None)
    win32gui.ShowWindow(hwnd, win32con.SW_SHOWNORMAL)
    win32gui.UpdateWindow(hwnd)
    win32gui.PumpMessages()


@unittest.skipUnless(os.environ.get('DSH_LIVE_DIALOG_TEST') == '1', 'explicit interactive test only')
class CleanupDialogLiveTest(unittest.TestCase):
    def test_unsaved_dialog_is_preserved_and_cleanup_reports_failure(self):
        from dsh_desktop import desktop_tools as d
        unique = uuid.uuid4().hex
        title = 'DSH temporary unsaved editor ' + unique
        prompt = 'DSH temporary save prompt ' + unique
        proc = subprocess.Popen([sys.executable, __file__, '--child', unique],
                                creationflags=subprocess.CREATE_NO_WINDOW)
        hwnd = 0
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                hwnd = win32gui.FindWindow(None, title)
                if hwnd:
                    break
                time.sleep(0.1)
            self.assertTrue(hwnd, 'temporary editor window did not appear')
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            owned_pids = {proc.pid} | {p.pid for p in psutil.Process(proc.pid).children(recursive=True)}
            self.assertIn(pid, owned_pids)
            key = d._current_window_key(hwnd)
            self.assertTrue(key)
            scope = 'dialog-' + unique
            d._STATE._launched_windows.append({'hwnd': hwnd, 'window_key': key, 'pid': pid,
                                              'title': title, 'scope_owner': scope, 't': time.time()})
            result = d.cleanup_session_footprint(close_browser=False, scope_owner=scope, timeout_per_app=1.5)
            dialog = win32gui.FindWindow(None, prompt)
            self.assertFalse(result['cleanup_success'])
            self.assertTrue(result['failed_apps'])
            self.assertTrue(win32gui.IsWindow(hwnd), 'editor was unexpectedly closed')
            self.assertTrue(dialog, 'save confirmation is not visible')
            self.assertIsNone(proc.poll(), 'editor process was terminated')
            print('live_save_prompt: cleanup failed safely; editor and confirmation remain visible')
        finally:
            dialog = win32gui.FindWindow(None, prompt)
            if dialog:
                win32gui.PostMessage(dialog, win32con.WM_COMMAND, win32con.IDCANCEL, 0)
            if hwnd and win32gui.IsWindow(hwnd):
                win32gui.PostMessage(hwnd, win32con.WM_APP + 17, 0, 0)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.terminate()  # Only the dedicated synthetic test process.
                proc.wait(timeout=5)


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--child':
        child_window(sys.argv[2])
    else:
        unittest.main()
