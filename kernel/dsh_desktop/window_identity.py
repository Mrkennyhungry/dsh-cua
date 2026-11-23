"""A window property expires with its HWND, including same-process handle reuse."""
import ctypes
import secrets
import threading

from .targets import RuntimeFault


class WindowIdentities:
    def __init__(self, epoch):
        self.property = f'DSHDoraemonRuntime_{epoch}'
        self._markers = {}
        self._lock = threading.Lock()
        self._api = ctypes.WinDLL('user32', use_last_error=True)
        self._api.GetPropW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        self._api.GetPropW.restype = ctypes.c_void_p
        self._api.SetPropW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_void_p]
        self._api.SetPropW.restype = ctypes.c_bool
        self._api.RemovePropW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        self._api.RemovePropW.restype = ctypes.c_void_p

    def acquire(self, hwnd, process_key):
        with self._lock:
            marker = self._api.GetPropW(hwnd, self.property)
            if not marker:
                marker = secrets.randbits(31) + 1
                if not self._api.SetPropW(hwnd, self.property, marker):
                    raise RuntimeFault('TARGET_IDENTITY_UNAVAILABLE', f'Cannot establish window identity, Win32 error {ctypes.get_last_error()}')
            self._markers[hwnd] = marker
            return process_key, marker

    def matches(self, hwnd, identity, process_key):
        return identity == (process_key, self._api.GetPropW(hwnd, self.property))

    def shutdown(self):
        with self._lock:
            for hwnd, marker in self._markers.items():
                if self._api.GetPropW(hwnd, self.property) == marker:
                    self._api.RemovePropW(hwnd, self.property)
            self._markers.clear()
