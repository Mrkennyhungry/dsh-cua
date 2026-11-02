"""DPI awareness must be Per-Monitor V2 and set exactly once."""

import ctypes
import unittest

from dsh_desktop import dpi


class DpiTests(unittest.TestCase):
    def test_first_call_reports_a_real_mode(self):
        mode = dpi.set_per_monitor_v2()
        self.assertIn(mode, {"per_monitor_v2", "per_monitor", "system", "already_set", "unavailable"})

    def test_second_call_is_idempotent(self):
        first = dpi.set_per_monitor_v2()
        second = dpi.set_per_monitor_v2()
        self.assertEqual(first, second)

    def test_process_is_per_monitor_aware(self):
        dpi.set_per_monitor_v2()
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
            user32.AreDpiAwarenessContextsEqual.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            user32.AreDpiAwarenessContextsEqual.restype = ctypes.c_bool
            context = user32.GetThreadDpiAwarenessContext()
            v2 = ctypes.c_void_p(-4)
            per_monitor = ctypes.c_void_p(-3)
            self.assertTrue(
                user32.AreDpiAwarenessContextsEqual(context, v2)
                or user32.AreDpiAwarenessContextsEqual(context, per_monitor),
                "the process must be per-monitor DPI aware, not system or unaware",
            )
        except AttributeError:
            # Pre-1703 Windows: only the fallback modes exist.
            self.assertIn(dpi.current_mode(), {"system", "already_set"})

    def test_records_the_winning_mode(self):
        dpi.set_per_monitor_v2()
        self.assertEqual(dpi.current_mode(), dpi.set_per_monitor_v2())
        self.assertNotEqual(dpi.current_mode(), "")


if __name__ == "__main__":
    unittest.main()
