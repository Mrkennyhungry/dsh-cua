"""Per-Monitor V2 DPI awareness — set once per process, before any window exists.

Why this module exists: the kernel measures windows (GetWindowRect / ClientToScreen)
and captures pixels (mss) in the same coordinate space. That only holds when the
process is DPI aware. Without it Windows virtualises coordinates by the system DPI,
so on a 125% display a click lands short of its target, and with two monitors at
different scales the error differs per monitor.

The DSH plugin path (dsh_bridge_cli) used to set nothing at all, which left the
whole kernel DPI-unaware; only the standalone app called SetProcessDpiAwareness(1)
(system aware, still wrong for mixed-DPI setups).
"""
from __future__ import annotations

import ctypes
import logging
import sys

logger = logging.getLogger(__name__)

# DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2, available on Windows 10 1703+.
_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)

_DONE = ""
_MODE = ""


def set_per_monitor_v2() -> str:
    """Make this process Per-Monitor V2 DPI aware. Idempotent.

    Returns the mode that won: "per_monitor_v2", "per_monitor", "system",
    "already_set", or "unavailable" (non-Windows). The call must happen before
    the first window is created; Windows ignores it afterwards.
    """
    global _DONE, _MODE
    if _DONE:
        return _MODE
    if sys.platform != "win32":
        _DONE = _MODE = "unavailable"
        return _MODE

    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetProcessDpiAwarenessContext.restype = ctypes.c_bool
        if user32.SetProcessDpiAwarenessContext(_PER_MONITOR_AWARE_V2):
            _DONE = _MODE = "per_monitor_v2"
            logger.info("DPI awareness: Per-Monitor V2")
            return _MODE
    except (AttributeError, OSError):
        pass

    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        # 2 = PROCESS_PER_MONITOR_DPI_AWARE
        if shcore.SetProcessDpiAwareness(2) == 0:
            _DONE = _MODE = "per_monitor"
            logger.info("DPI awareness: per-monitor (V1)")
            return _MODE
    except (AttributeError, OSError):
        pass

    try:
        if ctypes.WinDLL("user32").SetProcessDPIAware():
            _DONE = _MODE = "system"
            logger.warning("DPI awareness: system only; mixed-DPI monitors will be inaccurate")
            return _MODE
    except (AttributeError, OSError):
        pass

    _DONE = _MODE = "already_set"
    logger.info("DPI awareness: already set by an earlier call or the host process")
    return _MODE


def current_mode() -> str:
    """Return which mode set_per_monitor_v2() achieved (empty before it runs)."""
    return _MODE
