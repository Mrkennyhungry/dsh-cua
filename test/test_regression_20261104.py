"""Regression for the 2026-11-04 audit: clipboard ctypes, decoration prefixes,
and inline image size. These all failed silently in real 64-bit / 4K sessions."""
import base64
import ctypes
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np


class ClipboardCtypesTests(unittest.TestCase):
    """_read_clipboard_text must declare pointer-sized prototypes itself.

    type_text reads the clipboard BEFORE _write_clipboard_text has configured
    the process-wide ctypes prototype, so a fresh kernel used to crash with
    ArgumentError: int too long to convert on its first paste.
    """

    def test_read_declares_globallock_argtypes_without_a_prior_write(self):
        from dsh_desktop.game import input as gi

        calls = {}

        class FakeFunc:
            def __init__(self, name):
                self.name = name
                self.argtypes = None
                self.restype = None

            def __call__(self, *args):
                calls[self.name] = args
                if self.name == 'GlobalLock':
                    return 0x1234
                if self.name == 'GetClipboardData':
                    return 0x1_0000_0000  # pointer-sized handle above 2**31
                if self.name == 'OpenClipboard':
                    return 1
                return 0

        fake_kernel32 = type('K', (), {n: FakeFunc(n) for n in
                                       ('GlobalLock', 'GlobalUnlock')})()
        fake_user32 = type('U', (), {n: FakeFunc(n) for n in
                                     ('OpenClipboard', 'GetClipboardData', 'CloseClipboard')})()

        class FakeWindll:
            kernel32 = fake_kernel32
            user32 = fake_user32

        with patch.object(gi.ctypes, 'windll', FakeWindll()), \
             patch.object(gi.ctypes, 'wstring_at', return_value='probe-text'):
            result = gi._read_clipboard_text()

        self.assertEqual(result, 'probe-text')
        self.assertEqual(fake_kernel32.GlobalLock.argtypes, [ctypes.c_void_p])
        self.assertEqual(fake_kernel32.GlobalLock.restype, ctypes.c_void_p)
        self.assertEqual(fake_user32.GetClipboardData.argtypes, [ctypes.c_uint])
        self.assertIsInstance(calls['GlobalLock'][0], ctypes.c_void_p)

    def test_oversized_handle_never_raises_out_of_the_helper(self):
        from dsh_desktop.game import input as gi

        class Boom:
            argtypes = None
            restype = None

            def __call__(self, *args):
                raise ctypes.ArgumentError('int too long to convert')

        class FakeFunc:
            argtypes = None
            restype = None

            def __call__(self, *args):
                return 1

        class FakeWindll:
            kernel32 = type('K', (), {'GlobalLock': Boom(), 'GlobalUnlock': FakeFunc()})()
            user32 = type('U', (), {
                'OpenClipboard': FakeFunc(),
                'GetClipboardData': type('G', (), {
                    'argtypes': None, 'restype': None,
                    '__call__': lambda self, *a: 0x1_0000_0000})(),
                'CloseClipboard': FakeFunc()})()

        with patch.object(gi.ctypes, 'windll', FakeWindll()):
            self.assertIsNone(gi._read_clipboard_text())


class DecorationPrefixTests(unittest.TestCase):
    """Both the bare 0.4.5 class name and the suffixed current name must filter."""

    def setUp(self):
        import win32gui
        self._orig = win32gui.GetClassName
        self._classes = {}

        class Hwnd:
            def __init__(self, c):
                self.c = c

        self.Hwnd = Hwnd
        win32gui.GetClassName = lambda h: h.c

    def tearDown(self):
        import win32gui
        win32gui.GetClassName = self._orig

    def test_matches_all_known_forms(self):
        from dsh_desktop.decorations import is_decoration_window
        for name in ('DesktopCursorOverlay',
                     'DesktopCursorOverlay_1234_5678',
                     'DshDoraemonCommander_32276_2166090575120',
                     'DesktopWindowGlow'):
            with self.subTest(name=name):
                self.assertTrue(is_decoration_window(self.Hwnd(name)))

    def test_does_not_swallow_application_windows(self):
        from dsh_desktop.decorations import is_decoration_window
        for name in ('Chrome_WidgetWin_1', 'Notepad', 'DesktopCursorOverlayEvil'):
            with self.subTest(name=name):
                self.assertFalse(is_decoration_window(self.Hwnd(name)))


class InlineImageSizeTests(unittest.TestCase):
    """A 4K high-entropy PNG must never be returned as a >5 MB inline payload."""

    def test_large_png_is_downscaled_and_reencoded(self):
        from dsh_desktop.controller import AppController
        rng = np.random.default_rng(11)
        frame = rng.integers(0, 256, (2160, 3840, 3), dtype=np.uint8)
        tmp = Path(tempfile.gettempdir()) / 'dsh_inline_probe.png'
        cv2.imwrite(str(tmp), frame)
        self.addCleanup(tmp.unlink, missing_ok=True)
        self.assertGreater(tmp.stat().st_size, AppController.INLINE_IMAGE_MAX_BYTES)

        ctrl = object.__new__(AppController)
        result = ctrl.read_image(str(tmp))
        self.assertNotIn('error', result)
        url = result['content'][1]['image_url']['url']
        self.assertTrue(url.startswith('data:image/jpeg;base64,'))
        raw = base64.b64decode(url.split(',', 1)[1])
        self.assertLessEqual(len(raw), AppController.INLINE_IMAGE_MAX_BYTES)
        decoded = cv2.imdecode(np.frombuffer(raw, np.uint8), 1)
        self.assertLessEqual(max(decoded.shape[:2]), AppController.INLINE_IMAGE_MAX_DIM)

    def test_small_image_passes_through_unchanged(self):
        from dsh_desktop.controller import AppController
        small = np.full((40, 60, 3), 200, np.uint8)
        tmp = Path(tempfile.gettempdir()) / 'dsh_inline_small.png'
        cv2.imwrite(str(tmp), small)
        self.addCleanup(tmp.unlink, missing_ok=True)
        ctrl = object.__new__(AppController)
        result = ctrl.read_image(str(tmp))
        url = result['content'][1]['image_url']['url']
        self.assertTrue(url.startswith('data:image/png;base64,'))


if __name__ == '__main__':
    unittest.main()
